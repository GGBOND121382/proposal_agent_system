from __future__ import annotations

import copy
import json
from pathlib import Path

from jsonschema import Draft202012Validator

from app.executor import PromptExecutor
from app.model_semantic_contracts import (
    SEMANTIC_PROMPTS,
    build_semantic_model_input,
    expand_semantic_model_output,
    semantic_model_reference_errors,
    supports_semantic_model_contract,
)
from app.output_integrity import attach_trusted_source_catalog
from app.pack import PromptPack


ROOT = Path(__file__).resolve().parents[1]
PACK = PromptPack(ROOT / "prompt_pack")
WF1 = (
    "P-SCHEME-EXTRACT",
    "P-SCHEME-CRITIC",
    "P-PROJECT-DEFINITION-EXTRACT",
    "P-PROJECT-DEFINITION-CRITIC",
)
ARGUMENT_CHECK_DIMENSIONS = (
    "DOCUMENT_CONTRACT",
    "CENTRAL_PROPOSITION",
    "RESEARCH_GAP",
    "RESEARCH_QUESTIONS",
    "CLOSEST_PRIOR_WORK",
    "OBJECTIVE_TASK_ALIGNMENT",
    "METHOD_AND_EVALUATION",
    "FOUNDATION_EVIDENCE",
)


def _executor() -> PromptExecutor:
    executor = PromptExecutor.__new__(PromptExecutor)
    executor.pack = PACK
    return executor


def _envelope(prompt_id: str) -> dict:
    return attach_trusted_source_catalog(PACK.replay_input(prompt_id))


def _scheme_extract_output(**overrides) -> dict:
    output = {
        "status": "PASS",
        "document_kind": "APPLICATION_GUIDE",
        "scheme_name": "示例专项",
        "scheme_type": "重点研发",
        "guide_direction_name": "示例方向",
        "research_attribute": None,
        "funding_organization": "示例部委",
        "application_year": 2026,
        "duration_months": 36,
        "rules": [{
            "local_id": "R1",
            "rule_type": "MANDATORY_SCOPE",
            "statement": "围绕示例方向开展研究",
            "mandatory": True,
            "evidence_ids": ["S1"],
        }],
        "findings": [],
        "unresolved_items": [],
        "user_questions": [],
    }
    output.update(overrides)
    return output


def _scheme_critic_output(**overrides) -> dict:
    output = {
        "verdict": "ACCEPT",
        "checked_local_ids": ["R1"],
        "missing_rule_candidates": [],
        "numeric_checks": [{"local_id": "R1", "value_correct": True, "note": "数值一致"}],
        "findings": [],
        "unresolved_items": [],
        "user_questions": [],
    }
    output.update(overrides)
    return output


def _pd_extract_output(**overrides) -> dict:
    output = {
        "status": "PASS",
        "document_kind": "RESEARCH_PROPOSAL",
        "project_name": "示例项目",
        "items": [
            {"local_key": "I1", "item_type": "DEMAND", "domain": "BACKGROUND_AND_DEMAND",
             "summary": "业务扩张带来的优化需求", "attributes": {"urgency": "HIGH"}, "evidence_ids": ["S1"]},
            {"local_key": "I2", "item_type": "SCENARIO", "domain": "BACKGROUND_AND_DEMAND",
             "summary": "配送调度场景", "attributes": {}, "evidence_ids": ["S1"]},
            {"local_key": "I3", "item_type": "GAP", "domain": "STATE_GAP_ROOT_CAUSE",
             "summary": "缺乏可验证的优化能力", "attributes": {}, "evidence_ids": ["S1"]},
            {"local_key": "I4", "item_type": "OBJECTIVE", "domain": "OBJECTIVES",
             "summary": "形成可验证调度优化能力", "attributes": {}, "evidence_ids": ["S1"]},
        ],
        "relations": [
            {"from_key": "I1", "to_key": "I2", "relation_type": "OCCURS_IN", "evidence_ids": ["S1"]},
        ],
        "proposal_contract": {
            "primary_evaluation_logic": "MIXED",
            "target_evaluators": [],
            "max_main_pages": None,
            "max_core_research_questions": 2,
            "mandatory_sections": [],
            "appendix_only_topics": [],
            "forbidden_main_body_topics": [],
        },
        "argument_seed": {
            "central_question": {
                "statement": "如何把业务扩张需求转化为可验证的优化能力",
                "proposition_type": "DESIGN_PROPOSITION",
                "falsifiable_or_comparable": True,
                "boundary_conditions": [],
                "evidence_ids": ["S1"],
            },
            "research_questions": [{
                "statement": "如何形成可验证的调度优化方法",
                "question_type": "TECHNICAL",
                "gap_keys": ["I3"],
                "answerability": "DESIGN_VERIFIABLE",
                "success_evidence": ["约束满足率"],
                "evidence_ids": ["S1"],
            }],
            "in_scope": ["调度优化"],
            "out_of_scope": [],
        },
        "findings": [],
        "unresolved_items": [],
        "user_questions": [],
    }
    output.update(overrides)
    return output


