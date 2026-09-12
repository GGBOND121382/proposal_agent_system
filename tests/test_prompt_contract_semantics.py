from __future__ import annotations

import copy
import json
from pathlib import Path

from app.pack import PromptPack
from app.prompt_contracts import documented_finding_codes


ROOT = Path(__file__).resolve().parents[1]
PACK_ROOT = ROOT / "prompt_pack"


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _normal_case(pack: PromptPack, prompt_id: str) -> dict:
    manifest = _json(PACK_ROOT / "replay" / "manifest.json")
    item = next(
        item
        for item in manifest["cases"]
        if item["prompt_id"] == prompt_id and item["case_type"] == "normal"
    )
    return _json(PACK_ROOT / item["fixture_path"])


def test_input_schema_binds_registered_output_schema() -> None:
    pack = PromptPack(PACK_ROOT)
    for prompt_id in pack.prompt_ids():
        entry = pack.entry(prompt_id)
        declared = pack.schema(prompt_id, "input")["properties"][
            "expected_output_schema"
        ]
        assert declared["const"] == entry["output_schema"], prompt_id


def test_every_replay_obeys_schema_and_cross_field_protocol() -> None:
    pack = PromptPack(PACK_ROOT)
    manifest = _json(PACK_ROOT / "replay" / "manifest.json")
    for item in manifest["cases"]:
        case_path = PACK_ROOT / item["fixture_path"]
        case = _json(case_path)
        input_errors = pack.validate(item["prompt_id"], "input", case["input"])
        expected_valid = case["expected_validation"]["input_schema_valid"]
        assert (not input_errors) is expected_valid, (case_path, input_errors)

        output = case.get("expected_output")
        if isinstance(output, dict):
            assert not pack.validate(item["prompt_id"], "output", output), case_path


def test_replay_findings_use_prompt_documented_codes() -> None:
    pack = PromptPack(PACK_ROOT)
    manifest = _json(PACK_ROOT / "replay" / "manifest.json")
    for item in manifest["cases"]:
        output = _json(PACK_ROOT / item["fixture_path"]).get("expected_output")
        if not isinstance(output, dict):
            continue
        entry = pack.entry(item["prompt_id"])
        semantic_mode = str(entry.get("model_contract_mode") or "").upper() == "SEMANTIC"
        documented = documented_finding_codes(pack.prompt_text(item["prompt_id"]))
        if semantic_mode:
            assert entry.get("model_output_schema"), item["prompt_id"]
            continue
        assert documented, item["prompt_id"]
        for finding in output.get("findings") or []:
            assert finding["code"] in documented, (item["fixture_path"], finding["code"])


def test_wrong_expected_output_schema_is_rejected() -> None:
    pack = PromptPack(PACK_ROOT)
    prompt_id = "P-ARGUMENT-ARCHITECTURE"
    payload = copy.deepcopy(_normal_case(pack, prompt_id)["input"])
    payload["expected_output_schema"] = pack.entry("P-REVISION-PLAN")["output_schema"]
    errors = pack.validate(prompt_id, "input", payload)
    assert any("expected_output_schema" in error for error in errors)


def test_need_user_input_requires_a_blocking_question() -> None:
    pack = PromptPack(PACK_ROOT)
    prompt_id = "P-EXPRESSION-CRITIC"
    output = copy.deepcopy(_normal_case(pack, prompt_id)["expected_output"])
    output["status"] = "NEED_USER_INPUT"
    output["result"]["verdict"] = "REVISE"
    output["user_questions"] = []
    errors = pack.validate(prompt_id, "output", output)
    assert any("requires at least one blocking" in error for error in errors)


def test_non_input_status_cannot_carry_blocking_questions() -> None:
    pack = PromptPack(PACK_ROOT)
    prompt_id = "P-EXPRESSION-CRITIC"
    output = copy.deepcopy(_normal_case(pack, prompt_id)["expected_output"])
    output["user_questions"] = [
        {
            "question_id": "q-contract-001",
            "question_type": "CONFIRMATION",
            "question": "请确认当前候选。",
            "reason": "合同测试",
            "target_paths": ["/payload"],
            "answer_schema": {"type": "BOOLEAN", "allowed_values": [True, False]},
            "blocking": True,
            "priority": "P1",
        }
    ]
    errors = pack.validate(prompt_id, "output", output)
    assert any("cannot carry blocking user_questions" in error for error in errors)


