from __future__ import annotations

import copy
import json
from typing import Any

from .util import new_id, sha256_json, sha256_text

HASH_PLACEHOLDER = "a" * 64

CRITICAL_CONTEXT_PATHS = {
    "payload.project_definition", "payload.project_subgraph", "payload.proposal_contract",
    "payload.argument_graph_seed", "payload.argument_graph", "payload.architecture_candidate",
    "payload.narrative_architecture", "payload.section_contract", "payload.confirmed_plan",
    "payload.approved_blueprint", "payload.blueprint_candidate",
    "payload.classification_candidate", "payload.package_candidate", "payload.scheme_candidate",
    "payload.project_definition_candidate", "payload.proposal_contract_candidate",
    "payload.fact_candidates", "payload.template_candidate", "payload.revision_plan_candidate",
    "payload.blueprint_candidate", "payload.synthesis_candidate", "payload.result_package",
    "payload.content_candidate", "payload.polished_candidate", "payload.candidate_sections",
    "payload.candidate_document", "payload.document_section_map",
    "payload.prior_section_digest", "payload.revision_findings",
}


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
                self._set_path_if_valid(prompt_id, envelope, path, value, strict=True)
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
        sql = "SELECT id,prompt_id,input_json,output_json,created_at FROM prompt_runs WHERE project_id=? AND prompt_id IN ('P-WRITE-CONTENT','P-EXPRESSION-POLISH') AND status='PASS'"
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
            latest_by_section[section_id] = {"run_id": row["id"], "prompt_id": row.get("prompt_id"), "section": section, "candidate": candidate}
        return list(latest_by_section.values())


    @staticmethod
    def _prior_section_digest(candidates: list[dict[str, Any]], current_section_id: str | None = None) -> list[dict[str, Any]]:
        digests: list[dict[str, Any]] = []
        for item in candidates:
            section = item.get("section") or {}
            if current_section_id and section.get("section_id") == current_section_id:
                continue
            candidate = item.get("candidate") or {}
            advancement = candidate.get("claim_advancement") or {}
            paragraphs = [p for p in candidate.get("paragraphs", []) if isinstance(p, dict)]
            signatures: list[str] = []
            for paragraph in paragraphs:
                text = "".join(str(paragraph.get("text") or "").split())
                if text:
                    signatures.append(sha256_text(text)[:16])
            digests.append({
                "section_id": str(section.get("section_id") or ""),
                "title": str(section.get("title") or section.get("section_id") or "已生成章节"),
                "advanced_claim_ids": [str(x) for x in advancement.get("advanced_claim_ids", []) if x],
                "new_information_keys": [str(x) for x in advancement.get("new_information_keys", []) if x],
                "paragraph_roles": [str(p.get("paragraph_role") or "") for p in paragraphs if p.get("paragraph_role")],
                "sentence_signatures": signatures,
            })
        return digests[-30:]

    @staticmethod
    def _integration_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
        allowed = ["candidate_id", "candidate_text", "paragraphs", "trace_links", "term_usage", "unresolved_items", "claim_advancement"]
        return {key: candidate.get(key, [] if key in {"paragraphs", "trace_links", "term_usage", "unresolved_items"} else ({} if key == "claim_advancement" else "")) for key in allowed}

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

    @staticmethod
    def _scoped_architecture(architecture: dict[str, Any] | None, contract: dict[str, Any] | None) -> dict[str, Any] | None:
        if not architecture or not contract:
            return architecture
        keep_ids = {
            str(contract.get("section_id") or ""),
            *[str(x) for x in contract.get("prerequisite_section_ids", []) if x],
            *[str(x) for x in contract.get("must_not_repeat_section_ids", []) if x],
        }
        scoped = copy.deepcopy(architecture)
        scoped["section_contracts"] = [
            item for item in architecture.get("section_contracts", [])
            if str(item.get("section_id") or "") in keep_ids
        ]
        if not scoped["section_contracts"]:
            scoped["section_contracts"] = [copy.deepcopy(contract)]
        return scoped

    @staticmethod
    def _scoped_plan(plan: dict[str, Any] | None, architecture: dict[str, Any] | None, contract: dict[str, Any] | None) -> dict[str, Any] | None:
        if not plan or not contract:
            return plan
        relevant_ids = {
            *[str(x) for x in contract.get("must_advance_claim_ids", []) if x],
            *[str(x) for x in contract.get("must_use_evidence_ids", []) if x],
        }
        scoped = copy.deepcopy(plan)
        scoped["target_section_ids"] = [str(contract.get("section_id"))]
        scoped["read_only_section_ids"] = [
            str(x) for x in plan.get("read_only_section_ids", [])
            if str(x) in set(contract.get("prerequisite_section_ids", []))
        ]
        scoped["protected_section_ids"] = [
            str(x) for x in plan.get("protected_section_ids", [])
            if str(x) == str(contract.get("section_id"))
        ]
        tasks = [
            item for item in plan.get("tasks", [])
            if str(item.get("objective") or "") == str(contract.get("argument_function") or "")
        ]
        if not tasks:
            tasks = [
                item for item in plan.get("tasks", [])
                if relevant_ids & {str(x) for x in item.get("required_input_ids", []) if x}
            ][:2]
        if not tasks and plan.get("tasks"):
            tasks = [plan["tasks"][0]]
        scoped["tasks"] = copy.deepcopy(tasks)
        task_ids = {str(item.get("revision_task_id")) for item in tasks if item.get("revision_task_id")}
        scoped["dependencies"] = [
            item for item in plan.get("dependencies", [])
            if str(item.get("from_task_id") or item.get("source_task_id") or "") in task_ids
            and str(item.get("to_task_id") or item.get("target_task_id") or "") in task_ids
        ]
        scoped["narrative_architecture"] = copy.deepcopy(architecture or plan.get("narrative_architecture"))
        return scoped

    @staticmethod
    def _scoped_project_subgraph(project_definition: dict[str, Any] | None, contract: dict[str, Any] | None) -> dict[str, Any] | None:
        if not project_definition or not contract:
            return None
        seed_ids = {
            *[str(x) for x in contract.get("must_advance_claim_ids", []) if x],
            *[str(x) for x in contract.get("must_use_evidence_ids", []) if x],
        }
        relations = list(project_definition.get("relations", []))
        expanded = set(seed_ids)
        for relation in relations:
            source = str(relation.get("source_id") or "")
            target = str(relation.get("target_id") or "")
            if source in seed_ids or target in seed_ids:
                expanded.update([source, target])
        items = [item for item in project_definition.get("items", []) if str(item.get("item_id")) in expanded]
        if not items:
            items = list(project_definition.get("items", []))[:6]
            expanded = {str(item.get("item_id")) for item in items}
        scoped_relations = [
            item for item in relations
            if str(item.get("source_id")) in expanded and str(item.get("target_id")) in expanded
        ]
        return {
            "item_ids": [str(item.get("item_id")) for item in items],
            "relation_ids": [str(item.get("relation_id")) for item in scoped_relations],
            "items": copy.deepcopy(items),
            "relations": copy.deepcopy(scoped_relations),
        }

    @staticmethod
    def _scoped_facts(facts: list[dict[str, Any]], contract: dict[str, Any] | None, profile_id: str) -> list[dict[str, Any]]:
        if not facts:
            return []
        relevant_ids = {
            *[str(x) for x in (contract or {}).get("must_advance_claim_ids", []) if x],
            *[str(x) for x in (contract or {}).get("must_use_evidence_ids", []) if x],
        }
        exact = [item for item in facts if str(item.get("claim_id")) in relevant_ids]
        internal = [item for item in facts if item.get("claim_type") != "PUBLIC_CLAIM" and item not in exact]
        public = [item for item in facts if item.get("claim_type") == "PUBLIC_CLAIM" and item not in exact]
        public_profiles = {"BACKGROUND_AND_SIGNIFICANCE", "LITERATURE_REVIEW", "INNOVATION", "REFERENCES", "EVALUATION"}
        selected = [*exact, *internal[:6]]
        # Preserve at least one accepted public claim in every writing context so
        # non-literature sections can still trace cross-section public evidence.
        # Evidence-heavy profiles receive a wider public slice, while other
        # profiles receive only one claim to keep weak-model context bounded.
        selected.extend(public[:6] if profile_id in public_profiles else public[:1])
        seen: set[str] = set()
        result = []
        for item in selected:
            claim_id = str(item.get("claim_id") or sha256_json(item))
            if claim_id not in seen:
                seen.add(claim_id)
                result.append(copy.deepcopy(item))
        return result

    def _compact_read_only_context(self, project: dict[str, Any], candidates: list[dict[str, Any]], current_section_id: str | None) -> list[dict[str, Any]]:
        sections = []
        for item in candidates[-12:]:
            source = item.get("section") or {}
            if current_section_id and str(source.get("section_id")) == current_section_id:
                continue
            candidate = item.get("candidate") or {}
            advancement = candidate.get("claim_advancement") or {}
            summary = (
                f"章节贡献：{advancement.get('section_contribution', '')}；"
                f"推进命题：{', '.join(str(x) for x in advancement.get('advanced_claim_ids', []))}；"
                f"新增信息键：{', '.join(str(x) for x in advancement.get('new_information_keys', []))}。"
            )
            sections.append({
                "section_id": str(source.get("section_id") or "section-context"),
                "section_key": str(source.get("section_key") or source.get("title") or "已生成章节"),
                "title": str(source.get("title") or "已生成章节"),
                "level": int(source.get("level") or 1),
                "text": summary,
                "text_hash": sha256_text(summary),
                "block_ids": [],
                "contains_table": False,
                "contains_formula": False,
                "contains_image": False,
                "contains_comment": False,
                "contains_revision": False,
                "security_level": project["security_level"],
            })
        return sections

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

    @staticmethod
    def _repair_override(state: dict[str, Any], producer_prompt: str) -> Any:
        overrides = state.get("repair_overrides") or {}
        section_id = str(state.get("active_section_id") or "").strip()
        if section_id:
            scoped = f"section:{section_id}:{producer_prompt}"
            if scoped in overrides:
                return overrides[scoped]
        return overrides.get(producer_prompt)

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
        if "section_profile" in payload:
            replacements.append(("payload.section_profile", self.pack.section_profile_for((current_section or {}).get("title"))))
        if "readiness_stage" in payload:
            readiness_stage = (
                "READY_FOR_SECTION_PLANNING"
                if state.get("workflow_type") == "WF-4_PROPOSAL_AUTHORING"
                else "READY_FOR_ARGUMENT_ARCHITECTURE"
            )
            replacements.append(("payload.readiness_stage", readiness_stage))
        if "linked_sections" in payload and docs:
            if prompt_id in {"P-ARGUMENT-ARCHITECTURE", "P-ARGUMENT-ARCHITECTURE-CRITIC", "P-REVISION-PLAN", "P-REVISION-PLAN-CRITIC", "P-WRITE-BLUEPRINT", "P-WRITE-BLUEPRINT-CRITIC", "P-WRITE-CONTENT", "P-WRITE-CRITIC", "P-EXPRESSION-POLISH", "P-EXPRESSION-CRITIC", "P-INTEGRATION-CRITIC"}:
                linked = [
                    section for document in docs if document.get("document_role") == "CURRENT_PROPOSAL"
                    for section in document.get("sections", []) if section.get("title") != "全文"
                ]
            else:
                linked = [section for document in docs for section in document.get("sections", [])]
            replacements.append(("payload.linked_sections", linked[:100]))
        if "current_sections" in payload and docs:
            replacements.append(("payload.current_sections", [s for d in docs if d.get("document_role") == "CURRENT_PROPOSAL" for s in d.get("sections", []) if s.get("title") != "全文"][:100]))
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
            "proposal_contract_candidate": ("P-PROJECT-DEFINITION-EXTRACT", "proposal_contract"),
            "proposal_contract": ("P-PROJECT-DEFINITION-EXTRACT", "proposal_contract"),
            "argument_graph_seed": ("P-PROJECT-DEFINITION-EXTRACT", "argument_graph_seed"),
            "argument_graph_candidate": ("P-ARGUMENT-ARCHITECTURE", "argument_architecture"),
            "argument_graph": ("P-ARGUMENT-ARCHITECTURE", "argument_architecture"),
            "architecture_candidate": ("P-ARGUMENT-ARCHITECTURE", None),
            "fact_candidates": ("P-FACT-EXTRACT", "fact_candidates"),
            "template_candidate": ("P-TEMPLATE-EXTRACT", "template"),
            "revision_plan_candidate": ("P-REVISION-PLAN", "revision_plan"),
            "blueprint_candidate": ("P-WRITE-BLUEPRINT", "blueprint"),
            "content_candidate": ("P-WRITE-CONTENT", None),
            "polished_candidate": ("P-EXPRESSION-POLISH", None),
            # candidate_document is assembled from all latest section candidates
            # below; a single expression result is not a document.
            "research_plan": ("P-PUBLIC-RESEARCH-PLAN", None),
            "synthesis_candidate": ("P-PUBLIC-RESEARCH-SYNTHESIS", None),
        }
        for field, (producer, key) in result_map.items():
            if field in payload:
                value = self._result(project["id"], producer, key)
                repair_override = self._repair_override(state, producer)
                if repair_override is not None:
                    value = repair_override
                if value is not None:
                    replacements.append((f"payload.{field}", value))

        project_definition = self._result(project["id"], "P-PROJECT-DEFINITION-EXTRACT", "project_definition")
        proposal_contract = self._result(project["id"], "P-PROJECT-DEFINITION-EXTRACT", "proposal_contract")
        argument_graph_seed = self._result(project["id"], "P-PROJECT-DEFINITION-EXTRACT", "argument_graph_seed")
        argument_graph = self._result(project["id"], "P-ARGUMENT-ARCHITECTURE", "argument_architecture") or argument_graph_seed
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
        narrative_architecture = (plan or {}).get("narrative_architecture") if isinstance(plan, dict) else None
        section_contract = None
        if narrative_architecture and current_section:
            for contract in narrative_architecture.get("section_contracts", []):
                if contract.get("section_id") == current_section.get("section_id") or contract.get("title") == current_section.get("title"):
                    section_contract = contract
                    break
        blueprint = self._repair_override(state, "P-WRITE-BLUEPRINT")
        if blueprint is None:
            blueprint = self._result(project["id"], "P-WRITE-BLUEPRINT", "blueprint")
        content_candidates = self._content_candidates(project["id"], workflow_id if prompt_id == "P-INTEGRATION-CRITIC" else None)
        content = content_candidates[-1]["candidate"] if content_candidates else (self._result(project["id"], "P-EXPRESSION-POLISH") or self._result(project["id"], "P-WRITE-CONTENT"))
        safe_package = self._result(project["id"], "P-SAFE-ONLINE-PACKAGE")
        research_synthesis = self._result(project["id"], "P-PUBLIC-RESEARCH-SYNTHESIS")

        if "proposal_contract" in payload and proposal_contract:
            replacements.append(("payload.proposal_contract", proposal_contract))
        if "proposal_contract_candidate" in payload and proposal_contract:
            replacements.append(("payload.proposal_contract_candidate", proposal_contract))
        if "argument_graph_seed" in payload and argument_graph_seed:
            replacements.append(("payload.argument_graph_seed", argument_graph_seed))
        if "argument_graph" in payload and argument_graph:
            replacements.append(("payload.argument_graph", argument_graph))
        if "argument_graph_candidate" in payload and argument_graph:
            replacements.append(("payload.argument_graph_candidate", argument_graph))
        if "narrative_architecture" in payload and narrative_architecture:
            replacements.append(("payload.narrative_architecture", narrative_architecture))
        if "section_contract" in payload and section_contract:
            replacements.append(("payload.section_contract", section_contract))

        section_prompt_ids = {
            "P-WRITE-BLUEPRINT", "P-WRITE-BLUEPRINT-CRITIC", "P-WRITE-CONTENT",
            "P-WRITE-CRITIC", "P-EXPRESSION-POLISH", "P-EXPRESSION-CRITIC",
        }
        if prompt_id in section_prompt_ids:
            profile_id = str((payload.get("section_profile") or {}).get("profile_id") or (section_contract or {}).get("profile_id") or "")
            scoped_facts = self._scoped_facts(facts, section_contract, profile_id)
            # Facts can be scoped even before the planning workflow has produced a
            # Section Contract.  This keeps accepted public evidence available to
            # ad-hoc previews while still bounding the context for weak models.
            for field in ("confirmed_facts", "fact_context", "existing_facts"):
                if field in payload:
                    replacements.append((f"payload.{field}", scoped_facts))
            if section_contract:
                scoped_architecture = self._scoped_architecture(narrative_architecture, section_contract)
                scoped_plan = self._scoped_plan(plan, scoped_architecture, section_contract)
                scoped_subgraph = self._scoped_project_subgraph(project_definition, section_contract)
                if "narrative_architecture" in payload and scoped_architecture:
                    replacements.append(("payload.narrative_architecture", scoped_architecture))
                if "confirmed_plan" in payload and scoped_plan:
                    replacements.append(("payload.confirmed_plan", scoped_plan))
                if "project_subgraph" in payload and scoped_subgraph:
                    replacements.append(("payload.project_subgraph", scoped_subgraph))

        if "project_subgraph" in payload and project_definition and prompt_id not in section_prompt_ids:
            replacements.append(("payload.project_subgraph", {"item_ids": [x["item_id"] for x in project_definition.get("items", [])], "relation_ids": [x["relation_id"] for x in project_definition.get("relations", [])], "items": project_definition.get("items", []), "relations": project_definition.get("relations", [])}))
        for field in ["confirmed_facts", "fact_context", "existing_facts"]:
            if field in payload and facts and prompt_id not in section_prompt_ids:
                replacements.append((f"payload.{field}", facts))
        if "fact_package" in payload and facts:
            replacements.append(("payload.fact_package", {"schema_version": "2.0", "project_id": project["id"], "version": 1, "claims": facts, "conflicts": [], "package_hash": context_hash, "security_level": project["security_level"]}))
        if "template_context" in payload and template:
            replacements.append(("payload.template_context", template))
        if "confirmed_plan" in payload and plan and prompt_id not in section_prompt_ids:
            replacements.append(("payload.confirmed_plan", plan))
        if "approved_blueprint" in payload and blueprint:
            replacements.append(("payload.approved_blueprint", blueprint))
        if "content_candidate" in payload:
            raw_content = self._repair_override(state, "P-WRITE-CONTENT")
            if raw_content is None:
                raw_content = self._result(project["id"], "P-WRITE-CONTENT")
            if raw_content:
                replacements.append(("payload.content_candidate", raw_content))
        if "polished_candidate" in payload:
            polished = self._repair_override(state, "P-EXPRESSION-POLISH")
            if polished is None:
                polished = self._result(project["id"], "P-EXPRESSION-POLISH")
            if polished:
                replacements.append(("payload.polished_candidate", polished))
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
        if "result_package" in payload and research_synthesis:
            source_ids = sorted({
                str(ref.get("source_id"))
                for claim in research_synthesis.get("claims", [])
                if isinstance(claim, dict)
                for ref in claim.get("source_refs", [])
                if isinstance(ref, dict) and ref.get("source_id")
            })
            request_hash = sha256_json(safe_package or {"project_id": project["id"], "task": "PUBLIC_RESEARCH"})
            result_core = {
                "claims": research_synthesis.get("claims", []),
                "raw_text": json.dumps(research_synthesis, ensure_ascii=False, sort_keys=True),
                "source_ids": source_ids,
            }
            package_id = "online-result-" + sha256_json(result_core)[:16]
            result_package = {
                "package_id": package_id,
                "request_hash": request_hash,
                **result_core,
                "manifest_hash": sha256_json({"package_id": package_id, "request_hash": request_hash, **result_core}),
            }
            replacements.append(("payload.result_package", result_package))
            if "transfer_manifest" in payload:
                replacements.append(("payload.transfer_manifest", {
                    "package_id": package_id,
                    "request_hash": request_hash,
                    "content_hash": result_package["manifest_hash"],
                    "approved_by": "outbound-security-approval",
                    "approved_at": "2026-01-01T00:00:00Z",
                    "expires_at": None,
                }))
        if "trace_links" in payload and content_candidates:
            replacements.append(("payload.trace_links", [link for item in content_candidates for link in item["candidate"].get("trace_links", [])]))
        elif "trace_links" in payload and content:
            replacements.append(("payload.trace_links", content.get("trace_links", [])))
        if "prior_section_digest" in payload:
            replacements.append(("payload.prior_section_digest", self._prior_section_digest(
                content_candidates,
                str((current_section or {}).get("section_id") or "") or None,
            )))
        if "revision_findings" in payload:
            if prompt_id == "P-ARGUMENT-ARCHITECTURE":
                findings = list((state or {}).get("argument_revision_findings", []) or [])
            elif prompt_id == "P-REVISION-PLAN":
                findings = list((state or {}).get("planning_revision_findings", []) or [])
            else:
                active_section_id = str((current_section or {}).get("section_id") or "")
                repair_ids = {str(x) for x in (state or {}).get("integration_repair_section_ids", []) if x}
                findings = list((state or {}).get("integration_repair_findings", []) or []) if active_section_id in repair_ids else []
            replacements.append(("payload.revision_findings", findings))
        if "read_only_context" in payload and content_candidates:
            # Only semantic digests of previous chapters are sent to a section
            # writer.  Full prior prose caused quadratic context growth and made
            # weak/short-context models repeat text rather than advance claims.
            replacements.append(("payload.read_only_context", self._compact_read_only_context(
                project,
                content_candidates,
                str((current_section or {}).get("section_id") or "") or None,
            )))
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
            by_id = {str(section.get("section_id")): section for section in proposal_sections if section.get("section_id")}
            by_title = {str(section.get("title")): section for section in proposal_sections if section.get("title")}
            planned_sections = []
            for contract in (narrative_architecture or {}).get("section_contracts", []):
                if not isinstance(contract, dict) or contract.get("placement") == "OMIT":
                    continue
                section = by_id.get(str(contract.get("section_id"))) or by_title.get(str(contract.get("title")))
                if section:
                    planned_sections.append(section)
            proposal_sections = planned_sections or proposal_sections
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
            raw_instruction = config.get("task_instruction")
            if isinstance(raw_instruction, dict):
                objective = str(raw_instruction.get("objective") or project.get("description") or project.get("name") or "完成指定任务").strip()
            else:
                objective = str(raw_instruction or project.get("description") or project.get("name") or "完成指定任务").strip()
            if isinstance(payload.get("task_instruction"), dict):
                section_ids = [
                    str(section.get("section_id"))
                    for document in docs
                    if document.get("document_role") == "CURRENT_PROPOSAL"
                    for section in document.get("sections", [])
                    if section.get("section_id")
                ]
                replacements.append((
                    "payload.task_instruction",
                    self._structured_task_instruction(objective, section_ids, config, raw_instruction=raw_instruction),
                ))
            else:
                replacements.append(("payload.task_instruction", objective))
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
            self._set_path_if_valid(prompt_id, envelope, path, value, strict=path in CRITICAL_CONTEXT_PATHS)

    @staticmethod
    def _structured_task_instruction(
        instruction_text: str,
        section_ids: list[str],
        config: dict[str, Any],
        *,
        raw_instruction: Any = None,
    ) -> dict[str, Any]:
        source = config.get("task_instruction_structured")
        if not isinstance(source, dict) and isinstance(raw_instruction, dict):
            source = raw_instruction
        source = source if isinstance(source, dict) else {}

        def strings(value: Any) -> list[str]:
            if isinstance(value, (list, tuple)):
                return [str(item).strip() for item in value if str(item).strip()]
            if value is None:
                return []
            value = str(value).strip()
            return [value] if value else []

        defaults = {
            "specific_requirements": list(config.get("specific_requirements") or [
                "按已确认的任务范围与对象合同完成当前阶段产物",
                "所有实质结论使用可核验来源并保留来源绑定",
                "创新与结论按证据强度表述",
            ]),
            "must_preserve": list(config.get("must_preserve") or [
                "已确认的项目事实、约束、章节合同和人工决策",
                "未提供或未核验的事实保持UNKNOWN",
            ]),
            "forbidden_changes": list(config.get("forbidden_changes") or [
                "不得虚构论文、专利、数据、合作关系或预实验结果",
                "不得把待验证主张写成既有结论",
            ]),
            "acceptance_preferences": list(config.get("acceptance_preferences") or [
                "问题—差距—命题—方法—实验—成果形成闭环",
                "章节之间不重复、不串稿且可追溯",
            ]),
            "priority_order": list(config.get("priority_order") or [
                "事实与来源正确",
                "研究逻辑闭环",
                "方法和实验可验证",
                "表达与版式质量",
            ]),
        }
        requirements = strings(source.get("specific_requirements")) or strings(source.get("constraints")) or defaults["specific_requirements"]
        deliverables = strings(source.get("deliverables"))
        acceptance_preferences = strings(source.get("acceptance_preferences")) or deliverables or defaults["acceptance_preferences"]
        task_type = str(source.get("task_type") or "DRAFT_FROM_PROJECT_DEFINITION")
        allowed_task_types = {
            "COPY_EDIT_ONLY", "SUBSTANTIVE_REVISION", "DRAFT_FROM_PROJECT_DEFINITION",
            "PUBLIC_RESEARCH", "PUBLIC_TEMPLATE_ANALYSIS", "GENERIC_LANGUAGE_ASSIST",
        }
        if task_type not in allowed_task_types:
            task_type = "DRAFT_FROM_PROJECT_DEFINITION"
        core = {
            "schema_version": "2.0",
            "task_instruction_id": str(source.get("task_instruction_id") or "instruction-" + sha256_json({"objective": instruction_text, "sections": section_ids})[:16]),
            "task_type": task_type,
            "objective": str(source.get("objective") or instruction_text).strip() or instruction_text,
            "target_section_ids": strings(source.get("target_section_ids")) or list(section_ids),
            "specific_requirements": requirements,
            "must_preserve": strings(source.get("must_preserve")) or defaults["must_preserve"],
            "forbidden_changes": strings(source.get("forbidden_changes")) or defaults["forbidden_changes"],
            "acceptance_preferences": acceptance_preferences,
            "priority_order": strings(source.get("priority_order")) or defaults["priority_order"],
        }
        core["instruction_hash"] = sha256_json(core)
        return core

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

    def _set_path_if_valid(self, prompt_id: str, envelope: dict[str, Any], dotted_path: str, value: Any, *, strict: bool = False) -> bool:
        candidate = copy.deepcopy(envelope)
        parts = dotted_path.split(".")
        node = candidate
        for part in parts[:-1]:
            if part not in node or not isinstance(node[part], dict):
                if strict:
                    raise ValueError(f"Critical context path does not exist for {prompt_id}: {dotted_path}")
                return False
            node = node[part]
        node[parts[-1]] = value
        errors = self.pack.validate(prompt_id, "input", candidate)
        if errors:
            if strict:
                raise ValueError(f"Critical context replacement failed for {prompt_id} {dotted_path}: " + "; ".join(errors[:10]))
            return False
        envelope.clear()
        envelope.update(candidate)
        return True
