from __future__ import annotations

import copy

from app.contracts.semantic_checks import check_blueprint_semantics
from app.proposal_quality import ProposalQualityGuard


def _payload() -> dict:
    return {
        "section_profile": {"profile_id": "ABSTRACT"},
        "source_section": {"title": "摘要"},
        "section_contract": {
            "section_contract_id": "SC-1",
            "unique_information_keys": ["K-1", "K-2"],
            "required_argument_roles": ["PROBLEM", "METHOD"],
            "must_advance_claim_ids": ["CLAIM-1", "CLAIM-2"],
        },
        "prior_section_digest": [{"new_information_keys": ["K-2:used"]}],
    }


def _valid_blueprint() -> dict:
    return {
        "paragraphs": [
            {
                "paragraph_id": "P-1",
                "argument_role": "RESEARCH_QUESTION",
                "primary_claim_id": "CLAIM-1",
                "project_item_slots": [],
                "technical_slots": [],
                "required_evidence_ids": ["FACT-1"],
                "fact_slots": ["FACT-1"],
                "metric_slots": [],
                "novel_content_key": "K-1:problem",
                "function": "提出问题",
            },
            {
                "paragraph_id": "P-2",
                "argument_role": "METHOD",
                "primary_claim_id": "CLAIM-3",
                "project_item_slots": ["CLAIM-2"],
                "technical_slots": [],
                "required_evidence_ids": ["FACT-2"],
                "fact_slots": ["FACT-2"],
                "metric_slots": [],
                "novel_content_key": "K-2:method",
                "function": "说明方法",
            },
        ]
    }


def test_deterministic_blueprint_checker_accepts_contract_defined_equivalences() -> None:
    assert check_blueprint_semantics(_valid_blueprint(), _payload()) == ()


def test_deterministic_blueprint_checker_emits_registered_rule_ids() -> None:
    payload = _payload()
    blueprint = {
        "paragraphs": [
            {
                "paragraph_id": "P-1",
                "argument_role": "CONTEXT",
                "primary_claim_id": "CLAIM-1",
                "project_item_slots": [],
                "technical_slots": [],
                "required_evidence_ids": ["CLAIM-1"],
                "fact_slots": [],
                "metric_slots": [],
                "novel_content_key": "OTHER:key",
                "function": "背景",
            },
            {
                "paragraph_id": "P-2",
                "argument_role": "CONTEXT",
                "primary_claim_id": "CLAIM-4",
                "project_item_slots": [],
                "technical_slots": [],
                "required_evidence_ids": [],
                "fact_slots": [],
                "metric_slots": [],
                "novel_content_key": "K-2:used",
                "function": "背景补充",
            },
        ]
    }
    violations = check_blueprint_semantics(blueprint, payload)
    assert {item.rule_id for item in violations} == {
        "SC-ARGUMENT-ROLE-COMPATIBILITY",
        "SC-INFORMATION-KEY-HIERARCHY",
        "SC-CLAIM-COVERAGE",
        "SC-EVIDENCE-SELF-REFERENCE",
    }
    assert {item.code for item in violations} >= {
        "QG_BLUEPRINT_INFORMATION_KEY_OUTSIDE_CONTRACT",
        "QG_BLUEPRINT_REUSES_PRIOR_INFORMATION",
        "QG_BLUEPRINT_REQUIRED_ROLES_MISSING",
        "QG_BLUEPRINT_SELF_EVIDENCE",
        "QG_BLUEPRINT_REQUIRED_CLAIMS_MISSING",
    }
    paths_by_code = {item.code: item.target_path for item in violations}
    assert paths_by_code["QG_BLUEPRINT_INFORMATION_KEY_OUTSIDE_CONTRACT"] == (
        "paragraphs[P-1].novel_content_key"
    )
    assert paths_by_code["QG_BLUEPRINT_REUSES_PRIOR_INFORMATION"] == (
        "paragraphs[P-2].novel_content_key"
    )
    assert paths_by_code["QG_BLUEPRINT_SELF_EVIDENCE"] == (
        "paragraphs[P-1].required_evidence_ids"
    )
    assert paths_by_code["QG_BLUEPRINT_REQUIRED_ROLES_MISSING"] == "paragraphs"
    assert paths_by_code["QG_BLUEPRINT_REQUIRED_CLAIMS_MISSING"] == "paragraphs"


def test_deterministic_blueprint_checker_does_not_mutate_inputs() -> None:
    payload = _payload()
    blueprint = _valid_blueprint()
    before_payload = copy.deepcopy(payload)
    before_blueprint = copy.deepcopy(blueprint)
    check_blueprint_semantics(blueprint, payload)
    assert payload == before_payload
    assert blueprint == before_blueprint


