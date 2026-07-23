from __future__ import annotations

import copy
from pathlib import Path

from app.pack import PromptPack
from app.track_b import TrackBAgentPromptValidator


ROOT = Path(__file__).resolve().parents[1]


def _runtime():
    pack = PromptPack(ROOT / "prompt_pack")
    return pack, TrackBAgentPromptValidator(pack)


def _codes(output):
    return {item.get("code") for item in output.get("findings", []) if isinstance(item, dict)}


def test_track_b_repository_contract_covers_b1_to_b10():
    report = TrackBAgentPromptValidator.validate_repository(ROOT)
    assert report["status"] == "PASS", report
    assert set(report["checks"]) == {f"B{i}" for i in range(1, 11)}
    assert all(item["passed"] for item in report["checks"].values())


def test_production_runtime_enables_track_b_validator():
    source = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert "TrackBAgentPromptValidator(pack)" in source


def test_b1_scheme_extrapolation_cannot_be_mandatory():
    pack, validator = _runtime()
    env = pack.replay_input("P-SCHEME-EXTRACT")
    output = pack.replay_output("P-SCHEME-EXTRACT")
    rule = output["result"]["scheme_profile"]["rules"][0]
    rule["mandatory"] = True
    rule["source_refs"][0]["source_type"] = "MODEL_INFERENCE"
    checked = validator.apply("P-SCHEME-EXTRACT", env, output)
    assert checked["status"] == "REVISE"
    assert "QG_SCHEME_EXTRAPOLATION_AS_MANDATORY" in _codes(checked)


def test_b2_project_relation_direction_is_checked():
    pack, validator = _runtime()
    env = pack.replay_input("P-PROJECT-DEFINITION-EXTRACT")
    output = pack.replay_output("P-PROJECT-DEFINITION-EXTRACT")
    project = output["result"]["project_definition"]
    objective = copy.deepcopy(project["items"][0])
    objective["item_id"] = "objective-track-b"
    objective["item_type"] = "OBJECTIVE"
    work_package = copy.deepcopy(project["items"][0])
    work_package["item_id"] = "work-package-track-b"
    work_package["item_type"] = "WORK_PACKAGE"
    project["items"] = [objective, work_package]
    project["relations"] = [{
        "relation_id": "relation-track-b",
        "source_item_id": work_package["item_id"],
        "source_item_type": "WORK_PACKAGE",
        "relation_type": "DECOMPOSES_TO",
        "target_item_id": objective["item_id"],
        "target_item_type": "OBJECTIVE",
        "status": "CONFIRMED",
        "confidence": "HIGH",
        "source_refs": [],
        "security_level": "INTERNAL",
        "relation_hash": "1" * 64,
    }]
    checked = validator.apply("P-PROJECT-DEFINITION-EXTRACT", env, output)
    assert "QG_PROJECT_RELATION_DIRECTION_INVALID" in _codes(checked)


def test_b3_fact_records_must_be_atomic_and_numeric_values_bound():
    pack, validator = _runtime()
    env = pack.replay_input("P-FACT-EXTRACT")
    output = pack.replay_output("P-FACT-EXTRACT")
    claim = output["result"]["fact_candidates"][0]
    claim["claim_text"] = "团队已完成2个原型；项目拟在2027年开展3组验证。"
    claim["numeric_values"] = []
    checked = validator.apply("P-FACT-EXTRACT", env, output)
    assert {"QG_FACT_NOT_ATOMIC", "QG_FACT_NUMERIC_BINDING_MISSING"} <= _codes(checked)


def test_b3_identifier_numbers_do_not_require_fake_numeric_bindings():
    pack, validator = _runtime()
    identifier_claims = [
        "本材料仅用于阶段0运行基线验证。",
        "阶段0工作包处理规则抽取。",
        "工作流WF-1使用版本2.0契约。",
        "1. 输出必须结构化。",
        "（2）该条目用于说明结构。",
    ]
    for text in identifier_claims:
        env = pack.replay_input("P-FACT-EXTRACT")
        output = pack.replay_output("P-FACT-EXTRACT")
        claim = output["result"]["fact_candidates"][0]
        claim["claim_text"] = text
        claim["numeric_values"] = []
        checked = validator.apply("P-FACT-EXTRACT", env, output)
        assert "QG_FACT_NUMERIC_BINDING_MISSING" not in _codes(checked), text


def test_b3_substantive_numbers_still_require_numeric_bindings():
    pack, validator = _runtime()
    substantive_claims = [
        "团队已完成2个原型。",
        "项目拟在2027年开展验证。",
        "模型请求与响应配对率为100%。",
        "完整项目受理运行次数基线为0次。",
    ]
    for text in substantive_claims:
        env = pack.replay_input("P-FACT-EXTRACT")
        output = pack.replay_output("P-FACT-EXTRACT")
        claim = output["result"]["fact_candidates"][0]
        claim["claim_text"] = text
        claim["numeric_values"] = []
        checked = validator.apply("P-FACT-EXTRACT", env, output)
        assert "QG_FACT_NUMERIC_BINDING_MISSING" in _codes(checked), text