def _pd_critic_output(**overrides) -> dict:
    output = {
        "verdict": "ACCEPT",
        "checked_item_keys": ["K1"],
        "checked_relation_keys": [],
        "invalid_relation_keys": [],
        "status_upgrade_item_keys": [],
        "argument_checks": [
            {"dimension": dimension, "passed": True, "evidence": "审查通过。", "blocking_item_keys": []}
            for dimension in ARGUMENT_CHECK_DIMENSIONS
        ],
        "findings": [],
        "unresolved_items": [],
        "user_questions": [],
    }
    output.update(overrides)
    return output


MODEL_OUTPUTS = {
    "P-SCHEME-EXTRACT": _scheme_extract_output,
    "P-SCHEME-CRITIC": _scheme_critic_output,
    "P-PROJECT-DEFINITION-EXTRACT": _pd_extract_output,
    "P-PROJECT-DEFINITION-CRITIC": _pd_critic_output,
}


def test_all_wf1_model_nodes_use_semantic_contracts() -> None:
    for prompt_id in WF1:
        entry = PACK.entry(prompt_id)
        assert entry["model_contract_mode"] == "SEMANTIC"
        assert entry["model_input_schema"].startswith("schemas/model/")
        assert entry["model_output_schema"].startswith("schemas/model/")
        assert prompt_id in SEMANTIC_PROMPTS
        assert supports_semantic_model_contract(prompt_id)


def test_wf1_model_schemas_are_valid_json_schema() -> None:
    for prompt_id in WF1:
        for kind in ("input", "output"):
            Draft202012Validator.check_schema(PACK.model_schema(prompt_id, kind))


def test_wf1_model_inputs_validate_and_hide_machine_fields() -> None:
    forbidden = (
        "text_hash", "document_hash", "item_hash", "profile_hash", "package_hash",
        "relation_hash", "source_hash", "authority_rank", "document_id",
        "profile_id", "item_id", "relation_id", "security_context",
        "allowed_model_endpoint_ids", "project_id",
    )
    for prompt_id in WF1:
        envelope = _envelope(prompt_id)
        model_input = build_semantic_model_input(prompt_id, envelope)
        assert PACK.validate_model(prompt_id, "input", model_input) == []
        assert model_input["evidence_cards"], prompt_id
        text = json.dumps(model_input, ensure_ascii=False)
        for field in forbidden:
            assert field not in text, f"{prompt_id} leaks {field}"


