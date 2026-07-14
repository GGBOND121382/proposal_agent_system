from __future__ import annotations

import copy
import json
from typing import Any

from .util import new_id, sha256_json

HASH_PLACEHOLDER = "a" * 64


class ContextBuilder:
    """Builds schema-valid prompt envelopes from project state.

    The normal replay input is used as a typed seed. Real project objects replace seed
    values only when the resulting envelope still validates against the prompt schema.
    This makes partial projects runnable without allowing malformed context to leak into
    the model call.
    """

    def __init__(self, db, pack):
        self.db = db
        self.pack = pack

    def build(self, prompt_id: str, project_id: str, *, workflow_id: str | None = None, workflow_state: dict[str, Any] | None = None, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        project = self.db.fetchone("SELECT * FROM projects WHERE id=?", (project_id,))
        if not project:
            raise KeyError(f"Project not found: {project_id}")
        config = json.loads(project["config_json"])
        docs = self._documents(project_id)
        context_hash = sha256_json({"project": project, "documents": [d["document_hash"] for d in docs], "workflow_state": workflow_state or {}})
        envelope = self.pack.replay_input(prompt_id)
        envelope = self._replace_seed_values(envelope, project_id, context_hash)
        envelope["task"]["task_id"] = new_id("task")
        envelope["task"]["current_step"] = prompt_id.removeprefix("P-").replace("-", "_")
        if workflow_state and workflow_state.get("workflow_type"):
            workflow_type = workflow_state["workflow_type"]
            envelope["task"]["workflow_type"] = workflow_type.split("_", 1)[1] if workflow_type.startswith("WF-") and "_" in workflow_type else workflow_type
        required_environment = self._required_environment(prompt_id, workflow_state)
        execution_level = "PUBLIC" if required_environment == "ONLINE_PUBLIC" else project["security_level"]
        envelope["security_context"].update(
            {
                "project_security_level": execution_level,
                "input_max_security_level": execution_level,
                "required_environment": required_environment,
                "allowed_model_endpoint_ids": self._allowed_endpoints(project["security_level"], config, prompt_id),
                "prohibited_fields": config.get("prohibited_external_fields", []),
                "recipient_scope": config.get("recipient_scope", ["内部用户"]),
                "online_transfer_approval_status": self._online_approval_status(workflow_id),
            }
        )
        envelope["scope"]["project_id"] = project_id
        self._apply_common_payload(envelope, prompt_id, project, config, docs, context_hash, workflow_state or {}, workflow_id)
        if overrides:
            for path, value in overrides.items():
                self._set_path_if_valid(prompt_id, envelope, path, value)
        errors = self.pack.validate(prompt_id, "input", envelope)
        if errors:
            raise ValueError("Context builder produced invalid input: " + "; ".join(errors[:10]))
        return envelope

    def _documents(self, project_id: str) -> list[dict[str, Any]]:
        rows = self.db.fetchall("SELECT parsed_json,document_hash,role,security_level FROM documents WHERE project_id=? ORDER BY created_at", (project_id,))
        documents: list[dict[str, Any]] = []
        for row in rows:
            document = json.loads(row["parsed_json"])
            # ``safe_filename`` is upload/storage metadata, not part of the strict
            # document_context schema used by prompts.  Passing it through makes
            # schema-guarded context replacement fail silently and leaves Replay
            # seed documents in the model input.
            document.pop("safe_filename", None)
            documents.append(document)
        return documents


    def sections(self, project_id: str, role: str | None = None) -> list[dict[str, Any]]:
        """Return parsed document sections in upload order for workflow orchestration."""
        return [
            section
            for document in self._documents(project_id)
            if role is None or document.get("document_role") == role
            for section in document.get("sections", [])
        ]

    def _content_candidates(self, project_id: str, workflow_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT id,input_json,output_json,created_at FROM prompt_runs WHERE project_id=? AND prompt_id='P-WRITE-CONTENT' AND status='PASS'"
        params: list[Any] = [project_id]
        if workflow_id:
            sql += " AND workflow_id=?"
            params.append(workflow_id)
        sql += " ORDER BY created_at,id"
        latest_by_section: dict[str, dict[str, Any]] = {}
        for row in self.db.fetchall(sql, tuple(params)):
            if not row.get("output_json"):
                continue
            input_data = json.loads(row["input_json"])
            output_data = json.loads(row["output_json"])
            section = input_data.get("payload", {}).get("source_section") or {}
            candidate = output_data.get("result") or {}
            section_id = section.get("section_id")
            if not section_id or not candidate.get("candidate_id"):
                continue
            latest_by_section[section_id] = {"run_id": row["id"], "section": section, "candidate": candidate}
        return list(latest_by_section.values())

    @staticmethod
    def _integration_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
        allowed = ["candidate_id", "candidate_text", "paragraphs", "trace_links", "term_usage", "unresolved_items"]
        return {key: candidate.get(key, [] if key in {"paragraphs", "trace_links", "term_usage", "unresolved_items"} else "") for key in allowed}

    def _candidate_document(self, project: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, Any]:
        sections = []
        for item in candidates:
            source = item["section"]
            candidate = item["candidate"]
            text = candidate.get("candidate_text", "")
            sections.append(
                {
                    "section_id": source["section_id"],
                    "section_key": source.get("section_key") or source.get("title") or source["section_id"],
                    "title": source.get("title", ""),
                    "level": source.get("level", 1),
                    "text": text,
                    "text_hash": sha256_json({"section_id": source["section_id"], "text": text}),
                    "block_ids": [paragraph.get("paragraph_id") for paragraph in candidate.get("paragraphs", []) if paragraph.get("paragraph_id")],
                    "contains_table": any(paragraph.get("text", "").startswith("[[TABLE]]") for paragraph in candidate.get("paragraphs", [])),
                    "contains_formula": False,
                    "contains_image": False,
                    "contains_comment": False,
                    "contains_revision": False,
                    "security_level": project["security_level"],
                }
            )
        return {
            "document_id": f"candidate-{project['id']}",
            "version": 1,
            "sections": sections,
            "security_level": project["security_level"],
        }

    def _latest_output(self, project_id: str, prompt_id: str) -> dict[str, Any] | None:
        row = self.db.fetchone(
            """SELECT content_json FROM artifacts
               WHERE project_id=? AND prompt_id=?
                 AND artifact_type IN ('PROMPT_OUTPUT','SKILL_ENRICHED_PROMPT_OUTPUT')
               ORDER BY version DESC,created_at DESC LIMIT 1""",
            (project_id, prompt_id),
        )
        return json.loads(row["content_json"]) if row else None

    def _result(self, project_id: str, prompt_id: str, key: str | None = None) -> Any:
        output = self._latest_output(project_id, prompt_id)
        if not output:
            return None
        result = output.get("result")
        return result.get(key) if key and isinstance(result, dict) else result

    def _replace_seed_values(self, value: Any, project_id: str, context_hash: str) -> Any:
        if isinstance(value, dict):
            return {k: self._replace_seed_values(v, project_id, context_hash) for k, v in value.items()}
        if isinstance(value, list):
            return [self._replace_seed_values(v, project_id, context_hash) for v in value]
        if value == "project-001":
            return project_id
        if value == HASH_PLACEHOLDER:
            return context_hash
        return value

    def _required_environment(self, prompt_id: str, state: dict[str, Any] | None) -> str:
        env = self.pack.entry(prompt_id)["required_environment"]
        if env == "SAME_AS_ORIGINAL":
            return (state or {}).get("original_environment", "OFFLINE_LOCAL")
        return env

    def _allowed_endpoints(self, level: str, config: dict[str, Any], prompt_id: str) -> list[str]:
        required = self.pack.entry(prompt_id)["required_environment"]
        if required == "ONLINE_PUBLIC":
            return ["online-public-primary"] if config.get("internet_access_allowed", False) else []
        return config.get("allowed_model_endpoint_ids") or ["offline-primary"]

    def _online_approval_status(self, workflow_id: str | None) -> str:
        if not workflow_id:
            return "NOT_REQUIRED"
        row = self.db.fetchone(
            "SELECT status FROM gates WHERE workflow_id=? AND gate_type='OUTBOUND_SECURITY_APPROVAL' ORDER BY created_at DESC LIMIT 1",
            (workflow_id,),
        )
        return "APPROVED" if row and row["status"] == "APPROVED" else "NOT_REQUIRED"

    def _security_profile(self, project: dict[str, Any], config: dict[str, Any], context_hash: str) -> dict[str, Any]:
        return {
            "schema_version": "2.0",
            "profile_id": f"security-{project['id']}",
            "project_id": project["id"],
            "version": 1,
            "default_security_level": project["security_level"],
            "internet_access_allowed": bool(config.get("internet_access_allowed", False)),
            "anonymized_external_processing_allowed": bool(config.get("anonymized_external_processing_allowed", False)),
            "prohibited_external_fields": config.get("prohibited_external_fields", []),
            "allowed_public_topics": config.get("allowed_public_topics", []),
            "allowed_model_endpoint_ids": config.get("allowed_model_endpoint_ids") or ["offline-primary"],
            "outbound_approval_required": True,
            "import_approval_required": True,
            "final_content_approval_required": True,
            "final_export_approval_required": True,
            "log_content_policy": "FULL_IN_SECURE_ARTIFACT_ONLY",
            "retention_days": int(config.get("retention_days", 365)),
            "profile_hash": context_hash,
        }

    def _object_ref(self, object_id: str, object_type: str, security_level: str, context_hash: str, display_name: str) -> dict[str, Any]:
        return {"object_id": object_id, "object_type": object_type, "version": 1, "object_hash": context_hash, "security_level": security_level, "display_name": display_name}

    def _first_section(self, docs: list[dict[str, Any]], roles: set[str] | None = None) -> dict[str, Any] | None:
        for doc in docs:
            if roles and doc.get("document_role") not in roles:
                continue
            if doc.get("sections"):
                return doc["sections"][0]
        return None

    def _apply_common_payload(self, envelope: dict[str, Any], prompt_id: str, project: dict[str, Any], config: dict[str, Any], docs: list[dict[str, Any]], context_hash: str, state: dict[str, Any], workflow_id: str | None) -> None:
        payload = envelope["payload"]
        security_profile = self._security_profile(project, config, context_hash)
        for field in ["security_policy"]:
            if field in payload:
                self._set_path_if_valid(prompt_id, envelope, f"payload.{field}", security_profile)
        if "security_constraints" in payload:
            self._set_path_if_valid(prompt_id, envelope, "payload.security_constraints", envelope["security_context"])

        guide_docs = [d for d in docs if d.get("document_role") == "APPLICATION_GUIDE"] or docs
        source_docs = [d for d in docs if d.get("document_role") != "REFERENCE_PROPOSAL"] or docs
        reference_doc = next((d for d in docs if d.get("document_role") == "REFERENCE_PROPOSAL"), None)
        active_section_id = state.get("active_section_id")
        current_section = None
        if active_section_id:
            current_section = next(
                (section for doc in docs for section in doc.get("sections", []) if section.get("section_id") == active_section_id),
                None,
            )
        current_section = current_section or self._first_section(docs, {"CURRENT_PROPOSAL"}) or self._first_section(docs)

        replacements: list[tuple[str, Any]] = []
        if "guide_documents" in payload and guide_docs:
            replacements.append(("payload.guide_documents", guide_docs))
        if "source_documents" in payload and source_docs:
            replacements.append(("payload.source_documents", source_docs))
        if "reference_document" in payload and reference_doc:
            replacements.append(("payload.reference_document", reference_doc))
        if "source_section" in payload and current_section:
            replacements.append(("payload.source_section", current_section))
        if "linked_sections" in payload and docs:
            replacements.append(("payload.linked_sections", [s for d in docs for s in d.get("sections", [])][:100]))
        if "read_only_context" in payload and docs:
            replacements.append(("payload.read_only_context", [s for d in docs for s in d.get("sections", [])][1:100]))
        if "object_context" in payload and docs:
            first = docs[0]
            replacements.append(("payload.object_context", self._object_ref(first["document_id"], "SOURCE_DOCUMENT", first["security_level"], first["document_hash"], first["title"])))
        if "content_segments" in payload and docs:
            segments = []
            for doc in docs:
                for sec in doc.get("sections", [])[:50]:
                    segments.append({
                        "segment_id": sec["section_id"],
                        "text": sec["text"],
                        "source_ref": self._source_ref(doc, sec),
                        "security_level": doc["security_level"],
                    })
            if segments:
                replacements.append(("payload.content_segments", segments))
        if "source_spans" in payload and docs:
            spans = []
            for doc in docs:
                for sec in doc.get("sections", [])[:100]:
                    spans.append({"span_id": sec["section_id"], "text": sec["text"], "source_ref": self._source_ref(doc, sec)})
            if spans:
                replacements.append(("payload.source_spans", spans))
        if "section_tree" in payload and reference_doc:
            tree = [{"section_id": s["section_id"], "title": s["title"], "level": s["level"], "sequence": i + 1} for i, s in enumerate(reference_doc.get("sections", []))]
            replacements.append(("payload.section_tree", tree))
        if "document_structure" in payload and guide_docs:
            structure = [{"document_id": d["document_id"], "section_ids": [s["section_id"] for s in d.get("sections", [])]} for d in guide_docs]
            replacements.append(("payload.document_structure", structure))

        # Producer -> consumer mappings.
        result_map = {
            "classification_candidate": ("P-SECURITY-CLASSIFY", None),
            "package_candidate": ("P-SAFE-ONLINE-PACKAGE", None),
            "scheme_candidate": ("P-SCHEME-EXTRACT", "scheme_profile"),
            "scheme_profile": ("P-SCHEME-EXTRACT", "scheme_profile"),
            "project_definition_candidate": ("P-PROJECT-DEFINITION-EXTRACT", "project_definition"),
            "project_definition": ("P-PROJECT-DEFINITION-EXTRACT", "project_definition"),
            "fact_candidates": ("P-FACT-EXTRACT", "fact_candidates"),
            "template_candidate": ("P-TEMPLATE-EXTRACT", "template"),
            "revision_plan_candidate": ("P-REVISION-PLAN", "revision_plan"),
            "blueprint_candidate": ("P-WRITE-BLUEPRINT", "blueprint"),
            "content_candidate": ("P-WRITE-CONTENT", None),
            "candidate_document": ("P-WRITE-CONTENT", None),
            "research_plan": ("P-PUBLIC-RESEARCH-PLAN", None),
            "synthesis_candidate": ("P-PUBLIC-RESEARCH-SYNTHESIS", None),
            "result_package": ("P-PUBLIC-RESEARCH-SYNTHESIS", None),
        }
        for field, (producer, key) in result_map.items():
            if field in payload:
                value = self._result(project["id"], producer, key)
                repair_override = (state.get("repair_overrides") or {}).get(producer)
                if repair_override is not None:
                    value = repair_override
                if value is not None:
                    replacements.append((f"payload.{field}", value))

        project_definition = self._result(project["id"], "P-PROJECT-DEFINITION-EXTRACT", "project_definition")
        internal_facts = self._result(project["id"], "P-FACT-EXTRACT", "fact_candidates") or []
        public_claims = self._result(project["id"], "P-PUBLIC-RESEARCH-SYNTHESIS", "claims") or []
        import_review = self._result(project["id"], "P-ONLINE-RESULT-IMPORT-CRITIC") or {}
        accepted_public_ids = set(import_review.get("accepted_claim_ids") or [])
        if accepted_public_ids:
            public_claims = [claim for claim in public_claims if claim.get("claim_id") in accepted_public_ids]
        facts = [*internal_facts, *public_claims]
        scheme = self._result(project["id"], "P-SCHEME-EXTRACT", "scheme_profile")
        template = self._result(project["id"], "P-TEMPLATE-EXTRACT", "template")
        plan = self._result(project["id"], "P-REVISION-PLAN", "revision_plan")
        blueprint = self._result(project["id"], "P-WRITE-BLUEPRINT", "blueprint")
        content_candidates = self._content_candidates(project["id"], workflow_id if prompt_id == "P-INTEGRATION-CRITIC" else None)
        content = content_candidates[-1]["candidate"] if content_candidates else self._result(project["id"], "P-WRITE-CONTENT")
        safe_package = self._result(project["id"], "P-SAFE-ONLINE-PACKAGE")

        if "project_subgraph" in payload and project_definition:
            replacements.append(("payload.project_subgraph", {"item_ids": [x["item_id"] for x in project_definition.get("items", [])], "relation_ids": [x["relation_id"] for x in project_definition.get("relations", [])], "items": project_definition.get("items", []), "relations": project_definition.get("relations", [])}))
        for field in ["confirmed_facts", "fact_context", "existing_facts"]:
            if field in payload and facts:
                replacements.append((f"payload.{field}", facts))
        if "fact_package" in payload and facts:
            replacements.append(("payload.fact_package", {"schema_version": "2.0", "project_id": project["id"], "version": 1, "claims": facts, "conflicts": [], "package_hash": context_hash, "security_level": project["security_level"]}))
        if "template_context" in payload and template:
            replacements.append(("payload.template_context", template))
        if "confirmed_plan" in payload and plan:
            replacements.append(("payload.confirmed_plan", self._object_ref(plan.get("plan_id", new_id("plan")), "REVISION_PLAN", project["security_level"], sha256_json(plan), "已确认修改计划")))
        if "approved_blueprint" in payload and blueprint:
            replacements.append(("payload.approved_blueprint", self._object_ref(blueprint.get("blueprint_id", new_id("bp")), "BLUEPRINT", project["security_level"], sha256_json(blueprint), "已审查写作蓝图")))
        if "safe_online_package" in payload and safe_package:
            display_topics = "、".join((safe_package.get("allowed_context") or [])[:4])
            display_name = "批准的在线任务包" + (f"（{display_topics}）" if display_topics else "")
            replacements.append(("payload.safe_online_package", self._object_ref(safe_package.get("package_id", new_id("online")), "SAFE_ONLINE_PACKAGE", "PUBLIC", sha256_json(safe_package), display_name)))
            # The online planner must receive the approved PUBLIC task content, not merely
            # an opaque object reference.  This field contains only the deterministic,
            # sanitized Safe Online Package and is validated by the prompt input schema.
            replacements.append(("payload.safe_online_package_content", {
                "package_id": safe_package.get("package_id", new_id("online")),
                "task_type": safe_package.get("task_type", "PUBLIC_RESEARCH"),
                "task_description": safe_package.get("task_description", "公开资料检索"),
                "queries": list(safe_package.get("queries") or []),
                "allowed_context": list(safe_package.get("allowed_context") or []),
                "prohibited_inferences": list(safe_package.get("prohibited_inferences") or []),
                "prohibited_outputs": list(safe_package.get("prohibited_outputs") or []),
                "security_level": "PUBLIC",
            }))
        if "approved_safe_package" in payload and safe_package:
            replacements.append(("payload.approved_safe_package", self._object_ref(safe_package.get("package_id", new_id("online")), "SAFE_ONLINE_PACKAGE", "PUBLIC", sha256_json(safe_package), "批准的在线任务包")))
        if "trace_links" in payload and content_candidates:
            replacements.append(("payload.trace_links", [link for item in content_candidates for link in item["candidate"].get("trace_links", [])]))
        elif "trace_links" in payload and content:
            replacements.append(("payload.trace_links", content.get("trace_links", [])))
        if "candidate_sections" in payload and content_candidates:
            replacements.append(("payload.candidate_sections", [
                {"section_id": item["section"]["section_id"], "candidate": self._integration_candidate(item["candidate"])}
                for item in content_candidates
            ]))
        if "document_section_map" in payload:
            proposal_sections = [
                section
                for doc in docs
                if doc.get("document_role") == "CURRENT_PROPOSAL"
                for section in doc.get("sections", [])
                if section.get("level", 0) >= 1 and section.get("title") != "全文"
            ]
            candidate_ids = {item["section"]["section_id"]: item["candidate"].get("candidate_id") for item in content_candidates}
            if proposal_sections:
                replacements.append(("payload.document_section_map", [
                    {
                        "section_id": section["section_id"],
                        "title": section.get("title", ""),
                        "level": section.get("level", 1),
                        "candidate_id": candidate_ids.get(section["section_id"]),
                    }
                    for section in proposal_sections
                ]))
        if "candidate_document" in payload and content_candidates:
            replacements.append(("payload.candidate_document", self._candidate_document(project, content_candidates)))

        if "task_instruction" in payload and config.get("task_instruction"):
            replacements.append(("payload.task_instruction", config["task_instruction"]))
        if "recipient_scope" in payload:
            replacements.append(("payload.recipient_scope", config.get("recipient_scope", ["内部用户"])))
        if "allowed_topics" in payload:
            replacements.append(("payload.allowed_topics", config.get("allowed_public_topics", ["公开政策", "公开学术资料"])))
        if "prohibited_fields" in payload:
            replacements.append(("payload.prohibited_fields", config.get("prohibited_external_fields", [])))

        search_results = state.get("public_search_results")
        if search_results:
            if "retrieved_sources" in payload:
                replacements.append(("payload.retrieved_sources", search_results.get("sources", [])))
            if "extracted_passages" in payload:
                replacements.append(("payload.extracted_passages", search_results.get("passages", [])))
            if "public_sources" in payload:
                replacements.append(("payload.public_sources", search_results.get("sources", [])))

        for path, value in replacements:
            self._set_path_if_valid(prompt_id, envelope, path, value)

    def _source_ref(self, doc: dict[str, Any], sec: dict[str, Any]) -> dict[str, Any]:
        return {
            "source_id": sec["section_id"],
            "source_type": {
                "APPLICATION_GUIDE": "APPLICATION_GUIDE",
                "CURRENT_PROPOSAL": "CURRENT_PROPOSAL",
                "TECHNICAL_DESIGN": "TECHNICAL_MATERIAL",
                "EVIDENCE_MATERIAL": "EVIDENCE_MATERIAL",
                "REFERENCE_PROPOSAL": "REFERENCE_PROPOSAL",
            }.get(doc.get("document_role"), "HISTORICAL_DOCUMENT"),
            "document_version_id": doc["document_version_id"],
            "section_id": sec["section_id"],
            "span_start": None,
            "span_end": None,
            "quoted_text": sec["text"][:500],
            "source_hash": sec["text_hash"],
            "authority_rank": doc["authority_rank"],
            "security_level": doc["security_level"],
        }

    def _set_path_if_valid(self, prompt_id: str, envelope: dict[str, Any], dotted_path: str, value: Any) -> bool:
        candidate = copy.deepcopy(envelope)
        parts = dotted_path.split(".")
        node = candidate
        for part in parts[:-1]:
            if part not in node or not isinstance(node[part], dict):
                return False
            node = node[part]
        node[parts[-1]] = value
        if self.pack.validate(prompt_id, "input", candidate):
            return False
        envelope.clear()
        envelope.update(candidate)
        return True
