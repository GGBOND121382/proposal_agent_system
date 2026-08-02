from __future__ import annotations

import copy
from pathlib import Path

from app.executor import PromptExecutionError, PromptExecutor
from app.pack import PromptPack
from app.quality_guard import ensure_quality_guard_observer, validate_guard_report
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


def test_production_runtime_track_b_satisfies_observer_contract():
    pack, validator = _runtime()
    assert ensure_quality_guard_observer(validator) is validator
    executor = PromptExecutor(
        None,
        pack,
        None,
        None,
        quality_guard=validator,
        quality_guard_enabled=True,
    )
    assert executor.quality_guard is validator


def test_executor_rejects_apply_only_guard_at_startup():
    class LegacyApplyOnlyGuard:
        def apply(self, prompt_id, envelope, output):
            return output

    pack = PromptPack(ROOT / "prompt_pack")
    try:
        PromptExecutor(
            None,
            pack,
            None,
            None,
            quality_guard=LegacyApplyOnlyGuard(),
            quality_guard_enabled=True,
        )
    except PromptExecutionError as exc:
        assert "does not expose the non-mutating observe contract" in str(exc)
    else:
        raise AssertionError("apply-only guard must fail during executor construction")


def test_b1_scheme_extrapolation_cannot_be_mandatory():
    pack, validator = _runtime()
    env = pack.replay_input("P-SCHEME-EXTRACT")
    output = pack.replay_output("P-SCHEME-EXTRACT")
    rule = output["result"]["scheme_profile"]["rules"][0]
    rule["mandatory"] = True
    rule["source_refs"][0]["source_type"] = "MODEL_INFERENCE"
    report = validator.observe("P-SCHEME-EXTRACT", env, output)
    assert report["status"] == "REVISE"
    assert "QG_SCHEME_EXTRAPOLATION_AS_MANDATORY" in _codes(report)


def test_need_user_input_is_not_converted_to_block_by_model_p0_finding():
    pack, validator = _runtime()
    env = pack.replay_input("P-SCHEME-EXTRACT")
    output = pack.replay_output("P-SCHEME-EXTRACT")
    output["status"] = "NEED_USER_INPUT"
    output["findings"] = [{
        "code": "SCHEME_MISSING_MANDATORY_RULE",
        "severity": "P0",
        "category": "SCHEME",
        "target_type": "DOCUMENT",
        "target_path_or_span": "guide_documents",
        "description": "Formal guide is missing.",
        "evidence_refs": [],
        "repairable": True,
        "repair_instruction": "Ask the user for the guide.",
        "suggested_route": "USER",
        "blocking": True,
    }]

    report = validator.observe("P-SCHEME-EXTRACT", env, output)

    assert report["status"] == "PASS"
    assert report["model_status_observed"] == "NEED_USER_INPUT"
    assert output["status"] == "NEED_USER_INPUT"


def test_deterministic_findings_do_not_rewrite_model_status_or_verdict():
    pack, validator = _runtime()
    env = pack.replay_input("P-SCHEME-EXTRACT")
    output = pack.replay_output("P-SCHEME-EXTRACT")
    output["status"] = "NEED_USER_INPUT"
    output["result"]["verdict"] = "BLOCK"
    rule = output["result"]["scheme_profile"]["rules"][0]
    rule["mandatory"] = True
    rule["source_refs"][0]["source_type"] = "MODEL_INFERENCE"
    before = copy.deepcopy(output)

    report = validator.observe("P-SCHEME-EXTRACT", env, output)

    assert report["status"] == "REVISE"
    assert report["model_status_observed"] == "NEED_USER_INPUT"
    assert output == before


def test_repair_scope_accepts_generic_collection_wildcard():
    _, validator = _runtime()
    findings = validator._audit_repair_scope(
        {
            "allowed_paths": [
                "content.research_design_matrix[*].method_ids",
            ],
            "protected_paths": [],
            "findings_to_repair": [{
                "finding_instance_id": "finding-matrix-reference-001",
                "code": "MATRIX_REFERENCE_UNKNOWN",
            }],
            "original_object": {"content": {"research_design_matrix": []}},
        },
        {
            "changed_paths": [
                "content.research_design_matrix[0].method_ids",
                "content.research_design_matrix[3].method_ids",
            ],
            "resolved_finding_ids": ["finding-matrix-reference-001"],
            "unresolved_finding_ids": [],
        },
    )

    assert "QG_REPAIR_PATH_OUTSIDE_ALLOWLIST" not in {
        finding.code for finding in findings
    }


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
    report = validator.observe("P-PROJECT-DEFINITION-EXTRACT", env, output)
    assert "QG_PROJECT_RELATION_DIRECTION_INVALID" in _codes(report)


