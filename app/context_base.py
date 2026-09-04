from __future__ import annotations

import copy
import json
import re
from contextvars import ContextVar
from datetime import date, datetime
from typing import Any

from jsonschema import Draft202012Validator

from .candidate_integrity import (
    candidate_text_divergence,
    paragraph_identity_error,
    visible_candidate_snapshot,
)
from .paragraph_order import canonical_candidate_text, ordered_paragraphs, paragraph_sequence_error
from .privacy import find_sensitive_values
from .proposal_quality import SECTION_FUNCTION_ROLE_ALIASES
from .workflow_repair import repair_override_key, producer_consumer_value
from .background_research import (
    BACKGROUND_DIMENSIONS,
    WF3B_PLAN_PROMPT,
    WF3B_PLAN_CRITIC_PROMPT,
    WF3B_RESEARCH_CRITIC,
    WF3B_SYNTHESIS_PROMPT,
    WF3B_WORKFLOW_TYPE,
    background_execution_contract,
    build_background_cards,
)
from .model_semantic_contracts import project_argument_authoritative_state
from .wf3_contracts import wf3_safe_package_valid_until
from .util import new_id, sha256_json, sha256_text
from .wf3_input import (
    WF3_INPUT_GATE_TYPE,
    WorkflowInputRequired,
    allowed_topics_gate_questions,
    build_research_need,
    input_gate_questions,
    normalize_target_task_type,
    normalize_wf3_time_constraints,
)
from .workflow_input import (
    APPLICATION_GUIDE_INPUT,
    CURRENT_PROPOSAL_INPUT,
    PROJECT_MATERIAL_INPUT,
    REFERENCE_TEMPLATE_INPUT,
    build_human_resolutions,
    canonicalize_human_resolution,
    human_resolution_scope_key,
    material_input_questions,
    resolution_overrides,
    section_id_for_run,
)

HASH_PLACEHOLDER = "a" * 64