def test_b7_critic_findings_must_be_precise():
    pack, validator = _runtime()
    env = pack.replay_input("P-WRITE-CRITIC")
    output = pack.replay_output("P-WRITE-CRITIC")
    output["findings"] = [{
        "code": "VAGUE_FINDING",
        "severity": "P1",
        "category": "CONTENT",
        "target_type": "SECTION_CANDIDATE",
        "target_path_or_span": "",
        "description": "建议完善",
        "evidence_refs": [],
        "repairable": True,
        "repair_instruction": "优化",
        "suggested_route": "WRITING_AGENT",
        "blocking": True,
    }]
    checked = validator.apply("P-WRITE-CRITIC", env, output)
    assert "QG_CRITIC_FINDING_NOT_PRECISE" in _codes(checked)


def test_b7_targeted_repair_cannot_modify_protected_or_unlisted_paths():
    pack, validator = _runtime()
    env = pack.replay_input("P-TARGETED-REPAIR")
    output = pack.replay_output("P-TARGETED-REPAIR")
    output["result"]["changed_paths"] = ["forbidden.path"]
    checked = validator.apply("P-TARGETED-REPAIR", env, output)
    assert checked["status"] == "REVISE"
    assert "QG_REPAIR_PATH_OUTSIDE_ALLOWLIST" in _codes(checked)


def test_b8_expression_polish_preserves_structural_blocks():
    pack, validator = _runtime()
    env = pack.replay_input("P-EXPRESSION-POLISH")
    output = pack.replay_output("P-EXPRESSION-POLISH")
    source = env["payload"]["content_candidate"]
    source["candidate_text"] = source.get("candidate_text", "") + "\n[[TABLE]] 指标 | 数值"
    checked = validator.apply("P-EXPRESSION-POLISH", env, output)
    assert "QG_EXPRESSION_STRUCTURE_BLOCK_CHANGED" in _codes(checked)


def test_b9_conclusion_answers_all_questions_and_reuses_known_claims_only():
    pack, validator = _runtime()
    env = pack.replay_input("P-WRITE-CONTENT")
    output = pack.replay_output("P-WRITE-CONTENT")
    env["payload"]["section_profile"]["profile_id"] = "CONCLUSION"
    env["payload"]["argument_graph"] = {
        "central_proposition": {"node_id": "central-track-b"},
        "research_questions": [
            {"node_id": "rq-track-b-1"},
            {"node_id": "rq-track-b-2"},
        ],
        "nodes": [{"node_id": "known-contribution-track-b"}],
    }
    for paragraph in output["result"]["paragraphs"]:
        paragraph["primary_claim_id"] = "central-track-b"
    output["result"]["claim_advancement"]["advanced_claim_ids"] = [
        "central-track-b",
        "new-unproved-method-track-b",
    ]
    checked = validator.apply("P-WRITE-CONTENT", env, output)
    assert {
        "QG_CONCLUSION_QUESTIONS_UNANSWERED",
        "QG_CONCLUSION_INTRODUCES_NEW_CLAIM",
    } <= _codes(checked)
    assert pack.section_profile_for("结论")["profile_id"] == "CONCLUSION"


def test_b10_appendix_is_excluded_from_main_body_repetition_statistics():
    pack, validator = _runtime()
    env = pack.replay_input("P-INTEGRATION-CRITIC")
    output = pack.replay_output("P-INTEGRATION-CRITIC")
    seed = copy.deepcopy(env["payload"]["candidate_sections"][0]["candidate"])
    repeated = "该段落只用于验证主文与附录分区统计，不应因为附录重复而判定主文重复。"
    sections = []
    section_map = []
    contracts = []
    for section_id, placement in (("main-track-b", "MAIN_BODY"), ("appendix-track-b", "APPENDIX")):
        candidate = copy.deepcopy(seed)
        candidate["candidate_id"] = f"candidate-{section_id}"
        candidate["candidate_text"] = repeated
        candidate["paragraphs"] = [copy.deepcopy(candidate["paragraphs"][0])]
        candidate["paragraphs"][0]["paragraph_id"] = f"paragraph-{section_id}"
        candidate["paragraphs"][0]["text"] = repeated
        candidate["claim_advancement"]["new_information_keys"] = [f"information-{section_id}"]
        candidate["claim_advancement"]["advanced_claim_ids"] = [f"claim-{section_id}"]
        sections.append({"section_id": section_id, "candidate": candidate})
        section_map.append({
            "section_id": section_id,
            "title": section_id,
            "level": 1,
            "candidate_id": candidate["candidate_id"],
        })
        contracts.append({"section_id": section_id, "placement": placement})
    env["payload"]["candidate_sections"] = sections
    env["payload"]["document_section_map"] = section_map
    env["payload"]["narrative_architecture"] = {"section_contracts": contracts}
    checked = validator.apply("P-INTEGRATION-CRITIC", env, output)
    assert "QG_DOCUMENT_TEMPLATE_REPETITION" not in _codes(checked)
    assert pack.validate("P-INTEGRATION-CRITIC", "output", checked) == []


def test_b10_main_body_blocks_appendix_only_engineering_topics():
    pack, validator = _runtime()
    env = pack.replay_input("P-INTEGRATION-CRITIC")
    output = pack.replay_output("P-INTEGRATION-CRITIC")
    section = env["payload"]["candidate_sections"][0]
    section_id = section["section_id"]
    section["candidate"]["candidate_text"] += "\nDocker安装步骤和Trace审计日志如下。"
    env["payload"]["narrative_architecture"] = {
        "section_contracts": [{"section_id": section_id, "placement": "MAIN_BODY"}],
    }
    checked = validator.apply("P-INTEGRATION-CRITIC", env, output)
    assert "QG_MAIN_BODY_CONTAINS_APPENDIX_TOPIC" in _codes(checked)
