from __future__ import annotations

from app.contracts.semantic_checks import check_blueprint_semantics
from app.contracts.semantic_contract import get_semantic_contract
from app.simulated_llm import SimulatedLLM


def _foundation_payload() -> dict:
    evidence_ids = [f"foundation-evidence-{index}" for index in range(1, 6)]
    graph = {
        "central_proposition": {
            "node_id": "claim-foundation-capability",
            "node_type": "CENTRAL_PROPOSITION",
            "statement": "既有研究成果能够支撑本项目的关键任务",
        },
        "research_questions": [],
        "nodes": [
            {
                "node_id": evidence_id,
                "node_type": "FOUNDATION_EVIDENCE",
                "statement": f"前期成果证据{index}",
            }
            for index, evidence_id in enumerate(evidence_ids, 1)
        ],
    }
    return {
        "source_section": {"section_id": "section-foundation", "title": "研究基础"},
        "section_profile": {"profile_id": "RESEARCH_FOUNDATION"},
        "section_contract": {
            "section_contract_id": "contract-foundation",
            "argument_function": "说明前期成果如何支撑本项目任务，并如实界定能力边界",
            "required_argument_roles": ["EVIDENCE", "WARRANT", "BOUNDARY"],
            "must_advance_claim_ids": ["claim-foundation-capability"],
            "must_use_evidence_ids": evidence_ids,
            "unique_information_keys": ["foundation-capability"],
            "must_not_repeat_section_ids": [],
            "word_budget": 600,
        },
        "argument_graph": graph,
        "confirmed_facts": [],
        "prior_section_digest": [],
        "metric_inputs": [],
    }


def test_semantic_contract_registers_required_evidence_coverage() -> None:
    contract = get_semantic_contract()
    rule = contract.rule("SC-EVIDENCE-CONTRACT-COVERAGE")

    assert rule.responsibility.value == "DETERMINISTIC_GUARD"
    assert contract.required_evidence_contract_field == "must_use_evidence_ids"


def test_blueprint_semantics_rejects_missing_contract_evidence() -> None:
    payload = _foundation_payload()
    blueprint = {
        "paragraphs": [
            {
                "paragraph_id": "bp-1",
                "argument_role": "EVIDENCE",
                "primary_claim_id": "claim-foundation-capability",
                "required_evidence_ids": ["foundation-evidence-1"],
                "project_item_slots": [],
                "technical_slots": [],
                "novel_content_key": "foundation-capability:evidence",
            },
            {
                "paragraph_id": "bp-2",
                "argument_role": "WARRANT",
                "primary_claim_id": "claim-foundation-capability",
                "required_evidence_ids": [],
                "project_item_slots": [],
                "technical_slots": [],
                "novel_content_key": "foundation-capability:warrant",
            },
            {
                "paragraph_id": "bp-3",
                "argument_role": "BOUNDARY",
                "primary_claim_id": "claim-foundation-capability",
                "required_evidence_ids": [],
                "project_item_slots": [],
                "technical_slots": [],
                "novel_content_key": "foundation-capability:boundary",
            },
        ]
    }

    violations = check_blueprint_semantics(blueprint, payload)

    missing = next(
        item for item in violations if item.code == "QG_BLUEPRINT_REQUIRED_EVIDENCE_MISSING"
    )
    assert missing.rule_id == "SC-EVIDENCE-CONTRACT-COVERAGE"
    assert "foundation-evidence-5" in missing.description


def test_simulated_blueprint_and_content_bind_all_contract_evidence() -> None:
    payload = _foundation_payload()
    simulator = SimulatedLLM(object())
    blueprint_output = simulator._handle_write_blueprint(
        {"status": "PASS", "result": {"blueprint": {}}, "findings": []},
        {"payload": payload},
    )

    assert blueprint_output["status"] == "PASS", blueprint_output.get("findings")
    blueprint = blueprint_output["result"]["blueprint"]
    required = set(payload["section_contract"]["must_use_evidence_ids"])
    blueprint_bound = {
        evidence_id
        for paragraph in blueprint["paragraphs"]
        for evidence_id in paragraph.get("required_evidence_ids") or []
    }
    assert required <= blueprint_bound

    content_payload = dict(payload)
    content_payload["approved_blueprint"] = blueprint
    content_output = simulator._handle_write_content(
        {"status": "PASS", "result": {}, "findings": []},
        {"payload": content_payload},
    )
    content_bound = {
        evidence_id
        for paragraph in content_output["result"]["paragraphs"]
        for evidence_id in paragraph.get("evidence_ids") or []
    }
    assert required <= content_bound
    assert all(
        paragraph["primary_claim_id"] not in paragraph.get("evidence_ids", [])
        for paragraph in content_output["result"]["paragraphs"]
    )

class _FoundationProfilePack:
    @staticmethod
    def section_profile_for(title: str) -> dict:
        assert title == "研究基础"
        return {
            "profile_id": "RESEARCH_FOUNDATION",
            "acceptance_rules": [
                "每项能力由可定位前期成果支撑",
                "说明证据与具体任务之间的支撑关系",
            ],
        }


def test_foundation_narrative_contract_separates_task_claims_from_team_evidence() -> None:
    graph = {
        "central_proposition": {
            "node_id": "prop-001",
            "statement": "前期能力能够支撑项目任务",
        },
        "research_questions": [],
        "nodes": [
            {"node_id": "wp-001", "node_type": "WORK_PACKAGE"},
            {"node_id": "wp-002", "node_type": "WORK_PACKAGE"},
            {
                "node_id": "foundation-001",
                "node_type": "TEAM_EVIDENCE",
                "status": "SUPPORTED",
                "source_refs": [{"source_id": "evidence-doc-001"}],
            },
        ],
    }
    simulator = SimulatedLLM(_FoundationProfilePack())

    architecture = simulator._narrative_architecture({
        "payload": {
            "argument_graph": graph,
            "linked_sections": [
                {"section_id": "section-foundation", "title": "研究基础"}
            ],
        }
    })
    contract = architecture["section_contracts"][0]

    assert contract["must_advance_claim_ids"] == ["wp-001", "wp-002"]
    assert contract["must_use_evidence_ids"] == ["foundation-001"]
    assert not (
        set(contract["must_advance_claim_ids"])
        & set(contract["must_use_evidence_ids"])
    )

