from __future__ import annotations

import ast
import copy
import json
from pathlib import Path

import pytest

from app.contracts.semantic_contract import get_semantic_contract
from app.context_base import ContextBuilder
from app.model_semantic_contracts import (
    argument_authoritative_repair_paths,
    build_argument_architecture_critic_model_input,
    expand_argument_architecture_critic_model_output,
    expand_argument_architecture_model_output,
    project_argument_authoritative_state,
    semantic_model_reference_errors,
    targeted_repair_semantic_errors,
)
from tests.test_semantic_contract_final_closure_v7 import (
    _critic_context,
    _fail_dimension,
    _unit_key,
)
from app.pack import PromptPack
from app.workflow_repair import WorkflowRepairMixin

from tests.test_semantic_model_contracts_v1 import (
    _argument_envelope_with_evidence,
    _critic_output_for_canonical,
    _critic_semantic_all_pass,
    _semantic_argument_output,
)


def _producer() -> tuple[dict, dict]:
    envelope = _argument_envelope_with_evidence()
    canonical = expand_argument_architecture_model_output(
        envelope, _semantic_argument_output(envelope)
    )
    return envelope, canonical


def _repair_envelope(canonical: dict, path: str) -> dict:
    return {
        "payload": {
            "original_object": {
                "object_type": "ARGUMENT_ARCHITECTURE",
                "content": canonical["result"],
            },
            "allowed_paths": ["/content" + path],
            "protected_paths": [],
        }
    }


def test_v8_state_ownership_contract_has_one_authoritative_root_and_no_overlap_suppression():
    contract = get_semantic_contract()
    state = contract.rule("SC-ARGUMENT-STATE-OWNERSHIP").config
    assert state["authoritative_root"] == "result/authored_state"
    assert state["overlap_policy"] == "COEXIST"
    assert state["repair_mode"] == "AUTHORITATIVE_STATE_TRANSACTION"
    assert state["field_writers"]["result/authored_state"] == "ARGUMENT_STATE_COMMITTER"
    assert state["semantic_observation_identity"] == "RUN_SCOPED_INSTANCE"
    assert state["namespace_writers"] == {
        "MACHINE_DEFECT": "DETERMINISTIC_RUNTIME",
        "SEMANTIC_OBSERVATION": "ARGUMENT_CRITIC_RUNTIME_FROM_MODEL_ISSUES",
    }
    assert all(
        "model_issue_owners" not in family
        for family in contract.rule("SC-ARGUMENT-DETERMINISTIC-DEFECTS").config["families"].values()
    )


def test_v8_producer_persists_authoritative_state_and_projection_is_reproducible():
    envelope, canonical = _producer()
    result = canonical["result"]
    assert result["projection_meta"]["projection_version"] == "ARGUMENT_PROJECTOR_V2"
    rebuilt = project_argument_authoritative_state(envelope, copy.deepcopy(result["authored_state"]))
    assert rebuilt["result"] == result


def test_v8_critic_ignores_tampered_derived_evidence_cache():
    envelope, canonical = _producer()
    gap = next(
        node
        for node in canonical["result"]["argument_architecture"]["nodes"]
        if node.get("node_type") == "RESEARCH_GAP"
    )
    gap["status"] = "UNKNOWN"
    gap["source_refs"] = []
    canonical["result"]["authored_evidence_bindings"] = []
    output = _critic_output_for_canonical(canonical, envelope)
    assert output["status"] == "PASS"
    assert output["result"]["deterministic_receipts"] == []