def test_wf1_model_outputs_expand_to_valid_canonical_outputs() -> None:
    for prompt_id in WF1:
        envelope = _envelope(prompt_id)
        semantic = MODEL_OUTPUTS[prompt_id]()
        assert PACK.validate_model(prompt_id, "output", semantic) == []
        assert semantic_model_reference_errors(prompt_id, envelope, semantic) == []
        expanded = expand_semantic_model_output(prompt_id, envelope, semantic)
        normalized = _executor()._normalize_output(prompt_id, expanded, envelope)
        assert PACK.validate(prompt_id, "output", normalized) == []
        assert normalized["status"] == "PASS"
        assert normalized["prompt_version"] == PACK.entry(prompt_id)["prompt_version"]


def test_scheme_extract_runtime_owns_ids_hashes_and_coverage() -> None:
    envelope = _envelope("P-SCHEME-EXTRACT")
    semantic = _scheme_extract_output()
    expanded = expand_semantic_model_output("P-SCHEME-EXTRACT", envelope, semantic)
    normalized = _executor()._normalize_output("P-SCHEME-EXTRACT", expanded, envelope)
    assert PACK.validate("P-SCHEME-EXTRACT", "output", normalized) == []
    profile = normalized["result"]["scheme_profile"]
    assert profile["profile_id"]
    assert len(profile["profile_hash"]) == 64
    rule = profile["rules"][0]
    assert rule["rule_id"] != "R1"
    assert rule["source_refs"][0]["quoted_text"]
    assert rule["source_refs"][0]["source_hash"]
    assert normalized["result"]["extraction_coverage"]
    coverage = normalized["result"]["extraction_coverage"][0]
    assert coverage["covered_rule_ids"] == [rule["rule_id"]]


def test_scheme_critic_maps_local_ids_back_to_candidate_rule_ids() -> None:
    envelope = _envelope("P-SCHEME-CRITIC")
    candidate_rule_id = envelope["payload"]["scheme_candidate"]["rules"][0]["rule_id"]
    semantic = _scheme_critic_output(
        findings=[{
            "code": "SCHEME_RULE_EVIDENCE_WEAK",
            "severity": "P2",
            "description": "规则证据较弱。",
            "target_local_id": "R1",
            "evidence_ids": ["S1"],
            "blocking": False,
            "route": "ORIGINAL_PRODUCER",
            "repair_instruction": "补充证据。",
        }],
    )
    assert semantic_model_reference_errors("P-SCHEME-CRITIC", envelope, semantic) == []
    expanded = expand_semantic_model_output("P-SCHEME-CRITIC", envelope, semantic)
    normalized = _executor()._normalize_output("P-SCHEME-CRITIC", expanded, envelope)
    assert PACK.validate("P-SCHEME-CRITIC", "output", normalized) == []
    assert normalized["result"]["checked_rule_ids"] == [candidate_rule_id]
    assert normalized["result"]["numeric_checks"][0]["rule_id"] == candidate_rule_id
    assert normalized["findings"][0]["target_path_or_span"] == "/payload/scheme_candidate/rules/0"


def test_pd_extract_expands_items_relations_contract_and_argument_seed() -> None:
    envelope = _envelope("P-PROJECT-DEFINITION-EXTRACT")
    semantic = _pd_extract_output()
    expanded = expand_semantic_model_output("P-PROJECT-DEFINITION-EXTRACT", envelope, semantic)
    normalized = _executor()._normalize_output("P-PROJECT-DEFINITION-EXTRACT", expanded, envelope)
    assert PACK.validate("P-PROJECT-DEFINITION-EXTRACT", "output", normalized) == []
    package = normalized["result"]["project_definition"]
    assert len(package["package_hash"]) == 64
    assert len(package["items"]) == 4
    for item in package["items"]:
        assert len(item["item_hash"]) == 64
        assert item["item_id"].startswith("item-")
        assert item["knowledge_status"] == "DOCUMENT_EXTRACTED"
    relation = package["relations"][0]
    assert len(relation["relation_hash"]) == 64
    item_ids = {item["item_id"] for item in package["items"]}
    assert {relation["source_item_id"], relation["target_item_id"]} <= item_ids
    contract = normalized["result"]["proposal_contract"]
    assert contract["document_type"] == "RESEARCH_PROPOSAL"
    assert contract["max_core_research_questions"] == 2
    seed = normalized["result"]["argument_graph_seed"]
    assert seed["research_questions"][0]["linked_gap_ids"]
    assert seed["research_questions"][0]["linked_gap_ids"][0] in item_ids
    assert seed["nodes"]
    assert seed["edges"]