_CURRENT_WORKFLOW_ID: ContextVar[str | None] = ContextVar(
    "proposal_context_workflow_id",
    default=None,
)
_WORKFLOW_ARTIFACT_SOURCE_CACHE: ContextVar[dict[str, tuple[str, ...]] | None] = ContextVar(
    "proposal_context_artifact_source_cache",
    default=None,
)

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
        self._input_schema_cache: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _resolve_workflow_id(workflow_id: str | None) -> str | None:
        """Resolve workflow identity at the artifact access boundary.

        Workflow-aware callers must pass the id explicitly. The ContextVar is
        retained only as a compatibility fallback for helpers invoked strictly
        inside one context-build scope.
        """
        explicit = str(workflow_id or "").strip()
        return explicit or _CURRENT_WORKFLOW_ID.get()

    def build(self, prompt_id: str, project_id: str, *, workflow_id: str | None = None, workflow_state: dict[str, Any] | None = None, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        project = self.db.fetchone("SELECT * FROM projects WHERE id=?", (project_id,))
        if not project:
            raise KeyError(f"Project not found: {project_id}")
        config = json.loads(project["config_json"])
        docs = self._documents(project_id)
        state = copy.deepcopy(workflow_state or {})
        requested_overrides = copy.deepcopy(overrides or {})
        context_hash = sha256_json({"project": project, "documents": [d["document_hash"] for d in docs], "workflow_state": state})
        envelope = self.pack.replay_input(prompt_id)
        envelope = self._replace_seed_values(envelope, project_id, context_hash)
        envelope["task"]["task_id"] = new_id("task")
        envelope["task"]["current_step"] = prompt_id.removeprefix("P-").replace("-", "_")
        if state.get("workflow_type"):
            workflow_type = state["workflow_type"]
            envelope["task"]["workflow_type"] = workflow_type.split("_", 1)[1] if workflow_type.startswith("WF-") and "_" in workflow_type else workflow_type
        required_environment = self._required_environment(prompt_id, state)
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
        workflow_token = _CURRENT_WORKFLOW_ID.set(workflow_id)
        source_cache_token = _WORKFLOW_ARTIFACT_SOURCE_CACHE.set({})
        try:
            self._apply_common_payload(
                envelope,
                prompt_id,
                project,
                config,
                docs,
                context_hash,
                state,
                workflow_id,
            )
        finally:
            _WORKFLOW_ARTIFACT_SOURCE_CACHE.reset(source_cache_token)
            _CURRENT_WORKFLOW_ID.reset(workflow_token)
        if requested_overrides:
            for path, value in requested_overrides.items():
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

    def _content_candidates(
        self,
        project_id: str,
        workflow_id: str | None = None,
        *,
        section_results: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Return final section candidates in the workflow contract order.

        A full-proposal parent owns the frozen ``section_results`` index while
        producer runs may belong to several child workflows.  Discovering runs
        by project timestamps therefore loses both lineage and section order.
        When the workflow index is available, it is the sole visibility source:
        run IDs are resolved explicitly and each section selects the polished
        candidate consumed by its final successful independent Expression Critic.
        The legacy workflow query remains
        only for contexts that do not yet carry a section-result index.
        """

        producer_prompts = {"P-WRITE-CONTENT", "P-EXPRESSION-POLISH"}
        final_review_prompt = "P-EXPRESSION-CRITIC"
        indexed_results = [item for item in (section_results or []) if isinstance(item, dict)]
        if indexed_results:
            run_ids = [
                str(run.get("run_id") or "")
                for item in indexed_results
                for run in (item.get("runs") or [])
                if isinstance(run, dict)
                and run.get("status") == "PASS"
                and run.get("prompt_id") in {*producer_prompts, final_review_prompt}
                and str(run.get("run_id") or "")
            ]
            rows_by_id: dict[str, dict[str, Any]] = {}
            if run_ids:
                placeholders = ",".join("?" for _ in run_ids)
                rows = self.db.fetchall(
                    "SELECT id,prompt_id,input_json,output_json FROM prompt_runs "
                    f"WHERE project_id=? AND status='PASS' AND id IN ({placeholders})",
                    (project_id, *run_ids),
                )
                rows_by_id = {str(row.get("id") or ""): row for row in rows}

            ordered: list[dict[str, Any]] = []
            missing: list[str] = []
            for item in indexed_results:
                expected_section_id = str(item.get("section_id") or "")
                selected: dict[str, Any] | None = None
                for run in reversed(item.get("runs") or []):
                    if not isinstance(run, dict):
                        continue
                    if run.get("status") != "PASS" or run.get("prompt_id") != final_review_prompt:
                        continue
                    row = rows_by_id.get(str(run.get("run_id") or ""))
                    if not row or not row.get("input_json"):
                        continue
                    input_data = json.loads(row.get("input_json") or "{}")
                    payload = input_data.get("payload") or {}
                    section = payload.get("source_section") or {}
                    if str(section.get("section_id") or "") != expected_section_id:
                        continue
                    candidate = payload.get("polished_candidate") or {}
                    if not candidate.get("candidate_id"):
                        continue
                    selected = {
                        "run_id": row["id"],
                        "prompt_id": row.get("prompt_id"),
                        "section": section,
                        "candidate": candidate,
                    }
                    break
                if selected is None:
                    missing.append(expected_section_id or "<unknown-section>")
                else:
                    ordered.append(selected)
            if missing:
                raise ValueError(
                    "Frozen section-result index has no successful final candidate for: "
                    + ", ".join(missing)
                )
            return ordered

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

    def _bound_authoring_section_results(
        self,
        project_id: str,
        state: dict[str, Any],
    ) -> tuple[str, list[dict[str, Any]]]:
        bindings = state.get("prerequisite_workflow_ids") or {}
        authoring_id = str(bindings.get("WF-4_PROPOSAL_AUTHORING") or "").strip()
        if not authoring_id:
            raise ValueError("Final confidentiality review has no frozen WF-4 prerequisite binding.")
        row = self.db.fetchone(
            "SELECT project_id,workflow_type,status,state_json FROM workflows WHERE id=?",
            (authoring_id,),
        )
        if not row:
            raise ValueError(f"Frozen authoring workflow does not exist: {authoring_id}")
        if str(row.get("project_id") or "") != project_id:
            raise ValueError("Frozen authoring workflow belongs to another project.")
        if row.get("workflow_type") != "WF-4_PROPOSAL_AUTHORING":
            raise ValueError("Frozen prerequisite is not a WF-4 authoring workflow.")
        authoring_state = json.loads(row.get("state_json") or "{}")
        if authoring_state.get("parent_workflow_id"):
            raise ValueError("Final confidentiality review must bind the top-level WF-4 workflow, not a child worker.")
        if row.get("status") != "COMPLETED":
            raise ValueError("Frozen WF-4 authoring workflow is not completed.")
        section_results = [
            item for item in authoring_state.get("section_results") or []
            if isinstance(item, dict)
        ]
        if not section_results:
            raise ValueError("Frozen WF-4 authoring workflow has no section-result index.")
        return authoring_id, section_results

    @staticmethod
    def _assert_final_candidate_integrity(candidates: list[dict[str, Any]]) -> None:
        for item in candidates:
            section_id = str((item.get("section") or {}).get("section_id") or "<unknown-section>")
            candidate = item.get("candidate") or {}
            errors = [
                paragraph_sequence_error(candidate.get("paragraphs")),
                paragraph_identity_error(candidate.get("paragraphs")),
                candidate_text_divergence(candidate),
            ]
            errors = [error for error in errors if error]
            if errors:
                raise ValueError(
                    f"Frozen candidate {section_id} violates the canonical paragraph contract: "
                    + "; ".join(errors)
                )

    def final_review_candidate_set_snapshot(
        self,
        project_id: str,
        state: dict[str, Any],
        *,
        document_section_map: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Bind the final review to exact WF-4 candidate and paragraph identities."""

        authoring_id, section_results = self._bound_authoring_section_results(project_id, state)
        candidates = self._content_candidates(
            project_id,
            authoring_id,
            section_results=section_results,
        )
        self._assert_final_candidate_integrity(candidates)
        snapshot = visible_candidate_snapshot(
            candidates,
            document_section_map=document_section_map,
        )
        if snapshot.get("section_count") != len(section_results):
            raise ValueError(
                "Frozen WF-4 candidate identity is incomplete: "
                f"expected {len(section_results)} sections, got {snapshot.get('section_count')}"
            )
        return snapshot


    @staticmethod
    def _prior_section_digest(candidates: list[dict[str, Any]], current_section_id: str | None = None) -> list[dict[str, Any]]:
        digests: list[dict[str, Any]] = []
        for item in candidates:
            section = item.get("section") or {}
            if current_section_id and section.get("section_id") == current_section_id:
                continue
            candidate = item.get("candidate") or {}
            advancement = candidate.get("claim_advancement") or {}
            paragraphs = ordered_paragraphs(candidate.get("paragraphs"))
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
        result = {
            key: candidate.get(
                key,
                [] if key in {"paragraphs", "trace_links", "term_usage", "unresolved_items"}
                else ({} if key == "claim_advancement" else ""),
            )
            for key in allowed
        }
        result["paragraphs"] = ordered_paragraphs(candidate.get("paragraphs"))
        result["candidate_text"] = canonical_candidate_text(candidate)
        return result

    def _candidate_document(self, project: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, Any]:
        sections = []
        for item in candidates:
            source = item["section"]
            candidate = item["candidate"]
            paragraphs = ordered_paragraphs(candidate.get("paragraphs"))
            text = canonical_candidate_text(candidate)
            sections.append(
                {
                    "section_id": source["section_id"],
                    "section_key": source.get("section_key") or source.get("title") or source["section_id"],
                    "title": source.get("title", ""),
                    "level": source.get("level", 1),
                    "text": text,
                    "text_hash": sha256_json({"section_id": source["section_id"], "text": text}),
                    "block_ids": [paragraph.get("paragraph_id") for paragraph in paragraphs if paragraph.get("paragraph_id")],
                    "contains_table": any(paragraph.get("text", "").startswith("[[TABLE]]") for paragraph in paragraphs),
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
        profile_keywords = {
            "BACKGROUND_AND_SIGNIFICANCE": ("问题", "差距", "局限", "基线", "边界"),
            "LITERATURE_REVIEW": ("现状", "文献", "基线", "比较"),
            "RESEARCH_CONTENT": ("问题", "目标", "任务", "命题"),
            "TECHNICAL_ROUTE": ("方法", "技术路线", "实验", "指标", "基线", "约束"),
            "INNOVATION": ("创新", "基线", "比较", "最近工作"),
            "EVALUATION": ("实验", "指标", "均值", "方差", "置信区间", "效应量"),
            "FOUNDATION": ("团队", "成果", "能力", "基础"),
        }.get(profile_id, ())
        contract_text = json.dumps(contract or {}, ensure_ascii=False)
        baseline_tokens = {
            f"B{match}"
            for identifier in relevant_ids
            for match in re.findall(r"(?:GAP|RQ|IH)[-_]?0*(\d+)", identifier, flags=re.I)
        }

        def relevance(item: dict[str, Any]) -> tuple[int, str]:
            text = json.dumps(item, ensure_ascii=False)
            score = sum(4 for identifier in relevant_ids if identifier and identifier in text)
            score += sum(6 for token in baseline_tokens if token in text)
            score += sum(1 for keyword in profile_keywords if keyword in text)
            if any(keyword in contract_text and keyword in text for keyword in profile_keywords):
                score += 2
            return score, str(item.get("claim_id") or "")

        relevant_internal = sorted(internal, key=relevance, reverse=True)
        public_profiles = {"BACKGROUND_AND_SIGNIFICANCE", "LITERATURE_REVIEW", "INNOVATION", "REFERENCES", "EVALUATION"}
        selected = [*exact, *relevant_internal[:12]]
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

    def _workflow_lineage_ids(self, workflow_id: str | None) -> list[str]:
        """Return current workflow followed by its explicit parent chain."""
        if not workflow_id:
            return []
        lineage: list[str] = []
        seen: set[str] = set()
        current = str(workflow_id)
        project_id: str | None = None
        for _ in range(8):
            if not current or current in seen:
                break
            row = self.db.fetchone(
                "SELECT id,project_id,state_json FROM workflows WHERE id=?",
                (current,),
            )
            if not row:
                break
            if project_id is None:
                project_id = str(row["project_id"])
            elif str(row["project_id"]) != project_id:
                break
            lineage.append(current)
            seen.add(current)
            state = json.loads(row.get("state_json") or "{}")
            current = str(state.get("parent_workflow_id") or "").strip()
        return lineage

    def _workflow_artifact_source_ids(self, workflow_id: str | None) -> list[str]:
        """Return the frozen artifact source set for a workflow.

        Sources are limited to the current workflow, its explicit parent chain,
        and prerequisite workflow ids captured when the workflow started (or was
        migrated).  Arbitrary completed workflows are deliberately excluded so a
        later rerun cannot silently change the context of an in-flight workflow.

        A context build may request dozens of prior prompt results.  The workflow
        lineage and prerequisite bindings are immutable for that build, so they
        are resolved once and cached in a ContextVar scoped to the current async
        task.  This avoids repeatedly parsing large workflow state documents and
        is safe for concurrent section workers.
        """
        cache = _WORKFLOW_ARTIFACT_SOURCE_CACHE.get()
        cache_key = str(workflow_id or "")
        if cache is not None and cache_key in cache:
            return list(cache[cache_key])

        lineage = self._workflow_lineage_ids(workflow_id)
        source_ids: list[str] = list(lineage)
        seen = set(source_ids)
        root_row = (
            self.db.fetchone("SELECT project_id FROM workflows WHERE id=?", (lineage[0],))
            if lineage
            else None
        )
        project_id = str((root_row or {}).get("project_id") or "")

        # Prerequisite bindings are a frozen dependency graph, not merely one
        # hop of context.  A downstream workflow that binds WF-4 must retain
        # WF-4's frozen WF-1/WF-2/WF-3 evidence ancestry as well; otherwise a
        # fresh authoritative projection can lose evidence that the persisted
        # authored_state legitimately references.  Traverse only explicit,
        # same-project, COMPLETED prerequisite edges and stop on seen ids.
        cursor = 0
        while cursor < len(source_ids):
            source_id = source_ids[cursor]
            cursor += 1
            row = self.db.fetchone(
                "SELECT project_id,state_json FROM workflows WHERE id=?",
                (source_id,),
            )
            if not row or (project_id and str(row.get("project_id") or "") != project_id):
                continue
            state = json.loads(row.get("state_json") or "{}")
            bindings = state.get("prerequisite_workflow_ids") or {}
            if not isinstance(bindings, dict):
                continue
            for bound_id in bindings.values():
                candidate = str(bound_id or "").strip()
                if not candidate or candidate in seen:
                    continue
                candidate_row = self.db.fetchone(
                    "SELECT project_id,status FROM workflows WHERE id=?",
                    (candidate,),
                )
                if (
                    not candidate_row
                    or str(candidate_row.get("project_id") or "") != project_id
                    or str(candidate_row.get("status") or "") != "COMPLETED"
                ):
                    continue
                source_ids.append(candidate)
                seen.add(candidate)
        if cache is not None:
            cache[cache_key] = tuple(source_ids)
        return source_ids

    def _accepted_output(
        self,
        project_id: str,
        prompt_id: str,
        workflow_ids: list[str],
    ) -> dict[str, Any] | None:
        """Return an explicitly gate-accepted non-PASS output.

        A REVISE/NEED_USER_INPUT result can be accepted as the documented output
        of an intake stage.  The gate engine records that decision in
        ``accepted_step_results`` without rewriting the model's original status.
        Consumers must therefore resolve the accepted run explicitly instead of
        treating every REVISE artifact as usable or silently falling back to a
        replay scaffold.
        """
        for workflow_id in workflow_ids:
            workflow = self.db.fetchone(
                "SELECT project_id,state_json FROM workflows WHERE id=?",
                (workflow_id,),
            )
            if not workflow or str(workflow["project_id"]) != str(project_id):
                continue
            state = json.loads(workflow.get("state_json") or "{}")
            accepted = state.get("accepted_step_results") or {}
            if not isinstance(accepted, dict):
                continue
            ordered = sorted(
                (
                    (int(step) if str(step).isdigit() else -1, item)
                    for step, item in accepted.items()
                    if isinstance(item, dict)
                ),
                reverse=True,
            )
            for _, item in ordered:
                run_id = str(item.get("run_id") or "").strip()
                gate_id = str(item.get("gate_id") or "").strip()
                if not run_id or not gate_id:
                    continue
                row = self.db.fetchone(
                    """SELECT r.output_json,r.status
                         FROM prompt_runs r
                         JOIN gates g
                           ON g.id=? AND g.workflow_id=r.workflow_id
                          AND g.target_id=r.id AND g.status='APPROVED'
                        WHERE r.id=? AND r.project_id=? AND r.workflow_id=?
                          AND r.prompt_id=? AND r.output_json IS NOT NULL
                          AND EXISTS (
                              SELECT 1 FROM artifacts a
                               WHERE a.project_id=r.project_id
                                 AND a.workflow_id=r.workflow_id
                                 AND a.prompt_id=r.prompt_id
                                 AND a.content_json=r.output_json
                                 AND a.artifact_type IN (
                                     'PROMPT_OUTPUT',
                                     'SKILL_ENRICHED_PROMPT_OUTPUT'
                                 )
                          )""",
                    (gate_id, run_id, project_id, workflow_id, prompt_id),
                )
                if not row:
                    continue
                row_status = str(row.get("status") or "")
                accepted_status = str(item.get("status") or "")
                migration = state.get("business_block_migration") or {}
                status_migrated_with_audit = (
                    isinstance(migration, dict)
                    and str(migration.get("run_id") or "") == run_id
                    and str(migration.get("from_status") or "") == row_status
                    and str(migration.get("to_status") or "") == accepted_status
                    and row_status == "BLOCK"
                    and accepted_status == "NEED_USER_INPUT"
                    and bool(migration.get("output_normalizer_version"))
                    and bool(migration.get("migrated_at"))
                )
                if row_status != accepted_status and not status_migrated_with_audit:
                    continue
                output = json.loads(row["output_json"])
                if isinstance(output, dict):
                    return output
        return None

    def _latest_output(
        self,
        project_id: str,
        prompt_id: str,
        *,
        workflow_id: str | None = None,
        exact_workflow: bool = False,
    ) -> dict[str, Any] | None:
        """Return only usable prompt artifacts.

        A consumer may read artifacts produced earlier in its own workflow, or
        artifacts from a completed prerequisite workflow.  Failed, rejected,
        still-running, or unrelated concurrent workflow artifacts are excluded.
        This prevents a newer BLOCK/REVISE artifact from shadowing the last
        confirmed PASS result.
        """
        active_workflow_id = self._resolve_workflow_id(workflow_id)
        if active_workflow_id:
            workflow_row = self.db.fetchone(
                "SELECT project_id,state_json FROM workflows WHERE id=?",
                (active_workflow_id,),
            )
            if workflow_row and str(workflow_row.get("project_id")) == str(project_id):
                try:
                    workflow_state = json.loads(workflow_row.get("state_json") or "{}")
                except (TypeError, json.JSONDecodeError):
                    workflow_state = {}
                baseline = (workflow_state.get("wf3_accepted_model_baselines") or {}).get(
                    prompt_id
                )
                if isinstance(baseline, dict) and baseline.get("run_id"):
                    baseline_row = self.db.fetchone(
                        """SELECT output_json,output_hash FROM prompt_runs
                           WHERE id=? AND project_id=? AND workflow_id=? AND prompt_id=?
                             AND status='PASS' AND output_json IS NOT NULL""",
                        (
                            str(baseline["run_id"]),
                            project_id,
                            active_workflow_id,
                            prompt_id,
                        ),
                    )
                    if not baseline_row:
                        raise ValueError(
                            f"WF-3 accepted baseline run is missing: {baseline['run_id']}"
                        )
                    decoded = json.loads(baseline_row["output_json"])
                    expected_hash = str(baseline.get("output_hash") or "")
                    if expected_hash and sha256_json(decoded) != expected_hash:
                        raise ValueError(
                            f"WF-3 accepted baseline hash mismatch: {baseline['run_id']}"
                        )
                    return decoded
            accepted_source_ids = (
                [active_workflow_id]
                if exact_workflow
                else self._workflow_artifact_source_ids(active_workflow_id)
            )
            accepted_output = self._accepted_output(
                project_id,
                prompt_id,
                accepted_source_ids,
            )
            if accepted_output is not None:
                return accepted_output
        params: list[Any] = [project_id, prompt_id]
        scope_sql = ""
        ordering = "a.version DESC,a.created_at DESC"
        status_sql = "a.status='PASS'"
        if exact_workflow:
            if not active_workflow_id:
                return None
            scope_sql = " AND a.workflow_id=?"
            params.append(active_workflow_id)
        elif active_workflow_id:
            source_ids = self._workflow_artifact_source_ids(active_workflow_id)
            if source_ids:
                placeholders = ",".join("?" for _ in source_ids)
                scope_sql = f" AND a.workflow_id IN ({placeholders})"
                params.extend(source_ids)
                if len(source_ids) > 1:
                    secondary_placeholders = ",".join("?" for _ in source_ids[1:])
                    ordering = (
                        "CASE WHEN a.workflow_id=? THEN 0 "
                        f"WHEN a.workflow_id IN ({secondary_placeholders}) THEN 1 ELSE 2 END,"
                        "a.version DESC,a.created_at DESC"
                    )
                    params.append(source_ids[0])
                    params.extend(source_ids[1:])
                else:
                    ordering = "CASE WHEN a.workflow_id=? THEN 0 ELSE 1 END,a.version DESC,a.created_at DESC"
                    params.append(source_ids[0])
            else:
                # Direct prompt execution and isolated tests may not have a
                # persisted workflow row. Project-level PASS artifacts remain
                # usable there, but a real workflow never consumes them.
                scope_sql = " AND (a.workflow_id IS NULL OR w.status='COMPLETED')"
                status_sql = "a.status IN ('PASS','CANDIDATE')"
        else:
            scope_sql = " AND (a.workflow_id IS NULL OR w.status='COMPLETED')"
            status_sql = "a.status IN ('PASS','CANDIDATE')"
        row = self.db.fetchone(
            f"""SELECT a.content_json FROM artifacts a
                LEFT JOIN workflows w ON w.id=a.workflow_id
                WHERE a.project_id=? AND a.prompt_id=?
                  AND a.artifact_type IN ('PROMPT_OUTPUT','SKILL_ENRICHED_PROMPT_OUTPUT')
                  AND {status_sql}
                  {scope_sql}
                ORDER BY {ordering} LIMIT 1""",
            tuple(params),
        )
        return json.loads(row["content_json"]) if row else None

    def _result(
        self,
        project_id: str,
        prompt_id: str,
        key: str | None = None,
        *,
        workflow_id: str | None = None,
        exact_workflow: bool = False,
    ) -> Any:
        output = self._latest_output(
            project_id,
            prompt_id,
            workflow_id=workflow_id,
            exact_workflow=exact_workflow,
        )
        if not output:
            return None
        result = output.get("result")
        return result.get(key) if key and isinstance(result, dict) else result

    def _repair_override(
        self,
        state: dict[str, Any],
        producer_prompt: str,
        *,
        workflow_id: str | None,
    ) -> Any:
        target_key = repair_override_key(producer_prompt, state)
        workflow_id = self._resolve_workflow_id(workflow_id)
        indexed_ids = (state.get("repair_application_artifact_ids") or {}).get(target_key)
        indexed_ids = [str(item) for item in indexed_ids or [] if str(item).strip()]
        if workflow_id and indexed_ids:
            strict_argument_repair = producer_prompt == "P-ARGUMENT-ARCHITECTURE"
            placeholders = ",".join("?" for _ in indexed_ids)
            params: list[Any] = [workflow_id, producer_prompt, *indexed_ids]
            rows = self.db.fetchall(
                f"""SELECT id,version,content_json
                    FROM artifacts
                    WHERE workflow_id=?
                      AND artifact_type='REPAIR_APPLICATION'
                      AND prompt_id=?
                      AND status='PASS'
                      AND id IN ({placeholders})
                    ORDER BY version DESC,created_at DESC,id DESC""",
                tuple(params),
            )
            if strict_argument_repair and not rows:
                raise ValueError(
                    "Active Argument repair pointer does not resolve to a persisted REPAIR_APPLICATION"
                )
            for row in rows:
                try:
                    payload = json.loads(row.get("content_json") or "{}")
                except (TypeError, json.JSONDecodeError) as exc:
                    if strict_argument_repair:
                        raise ValueError(
                            f"Active Argument REPAIR_APPLICATION {row['id']} is not valid JSON"
                        ) from exc
                    continue
                if str(payload.get("workflow_id") or workflow_id) != str(workflow_id):
                    continue
                if str(payload.get("producer_prompt") or producer_prompt) != producer_prompt:
                    continue
                if str(payload.get("target_key") or "") != target_key:
                    continue
                if str(payload.get("application_status") or "APPLIED") != "APPLIED":
                    continue
                if "repaired_value" in payload:
                    raw_value = payload["repaired_value"]
                    expected_hash = str(payload.get("repaired_value_hash") or "").strip()
                    if expected_hash and sha256_json(raw_value) != expected_hash:
                        raise ValueError(
                            f"Active REPAIR_APPLICATION {row['id']} repaired_value hash mismatch"
                        )
                    if producer_prompt == "P-ARGUMENT-ARCHITECTURE":
                        declared_shape = str(payload.get("repaired_value_shape") or "").strip()
                        if declared_shape and declared_shape != "PRODUCER_RESULT":
                            raise ValueError(
                                "Argument REPAIR_APPLICATION repaired_value must be PRODUCER_RESULT"
                            )
                        # v8 migration compatibility: old semantic repair artifacts
                        # accidentally stored the full Producer protocol envelope,
                        # while contract-repair artifacts already stored output[result].
                        if isinstance(raw_value, dict) and isinstance(raw_value.get("authored_state"), dict):
                            value = copy.deepcopy(raw_value)
                        elif (
                            not declared_shape
                            and isinstance(raw_value, dict)
                            and isinstance((raw_value.get("result") or {}).get("authored_state"), dict)
                        ):
                            value = copy.deepcopy(raw_value["result"])
                        else:
                            raise ValueError(
                                "Argument REPAIR_APPLICATION has no ProducerResult authoritative state"
                            )
                        canonical_output = payload.get("repaired_canonical_output")
                        if isinstance(canonical_output, dict):
                            canonical_value = producer_consumer_value(
                                producer_prompt, canonical_output
                            )
                            if canonical_value != value:
                                raise ValueError(
                                    "Argument REPAIR_APPLICATION consumer value does not match canonical Producer output"
                                )
                        return value
                    return copy.deepcopy(raw_value)
            if strict_argument_repair:
                raise ValueError(
                    "Active Argument repair pointer did not resolve to a current applicable repair value"
                )

        return None

    @staticmethod
    def _canonicalize_argument_result_from_sections(
        value: Any,
        sections: list[dict[str, Any]],
    ) -> Any:
        """Reconcile an argument result with explicit RC/BASE tags in source text.

        Targeted repairs can legitimately replace a producer result, but an older
        repair may have been created before all explicitly numbered work packages
        were materialized.  Consumers must not then see a graph that contradicts
        the authoritative RQ-to-RC table in CURRENT_PROPOSAL material.

        Only exact, source-authored ``RC-n``/``BASE-n`` identifiers and exact
        same-line ``RQ-n`` -> ``RC-n`` mappings are used.  No untagged prose is
        interpreted as a new project entity.
        """
        if not isinstance(value, dict):
            return value
        canonical = copy.deepcopy(value)
        # v8 Argument Architecture has exactly one writable semantic source.
        # Context assembly must never reconcile or enrich projector-owned fields
        # from section prose; consumers either use this frozen projection or the
        # semantic Critic reprojects from ``authored_state``.  Keep the legacy
        # reconciliation only for historical results that predate authored_state.
        if isinstance(canonical.get("authored_state"), dict):
            return canonical
        is_full_result = isinstance(canonical.get("argument_architecture"), dict)
        architecture = (
            canonical.get("argument_architecture")
            if is_full_result
            else canonical
        )
        if not isinstance(architecture, dict):
            return canonical
        nodes = architecture.get("nodes")
        if not isinstance(nodes, list):
            return canonical

        tag_pattern = re.compile(
            r"(?<![A-Za-z0-9])`?(RQ|OBJ|RC|BASE)-0*(\d+)`?(?![A-Za-z0-9])",
            re.IGNORECASE,
        )
        rq_pattern = re.compile(
            r"(?<![A-Za-z0-9])`?RQ-0*(\d+)`?(?![A-Za-z0-9])",
            re.IGNORECASE,
        )
        rc_pattern = re.compile(
            r"(?<![A-Za-z0-9])`?RC-0*(\d+)`?(?![A-Za-z0-9])",
            re.IGNORECASE,
        )
        obj_pattern = re.compile(
            r"(?<![A-Za-z0-9])`?OBJ-0*(\d+)`?(?![A-Za-z0-9])",
            re.IGNORECASE,
        )

        def canonical_tag_id(value: Any) -> str:
            raw = str(value or "").strip()
            match = re.fullmatch(
                r"(RQ|OBJ|RC|BASE)-0*(\d+)", raw, flags=re.IGNORECASE
            )
            if not match:
                return raw
            return f"{match.group(1).upper()}-{int(match.group(2)):03d}"

        # Legacy persisted argument results used non-padded tag identities and
        # two edge field presentations. Canonicalize identities at this single
        # trusted context boundary; do not infer entities from free prose.
        for node in nodes:
            if isinstance(node, dict) and node.get("node_id"):
                node["node_id"] = canonical_tag_id(node["node_id"])
        research_questions = architecture.get("research_questions")
        if isinstance(research_questions, list):
            for question in research_questions:
                if isinstance(question, dict) and question.get("node_id"):
                    question["node_id"] = canonical_tag_id(question["node_id"])
        edges = architecture.get("edges")
        if not isinstance(edges, list):
            edges = []
        for edge in edges:
            if not isinstance(edge, dict):
                continue
            for field in ("source_id", "target_id", "source_node_id", "target_node_id"):
                if edge.get(field):
                    edge[field] = canonical_tag_id(edge[field])

        declarations: dict[str, tuple[str, dict[str, Any] | None]] = {}
        declared_question_work_packages: dict[str, str] = {}
        declared_question_objectives: dict[str, str] = {}
        for section in sections:
            if not isinstance(section, dict):
                continue
            section_offset = 0
            for raw_line in str(section.get("text") or "").splitlines(keepends=True):
                line = raw_line.rstrip("\r\n")
                rq_numbers = rq_pattern.findall(line)
                rc_numbers = rc_pattern.findall(line)
                obj_numbers = obj_pattern.findall(line)
                if rq_numbers and rc_numbers:
                    pairs = (
                        zip(rq_numbers, rc_numbers)
                        if len(rq_numbers) == len(rc_numbers)
                        else [(rq_numbers[0], rc_numbers[0])]
                    )
                    for rq_number, rc_number in pairs:
                        declared_question_work_packages[
                            f"RQ-{int(rq_number):03d}"
                        ] = f"RC-{int(rc_number):03d}"
                if rq_numbers and obj_numbers:
                    pairs = (
                        zip(rq_numbers, obj_numbers)
                        if len(rq_numbers) == len(obj_numbers)
                        else [(rq_numbers[0], obj_numbers[0])]
                    )
                    for rq_number, obj_number in pairs:
                        declared_question_objectives[
                            f"RQ-{int(rq_number):03d}"
                        ] = f"OBJ-{int(obj_number):03d}"
                matches = list(tag_pattern.finditer(line))
                for index, match in enumerate(matches):
                    prefix = match.group(1).upper()
                    node_id = f"{prefix}-{int(match.group(2)):03d}"
                    end = matches[index + 1].start() if index + 1 < len(matches) else len(line)
                    statement = line[match.end():end]
                    statement = re.sub(
                        r"^[\s*：:;；、|`—\-]+|[\s*|`]+$",
                        "",
                        statement,
                    ).strip()
                    if not statement:
                        statement = f"源材料显式定义的{prefix}条目 {node_id}"
                    statement = statement[:360]
                    current_statement = declarations.get(node_id, ("", None))[0]
                    if len(statement) > len(current_statement):
                        source_ref = section.get("_source_ref")
                        if isinstance(source_ref, dict):
                            source_ref = copy.deepcopy(source_ref)
                            source_ref["span_start"] = section_offset
                            source_ref["span_end"] = section_offset + len(line)
                            source_ref["quoted_text"] = line
                        elif section.get("document_id") or section.get("section_id"):
                            source_ref = {
                                "source_id": str(
                                    section.get("document_id")
                                    or section.get("section_id")
                                ),
                                "source_type": "CURRENT_PROPOSAL",
                                "document_version_id": section.get("document_version_id"),
                                "section_id": section.get("section_id"),
                                "span_start": section_offset,
                                "span_end": section_offset + len(line),
                                "quoted_text": line,
                                "source_hash": section.get("text_hash") or sha256_text(line),
                                "authority_rank": int(section.get("authority_rank") or 85),
                                "security_level": section.get("security_level") or "INTERNAL",
                            }
                        else:
                            source_ref = None
                        declarations[node_id] = (statement, source_ref)
                section_offset += len(raw_line)

        existing_node_ids = {
            str(node.get("node_id"))
            for node in nodes
            if isinstance(node, dict) and node.get("node_id")
        }
        for node_id, (statement, source_ref) in declarations.items():
            if node_id in existing_node_ids:
                continue
            prefix = node_id.split("-", 1)[0]
            node_type = {
                "RQ": "RESEARCH_QUESTION",
                "OBJ": "OBJECTIVE",
                "RC": "WORK_PACKAGE",
                "BASE": "TEAM_EVIDENCE",
            }[prefix]
            nodes.append({
                "node_id": node_id,
                "node_type": node_type,
                "statement": statement,
                "status": "UNKNOWN" if prefix == "BASE" else "PLANNED",
                "source_refs": [source_ref] if source_ref else [],
            })
            existing_node_ids.add(node_id)
        architecture["nodes"] = nodes

        matrix = (
            canonical.get("research_design_matrix")
            if is_full_result
            else None
        )
        if isinstance(matrix, list):
            for row in matrix:
                if not isinstance(row, dict):
                    continue
                question_id = str(row.get("research_question_id") or "")
                question_id = canonical_tag_id(question_id)
                row["research_question_id"] = question_id
                work_package_id = declared_question_work_packages.get(question_id)
                if work_package_id and work_package_id in existing_node_ids:
                    row["work_package_ids"] = [work_package_id]
                else:
                    row["work_package_ids"] = [
                        canonical_tag_id(item)
                        for item in row.get("work_package_ids") or []
                    ]
                objective_id = declared_question_objectives.get(question_id)
                if objective_id and objective_id in existing_node_ids:
                    row["objective_ids"] = [objective_id]
                else:
                    row["objective_ids"] = [
                        canonical_tag_id(item)
                        for item in row.get("objective_ids") or []
                    ]

        existing_edge_pairs = {
            (
                str(edge.get("source_id") or edge.get("source_node_id") or ""),
                str(edge.get("target_id") or edge.get("target_node_id") or ""),
            )
            for edge in edges
            if isinstance(edge, dict)
        }
        for question_id, work_package_id in declared_question_work_packages.items():
            objective_id = declared_question_objectives.get(question_id)
            if (
                not objective_id
                or objective_id not in existing_node_ids
                or work_package_id not in existing_node_ids
                or (objective_id, work_package_id) in existing_edge_pairs
            ):
                continue
            edges.append({
                "edge_id": (
                    f"edge-system-{objective_id.lower()}-{work_package_id.lower()}"
                ),
                "source_id": objective_id,
                "relation": "DECOMPOSES_TO",
                "target_id": work_package_id,
                "rationale": "按源材料中的研究问题—目标—研究内容闭环映射建立。",
            })
            existing_edge_pairs.add((objective_id, work_package_id))
        architecture["edges"] = edges
        if is_full_result:
            canonical["argument_architecture"] = architecture
        return canonical

    @staticmethod
    def _bind_argument_result_evidence(
        value: Any,
        facts: list[dict[str, Any]],
    ) -> Any:
        """Bind exact same-ID graph nodes to already approved fact evidence."""
        if not isinstance(value, dict):
            return value
        canonical = copy.deepcopy(value)
        # Evidence bindings/status are projector-owned for authoritative v8
        # Argument results.  Exact-ID fact enrichment remains legacy-only; doing
        # it here would create a second writer for the same semantic fact.
        if isinstance(canonical.get("authored_state"), dict):
            return canonical
        architecture = (
            canonical.get("argument_architecture")
            if isinstance(canonical.get("argument_architecture"), dict)
            else canonical
        )
        if not isinstance(architecture, dict):
            return canonical
        nodes = architecture.get("nodes")
        if not isinstance(nodes, list):
            return canonical
        fact_by_id: dict[str, dict[str, Any]] = {}
        for fact in facts:
            if not isinstance(fact, dict):
                continue
            fact_id = next(
                (
                    str(fact.get(key))
                    for key in ("claim_id", "fact_id", "item_id")
                    if fact.get(key)
                ),
                "",
            )
            if fact_id and isinstance(fact.get("source_refs"), list):
                fact_by_id[fact_id] = fact
        for node in nodes:
            if not isinstance(node, dict):
                continue
            fact = fact_by_id.get(str(node.get("node_id") or ""))
            if not fact or not fact.get("source_refs"):
                continue
            if not node.get("source_refs"):
                node["source_refs"] = copy.deepcopy(fact["source_refs"])
            if (
                node.get("status") == "UNKNOWN"
                and fact.get("knowledge_status")
                in {"CONFIRMED", "DOCUMENT_EXTRACTED"}
            ):
                node["status"] = "SUPPORTED"
        architecture["nodes"] = nodes
        if isinstance(canonical.get("argument_architecture"), dict):
            canonical["argument_architecture"] = architecture
        return canonical

    def _reproject_authoritative_argument_result(
        self,
        project_id: str,
        state: dict[str, Any],
        *,
        workflow_id: str | None,
        facts: list[dict[str, Any]] | None = None,
        proposal_contract: dict[str, Any] | None = None,
        argument_graph_seed: dict[str, Any] | None = None,
        project_subgraph: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Freshly project every v8+ Argument consumer from authored state.

        Persisted graph/matrix/status/evidence fields are caches only.  This
        helper is the sole ContextBuilder adapter from the persisted/repaired
        ProducerResult to its current derived consumer view.
        """
        raw = (
            self._repair_override(
                state,
                "P-ARGUMENT-ARCHITECTURE",
                workflow_id=workflow_id,
            )
            or self._result(
                project_id,
                "P-ARGUMENT-ARCHITECTURE",
                workflow_id=workflow_id,
            )
        )
        authored = raw.get("authored_state") if isinstance(raw, dict) else None
        if not isinstance(authored, dict):
            return None

        if facts is None:
            internal = self._result(project_id, "P-FACT-EXTRACT", "fact_candidates") or []
            public = self._approved_public_claims(project_id, workflow_id=workflow_id)
            facts = [*internal, *public]
        if proposal_contract is None:
            proposal_contract = (
                self._result(
                    project_id,
                    "P-PROJECT-DEFINITION-EXTRACT",
                    "proposal_contract",
                )
                or {}
            )
        if argument_graph_seed is None:
            argument_graph_seed = (
                self._result(
                    project_id,
                    "P-PROJECT-DEFINITION-EXTRACT",
                    "argument_graph_seed",
                    workflow_id=workflow_id,
                )
                or {}
            )
        if project_subgraph is None:
            project_definition = self._result(
                project_id,
                "P-PROJECT-DEFINITION-EXTRACT",
                "project_definition",
            )
            project_subgraph = (
                {
                    "item_ids": [x["item_id"] for x in project_definition.get("items", [])],
                    "relation_ids": [
                        x["relation_id"]
                        for x in project_definition.get("relations", [])
                    ],
                    "items": project_definition.get("items", []),
                    "relations": project_definition.get("relations", []),
                }
                if isinstance(project_definition, dict)
                else {}
            )

        projection_envelope = {
            "schema_version": "2.0",
            "prompt_id": "P-ARGUMENT-ARCHITECTURE",
            "prompt_version": str(
                self.pack.entry("P-ARGUMENT-ARCHITECTURE").get("prompt_version")
                or "8.0.0"
            ),
            "payload": {
                "proposal_contract": proposal_contract or {},
                "confirmed_facts": facts or [],
                "argument_graph_seed": argument_graph_seed or {},
                "project_subgraph": project_subgraph or {},
            },
        }
        return project_argument_authoritative_state(
            projection_envelope, copy.deepcopy(authored)
        )["result"]

    @staticmethod
    def _canonicalize_revision_plan_roles(value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        canonical = copy.deepcopy(value)
        plan = (
            canonical.get("revision_plan")
            if isinstance(canonical.get("revision_plan"), dict)
            else canonical
        )
        architecture = plan.get("narrative_architecture")
        if not isinstance(architecture, dict):
            return canonical
        for contract in architecture.get("section_contracts") or []:
            if not isinstance(contract, dict):
                continue
            roles: list[str] = []
            for role_value in contract.get("required_argument_roles") or []:
                role = SECTION_FUNCTION_ROLE_ALIASES.get(
                    str(role_value),
                    str(role_value),
                )
                if role not in roles:
                    roles.append(role)
            contract["required_argument_roles"] = roles
        if isinstance(canonical.get("revision_plan"), dict):
            canonical["revision_plan"] = plan
        return canonical

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

    @staticmethod
    def _planning_template_context(template: dict[str, Any]) -> dict[str, Any]:
        components = [
            component
            for component in template.get("components") or []
            if isinstance(component, dict) and component.get("component_id")
        ]
        rules = [
            str(rule).strip()
            for rule in template.get("format_rules") or []
            if str(rule).strip()
        ]
        global_argument = str(template.get("global_argument") or "").strip()
        if global_argument:
            rules.append(f"全局论证主线：{global_argument}")
        return {
            "template_id": str(template.get("template_id") or "template-current"),
            "component_ids": [str(component["component_id"]) for component in components],
            "rules": rules,
        }

    def _wf3_source_items(
        self,
        project: dict[str, Any],
        docs: list[dict[str, Any]],
        *,
        workflow_id: str | None,
    ) -> list[dict[str, Any]]:
        """Return references to persisted source objects without copying their content.

        P-SAFE-ONLINE-PACKAGE runs offline and needs provenance to decide what may be
        summarized into an outbound package. Object references are sufficient here;
        the subsequent security prompt remains responsible for minimization and
        anonymization.
        """
        refs: list[dict[str, Any]] = []
        seen: set[str] = set()

        def append(ref: dict[str, Any]) -> None:
            object_id = str(ref.get("object_id") or "").strip()
            if not object_id or object_id in seen:
                return
            seen.add(object_id)
            refs.append(ref)

        for document in docs[:24]:
            append({
                "object_id": str(document.get("document_id") or ""),
                "object_type": "SOURCE_DOCUMENT:" + str(document.get("document_role") or "OTHER"),
                "version": 1,
                "object_hash": document.get("document_hash"),
                "security_level": str(document.get("security_level") or project["security_level"]),
                "display_name": str(document.get("title") or document.get("document_id") or "项目材料")[:200],
            })

        active_workflow_id = self._resolve_workflow_id(workflow_id)
        active_exists = bool(
            active_workflow_id
            and self.db.fetchone("SELECT id FROM workflows WHERE id=?", (active_workflow_id,))
        )
        artifact_params: list[Any] = [project["id"]]
        if active_exists:
            source_ids = self._workflow_artifact_source_ids(active_workflow_id)
            if source_ids:
                placeholders = ",".join("?" for _ in source_ids)
                workflow_scope = f"a.workflow_id IN ({placeholders})"
                artifact_params.extend(source_ids)
            else:
                workflow_scope = "0=1"
        else:
            workflow_scope = "(a.workflow_id IS NULL OR w.status='COMPLETED')"
        rows = self.db.fetchall(
            f"""SELECT a.id,a.prompt_id,a.version,a.security_level,a.context_hash,a.created_at
               FROM artifacts a
               LEFT JOIN workflows w ON w.id=a.workflow_id
               WHERE a.project_id=? AND a.status='PASS'
                 AND {workflow_scope}
                 AND a.prompt_id IN (
                   'P-SCHEME-EXTRACT',
                   'P-PROJECT-DEFINITION-EXTRACT',
                   'P-FACT-EXTRACT',
                   'P-PROJECT-READINESS-CRITIC'
                 )
               ORDER BY a.created_at DESC,a.id DESC""",
            tuple(artifact_params),
        )
        latest_by_prompt: dict[str, dict[str, Any]] = {}
        for row in rows:
            prompt_id = str(row.get("prompt_id") or "")
            if prompt_id and prompt_id not in latest_by_prompt:
                latest_by_prompt[prompt_id] = row
        for prompt_id, row in latest_by_prompt.items():
            append({
                "object_id": str(row["id"]),
                "object_type": "PROMPT_ARTIFACT:" + prompt_id,
                "version": max(1, int(row.get("version") or 1)),
                "object_hash": row.get("context_hash"),
                "security_level": str(row.get("security_level") or project["security_level"]),
                "display_name": prompt_id,
            })
        return refs[:30]

    def _wf3_online_assist_payload(
        self,
        *,
        project: dict[str, Any],
        config: dict[str, Any],
        docs: list[dict[str, Any]],
        state: dict[str, Any],
        workflow_id: str | None,
    ) -> dict[str, Any]:
        options = copy.deepcopy(state.get("options")) if isinstance(state.get("options"), dict) else {}
        if isinstance(options.get("wf3"), dict):
            nested = copy.deepcopy(options["wf3"])
            nested.update({key: value for key, value in options.items() if key != "wf3"})
            options = nested
        current_argument = self._reproject_authoritative_argument_result(
            project["id"], state, workflow_id=workflow_id
        )
        argument_graph = (
            (current_argument or {}).get("argument_architecture")
            or self._result(
                project["id"],
                "P-ARGUMENT-ARCHITECTURE",
                "argument_architecture",
                workflow_id=workflow_id,
            )
            or self._result(
                project["id"],
                "P-PROJECT-DEFINITION-EXTRACT",
                "argument_graph_seed",
                workflow_id=workflow_id,
            )
        )
        research_need, origin = build_research_need(
            project_id=project["id"],
            options=options,
            project=project,
            config=config,
            argument_graph=argument_graph,
        )
        if research_need is None:
            missing = ["payload.research_need.question"]
            raise WorkflowInputRequired(
                "P-SAFE-ONLINE-PACKAGE",
                gate_type=WF3_INPUT_GATE_TYPE,
                missing_paths=missing,
                questions=input_gate_questions(),
                message=(
                    "WF-3 缺少可批准的公开研究问题，且无法从已确认的研究问题或研究差距中可靠推导。"
                    "请通过用户输入门禁补充研究问题；系统不会将 Schema 占位值发送给模型。"
                ),
            )
        topic_boundary_declared = (
            "allowed_public_topics" in options
            or "allowed_public_topics" in config
        )
        allowed_topics = [
            str(item).strip()
            for item in (
                options.get("allowed_public_topics")
                or config.get("allowed_public_topics")
                or ([] if topic_boundary_declared else ["公开学术资料"])
            )
            if str(item).strip()
        ]
        if not allowed_topics:
            raise WorkflowInputRequired(
                "P-SAFE-ONLINE-PACKAGE",
                gate_type=WF3_INPUT_GATE_TYPE,
                missing_paths=["payload.allowed_topics"],
                questions=allowed_topics_gate_questions(),
                message=(
                    "WF-3 尚未获得本次工作流允许对外检索的公开主题。"
                    "该审批边界由人工 Gate 确认，不交给模型推断。"
                ),
            )
        source_items = options.get("source_items") if isinstance(options.get("source_items"), list) else None
        if source_items is None:
            source_items = self._wf3_source_items(project, docs, workflow_id=workflow_id)
        target_task_type = normalize_target_task_type(options.get("target_task_type"))
        previous_resolution = state.get("wf3_input_resolution") if isinstance(state.get("wf3_input_resolution"), dict) else {}
        if (
            previous_resolution.get("origin") == "USER_INPUT_GATE"
            and str(previous_resolution.get("need_id") or research_need["need_id"]) == str(research_need["need_id"])
        ):
            origin = "USER_INPUT_GATE"
        state["wf3_input_resolution"] = {
            "origin": origin,
            "need_id": research_need["need_id"],
            "target_task_type": target_task_type,
            "source_item_count": len(source_items),
            "allowed_topics": list(allowed_topics),
        }
        return {
            "research_need": research_need,
            "source_items": source_items,
            "target_task_type": target_task_type,
            "allowed_topics": allowed_topics,
        }

    @staticmethod
    def _wf3_source_summary(source_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        summary: list[dict[str, Any]] = []
        for item in source_items:
            if not isinstance(item, dict) or not item.get("object_id"):
                continue
            object_type = str(item.get("object_type") or "SOURCE_OBJECT").strip()
            summary.append({
                "source_item_id": str(item["object_id"]),
                "abstracted_summary": f"来源类型：{object_type}",
                "original_security_level": str(item.get("security_level") or "INTERNAL"),
            })
        return summary

    @staticmethod
    def _wf3_deterministic_scan(
        package_candidate: dict[str, Any],
        config: dict[str, Any],
    ) -> dict[str, Any]:
        matches = find_sensitive_values(package_candidate, config, include_generic_patterns=True)
        matched_rules = sorted({f"{item.entity_type}:{item.path}" for item in matches})
        redacted_fields = sorted({
            str(item)
            for item in package_candidate.get("removed_fields") or []
            if str(item).strip()
        })
        return {
            "passed": not matches,
            "matched_rules": matched_rules,
            "redacted_fields": redacted_fields,
        }

    @staticmethod
    def _wf3_time_constraints(options: dict[str, Any]) -> dict[str, Any]:
        return normalize_wf3_time_constraints(options)

    @staticmethod
    def _wf3_iso_date(value: Any) -> str | None:
        text = str(value or "").strip()
        if not text:
            return None
        chinese = re.fullmatch(r"(\d{4})年(\d{1,2})月(\d{1,2})日", text)
        if chinese:
            parsed = date(
                int(chinese.group(1)),
                int(chinese.group(2)),
                int(chinese.group(3)),
            )
            return parsed.isoformat()
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
        except ValueError as exc:
            raise ValueError(
                "WF-3 valid_until must be an ISO date/date-time or YYYY年M月D日"
            ) from exc

    def _wf3_effective_safe_package(
        self,
        safe_package: dict[str, Any],
        *,
        state: dict[str, Any],
        workflow_id: str | None,
    ) -> dict[str, Any]:
        """Return the runtime-owned effective package without human TTL overlays."""

        effective = copy.deepcopy(safe_package)
        existing = self._wf3_iso_date(effective.get("valid_until"))
        effective["valid_until"] = existing or wf3_safe_package_valid_until()
        return effective

    def _wf3_outbound_approval(
        self,
        workflow_id: str | None,
    ) -> dict[str, str] | None:
        if not workflow_id:
            return None
        row = self.db.fetchone(
            """SELECT decision_json,updated_at FROM gates
               WHERE workflow_id=? AND gate_type='OUTBOUND_SECURITY_APPROVAL'
                 AND status='APPROVED'
               ORDER BY updated_at DESC,created_at DESC LIMIT 1""",
            (workflow_id,),
        )
        if not row:
            raise ValueError(
                "WF-3 import manifest requires an approved outbound security Gate"
            )
        try:
            decision = json.loads(row.get("decision_json") or "{}")
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("WF-3 outbound approval decision is invalid") from exc
        decided_by = str(decision.get("decided_by") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", decided_by):
            decided_by = "approver-" + sha256_text(decided_by)[:20]
        decided_at = str(
            decision.get("decided_at") or row.get("updated_at") or ""
        ).strip()
        if not decided_at:
            raise ValueError("WF-3 outbound approval has no decision timestamp")
        return {"approved_by": decided_by, "approved_at": decided_at}

    @staticmethod
    def _wf3_evidence_requirements(options: dict[str, Any]) -> list[str]:
        configured = [
            str(item).strip()
            for item in options.get("evidence_requirements") or []
            if str(item).strip()
        ]
        return configured or [
            "优先使用官方机构、标准组织、原始论文或其他一手公开来源",
            "每个实质结论必须绑定可访问来源并记录发布日期或访问时间",
            "明确区分来源事实、跨来源综合和模型推断",
            "比较代表性方法时同时记录适用边界、局限和时间范围",
        ]

    @staticmethod
    def _wf3_retrieval_contract(options: dict[str, Any]) -> dict[str, Any]:
        """Task-approved retrieval execution contract overrides (Phase 3).

        Only keys explicitly declared in the workflow options are emitted; all other
        contract fields derive from settings/provider defaults inside the search skill.
        The model never sees or generates these values.
        """

        contract: dict[str, Any] = {}
        channels = [
            str(item).strip().upper()
            for item in options.get("required_channels") or []
            if str(item).strip()
        ]
        if channels:
            contract["required_channels"] = list(dict.fromkeys(channels))
        providers = [
            str(item).strip().lower()
            for item in options.get("required_providers") or []
            if str(item).strip()
        ]
        if providers:
            contract["required_providers"] = list(dict.fromkeys(providers))
        if options.get("minimum_fulltext_sources_per_query") is not None:
            contract["minimum_fulltext_sources_per_query"] = max(
                0, int(options.get("minimum_fulltext_sources_per_query"))
            )
        if options.get("allow_snippet_only") is not None:
            contract["allow_snippet_only"] = bool(options.get("allow_snippet_only"))
        if options.get("require_web_discovery") is not None:
            contract["require_web_discovery"] = bool(options.get("require_web_discovery"))
        return contract

    @staticmethod
    def _wf3b_retrieval_summary(
        search_results: dict[str, Any],
        required_dimensions: list[str],
    ) -> dict[str, Any]:
        """Project runtime retrieval evidence into the WF-3B prompt contract."""

        sources = [
            item
            for item in (
                search_results.get("source_catalog")
                or search_results.get("sources")
                or []
            )
            if isinstance(item, dict)
        ]
        academic_providers = {"OPENALEX", "CROSSREF", "SEMANTIC_SCHOLAR"}
        academic_count = 0
        web_count = 0
        for source in sources:
            verification = (
                source.get("verification")
                if isinstance(source.get("verification"), dict)
                else {}
            )
            providers = [
                str(item).upper()
                for item in (
                    verification.get("discovery_providers")
                    or source.get("discovery_providers")
                    or [
                        verification.get("discovery_provider")
                        or source.get("discovery_provider")
                    ]
                )
                if str(item or "").strip()
            ]
            channel = str(
                verification.get("channel") or source.get("channel") or ""
            ).upper()
            if any(provider in academic_providers for provider in providers) or channel == "ACADEMIC":
                academic_count += 1
            else:
                web_count += 1

        sufficiency = (
            search_results.get("research_sufficiency")
            if isinstance(search_results.get("research_sufficiency"), dict)
            else {}
        )
        status = str(sufficiency.get("status") or "DEGRADED").upper()
        if status not in {"SUFFICIENT", "DEGRADED", "BLOCKING_FAILURE"}:
            status = "DEGRADED" if sufficiency.get("may_continue", True) else "BLOCKING_FAILURE"
        allowed_dimensions = set(required_dimensions or BACKGROUND_DIMENSIONS)
        uncovered: list[str] = []
        for gap in sufficiency.get("research_gaps") or []:
            if not isinstance(gap, dict):
                continue
            dimension = str(gap.get("dimension") or "").upper()
            if dimension in allowed_dimensions and dimension not in uncovered:
                uncovered.append(dimension)
        blocking_reasons = list(
            dict.fromkeys(
                str(item).strip()
                for item in sufficiency.get("blocking_reasons") or []
                if str(item).strip()
            )
        )
        return {
            "status": status,
            "web_hit_count": web_count,
            "academic_hit_count": academic_count,
            "uncovered_dimensions": uncovered,
            "blocking_reasons": blocking_reasons,
        }

    @staticmethod
    def _wf3b_extracted_passages(search_results: dict[str, Any]) -> list[dict[str, Any]]:
        """Add the prompt-contract channel to archived WF-3B passages."""

        academic_providers = {"openalex", "crossref", "semantic_scholar"}
        web_providers = {"searxng", "browser", "browser_search"}
        channels_by_source: dict[str, str] = {}
        for source in search_results.get("source_catalog") or []:
            if not isinstance(source, dict):
                continue
            source_id = str(source.get("source_id") or "").strip()
            providers = {
                str(item or "").strip().lower()
                for item in source.get("discovery_providers") or []
                if str(item or "").strip()
            }
            if providers & web_providers:
                channel = "WEB_SEARCH"
            elif providers & academic_providers:
                channel = "ACADEMIC"
            else:
                channel = "WEB_SEARCH"
            if source_id:
                channels_by_source[source_id] = channel

        projected: list[dict[str, Any]] = []
        for passage in search_results.get("passages") or []:
            if not isinstance(passage, dict):
                continue
            source_ref = passage.get("source_ref")
            if not isinstance(source_ref, dict):
                continue
            passage_id = str(passage.get("passage_id") or "").strip()
            text = str(passage.get("text") or "").strip()
            relevance = str(passage.get("relevance") or "").strip()
            if not passage_id or not text or not relevance:
                continue
            source_id = str(source_ref.get("source_id") or "").strip()
            channel = str(passage.get("channel") or "").strip().upper()
            if channel not in {"ACADEMIC", "WEB_SEARCH"}:
                channel = channels_by_source.get(source_id, "WEB_SEARCH")
            projected.append({
                # The synthesis model must cite source_id.  Giving the same
                # evidence two unrelated opaque IDs caused it to transform
                # ``passage-<id>`` into a nonexistent ``public-src-<id>``.
                # Align the prompt-only passage identifier with its source;
                # immutable archive identities remain unchanged.
                "passage_id": source_id or passage_id,
                "source_ref": source_ref,
                "channel": channel,
                "text": text,
                "relevance": relevance,
            })
        return projected

    @staticmethod
    def _wf3b_prompt_research_plan(plan: dict[str, Any]) -> dict[str, Any]:
        """Project the runtime plan onto the narrower synthesis/critic schema."""

        source = plan if isinstance(plan, dict) else {}
        projected_queries: list[dict[str, Any]] = []
        for item in source.get("queries") or []:
            if not isinstance(item, dict):
                continue
            projected_queries.append({
                key: copy.deepcopy(item.get(key))
                for key in ("query_id", "query", "dimension", "purpose")
                if item.get(key) is not None
            })
        projected = {
            key: copy.deepcopy(source.get(key))
            for key in (
                "plan_id",
                "task_type",
                "topic_id",
                "required_dimensions",
                "time_scope",
                "evidence_requirements",
                "prohibited_inferences",
                "binding_contract_version",
            )
            if source.get(key) is not None
        }
        projected["queries"] = projected_queries
        return projected

    def _approved_public_claims(
        self,
        project_id: str,
        *,
        workflow_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return only claims accepted by the WF-3 bound to the active workflow."""
        active_workflow_id = self._resolve_workflow_id(workflow_id)
        params: list[Any] = [project_id]
        workflow_filter = ""
        if active_workflow_id:
            source_ids = self._workflow_artifact_source_ids(active_workflow_id)
            wf3_ids: list[str] = []
            for source_id in source_ids:
                row = self.db.fetchone(
                    "SELECT workflow_type FROM workflows WHERE id=?",
                    (source_id,),
                )
                if row and row.get("workflow_type") == "WF-3_HYBRID_ONLINE_ASSIST":
                    wf3_ids.append(source_id)
            if not wf3_ids:
                return []
            placeholders = ",".join("?" for _ in wf3_ids)
            workflow_filter = f" AND w.id IN ({placeholders})"
            params.extend(wf3_ids)
        review_row = self.db.fetchone(
            f"""SELECT a.workflow_id,a.content_json
               FROM artifacts a
               JOIN workflows w ON w.id=a.workflow_id
               JOIN gates g ON g.workflow_id=w.id
               WHERE a.project_id=?
                 AND a.prompt_id='P-ONLINE-RESULT-IMPORT-CRITIC'
                 AND a.artifact_type IN ('PROMPT_OUTPUT','SKILL_ENRICHED_PROMPT_OUTPUT')
                 AND a.status='PASS'
                 AND w.workflow_type='WF-3_HYBRID_ONLINE_ASSIST'
                 AND w.status='COMPLETED'
                 AND g.gate_type='ONLINE_RESULT_IMPORT_APPROVAL'
                 AND g.status='APPROVED'
                 {workflow_filter}
               ORDER BY w.updated_at DESC,a.version DESC,a.created_at DESC LIMIT 1""",
            tuple(params),
        )
        if not review_row:
            return []
        review_output = json.loads(review_row["content_json"])
        review = review_output.get("result") if isinstance(review_output, dict) else None
        accepted_ids = {
            str(item)
            for item in (review or {}).get("accepted_claim_ids") or []
            if str(item).strip()
        }
        if not accepted_ids:
            return []
        synthesis = self._result(
            project_id,
            "P-PUBLIC-RESEARCH-SYNTHESIS",
            workflow_id=str(review_row["workflow_id"]),
            exact_workflow=True,
        ) or {}
        return [
            copy.deepcopy(claim)
            for claim in synthesis.get("claims") or []
            if isinstance(claim, dict) and str(claim.get("claim_id") or "") in accepted_ids
        ]

    def _human_resolution_artifacts_for_prompt(
        self,
        *,
        state: dict[str, Any],
        prompt_id: str,
        workflow_id: str | None,
    ) -> list[dict[str, Any]]:
        if not workflow_id:
            return []

        section_id = str(state.get("active_section_id") or "").strip() or None
        workflow_row = self.db.fetchone(
            "SELECT current_step FROM workflows WHERE id=?",
            (workflow_id,),
        )
        workflow_step = int((workflow_row or {}).get("current_step") or 0)
        scope_key = human_resolution_scope_key(
            prompt_id,
            section_id=section_id,
            workflow_step=workflow_step,
        )
        index = state.get("human_resolution_artifact_ids") or {}
        indexed_ids = [
            str(item)
            for item in (index.get(scope_key) or [])
            if str(item).strip()
        ]
        # Read-only migration visibility.  Section prompts accept legacy
        # prompt-only artifacts only when their Gate target can be proven to
        # belong to the current section below.
        for item in index.get(prompt_id) or []:
            normalized = str(item).strip()
            if normalized and normalized not in indexed_ids:
                indexed_ids.append(normalized)
        if not indexed_ids:
            # The workflow-state index is the visibility boundary for immutable
            # resolution artifacts.  Querying the project-wide artifact table
            # without that index both weakens scope and creates avoidable SQLite
            # contention in concurrent authoring workflows.
            return []

        params: list[Any] = [workflow_id, prompt_id]
        id_filter = ""
        if indexed_ids:
            placeholders = ",".join("?" for _ in indexed_ids)
            id_filter = f" AND id IN ({placeholders})"
            params.extend(indexed_ids)
        rows = self.db.fetchall(
            f"""SELECT id,version,content_json
                FROM artifacts
                WHERE workflow_id=?
                  AND artifact_type='HUMAN_RESOLUTION'
                  AND prompt_id=?
                  AND status='PASS'
                  {id_filter}
                ORDER BY version ASC,created_at ASC,id ASC""",
            tuple(params),
        )

        selected: list[tuple[int, dict[str, Any], tuple[str, str]]] = []
        for row in rows:
            try:
                payload = json.loads(row.get("content_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            if str(payload.get("workflow_id") or workflow_id) != str(workflow_id):
                continue
            if str(payload.get("prompt_id") or prompt_id) != prompt_id:
                continue
            payload_scope = str(payload.get("scope_key") or "").strip()
            payload_section = str(payload.get("section_id") or "").strip() or None
            if payload_scope:
                if payload_scope != scope_key:
                    continue
            elif section_id:
                if payload_section != section_id:
                    gate = self.db.fetchone(
                        "SELECT target_id FROM gates WHERE id=? AND workflow_id=?",
                        (str(payload.get("gate_id") or ""), workflow_id),
                    )
                    inferred = section_id_for_run(
                        state,
                        str((gate or {}).get("target_id") or payload.get("target_run_id") or ""),
                    )
                    if inferred != section_id:
                        continue
            elif payload_section:
                # A section-bound legacy artifact must not be exposed to a
                # workflow-step prompt merely because the prompt id matches.
                continue
            resolution = payload.get("resolution")
            if not isinstance(resolution, dict):
                continue
            try:
                resolution = canonicalize_human_resolution(resolution)
            except ValueError:
                continue
            question_id = str(
                resolution.get("question_id")
                or resolution.get("resolution_id")
                or ""
            ).strip()
            question_text = " ".join(
                str(resolution.get("question") or "").split()
            )
            question_identity = (question_id, question_text)
            # Generated question ids are only positional within one Gate and may
            # be reused for unrelated questions in a later round. Supersession
            # therefore requires both the id and normalized question text.
            # Distinct questions under the same broad payload object remain
            # independently visible.
            selected = [
                item
                for item in selected
                if item[2] != question_identity
            ]
            selected.append(
                (
                    int(row.get("version") or 0),
                    copy.deepcopy(resolution),
                    question_identity,
                )
            )

        return [
            resolution
            for _, resolution, _ in sorted(
                selected,
                key=lambda item: (item[0], str(item[1].get("resolution_id") or "")),
            )
        ][-50:]
    def _human_resolutions_for_prompt(
        self,
        state: dict[str, Any],
        prompt_id: str,
        workflow_id: str | None,
    ) -> list[dict[str, Any]]:
        section_id = str(state.get("active_section_id") or "").strip() or None
        workflow_row = (
            self.db.fetchone("SELECT current_step FROM workflows WHERE id=?", (workflow_id,))
            if workflow_id
            else None
        )
        workflow_step = int((workflow_row or {}).get("current_step") or 0)
        scope_key = human_resolution_scope_key(
            prompt_id,
            section_id=section_id,
            workflow_step=workflow_step,
        )
        artifact_index = state.get("human_resolution_artifact_ids") or {}
        indexed_authority_exists = bool(
            artifact_index.get(scope_key) or artifact_index.get(prompt_id)
        )
        artifact_resolutions = self._human_resolution_artifacts_for_prompt(
            state=state,
            prompt_id=prompt_id,
            workflow_id=workflow_id,
        )
        if artifact_resolutions or indexed_authority_exists:
            # Once an immutable Artifact index exists, invalid, stale, or
            # cross-scope artifacts must fail closed.  Falling back to mutable
            # legacy state would silently resurrect an answer that the current
            # scoped Artifact set did not authorize.
            return artifact_resolutions

        # Migration-only fallback. New Gate decisions are persisted as immutable
        # HUMAN_RESOLUTION artifacts and must not repopulate workflow state.
        legacy_index = state.get("human_resolutions") or {}
        records = list(legacy_index.get(scope_key) or [])
        for item in legacy_index.get(prompt_id) or []:
            if item not in records:
                records.append(item)
        resolved: list[dict[str, Any]] = []
        for item in records[-50:]:
            if not isinstance(item, dict):
                continue
            item_scope = str(item.get("scope_key") or "").strip()
            item_section = str(item.get("section_id") or "").strip() or None
            if item_scope and item_scope != scope_key:
                continue
            if section_id and not item_scope and item_section != section_id:
                # Unscoped prompt-only state is ambiguous across proposal
                # sections and is intentionally not injected.
                continue
            if not section_id and item_section:
                continue
            try:
                resolved.append(canonicalize_human_resolution(item))
            except ValueError:
                continue
        if resolved or prompt_id != "P-SAFE-ONLINE-PACKAGE":
            return resolved

        wf3_resolution = state.get("wf3_input_resolution") if isinstance(state.get("wf3_input_resolution"), dict) else {}
        origin = str(wf3_resolution.get("origin") or "")
        if origin not in {"USER_INPUT_GATE", "WORKFLOW_OPTIONS"}:
            return []

        options = state.get("options") if isinstance(state.get("options"), dict) else {}
        need = options.get("research_need") if isinstance(options.get("research_need"), dict) else {}
        question = str(need.get("question") or "").strip()
        if not question:
            return []

        questions = input_gate_questions()
        answers = [
            {"question_id": "wf3-research-question", "answer": question},
            {"question_id": "wf3-reason-online-needed", "answer": need.get("reason_online_needed")},
            {"question_id": "wf3-desired-output", "answer": need.get("desired_output")},
            {"question_id": "wf3-target-task-type", "answer": options.get("target_task_type") or "PUBLIC_RESEARCH"},
        ]
        gate_id = f"workflow-options-{workflow_id or 'unknown'}"
        decided_by = "workflow-start-request"
        decided_role = "PROJECT_OWNER"

        gate = None
        if workflow_id:
            gate = self.db.fetchone(
                """SELECT id,questions_json,decision_json,required_role
                   FROM gates
                   WHERE workflow_id=? AND gate_type=? AND status='APPROVED'
                   ORDER BY updated_at DESC LIMIT 1""",
                (workflow_id, WF3_INPUT_GATE_TYPE),
            )
        if gate:
            try:
                stored_questions = json.loads(gate.get("questions_json") or "[]")
                decision = json.loads(gate.get("decision_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                stored_questions, decision = [], {}
            if isinstance(stored_questions, list) and stored_questions:
                questions = stored_questions
            if isinstance(decision.get("answers"), list) and decision.get("answers"):
                answers = decision["answers"]
            gate_id = str(gate.get("id") or gate_id)
            decided_by = str(decision.get("decided_by") or "persisted-approved-gate")
            decided_role = str(decision.get("decided_role") or gate.get("required_role") or "PROJECT_OWNER")
        elif origin == "USER_INPUT_GATE":
            gate_id = f"legacy-approved-wf3-input-{workflow_id or 'unknown'}"
            decided_by = "persisted-workflow-state"

        try:
            return build_human_resolutions(
                gate_id=gate_id,
                prompt_id=prompt_id,
                questions=questions,
                answers=answers,
                decided_by=decided_by,
                decided_role=decided_role,
            )
        except ValueError:
            return []

    def _first_section(self, docs: list[dict[str, Any]], roles: set[str] | None = None) -> dict[str, Any] | None:
        for doc in docs:
            if roles and doc.get("document_role") not in roles:
                continue
            if doc.get("sections"):
                return doc["sections"][0]
        return None

    def _apply_common_payload(self, envelope: dict[str, Any], prompt_id: str, project: dict[str, Any], config: dict[str, Any], docs: list[dict[str, Any]], context_hash: str, state: dict[str, Any], workflow_id: str | None) -> None:
        payload = envelope["payload"]
        strict_live_inputs = str(getattr(self, "runtime_mode", "REPLAY")).upper() == "LIVE"
        material_dependent_fields = {
            "object_context",
            "original_object",
            "source_documents",
            "guide_documents",
            "reference_document",
            "source_section",
            "document_structure",
            "section_tree",
        }
        if strict_live_inputs and not docs and material_dependent_fields.intersection(payload):
            raise WorkflowInputRequired(
                prompt_id,
                gate_type=PROJECT_MATERIAL_INPUT,
                missing_paths=[f"payload.{field}" for field in sorted(material_dependent_fields.intersection(payload))],
                questions=material_input_questions(PROJECT_MATERIAL_INPUT),
                message="当前步骤缺少真实项目材料。请先上传材料；系统不会使用 Replay 或 Schema 占位对象代替。",
            )
        security_profile = self._security_profile(project, config, context_hash)
        for field in ["security_policy"]:
            if field in payload:
                self._set_path_if_valid(prompt_id, envelope, f"payload.{field}", security_profile)
        if "security_constraints" in payload:
            self._set_path_if_valid(prompt_id, envelope, "payload.security_constraints", envelope["security_context"])

        guide_docs = [d for d in docs if d.get("document_role") == "APPLICATION_GUIDE"]
        if not guide_docs and prompt_id in {"P-SCHEME-EXTRACT", "P-SCHEME-CRITIC"}:
            guide_keywords = (
                "指南", "通知", "申报", "申请", "要求", "项目属性",
                "篇幅", "边界", "执行约束",
            )
            guide_docs = []
            for document in docs:
                selected = [
                    section
                    for section in document.get("sections", [])
                    if any(keyword in str(section.get("title") or "") for keyword in guide_keywords)
                ]
                if not selected:
                    continue
                compact_document = copy.deepcopy(document)
                compact_document["sections"] = selected
                guide_docs.append(compact_document)
        if strict_live_inputs and prompt_id in {"P-SCHEME-EXTRACT", "P-SCHEME-CRITIC"} and not guide_docs:
            raise WorkflowInputRequired(
                prompt_id,
                gate_type=APPLICATION_GUIDE_INPUT,
                missing_paths=["payload.guide_documents", "payload.document_structure"],
                questions=material_input_questions(APPLICATION_GUIDE_INPUT),
                message="未找到 APPLICATION_GUIDE 材料，且其他材料中没有可可靠识别的指南章节。",
            )
        source_docs = [d for d in docs if d.get("document_role") != "REFERENCE_PROPOSAL"] or docs
        reference_doc = next((d for d in docs if d.get("document_role") == "REFERENCE_PROPOSAL"), None)
        if strict_live_inputs and reference_doc is None and prompt_id in {"P-TEMPLATE-EXTRACT", "P-TEMPLATE-CRITIC"}:
            raise WorkflowInputRequired(
                prompt_id,
                gate_type=REFERENCE_TEMPLATE_INPUT,
                missing_paths=["payload.reference_document", "payload.section_tree"],
                questions=material_input_questions(REFERENCE_TEMPLATE_INPUT),
                message="未找到 REFERENCE_PROPOSAL。当前申请书不得被静默当作参考模板。",
            )
        active_section_id = state.get("active_section_id")
        current_section = None
        explicit_active_section = state.get("active_section")
        if (
            isinstance(explicit_active_section, dict)
            and explicit_active_section.get("section_id")
            and (
                not active_section_id
                or str(explicit_active_section.get("section_id")) == str(active_section_id)
            )
        ):
            current_section = copy.deepcopy(explicit_active_section)
        if current_section is None and active_section_id:
            current_section = next(
                (section for doc in docs for section in doc.get("sections", []) if section.get("section_id") == active_section_id),
                None,
            )
        current_section = current_section or self._first_section(docs, {"CURRENT_PROPOSAL"})
        if strict_live_inputs and "source_section" in payload and current_section is None:
            raise WorkflowInputRequired(
                prompt_id,
                gate_type=CURRENT_PROPOSAL_INPUT,
                missing_paths=["payload.source_section"],
                questions=material_input_questions(CURRENT_PROPOSAL_INPUT),
                message="当前步骤需要 CURRENT_PROPOSAL 中的真实章节；其他材料不会被替代为待写章节。",
            )

        requested_target_ids = [
            str(section_id)
            for section_id in (state.get("options") or {}).get("target_section_ids") or []
            if section_id
        ]
        requested_target_sections = [
            section
            for section_id in requested_target_ids
            for document in docs
            for section in document.get("sections", [])
            if str(section.get("section_id") or "") == section_id
        ]
        if prompt_id == "P-REVISION-PLAN" and requested_target_sections:
            current_section = requested_target_sections[0]

        replacements: list[tuple[str, Any]] = []
        human_override_paths: set[str] = set()
        if prompt_id == "P-SAFE-ONLINE-PACKAGE":
            wf3_payload = self._wf3_online_assist_payload(
                project=project,
                config=config,
                docs=docs,
                state=state,
                workflow_id=workflow_id,
            )
            replacements.extend([
                ("payload.research_need", wf3_payload["research_need"]),
                ("payload.source_items", wf3_payload["source_items"]),
                ("payload.target_task_type", wf3_payload["target_task_type"]),
                ("payload.allowed_topics", wf3_payload["allowed_topics"]),
            ])
        if prompt_id == "P-SAFE-ONLINE-PACKAGE-CRITIC":
            wf3_payload = self._wf3_online_assist_payload(
                project=project,
                config=config,
                docs=docs,
                state=state,
                workflow_id=workflow_id,
            )
            package_candidate = self._result(project["id"], "P-SAFE-ONLINE-PACKAGE") or {}
            # Approved public topics are a workflow-owned authoritative object.
            # The Critic must consume the exact same value as the Producer; do
            # not rebuild this boundary independently from project security
            # configuration, which may legitimately leave the legacy field empty.
            critic_security_policy = copy.deepcopy(security_profile)
            critic_security_policy["allowed_public_topics"] = list(
                wf3_payload["allowed_topics"]
            )
            replacements.extend([
                ("payload.allowed_topics", list(wf3_payload["allowed_topics"])),
                ("payload.security_policy", critic_security_policy),
                ("payload.source_summary", self._wf3_source_summary(wf3_payload["source_items"])),
                ("payload.deterministic_scan", self._wf3_deterministic_scan(package_candidate, config)),
            ])
        if prompt_id == "P-PUBLIC-RESEARCH-PLAN":
            options = state.get("options") if isinstance(state.get("options"), dict) else {}
            safe_package_for_plan = self._result(project["id"], "P-SAFE-ONLINE-PACKAGE") or {}
            task_type = normalize_target_task_type(
                options.get("target_task_type") or safe_package_for_plan.get("task_type")
            )
            known_sources = options.get("known_public_sources")
            if not isinstance(known_sources, list):
                known_sources = []
            replacements.extend([
                ("payload.task_type", task_type),
                ("payload.known_public_sources", known_sources),
                ("payload.time_constraints", self._wf3_time_constraints(options)),
                ("payload.evidence_requirements", self._wf3_evidence_requirements(options)),
                ("payload.retrieval_contract", self._wf3_retrieval_contract(options)),
            ])
        if prompt_id == "P-PUBLIC-RESEARCH-PLAN-SCOPE-CRITIC":
            wf3_payload = self._wf3_online_assist_payload(
                project=project,
                config=config,
                docs=docs,
                state=state,
                workflow_id=workflow_id,
            )
            safe_package = self._result(
                project["id"],
                "P-SAFE-ONLINE-PACKAGE",
                workflow_id=workflow_id,
                exact_workflow=True,
            ) or {}
            plan = self._result(
                project["id"],
                "P-PUBLIC-RESEARCH-PLAN",
                workflow_id=workflow_id,
                exact_workflow=True,
            ) or {}
            replacements.extend([
                ("payload.approved_boundary", {
                    "task_description": str(safe_package.get("task_description") or wf3_payload["research_need"].get("question") or "").strip(),
                    "allowed_topics": list(wf3_payload["allowed_topics"]),
                    "allowed_context": list(safe_package.get("allowed_context") or []),
                    "prohibited_inferences": list(safe_package.get("prohibited_inferences") or []),
                    "prohibited_outputs": list(safe_package.get("prohibited_outputs") or []),
                }),
                ("payload.research_questions", list(plan.get("research_questions") or [])),
                ("payload.executable_queries", list(plan.get("queries") or [])),
            ])
        if prompt_id == WF3B_PLAN_PROMPT:
            options = state.get("options") if isinstance(state.get("options"), dict) else {}
            required_dimensions = [
                str(item).upper()
                for item in options.get("required_dimensions") or BACKGROUND_DIMENSIONS
                if str(item).upper() in BACKGROUND_DIMENSIONS
            ]
            optional_dimensions = [
                str(item).upper()
                for item in options.get("optional_dimensions") or []
                if str(item).upper() in BACKGROUND_DIMENSIONS
                and str(item).upper() not in required_dimensions
            ]
            retrieval_contract = background_execution_contract(
                self._wf3_retrieval_contract(options)
            )
            replacements.extend([
                ("payload.task_type", "PUBLIC_BACKGROUND_RESEARCH"),
                ("payload.topic", {
                    "topic_id": str(options.get("topic_id") or ""),
                    "topic_description": str(options.get("topic") or "").strip(),
                }),
                ("payload.required_dimensions", required_dimensions),
                ("payload.optional_dimensions", optional_dimensions),
                ("payload.known_public_sources", list(options.get("known_public_sources") or [])),
                ("payload.time_constraints", self._wf3_time_constraints(options)),
                ("payload.evidence_requirements", self._wf3_evidence_requirements(options)),
                ("payload.retrieval_contract", retrieval_contract),
            ])
        if prompt_id == WF3B_PLAN_CRITIC_PROMPT:
            options = state.get("options") if isinstance(state.get("options"), dict) else {}
            safe_package_for_plan = self._result(
                project["id"],
                "P-SAFE-ONLINE-PACKAGE",
                workflow_id=workflow_id,
                exact_workflow=True,
            ) or {}
            plan = self._result(
                project["id"],
                WF3B_PLAN_PROMPT,
                workflow_id=workflow_id,
                exact_workflow=True,
            ) or {}
            approved_topics = list(
                options.get("allowed_public_topics")
                or config.get("allowed_public_topics")
                or [str(options.get("topic") or "").strip()]
            )
            replacements.extend([
                ("payload.approved_boundary", {
                    "task_description": str(safe_package_for_plan.get("task_description") or options.get("topic") or "").strip(),
                    "topic_description": str(options.get("topic") or "").strip(),
                    "allowed_topics": [str(item).strip() for item in approved_topics if str(item).strip()],
                    "allowed_context": list(safe_package_for_plan.get("allowed_context") or []),
                    "prohibited_inferences": list(safe_package_for_plan.get("prohibited_inferences") or []),
                    "prohibited_outputs": list(safe_package_for_plan.get("prohibited_outputs") or []),
                }),
                ("payload.required_dimensions", list(options.get("required_dimensions") or BACKGROUND_DIMENSIONS)),
                ("payload.executable_queries", list(plan.get("queries") or [])),
            ])
        human_resolutions = self._human_resolutions_for_prompt(state, prompt_id, workflow_id)
        if "human_resolutions" in payload or human_resolutions:
            replacements.append(("payload.human_resolutions", human_resolutions))
        resolved_overrides = resolution_overrides(human_resolutions)
        for path, value in resolved_overrides.items():
            human_override_paths.add(path)
            replacements.append((path, copy.deepcopy(value)))
        # Legacy state is a migration-only fallback and never overrides a
        # versioned HUMAN_RESOLUTION artifact for the same target path.
        for path, value in ((state.get("human_input_overrides") or {}).get(prompt_id) or {}).items():
            if isinstance(path, str) and path and path not in resolved_overrides:
                human_override_paths.add(path)
                replacements.append((path, copy.deepcopy(value)))
        if prompt_id == "P-REVISION-PLAN" and requested_target_sections:
            replacements.extend([
                ("scope.target_object_ids", requested_target_ids),
                ("scope.read_only_object_ids", []),
                ("scope.protected_object_ids", []),
            ])
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
            if prompt_id == "P-REVISION-PLAN" and requested_target_sections:
                linked = requested_target_sections[1:]
            elif prompt_id in {"P-ARGUMENT-ARCHITECTURE", "P-ARGUMENT-ARCHITECTURE-CRITIC", "P-REVISION-PLAN", "P-REVISION-PLAN-CRITIC", "P-WRITE-BLUEPRINT", "P-WRITE-BLUEPRINT-CRITIC", "P-WRITE-CONTENT", "P-WRITE-CRITIC", "P-EXPRESSION-POLISH", "P-EXPRESSION-CRITIC", "P-INTEGRATION-CRITIC"}:
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
            replacements.append((
                "payload.original_object",
                {
                    "object_type": "SOURCE_DOCUMENT",
                    "object_id": first["document_id"],
                    "object_hash": first["document_hash"],
                    "content": first,
                },
            ))
        if "existing_labels" in payload:
            replacements.append((
                "payload.existing_labels",
                [
                    {
                        "object_id": doc["document_id"],
                        "security_level": doc["security_level"],
                        "basis": "上传材料登记时指定的安全等级",
                    }
                    for doc in docs
                ],
            ))
        if "deterministic_findings" in payload and prompt_id.endswith("-CRITIC"):
            producer_output = self._latest_output(project["id"], prompt_id.removesuffix("-CRITIC"), workflow_id=workflow_id)
            replacements.append((
                "payload.deterministic_findings",
                list((producer_output or {}).get("findings") or []),
            ))
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
                    if not str(sec.get("text") or "").strip():
                        continue
                    spans.append({"span_id": sec["section_id"], "text": sec["text"], "source_ref": self._source_ref(doc, sec)})
            if spans:
                replacements.append(("payload.source_spans", spans))
        if "authority_rules" in payload:
            replacements.append(("payload.authority_rules", {
                "version": "2.0",
                "ordered_source_types": [
                    "USER_CONFIRMATION", "APPLICATION_GUIDE", "TASK_BOOK",
                    "CONTRACT", "CURRENT_PROPOSAL", "EVIDENCE_MATERIAL",
                    "TECHNICAL_MATERIAL", "HISTORICAL_DOCUMENT",
                    "REFERENCE_PROPOSAL", "PUBLIC_SOURCE", "MODEL_INFERENCE",
                ],
            }))
        if "section_tree" in payload and reference_doc:
            tree = []
            ancestors: list[dict[str, Any]] = []
            for section in reference_doc.get("sections", []):
                level = int(section.get("level", 0))
                while ancestors and int(ancestors[-1].get("level", 0)) >= level:
                    ancestors.pop()
                tree.append({
                    "section_id": section["section_id"],
                    "title": str(section.get("title") or "未命名章节"),
                    "level": level,
                    "parent_section_id": ancestors[-1]["section_id"] if ancestors else None,
                })
                ancestors.append(section)
            replacements.append(("payload.section_tree", tree))
        if "style_summary" in payload:
            replacements.append((
                "payload.style_summary",
                {
                    "paragraph_styles": [],
                    "heading_styles": [],
                    "table_styles": [],
                },
            ))
        if "document_structure" in payload and guide_docs:
            structure = [
                {
                    "section_id": section["section_id"],
                    "title": section["title"],
                    "level": section["level"],
                    "text_hash": section["text_hash"],
                }
                for document in guide_docs
                for section in document.get("sections", [])
            ]
            replacements.append(("payload.document_structure", structure))
        if "extraction_scope" in payload:
            replacements.append(("payload.extraction_scope", ["全部已上传材料及其章节"]))

        if "relation_matrix" in payload:
            replacements.append(("payload.relation_matrix", {
                "version": self.pack.relation_matrix["version"],
                "allowed_relations": copy.deepcopy(self.pack.relation_matrix["allowed_relations"]),
            }))

        current_proposal_sections: list[dict[str, Any]] = []
        for document in docs:
            if document.get("document_role") != "CURRENT_PROPOSAL":
                continue
            for section in document.get("sections", []):
                if not isinstance(section, dict):
                    continue
                enriched_section = copy.deepcopy(section)
                enriched_section["_source_ref"] = {
                    "source_id": document.get("document_id"),
                    "source_type": "CURRENT_PROPOSAL",
                    "document_version_id": document.get("document_version_id"),
                    "section_id": section.get("section_id"),
                    "span_start": 0,
                    "span_end": len(str(section.get("text") or "")),
                    "quoted_text": str(section.get("text") or ""),
                    "source_hash": (
                        section.get("text_hash")
                        or document.get("document_hash")
                    ),
                    "authority_rank": int(document.get("authority_rank") or 85),
                    "security_level": (
                        section.get("security_level")
                        or document.get("security_level")
                        or "INTERNAL"
                    ),
                }
                current_proposal_sections.append(enriched_section)
        internal_facts_for_argument = (
            self._result(project["id"], "P-FACT-EXTRACT", "fact_candidates")
            or []
        )
        public_claims_for_argument = self._approved_public_claims(
            project["id"], workflow_id=workflow_id
        )
        internal_facts = internal_facts_for_argument
        public_claims = public_claims_for_argument
        facts = [*internal_facts, *public_claims]
        project_definition = self._result(
            project["id"], "P-PROJECT-DEFINITION-EXTRACT", "project_definition"
        )
        proposal_contract = self._result(
            project["id"], "P-PROJECT-DEFINITION-EXTRACT", "proposal_contract"
        )
        argument_graph_seed = self._result(
            project["id"],
            "P-PROJECT-DEFINITION-EXTRACT",
            "argument_graph_seed",
            workflow_id=workflow_id,
        )
        project_subgraph_for_argument = (
            {
                "item_ids": [x["item_id"] for x in project_definition.get("items", [])],
                "relation_ids": [
                    x["relation_id"] for x in project_definition.get("relations", [])
                ],
                "items": project_definition.get("items", []),
                "relations": project_definition.get("relations", []),
            }
            if isinstance(project_definition, dict)
            else {}
        )
        raw_argument_result = (
            self._repair_override(state, "P-ARGUMENT-ARCHITECTURE", workflow_id=workflow_id)
            or self._result(project["id"], "P-ARGUMENT-ARCHITECTURE")
        )
        authoritative_projection = self._reproject_authoritative_argument_result(
            project["id"],
            state,
            workflow_id=workflow_id,
            facts=facts,
            proposal_contract=proposal_contract or {},
            argument_graph_seed=argument_graph_seed or {},
            project_subgraph=project_subgraph_for_argument,
        )
        if authoritative_projection is not None:
            canonical_argument_result = authoritative_projection
        else:
            # Pre-v8 migration compatibility only. Legacy projections have no
            # authoritative source and therefore retain the old reconciliation.
            canonical_argument_result = self._canonicalize_argument_result_from_sections(
                raw_argument_result, current_proposal_sections
            )
            canonical_argument_result = self._bind_argument_result_evidence(
                canonical_argument_result, facts
            )

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
        is_background_workflow = str(state.get("workflow_type") or "") == WF3B_WORKFLOW_TYPE
        if is_background_workflow:
            result_map["research_plan"] = (WF3B_PLAN_PROMPT, None)
            result_map["synthesis_candidate"] = (WF3B_SYNTHESIS_PROMPT, None)
        background_critic_synthesis: dict[str, Any] | None = None
        for field, (producer, key) in result_map.items():
            if field in payload:
                if (
                    producer == "P-ARGUMENT-ARCHITECTURE"
                    and canonical_argument_result is not None
                ):
                    value = canonical_argument_result
                    if key and isinstance(value, dict):
                        value = value.get(key)
                else:
                    value = self._result(project["id"], producer, key, workflow_id=workflow_id)
                    repair_override = self._repair_override(state, producer, workflow_id=workflow_id)
                    if repair_override is not None:
                        value = repair_override
                    if (
                        producer == "P-REVISION-PLAN"
                        and value is not None
                    ):
                        value = self._canonicalize_revision_plan_roles(value)
                if value is not None:
                    if field == "research_plan" and is_background_workflow:
                        value = self._wf3b_prompt_research_plan(value)
                    if (
                        field == "synthesis_candidate"
                        and is_background_workflow
                        and prompt_id == WF3B_RESEARCH_CRITIC
                    ):
                        # The background critic reviews runtime-built evidence
                        # cards, not raw model claims.  Defer the replacement
                        # until the search results and claim-validation bindings
                        # are available below so the card bundle matches the
                        # persist path exactly.
                        background_critic_synthesis = value if isinstance(value, dict) else {}
                        continue
                    replacements.append((f"payload.{field}", value))

        argument_override = canonical_argument_result
        if isinstance(argument_override, dict):
            argument_graph = (
                argument_override.get("argument_architecture")
                or argument_override
            )
        else:
            argument_graph = (
                self._result(
                    project["id"],
                    "P-ARGUMENT-ARCHITECTURE",
                    "argument_architecture",
                )
                or argument_graph_seed
            )
        scheme = self._result(project["id"], "P-SCHEME-EXTRACT", "scheme_profile")
        template = self._result(project["id"], "P-TEMPLATE-EXTRACT", "template")
        plan = self._canonicalize_revision_plan_roles(
            self._result(
                project["id"],
                "P-REVISION-PLAN",
                "revision_plan",
            )
        )
        narrative_architecture = (plan or {}).get("narrative_architecture") if isinstance(plan, dict) else None
        section_contract = None
        if narrative_architecture and current_section:
            for contract in narrative_architecture.get("section_contracts", []):
                if contract.get("section_id") == current_section.get("section_id") or contract.get("title") == current_section.get("title"):
                    section_contract = contract
                    break
        blueprint = self._repair_override(state, "P-WRITE-BLUEPRINT", workflow_id=workflow_id)
        if blueprint is None:
            blueprint = self._result(project["id"], "P-WRITE-BLUEPRINT", "blueprint")
        candidate_workflow_id = workflow_id if prompt_id == "P-INTEGRATION-CRITIC" else None
        candidate_section_results = (state.get("section_results") or []) if prompt_id == "P-INTEGRATION-CRITIC" else None
        if prompt_id == "P-FINAL-CONFIDENTIALITY-REVIEW":
            candidate_workflow_id, candidate_section_results = self._bound_authoring_section_results(
                project["id"],
                state,
            )
        content_candidates = self._content_candidates(
            project["id"],
            candidate_workflow_id,
            section_results=candidate_section_results,
        )
        if prompt_id == "P-FINAL-CONFIDENTIALITY-REVIEW":
            self._assert_final_candidate_integrity(content_candidates)
        content = content_candidates[-1]["candidate"] if content_candidates else (self._result(project["id"], "P-EXPRESSION-POLISH") or self._result(project["id"], "P-WRITE-CONTENT"))
        safe_package = self._result(
            project["id"], "P-SAFE-ONLINE-PACKAGE", workflow_id=workflow_id
        )
        if isinstance(safe_package, dict):
            safe_package = self._wf3_effective_safe_package(
                safe_package,
                state=state,
                workflow_id=workflow_id,
            )
            if is_background_workflow:
                # The shared safety prompt emits the generic PUBLIC_RESEARCH
                # label. WF-3B projects that already-approved package into its
                # background-research subtype without changing any content.
                safe_package["task_type"] = "PUBLIC_BACKGROUND_RESEARCH"
        research_synthesis = self._result(
            project["id"],
            WF3B_SYNTHESIS_PROMPT if is_background_workflow else "P-PUBLIC-RESEARCH-SYNTHESIS",
            workflow_id=workflow_id,
        )

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
            scoped_subgraph = (
                self._scoped_project_subgraph(project_definition, section_contract)
                if section_contract
                else None
            )
            # Facts can be scoped even before the planning workflow has produced a
            # Section Contract.  This keeps accepted public evidence available to
            # ad-hoc previews while still bounding the context for weak models.
            for field in ("confirmed_facts", "fact_context", "existing_facts"):
                if field in payload:
                    replacements.append((f"payload.{field}", scoped_facts))
            scoped_items = [
                item
                for item in (scoped_subgraph or {}).get("items") or []
                if isinstance(item, dict) and item.get("item_id")
            ]

            def project_item_refs(item_types: set[str]) -> list[dict[str, Any]]:
                return [
                    {
                        "object_id": str(item["item_id"]),
                        "object_type": str(item.get("item_type") or "PROJECT_ITEM"),
                        "version": 1,
                        "object_hash": item.get("item_hash"),
                        "security_level": str(item.get("security_level") or project["security_level"]),
                        "display_name": str(item.get("content") or item["item_id"])[:200],
                    }
                    for item in scoped_items
                    if str(item.get("item_type") or "") in item_types
                ]

            if "technical_inputs" in payload:
                replacements.append((
                    "payload.technical_inputs",
                    project_item_refs({
                        "EXISTING_APPROACH",
                        "WORK_PACKAGE",
                        "METHOD",
                        "EXPERIMENT",
                        "CAPABILITY",
                    }),
                ))
            if "metric_inputs" in payload:
                replacements.append((
                    "payload.metric_inputs",
                    project_item_refs({"METRIC", "EXPERIMENT"}),
                ))
            if section_contract:
                scoped_architecture = self._scoped_architecture(narrative_architecture, section_contract)
                scoped_plan = self._scoped_plan(plan, scoped_architecture, section_contract)
                if "narrative_architecture" in payload and scoped_architecture:
                    replacements.append(("payload.narrative_architecture", scoped_architecture))
                if "confirmed_plan" in payload and scoped_plan:
                    replacements.append(("payload.confirmed_plan", scoped_plan))
                if "project_subgraph" in payload and scoped_subgraph:
                    replacements.append(("payload.project_subgraph", scoped_subgraph))

        if "project_subgraph" in payload and project_definition and prompt_id not in section_prompt_ids:
            replacements.append(("payload.project_subgraph", {"item_ids": [x["item_id"] for x in project_definition.get("items", [])], "relation_ids": [x["relation_id"] for x in project_definition.get("relations", [])], "items": project_definition.get("items", []), "relations": project_definition.get("relations", [])}))
        for field in ["confirmed_facts", "fact_context", "existing_facts"]:
            if field in payload and prompt_id not in section_prompt_ids:
                replacements.append((f"payload.{field}", facts))
        if "locked_facts" in payload:
            replacements.append(("payload.locked_facts", []))
        if "open_conflicts" in payload:
            replacements.append(("payload.open_conflicts", []))
        if "fact_package" in payload and facts:
            fact_package = {
                "schema_version": "2.0",
                "project_id": project["id"],
                "version": 1,
                "claims": facts,
                "conflicts": [],
                "security_level": project["security_level"],
            }
            fact_package["package_hash"] = sha256_json(fact_package)
            replacements.append(("payload.fact_package", fact_package))
        if "template_context" in payload and template:
            template_context = (
                self._planning_template_context(template)
                if prompt_id in {
                    "P-REVISION-PLAN",
                    "P-WRITE-BLUEPRINT",
                    "P-WRITE-CONTENT",
                }
                else template
            )
            replacements.append(("payload.template_context", template_context))
        if "writing_mode" in payload:
            writing_mode = str(
                ((state.get("options") or {}).get("writing_mode") or "SUBSTANTIVE_REVISION")
            )
            if writing_mode not in {
                "COPY_EDIT_ONLY",
                "SUBSTANTIVE_REVISION",
                "DRAFT_FROM_PROJECT_DEFINITION",
            }:
                writing_mode = "SUBSTANTIVE_REVISION"
            replacements.append(("payload.writing_mode", writing_mode))
        if "confirmed_plan" in payload and plan and prompt_id not in section_prompt_ids:
            replacements.append(("payload.confirmed_plan", plan))
        if "approved_blueprint" in payload and blueprint:
            replacements.append(("payload.approved_blueprint", blueprint))
        if "content_candidate" in payload:
            raw_content = self._repair_override(state, "P-WRITE-CONTENT", workflow_id=workflow_id)
            if raw_content is None:
                raw_content = self._result(project["id"], "P-WRITE-CONTENT")
            if raw_content:
                replacements.append(("payload.content_candidate", raw_content))
        if "polished_candidate" in payload:
            polished = self._repair_override(state, "P-EXPRESSION-POLISH", workflow_id=workflow_id)
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
            if "safe_online_package_content" in payload:
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
            if prompt_id == "P-ONLINE-RESULT-IMPORT-CRITIC":
                replacements.append(("payload.approved_safe_package_content", {
                    "task_description": safe_package.get("task_description", "公开资料检索"),
                    "queries": list(safe_package.get("queries") or []),
                    "allowed_context": list(safe_package.get("allowed_context") or []),
                    "prohibited_inferences": list(safe_package.get("prohibited_inferences") or []),
                    "prohibited_outputs": list(safe_package.get("prohibited_outputs") or []),
                }))
        if "result_package" in payload and research_synthesis:
            source_ids = sorted({
                str(ref.get("source_id"))
                for claim in research_synthesis.get("claims", [])
                if isinstance(claim, dict)
                for ref in claim.get("source_refs", [])
                if isinstance(ref, dict) and ref.get("source_id")
            })
            request_hash = sha256_json(safe_package or {"project_id": project["id"], "task": "PUBLIC_RESEARCH"})
            # The import review partitions canonical claim IDs, so the package
            # carries the shared WF-3 claim shape.  WF-3B-only background fields
            # (dimension/target_section_profiles/conflicts/limitations) stay on
            # the committed synthesis result and the persisted background
            # artifact; they are projection-only stripped here.
            package_claims = [
                {
                    key: value
                    for key, value in claim.items()
                    if key not in ("dimension", "target_section_profiles", "conflicts", "limitations")
                }
                if isinstance(claim, dict)
                else claim
                for claim in research_synthesis.get("claims", [])
            ]
            result_core = {
                "claims": package_claims,
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
                approval = self._wf3_outbound_approval(workflow_id)
                if approval is None:
                    # Replay/schema fixtures have no workflow Gate. Preserve their
                    # recorded manifest instead of inventing approval provenance.
                    approval = None
                else:
                    replacements.append(("payload.transfer_manifest", {
                        "package_id": package_id,
                        "request_hash": request_hash,
                        "content_hash": result_package["manifest_hash"],
                        "approved_by": approval["approved_by"],
                        "approved_at": approval["approved_at"],
                        "expires_at": safe_package.get("valid_until") if safe_package else None,
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
            producer_revision_findings = (state or {}).get("producer_revision_findings") or {}
            if prompt_id in producer_revision_findings:
                findings = list(producer_revision_findings.get(prompt_id) or [])
            elif prompt_id == "P-ARGUMENT-ARCHITECTURE":
                findings = list((state or {}).get("argument_revision_findings", []) or [])
            elif prompt_id == "P-REVISION-PLAN":
                findings = list((state or {}).get("planning_revision_findings", []) or [])
            else:
                active_section_id = str((current_section or {}).get("section_id") or "")
                section_findings = (state or {}).get("section_revision_findings") or {}
                if active_section_id in section_findings:
                    findings = list(section_findings.get(active_section_id) or [])
                else:
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
        if "terminology" in payload:
            terminology_by_canonical: dict[str, dict[str, Any]] = {}
            for item in content_candidates:
                for usage in (item.get("candidate") or {}).get("term_usage") or []:
                    if not isinstance(usage, dict):
                        continue
                    term = str(usage.get("term") or "").strip()
                    canonical = str(usage.get("canonical_term") or term).strip()
                    if not canonical:
                        continue
                    entry = terminology_by_canonical.setdefault(
                        canonical,
                        {
                            "canonical_term": canonical,
                            "aliases": [],
                            "definition": (
                                "Canonical usage collected from approved candidate "
                                "section terminology metadata."
                            ),
                        },
                    )
                    if term and term != canonical and term not in entry["aliases"]:
                        entry["aliases"].append(term)
            # An empty terminology table is valid and materially different from
            # an untouched schema scaffold. Always populate this required field
            # so LIVE context validation never treats it as unresolved.
            replacements.append(
                ("payload.terminology", list(terminology_by_canonical.values()))
            )
        if "prior_security_findings" in payload:
            # The final confidentiality review requires this collection even
            # when no earlier security review opened a finding.  In LIVE mode
            # an untouched empty schema array is deliberately treated as a
            # scaffold marker, so explicitly populate the real (empty) state.
            prior_security_findings: list[dict[str, Any]] = []
            rows = self.db.fetchall(
                """SELECT output_json FROM prompt_runs
                   WHERE project_id=?
                     AND prompt_id IN (
                       'P-SECURITY-CLASSIFY-CRITIC',
                       'P-SAFE-ONLINE-PACKAGE-CRITIC',
                       'P-ONLINE-RESULT-IMPORT-CRITIC'
                     )
                   ORDER BY created_at,id""",
                (project["id"],),
            )
            seen_security_findings: set[str] = set()
            for row in rows:
                output = json.loads(row.get("output_json") or "{}")
                for finding in output.get("findings") or []:
                    if not isinstance(finding, dict) or finding.get("category") != "SECURITY":
                        continue
                    identity = sha256_json(finding)
                    if identity in seen_security_findings:
                        continue
                    seen_security_findings.add(identity)
                    prior_security_findings.append(copy.deepcopy(finding))
            replacements.append(
                ("payload.prior_security_findings", prior_security_findings)
            )
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

        if "task_instruction" in payload:
            raw_instruction = (
                config.get("task_instruction")
                or project.get("description")
                or project.get("name")
            )
            if isinstance(raw_instruction, dict):
                objective = str(
                    raw_instruction.get("objective")
                    or project.get("description")
                    or project.get("name")
                    or ""
                ).strip()
            else:
                objective = str(
                    raw_instruction
                    or project.get("description")
                    or project.get("name")
                    or ""
                ).strip()
            if objective and isinstance(payload.get("task_instruction"), dict):
                section_ids = requested_target_ids or [
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
            elif objective:
                replacements.append(("payload.task_instruction", objective))
        if "recipient_scope" in payload:
            replacements.append(("payload.recipient_scope", config.get("recipient_scope", ["内部用户"])))
        if "intended_uses" in payload:
            workflow_options = state.get("options") if isinstance(state.get("options"), dict) else {}
            intended_uses = (
                workflow_options.get("intended_uses")
                or config.get("intended_uses")
                or ["项目材料安全分类与后续申请书工作流处理"]
            )
            if isinstance(intended_uses, str):
                intended_uses = [intended_uses]
            replacements.append((
                "payload.intended_uses",
                [str(item).strip() for item in intended_uses if str(item).strip()],
            ))
        if "allowed_topics" in payload and prompt_id != "P-SAFE-ONLINE-PACKAGE":
            wf3_options = state.get("options") if isinstance(state.get("options"), dict) else {}
            nested_wf3 = wf3_options.get("wf3") if isinstance(wf3_options.get("wf3"), dict) else {}
            wf3_resolution = state.get("wf3_input_resolution") if isinstance(state.get("wf3_input_resolution"), dict) else {}
            approved_topics = (
                wf3_options.get("allowed_public_topics")
                or nested_wf3.get("allowed_public_topics")
                or config.get("allowed_public_topics")
                or wf3_resolution.get("allowed_topics")
                or payload.get("allowed_topics")
                or []
            )
            replacements.append(("payload.allowed_topics", approved_topics))
        if "prohibited_fields" in payload:
            replacements.append(("payload.prohibited_fields", config.get("prohibited_external_fields", [])))

        search_results = (
            state.get("background_search_results")
            if is_background_workflow
            else state.get("public_search_results")
        )
        if search_results:
            synthesis_prompt_ids = (
                {WF3B_SYNTHESIS_PROMPT, WF3B_RESEARCH_CRITIC}
                if is_background_workflow
                else {"P-PUBLIC-RESEARCH-SYNTHESIS", "P-PUBLIC-RESEARCH-CRITIC"}
            )
            if prompt_id in synthesis_prompt_ids:
                sufficiency = search_results.get("research_sufficiency") or {
                    "schema_version": "1.0",
                    "status": "SUFFICIENT",
                    "coverage_status": str((search_results.get("coverage") or {}).get("status") or "PASS"),
                    "research_gaps": [],
                    "blocking_reasons": [],
                    "retrieval_health_status": str((search_results.get("retrieval_health") or {}).get("status") or "UNOBSERVED"),
                    "may_continue": True,
                }
                if not is_background_workflow:
                    replacements.append(("payload.research_sufficiency", sufficiency))
                if is_background_workflow and "retrieval_summary" in payload:
                    options = state.get("options") if isinstance(state.get("options"), dict) else {}
                    replacements.append((
                        "payload.retrieval_summary",
                        self._wf3b_retrieval_summary(
                            search_results,
                            list(options.get("required_dimensions") or BACKGROUND_DIMENSIONS),
                        ),
                    ))
            if "retrieved_sources" in payload:
                replacements.append(("payload.retrieved_sources", search_results.get("sources", [])))
            critic_prompt_id = WF3B_RESEARCH_CRITIC if is_background_workflow else "P-PUBLIC-RESEARCH-CRITIC"
            if "extracted_passages" in payload or prompt_id == critic_prompt_id:
                replacements.append((
                    "payload.extracted_passages",
                    self._wf3b_extracted_passages(search_results)
                    if is_background_workflow
                    else search_results.get("passages", []),
                ))
            if "public_sources" in payload:
                replacements.append(("payload.public_sources", search_results.get("sources", [])))
            if prompt_id == "P-ONLINE-RESULT-IMPORT-CRITIC":
                replacements.append(("payload.public_source_passages", search_results.get("passages", [])))

        if background_critic_synthesis is not None:
            wf3b_options = state.get("options") if isinstance(state.get("options"), dict) else {}
            claim_validation = (
                state.get("background_claim_validation")
                if isinstance(state.get("background_claim_validation"), dict)
                else None
            )
            card_bundle = build_background_cards(
                background_critic_synthesis,
                search_results if isinstance(search_results, dict) else {},
                claim_validation,
                required_dimensions=wf3b_options.get("required_dimensions") or (),
                topic_id=str(wf3b_options.get("topic_id") or ""),
            )
            replacements.append((
                "payload.synthesis_candidate",
                {
                    "background_cards": card_bundle["background_cards"],
                    "background_gaps": card_bundle["background_gaps"],
                    "coverage_summary": str(background_critic_synthesis.get("coverage_summary") or ""),
                },
            ))

        for path, value in replacements:
            self._set_path_if_valid(
                prompt_id,
                envelope,
                path,
                value,
                strict=path in CRITICAL_CONTEXT_PATHS or path in human_override_paths,
            )

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

    def _input_schema(self, prompt_id: str) -> dict[str, Any]:
        schema = self._input_schema_cache.get(prompt_id)
        if schema is None:
            schema = self.pack.inlined_schema(prompt_id, "input")
            self._input_schema_cache[prompt_id] = schema
        return schema
    @classmethod
    def _property_schema(cls, schema: Any, property_name: str) -> dict[str, Any] | None:
        """Return the value schema for one object property.

        Prompt schemas occasionally distribute property constraints across
        composition keywords.  ``allOf`` constraints must all hold, whereas
        ``anyOf``/``oneOf`` alternatives describe admissible branches.  The
        returned schema validates only the replacement value; the completed
        envelope is still validated once at the end of ``build`` so
        cross-field conditions remain authoritative.
        """

        if not isinstance(schema, dict):
            return None

        direct = None
        properties = schema.get("properties")
        if isinstance(properties, dict) and isinstance(properties.get(property_name), dict):
            direct = properties[property_name]

        all_of: list[dict[str, Any]] = []
        for branch in schema.get("allOf") or []:
            branch_schema = cls._property_schema(branch, property_name)
            if branch_schema is not None:
                all_of.append(branch_schema)

        alternatives: list[dict[str, Any]] = []
        for keyword in ("anyOf", "oneOf"):
            for branch in schema.get(keyword) or []:
                branch_schema = cls._property_schema(branch, property_name)
                if branch_schema is not None:
                    alternatives.append(branch_schema)

        constraints: list[dict[str, Any]] = []
        if direct is not None:
            constraints.append(direct)
        constraints.extend(all_of)
        if alternatives:
            constraints.append({"anyOf": alternatives})
        if not constraints:
            additional = schema.get("additionalProperties")
            return additional if isinstance(additional, dict) else None
        if len(constraints) == 1:
            return constraints[0]
        return {"allOf": constraints}
    def _schema_for_path(self, prompt_id: str, dotted_path: str) -> dict[str, Any] | None:
        schema: dict[str, Any] | None = self._input_schema(prompt_id)
        for part in dotted_path.split("."):
            schema = self._property_schema(schema, part)
            if schema is None:
                return None
        return schema
    @staticmethod
    def _value_schema_errors(schema: dict[str, Any], value: Any) -> list[str]:
        validator = Draft202012Validator(
            schema,
            format_checker=Draft202012Validator.FORMAT_CHECKER,
        )
        errors = sorted(validator.iter_errors(value), key=lambda error: list(error.absolute_path))
        result: list[str] = []
        for error in errors:
            path = "/" + "/".join(str(item) for item in error.absolute_path)
            result.append(f"{path or '/'}: {error.message}")
        return result
    def _set_path_if_valid(self, prompt_id: str, envelope: dict[str, Any], dotted_path: str, value: Any, *, strict: bool = False) -> bool:
        parts = dotted_path.split(".")
        node: dict[str, Any] = envelope
        for part in parts[:-1]:
            if part not in node or not isinstance(node[part], dict):
                if strict:
                    raise ValueError(f"Critical context path does not exist for {prompt_id}: {dotted_path}")
                return False
            node = node[part]

        value_schema = self._schema_for_path(prompt_id, dotted_path)
        if value_schema is None:
            if strict:
                raise ValueError(f"Critical context schema path does not exist for {prompt_id}: {dotted_path}")
            return False
        errors = self._value_schema_errors(value_schema, value)
        if errors:
            if strict:
                raise ValueError(f"Critical context replacement failed for {prompt_id} {dotted_path}: " + "; ".join(errors[:10]))
            return False

        node[parts[-1]] = copy.deepcopy(value)
        return True