def test_v8_machine_defect_and_independent_semantic_observation_coexist():
    envelope = _argument_envelope_with_evidence()
    semantic = _semantic_argument_output(envelope)
    evaluation = semantic["research_threads"][0]["work_packages"][0]["methods"][0]["evaluations"][0]
    evaluation["success_criteria"] = []
    canonical = expand_argument_architecture_model_output(envelope, semantic)
    critic_envelope, model_input, critic_semantic = _critic_context(canonical, envelope)
    _fail_dimension(critic_semantic, "METRIC_JUSTIFICATION")
    critic_semantic["issues"] = [{
        "code": "ARGUMENT_METRIC_JUSTIFICATION_MISSING",
        "dimension": "METRIC_JUSTIFICATION",
        "severity": "P0",
        "target": {
            "component": "EVALUATION",
            "thread_index": 0,
            "item_index": 0,
            "review_unit_key": _unit_key(model_input, "EXPERIMENT_DESIGN"),
        },
        "description": "Even after the machine-detected omission is repaired, the intended evaluation semantics remain ambiguous.",
        "evidence_ids": [],
        "repair_instruction": "Ask the user to choose the intended evaluation semantics.",
        "resolution": "USER_INPUT",
    }]
    critic_semantic["user_questions"] = [{
        "question_id": "Q-V8-SEMANTIC-001",
        "area": "METRIC_JUSTIFICATION",
        "question": "Which evaluation semantics should govern this experiment?",
        "reason": "The semantic choice is independent from the missing success criterion.",
        "blocking": True,
        "question_type": "MISSING_INFORMATION",
        "allowed_values": [],
    }]
    assert semantic_model_reference_errors(
        "P-ARGUMENT-ARCHITECTURE-CRITIC", critic_envelope, critic_semantic
    ) == []
    output = expand_argument_architecture_critic_model_output(
        critic_envelope, critic_semantic
    )
    matching = [
        item
        for item in output["findings"]
        if item.get("code") == "ARGUMENT_METRIC_JUSTIFICATION_MISSING"
    ]
    assert {item["suggested_route"] for item in matching} == {"USER", "ORIGINAL_PRODUCER"}
    assert {item["defect_namespace"] for item in matching} == {
        "SEMANTIC_OBSERVATION", "MACHINE_DEFECT"
    }
    semantic_item = next(item for item in matching if item["defect_namespace"] == "SEMANTIC_OBSERVATION")
    machine_item = next(item for item in matching if item["defect_namespace"] == "MACHINE_DEFECT")
    assert semantic_item["defect_key"] is None
    assert machine_item["defect_key"]
    assert output["status"] == "NEED_USER_INPUT"
    assert output["user_questions"]


def test_v8_semantic_observations_use_run_scoped_identity_without_fake_stable_defect_key():
    envelope, canonical = _producer()
    critic_envelope, model_input, semantic = _critic_context(canonical, envelope)
    _fail_dimension(semantic, "METHOD_SUBSTANCE")
    key = _unit_key(model_input, "FORMAL_MODEL")
    base = {
        "code": "ARGUMENT_METHOD_SUBSTANCE_WEAK",
        "dimension": "METHOD_SUBSTANCE",
        "severity": "P1",
        "target": {"component": "METHOD", "thread_index": 0, "item_index": 0, "review_unit_key": key},
        "evidence_ids": [],
        "resolution": "LOCAL_EDIT",
    }
    issue_a = {**base, "description": "Assumption semantics are underspecified.", "repair_instruction": "Clarify assumptions."}
    issue_b = {**base, "description": "Algorithmic mechanism is underspecified.", "repair_instruction": "Clarify mechanism."}
    first = copy.deepcopy(semantic); first["issues"] = [issue_a, issue_b]
    second = copy.deepcopy(semantic); second["issues"] = [issue_b, issue_a]
    out_a = expand_argument_architecture_critic_model_output(critic_envelope, first)
    out_b = expand_argument_architecture_critic_model_output(critic_envelope, second)

    def observations(output: dict) -> list[dict]:
        return [
            item for item in output["findings"]
            if item.get("defect_namespace") == "SEMANTIC_OBSERVATION"
            and item.get("code") == "ARGUMENT_METHOD_SUBSTANCE_WEAK"
        ]

    for output in (out_a, out_b):
        items = observations(output)
        assert len(items) == 2
        assert {item["description"] for item in items} == {issue_a["description"], issue_b["description"]}
        assert all(item["defect_key"] is None for item in items)
        assert len({item["finding_instance_id"] for item in items}) == 2


