from __future__ import annotations

import copy

from app.model_semantic_contracts import (
    build_argument_architecture_critic_model_input,
    expand_argument_architecture_critic_model_output,
    expand_argument_architecture_model_output,
    semantic_model_reference_errors,
)
from tests.test_semantic_model_contracts_v1 import (
    PACK,
    _argument_envelope_with_evidence,
    _critic_semantic_all_pass,
    _semantic_argument_output,
)


def _critic_context(canonical: dict, producer_envelope: dict):
    envelope = copy.deepcopy(PACK.replay_input("P-ARGUMENT-ARCHITECTURE-CRITIC"))
    envelope["payload"]["architecture_candidate"] = canonical["result"]
    envelope["payload"]["confirmed_facts"] = copy.deepcopy(
        producer_envelope["payload"]["confirmed_facts"]
    )
    model_input = build_argument_architecture_critic_model_input(envelope)
    return envelope, model_input, _critic_semantic_all_pass(model_input)


def _unit_key(model_input: dict, node_component: str) -> str:
    return next(
        unit["unit_key"]
        for unit in model_input["candidate"]["review_units"]
        if unit["component"] == node_component
    )


def _fail_dimension(critic_semantic: dict, dimension: str) -> None:
    for item in critic_semantic["quality_dimensions"]:
        if item["dimension"] == dimension:
            item.update(score=1, passed=False, evidence=["model issue"], required_action="repair")


def test_v8_baseline_machine_defect_and_evaluation_semantic_observation_coexist():
    envelope = _argument_envelope_with_evidence()
    semantic = _semantic_argument_output(envelope)
    evaluation = semantic["research_threads"][0]["work_packages"][0]["methods"][0]["evaluations"][0]
    evaluation["baselines"][0]["evidence_ids"] = []
    canonical = expand_argument_architecture_model_output(envelope, semantic)
    critic_envelope, model_input, critic_semantic = _critic_context(canonical, envelope)
    _fail_dimension(critic_semantic, "METRIC_JUSTIFICATION")
    critic_semantic["issues"] = [{
        "code": "ARGUMENT_METRIC_JUSTIFICATION_MISSING",
        "dimension": "METRIC_JUSTIFICATION",
        "severity": "P0",
        "target": {
            "component": "EVALUATION", "thread_index": 0, "item_index": 0,
            "review_unit_key": _unit_key(model_input, "EXPERIMENT_DESIGN"),
        },
        "description": "model incorrectly requests BLOCK",
        "evidence_ids": [],
        "repair_instruction": "block",
        "resolution": "BLOCK",
    }]
    output = expand_argument_architecture_critic_model_output(critic_envelope, critic_semantic)
    receipts = [r for r in output["result"]["deterministic_receipts"] if r["finding_code"] == "ARGUMENT_METRIC_JUSTIFICATION_MISSING"]
    assert len(receipts) == 1
    assert receipts[0]["semantic_component"] == "BASELINE"
    assert receipts[0]["owner_semantic_component"] == "EVALUATION"
    matching = [f for f in output["findings"] if f.get("code") == "ARGUMENT_METRIC_JUSTIFICATION_MISSING"]
    assert len(matching) == 2
    assert {f["defect_namespace"] for f in matching} == {"MACHINE_DEFECT", "SEMANTIC_OBSERVATION"}
    assert {f["suggested_route"] for f in matching} == {"ORIGINAL_PRODUCER", "ARGUMENT_ARCHITECTURE_AGENT"}
    # The model's legacy severity/resolution fields are advisory only. Runtime
    # keeps the machine defect on ORIGINAL_PRODUCER and routes the semantic
    # observation by the registered issue policy.
    assert output["status"] == "REVISE"