def test_pd_critic_requires_eight_argument_dimensions() -> None:
    envelope = _envelope("P-PROJECT-DEFINITION-CRITIC")
    semantic = _pd_critic_output()
    semantic["argument_checks"] = semantic["argument_checks"][:-1]
    assert PACK.validate_model("P-PROJECT-DEFINITION-CRITIC", "output", semantic)
    semantic = _pd_critic_output()
    semantic["argument_checks"][0]["dimension"] = "CENTRAL_PROPOSITION"
    errors = semantic_model_reference_errors("P-PROJECT-DEFINITION-CRITIC", envelope, semantic)
    assert any("argument_checks" in error for error in errors)


def test_wf1_unknown_evidence_ids_are_rejected_before_expansion() -> None:
    envelope = _envelope("P-SCHEME-EXTRACT")
    semantic = _scheme_extract_output()
    semantic["rules"][0]["evidence_ids"] = ["S9"]
    errors = semantic_model_reference_errors("P-SCHEME-EXTRACT", envelope, semantic)
    assert any("S9" in error for error in errors)

    envelope = _envelope("P-PROJECT-DEFINITION-EXTRACT")
    semantic = _pd_extract_output()
    semantic["argument_seed"]["research_questions"][0]["gap_keys"] = ["I9"]
    errors = semantic_model_reference_errors("P-PROJECT-DEFINITION-EXTRACT", envelope, semantic)
    assert any("I9" in error for error in errors)


def test_wf1_self_relation_and_unknown_local_ids_are_rejected() -> None:
    envelope = _envelope("P-PROJECT-DEFINITION-EXTRACT")
    semantic = _pd_extract_output()
    semantic["relations"] = [{"from_key": "I1", "to_key": "I1", "relation_type": "USES", "evidence_ids": []}]
    errors = semantic_model_reference_errors("P-PROJECT-DEFINITION-EXTRACT", envelope, semantic)
    assert any("self-referencing" in error for error in errors)

    envelope = _envelope("P-SCHEME-CRITIC")
    semantic = _scheme_critic_output(checked_local_ids=["R9"])
    errors = semantic_model_reference_errors("P-SCHEME-CRITIC", envelope, semantic)
    assert any("R9" in error for error in errors)

    envelope = _envelope("P-PROJECT-DEFINITION-CRITIC")
    semantic = _pd_critic_output(invalid_relation_keys=["L9"])
    errors = semantic_model_reference_errors("P-PROJECT-DEFINITION-CRITIC", envelope, semantic)
    assert any("L9" in error for error in errors)


def test_wf1_choice_question_requires_allowed_values() -> None:
    envelope = _envelope("P-SCHEME-EXTRACT")
    semantic = _scheme_extract_output(
        user_questions=[{
            "question_type": "CHOICE",
            "question": "选哪个方向？",
            "reason": "指南列出两个方向。",
            "answer_shape": "STRING",
            "allowed_values": [],
            "blocking": True,
            "priority": "P1",
        }],
    )
    errors = semantic_model_reference_errors("P-SCHEME-EXTRACT", envelope, semantic)
    assert any("allowed_values" in error for error in errors)