def test_self_evidence_finding_targets_the_actual_evidence_field() -> None:
    blueprint = _valid_blueprint()
    blueprint["paragraphs"][0]["fact_slots"] = ["CLAIM-1"]
    blueprint["paragraphs"][0]["required_evidence_ids"] = ["FACT-1"]

    violations = check_blueprint_semantics(blueprint, _payload())
    self_evidence = [
        item for item in violations
        if item.code == "QG_BLUEPRINT_SELF_EVIDENCE"
    ]

    assert [item.target_path for item in self_evidence] == [
        "paragraphs[P-1].fact_slots"
    ]


def test_quality_guard_delegates_deterministic_blueprint_findings() -> None:
    payload = _payload()
    blueprint = _valid_blueprint()
    blueprint["paragraphs"][0]["required_evidence_ids"] = ["CLAIM-1"]
    expected_codes = {item.code for item in check_blueprint_semantics(blueprint, payload)}
    output = {
        "status": "PASS",
        "result": {"blueprint": blueprint},
        "findings": [],
        "unresolved_items": [],
        "user_questions": [],
        "source_refs": [],
        "warnings": [],
    }
    report = ProposalQualityGuard().observe(
        "P-WRITE-BLUEPRINT", {"payload": payload}, output
    )
    observed_codes = {item["code"] for item in report["findings"]}
    assert expected_codes <= observed_codes
    assert output["status"] == "PASS"
    assert output["findings"] == []


def test_producer_prompt_uses_shared_contract_without_local_counter_rules() -> None:
    from pathlib import Path

    prompt = Path("prompt_pack/prompts/writing/write_blueprint.md").read_text(encoding="utf-8")
    assert "primary_claim_id`、`project_item_slots`和`technical_slots`的并集" in prompt
    assert "不得把所有待覆盖命题强塞进单值`primary_claim_id`" in prompt
    assert "任何段落都不得把自己的`primary_claim_id`作为证据" in prompt
    assert "`根键:子键`" in prompt
    assert "共享合同的兼容关系" in prompt