def test_v8_prior_work_machine_defect_and_innovation_semantic_observation_coexist():
    envelope = _argument_envelope_with_evidence()
    semantic = _semantic_argument_output(envelope)
    semantic["research_threads"][0]["innovations"][0]["closest_prior_work"][0]["evidence_ids"] = []
    canonical = expand_argument_architecture_model_output(envelope, semantic)
    critic_envelope, model_input, critic_semantic = _critic_context(canonical, envelope)
    _fail_dimension(critic_semantic, "INNOVATION_BASELINE")
    critic_semantic["issues"] = [{
        "code": "INNOVATION_BASELINE_MISSING",
        "dimension": "INNOVATION_BASELINE",
        "severity": "P0",
        "target": {
            "component": "INNOVATION", "thread_index": 0, "item_index": 0,
            "review_unit_key": _unit_key(model_input, "NOVEL_MECHANISM"),
        },
        "description": "model incorrectly requests BLOCK",
        "evidence_ids": [],
        "repair_instruction": "block",
        "resolution": "BLOCK",
    }]
    output = expand_argument_architecture_critic_model_output(critic_envelope, critic_semantic)
    matching = [f for f in output["findings"] if f.get("code") == "INNOVATION_BASELINE_MISSING"]
    assert len(matching) == 2
    assert {f["defect_namespace"] for f in matching} == {"MACHINE_DEFECT", "SEMANTIC_OBSERVATION"}
    assert {f["suggested_route"] for f in matching} == {"ORIGINAL_PRODUCER"}
    # A model-requested BLOCK cannot override the Runtime policy for this issue.
    assert output["status"] == "REVISE"


def test_v7_issue_code_rejects_incompatible_semantic_target():
    envelope = _argument_envelope_with_evidence()
    canonical = expand_argument_architecture_model_output(envelope, _semantic_argument_output(envelope))
    critic_envelope, model_input, critic_semantic = _critic_context(canonical, envelope)
    _fail_dimension(critic_semantic, "INNOVATION_BASELINE")
    critic_semantic["issues"] = [{
        "code": "INNOVATION_BASELINE_MISSING",
        "dimension": "INNOVATION_BASELINE",
        "severity": "P1",
        "target": {
            "component": "FOUNDATION", "thread_index": 0, "item_index": 0,
            "review_unit_key": _unit_key(model_input, "TEAM_EVIDENCE"),
        },
        "description": "wrong semantic target",
        "evidence_ids": [],
        "repair_instruction": "repair",
        "resolution": "LOCAL_EDIT",
    }]
    errors = semantic_model_reference_errors(
        "P-ARGUMENT-ARCHITECTURE-CRITIC", critic_envelope, critic_semantic
    )
    assert any("cannot target semantic component" in error for error in errors)


def test_v8_authoritative_deterministic_failure_canonicalizes_quality_scorecard():
    envelope = _argument_envelope_with_evidence()
    semantic = _semantic_argument_output(envelope)
    semantic["research_threads"][0]["work_packages"][0]["methods"][0]["evaluations"][0]["success_criteria"] = []
    canonical = expand_argument_architecture_model_output(envelope, semantic)
    critic_envelope, _, critic_semantic = _critic_context(canonical, envelope)
    output = expand_argument_architecture_critic_model_output(critic_envelope, critic_semantic)
    metric = next(d for d in output["result"]["quality_dimensions"] if d["dimension"] == "METRIC_JUSTIFICATION")
    assert metric["passed"] is False
    assert metric["score"] == 1
    assert output["status"] == "REVISE"


def test_v8_stale_or_corrupt_derived_matrix_cache_is_ignored():
    envelope = _argument_envelope_with_evidence()
    semantic = _semantic_argument_output(envelope)
    second = copy.deepcopy(semantic["research_threads"][0])
    second["gap"]["statement"] = "second gap"
    second["question"]["statement"] = "second question"
    second["objective"]["statement"] = "second objective"
    semantic["research_threads"].append(second)
    canonical = expand_argument_architecture_model_output(envelope, semantic)
    assert canonical["status"] == "PASS"

    variants = []
    missing = copy.deepcopy(canonical); missing["result"]["research_design_matrix"].pop()
    variants.append(missing)
    duplicate = copy.deepcopy(canonical); duplicate["result"]["research_design_matrix"].append(copy.deepcopy(duplicate["result"]["research_design_matrix"][0]))
    variants.append(duplicate)
    cross = copy.deepcopy(canonical)
    cross["result"]["research_design_matrix"][0]["method_ids"] = [cross["result"]["research_design_matrix"][1]["method_ids"][0]]
    variants.append(cross)
    for variant in variants:
        critic_envelope, _, critic_semantic = _critic_context(variant, envelope)
        output = expand_argument_architecture_critic_model_output(critic_envelope, critic_semantic)
        assert output["status"] == "PASS"
        assert not any(r["defect_family"] == "DESIGN_MATRIX_THREAD_COVERAGE_MISMATCH" for r in output["result"]["deterministic_receipts"])


