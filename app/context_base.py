from __future__ import annotations

import copy
import json
import re

import yaml
from typing import Any

from .task_instruction import instruction_text
from .proposal_constraints import extract_hard_constraints, merge_contract_constraints
from .util import new_id, sha256_json, sha256_text, utc_now

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
        # Once a full-proposal Integration Critic has passed, all downstream
        # consumers (especially final confidentiality review) must read the exact
        # frozen section manifest, not a project-wide mix of later retry outputs.
        if workflow_id is None:
            workflow_rows = self.db.fetchall(
                "SELECT state_json,status,updated_at,id FROM workflows "
                "WHERE project_id=? AND workflow_type='WF-4_PROPOSAL_AUTHORING' "
                "AND status='COMPLETED' ORDER BY updated_at DESC,id DESC",
                (project_id,),
            )
            for workflow_row in workflow_rows:
                state = json.loads(workflow_row.get("state_json") or "{}")
                if state.get("parent_workflow_id"):
                    continue
                reviews = [
                    item for item in state.get("full_proposal_review_history") or []
                    if isinstance(item, dict) and item.get("status") == "PASS"
                ]
                if not reviews:
                    continue
                manifest = reviews[-1].get("section_manifest") or []
                frozen: list[dict[str, Any]] = []
                for item in manifest:
                    run_id = str(item.get("polish_run_id") or "")
                    row = self.db.fetchone(
                        "SELECT id,prompt_id,input_json,output_json,created_at FROM prompt_runs "
                        "WHERE project_id=? AND id=? AND prompt_id='P-EXPRESSION-POLISH' AND status='PASS'",
                        (project_id, run_id),
                    )
                    if not row or not row.get("output_json"):
                        raise RuntimeError(f"Frozen Integration Critic candidate is unavailable: {run_id}")
                    input_data = json.loads(row["input_json"] or "{}")
                    output_data = json.loads(row["output_json"] or "{}")
                    section = (input_data.get("payload") or {}).get("source_section") or {}
                    candidate = output_data.get("result") or {}
                    if str(section.get("section_id") or "") != str(item.get("section_id") or ""):
                        raise RuntimeError(f"Frozen candidate section mismatch: {run_id}")
                    if str(candidate.get("candidate_id") or "") != str(item.get("candidate_id") or ""):
                        raise RuntimeError(f"Frozen candidate id mismatch: {run_id}")
                    frozen.append({
                        "run_id": row["id"],
                        "prompt_id": row.get("prompt_id"),
                        "section": section,
                        "candidate": candidate,
                    })
                if frozen:
                    return frozen

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
    def _candidate_body_text(candidate: dict[str, Any]) -> str:
        paragraphs = candidate.get("paragraphs") or []
        values: list[str] = []
        for paragraph in paragraphs:
            if isinstance(paragraph, dict):
                text = str(paragraph.get("text") or "").strip()
            else:
                text = str(paragraph or "").strip()
            if text:
                values.append(text)
        if values:
            return "\n\n".join(values)
        return str(candidate.get("candidate_text") or "").strip()

    @classmethod
    def _integration_candidate(cls, candidate: dict[str, Any]) -> dict[str, Any]:
        allowed = ["candidate_id", "candidate_text", "paragraphs", "trace_links", "term_usage", "unresolved_items", "claim_advancement"]
        result = {key: candidate.get(key, [] if key in {"paragraphs", "trace_links", "term_usage", "unresolved_items"} else ({} if key == "claim_advancement" else "")) for key in allowed}
        # Integration Critic, final security review and exporter must inspect the
        # same prose.  Paragraph blocks are the canonical reviewed representation;
        # candidate_text is deterministically rebuilt from them rather than trusting
        # a stale/meta summary left by a provider response.
        result["candidate_text"] = cls._candidate_body_text(candidate)
        return result

    def _candidate_document(self, project: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, Any]:
        sections = []
        for item in candidates:
            source = item["section"]
            candidate = item["candidate"]
            text = self._candidate_body_text(candidate)
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
        all_tasks = [item for item in plan.get("tasks", []) if isinstance(item, dict)]
        section_id = str(contract.get("section_id") or "")
        section_title = str(contract.get("title") or "")
        function = str(contract.get("argument_function") or "")

        def normalized(value: object) -> str:
            return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", str(value or "")).lower()

        function_key = normalized(function)
        title_key = normalized(section_title)
        scored: list[tuple[int, int, dict[str, Any]]] = []
        for index, item in enumerate(all_tasks):
            score = 0
            objective = str(item.get("objective") or "")
            objective_key = normalized(objective)
            if function_key and objective_key == function_key:
                score += 100
            elif function_key and (function_key in objective_key or objective_key in function_key):
                score += 60
            required = {str(x) for x in item.get("required_input_ids", []) if x}
            score += 20 * len(relevant_ids & required)
            searchable = normalized(
                " ".join(
                    [
                        objective,
                        *[str(x) for x in item.get("acceptance_rules", [])],
                        *[str(x) for x in item.get("issue_ids", [])],
                    ]
                )
            )
            if section_id and normalized(section_id) in searchable:
                score += 80
            if title_key and title_key in searchable:
                score += 50
            scored.append((score, index, item))

        positive = [row for row in scored if row[0] > 0]
        tasks = [row[2] for row in sorted(positive, key=lambda row: (-row[0], row[1]))[:2]]
        if not tasks:
            # A well-formed full proposal normally has one planning task per frozen
            # section contract.  Use the same deterministic order only when both
            # collections have the same cardinality; never fall back to task 0 for
            # every section, which caused cross-section task contamination.
            contracts = [
                item for item in (architecture or {}).get("section_contracts", [])
                if isinstance(item, dict) and item.get("section_id")
            ]
            if len(contracts) == len(all_tasks) and section_id:
                for index, item in enumerate(contracts):
                    if str(item.get("section_id")) == section_id:
                        tasks = [all_tasks[index]]
                        break
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
            # Missing contract evidence must be visible to the caller.  Returning
            # the first unrelated project objects hid broken aliases and allowed a
            # section to pass with evidence from another topic.
            return {
                "item_ids": [],
                "relation_ids": [],
                "items": [],
                "relations": [],
                "missing_seed_ids": sorted(seed_ids),
            }
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
        if "writing_mode" in payload:
            replacements.append(("payload.writing_mode", "DRAFT_FROM_PROJECT_DEFINITION"))
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
        if "original_object" in payload and docs:
            first = docs[0]
            replacements.append(("payload.original_object", {
                "object_type": "SOURCE_DOCUMENT",
                "object_id": first["document_id"],
                "object_hash": first["document_hash"],
                "content": {
                    "title": first.get("title") or first.get("filename") or "source document",
                    "document_role": first.get("document_role"),
                    "security_level": first.get("security_level"),
                    "sections": [
                        {"section_id": sec.get("section_id"), "title": sec.get("title"), "text": sec.get("text")}
                        for sec in (first.get("sections") or [])
                    ],
                },
            }))
        if "deterministic_findings" in payload:
            # An empty deterministic finding set is a real quality-check result.
            replacements.append(("payload.deterministic_findings", []))
        if "open_conflicts" in payload:
            # No unresolved source/fact conflicts is also an explicit workflow fact.
            replacements.append(("payload.open_conflicts", []))
        if "relation_matrix" in payload:
            matrix = yaml.safe_load((self.pack.root / "knowledge/relation_matrix.yaml").read_text(encoding="utf-8"))
            replacements.append(("payload.relation_matrix", {
                "version": str(matrix.get("version") or "2.0"),
                "allowed_relations": [list(item) for item in (matrix.get("allowed_relations") or [])],
            }))
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
                    text = str(sec.get("text") or "").strip()
                    if not text:
                        continue
                    spans.append({"span_id": sec["section_id"], "text": text, "source_ref": self._source_ref(doc, sec)})
            if spans:
                replacements.append(("payload.source_spans", spans))
        if "authority_rules" in payload:
            replacements.append(("payload.authority_rules", {
                "version": "2.0",
                "ordered_source_types": [
                    "USER_CONFIRMATION", "APPLICATION_GUIDE", "TASK_BOOK", "CONTRACT",
                    "CURRENT_PROPOSAL", "TECHNICAL_MATERIAL", "EVIDENCE_MATERIAL",
                    "HISTORICAL_DOCUMENT", "REFERENCE_PROPOSAL", "MODEL_INFERENCE",
                ],
            }))
        if "existing_facts" in payload:
            replacements.append(("payload.existing_facts", []))
        if "locked_facts" in payload:
            replacements.append(("payload.locked_facts", []))
        if "section_tree" in payload and reference_doc:
            tree = []
            level_stack: dict[int, str] = {}
            for section in reference_doc.get("sections", []):
                level = int(section.get("level") or 0)
                parent_id = None
                if level > 0:
                    parent_levels = [item for item in level_stack if item < level]
                    if parent_levels:
                        parent_id = level_stack[max(parent_levels)]
                tree.append({
                    "section_id": section["section_id"],
                    "title": section.get("title") or "未命名章节",
                    "level": level,
                    "parent_section_id": parent_id,
                })
                level_stack[level] = section["section_id"]
                for stale_level in [item for item in level_stack if item > level]:
                    level_stack.pop(stale_level, None)
            replacements.append(("payload.section_tree", tree))
        if "document_structure" in payload and guide_docs:
            structure = [
                {
                    "section_id": sec["section_id"],
                    "title": sec.get("title") or "全文",
                    "level": int(sec.get("level") or 0),
                    "text_hash": sec.get("text_hash") or sha256_json(sec.get("text") or ""),
                }
                for doc in guide_docs
                for sec in doc.get("sections", [])
            ]
            replacements.append(("payload.document_structure", structure))
        if "extraction_scope" in payload:
            replacements.append(("payload.extraction_scope", [
                "项目性质与适用范围",
                "执行周期与经费约束",
                "正文结构与篇幅要求",
                "图表和参考文献要求",
                "事实边界与禁止补写事项",
                "评审逻辑与验收要求",
            ]))
        if "style_summary" in payload:
            replacements.append(("payload.style_summary", {
                "paragraph_styles": [
                    "中文科技申请书正文段落：首行缩进、两端对齐、段前段后适度留白",
                    "图表题注独立成段并按章节连续编号",
                ],
                "heading_styles": [
                    "一级标题采用中文编号并突出显示",
                    "二级标题采用阿拉伯数字层级编号",
                    "三级标题简洁描述单一论证功能",
                ],
                "table_styles": [
                    "表格使用简洁网格或三线表结构",
                    "表头明确指标、对象、来源或验收方式",
                ],
            }))
        if "research_need" in payload:
            replacements.append((
                "payload.research_need",
                self._research_need(project, config, context_hash),
            ))
        if "source_items" in payload:
            source_items = [
                self._object_ref(
                    str(document.get("document_id") or f"doc-{index}"),
                    str(document.get("document_role") or "DOCUMENT"),
                    str(document.get("security_level") or project["security_level"]),
                    str(document.get("document_hash") or context_hash),
                    str(document.get("title") or document.get("document_id") or f"输入材料{index}"),
                )
                for index, document in enumerate(source_docs, start=1)
            ]
            replacements.append(("payload.source_items", source_items))
        if "target_task_type" in payload:
            replacements.append(("payload.target_task_type", "PUBLIC_RESEARCH"))
        if "source_summary" in payload:
            replacements.append((
                "payload.source_summary",
                self._source_summaries(source_docs, project),
            ))
        if "deterministic_scan" in payload:
            package_candidate = self._result(project["id"], "P-SAFE-ONLINE-PACKAGE") or {}
            package_text = json.dumps(package_candidate, ensure_ascii=False)
            forbidden_literals = [
                str(project.get("id") or ""),
                str(project.get("name") or ""),
                *[str(item) for item in config.get("prohibited_external_fields", [])],
            ]
            leaked = [item for item in forbidden_literals if item and item in package_text]
            replacements.append(("payload.deterministic_scan", {
                "passed": not leaked,
                "matched_rules": [
                    "PUBLIC_ONLY_CONTEXT",
                    "IDENTITY_FIELDS_REMOVED",
                    "INTERNAL_IDS_REMOVED",
                    "NO_PRIVATE_DATA_OR_UNVERIFIED_RESULTS",
                ],
                "redacted_fields": list(package_candidate.get("removed_fields") or leaked),
            }))
        if "task_type" in payload and prompt_id == "P-PUBLIC-RESEARCH-PLAN":
            replacements.append(("payload.task_type", "PUBLIC_RESEARCH"))
        if "known_public_sources" in payload:
            replacements.append((
                "payload.known_public_sources",
                self._known_public_sources(config),
            ))
        if "time_constraints" in payload:
            configured = config.get("research_time_constraints") or {}
            replacements.append(("payload.time_constraints", {
                "start_date": str(configured.get("start_date") or "2000-01-01"),
                "end_date": str(configured.get("end_date") or utc_now()[:10]),
                "freshness_required": bool(configured.get("freshness_required", True)),
            }))
        if "evidence_requirements" in payload:
            requirements = list(config.get("research_evidence_requirements") or [
                "优先使用同行评审论文、出版商页面、DOI元数据和官方数据集页面",
                "同时覆盖经典基础工作和近期工作，并记录检索日期",
                "每条来源保留标题、作者、年份、出版方、DOI或稳定URL",
                "提取可复用的数据集、基线、评价指标、统计检验和有效性威胁",
                "不得把公开工作的实验结论推断为本项目已有成果",
                "无法核验全文时仅使用可核验元数据和摘要，不补写细节",
            ])
            replacements.append(("payload.evidence_requirements", requirements))

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
        repair_result_keys = {
            "P-SCHEME-EXTRACT": "scheme_profile",
            "P-PROJECT-DEFINITION-EXTRACT": "project_definition",
            "P-FACT-EXTRACT": "fact_candidates",
            "P-TEMPLATE-EXTRACT": "template",
            "P-ARGUMENT-ARCHITECTURE": "argument_architecture",
            "P-REVISION-PLAN": "revision_plan",
            "P-WRITE-BLUEPRINT": "blueprint",
            "P-WRITE-CONTENT": None,
            "P-EXPRESSION-POLISH": None,
        }
        for field, (producer, key) in result_map.items():
            if field in payload:
                value = self._result(project["id"], producer, key)
                repair_override = self._repair_override(state, producer)
                if repair_override is not None and key == repair_result_keys.get(producer):
                    value = repair_override
                if value is not None:
                    replacements.append((f"payload.{field}", value))

        project_definition = self._result(project["id"], "P-PROJECT-DEFINITION-EXTRACT", "project_definition")
        proposal_contract = self._result(project["id"], "P-PROJECT-DEFINITION-EXTRACT", "proposal_contract")
        scheme = self._result(project["id"], "P-SCHEME-EXTRACT", "scheme_profile")
        hard_constraints = extract_hard_constraints(scheme)
        proposal_contract = merge_contract_constraints(proposal_contract, hard_constraints)
        argument_graph_seed = self._result(project["id"], "P-PROJECT-DEFINITION-EXTRACT", "argument_graph_seed")
        argument_graph = self._result(project["id"], "P-ARGUMENT-ARCHITECTURE", "argument_architecture") or argument_graph_seed
        internal_facts = self._result(project["id"], "P-FACT-EXTRACT", "fact_candidates") or []
        public_claims = self._result(project["id"], "P-PUBLIC-RESEARCH-SYNTHESIS", "claims") or []
        import_review = self._result(project["id"], "P-ONLINE-RESULT-IMPORT-CRITIC") or {}
        accepted_public_ids = set(import_review.get("accepted_claim_ids") or [])
        if accepted_public_ids:
            public_claims = [claim for claim in public_claims if claim.get("claim_id") in accepted_public_ids]
        facts = [*internal_facts, *public_claims]
        template = self._result(project["id"], "P-TEMPLATE-EXTRACT", "template")
        plan = self._result(project["id"], "P-REVISION-PLAN", "revision_plan")
        narrative_architecture = (plan or {}).get("narrative_architecture") if isinstance(plan, dict) else None
        section_contract = None
        if narrative_architecture and current_section:
            for contract in narrative_architecture.get("section_contracts", []):
                current_title = re.sub(r"^\s*[一二三四五六七八九十0-9]+[.、．）)]\s*", "", str(current_section.get("title") or "")).strip()
                contract_title = re.sub(r"^\s*[一二三四五六七八九十0-9]+[.、．）)]\s*", "", str(contract.get("title") or "")).strip()
                if (
                    contract.get("section_id") == current_section.get("section_id")
                    or contract.get("title") == current_section.get("title")
                    or (current_title and current_title == contract_title)
                ):
                    section_contract = contract
                    break
        blueprint = self._repair_override(state, "P-WRITE-BLUEPRINT")
        if blueprint is None:
            blueprint = self._result(project["id"], "P-WRITE-BLUEPRINT", "blueprint")
        # Full-proposal candidates are produced by persistent child workflows, not by
        # the parent WF-4 workflow that invokes P-INTEGRATION-CRITIC.  Filtering
        # by the parent workflow_id therefore returns no rows and leaves the
        # strict input-schema scaffold in candidate_sections.  Aggregate the
        # latest accepted candidate per section across this project instead.
        content_candidates = self._content_candidates(project["id"], None)
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
            project_items = list((project_definition or {}).get("items") or [])
            def _item_ref(item: dict[str, Any]) -> dict[str, Any]:
                content = item.get("content") or {}
                display_name = (
                    content.get("name") or content.get("project_name") or content.get("description")
                    or item.get("item_type") or item.get("item_id")
                )
                return {
                    "object_id": str(item.get("item_id")),
                    "object_type": str(item.get("item_type") or "PROJECT_ITEM"),
                    "version": 1,
                    "object_hash": item.get("item_hash"),
                    "security_level": str(item.get("security_level") or project["security_level"]),
                    "display_name": str(display_name)[:240],
                }
            if "technical_inputs" in payload:
                technical = [
                    _item_ref(item) for item in project_items
                    if str(item.get("item_type") or "") in {"WORK_PACKAGE", "METHOD", "DATA_RESOURCE", "EXPERIMENT"}
                ]
                replacements.append(("payload.technical_inputs", technical[:20]))
            if "metric_inputs" in payload:
                metrics = [
                    _item_ref(item) for item in project_items
                    if str(item.get("item_type") or "") in {"METRIC", "DELIVERABLE", "EXPERIMENT"}
                ]
                replacements.append(("payload.metric_inputs", metrics[:20]))
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
            # The planning and drafting schemas use the compact template reference,
            # while expression-stage schemas require the complete confirmed template.
            compact_template_prompts = {
                "P-REVISION-PLAN", "P-REVISION-PLAN-CRITIC",
                "P-WRITE-BLUEPRINT", "P-WRITE-BLUEPRINT-CRITIC",
                "P-WRITE-CONTENT", "P-WRITE-CRITIC",
            }
            if prompt_id in compact_template_prompts:
                replacements.append(("payload.template_context", {
                    "template_id": str(template.get("template_id") or "template-research-proposal-structure-v1"),
                    "component_ids": [
                        str(item.get("component_id")) for item in (template.get("components") or [])
                        if item.get("component_id")
                    ],
                    "rules": [
                        str(item) for item in (template.get("format_rules") or []) if str(item).strip()
                    ] + ([str(template.get("global_argument"))] if template.get("global_argument") else []),
                }))
            else:
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
        if "terminology" in payload:
            replacements.append((
                "payload.terminology",
                self._terminology(config, state),
            ))
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
            instruction_value = config.get("task_instruction")
            instruction_objective = instruction_text(
                instruction_value,
                str(project.get("description") or project.get("name") or ""),
            )
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
                    self._structured_task_instruction(
                        instruction_objective,
                        section_ids,
                        config,
                        raw_instruction=instruction_value,
                    ),
                ))
            else:
                replacements.append(("payload.task_instruction", instruction_objective))
        if "recipient_scope" in payload:
            replacements.append(("payload.recipient_scope", config.get("recipient_scope", ["内部用户"])))
        if "prior_security_findings" in payload:
            replacements.append((
                "payload.prior_security_findings",
                list((state or {}).get("prior_security_findings") or []),
            ))
        if "allowed_topics" in payload:
            replacements.append(("payload.allowed_topics", config.get("allowed_public_topics", ["公开政策", "公开学术资料"])))
        if "prohibited_fields" in payload:
            replacements.append(("payload.prohibited_fields", config.get("prohibited_external_fields", [])))

        # Bind the safe-online-package schemas to the confirmed project graph
        # without embedding project-specific topics or responses in production code.
        if prompt_id == "P-SAFE-ONLINE-PACKAGE-CRITIC":
            package_for_scan = self._result(project["id"], "P-SAFE-ONLINE-PACKAGE") or {}
            source_summary = self._project_item_summaries(project["id"], project)
            package_text = json.dumps(package_for_scan, ensure_ascii=False)
            forbidden_literals = [
                str(project.get("id") or ""),
                str(project.get("name") or ""),
                *[str(item) for item in config.get("prohibited_external_fields", [])],
            ]
            leaked = [token for token in forbidden_literals if token and token in package_text]
            replacements.extend([
                ("payload.source_summary", source_summary),
                ("payload.deterministic_scan", {
                    "passed": not leaked,
                    "matched_rules": [
                        "PUBLIC_SECURITY_LEVEL_ONLY",
                        "PROJECT_AND_PERSON_IDENTITY_REMOVED",
                        "INTERNAL_OBJECT_IDENTIFIERS_REMOVED",
                        "PRIVATE_DATA_AND_UNVERIFIED_RESULTS_PROHIBITED",
                        "PUBLIC_RESEARCH_SCOPE_ENFORCED",
                    ],
                    "redacted_fields": list(package_for_scan.get("removed_fields") or leaked),
                }),
            ])

        if prompt_id == "P-SAFE-ONLINE-PACKAGE":
            replacements.extend([
                ("payload.research_need", self._research_need(project, config, context_hash)),
                ("payload.source_items", self._research_object_refs(project["id"], project, context_hash)),
                ("payload.target_task_type", "PUBLIC_RESEARCH"),
            ])

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

    def _project_definition_items(self, project_id: str) -> list[dict[str, Any]]:
        package = self._result(project_id, "P-PROJECT-DEFINITION-EXTRACT", "project_definition") or {}
        return [item for item in package.get("items", []) if isinstance(item, dict)]

    @staticmethod
    def _item_statement(item: dict[str, Any]) -> str:
        return str(
            item.get("statement")
            or item.get("description")
            or item.get("name")
            or item.get("title")
            or item.get("item_type")
            or "研究事项"
        ).strip()

    def _research_focus(self, project: dict[str, Any]) -> str:
        preferred = {
            "GAP", "ROOT_CAUSE", "PROBLEM", "OBJECTIVE", "WORK_PACKAGE",
            "METHOD", "DATA_RESOURCE", "EXPERIMENT", "INNOVATION", "METRIC",
        }
        statements = []
        for item in self._project_definition_items(project["id"]):
            if str(item.get("item_type") or "").upper() not in preferred:
                continue
            statement = self._item_statement(item)
            if statement and statement not in statements:
                statements.append(statement)
            if len(statements) >= 8:
                break
        if statements:
            return "；".join(statements)
        return str(project.get("description") or project.get("name") or "当前科研项目")

    def _research_need(
        self,
        project: dict[str, Any],
        config: dict[str, Any],
        context_hash: str,
    ) -> dict[str, Any]:
        configured = config.get("research_need")
        if isinstance(configured, dict) and all(
            configured.get(key)
            for key in ("need_id", "question", "reason_online_needed", "desired_output")
        ):
            return copy.deepcopy(configured)
        focus = self._research_focus(project)
        question = (
            "围绕以下研究焦点，公开研究形成了哪些代表性理论、方法、数据资源、"
            "评价指标、对照基线和已知局限；哪些证据能够支撑研究差距、技术路线与验证设计："
            + focus
        )
        return {
            "need_id": "research-need-" + sha256_json({"focus": focus, "context": context_hash})[:16],
            "question": question,
            "reason_online_needed": (
                "项目输入定义了研究目标和边界，但公开文献、近期进展、数据资源版本与"
                "对照证据必须通过真实检索核验，不能仅依赖模型记忆。"
            ),
            "desired_output": (
                "形成可追溯的公开证据包，覆盖研究脉络、近期工作、数据资源、基线、"
                "评价指标、有效性威胁和未解决问题，并为可写入申请书的主张保留稳定来源标识。"
            ),
        }

    def _source_summaries(
        self,
        source_docs: list[dict[str, Any]],
        project: dict[str, Any],
    ) -> list[dict[str, Any]]:
        summaries: list[dict[str, Any]] = []
        for document in source_docs:
            section_titles = [
                str(section.get("title") or "").strip()
                for section in document.get("sections", [])[:8]
                if str(section.get("title") or "").strip()
            ]
            role = str(document.get("document_role") or "OTHER")
            abstracted = f"{role}材料，主要结构：" + ("、".join(section_titles) if section_titles else "未提供章节标题")
            project_name = str(project.get("name") or "")
            if project_name:
                abstracted = abstracted.replace(project_name, "[PROJECT]")
            summaries.append({
                "source_item_id": str(document.get("document_id") or "document-source"),
                "abstracted_summary": abstracted[:1000],
                "original_security_level": str(document.get("security_level") or project["security_level"]),
            })
        return summaries

    def _project_item_summaries(
        self,
        project_id: str,
        project: dict[str, Any],
    ) -> list[dict[str, Any]]:
        allowed_types = {
            "GAP", "ROOT_CAUSE", "PROBLEM", "OBJECTIVE", "WORK_PACKAGE",
            "METHOD", "DATA_RESOURCE", "EXPERIMENT", "INNOVATION", "METRIC",
        }
        rows: list[dict[str, Any]] = []
        for item in self._project_definition_items(project_id):
            if str(item.get("item_type") or "").upper() not in allowed_types:
                continue
            summary = self._item_statement(item)
            project_name = str(project.get("name") or "")
            if project_name:
                summary = summary.replace(project_name, "[PROJECT]")
            rows.append({
                "source_item_id": str(item.get("item_id") or "project-item"),
                "abstracted_summary": summary[:1000],
                "original_security_level": str(item.get("security_level") or project["security_level"]),
            })
        return rows

    def _research_object_refs(
        self,
        project_id: str,
        project: dict[str, Any],
        context_hash: str,
    ) -> list[dict[str, Any]]:
        allowed_types = {
            "GAP", "ROOT_CAUSE", "PROBLEM", "OBJECTIVE", "WORK_PACKAGE",
            "METHOD", "DATA_RESOURCE", "EXPERIMENT", "INNOVATION", "METRIC",
        }
        refs: list[dict[str, Any]] = []
        for item in self._project_definition_items(project_id):
            item_type = str(item.get("item_type") or "PROJECT_ITEM").upper()
            if item_type not in allowed_types:
                continue
            refs.append({
                "object_id": str(item.get("item_id") or "project-item"),
                "object_type": item_type,
                "version": int(item.get("version") or 1),
                "object_hash": str(item.get("item_hash") or context_hash),
                "security_level": str(item.get("security_level") or project["security_level"]),
                "display_name": self._item_statement(item)[:300],
            })
        return refs

    @staticmethod
    def _known_public_sources(config: dict[str, Any]) -> list[dict[str, Any]]:
        rows = config.get("known_public_sources") or []
        return [copy.deepcopy(item) for item in rows if isinstance(item, dict)]

    @staticmethod
    def _terminology(config: dict[str, Any], state: dict[str, Any]) -> list[dict[str, Any]]:
        rows = state.get("terminology") or config.get("terminology") or []
        return [
            {
                "canonical_term": str(item.get("canonical_term") or ""),
                "aliases": [str(alias) for alias in item.get("aliases", []) if str(alias)],
                "definition": str(item.get("definition") or ""),
            }
            for item in rows
            if isinstance(item, dict)
            and item.get("canonical_term")
            and item.get("definition")
        ]

    @staticmethod
    def _structured_task_instruction(
        instruction_text: str,
        section_ids: list[str],
        config: dict[str, Any],
        *,
        raw_instruction: Any = None,
    ) -> dict[str, Any]:
        structured = config.get("task_instruction_structured")
        if not isinstance(structured, dict) and isinstance(raw_instruction, dict):
            structured = raw_instruction
        if isinstance(structured, dict):
            result = copy.deepcopy(structured)
            result.setdefault("schema_version", "2.0")
            result.setdefault("task_instruction_id", "instruction-" + sha256_json(result)[:16])
            result.setdefault("task_type", "DRAFT_FROM_PROJECT_DEFINITION")
            result.setdefault("objective", instruction_text)
            result.setdefault("target_section_ids", section_ids)
            for key in (
                "specific_requirements", "must_preserve", "forbidden_changes",
                "acceptance_preferences", "priority_order",
            ):
                result.setdefault(key, [])
            result["instruction_hash"] = sha256_json({
                key: value for key, value in result.items() if key != "instruction_hash"
            })
            return result
        core = {
            "schema_version": "2.0",
            "task_instruction_id": "instruction-" + sha256_json(instruction_text)[:16],
            "task_type": "DRAFT_FROM_PROJECT_DEFINITION",
            "objective": instruction_text,
            "target_section_ids": section_ids,
            "specific_requirements": list(config.get("specific_requirements") or [
                "按已确认的章节合同完成完整申请书",
                "公开调研使用可核验来源并保留来源绑定",
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