def test_status_and_critic_verdict_must_agree() -> None:
    pack = PromptPack(PACK_ROOT)
    prompt_id = "P-EXPRESSION-CRITIC"
    output = copy.deepcopy(_normal_case(pack, prompt_id)["expected_output"])
    output["status"] = "BLOCK"
    output["result"]["verdict"] = "ACCEPT"
    errors = pack.validate(prompt_id, "output", output)
    assert any("conflicts with status BLOCK" in error for error in errors)


def test_pass_cannot_hide_blocking_findings_or_unresolved_items() -> None:
    pack = PromptPack(PACK_ROOT)
    prompt_id = "P-EXPRESSION-CRITIC"
    normal = _normal_case(pack, prompt_id)["expected_output"]

    with_finding = copy.deepcopy(normal)
    with_finding["findings"] = [
        {
            "code": next(iter(documented_finding_codes(pack.prompt_text(prompt_id)))),
            "severity": "P1",
            "category": "CONTENT",
            "target_type": "PROMPT_OUTPUT",
            "target_path_or_span": "/result",
            "description": "阻塞问题",
            "evidence_refs": [],
            "repairable": True,
            "repair_instruction": "修复问题",
            "suggested_route": "ORIGINAL_PRODUCER",
            "blocking": True,
        }
    ]
    assert any("PASS cannot carry blocking findings" in error for error in pack.validate(prompt_id, "output", with_finding))

    with_unresolved = copy.deepcopy(normal)
    with_unresolved["unresolved_items"] = [
        {
            "item_id": "unresolved-contract-001",
            "type": "MISSING",
            "description": "缺失信息",
            "target_paths": ["/payload"],
            "required_action": "补充信息",
            "blocking": True,
        }
    ]
    assert any("PASS cannot carry blocking unresolved_items" in error for error in pack.validate(prompt_id, "output", with_unresolved))


def test_legacy_replay_builder_contains_contract_guards() -> None:
    source = (PACK_ROOT / "tools" / "build_v2.py").read_text(encoding="utf-8")
    assert "'expected_output_schema':{'type':'string','const':" in source
    assert "'NEED_USER_INPUT': 'REVISE'" in source
    assert "'BLOCK': 'BLOCK'" in source
    assert "26个Prompt" not in source
    assert "130组" not in source


def test_prompt_contract_audit_matrix_has_no_blocking_issue() -> None:
    from scripts.audit_prompt_contracts import build_matrix

    report = build_matrix(PACK_ROOT)
    assert report["status"] == "PASS", report["blocking_issues"]
    assert report["summary"]["prompt_count"] == 39
    assert report["summary"]["replay_count"] == 195
    assert report["summary"]["normalization_boundary"][
        "inlined_required_output_fields"
    ] > 0


def test_runtime_output_rejects_undocumented_finding_code() -> None:
    pack = PromptPack(PACK_ROOT)
    prompt_id = "P-EXPRESSION-CRITIC"
    output = copy.deepcopy(_normal_case(pack, prompt_id)["expected_output"])
    output["status"] = "REVISE"
    output["result"]["verdict"] = "REVISE"
    output["findings"] = [
        {
            "code": "UNDECLARED_TEST_FINDING",
            "severity": "P1",
            "category": "CONTENT",
            "target_type": "PROMPT_OUTPUT",
            "target_path_or_span": "/result",
            "description": "测试未声明 Finding。",
            "evidence_refs": [],
            "repairable": True,
            "repair_instruction": "使用 Prompt 声明的 Finding 代码。",
            "suggested_route": "ORIGINAL_PRODUCER",
            "blocking": True,
        }
    ]
    errors = pack.validate(prompt_id, "output", output)
    assert any("is not documented" in error for error in errors)