def test_v7_missing_success_criteria_is_shared_structural_failure_for_producer_and_critic():
    envelope = _argument_envelope_with_evidence()
    semantic = _semantic_argument_output(envelope)
    semantic["research_threads"][0]["work_packages"][0]["methods"][0]["evaluations"][0]["success_criteria"] = []
    canonical = expand_argument_architecture_model_output(envelope, semantic)
    assert canonical["status"] == "REVISE"
    assert any(g["defect_family"] == "STRUCTURAL_REQUIREMENT_UNSATISFIED" for g in canonical["result"]["evidence_gap_report"])
    critic_envelope, _, critic_semantic = _critic_context(canonical, envelope)
    output = expand_argument_architecture_critic_model_output(critic_envelope, critic_semantic)
    assert output["status"] == "REVISE"
    assert any(r["defect_family"] == "STRUCTURAL_REQUIREMENT_UNSATISFIED" for r in output["result"]["deterministic_receipts"])


def test_v7_multiple_model_subissues_same_target_are_not_deduplicated():
    envelope = _argument_envelope_with_evidence()
    canonical = expand_argument_architecture_model_output(envelope, _semantic_argument_output(envelope))
    critic_envelope, model_input, critic_semantic = _critic_context(canonical, envelope)
    _fail_dimension(critic_semantic, "METHOD_SUBSTANCE")
    target = {
        "component": "METHOD", "thread_index": 0, "item_index": 0,
        "review_unit_key": _unit_key(model_input, "FORMAL_MODEL"),
    }
    critic_semantic["issues"] = [
        {
            "code": "ARGUMENT_METHOD_SUBSTANCE_WEAK", "dimension": "METHOD_SUBSTANCE",
            "severity": "P2", "target": copy.deepcopy(target),
            "description": description, "evidence_ids": [],
            "repair_instruction": "repair", "resolution": "LOCAL_EDIT",
        }
        for description in ("weak formulation", "weak algorithmic detail")
    ]
    output = expand_argument_architecture_critic_model_output(critic_envelope, critic_semantic)
    matching = [f for f in output["findings"] if f.get("code") == "ARGUMENT_METHOD_SUBSTANCE_WEAK"]
    assert len(matching) == 2
    assert all(f["defect_key"] is None for f in matching)
    assert len({f["finding_instance_id"] for f in matching}) == 2


def test_v7_multiple_failed_leaf_objects_keep_one_receipt_and_finding_each():
    envelope = _argument_envelope_with_evidence()
    semantic = _semantic_argument_output(envelope)
    evaluation = semantic["research_threads"][0]["work_packages"][0]["methods"][0]["evaluations"][0]
    evaluation["baselines"][0]["evidence_ids"] = []
    evaluation["baselines"].append({"statement": "second baseline", "evidence_ids": []})
    canonical = expand_argument_architecture_model_output(envelope, semantic)
    gaps = [g for g in canonical["result"]["evidence_gap_report"] if g["finding_code"] == "ARGUMENT_METRIC_JUSTIFICATION_MISSING" and g["defect_family"] == "EVIDENCE_REQUIREMENT_UNSATISFIED"]
    assert len(gaps) == 2
    assert len({g["defect_key"] for g in gaps}) == 2
    critic_envelope, _, critic_semantic = _critic_context(canonical, envelope)
    output = expand_argument_architecture_critic_model_output(critic_envelope, critic_semantic)
    receipts = [r for r in output["result"]["deterministic_receipts"] if r["finding_code"] == "ARGUMENT_METRIC_JUSTIFICATION_MISSING" and r["defect_family"] == "EVIDENCE_REQUIREMENT_UNSATISFIED"]
    findings = [f for f in output["findings"] if f.get("code") == "ARGUMENT_METRIC_JUSTIFICATION_MISSING"]
    assert len(receipts) == len(findings) == 2
    assert {r["defect_key"] for r in receipts} == {f["defect_key"] for f in findings}


