from __future__ import annotations

import copy
import inspect
import json
import re
from typing import Any

from .executor import PromptExecutionError
from .deterministic_repair import apply_deterministic_contract_repairs
from .model_semantic_contracts import (
    argument_authoritative_repair_paths,
    project_argument_authoritative_state,
    targeted_repair_structural_blockers,
)
from .util import new_id, sha256_json, utc_now
from .repair_ledger import RepairLedger
from .runtime_failures import FailureCategory, classify_runtime_failure
from .secret_redaction import redact_secret_text
from .retry_policy import ProviderRetriesExhausted
from .json_pointer import (
    JsonPointerError,
    is_ancestor_or_same,
    join_pointer,
    parse_pointer,
)
from .output_integrity import attach_trusted_source_catalog
from .workflow_defs import CRITIC_PRODUCER
from .background_research import (
    BACKGROUND_DIMENSIONS,
    WF3B_PLAN_PROMPT,
    normalize_background_plan,
)
from .contracts.semantic_contract import get_semantic_contract
from .wf3_contracts import compare_public_search_candidates, summarize_public_search
from .research import PublicResearchPlanError
from .skills.research_execution import (
    ResearchExecutionContractError,
    build_plan_lock,
    validate_plan_transition,
)






def _authoritative_argument_producer_prompt() -> str:
    return str(
        get_semantic_contract()
        .rule("SC-ARGUMENT-LIFECYCLE-COMPOSITION")
        .config.get("producer_prompt")
        or ""
    )


PRODUCER_RESULT_KEY = {
    "P-SCHEME-EXTRACT": "scheme_profile",
    "P-PROJECT-DEFINITION-EXTRACT": "project_definition",
    "P-FACT-EXTRACT": "fact_candidates",
    "P-TEMPLATE-EXTRACT": "template",
    # The critic evaluates both the graph and the research-design matrix, so a
    # targeted repair must receive the full producer result rather than only
    # the nested argument_architecture graph.
    "P-ARGUMENT-ARCHITECTURE": None,
    "P-REVISION-PLAN": "revision_plan",
    "P-WRITE-BLUEPRINT": "blueprint",
    "P-WRITE-CONTENT": None,
    "P-EXPRESSION-POLISH": None,
}


def producer_consumer_value(prompt_id: str, protocol_output: dict[str, Any]) -> Any:
    """Return the exact value downstream consumers read from one Producer output.

    ``protocol_output`` is the complete Prompt output envelope.  ``repaired_value``
    artifacts must persist this consumer value, never the outer envelope.
    """
    if not isinstance(protocol_output, dict):
        return None
    result = protocol_output.get("result")
    key = PRODUCER_RESULT_KEY.get(prompt_id)
    if key is None:
        return copy.deepcopy(result) if result is not None else None
    if not isinstance(result, dict):
        return None
    return copy.deepcopy(result.get(key))


def repair_override_key(producer_prompt: str, state: dict[str, Any]) -> str:
    section_id = str(state.get("active_section_id") or "").strip()
    return f"section:{section_id}:{producer_prompt}" if section_id else producer_prompt

PRODUCER_ROLE = {
    "P-SECURITY-CLASSIFY": "SECURITY_REVIEW_AGENT",
    "P-SAFE-ONLINE-PACKAGE": "SECURITY_REVIEW_AGENT",
    "P-SCHEME-EXTRACT": "PROJECT_KNOWLEDGE_AGENT",
    "P-PROJECT-DEFINITION-EXTRACT": "PROJECT_KNOWLEDGE_AGENT",
    "P-FACT-EXTRACT": "PROJECT_KNOWLEDGE_AGENT",
    "P-TEMPLATE-EXTRACT": "TEMPLATE_AGENT",
    "P-ARGUMENT-ARCHITECTURE": "ARGUMENT_ARCHITECTURE_AGENT",
    "P-REVISION-PLAN": "PLANNING_AGENT",
    "P-WRITE-BLUEPRINT": "WRITING_AGENT",
    "P-WRITE-CONTENT": "WRITING_AGENT",
    "P-EXPRESSION-POLISH": "EXPRESSION_EDITOR_AGENT",
    "P-PUBLIC-RESEARCH-SYNTHESIS": "PROJECT_KNOWLEDGE_AGENT",
}