def test_model_status_field_cannot_override_runtime_status_resolution() -> None:
    envelope = _envelope("P-SCHEME-EXTRACT")
    semantic = _scheme_extract_output(
        status="PASS",
        user_questions=[{
            "question_type": "MISSING_INFORMATION",
            "question": "申报年份是哪一年？",
            "reason": "证据中未给出。",
            "answer_shape": "NUMBER",
            "allowed_values": [],
            "blocking": True,
            "priority": "P1",
        }],
    )
    expanded = expand_semantic_model_output("P-SCHEME-EXTRACT", envelope, semantic)
    assert expanded["status"] == "NEED_USER_INPUT"
    normalized = _executor()._normalize_output("P-SCHEME-EXTRACT", expanded, envelope)
    assert PACK.validate("P-SCHEME-EXTRACT", "output", normalized) == []
    assert normalized["status"] == "NEED_USER_INPUT"
    assert normalized["user_questions"][0]["blocking"] is True
    assert normalized["user_questions"][0]["answer_schema"]["type"] == "NUMBER"


def test_user_routed_blocking_findings_derive_question_and_gate() -> None:
    envelope = _envelope("P-SCHEME-EXTRACT")
    semantic = _scheme_extract_output(
        findings=[{
            "code": "SCHEME_SCOPE_CONFLICT",
            "severity": "P1",
            "description": "两个方向描述冲突，需要人工确认。",
            "target_local_id": "R1",
            "evidence_ids": ["S1"],
            "blocking": True,
            "route": "USER",
            "repair_instruction": None,
        }],
    )
    expanded = expand_semantic_model_output("P-SCHEME-EXTRACT", envelope, semantic)
    assert expanded["status"] == "NEED_USER_INPUT"
    assert expanded["findings"][0]["repairable"] is False
    assert any(q["blocking"] for q in expanded["user_questions"])
    normalized = _executor()._normalize_output("P-SCHEME-EXTRACT", expanded, envelope)
    assert PACK.validate("P-SCHEME-EXTRACT", "output", normalized) == []


def test_block_route_clears_blocking_questions_and_sets_block_status() -> None:
    envelope = _envelope("P-SCHEME-CRITIC")
    semantic = _scheme_critic_output(
        verdict="BLOCK",
        findings=[{
            "code": "SCHEME_CANDIDATE_UNRELATED",
            "severity": "P0",
            "description": "候选规则与证据完全不相关。",
            "target_local_id": None,
            "evidence_ids": [],
            "blocking": True,
            "route": "BLOCK",
            "repair_instruction": None,
        }],
        user_questions=[{
            "question_type": "CONFIRMATION",
            "question": "是否继续？",
            "reason": "模型遗留问题。",
            "answer_shape": "BOOLEAN",
            "allowed_values": [],
            "blocking": True,
            "priority": "P2",
        }],
    )
    expanded = expand_semantic_model_output("P-SCHEME-CRITIC", envelope, semantic)
    assert expanded["status"] == "BLOCK"
    assert expanded["result"]["verdict"] == "BLOCK"
    assert all(not q["blocking"] for q in expanded["user_questions"])
    normalized = _executor()._normalize_output("P-SCHEME-CRITIC", expanded, envelope)
    assert PACK.validate("P-SCHEME-CRITIC", "output", normalized) == []
    assert normalized["status"] == "BLOCK"


def test_blocking_finding_without_questions_yields_revise() -> None:
    envelope = _envelope("P-PROJECT-DEFINITION-CRITIC")
    semantic = _pd_critic_output(
        verdict="REVISE",
        findings=[{
            "code": "PROJECT_GRAPH_INCOMPLETE",
            "severity": "P1",
            "description": "目标缺少方法支撑。",
            "target_local_id": "K1",
            "evidence_ids": [],
            "blocking": True,
            "route": "ORIGINAL_PRODUCER",
            "repair_instruction": "补充方法条目。",
        }],
    )
    expanded = expand_semantic_model_output("P-PROJECT-DEFINITION-CRITIC", envelope, semantic)
    assert expanded["status"] == "REVISE"
    assert expanded["result"]["verdict"] == "REVISE"
    normalized = _executor()._normalize_output("P-PROJECT-DEFINITION-CRITIC", expanded, envelope)
    assert PACK.validate("P-PROJECT-DEFINITION-CRITIC", "output", normalized) == []
    assert normalized["status"] == "REVISE"