def test_v8_stale_or_corrupt_derived_graph_cache_is_ignored():
    envelope = _argument_envelope_with_evidence()
    canonical = expand_argument_architecture_model_output(envelope, _semantic_argument_output(envelope))
    graph = canonical["result"]["argument_architecture"]
    graph["nodes"].append({
        "node_id": "orphan-baseline-v7", "node_type": "BASELINE",
        "statement": "orphan baseline", "status": "PLANNED", "source_refs": [],
    })
    matrix = canonical["result"]["research_design_matrix"][0]
    graph["edges"].append({
        "edge_id": "wrong-signature-v7",
        "source_id": matrix["method_ids"][0],
        "relation": "MEASURED_BY",
        "target_id": matrix["innovation_ids"][0],
        "rationale": "invalid endpoint types",
    })
    critic_envelope, _, critic_semantic = _critic_context(canonical, envelope)
    output = expand_argument_architecture_critic_model_output(critic_envelope, critic_semantic)
    assert output["status"] == "PASS"
    topology = [r for r in output["result"]["deterministic_receipts"] if r["defect_family"] == "GRAPH_TOPOLOGY_INVALID"]
    assert topology == []


def test_v8_derived_chain_cache_tamper_cannot_suppress_model_semantic_block():
    envelope = _argument_envelope_with_evidence()
    canonical = expand_argument_architecture_model_output(envelope, _semantic_argument_output(envelope))
    canonical["result"]["argument_architecture"]["edges"] = [
        edge for edge in canonical["result"]["argument_architecture"]["edges"]
        if edge.get("relation") != "MOTIVATES"
    ]
    critic_envelope, _, critic_semantic = _critic_context(canonical, envelope)
    _fail_dimension(critic_semantic, "ARGUMENT_CHAIN")
    critic_semantic["issues"] = [{
        "code": "GAP_QUESTION_CHAIN_BROKEN", "dimension": "ARGUMENT_CHAIN",
        "severity": "P0",
        "target": {"component": "THREAD", "thread_index": 0, "item_index": 0, "review_unit_key": None},
        "description": "model incorrectly blocks entire thread", "evidence_ids": [],
        "repair_instruction": "block", "resolution": "BLOCK",
    }]
    output = expand_argument_architecture_critic_model_output(critic_envelope, critic_semantic)
    assert output["status"] == "REVISE"
    assert any(
        f["defect_namespace"] == "SEMANTIC_OBSERVATION"
        and f["suggested_route"] == "ORIGINAL_PRODUCER"
        for f in output["findings"] if f.get("semantic_thread") == 0
    )


def test_v7_matrix_row_order_does_not_change_thread_identity():
    envelope = _argument_envelope_with_evidence()
    semantic = _semantic_argument_output(envelope)
    second = copy.deepcopy(semantic["research_threads"][0])
    second["gap"]["statement"] = "second gap"
    second["question"]["statement"] = "second question"
    second["objective"]["statement"] = "second objective"
    semantic["research_threads"].append(second)
    canonical = expand_argument_architecture_model_output(envelope, semantic)
    canonical["result"]["research_design_matrix"].reverse()
    critic_envelope, _, critic_semantic = _critic_context(canonical, envelope)
    output = expand_argument_architecture_critic_model_output(critic_envelope, critic_semantic)
    assert output["status"] == "PASS"
    assert output["result"]["deterministic_receipts"] == []