class WorkflowRepairMixin:
    _CONTRACT_REPAIR_EXCLUDED_FAILURE_KINDS = frozenset({
        "TRANSPORT",
        "TIMEOUT",
        "EMPTY_STREAM",
        "STREAM_EVENT",
        "RESPONSE_PARSE",
        "OUTPUT_TRUNCATED",
    })
    _STATUS_CLOSURE_PATHS = (
        "/status",
        "/findings",
        "/user_questions",
        "/unresolved_items",
    )

    @staticmethod
    def _contract_error_pointer(error: str) -> str | None:
        """Return only an RFC 6901 pointer already present at error start."""

        match = re.match(r"^(/[^\s:]*)(?::|\s|$)", str(error or ""))
        if not match:
            return None
        pointer = match.group(1)
        if pointer == "/":
            return None
        try:
            parse_pointer(pointer)
        except JsonPointerError:
            return None
        return pointer

    @classmethod
    def _contract_repair_findings(
        cls,
        prompt_id: str,
        candidate: dict[str, Any],
        validation_errors: list[str],
    ) -> tuple[list[dict[str, Any]], list[str]] | None:
        """Map located validator errors to the existing repair protocol."""

        candidate_hash = sha256_json(candidate)
        findings: list[dict[str, Any]] = []
        allowed_paths: list[str] = []
        for error in validation_errors:
            description = str(error or "")
            pointer = cls._contract_error_pointer(description)
            if not description or pointer is None:
                return None
            digest = sha256_json(
                {
                    "prompt_id": prompt_id,
                    "candidate_hash": candidate_hash,
                    "validator_error": description,
                }
            )
            findings.append(
                {
                    "finding_instance_id": f"finding-{digest[:32]}",
                    "code": "OUTPUT_CONTRACT_VIOLATION",
                    "severity": "P1",
                    "category": "SYSTEM",
                    "target_type": "PRODUCER_OUTPUT",
                    "target_path_or_span": pointer,
                    "description": description,
                    "evidence_refs": [],
                    "repairable": True,
                    "repair_instruction": (
                        "修复该 Validator error，并保持其它内容不变。"
                    ),
                    "suggested_route": "ORIGINAL_PRODUCER",
                    "blocking": True,
                }
            )
            allowed_paths.append(pointer)

        def requires_status_closure(path: str) -> bool:
            parts = parse_pointer(path)
            if not parts:
                return False
            root = parts[0]
            # Status/question/unresolved errors are inherently cross-field.  A
            # Finding-level error may also describe a missing gate counterpart,
            # but a scalar nested Finding error (for example /repairable) must
            # stay narrowly scoped instead of authorizing the whole gate state.
            if root in {"status", "user_questions", "unresolved_items"}:
                return True
            return root == "findings" and len(parts) <= 2

        if any(requires_status_closure(path) for path in allowed_paths):
            allowed_paths.extend(cls._STATUS_CLOSURE_PATHS)
        return findings, list(dict.fromkeys(allowed_paths))

    @staticmethod
    def _contract_repair_protection(
        candidate: dict[str, Any],
        allowed_paths: list[str],
    ) -> tuple[list[str], list[dict[str, str]]]:
        """Protect every top-level subtree outside the authorized roots."""

        allowed_roots = {
            parse_pointer(path)[0]
            for path in allowed_paths
            if parse_pointer(path)
        }
        protected_paths = [
            join_pointer("content", key)
            for key in candidate
            if str(key) not in allowed_roots
        ]
        protected_hashes = [
            {
                "path": path,
                "hash": sha256_json(candidate[parse_pointer(path)[1]]),
            }
            for path in protected_paths
        ]
        return protected_paths, protected_hashes

    def _validate_repaired_producer_output(
        self,
        *,
        prompt_id: str,
        candidate: dict[str, Any],
        producer_input: dict[str, Any],
        quality_input: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Run the repaired object through the original deterministic chain."""

        immutable_candidate = copy.deepcopy(candidate)
        consumed = self.executor._normalize_output(
            prompt_id,
            copy.deepcopy(immutable_candidate),
            producer_input,
        )
        policy = getattr(self.executor, "policy", None)
        assert_unchanged = getattr(policy, "assert_output_unchanged", None)
        if callable(assert_unchanged):
            assert_unchanged(
                immutable_candidate,
                consumed,
                stage="output_normalization",
            )
        guard_report = self.executor._observe_guard(
            prompt_id,
            quality_input,
            consumed,
        )
        output_errors = self.pack.validate(prompt_id, "output", consumed)
        if output_errors:
            raise PromptExecutionError(
                "Repaired producer output failed strict schema validation",
                validation_errors=output_errors,
            )
        self.executor._validate_output_semantics(
            prompt_id,
            producer_input,
            consumed,
        )
        return consumed, guard_report


    async def _repair_producer_contract_failure(
        self,
        wf: dict[str, Any],
        state: dict[str, Any],
        *,
        prompt_id: str,
        envelope: dict[str, Any],
        exc: BaseException,
        classification: Any,
    ) -> dict[str, Any]:
        """Repair a located producer contract failure without broadening authority.

        Deterministic representation/policy rules are applied first on a copy.
        Only unresolved local semantic errors are sent to P-TARGETED-REPAIR.
        Contract-shape failures from the repair model receive feedback-aware fresh
        generations; transient provider failures remain provider retry concerns.
        """
        result: dict[str, Any] = {"attempted": False, "result": None}
        if (
            prompt_id not in PRODUCER_RESULT_KEY
            or prompt_id == "P-TARGETED-REPAIR"
            or classification.category is not FailureCategory.OUTPUT_CONTRACT
            or str(classification.failure_kind or "") in self._CONTRACT_REPAIR_EXCLUDED_FAILURE_KINDS
        ):
            return result
        validation_errors=[str(x) for x in (getattr(exc,"validation_errors",None) or []) if str(x)]
        run_id=str(getattr(exc,"run_id","") or "").strip()
        if not validation_errors or not run_id: return result
        row=self.db.fetchone(
            "SELECT id,input_json,output_json FROM prompt_runs WHERE id=? AND project_id=? AND workflow_id=? AND prompt_id=? AND status='ERROR' AND output_json IS NOT NULL",
            (run_id,wf["project_id"],wf["id"],prompt_id),
        )
        if not row: return result
        try:
            source_candidate=json.loads(row.get("output_json") or "null")
            producer_input=json.loads(row.get("input_json") or "null")
        except (TypeError,json.JSONDecodeError): return result
        if not isinstance(source_candidate,dict) or not isinstance(producer_input,dict): return result
        mapped=self._contract_repair_findings(prompt_id,source_candidate,validation_errors)
        if mapped is None: return result
        result["attempted"]=True
        findings,validator_paths=mapped
        source_candidate_hash=sha256_json(source_candidate)
        if not str(state.get("original_environment") or "").strip():
            env=str(self.pack.entry(prompt_id).get("required_environment") or "").strip()
            if env in {"OFFLINE_LOCAL","ONLINE_PUBLIC"}: state["original_environment"]=env

        def producer_value(validated):
            return producer_consumer_value(prompt_id, validated)
        def local_route(mode):
            return {"environment":str(state.get("original_environment") or "OFFLINE_LOCAL"),"model_id":mode,"endpoint_id":"local-deterministic"}
        def finalize(*,validated,guard_report,repair_run_id,route,call_key,active_findings,canonical_allowed_paths,repair_mode,deterministic_rule_ids=None):
            repaired_value=producer_value(validated)
            if repaired_value is None: return result
            artifact_id=self._persist_repair_application(
                wf=wf,state=state,producer_prompt=prompt_id,critic_prompt="OUTPUT_CONTRACT_VALIDATOR",repair_run_id=repair_run_id,
                original_object_hash=source_candidate_hash,repaired_value=repaired_value,findings=active_findings,allowed_paths=canonical_allowed_paths,
                collection_key=None,repaired_canonical_output=validated,
            )
            active_section_id=str(state.get("active_section_id") or "").strip()
            if active_section_id:
                target_key=self._repair_override_key(prompt_id,state)
                state.setdefault("producer_contract_repair_markers",{})[target_key]={"section_id":active_section_id,"repair_run_id":repair_run_id,"repair_application_artifact_id":artifact_id}
            result["result"]={"run_id":repair_run_id,"prompt_id":prompt_id,"status":validated.get("status","ERROR"),"route":route,"output":validated,
                              "guard_report":guard_report,"quality_guard_enabled":getattr(self.executor,"quality_guard_enabled",False),
                              "guard_observation_status":guard_report.get("observation_status"),"call_key":call_key,
                              "contract_repair":{"source_run_id":run_id,"repair_run_id":repair_run_id,"repair_application_artifact_id":artifact_id,"repair_mode":repair_mode,
                                                 "allowed_paths":canonical_allowed_paths,"original_hash":source_candidate_hash,"repaired_hash":sha256_json(validated),
                                                 "deterministic_rule_ids":list(deterministic_rule_ids or [])}}
            return result

        deterministic=apply_deterministic_contract_repairs(source_candidate,validation_errors,validator_paths)
        candidate=deterministic.candidate; deterministic_paths=list(deterministic.changed_paths); deterministic_rules=list(deterministic.rule_ids)
        if deterministic_paths:
            try:
                validated,guard_report=self._validate_repaired_producer_output(prompt_id=prompt_id,candidate=candidate,producer_input=producer_input,quality_input=envelope)
            except PromptExecutionError as deterministic_error:
                validation_errors=[str(x) for x in (getattr(deterministic_error,"validation_errors",None) or []) if str(x)]
                if not validation_errors: return result
                remapped=self._contract_repair_findings(prompt_id,candidate,validation_errors)
                if remapped is None: return result
                findings,validator_paths=remapped
            else:
                if sha256_json(validated)==source_candidate_hash: return result
                canonical_allowed=[join_pointer("content",*parse_pointer(path)) for path in deterministic_paths]
                return finalize(validated=validated,guard_report=guard_report,repair_run_id=f"deterministic-{run_id}",route=local_route("deterministic-contract-repair"),call_key=None,
                                active_findings=findings,canonical_allowed_paths=canonical_allowed,repair_mode="DETERMINISTIC",deterministic_rule_ids=deterministic_rules)

        # Argument Architecture is authoritative-state based.  Once the semantic
        # model response has been expanded into a full Producer protocol output,
        # that envelope and every derived projection are Runtime-owned.  If the
        # canonical full-output contract still fails after deterministic
        # representation repair, do not hand the protocol envelope to the LLM
        # Targeted Repair agent.  A fresh Producer attempt (owned by the common
        # bounded retry path) or an explicit runtime-contract failure is the only
        # safe recovery; otherwise Targeted Repair would become a second writer
        # for machine-owned fields.
        if prompt_id == _authoritative_argument_producer_prompt():
            state.setdefault("contract_repair_escalations", []).append(
                {
                    "prompt_id": prompt_id,
                    "source_run_id": run_id,
                    "reason": "AUTHORITATIVE_RUNTIME_CONTRACT_REGENERATION_REQUIRED",
                    "details": list(validation_errors)[:20],
                    "created_at": utc_now(),
                }
            )
            del state["contract_repair_escalations"][:-50]
            self._update(wf, state=state)
            return result

        canonical_allowed_paths=[join_pointer("content",*parse_pointer(path)) for path in validator_paths]
        protected_paths,protected_hashes=self._contract_repair_protection(candidate,validator_paths)
        candidate_hash=sha256_json(candidate)
        original_object={"object_type":prompt_id.removeprefix("P-").replace("-","_"),"object_id":f"contract-{run_id}"[:128],"object_hash":candidate_hash,"content":copy.deepcopy(candidate)}
        original_ref={"object_id":original_object["object_id"],"object_type":original_object["object_type"],"version":1,"object_hash":candidate_hash,"security_level":self._project_level(wf["project_id"]),"display_name":f"{prompt_id}原始失败输出"}
        base_overrides={"payload.original_object":original_object,"payload.original_producer":PRODUCER_ROLE[prompt_id],"payload.findings_to_repair":findings,
                        "payload.allowed_paths":canonical_allowed_paths,"payload.protected_paths":protected_paths,"payload.protected_hashes":protected_hashes,
                        "payload.original_input_refs":[original_ref],"payload.inherited_source_catalog":self._inherited_producer_source_catalog(wf,state,prompt_id)}
        structural_blockers=targeted_repair_structural_blockers({"payload":{"original_object":original_object,"original_producer":PRODUCER_ROLE[prompt_id],"findings_to_repair":findings,
                                                                            "allowed_paths":canonical_allowed_paths,"protected_paths":protected_paths,"protected_hashes":protected_hashes,"human_resolutions":[]}})
        if structural_blockers:
            state.setdefault("contract_repair_escalations",[]).append({"prompt_id":prompt_id,"source_run_id":run_id,"reason":"STRUCTURAL_SEMANTIC_REGENERATION_REQUIRED","details":structural_blockers,"created_at":utc_now()})
            del state["contract_repair_escalations"][:-50]; self._update(wf,state=state); return result

        options=state.get("options") or {}
        try: contract_retry_limit=int(options.get("targeted_repair_contract_retry_limit",options.get("provider_retry_limit",2)))
        except (TypeError,ValueError): contract_retry_limit=2
        contract_retry_limit=max(0,min(contract_retry_limit,5)); feedback=[]
        for semantic_attempt in range(1,contract_retry_limit+2):
            attempt_overrides=dict(base_overrides)
            if feedback: attempt_overrides["payload.contract_feedback"]={"attempt":semantic_attempt,"validation_errors":feedback[:20]}
            try:
                repair_envelope=self.context_builder.build("P-TARGETED-REPAIR",wf["project_id"],workflow_id=wf["id"],workflow_state=state,overrides=attempt_overrides)
                repair_call_key="call-contract-repair-"+sha256_json({"workflow_id":wf["id"],"prompt_id":prompt_id,"source_run_id":run_id,"candidate_hash":candidate_hash,"semantic_attempt":semantic_attempt,"feedback":feedback})[:24]
                repaired=await self._execute_prompt_with_provider_retry(wf,state,prompt_id="P-TARGETED-REPAIR",envelope=repair_envelope,call_key=repair_call_key,retry_categories=frozenset({FailureCategory.PROVIDER_TRANSIENT}))
            except ProviderRetriesExhausted:
                return result
            except (PromptExecutionError,ValueError,KeyError,TypeError) as repair_error:
                rc=classify_runtime_failure(repair_error)
                if rc.category is FailureCategory.OUTPUT_CONTRACT and semantic_attempt<=contract_retry_limit:
                    feedback=[str(x) for x in (getattr(repair_error,"validation_errors",None) or []) if str(x)] or [redact_secret_text(str(repair_error))]
                    continue
                return result
            repair_output=repaired.get("output"); repair_payload=repair_output.get("result") if isinstance(repair_output,dict) else None; repaired_candidate=repair_payload.get("repaired_object") if isinstance(repair_payload,dict) else None
            if isinstance(repaired_candidate,dict) and isinstance(repaired_candidate.get("content"),dict): repaired_candidate=repaired_candidate["content"]
            if repaired.get("status")!="PASS": return result
            if not isinstance(repaired_candidate,dict):
                if semantic_attempt<=contract_retry_limit: feedback=["Targeted Repair did not produce a repaired object"]; continue
                return result
            try:
                validated,guard_report=self._validate_repaired_producer_output(prompt_id=prompt_id,candidate=repaired_candidate,producer_input=producer_input,quality_input=envelope)
            except PromptExecutionError as producer_validation_error:
                if semantic_attempt<=contract_retry_limit:
                    feedback=[str(x) for x in (getattr(producer_validation_error,"validation_errors",None) or []) if str(x)] or [redact_secret_text(str(producer_validation_error))]; continue
                return result
            if sha256_json(validated)==source_candidate_hash: return result
            combined_allowed=list(dict.fromkeys([*[join_pointer("content",*parse_pointer(path)) for path in deterministic_paths],*canonical_allowed_paths]))
            return finalize(validated=validated,guard_report=guard_report,repair_run_id=str(repaired.get("run_id") or ""),route=copy.deepcopy(repaired.get("route") or {}),call_key=repaired.get("call_key"),
                            active_findings=findings,canonical_allowed_paths=combined_allowed,repair_mode="SEMANTIC_OPERATIONS",deterministic_rule_ids=deterministic_rules)
        return result


    def _replay_producer_contract_repair(
        self,
        wf: dict[str, Any],
        *,
        prompt_id: str,
        envelope: dict[str, Any],
        source_run_id: str,
        repair_run_id: str,
        repair_application_artifact_id: str | None = None,
    ) -> dict[str, Any]:
        """Revalidate a committed contract repair without regenerating it."""
        source=self.db.fetchone("SELECT input_json FROM prompt_runs WHERE id=? AND project_id=? AND workflow_id=? AND prompt_id=? AND output_json IS NOT NULL",(source_run_id,wf["project_id"],wf["id"],prompt_id))
        if not source: raise PromptExecutionError("Persisted producer contract repair source is unavailable")
        try: producer_input=json.loads(source.get("input_json") or "null")
        except (TypeError,json.JSONDecodeError) as exc: raise PromptExecutionError("Persisted producer contract repair source is invalid") from exc
        repaired_candidate=None; model_id=None; endpoint_id=None
        if repair_application_artifact_id:
            artifact=self.db.fetchone("SELECT content_json FROM artifacts WHERE id=? AND project_id=? AND workflow_id=? AND artifact_type='REPAIR_APPLICATION'",(repair_application_artifact_id,wf["project_id"],wf["id"]))
            if artifact:
                try:
                    ap=json.loads(artifact.get("content_json") or "null"); canonical=ap.get("repaired_canonical_output")
                    if isinstance(canonical,dict): repaired_candidate=canonical; model_id="deterministic-contract-repair" if str(repair_run_id).startswith("deterministic-") else "persisted-targeted-repair"; endpoint_id="repair-application-artifact"
                except (TypeError,json.JSONDecodeError): repaired_candidate=None
        if repaired_candidate is None:
            repair=self.db.fetchone("SELECT output_json,model_id,endpoint_id FROM prompt_runs WHERE id=? AND project_id=? AND workflow_id=? AND prompt_id='P-TARGETED-REPAIR' AND output_json IS NOT NULL",(repair_run_id,wf["project_id"],wf["id"]))
            if not repair: raise PromptExecutionError("Persisted producer contract repair is unavailable")
            try:
                ro=json.loads(repair.get("output_json") or "null"); repaired_candidate=ro["result"]["repaired_object"]
                if isinstance(repaired_candidate,dict) and isinstance(repaired_candidate.get("content"),dict): repaired_candidate=repaired_candidate["content"]
            except (TypeError,KeyError,json.JSONDecodeError) as exc: raise PromptExecutionError("Persisted producer contract repair is invalid") from exc
            model_id=repair.get("model_id"); endpoint_id=repair.get("endpoint_id")
        if not isinstance(repaired_candidate,dict): raise PromptExecutionError("Persisted producer contract repair has no canonical output")
        validated,guard_report=self._validate_repaired_producer_output(prompt_id=prompt_id,candidate=repaired_candidate,producer_input=producer_input,quality_input=envelope)
        return {"run_id":repair_run_id,"prompt_id":prompt_id,"status":validated.get("status","ERROR"),"route":{"environment":wf.get("state",{}).get("original_environment","OFFLINE_LOCAL"),"model_id":model_id,"endpoint_id":endpoint_id},"output":validated,"guard_report":guard_report,
                "quality_guard_enabled":getattr(self.executor,"quality_guard_enabled",False),"guard_observation_status":guard_report.get("observation_status"),"call_key":None,"reused_committed_result":True,
                "contract_repair":{"source_run_id":source_run_id,"repair_run_id":repair_run_id,"repair_application_artifact_id":repair_application_artifact_id}}

    def _inherited_producer_source_catalog(
        self,
        wf: dict[str, Any],
        state: dict[str, Any],
        producer_prompt: str,
    ) -> list[dict[str, Any]]:
        """Return the semantic source namespace seen by the original producer.

        A repair is a constrained continuation of the producer call.  It must
        therefore inherit the producer's read-only entity namespace; otherwise
        a critic can legitimately request an existing upstream entity while the
        repair validator incorrectly treats that identifier as dangling.

        The inherited value contains catalog metadata only, never copied source
        prose.  Synthetic envelope/container identities are omitted because
        they are call-local implementation details rather than business
        entities the repaired object may reference.
        """

        db = getattr(self, "db", None)
        if not callable(getattr(db, "fetchone", None)) or not callable(
            getattr(db, "fetchall", None)
        ):
            return []

        candidate_run_ids: list[str] = []
        active_section_id = str(state.get("active_section_id") or "").strip()
        if active_section_id:
            progress = (
                (state.get("section_progress") or {}).get(active_section_id) or {}
            )
            candidate_run_ids.extend(
                str(item.get("run_id") or "").strip()
                for item in reversed(progress.get("runs") or [])
                if isinstance(item, dict)
                and item.get("prompt_id") == producer_prompt
                and str(item.get("run_id") or "").strip()
            )

        rows: list[dict[str, Any]] = []
        seen_run_ids: set[str] = set()
        for run_id in candidate_run_ids:
            if run_id in seen_run_ids:
                continue
            seen_run_ids.add(run_id)
            row = db.fetchone(
                "SELECT id,input_json FROM prompt_runs "
                "WHERE id=? AND workflow_id=? AND prompt_id=? AND output_json IS NOT NULL",
                (run_id, wf["id"], producer_prompt),
            )
            if row:
                rows.append(row)
        rows.extend(
            row
            for row in db.fetchall(
                "SELECT id,input_json FROM prompt_runs "
                "WHERE workflow_id=? AND prompt_id=? AND output_json IS NOT NULL "
                "ORDER BY created_at DESC,id DESC",
                (wf["id"], producer_prompt),
            )
            if str(row.get("id") or "") not in seen_run_ids
        )

        for row in rows:
            try:
                producer_input = json.loads(row.get("input_json") or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            catalog = attach_trusted_source_catalog(producer_input).get(
                "trusted_source_catalog"
            ) or []
            inherited: list[dict[str, Any]] = []
            seen_source_ids: set[str] = set()
            for entry in catalog:
                if not isinstance(entry, dict):
                    continue
                source_id = str(entry.get("source_id") or "").strip()
                if (
                    not source_id
                    or source_id.startswith("input-")
                    or source_id in seen_source_ids
                ):
                    continue
                seen_source_ids.add(source_id)
                inherited.append(copy.deepcopy(entry))
            if inherited:
                return inherited
        return []

    @staticmethod
    def _identified_repair_findings(
        critic_prompt: str,
        findings: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Give every repairable finding a stable per-instance identity.

        Finding codes describe a class of defect and are not unique.  The
        critic may legitimately emit the same code for different target paths,
        so repair closure must be tracked by a deterministic instance id.
        """

        identified: list[dict[str, Any]] = []
        for ordinal, finding in enumerate(findings):
            item = copy.deepcopy(finding)
            defect_key = str(item.get("defect_key") or "").strip()
            if defect_key:
                # Semantic-contract findings already own a stable machine identity.
                # Repair tracking must preserve that identity across wording changes
                # instead of rebuilding it from descriptions or target prose.
                digest = sha256_json(
                    {
                        "critic_prompt": critic_prompt,
                        "defect_key": defect_key,
                    }
                )
            else:
                # Legacy/non-semantic critics do not yet expose defect_key. Preserve
                # their established per-instance identity contract unchanged.
                digest = sha256_json(
                    {
                        "critic_prompt": critic_prompt,
                        "ordinal": ordinal,
                        "code": item.get("code"),
                        "target_type": item.get("target_type"),
                        "target_path_or_span": item.get("target_path_or_span"),
                        "description": item.get("description"),
                    }
                )
            item["finding_instance_id"] = f"finding-{digest[:32]}"
            identified.append(item)
        return identified

    @classmethod
    def _record_nonexecutable_repair_failure(
        cls,
        state: dict[str, Any],
        *,
        critic_prompt: str,
        reason_code: str,
        error: str,
        finding_instance_ids: list[str] | None = None,
    ) -> None:
        attempt_key = cls._repair_state_key(critic_prompt, state)
        failure = {
            "critic_prompt": critic_prompt,
            "category": "SEMANTIC_REPAIR_REJECTED",
            "reason_code": reason_code,
            "error": error,
            "repair_attempt_key": attempt_key,
            "finding_instance_ids": list(finding_instance_ids or []),
            "technical_retries_used": 0,
            "provider_retries_used": 0,
            "consumes_semantic_repair_budget": False,
            "recorded_at": utc_now(),
        }
        state["last_targeted_repair_failure"] = failure
        RepairLedger.repair_not_executable(
            state,
            attempt_key,
            details=failure,
        )

    @staticmethod
    def _targeted_repair_failure_message(
        state: dict[str, Any],
        *,
        prompt_id: str,
        fallback: str,
    ) -> str:
        failure = state.get("last_targeted_repair_failure")
        if not isinstance(failure, dict):
            return fallback
        category = str(failure.get("category") or "TARGETED_REPAIR_FAILURE")
        error = str(failure.get("error") or fallback)
        validation_errors = [
            str(item)
            for item in failure.get("validation_errors") or []
            if str(item).strip()
        ]
        if validation_errors:
            error += " | " + "; ".join(validation_errors[:5])
        retries = int(failure.get("technical_retries_used") or 0)
        provider_retries = int(failure.get("provider_retries_used") or 0)
        run_id = str(failure.get("run_id") or "").strip()
        suffix = f"; run_id={run_id}" if run_id else ""
        return (
            f"{prompt_id} targeted repair failed [{category}] after "
            f"{retries} technical contract retries and {provider_retries} provider retries: "
            f"{error}{suffix}. "
            "Semantic repair budget was not consumed."
        )

    def _context_result(
        self,
        project_id: str,
        prompt_id: str,
        key: str | None = None,
        *,
        workflow_id: str | None = None,
        exact_workflow: bool = False,
    ) -> Any:
        """Read a context result with workflow scoping when the builder supports it.

        Lightweight test/dry-run builders may implement the legacy three-argument
        interface. Signature inspection preserves that compatibility without
        swallowing TypeError raised inside a real builder implementation.
        """
        reader = self.context_builder._result
        parameters = inspect.signature(reader).parameters
        if "workflow_id" in parameters:
            return reader(
                project_id,
                prompt_id,
                key,
                workflow_id=workflow_id,
                exact_workflow=exact_workflow,
            )
        return reader(project_id, prompt_id, key)

    async def _run_public_search(self, wf: dict[str, Any], state: dict[str, Any]) -> None:
        mode = self.executor.gateway.settings.runtime_mode
        if mode in {"REPLAY", "MOCK"}:
            candidate = {
                "sources": [],
                "passages": [],
                "queries": [],
                "mode": mode,
            }
        else:
            plan = self._context_result(
                wf["project_id"],
                "P-PUBLIC-RESEARCH-PLAN",
                workflow_id=wf["id"],
                exact_workflow=True,
            ) or {}
            candidate_lock = build_plan_lock(plan)
            approved_lock = state.get("public_research_plan_lock")
            if isinstance(approved_lock, dict) and approved_lock:
                try:
                    next_lock = validate_plan_transition(
                        approved_lock,
                        plan,
                        allow_additive=True,
                    )
                except ResearchExecutionContractError as exc:
                    raise PublicResearchPlanError(
                        str(exc),
                        details={
                            "code": exc.code,
                            **exc.details,
                            "approved_plan_hash": approved_lock.get("plan_hash"),
                            "candidate_plan_hash": candidate_lock.get("plan_hash"),
                        },
                    ) from exc
                if next_lock.get("plan_hash") != approved_lock.get("plan_hash"):
                    state.setdefault("public_research_plan_lock_history", []).append(
                        {
                            "recorded_at": utc_now(),
                            "from_plan_hash": approved_lock.get("plan_hash"),
                            "to_plan_hash": next_lock.get("plan_hash"),
                            "transition": "ADDITIVE_PLAN_DELTA",
                        }
                    )
                    del state["public_research_plan_lock_history"][:-20]
                state["public_research_plan_lock"] = next_lock
            else:
                state["public_research_plan_lock"] = candidate_lock
            provider = self.executor.gateway.settings.public_search_provider
            if mode == "SIMULATED" and provider == "disabled":
                candidate = self.research_service.simulated_search(plan)
            else:
                candidate = await self.research_service.search(
                    plan,
                    project_id=wf["project_id"],
                    workflow_id=wf["id"],
                    security_level="PUBLIC",
                )

        accepted = state.get("public_search_results")
        if isinstance(accepted, dict) and accepted:
            comparison = compare_public_search_candidates(accepted, candidate)
            state.setdefault("public_search_candidate_history", []).append(
                {
                    "recorded_at": utc_now(),
                    "decision": "ACCEPT" if comparison["accepted"] else "REJECT",
                    **comparison,
                }
            )
            del state["public_search_candidate_history"][:-20]
            if not comparison["accepted"]:
                # The full rejected result remains in the immutable Search skill
                # artifact/archive.  Workflow state retains only its exact hash
                # and comparison so a large failed candidate cannot replace or
                # duplicate the accepted context.
                return
        state["public_search_results"] = candidate
        state["public_search_accepted_baseline"] = summarize_public_search(candidate)
        if isinstance(candidate.get("research_sufficiency"), dict):
            state["research_sufficiency"] = copy.deepcopy(candidate["research_sufficiency"])
            state["research_gaps"] = copy.deepcopy(candidate.get("research_gaps") or candidate["research_sufficiency"].get("research_gaps") or [])
        if isinstance(candidate.get("retrieval_health"), dict):
            state["retrieval_health"] = copy.deepcopy(candidate["retrieval_health"])

    async def _run_background_search(self, wf: dict[str, Any], state: dict[str, Any]) -> None:
        """PUBLIC_SEARCH step for WF-3B, mirrored on distinct state keys.

        Same plan-lock, candidate non-regression and failure semantics as
        ``_run_public_search``, but the plan is frozen against the WF-3B
        background dimensions and execution goes through
        ``BackgroundResearchService`` (``application_background`` profile,
        forced ``require_web_discovery``).
        """
        mode = self.executor.gateway.settings.runtime_mode
        if mode in {"REPLAY", "MOCK"}:
            candidate = {
                "sources": [],
                "passages": [],
                "queries": [],
                "mode": mode,
            }
        else:
            plan = self._context_result(
                wf["project_id"],
                WF3B_PLAN_PROMPT,
                workflow_id=wf["id"],
                exact_workflow=True,
            ) or {}
            options = state.get("options") if isinstance(state.get("options"), dict) else {}
            required_dimensions = [
                str(item).strip().upper()
                for item in options.get("required_dimensions") or []
                if str(item).strip()
            ] or list(BACKGROUND_DIMENSIONS)
            plan, dimension_findings = normalize_background_plan(
                plan,
                required_dimensions=required_dimensions,
            )
            if dimension_findings:
                state.setdefault("background_plan_dimension_findings", []).append(
                    {
                        "recorded_at": utc_now(),
                        "findings": dimension_findings,
                    }
                )
                del state["background_plan_dimension_findings"][:-20]
            candidate_lock = build_plan_lock(plan)
            approved_lock = state.get("background_research_plan_lock")
            if isinstance(approved_lock, dict) and approved_lock:
                try:
                    next_lock = validate_plan_transition(
                        approved_lock,
                        plan,
                        allow_additive=True,
                        # WF-3B deterministically derives query-to-dimension
                        # bindings from the model-authored dimension field.  A
                        # legacy lock may predate that adapter, so adding only
                        # those bindings is a strengthening migration; query IDs
                        # and text remain immutable.
                        allow_binding_enrichment=True,
                    )
                except ResearchExecutionContractError as exc:
                    raise PublicResearchPlanError(
                        str(exc),
                        details={
                            "code": exc.code,
                            **exc.details,
                            "approved_plan_hash": approved_lock.get("plan_hash"),
                            "candidate_plan_hash": candidate_lock.get("plan_hash"),
                        },
                    ) from exc
                if next_lock.get("plan_hash") != approved_lock.get("plan_hash"):
                    state.setdefault("background_research_plan_lock_history", []).append(
                        {
                            "recorded_at": utc_now(),
                            "from_plan_hash": approved_lock.get("plan_hash"),
                            "to_plan_hash": next_lock.get("plan_hash"),
                            "transition": "ADDITIVE_PLAN_DELTA",
                        }
                    )
                    del state["background_research_plan_lock_history"][:-20]
                state["background_research_plan_lock"] = next_lock
            else:
                state["background_research_plan_lock"] = candidate_lock
            provider = self.executor.gateway.settings.public_search_provider
            if mode == "SIMULATED" and provider == "disabled":
                candidate = self._background_research().simulated_search(plan)
            else:
                candidate = await self._background_research().search(
                    plan,
                    project_id=wf["project_id"],
                    workflow_id=wf["id"],
                    security_level="PUBLIC",
                )

        accepted = state.get("background_search_results")
        if isinstance(accepted, dict) and accepted:
            comparison = compare_public_search_candidates(accepted, candidate)
            state.setdefault("background_search_candidate_history", []).append(
                {
                    "recorded_at": utc_now(),
                    "decision": "ACCEPT" if comparison["accepted"] else "REJECT",
                    **comparison,
                }
            )
            del state["background_search_candidate_history"][:-20]
            if not comparison["accepted"]:
                return
        state["background_search_results"] = candidate
        state["background_search_accepted_baseline"] = summarize_public_search(candidate)
        if isinstance(candidate.get("research_sufficiency"), dict):
            state["background_research_sufficiency"] = copy.deepcopy(candidate["research_sufficiency"])
            state["background_research_gaps"] = copy.deepcopy(candidate.get("research_gaps") or candidate["research_sufficiency"].get("research_gaps") or [])
        if isinstance(candidate.get("retrieval_health"), dict):
            state["background_retrieval_health"] = copy.deepcopy(candidate["retrieval_health"])

    @staticmethod
    def _repair_state_key(prompt_id: str, state: dict[str, Any]) -> str:
        """Return a section-scoped repair budget key when authoring a section.

        Section drafts are independent mutable objects.  A repair consumed for one
        section must not exhaust the budget or leak an override into another section.
        Non-section workflows retain the historic prompt-level key.
        """
        section_id = str(state.get("active_section_id") or "").strip()
        return f"section:{section_id}:{prompt_id}" if section_id else prompt_id

    @classmethod
    def _repair_override_key(cls, producer_prompt: str, state: dict[str, Any]) -> str:
        return repair_override_key(producer_prompt, state)

    @classmethod
    def _deactivate_repair_application(
        cls,
        state: dict[str, Any],
        producer_prompt: str,
    ) -> None:
        """Deactivate the current repair pointer without mutating artifacts.

        A newly generated producer result supersedes any repair application for
        the previous candidate.  Historical artifacts remain immutable for
        audit, while the workflow state retains only pointers that are currently
        active.
        """
        target_key = cls._repair_override_key(producer_prompt, state)
        index = state.get("repair_application_artifact_ids")
        if not isinstance(index, dict):
            return
        index.pop(target_key, None)
        if not index:
            state.pop("repair_application_artifact_ids", None)

    def _supersede_repair_subject(
        self,
        state: dict[str, Any],
        *,
        critic_prompt: str,
        producer_prompt: str,
        reason: str,
        preserve_application_artifact_id: str | None = None,
    ) -> None:
        attempt_key = self._repair_state_key(critic_prompt, state)
        RepairLedger.reset_semantic_budget(
            state,
            attempt_key,
            reason=reason,
            details={
                "critic_prompt": critic_prompt,
                "producer_prompt": producer_prompt,
            },
        )
        attempts = state.get("repair_attempts")
        if isinstance(attempts, dict):
            attempts.pop(attempt_key, None)
        target_key = self._repair_override_key(producer_prompt, state)
        markers = state.get("producer_contract_repair_markers")
        marker = markers.pop(target_key, None) if isinstance(markers, dict) else None
        if isinstance(markers, dict) and not markers:
            state.pop("producer_contract_repair_markers", None)
        section_id = str(state.get("active_section_id") or "").strip()
        progress = (state.get("section_progress") or {}).get(section_id) or {}
        latest_run = (progress.get("runs") or [{}])[-1]
        preserve_id = str(preserve_application_artifact_id or "").strip()
        active_index = state.get("repair_application_artifact_ids")
        active_ids = (active_index or {}).get(target_key) if isinstance(active_index, dict) else None
        preserve_explicit_application = bool(
            preserve_id and preserve_id in {str(item) for item in (active_ids or [])}
        )
        preserve_current_contract_repair = bool(
            preserve_explicit_application
            or (
                isinstance(marker, dict)
                and str(marker.get("section_id") or "") == section_id
                and str(marker.get("repair_run_id") or "")
                == str(latest_run.get("run_id") or "")
            )
        )
        if preserve_explicit_application and isinstance(active_index, dict):
            active_index[target_key] = [preserve_id]
        elif not preserve_current_contract_repair:
            self._deactivate_repair_application(state, producer_prompt)
        pending = state.get("pending_repair_rereviews")
        if isinstance(pending, dict):
            checkpoint = pending.get(critic_prompt)
            if isinstance(checkpoint, dict) and checkpoint.get("repair_attempt_key") == attempt_key:
                pending.pop(critic_prompt, None)
            if not pending:
                state.pop("pending_repair_rereviews", None)

    @staticmethod
    def _reset_section_repair_state(
        state: dict[str, Any],
        section_id: str,
        *,
        reason: str,
    ) -> None:
        prefix = f"section:{section_id}:"
        attempts = state.get("repair_attempts")
        if isinstance(attempts, dict):
            for key in list(attempts):
                if key.startswith(prefix):
                    attempts.pop(key, None)
        ledger = state.get(RepairLedger.ROOT_KEY)
        if isinstance(ledger, dict):
            counts = ledger.get("semantic_repairs")
            if isinstance(counts, dict):
                for key in list(counts):
                    if key.startswith(prefix):
                        RepairLedger.reset_semantic_budget(
                            state, key, reason=reason, details={"section_id": section_id}
                        )
        repair_index = state.get("repair_application_artifact_ids")
        if isinstance(repair_index, dict):
            for key in list(repair_index):
                if key.startswith(prefix):
                    repair_index.pop(key, None)
            if not repair_index:
                state.pop("repair_application_artifact_ids", None)
        pending = state.get("pending_repair_rereviews")
        if isinstance(pending, dict):
            for prompt_id, checkpoint in list(pending.items()):
                if isinstance(checkpoint, dict) and str(
                    checkpoint.get("repair_attempt_key") or ""
                ).startswith(prefix):
                    pending.pop(prompt_id, None)
            if not pending:
                state.pop("pending_repair_rereviews", None)

    def _can_auto_repair(self, prompt_id: str, state: dict[str, Any]) -> bool:
        if prompt_id not in CRITIC_PRODUCER:
            return False
        key = self._repair_state_key(prompt_id, state)
        options = state.get("options") or {}
        try:
            configured_limit = int(options.get("targeted_repair_limit", 1))
        except (TypeError, ValueError):
            configured_limit = 1
        # One pass remains the production-safe default. Acceptance/test runs may
        # explicitly permit bounded convergence when an independent re-review
        # exposes a second set of repairable findings.
        repair_limit = max(1, min(configured_limit, 3))
        ledger_count = RepairLedger.count(state, "semantic_repairs", key)
        legacy_count = int(state.setdefault("repair_attempts", {}).get(key, 0))
        return max(ledger_count, legacy_count) < repair_limit

    _REPAIR_ID_FIELDS = (
        "claim_id",
        "item_id",
        "node_id",
        "edge_id",
        "paragraph_id",
        "section_id",
        "plan_id",
        "candidate_id",
        "blueprint_id",
        "package_id",
        "template_id",
        "object_id",
        "id",
    )

    @classmethod
    def _repair_content_adapter(
        cls,
        original: Any,
        result_key: str | None,
    ) -> tuple[dict[str, Any], str | None]:
        """Adapt producer results to the object-only targeted-repair contract.

        ``P-TARGETED-REPAIR`` intentionally accepts an object as
        ``original_object.content``.  Most producer results are objects, while
        ``P-FACT-EXTRACT`` exposes the ``fact_candidates`` result as a list.
        Wrap collection-shaped results under their canonical result key and
        remember that key so the repaired value can be restored before it is
        persisted as a versioned ``REPAIR_APPLICATION`` artifact.
        """
        if isinstance(original, dict):
            return copy.deepcopy(original), None
        if isinstance(original, list):
            collection_key = str(result_key or "items").strip() or "items"
            return {collection_key: copy.deepcopy(original)}, collection_key
        raise TypeError(
            "Targeted repair only supports object or list producer results; "
            f"received {type(original).__name__}"
        )

    @classmethod
    def _repair_object_id(
        cls,
        content: dict[str, Any],
        producer: str,
    ) -> str:
        for field in cls._REPAIR_ID_FIELDS:
            value = content.get(field)
            if isinstance(value, (str, int)) and str(value).strip():
                return str(value).strip()
        producer_slug = producer.removeprefix("P-").lower().replace("_", "-")
        return f"repair:{producer_slug}:{sha256_json(content)[:16]}"

    @classmethod
    def _collection_item_index(
        cls,
        items: list[Any],
        selector: str,
    ) -> int | None:
        """Resolve a Critic collection selector to one concrete array index."""

        identity = str(selector or "").strip()
        if not identity:
            return None
        if identity.isdigit():
            index = int(identity)
            return index if 0 <= index < len(items) else None
        field_name: str | None = None
        field_value = identity
        if "=" in identity:
            field_name, field_value = (part.strip() for part in identity.split("=", 1))
            if field_name not in cls._REPAIR_ID_FIELDS or not field_value:
                return None
        candidate_fields = (field_name,) if field_name else cls._REPAIR_ID_FIELDS
        matches = [
            index
            for index, item in enumerate(items)
            if isinstance(item, dict)
            and any(str(item.get(field) or "") == field_value for field in candidate_fields)
        ]
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _locator_tokens(raw_path: str) -> list[str]:
        """Parse a Critic locator into decoded path/selector tokens.

        Critic finding locations are not part of the Targeted Repair wire
        protocol and may use human-readable dot/bracket notation.  This method
        translates that locator exactly once at the boundary.  All paths sent
        to or returned by ``P-TARGETED-REPAIR`` remain strict RFC 6901 JSON
        Pointers.
        """

        raw = str(raw_path or "").strip()
        if not raw:
            return []
        if raw.startswith("/"):
            tokens = list(parse_pointer(raw))
        else:
            raw = re.sub(r"^\$\.?", "", raw)
            tokens: list[str] = []
            for field, selector in re.findall(
                r"(?:^|\.)([A-Za-z_][A-Za-z0-9_-]*)|\[([^\]]+)\]",
                raw,
            ):
                token = (field or selector).strip()
                if token:
                    tokens.append(token)
            if not tokens and raw:
                tokens = [part.strip() for part in raw.split(".") if part.strip()]
        while tokens and tokens[0] in {"result", "payload"}:
            tokens.pop(0)
        if tokens and tokens[0] == "content":
            tokens.pop(0)
        if tokens and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*_candidate", tokens[0]):
            tokens.pop(0)
        return tokens

    @classmethod
    def _paragraph_tokens(
        cls,
        tokens: list[str],
        *,
        content: dict[str, Any],
    ) -> list[str] | None:
        paragraphs = content.get("paragraphs")
        if not isinstance(paragraphs, list):
            return None
        if tokens and tokens[0] == "paragraphs":
            if len(tokens) == 1:
                return ["paragraphs"]
            index = cls._collection_item_index(paragraphs, tokens[1])
            if index is None:
                return None
            return ["paragraphs", str(index), *tokens[2:]]
        if tokens:
            index = cls._collection_item_index(paragraphs, tokens[0])
            if index is not None:
                return ["paragraphs", str(index), *tokens[1:]]
        return None

    @classmethod
    def _canonical_repair_path(
        cls,
        raw_path: str,
        *,
        content: dict[str, Any],
        collection_key: str | None,
    ) -> str:
        """Translate one Critic locator to a resolvable RFC 6901 repair path."""

        try:
            tokens = cls._locator_tokens(raw_path)
        except JsonPointerError as exc:
            raise ValueError(str(exc)) from exc
        if not tokens:
            return join_pointer("content", collection_key) if collection_key else join_pointer("content")

        if collection_key:
            items = content.get(collection_key)
            if not isinstance(items, list):
                raise ValueError(f"repair collection does not exist: {collection_key}")
            if tokens[0] == collection_key:
                if len(tokens) == 1:
                    return join_pointer("content", collection_key)
                selector = tokens[1]
                suffix = tokens[2:]
            else:
                selector = tokens[0]
                suffix = tokens[1:]
            index = cls._collection_item_index(items, selector)
            if index is None:
                if len(items) == 1 and isinstance(items[0], dict) and selector in items[0]:
                    index = 0
                    suffix = tokens
                else:
                    raise ValueError(
                        f"Critic locator does not identify one {collection_key} item: {raw_path!r}"
                    )
            return join_pointer("content", collection_key, index, *suffix)

        paragraph_tokens = cls._paragraph_tokens(tokens, content=content)
        if paragraph_tokens is not None:
            return join_pointer("content", *paragraph_tokens)
        if tokens[0] not in content:
            raise ValueError(
                f"Critic locator does not identify an existing repair-object field: {raw_path!r}"
            )
        return join_pointer("content", *tokens)

    @staticmethod
    def _restore_repaired_shape(
        repaired_object: Any,
        collection_key: str | None,
    ) -> tuple[bool, Any]:
        value = repaired_object
        if isinstance(value, dict) and isinstance(value.get("content"), dict):
            is_metadata_wrapper = any(
                key in value for key in ("object_id", "object_type", "object_hash")
            )
            is_plain_wrapper = set(value) == {"content"}
            contains_wrapped_collection = bool(
                collection_key and collection_key in value["content"]
            )
            if is_metadata_wrapper or is_plain_wrapper or contains_wrapped_collection:
                value = value["content"]
        if collection_key:
            if not isinstance(value, dict) or not isinstance(value.get(collection_key), list):
                return False, None
            return True, copy.deepcopy(value[collection_key])
        if not isinstance(value, dict):
            return False, None
        return True, copy.deepcopy(value)

    def _persist_repair_application(
        self,
        *,
        wf: dict[str, Any],
        state: dict[str, Any],
        producer_prompt: str,
        critic_prompt: str,
        repair_run_id: str,
        original_object_hash: str,
        repaired_value: Any,
        findings: list[dict[str, Any]],
        allowed_paths: list[str],
        collection_key: str | None,
        repaired_canonical_output: dict[str, Any] | None = None,
        atomic_rereview_checkpoint: dict[str, Any] | None = None,
    ) -> str:
        target_key = self._repair_override_key(producer_prompt, state)
        artifact_id = new_id("artifact")
        payload = {
            "schema_version": "1.0.0",
            "workflow_id": wf["id"],
            "producer_prompt": producer_prompt,
            "critic_prompt": critic_prompt,
            "target_key": target_key,
            "section_id": str(state.get("active_section_id") or "") or None,
            "repair_run_id": repair_run_id,
            "application_status": "APPLIED",
            "original_object_hash": original_object_hash,
            "repaired_value_hash": sha256_json(repaired_value),
            "repaired_value": copy.deepcopy(repaired_value),
            "repaired_value_shape": (
                "PRODUCER_RESULT"
                if producer_prompt == "P-ARGUMENT-ARCHITECTURE"
                else "PRODUCER_CONSUMER_VALUE"
            ),
            "repaired_canonical_output": (
                copy.deepcopy(repaired_canonical_output)
                if isinstance(repaired_canonical_output, dict)
                else None
            ),
            "repaired_canonical_output_shape": (
                "PRODUCER_PROTOCOL_OUTPUT"
                if isinstance(repaired_canonical_output, dict)
                else None
            ),
            "finding_codes": [
                str(item.get("code")) for item in findings if item.get("code")
            ],
            "allowed_paths": list(allowed_paths),
            "collection_key": collection_key,
            "authority": "TARGETED_REPAIR_APPLICATION",
        }
        next_state = copy.deepcopy(state)
        ids = next_state.setdefault("repair_application_artifact_ids", {}).setdefault(
            target_key, []
        )
        ids.append(artifact_id)
        del ids[:-50]
        if atomic_rereview_checkpoint is not None:
            checkpoint = copy.deepcopy(atomic_rereview_checkpoint)
            repair_id = str(checkpoint.get("repair_id") or "").strip()
            repair_attempt_key = str(checkpoint.get("repair_attempt_key") or "").strip()
            checkpoint_critic = str(checkpoint.get("critic_prompt") or critic_prompt).strip()
            if not repair_id or not repair_attempt_key or not checkpoint_critic:
                raise ValueError("atomic repair rereview checkpoint is incomplete")
            RepairLedger.applied(
                next_state,
                repair_attempt_key,
                repair_id=repair_id,
                run_id=repair_run_id or None,
                application_artifact_id=artifact_id,
                details={
                    "critic_prompt": checkpoint_critic,
                    "producer_prompt": producer_prompt,
                    "finding_codes": [
                        str(item.get("code")) for item in findings if item.get("code")
                    ],
                },
            )
            next_state.setdefault("pending_repair_rereviews", {})[checkpoint_critic] = {
                "repair_id": repair_id,
                "repair_attempt_key": repair_attempt_key,
                "repair_application_artifact_id": artifact_id,
                "run_id": repair_run_id or None,
                "critic_prompt": checkpoint_critic,
            }
        with self.db.transaction() as tx:
            version = tx.next_artifact_version(
                project_id=wf["project_id"],
                workflow_id=wf["id"],
                artifact_type="REPAIR_APPLICATION",
                prompt_id=producer_prompt,
            )
            tx.execute(
                """INSERT INTO artifacts(id,project_id,workflow_id,artifact_type,prompt_id,version,status,security_level,context_hash,content_json,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    artifact_id,
                    wf["project_id"],
                    wf["id"],
                    "REPAIR_APPLICATION",
                    producer_prompt,
                    version,
                    "PASS",
                    self._project_level(wf["project_id"]),
                    sha256_json(payload),
                    json.dumps(payload, ensure_ascii=False),
                    utc_now(),
                ),
            )
            updated_at = tx.update_workflow(
                workflow_id=wf["id"],
                status=str(wf.get("status") or "RUNNING"),
                current_step=int(wf.get("current_step") or 0),
                state=next_state,
                expected_updated_at=wf.get("updated_at"),
            )
            tx.audit(
                "REPAIR_APPLICATION_APPLIED",
                project_id=wf["project_id"],
                object_id=artifact_id,
                metadata={
                    "workflow_id": wf["id"],
                    "producer_prompt": producer_prompt,
                    "critic_prompt": critic_prompt,
                    "target_key": target_key,
                    "repair_run_id": repair_run_id,
                    "version": version,
                },
            )

        state.clear()
        state.update(next_state)
        wf["state"] = state
        wf["updated_at"] = updated_at
        return artifact_id

    @staticmethod
    def _repair_lifecycle_identity(repaired: dict[str, Any]) -> tuple[str, str, str]:
        repair_id = str(repaired.get("repair_id") or "").strip()
        attempt_key = str(repaired.get("repair_attempt_key") or "").strip()
        artifact_id = str(
            repaired.get("repair_application_artifact_id") or ""
        ).strip()
        if not repair_id or not attempt_key or not artifact_id:
            raise ValueError("Applied repair is missing lifecycle identity")
        return repair_id, attempt_key, artifact_id

    @staticmethod
    def _workflow_repair_rereview_checkpoint(
        state: dict[str, Any],
        prompt_id: str,
    ) -> dict[str, Any] | None:
        """Return the only valid workflow-level pending re-review checkpoint.

        Generic workflows are sequential.  A pending re-review for another
        Prompt therefore proves that the workflow step and persisted repair
        lifecycle diverged; silently ignoring it could complete a workflow
        without independent verification.
        """

        pending = state.get("pending_repair_rereviews")
        if pending is None:
            return None
        if not isinstance(pending, dict):
            raise ValueError("pending_repair_rereviews must be an object")
        if not pending:
            return None
        unexpected = sorted(str(key) for key in pending if str(key) != prompt_id)
        if unexpected:
            raise ValueError(
                "Persisted workflow repair re-review checkpoint belongs to "
                f"another Prompt: {unexpected}; current {prompt_id}."
            )
        checkpoint = pending.get(prompt_id)
        if not isinstance(checkpoint, dict):
            raise ValueError(
                f"Persisted repair re-review checkpoint for {prompt_id} is invalid."
            )
        checkpoint_prompt = str(checkpoint.get("critic_prompt") or "")
        if checkpoint_prompt and checkpoint_prompt != prompt_id:
            raise ValueError(
                "Persisted repair re-review checkpoint Prompt identity does not "
                f"match: expected {checkpoint_prompt}, current {prompt_id}."
            )
        return checkpoint

    @staticmethod
    def _clear_workflow_repair_rereview(
        state: dict[str, Any],
        prompt_id: str,
    ) -> None:
        pending = state.get("pending_repair_rereviews")
        if not isinstance(pending, dict):
            return
        pending.pop(prompt_id, None)
        if not pending:
            state.pop("pending_repair_rereviews", None)

    @classmethod
    def _repair_rereview_checkpoint(
        cls,
        repaired: dict[str, Any],
    ) -> dict[str, Any]:
        repair_id, attempt_key, artifact_id = cls._repair_lifecycle_identity(repaired)
        return {
            "repair_id": repair_id,
            "repair_attempt_key": attempt_key,
            "repair_application_artifact_id": artifact_id,
            "run_id": str(repaired.get("run_id") or "") or None,
        }

    def _start_repair_rereview(
        self,
        state: dict[str, Any],
        repaired: dict[str, Any],
        *,
        critic_prompt: str,
    ) -> int:
        repair_id, attempt_key, artifact_id = self._repair_lifecycle_identity(repaired)
        count = RepairLedger.rereview_started(
            state,
            attempt_key,
            repair_id=repair_id,
            run_id=str(repaired.get("run_id") or "") or None,
            application_artifact_id=artifact_id,
            details={"critic_prompt": critic_prompt},
        )
        state.setdefault("repair_attempts", {})[attempt_key] = count
        return count

    def _complete_repair_rereview(
        self,
        state: dict[str, Any],
        repaired: dict[str, Any],
        *,
        critic_prompt: str,
        review_run_id: str | None,
        status: str,
    ) -> int:
        repair_id, attempt_key, artifact_id = self._repair_lifecycle_identity(repaired)
        return RepairLedger.rereview_completed(
            state,
            attempt_key,
            repair_id=repair_id,
            run_id=review_run_id,
            application_artifact_id=artifact_id,
            status=status,
            details={"critic_prompt": critic_prompt},
        )

    async def _auto_repair(self, wf: dict[str, Any], critic_prompt: str, critic_input: dict[str, Any], critic_output: dict[str, Any], state: dict[str, Any]) -> dict[str, Any] | None:
        producer = CRITIC_PRODUCER[critic_prompt]
        pack = getattr(self, "pack", None)
        entry_reader = getattr(pack, "entry", None)
        critic_entry = entry_reader(critic_prompt) if callable(entry_reader) else {}
        semantic_critic = str(critic_entry.get("model_contract_mode") or "").upper() == "SEMANTIC"
        findings = self._identified_repair_findings(critic_prompt, [
            item
            for item in critic_output.get("findings", [])
            if item.get("repairable", False)
            and not (semantic_critic and str(item.get("suggested_route") or "").upper() == "ORIGINAL_PRODUCER")
            and not (
                str(item.get("code") or "").startswith("QG_")
                and str(item.get("target_type") or "").endswith("CRITIC")
            )
        ])
        if not findings:
            return None
        result_key = PRODUCER_RESULT_KEY.get(producer)
        repair_override_reader = getattr(
            self.context_builder,
            "_repair_override",
            None,
        )
        repair_override = (
            repair_override_reader(
                state,
                producer,
                workflow_id=str(wf.get("id") or "") or None,
            )
            if callable(repair_override_reader)
            else None
        )
        if repair_override is not None:
            original = repair_override
        elif hasattr(self.context_builder, "_section_prompt_result"):
            original = self.context_builder._section_prompt_result(
                wf["project_id"],
                producer,
                workflow_id=wf.get("id"),
                section_id=str(state.get("active_section_id") or "") or None,
                key=result_key,
            )
        else:
            original = self._context_result(
                wf["project_id"],
                producer,
                result_key,
                workflow_id=wf["id"],
                exact_workflow=True,
            )
        finding_instance_ids = [
            str(item.get("finding_instance_id") or "")
            for item in findings
            if str(item.get("finding_instance_id") or "").strip()
        ]
        if original is None:
            self._record_nonexecutable_repair_failure(
                state,
                critic_prompt=critic_prompt,
                reason_code="ORIGINAL_OBJECT_UNAVAILABLE",
                error=(
                    f"{critic_prompt} returned repairable findings, but the exact "
                    f"{producer} producer object is unavailable at the current checkpoint."
                ),
                finding_instance_ids=finding_instance_ids,
            )
            return None
        try:
            repair_content, collection_key = self._repair_content_adapter(
                original,
                result_key,
            )
        except TypeError as exc:
            self._record_nonexecutable_repair_failure(
                state,
                critic_prompt=critic_prompt,
                reason_code="ORIGINAL_OBJECT_SHAPE_UNSUPPORTED",
                error=str(exc),
                finding_instance_ids=finding_instance_ids,
            )
            return None

        attempt_key = self._repair_state_key(critic_prompt, state)
        previous_attempts = int(
            state.setdefault("repair_attempts", {}).get(attempt_key, 0)
        )
        object_id = self._repair_object_id(repair_content, producer)
        original_object = {
            "object_type": producer.removeprefix("P-").replace("-", "_"),
            "object_id": object_id,
            "object_hash": sha256_json(repair_content),
            "content": repair_content,
        }
        original_ref = {
            "object_id": object_id,
            "object_type": original_object["object_type"],
            "version": 1,
            "object_hash": original_object["object_hash"],
            "security_level": self._project_level(wf["project_id"]),
            "display_name": f"{producer}原始输出",
        }
        allowed_paths = []
        original_paragraph_ids = [
            str(item.get("paragraph_id"))
            for item in repair_content.get("paragraphs") or []
            if isinstance(item, dict) and item.get("paragraph_id")
        ]
        paragraph_index_by_id = {
            str(item.get("paragraph_id")): index
            for index, item in enumerate(repair_content.get("paragraphs") or [])
            if isinstance(item, dict) and item.get("paragraph_id")
        }
        def split_target_paths(value: str) -> list[str]:
            parts: list[str] = []
            current: list[str] = []
            bracket_depth = 0
            for char in value:
                if char == "[":
                    bracket_depth += 1
                elif char == "]" and bracket_depth:
                    bracket_depth -= 1
                if char in {",", ";"} and bracket_depth == 0:
                    part = "".join(current).strip()
                    if part:
                        parts.append(part)
                    current = []
                else:
                    current.append(char)
            part = "".join(current).strip()
            if part:
                parts.append(part)
            return parts

        def expand_numeric_bracket_ranges(value: str) -> list[str]:
            match = re.search(r"paragraphs\[([^\]]+)\]", value)
            if not match:
                return [value]
            identities: list[str] = []
            for token in match.group(1).split(","):
                token = token.strip()
                range_match = re.fullmatch(r"(\d+)\s*-\s*(\d+)", token)
                if not range_match:
                    identities.append(token)
                    continue
                start, end = map(int, range_match.groups())
                step = 1 if end >= start else -1
                identities.extend(str(index) for index in range(start, end + step, step))
            return [
                value[: match.start(1)] + identity + value[match.end(1) :]
                for identity in identities
                if identity
            ]

        for finding in findings:
            target = str(finding.get("target_path_or_span") or "result")
            target_parts = [
                expanded
                for part in split_target_paths(target)
                for expanded in expand_numeric_bracket_ranges(part)
            ]
            bracket_ids = [
                item.strip()
                for part in target_parts
                for match in re.finditer(r"paragraphs\[([^\]]+)\]", part)
                for item in match.group(1).split(",")
                if item.strip() and not item.strip().isdigit()
            ]
            canonical_paths_for_finding: list[str] = []
            for part in target_parts:
                if not str(part or "").strip():
                    continue
                try:
                    canonical_path = self._canonical_repair_path(
                        part,
                        content=repair_content,
                        collection_key=collection_key,
                    )
                except ValueError:
                    continue
                allowed_paths.append(canonical_path)
                canonical_paths_for_finding.append(canonical_path)
            if producer in {"P-WRITE-CONTENT", "P-WRITE-BLUEPRINT"}:
                paragraph_ids = [
                    *bracket_ids,
                    *re.findall(
                        r"(?<![A-Za-z0-9-])((?:P|para)-[A-Za-z0-9-]+)(?![A-Za-z0-9-])",
                        target + " " + str(finding.get("repair_instruction") or ""),
                        flags=re.I,
                    ),
                ]
                for paragraph_id in dict.fromkeys(paragraph_ids):
                    paragraph_index = paragraph_index_by_id.get(paragraph_id)
                    paragraph_root = (
                        join_pointer("content", "paragraphs", paragraph_index)
                        if paragraph_index is not None
                        else None
                    )
                    if (
                        paragraph_root is not None
                        and not any(
                            is_ancestor_or_same(paragraph_root, path)
                            for path in canonical_paths_for_finding
                        )
                    ):
                        allowed_paths.append(
                            paragraph_root
                        )
            finding_code = str(finding.get("code") or "")
            finding_text = " ".join(
                str(finding.get(field) or "")
                for field in ("description", "repair_instruction", "target_path_or_span")
            )
            if (
                producer == "P-WRITE-BLUEPRINT"
                and "WORD_BUDGET" in finding_code
            ):
                allowed_paths.extend(
                    join_pointer("content", "paragraphs", paragraph_index_by_id[paragraph_id], "word_budget")
                    for paragraph_id in original_paragraph_ids
                )
            if (
                producer == "P-WRITE-BLUEPRINT"
                and (
                    "CONTENT_KEY" in finding_code
                    or "novel_content_key" in finding_text
                )
            ):
                allowed_paths.extend(
                    join_pointer("content", "paragraphs", paragraph_index_by_id[paragraph_id], "novel_content_key")
                    for paragraph_id in original_paragraph_ids
                )
            if producer == "P-WRITE-CONTENT" and any(
                marker in finding_text
                for marker in ("PAGE_BUDGET", "篇幅", "字数", "word budget")
            ):
                allowed_paths.extend(
                    join_pointer("content", "paragraphs", index, "text")
                    for index, _paragraph_id in enumerate(original_paragraph_ids)
                )
            if producer == "P-WRITE-CONTENT" and "novel_content_key" in finding_text:
                allowed_paths.extend(
                    join_pointer("content", "paragraphs", index, "novel_content_key")
                    for index, _paragraph_id in enumerate(original_paragraph_ids)
                )
        if producer == "P-ARGUMENT-ARCHITECTURE":
            authored_paths = argument_authoritative_repair_paths(repair_content, findings)
            allowed_paths = [
                join_pointer("content", *parse_pointer(path))
                for path in authored_paths
            ]

        if producer == "P-WRITE-CONTENT" and any(
            path.endswith("/text") for path in allowed_paths
        ):
            allowed_paths.append(join_pointer("content", "candidate_text"))
        if producer == "P-WRITE-CONTENT" and any(
            path.endswith(("/primary_claim_id", "/novel_content_key"))
            for path in allowed_paths
        ):
            allowed_paths.append(join_pointer("content", "claim_advancement"))
        allowed_paths = list(dict.fromkeys(allowed_paths))

        overrides = {
            "payload.original_object": original_object,
            "payload.original_producer": PRODUCER_ROLE.get(producer, "WRITING_AGENT"),
            "payload.findings_to_repair": findings,
            "payload.allowed_paths": allowed_paths,
            "payload.protected_paths": [],
            "payload.protected_hashes": [],
            "payload.original_input_refs": [original_ref],
            "payload.inherited_source_catalog": (
                self._inherited_producer_source_catalog(wf, state, producer)
            ),
        }
        active_section_id = str(state.get("active_section_id") or "")
        active_progress = (
            (state.get("section_progress") or {}).get(active_section_id)
            if active_section_id
            else None
        )
        checkpoint_identity = {
            "workflow_step": int(wf.get("current_step") or 0),
            "section_id": active_section_id or None,
            "section_phase": (
                str(active_progress.get("phase") or "") or None
                if isinstance(active_progress, dict)
                else None
            ),
            "critic_prompt": critic_prompt,
            "repair_attempt_key": attempt_key,
            "original_object_hash": original_object["object_hash"],
            "findings_hash": sha256_json(findings),
            "allowed_paths_hash": sha256_json(allowed_paths),
        }
        previous_failure = state.get("last_targeted_repair_failure")
        migration = state.get("contract_migration_recovery")
        resume_checkpoint = (
            isinstance(previous_failure, dict)
            and isinstance(migration, dict)
            and int(migration.get("checkpoint_identity_version") or 0) >= 1
            and int(migration["step"] if migration.get("step") is not None else -1)
            == int(checkpoint_identity["workflow_step"])
            and str(migration.get("section_id") or "")
            == str(checkpoint_identity["section_id"] or "")
            and str(migration.get("section_phase") or "")
            == str(checkpoint_identity["section_phase"] or "")
            and str(migration.get("prompt_id") or "") == "P-TARGETED-REPAIR"
            and str(migration.get("failed_run_id") or "")
            == str(previous_failure.get("run_id") or "")
            and str(previous_failure.get("category") or "")
            == FailureCategory.OUTPUT_CONTRACT.value
            and str(previous_failure.get("critic_prompt") or "") == critic_prompt
            and str(previous_failure.get("repair_attempt_key") or "") == attempt_key
            and bool(str(previous_failure.get("repair_id") or "").strip())
        )
        if resume_checkpoint:
            checkpoint_version = int(
                previous_failure.get("repair_checkpoint_version") or 0
            )
            if checkpoint_version >= 1:
                resume_checkpoint = all(
                    previous_failure.get(key) == value
                    for key, value in checkpoint_identity.items()
                )
            else:
                # Legacy repairs can be upgraded in place only when their
                # already-persisted section-scoped attempt key exactly matches
                # the current repair subject.  Bare legacy failures are not
                # attributable and therefore receive no implicit resume.
                resume_checkpoint = (
                    str(previous_failure.get("repair_attempt_key") or "")
                    == attempt_key
                    and not previous_failure.get("section_id")
                    and previous_failure.get("workflow_step") is None
                )
                if resume_checkpoint:
                    previous_failure.update({
                        "repair_checkpoint_version": 1,
                        **checkpoint_identity,
                    })
        if resume_checkpoint:
            repair_id = str(previous_failure["repair_id"])
            technical_retries_used = max(
                0, int(previous_failure.get("technical_retries_used") or 0)
            )
            resumed_call_key = str(previous_failure.get("call_key") or "").strip()
        else:
            repair_id = new_id("repair")
            technical_retries_used = 0
            resumed_call_key = ""
            state.pop("contract_migration_recovery", None)
        ledger_details = {
            "critic_prompt": critic_prompt,
            "producer_prompt": producer,
            "finding_instance_ids": [
                str(item.get("finding_instance_id"))
                for item in findings
                if item.get("finding_instance_id")
            ],
            "finding_codes": [
                str(item.get("code")) for item in findings if item.get("code")
            ],
        }
        if not resume_checkpoint:
            RepairLedger.repair_created(
                state,
                attempt_key,
                repair_id=repair_id,
                details=ledger_details,
            )
        options = state.get("options") or {}
        try:
            contract_retry_limit = int(
                options.get(
                    "targeted_repair_contract_retry_limit",
                    options.get("provider_retry_limit", 2),
                )
            )
        except (TypeError, ValueError):
            contract_retry_limit = 2
        contract_retry_limit = max(0, min(contract_retry_limit, 5))
        contract_retry_key = f"{attempt_key}:repair:{repair_id}:contract"
        repaired: dict[str, Any]

        while True:
            execution_attempt = technical_retries_used + 1
            attempt_call_key = (
                resumed_call_key
                if resume_checkpoint
                and execution_attempt
                == int(previous_failure.get("execution_attempt") or 0)
                and resumed_call_key
                else "call-repair-" + sha256_json({
                    "workflow_id": wf["id"],
                    "repair_id": repair_id,
                    "execution_attempt": execution_attempt,
                })[:24]
            )
            attempt_overrides = dict(overrides)
            if technical_retries_used:
                previous_failure = state.get("last_targeted_repair_failure") or {}
                validation_errors = list(
                    previous_failure.get("validation_errors") or []
                )
                attempt_overrides["payload.contract_feedback"] = {
                    "attempt": execution_attempt,
                    "validation_errors": validation_errors[:20]
                    or [str(previous_failure.get("error") or "output contract failure")],
                }
            try:
                envelope = self.context_builder.build(
                    "P-TARGETED-REPAIR",
                    wf["project_id"],
                    workflow_id=wf["id"],
                    workflow_state=state,
                    overrides=attempt_overrides,
                )
                provider_retry = getattr(
                    self, "_execute_prompt_with_provider_retry", None
                )
                if callable(provider_retry):
                    repaired = await provider_retry(
                        wf,
                        state,
                        prompt_id="P-TARGETED-REPAIR",
                        envelope=envelope,
                        call_key=attempt_call_key,
                        # Contract-shape failures need the feedback-aware loop
                        # below. The shared provider loop owns only transient
                        # transport/rate-limit/service recovery here.
                        retry_categories=frozenset({
                            FailureCategory.PROVIDER_TRANSIENT
                        }),
                    )
                else:
                    repaired = await self.executor.execute(
                        "P-TARGETED-REPAIR",
                        envelope,
                        project_id=wf["project_id"],
                        workflow_id=wf["id"],
                        original_environment=state.get(
                            "original_environment", "OFFLINE_LOCAL"
                        ),
                    )
            except (
                PromptExecutionError,
                ValueError,
                KeyError,
                ProviderRetriesExhausted,
            ) as exc:
                provider_retries_used = 0
                classified_exc: BaseException = exc
                if isinstance(exc, ProviderRetriesExhausted):
                    classification = exc.classification
                    classified_exc = exc.original_exception
                    provider_retries_used = max(
                        0, exc.decision.completed_attempts - 1
                    )
                else:
                    classification = classify_runtime_failure(exc)
                validation_errors = list(
                    getattr(classified_exc, "validation_errors", None) or []
                )
                failed_run_id = str(
                    getattr(classified_exc, "run_id", None) or ""
                ) or None
                failure = {
                    "critic_prompt": critic_prompt,
                    "category": classification.category.value,
                    "reason": classification.reason,
                    "error": redact_secret_text(str(classified_exc)),
                    "validation_errors": validation_errors,
                    "repair_id": repair_id,
                    "repair_attempt_key": attempt_key,
                    "run_id": failed_run_id,
                    "execution_attempt": execution_attempt,
                    "technical_retries_used": technical_retries_used,
                    "provider_retries_used": provider_retries_used,
                    "consumes_semantic_repair_budget": False,
                    "repair_checkpoint_version": 1,
                    **checkpoint_identity,
                    "call_key": attempt_call_key,
                    "recorded_at": utc_now(),
                }
                state["last_targeted_repair_failure"] = failure
                if (
                    classification.category is FailureCategory.OUTPUT_CONTRACT
                ):
                    RepairLedger.contract_rejected(
                        state,
                        contract_retry_key,
                        repair_id=repair_id,
                        run_id=failed_run_id,
                        details={**ledger_details, **failure},
                    )
                if (
                    classification.category is FailureCategory.OUTPUT_CONTRACT
                    and technical_retries_used < contract_retry_limit
                ):
                    technical_retries_used = RepairLedger.technical_retry(
                        state,
                        contract_retry_key,
                        repair_id=repair_id,
                        run_id=failed_run_id,
                        details={
                            **ledger_details,
                            "next_execution_attempt": execution_attempt + 1,
                            "validation_errors": validation_errors,
                        },
                    )
                    state["last_targeted_repair_failure"][
                        "technical_retries_used"
                    ] = technical_retries_used
                    continue
                state["last_targeted_repair_failure"][
                    "technical_retries_used"
                ] = technical_retries_used
                return None
            break

        if technical_retries_used:
            RepairLedger.contract_recovered(
                state,
                contract_retry_key,
                repair_id=repair_id,
                run_id=str(repaired.get("run_id") or "") or None,
                details={
                    **ledger_details,
                    "technical_retries_used": technical_retries_used,
                },
            )
        state.pop("last_targeted_repair_failure", None)
        state.pop("contract_migration_recovery", None)
        run_id = str(repaired.get("run_id") or "") or None
        RepairLedger.model_returned(
            state,
            attempt_key,
            repair_id=repair_id,
            run_id=run_id,
            details={**ledger_details, "status": repaired.get("status")},
        )
        output = repaired.get("output")
        result_payload = output.get("result") if isinstance(output, dict) else None
        if not isinstance(result_payload, dict):
            state["last_targeted_repair_failure"] = {
                "critic_prompt": critic_prompt,
                "category": "SEMANTIC_REPAIR_REJECTED",
                "error": "P-TARGETED-REPAIR returned no result object",
                "repair_id": repair_id,
                "run_id": run_id,
                "technical_retries_used": technical_retries_used,
                "consumes_semantic_repair_budget": False,
                "repair_checkpoint_version": 1,
                **checkpoint_identity,
                "recorded_at": utc_now(),
            }
            return None
        RepairLedger.schema_validated(
            state,
            attempt_key,
            repair_id=repair_id,
            run_id=run_id,
            details=ledger_details,
        )
        if repaired.get("status") != "PASS":
            unresolved_ids = list(result_payload.get("unresolved_finding_ids") or [])
            failure = {
                "critic_prompt": critic_prompt,
                "category": "SEMANTIC_REPAIR_REJECTED",
                "error": (
                    f"P-TARGETED-REPAIR returned {repaired.get('status')}; "
                    f"unresolved_finding_ids={unresolved_ids}"
                ),
                "repair_id": repair_id,
                "run_id": run_id,
                "technical_retries_used": technical_retries_used,
                "consumes_semantic_repair_budget": False,
                "repair_checkpoint_version": 1,
                **checkpoint_identity,
                "recorded_at": utc_now(),
            }
            state["last_targeted_repair_failure"] = failure
            RepairLedger.repair_rejected(
                state,
                attempt_key,
                repair_id=repair_id,
                run_id=run_id,
                details={**ledger_details, **failure},
            )
            return None
        repaired_object = result_payload.get("repaired_object")
        restored, repaired_value = self._restore_repaired_shape(
            repaired_object,
            collection_key,
        )
        if not restored or sha256_json(repaired_value) == sha256_json(original):
            failure = {
                "critic_prompt": critic_prompt,
                "category": "SEMANTIC_REPAIR_REJECTED",
                "error": "P-TARGETED-REPAIR produced no applicable semantic diff",
                "repair_id": repair_id,
                "run_id": run_id,
                "technical_retries_used": technical_retries_used,
                "consumes_semantic_repair_budget": False,
                "repair_checkpoint_version": 1,
                **checkpoint_identity,
                "recorded_at": utc_now(),
            }
            state["last_targeted_repair_failure"] = failure
            RepairLedger.repair_rejected(
                state,
                attempt_key,
                repair_id=repair_id,
                run_id=run_id,
                details={**ledger_details, **failure},
            )
            return None
        repaired_canonical_output: dict[str, Any] | None = None
        if producer == "P-ARGUMENT-ARCHITECTURE":
            authored_state = repaired_value.get("authored_state") if isinstance(repaired_value, dict) else None
            if not isinstance(authored_state, dict):
                failure = {
                    "critic_prompt": critic_prompt,
                    "category": "SEMANTIC_REPAIR_REJECTED",
                    "error": "Argument Architecture repair did not preserve authoritative authored_state",
                    "repair_id": repair_id,
                    "run_id": run_id,
                    "technical_retries_used": technical_retries_used,
                    "consumes_semantic_repair_budget": False,
                    "repair_checkpoint_version": 1,
                    **checkpoint_identity,
                    "recorded_at": utc_now(),
                }
                state["last_targeted_repair_failure"] = failure
                RepairLedger.repair_rejected(
                    state, attempt_key, repair_id=repair_id, run_id=run_id,
                    details={**ledger_details, **failure},
                )
                return None
            try:
                repaired_canonical_output = project_argument_authoritative_state(
                    critic_input, authored_state
                )
                projection_errors = self.pack.validate(
                    producer, "output", repaired_canonical_output
                )
                if projection_errors:
                    raise PromptExecutionError(
                        "Reprojected Argument Architecture failed producer schema validation",
                        validation_errors=projection_errors,
                    )
            except (PromptExecutionError, ValueError, KeyError, TypeError) as exc:
                validation_errors = list(getattr(exc, "validation_errors", None) or [])
                failure = {
                    "critic_prompt": critic_prompt,
                    "category": "SEMANTIC_REPAIR_REJECTED",
                    "error": redact_secret_text(str(exc)),
                    "validation_errors": validation_errors,
                    "repair_id": repair_id,
                    "run_id": run_id,
                    "technical_retries_used": technical_retries_used,
                    "consumes_semantic_repair_budget": False,
                    "repair_checkpoint_version": 1,
                    **checkpoint_identity,
                    "recorded_at": utc_now(),
                }
                state["last_targeted_repair_failure"] = failure
                RepairLedger.repair_rejected(
                    state, attempt_key, repair_id=repair_id, run_id=run_id,
                    details={**ledger_details, **failure},
                )
                return None
            # Only the reprojected canonical producer result can be committed.
            # The full protocol output is retained separately for validation/audit;
            # downstream consumers receive exactly output["result"].
            repaired_value = producer_consumer_value(producer, repaired_canonical_output)
            if not isinstance(repaired_value, dict):
                raise PromptExecutionError(
                    "Reprojected Argument Architecture has no ProducerResult consumer value"
                )

        RepairLedger.diff_validated(
            state,
            attempt_key,
            repair_id=repair_id,
            run_id=run_id,
            details={
                **ledger_details,
                "original_hash": original_object["object_hash"],
                "repaired_hash": sha256_json(repaired_value),
            },
        )
        if collection_key:
            state.setdefault("repair_shape_adaptations", []).append({
                "producer_prompt": producer,
                "critic_prompt": critic_prompt,
                "collection_key": collection_key,
                "object_id": object_id,
                "repair_run_id": repaired["run_id"],
            })
        state["original_environment"] = repaired["route"]["environment"]
        artifact_id = self._persist_repair_application(
            wf=wf,
            state=state,
            producer_prompt=producer,
            critic_prompt=critic_prompt,
            repair_run_id=repaired["run_id"],
            original_object_hash=original_object["object_hash"],
            repaired_value=repaired_value,
            findings=findings,
            allowed_paths=allowed_paths,
            collection_key=collection_key,
            repaired_canonical_output=repaired_canonical_output,
            atomic_rereview_checkpoint=(
                {
                    "repair_id": repair_id,
                    "repair_attempt_key": attempt_key,
                    "critic_prompt": critic_prompt,
                }
                if producer == "P-ARGUMENT-ARCHITECTURE"
                else None
            ),
        )
        self.quality_manager.record_targeted_repair(
            project_id=wf["project_id"],
            workflow_id=str(state.get("quality_parent_workflow_id") or wf["id"]),
            repair_run_id=repaired["run_id"],
            finding_codes=[str(item.get("code")) for item in findings if item.get("code")],
            finding_instances=findings,
            critic_prompt_id=critic_prompt,
            workflow_state=state,
        )
        if producer != "P-ARGUMENT-ARCHITECTURE":
            RepairLedger.applied(
                state,
                attempt_key,
                repair_id=repair_id,
                run_id=run_id,
                application_artifact_id=artifact_id,
                details=ledger_details,
            )
        repaired["repair_id"] = repair_id
        repaired["repair_attempt_key"] = attempt_key
        repaired["repair_application_artifact_id"] = artifact_id
        return repaired