@pytest.mark.parametrize(
    ("path", "value", "needle"),
    [
        ("/authored_state/central_proposition/proposition_type", "BOGUS", "is not one of"),
        ("/authored_state/central_proposition/statement", "", "non-empty"),
    ],
)
def test_v8_targeted_repair_rejects_schema_invalid_authoritative_state(path: str, value: str, needle: str):
    _, canonical = _producer()
    errors = targeted_repair_semantic_errors(
        _repair_envelope(canonical, path),
        {"decision": "APPLY", "changes": [{"path": path, "value": value}], "escalation_reason": None},
    )
    assert errors
    assert any(needle in error for error in errors)


def test_v8_repair_reprojects_scope_derived_fields_from_authoritative_state():
    envelope, canonical = _producer()
    authored = copy.deepcopy(canonical["result"]["authored_state"])
    authored["scope"]["in_scope"] = ["仅保留动态重规划核心问题"]
    repaired = project_argument_authoritative_state(envelope, authored)
    assert repaired["result"]["argument_architecture"]["scope_boundaries"]["in_scope"] == ["仅保留动态重规划核心问题"]
    assert repaired["result"]["scope_decision"]["main_body_focus"] == ["仅保留动态重规划核心问题"]


def test_v8_repair_target_mapping_only_exposes_authoritative_state():
    envelope, canonical = _producer()
    critic_envelope = copy.deepcopy(envelope)
    critic_envelope.setdefault("payload", {})["architecture_candidate"] = canonical["result"]
    model_input = build_argument_architecture_critic_model_input(critic_envelope)
    method_key = _unit_key(model_input, "FORMAL_MODEL")
    paths = argument_authoritative_repair_paths(
        canonical["result"],
        [{
            "code": "ARGUMENT_METHOD_SUBSTANCE_WEAK",
            "semantic_component": "METHOD",
            "semantic_thread": 0,
            "semantic_review_unit_key": method_key,
            "target_path_or_span": "/result/argument_architecture/nodes/0",
        }],
    )
    assert paths
    assert all(path.startswith("/authored_state/") for path in paths)
    assert not any("/argument_architecture/" in path for path in paths)


def test_v8_critic_missing_authoritative_state_fails_closed_at_semantic_boundary():
    envelope, canonical = _producer()
    critic_envelope, model_input, semantic = _critic_context(canonical, envelope)
    critic_envelope["payload"]["architecture_candidate"].pop("authored_state")
    errors = semantic_model_reference_errors(
        "P-ARGUMENT-ARCHITECTURE-CRITIC", critic_envelope, semantic
    )
    assert any("authoritative state is required" in error for error in errors)
    with pytest.raises(ValueError, match="missing authoritative authored_state"):
        expand_argument_architecture_critic_model_output(critic_envelope, semantic)


class _V8ArgumentRepairContext:
    def __init__(self, original: dict) -> None:
        self.original = copy.deepcopy(original)
        self.last_overrides: dict | None = None

    def _repair_override(self, state, producer_prompt, *, workflow_id=None):
        assert producer_prompt == "P-ARGUMENT-ARCHITECTURE"
        return copy.deepcopy(self.original)

    def build(self, prompt_id, project_id, **kwargs):
        assert prompt_id == "P-TARGETED-REPAIR"
        self.last_overrides = copy.deepcopy(kwargs.get("overrides") or {})
        return {"prompt_id": prompt_id, "overrides": self.last_overrides}


class _V8QualityManager:
    def __init__(self) -> None:
        self.calls = []

    def record_targeted_repair(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))