def test_v7_unrelated_same_thread_model_issue_is_not_swallowed_by_chain_receipt():
    envelope = _argument_envelope_with_evidence()
    canonical = expand_argument_architecture_model_output(envelope, _semantic_argument_output(envelope))
    canonical["result"]["argument_architecture"]["edges"] = [
        edge for edge in canonical["result"]["argument_architecture"]["edges"]
        if edge.get("relation") != "MOTIVATES"
    ]
    critic_envelope, model_input, critic_semantic = _critic_context(canonical, envelope)
    _fail_dimension(critic_semantic, "ARGUMENT_CHAIN")
    method_key = _unit_key(model_input, "FORMAL_MODEL")
    critic_semantic["issues"] = [{
        "code": "RESEARCH_DESIGN_INCOMPLETE", "dimension": "ARGUMENT_CHAIN",
        "severity": "P1",
        "target": {
            "component": "METHOD", "thread_index": 0, "item_index": 0,
            "review_unit_key": method_key,
        },
        "description": "The method has a separate semantic linkage defect.",
        "evidence_ids": [], "repair_instruction": "Rework the semantic linkage.",
        "resolution": "BLOCK",
    }]
    assert semantic_model_reference_errors(
        "P-ARGUMENT-ARCHITECTURE-CRITIC", critic_envelope, critic_semantic
    ) == []
    output = expand_argument_architecture_critic_model_output(critic_envelope, critic_semantic)
    model_findings = [
        finding for finding in output["findings"]
        if finding.get("semantic_component") == "METHOD"
        and finding.get("suggested_route") == "ORIGINAL_PRODUCER"
    ]
    assert len(model_findings) == 1
    assert output["status"] == "REVISE"


def test_v7_precise_target_thread_must_match_canonical_review_unit_owner():
    envelope = _argument_envelope_with_evidence()
    semantic = _semantic_argument_output(envelope)
    second = copy.deepcopy(semantic["research_threads"][0])
    second["gap"]["statement"] = "second gap"
    second["question"]["statement"] = "second question"
    second["objective"]["statement"] = "second objective"
    semantic["research_threads"].append(second)
    canonical = expand_argument_architecture_model_output(envelope, semantic)
    critic_envelope, model_input, critic_semantic = _critic_context(canonical, envelope)
    _fail_dimension(critic_semantic, "METHOD_SUBSTANCE")
    method_key = next(
        unit["unit_key"] for unit in model_input["candidate"]["review_units"]
        if unit["component"] == "FORMAL_MODEL"
    )
    critic_semantic["issues"] = [{
        "code": "ARGUMENT_METHOD_SUBSTANCE_WEAK", "dimension": "METHOD_SUBSTANCE",
        "severity": "P2",
        "target": {
            "component": "METHOD", "thread_index": 1, "item_index": 0,
            "review_unit_key": method_key,
        },
        "description": "wrong thread coordinate", "evidence_ids": [],
        "repair_instruction": "repair", "resolution": "LOCAL_EDIT",
    }]
    errors = semantic_model_reference_errors(
        "P-ARGUMENT-ARCHITECTURE-CRITIC", critic_envelope, critic_semantic
    )
    assert any("belongs to thread 0" in error for error in errors)


def test_v7_thread_target_must_reference_existing_thread():
    envelope = _argument_envelope_with_evidence()
    canonical = expand_argument_architecture_model_output(envelope, _semantic_argument_output(envelope))
    critic_envelope, _, critic_semantic = _critic_context(canonical, envelope)
    _fail_dimension(critic_semantic, "ARGUMENT_CHAIN")
    critic_semantic["issues"] = [{
        "code": "GAP_QUESTION_CHAIN_BROKEN", "dimension": "ARGUMENT_CHAIN",
        "severity": "P1",
        "target": {"component": "THREAD", "thread_index": 99, "item_index": 0, "review_unit_key": None},
        "description": "invalid thread", "evidence_ids": [],
        "repair_instruction": "repair", "resolution": "REGENERATE",
    }]
    errors = semantic_model_reference_errors(
        "P-ARGUMENT-ARCHITECTURE-CRITIC", critic_envelope, critic_semantic
    )
    assert any("existing research thread" in error for error in errors)