def test_producer_schema_and_replays_share_prompt_version() -> None:
    import json
    from pathlib import Path

    input_schema = json.loads(
        Path("prompt_pack/schemas/prompts/write_blueprint_input.schema.json").read_text(
            encoding="utf-8"
        )
    )
    output_schema = json.loads(
        Path("prompt_pack/schemas/prompts/write_blueprint_output.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert input_schema["properties"]["prompt_version"] == {"const": "3.2.0"}
    assert output_schema["properties"]["prompt_version"] == {"const": "3.2.0"}

    for path in Path("prompt_pack/replay/cases/write_blueprint").glob("*.json"):
        case = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(case.get("input"), dict):
            assert case["input"]["prompt_version"] == "3.2.0"
        if isinstance(case.get("expected_output"), dict):
            assert case["expected_output"]["prompt_version"] == "3.2.0"


def test_producer_normal_replay_satisfies_deterministic_contract() -> None:
    import json
    from pathlib import Path

    case = json.loads(
        Path("prompt_pack/replay/cases/write_blueprint/normal.json").read_text(
            encoding="utf-8"
        )
    )
    payload = case["input"]["payload"]
    blueprint = case["expected_output"]["result"]["blueprint"]
    assert check_blueprint_semantics(blueprint, payload) == ()

    paragraphs = blueprint["paragraphs"]
    covered = {
        value
        for paragraph in paragraphs
        for field in ("primary_claim_id", "project_item_slots", "technical_slots")
        for value in (
            [paragraph[field]]
            if field == "primary_claim_id"
            else paragraph.get(field, [])
        )
    }
    assert set(payload["section_contract"]["must_advance_claim_ids"]) <= covered
    assert all(
        paragraph["primary_claim_id"] not in paragraph["required_evidence_ids"]
        for paragraph in paragraphs
    )


def test_builder_declares_v3_blueprint_contract_from_authoritative_sources() -> None:
    from pathlib import Path

    builder = Path("prompt_pack/tools/build_v2.py").read_text(encoding="utf-8")
    assert "'P-WRITE-BLUEPRINT': '3.2.0'" in builder
    assert "'proposal_contract','argument_graph','narrative_architecture','section_contract','prior_section_digest','revision_findings'" in builder
    assert "'argument_role':enum([" in builder
    assert "'required_evidence_ids':arr(idstr())" in builder
    assert "'novel_content_key':s(8)" in builder
    assert "payload':obj(payload_props, required=fields)" in builder


def test_critic_prompt_excludes_deterministic_guard_responsibilities() -> None:
    from pathlib import Path

    prompt = Path("prompt_pack/prompts/writing/write_blueprint_critic.md").read_text(
        encoding="utf-8"
    )
    assert "不得重新执行确定性Guard的机器规则" in prompt
    assert "ID是否存在、角色是否满足合同、信息键是否属于合同或重复" in prompt
    assert "不得据此生成阻断Finding或改变verdict" in prompt
    assert "由独立`guard_report`记录" in prompt
    assert "`ARGUMENT_ROLE_MISSING`" not in prompt
    assert "`WORD_BUDGET_INVALID`" not in prompt


def test_critic_schema_contains_only_qualitative_argument_dimensions() -> None:
    import json
    from pathlib import Path

    schema = json.loads(
        Path(
            "prompt_pack/schemas/prompts/write_blueprint_critic_output.schema.json"
        ).read_text(encoding="utf-8")
    )
    result = schema["properties"]["result"]
    dimensions = result["properties"]["argument_checks"]["items"]["properties"][
        "dimension"
    ]["enum"]
    assert dimensions == [
        "SECTION_FUNCTION",
        "CLAIM_ADVANCEMENT_QUALITY",
        "EVIDENCE_SUFFICIENCY",
        "PARAGRAPH_RELATIONSHIP",
        "NO_GENERIC_SIX_PART_TEMPLATE",
    ]
    assert "NOVEL_CONTENT_KEYS" not in dimensions
    assert "WORD_BUDGET" not in dimensions
    for field in (
        "uncovered_revision_task_ids",
        "invalid_slot_refs",
        "critical_unresolved_slot_ids",
    ):
        assert "独立guard_report" in result["properties"][field]["description"]


def test_critic_normal_replay_checks_every_valid_blueprint_paragraph() -> None:
    import json
    from pathlib import Path

    case = json.loads(
        Path("prompt_pack/replay/cases/write_blueprint_critic/normal.json").read_text(
            encoding="utf-8"
        )
    )
    payload = case["input"]["payload"]
    blueprint = payload["blueprint_candidate"]
    assert check_blueprint_semantics(blueprint, payload) == ()
    assert case["input"]["prompt_version"] == "3.2.0"
    result = case["expected_output"]["result"]
    assert result["checked_paragraph_ids"] == [
        paragraph["paragraph_id"] for paragraph in blueprint["paragraphs"]
    ]
    assert result["invalid_slot_refs"] == []
    assert all(item["passed"] for item in result["argument_checks"])


def test_simulated_producer_reuses_deterministic_contract_without_self_repair() -> None:
    import json
    from pathlib import Path

    from app.pack import PromptPack
    from app.simulated_llm import SimulatedLLM

    root = Path(__file__).resolve().parents[1]
    pack = PromptPack(root / "prompt_pack")
    case = json.loads(
        (root / "prompt_pack/replay/cases/write_blueprint/normal.json").read_text(
            encoding="utf-8"
        )
    )
    output = SimulatedLLM(pack).invoke("P-WRITE-BLUEPRINT", case["input"])
    blueprint = output["result"]["blueprint"]

    assert output["status"] == "PASS"
    assert pack.validate("P-WRITE-BLUEPRINT", "output", output) == []
    assert check_blueprint_semantics(blueprint, case["input"]["payload"]) == ()
    assert all(
        paragraph["primary_claim_id"] not in paragraph["required_evidence_ids"]
        for paragraph in blueprint["paragraphs"]
    )
    assert all(":" in paragraph["novel_content_key"] for paragraph in blueprint["paragraphs"])


def test_simulated_critic_uses_only_qualitative_dimensions() -> None:
    import json
    from pathlib import Path

    from app.pack import PromptPack
    from app.simulated_llm import SimulatedLLM

    root = Path(__file__).resolve().parents[1]
    pack = PromptPack(root / "prompt_pack")
    case = json.loads(
        (
            root
            / "prompt_pack/replay/cases/write_blueprint_critic/normal.json"
        ).read_text(encoding="utf-8")
    )
    output = SimulatedLLM(pack).invoke("P-WRITE-BLUEPRINT-CRITIC", case["input"])
    assert output["status"] == "PASS"
    assert pack.validate("P-WRITE-BLUEPRINT-CRITIC", "output", output) == []
    assert output["result"]["invalid_slot_refs"] == []
    assert [item["dimension"] for item in output["result"]["argument_checks"]] == [
        "SECTION_FUNCTION",
        "CLAIM_ADVANCEMENT_QUALITY",
        "EVIDENCE_SUFFICIENCY",
        "PARAGRAPH_RELATIONSHIP",
        "NO_GENERIC_SIX_PART_TEMPLATE",
    ]