def test_model_input_size_smaller_than_legacy_full_schema_prompt() -> None:
    for prompt_id in WF1:
        envelope = _envelope(prompt_id)
        model_input = build_semantic_model_input(prompt_id, envelope)
        semantic_chars = len(json.dumps(model_input, ensure_ascii=False))
        legacy_chars = len(json.dumps(envelope, ensure_ascii=False)) + len(
            json.dumps(PACK.inlined_schema(prompt_id, "output"), ensure_ascii=False)
        )
        assert semantic_chars < legacy_chars


def test_missing_required_arrays_default_to_empty_before_validation() -> None:
    from app.model_semantic_contracts import apply_semantic_model_output_defaults

    schema = PACK.model_schema("P-SCHEME-CRITIC", "output")
    minimal = {
        "verdict": "ACCEPT",
        "checked_local_ids": ["R1"],
        "numeric_checks": [],
    }
    normalized = apply_semantic_model_output_defaults(schema, minimal)
    assert PACK.validate_model("P-SCHEME-CRITIC", "output", normalized) == []
    assert normalized["findings"] == []
    assert normalized["missing_rule_candidates"] == []
    assert normalized["unresolved_items"] == []
    assert normalized["user_questions"] == []

    # Existing values are never overwritten.
    kept = apply_semantic_model_output_defaults(
        schema, {**minimal, "findings": [{"code": "X"}]}
    )
    assert kept["findings"] == [{"code": "X"}]

    # Non-array required fields are still enforced (no fabricated verdict).
    assert PACK.validate_model("P-SCHEME-CRITIC", "output", {"checked_local_ids": []})


def test_empty_string_allowed_values_are_dropped() -> None:
    from app.model_semantic_contracts import apply_semantic_model_output_defaults

    schema = PACK.model_schema("P-PROJECT-DEFINITION-EXTRACT", "output")
    output = {
        "status": "NEED_USER_INPUT",
        "document_kind": "RESEARCH_REPORT",
        "project_name": "示例调研",
        "items": [],
        "relations": [],
        "proposal_contract": {},
        "argument_seed": {},
        "findings": [],
        "unresolved_items": [],
        "user_questions": [
            {
                "question_type": "CHOICE",
                "question": "选择交付形态？",
                "reason": "范围确认",
                "answer_shape": "STRING",
                "allowed_values": ["", "调研报告"],
                "blocking": True,
                "priority": "P0",
            }
        ],
    }
    normalized = apply_semantic_model_output_defaults(schema, output)
    values = normalized["user_questions"][0]["allowed_values"]
    assert values == ["调研报告"]
    errors = PACK.validate_model("P-PROJECT-DEFINITION-EXTRACT", "output", normalized)
    assert not any("allowed_values" in error for error in errors)


def test_empty_string_becomes_null_where_schema_allows_null() -> None:
    from app.model_semantic_contracts import _null_empty_strings

    schema = {
        "type": "object",
        "properties": {
            "max_main_pages": {"type": ["integer", "null"]},
            "title": {"type": "string"},
            "contract": {
                "type": "object",
                "properties": {"review_rounds": {"type": ["integer", "null"]}},
            },
            "tags": {"type": "array", "items": {"type": ["string", "null"]}},
        },
    }
    value = {
        "max_main_pages": "",
        "title": "",
        "contract": {"review_rounds": "  "},
        "tags": ["a", ""],
    }
    cleaned = _null_empty_strings(schema, value)
    assert cleaned["max_main_pages"] is None
    assert cleaned["contract"]["review_rounds"] is None
    assert cleaned["tags"] == ["a", None]
    # Plain string fields keep their empty string (schema does not allow null).
    assert cleaned["title"] == ""
    # Fields without a property spec (e.g. reached through $ref) are untouched.
    assert _null_empty_strings({"$ref": "common/x.json"}, "") == ""