def test_v8_derived_graph_id_tamper_is_ignored_in_favor_of_authoritative_projection():
    envelope = _argument_envelope_with_evidence()
    canonical = expand_argument_architecture_model_output(envelope, _semantic_argument_output(envelope))
    graph = canonical["result"]["argument_architecture"]
    graph["central_proposition"]["node_id"] = graph["nodes"][0]["node_id"]
    critic_envelope, _, critic_semantic = _critic_context(canonical, envelope)
    output = expand_argument_architecture_critic_model_output(critic_envelope, critic_semantic)
    duplicates = [
        receipt for receipt in output["result"]["deterministic_receipts"]
        if receipt["defect_family"] == "GRAPH_TOPOLOGY_INVALID"
        and "重复 node_id" in receipt["description"]
    ]
    assert duplicates == []
    assert output["status"] == "PASS"


def test_v7_node_status_source_policy_is_registry_owned():
    from app.contracts.semantic_contract import get_semantic_contract
    from pathlib import Path

    config = get_semantic_contract().rule("SC-ARGUMENT-EVIDENCE-REQUIREMENTS").config
    status_policies = {
        str(item["status_node_type"]): str(item["source_policy"])
        for item in config["requirements"]
    }
    assert status_policies == {
        "CENTRAL_PROPOSITION": "STANDARD",
        "RESEARCH_GAP": "STANDARD",
        "LIMITATION_MECHANISM": "STANDARD",
        "CLOSEST_PRIOR_WORK": "STANDARD",
        "TEAM_EVIDENCE": "FOUNDATION",
        "BASELINE": "STANDARD",
    }
    runtime_source = (Path(__file__).resolve().parents[1] / "app" / "model_semantic_contracts.py").read_text(encoding="utf-8")
    start = runtime_source.index("def _semantic_node_status(")
    end = runtime_source.index("def _foundation_has_valid_support(", start)
    status_source = runtime_source[start:end]
    assert 'node_type == "TEAM_EVIDENCE"' not in status_source
    assert "_argument_node_status_source_policies" in status_source



def test_v7_decision_arbiter_cannot_promote_canonical_semantic_revise_to_pass():
    from app.contracts.semantic_contract import get_semantic_contract
    from app.decision_arbiter import DecisionArbiter
    from app.workflows import WorkflowEngine

    envelope = _argument_envelope_with_evidence()
    canonical = expand_argument_architecture_model_output(envelope, _semantic_argument_output(envelope))
    critic_envelope, model_input, critic_semantic = _critic_context(canonical, envelope)
    _fail_dimension(critic_semantic, "METHOD_SUBSTANCE")
    method_key = _unit_key(model_input, "FORMAL_MODEL")
    critic_semantic["issues"] = [{
        "code": "ARGUMENT_METHOD_SUBSTANCE_WEAK", "dimension": "METHOD_SUBSTANCE",
        "severity": "P2",
        "target": {
            "component": "METHOD", "thread_index": 0, "item_index": 0,
            "review_unit_key": method_key,
        },
        "description": "local semantic repair", "evidence_ids": [],
        "repair_instruction": "tighten method substance", "resolution": "LOCAL_EDIT",
    }]
    critic_output = expand_argument_architecture_critic_model_output(critic_envelope, critic_semantic)
    assert critic_output["status"] == "REVISE"
    assert len(critic_output["findings"]) == 1
    assert critic_output["findings"][0]["blocking"] is True
    assert critic_output["findings"][0]["suggested_route"] == "ARGUMENT_ARCHITECTURE_AGENT"
    assert critic_output["findings"][0]["severity"] == "P1"

    contract = get_semantic_contract()
    guard = {
        "schema_version": "2.0",
        "status": "PASS",
        "responsibility": "DETERMINISTIC_GUARD",
        "contract_version": contract.version,
        "contract_rule_registry_version": contract.rule_registry_version,
        "contract_hash": contract.contract_hash,
        "findings": [],
    }
    decision = DecisionArbiter().arbitrate(
        critic_output, guard, prompt_id="P-ARGUMENT-ARCHITECTURE-CRITIC"
    ).to_dict()
    assert decision["decision"] == "REVISE"
    status, effective = WorkflowEngine._effective_critic_result(
        {"status": critic_output["status"], "output": critic_output}, decision
    )
    assert status == "REVISE"
    assert effective["status"] == "REVISE"
    assert len(effective["findings"]) == 1
    assert effective["findings"][0]["defect_key"] == critic_output["findings"][0]["defect_key"]