class _V8ArgumentRepairHarness(WorkflowRepairMixin):
    def __init__(self, original: dict) -> None:
        self.context_builder = _V8ArgumentRepairContext(original)
        self.pack = PromptPack((__import__("pathlib").Path(__file__).resolve().parents[1]) / "prompt_pack")
        self.quality_manager = _V8QualityManager()
        self.persisted = None

    @staticmethod
    def _project_level(project_id: str) -> str:
        return "INTERNAL"

    def _inherited_producer_source_catalog(self, *args, **kwargs):
        return []

    async def _execute_prompt_with_provider_retry(self, wf, state, *, prompt_id, envelope, **kwargs):
        original = copy.deepcopy(envelope["overrides"]["payload.original_object"]["content"])
        # Simulate the exact v7 stale-projection failure: the model changes the
        # authored source but leaves the old derived scope_decision untouched.
        original["authored_state"]["scope"]["in_scope"] = ["仅保留动态重规划核心问题"]
        finding_ids = [
            str(item["finding_instance_id"])
            for item in envelope["overrides"]["payload.findings_to_repair"]
        ]
        return {
            "run_id": "run-v8-repair",
            "status": "PASS",
            "route": {"environment": "OFFLINE_LOCAL"},
            "call_key": "call-v8-repair",
            "output": {
                "status": "PASS",
                "result": {
                    "repaired_object": original,
                    "resolved_finding_ids": finding_ids,
                    "unresolved_finding_ids": [],
                },
            },
        }

    def _persist_repair_application(self, **kwargs):
        self.persisted = copy.deepcopy(kwargs)
        return "artifact-v8-repair"


def test_v8_targeted_repair_transaction_reprojects_before_persist():
    envelope, canonical = _producer()
    harness = _V8ArgumentRepairHarness(canonical["result"])
    critic_input = copy.deepcopy(envelope)
    critic_input["prompt_id"] = "P-ARGUMENT-ARCHITECTURE-CRITIC"
    critic_input["prompt_version"] = "8.0.0"
    critic_input.setdefault("payload", {})["architecture_candidate"] = copy.deepcopy(canonical["result"])
    critic_output = {
        "findings": [{
            "finding_instance_id": "F-V8-SCOPE-001",
            "defect_namespace": "SEMANTIC_OBSERVATION",
            "defect_key": None,
            "code": "ARGUMENT_SCOPE_VIOLATION",
            "severity": "P1",
            "category": "ARGUMENT",
            "target_type": "ARGUMENT_SEMANTIC_COMPONENT",
            "target_path_or_span": "/result/scope_decision",
            "semantic_component": "SCOPE",
            "semantic_thread": None,
            "semantic_review_unit_key": None,
            "description": "Scope needs a local semantic revision.",
            "evidence_refs": [],
            "repairable": True,
            "repair_instruction": "Narrow the authored in-scope boundary.",
            "suggested_route": "ARGUMENT_ARCHITECTURE_AGENT",
            "blocking": True,
        }]
    }
    state = {"options": {"targeted_repair_contract_retry_limit": 0}}
    repaired = __import__("asyncio").run(
        harness._auto_repair(
            {"id": "wf-v8", "project_id": "project-v8", "current_step": 0},
            "P-ARGUMENT-ARCHITECTURE-CRITIC",
            critic_input,
            critic_output,
            state,
        )
    )
    assert repaired is not None
    assert harness.persisted is not None
    persisted = harness.persisted["repaired_value"]
    assert persisted["authored_state"]["scope"]["in_scope"] == ["仅保留动态重规划核心问题"]
    assert persisted["argument_architecture"]["scope_boundaries"]["in_scope"] == ["仅保留动态重规划核心问题"]
    assert persisted["scope_decision"]["main_body_focus"] == ["仅保留动态重规划核心问题"]
    assert harness.persisted["repaired_canonical_output"]["result"] == persisted