def test_b3_fact_records_must_be_atomic_and_numeric_values_bound():
    pack, validator = _runtime()
    env = pack.replay_input("P-FACT-EXTRACT")
    output = pack.replay_output("P-FACT-EXTRACT")
    claim = output["result"]["fact_candidates"][0]
    claim["claim_text"] = "团队已完成2个原型；项目拟在2027年开展3组验证。"
    claim["numeric_values"] = []
    report = validator.observe("P-FACT-EXTRACT", env, output)
    assert {"QG_FACT_NOT_ATOMIC", "QG_FACT_NUMERIC_BINDING_MISSING"} <= _codes(report)


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
        report = validator.observe("P-FACT-EXTRACT", env, output)
        assert "QG_FACT_NUMERIC_BINDING_MISSING" not in _codes(report), text


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
        report = validator.observe("P-FACT-EXTRACT", env, output)
        assert "QG_FACT_NUMERIC_BINDING_MISSING" in _codes(report), text


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
    report = validator.observe("P-WRITE-CRITIC", env, output)
    assert "QG_CRITIC_FINDING_NOT_PRECISE" in _codes(report)


def test_b7_targeted_repair_rejects_non_pointer_paths():
    pack, validator = _runtime()
    env = pack.replay_input("P-TARGETED-REPAIR")
    output = pack.replay_output("P-TARGETED-REPAIR")
    env["payload"]["allowed_paths"] = ["/content/text"]
    env["payload"]["protected_paths"] = []
    output["result"]["changed_paths"] = ["content.text"]

    report = validator.observe("P-TARGETED-REPAIR", env, output)

    assert report["status"] == "REVISE"
    assert "QG_REPAIR_PATH_INVALID" in _codes(report)


def test_b7_targeted_repair_allows_only_pointer_descendants():
    pack, validator = _runtime()
    env = pack.replay_input("P-TARGETED-REPAIR")
    output = pack.replay_output("P-TARGETED-REPAIR")
    env["payload"]["allowed_paths"] = ["/content/paragraphs/1"]
    env["payload"]["protected_paths"] = []
    output["result"]["changed_paths"] = [
        "/content/paragraphs/1/novel_content_key",
    ]

    report = validator.observe("P-TARGETED-REPAIR", env, output)

    assert "QG_REPAIR_PATH_INVALID" not in _codes(report)
    assert "QG_REPAIR_PATH_OUTSIDE_ALLOWLIST" not in _codes(report)


def test_b7_targeted_repair_pointer_ancestry_is_token_based():
    pack, validator = _runtime()
    env = pack.replay_input("P-TARGETED-REPAIR")
    output = pack.replay_output("P-TARGETED-REPAIR")
    env["payload"]["allowed_paths"] = ["/content/paragraphs/1"]
    env["payload"]["protected_paths"] = []
    output["result"]["changed_paths"] = [
        "/content/paragraphs/10/novel_content_key",
    ]

    report = validator.observe("P-TARGETED-REPAIR", env, output)

    assert "QG_REPAIR_PATH_OUTSIDE_ALLOWLIST" in _codes(report)


def test_b7_targeted_repair_cannot_replace_ancestor_of_protected_path():
    pack, validator = _runtime()
    env = pack.replay_input("P-TARGETED-REPAIR")
    output = pack.replay_output("P-TARGETED-REPAIR")
    env["payload"]["allowed_paths"] = ["/content/paragraphs/0"]
    env["payload"]["protected_paths"] = [
        "/content/paragraphs/0/paragraph_id",
    ]
    output["result"]["changed_paths"] = ["/content/paragraphs/0"]

    report = validator.observe("P-TARGETED-REPAIR", env, output)

    assert "QG_REPAIR_PATH_OUTSIDE_ALLOWLIST" in _codes(report)


def test_b8_expression_polish_preserves_structural_blocks():
    pack, validator = _runtime()
    env = pack.replay_input("P-EXPRESSION-POLISH")
    output = pack.replay_output("P-EXPRESSION-POLISH")
    source = env["payload"]["content_candidate"]
    source["candidate_text"] = source.get("candidate_text", "") + "\n[[TABLE]] 指标 | 数值"
    report = validator.observe("P-EXPRESSION-POLISH", env, output)
    assert "QG_EXPRESSION_STRUCTURE_BLOCK_CHANGED" in _codes(report)


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
    report = validator.observe("P-WRITE-CONTENT", env, output)
    assert {
        "QG_CONCLUSION_QUESTIONS_UNANSWERED",
        "QG_CONCLUSION_INTRODUCES_NEW_CLAIM",
    } <= _codes(report)
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
    report = validator.observe("P-INTEGRATION-CRITIC", env, output)
    assert "QG_DOCUMENT_TEMPLATE_REPETITION" not in _codes(report)
    assert validate_guard_report(
        report,
        prompt_id="P-INTEGRATION-CRITIC",
        model_output=output,
    ) == []


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
    report = validator.observe("P-INTEGRATION-CRITIC", env, output)
    assert "QG_MAIN_BODY_CONTAINS_APPENDIX_TOPIC" in _codes(report)