def test_v7_repair_identity_uses_defect_key_not_description():
    from app.workflow_repair import WorkflowRepairMixin

    base = {
        "defect_key": "D-ARG-STABLE-001",
        "code": "ARGUMENT_METHOD_SUBSTANCE_WEAK",
        "target_type": "ARGUMENT_SEMANTIC_COMPONENT",
        "target_path_or_span": "/result/argument_architecture/nodes/0",
        "description": "first wording",
    }
    changed_wording = copy.deepcopy(base)
    changed_wording["description"] = "completely different natural-language wording"
    different_defect = copy.deepcopy(base)
    different_defect["defect_key"] = "D-ARG-STABLE-002"

    first = WorkflowRepairMixin._identified_repair_findings(
        "P-ARGUMENT-ARCHITECTURE-CRITIC", [base]
    )[0]
    second = WorkflowRepairMixin._identified_repair_findings(
        "P-ARGUMENT-ARCHITECTURE-CRITIC", [changed_wording]
    )[0]
    third = WorkflowRepairMixin._identified_repair_findings(
        "P-ARGUMENT-ARCHITECTURE-CRITIC", [different_defect]
    )[0]
    assert first["finding_instance_id"] == second["finding_instance_id"]
    assert first["finding_instance_id"] != third["finding_instance_id"]


def test_v7_quality_dimension_preserves_model_failure_evidence_alongside_runtime_failure():
    envelope = _argument_envelope_with_evidence()
    semantic = _semantic_argument_output(envelope)
    evaluation = semantic["research_threads"][0]["work_packages"][0]["methods"][0]["evaluations"][0]
    evaluation["baselines"][0]["evidence_ids"] = []
    canonical = expand_argument_architecture_model_output(envelope, semantic)
    critic_envelope, _, critic_semantic = _critic_context(canonical, envelope)
    for item in critic_semantic["quality_dimensions"]:
        if item["dimension"] == "METRIC_JUSTIFICATION":
            item.update(
                score=2,
                passed=False,
                evidence=["model independently found weak metric rationale"],
                required_action="strengthen semantic metric rationale",
            )
    output = expand_argument_architecture_critic_model_output(critic_envelope, critic_semantic)
    dimension = next(
        item for item in output["result"]["quality_dimensions"]
        if item["dimension"] == "METRIC_JUSTIFICATION"
    )
    assert dimension["passed"] is False
    assert "model independently found weak metric rationale" in dimension["evidence"]
    assert any("基线" in evidence or "证据" in evidence for evidence in dimension["evidence"])
    assert "strengthen semantic metric rationale" not in dimension["required_action"]
    assert dimension["required_action"]
    # quality_dimensions.required_action is Runtime-owned; the model's legacy
    # field may contribute evidence but cannot become the canonical action.


def test_v7_scoped_runtime_has_no_local_route_priority_or_regeneration_reconstruction_tables():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    runtime = (root / "app" / "model_semantic_contracts.py").read_text(encoding="utf-8")
    workflows = (root / "app" / "workflows.py").read_text(encoding="utf-8")
    merge_start = runtime.index("def _canonical_critic_work_items(")
    merge_end = runtime.index("def _argument_deterministic_receipts_for_candidate(", merge_start)
    assert "route_priority" not in runtime[merge_start:merge_end]
    assert "component_by_node_type" not in workflows
    assert "code_by_node_type" not in workflows