class _V8InvalidArgumentRepairHarness(_V8ArgumentRepairHarness):
    async def _execute_prompt_with_provider_retry(self, wf, state, *, prompt_id, envelope, **kwargs):
        original = copy.deepcopy(envelope["overrides"]["payload.original_object"]["content"])
        original["authored_state"]["central_proposition"]["proposition_type"] = "BOGUS"
        finding_ids = [
            str(item["finding_instance_id"])
            for item in envelope["overrides"]["payload.findings_to_repair"]
        ]
        return {
            "run_id": "run-v8-invalid-repair",
            "status": "PASS",
            "route": {"environment": "OFFLINE_LOCAL"},
            "call_key": "call-v8-invalid-repair",
            "output": {
                "status": "PASS",
                "result": {
                    "repaired_object": original,
                    "resolved_finding_ids": finding_ids,
                    "unresolved_finding_ids": [],
                },
            },
        }


def test_v8_invalid_authoritative_repair_is_rejected_before_any_persist():
    envelope, canonical = _producer()
    harness = _V8InvalidArgumentRepairHarness(canonical["result"])
    critic_input = copy.deepcopy(envelope)
    critic_input["prompt_id"] = "P-ARGUMENT-ARCHITECTURE-CRITIC"
    critic_input["prompt_version"] = "8.0.0"
    critic_input.setdefault("payload", {})["architecture_candidate"] = copy.deepcopy(canonical["result"])
    critic_output = {
        "findings": [{
            "finding_instance_id": "F-V8-INVALID-001",
            "defect_namespace": "SEMANTIC_OBSERVATION",
            "defect_key": None,
            "code": "ARGUMENT_PROPOSITION_UNTESTABLE",
            "severity": "P1",
            "category": "ARGUMENT",
            "target_type": "ARGUMENT_SEMANTIC_COMPONENT",
            "target_path_or_span": "/result/argument_architecture/central_proposition",
            "semantic_component": "CENTRAL_PROPOSITION",
            "semantic_thread": None,
            "semantic_review_unit_key": None,
            "description": "Repair proposition type.",
            "evidence_refs": [],
            "repairable": True,
            "repair_instruction": "Repair locally.",
            "suggested_route": "ARGUMENT_ARCHITECTURE_AGENT",
            "blocking": True,
        }]
    }
    state = {"options": {"targeted_repair_contract_retry_limit": 0}}
    repaired = __import__("asyncio").run(
        harness._auto_repair(
            {"id": "wf-v8-invalid", "project_id": "project-v8", "current_step": 0},
            "P-ARGUMENT-ARCHITECTURE-CRITIC",
            critic_input,
            critic_output,
            state,
        )
    )
    assert repaired is None
    assert harness.persisted is None
    assert state["last_targeted_repair_failure"]["category"] == "SEMANTIC_REPAIR_REJECTED"

def test_v8_context_section_reconciliation_cannot_rewrite_authoritative_projection():
    _, canonical = _producer()
    result = copy.deepcopy(canonical["result"])
    mutated = ContextBuilder._canonicalize_argument_result_from_sections(
        result,
        [{"section_id": "sec-1", "text": "RQ-1 -> RC-99\nRC-99 injected downstream node"}],
    )
    assert mutated == result


def test_v8_context_fact_binding_cannot_rewrite_authoritative_projection():
    _, canonical = _producer()
    result = copy.deepcopy(canonical["result"])
    gap = next(
        node for node in result["argument_architecture"]["nodes"]
        if node.get("node_type") == "RESEARCH_GAP"
    )
    original_gap = copy.deepcopy(gap)
    rebound = ContextBuilder._bind_argument_result_evidence(
        result,
        [{
            "claim_id": gap["node_id"],
            "knowledge_status": "CONFIRMED",
            "source_refs": [{
                "source_id": "injected",
                "source_type": "EVIDENCE_MATERIAL",
                "document_version_id": "doc-injected",
                "section_id": "sec-injected",
                "span_start": 0,
                "span_end": 8,
                "quoted_text": "injected",
                "source_hash": "0" * 64,
                "authority_rank": 100,
                "security_level": "INTERNAL",
            }],
        }],
    )
    rebound_gap = next(
        node for node in rebound["argument_architecture"]["nodes"]
        if node.get("node_id") == gap["node_id"]
    )
    assert rebound_gap == original_gap
    assert rebound == result


