from __future__ import annotations

from app.proposal_quality import ProposalQualityGuard


def _contract(index: int, claim_ids: list[str]) -> dict:
    sid = f"sec-{index:02d}"
    return {
        "section_contract_id": f"contract-{index:02d}",
        "section_id": sid,
        "title": f"章节{index}",
        "profile_id": f"PROFILE_{index}",
        "argument_function": f"承担章节{index}的独立论证功能并形成可核验输出。",
        "must_advance_claim_ids": claim_ids,
        "must_use_evidence_ids": [],
        "unique_information_keys": [f"section-{index:02d}-unique"],
        "required_argument_roles": ["EVIDENCE"],
        "prerequisite_section_ids": [],
        "must_not_repeat_section_ids": [],
        "allowed_shared_context_ids": ["prop-001"],
        "forbidden_topics": [],
        "max_overlap_ratio": 0.12,
        "word_budget": 500,
        "placement": "MAIN_BODY",
        "acceptance_rules": ["独立推进命题", "绑定证据"],
    }


def _plan(claim_allocations: list[list[str]]) -> dict:
    contracts = [_contract(i + 1, claims) for i, claims in enumerate(claim_allocations)]
    return {
        "target_section_ids": [item["section_id"] for item in contracts],
        "tasks": [],
        "narrative_architecture": {
            "main_body_word_budget": 10000,
            "section_contracts": contracts,
        },
    }


def _codes(findings) -> set[str]:
    return {item.code for item in findings}


def test_plan_rejects_noncentral_claim_allocated_to_quarter_of_main_body():
    guard = ProposalQualityGuard()
    plan = _plan([["rq-001"] if i < 4 else [f"claim-{i:02d}"] for i in range(14)])
    findings = guard._audit_plan(plan, {"argument_graph": {"central_proposition": {"node_id": "prop-001"}}})
    assert "QG_PLAN_CLAIM_OVERCONCENTRATION" in _codes(findings)


def test_plan_allows_central_proposition_and_limited_claim_ownership():
    guard = ProposalQualityGuard()
    allocations = []
    for i in range(14):
        claims = ["prop-001"]
        if i in {1, 4, 8}:
            claims.append("rq-001")
        allocations.append(claims)
    plan = _plan(allocations)
    findings = guard._audit_plan(plan, {"argument_graph": {"central_proposition": {"node_id": "prop-001"}}})
    assert "QG_PLAN_CLAIM_OVERCONCENTRATION" not in _codes(findings)