def test_v8_critic_rejects_dangling_optional_evidence_inside_authoritative_state():
    envelope, canonical = _producer()
    canonical["result"]["authored_state"]["research_threads"][0]["work_packages"][0]["evidence_ids"] = ["MISSING-EVIDENCE"]
    critic_envelope = copy.deepcopy(envelope)
    critic_envelope["prompt_id"] = "P-ARGUMENT-ARCHITECTURE-CRITIC"
    critic_envelope["prompt_version"] = "8.0.0"
    critic_envelope.setdefault("payload", {})["architecture_candidate"] = canonical["result"]
    with pytest.raises(ValueError, match="MISSING-EVIDENCE"):
        build_argument_architecture_critic_model_input(critic_envelope)


def test_v8_critic_rejects_invalid_local_structured_reference_in_authoritative_state():
    envelope, canonical = _producer()
    ref = canonical["result"]["authored_state"]["research_threads"][0]["innovations"][0]["evaluation_refs"][0]
    ref["evaluation_index"] = 999
    critic_envelope = copy.deepcopy(envelope)
    critic_envelope["prompt_id"] = "P-ARGUMENT-ARCHITECTURE-CRITIC"
    critic_envelope["prompt_version"] = "8.0.0"
    critic_envelope.setdefault("payload", {})["architecture_candidate"] = canonical["result"]
    with pytest.raises(ValueError, match="existing evaluation"):
        build_argument_architecture_critic_model_input(critic_envelope)


def test_v8_every_declared_derived_result_field_is_ignored_as_critic_cache():
    envelope, canonical = _producer()
    baseline_envelope = copy.deepcopy(envelope)
    baseline_envelope["prompt_id"] = "P-ARGUMENT-ARCHITECTURE-CRITIC"
    baseline_envelope["prompt_version"] = "8.0.0"
    baseline_envelope.setdefault("payload", {})["architecture_candidate"] = copy.deepcopy(canonical["result"])
    expected = build_argument_architecture_critic_model_input(baseline_envelope)

    derived_fields = get_semantic_contract().rule("SC-ARGUMENT-STATE-OWNERSHIP").config["derived_result_fields"]
    for field in derived_fields:
        tampered = copy.deepcopy(canonical["result"])
        tampered[field] = {"tampered": True}
        critic_envelope = copy.deepcopy(envelope)
        critic_envelope["prompt_id"] = "P-ARGUMENT-ARCHITECTURE-CRITIC"
        critic_envelope["prompt_version"] = "8.0.0"
        critic_envelope.setdefault("payload", {})["architecture_candidate"] = tampered
        assert build_argument_architecture_critic_model_input(critic_envelope) == expected, field


def test_v8_argument_quality_guard_is_observation_only_and_never_emits_actionable_findings():
    from app.proposal_quality import ProposalQualityGuard

    envelope, canonical = _producer()
    producer_tampered = copy.deepcopy(canonical)
    producer_tampered["result"]["research_design_matrix"] = []
    producer_report = ProposalQualityGuard().observe(
        "P-ARGUMENT-ARCHITECTURE", envelope, producer_tampered
    )
    assert producer_report["status"] == "PASS"
    assert producer_report["findings"] == []
    assert producer_report["observations"]["argument_semantic_authority"]["mode"] == "OBSERVE_CANONICAL_ONLY"
    assert producer_report["observations"]["argument_semantic_authority"]["derived_cache_trusted"] is False

    critic_envelope, _, critic_semantic = _critic_context(canonical, envelope)
    critic_output = expand_argument_architecture_critic_model_output(critic_envelope, critic_semantic)
    critic_envelope["payload"]["architecture_candidate"]["research_design_matrix"] = []
    critic_report = ProposalQualityGuard().observe(
        "P-ARGUMENT-ARCHITECTURE-CRITIC", critic_envelope, critic_output
    )
    assert critic_report["status"] == "PASS"
    assert critic_report["findings"] == []


def test_v8_argument_decision_arbiter_is_audit_only_and_cannot_override_canonical_output():
    from app.decision_arbiter import DecisionArbiter
    from app.workflows import WorkflowEngine

    envelope, canonical = _producer()
    critic_envelope, _, critic_semantic = _critic_context(canonical, envelope)
    critic_output = expand_argument_architecture_critic_model_output(critic_envelope, critic_semantic)
    assert critic_output["status"] == "PASS"

    contract = get_semantic_contract()
    guard = {
        "schema_version": "2.0",
        "status": "REVISE",
        "responsibility": "DETERMINISTIC_GUARD",
        "contract_version": contract.version,
        "contract_rule_registry_version": contract.rule_registry_version,
        "contract_hash": contract.contract_hash,
        "findings": [{
            "code": "QG_ARGUMENT_FAKE_PARALLEL_RULE",
            "severity": "P1",
            "category": "ARGUMENT",
            "target_type": "ARGUMENT_GRAPH",
            "target_path_or_span": "research_design_matrix",
            "description": "parallel guard must remain audit-only",
            "evidence_refs": [],
            "repairable": True,
            "repair_instruction": "should never enter canonical findings",
            "suggested_route": "ARGUMENT_ARCHITECTURE_AGENT",
            "blocking": True,
        }],
    }
    decision = DecisionArbiter().arbitrate(
        critic_output, guard, prompt_id="P-ARGUMENT-ARCHITECTURE-CRITIC"
    ).to_dict()
    assert decision["decision"] == "PASS"
    status, effective = WorkflowEngine._effective_critic_result(
        {"status": critic_output["status"], "output": critic_output}, decision
    )
    assert status == "PASS"
    assert effective == critic_output


def test_v8_downstream_prompt_schemas_do_not_strengthen_argument_work_package_cardinality():
    root = Path(__file__).resolve().parents[1] / "prompt_pack/schemas/prompts"
    seen: list[tuple[str, int | None, int | None]] = []

    def visit(value: object, schema_name: str) -> None:
        if isinstance(value, dict):
            properties = value.get("properties")
            if isinstance(properties, dict):
                field = properties.get("work_package_ids")
                if isinstance(field, dict):
                    minimum = field.get("minItems")
                    maximum = field.get("maxItems")
                    seen.append((schema_name, minimum if isinstance(minimum, int) else None, maximum if isinstance(maximum, int) else None))
            for child in value.values():
                visit(child, schema_name)
        elif isinstance(value, list):
            for child in value:
                visit(child, schema_name)

    for schema_path in sorted(root.glob("*.json")):
        visit(json.loads(schema_path.read_text(encoding="utf-8")), schema_path.name)

    assert seen
    assert all(minimum in {None, 0, 1} for _, minimum, _ in seen), seen
    # Argument Architecture has no semantic upper bound on work-package count, so
    # no downstream copy of the same ID set may silently invent one.
    assert all(maximum is None for _, _, maximum in seen), seen


def test_v8_prompt_pack_build_source_versions_match_registry_for_declared_prompts():
    root = Path(__file__).resolve().parents[1]
    build_source = (root / "prompt_pack/tools/build_v2.py").read_text(encoding="utf-8")
    module = ast.parse(build_source)
    build_versions: dict[str, str] = {}
    for node in module.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "PROMPT_VERSIONS"
            for target in node.targets
        ):
            build_versions = ast.literal_eval(node.value)
            break
    registry = json.loads(
        (root / "prompt_pack/config/prompt_registry.json").read_text(encoding="utf-8")
    )
    registry_versions = {
        str(item["prompt_id"]): str(item["prompt_version"])
        for item in registry["prompts"]
    }
    assert build_versions
    assert {pid: registry_versions.get(pid) for pid in build_versions} == build_versions
