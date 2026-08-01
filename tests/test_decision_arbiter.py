from __future__ import annotations

from copy import deepcopy

from app.contracts import get_semantic_contract
from app.executor import PromptExecutor
from app.proposal_quality import ProposalQualityGuard


def _invalid_blueprint_envelope() -> dict:
    return {
        "payload": {
            "section_contract": {
                "section_contract_id": "SC-001",
                "required_argument_roles": ["PROBLEM"],
                "must_advance_claim_ids": ["RQ-001"],
                "unique_information_keys": ["SC-001.unique_information_keys.0"],
            },
            "blueprint_candidate": {
                "paragraphs": [
                    {
                        "paragraph_id": "P-001",
                        "argument_role": "METHOD",
                        "primary_claim_id": "RQ-001",
                        "required_evidence_ids": ["RQ-001"],
                        "novel_content_key": "FOREIGN-KEY",
                        "project_item_slots": [],
                        "technical_slots": [],
                        "fact_slots": [],
                        "metric_slots": [],
                    }
                ]
            },
        }
    }


def test_guard_observation_preserves_raw_model_output() -> None:
    guard = ProposalQualityGuard()
    output = {
        "status": "PASS",
        "result": {"verdict": "ACCEPT"},
        "findings": [{"code": "MODEL-QUALITY", "description": "model finding"}],
        "warnings": [],
        "user_questions": [],
    }
    original = deepcopy(output)

    report = guard.observe("P-WRITE-BLUEPRINT-CRITIC", _invalid_blueprint_envelope(), output)

    assert output == original
    assert report["status"] == "REVISE"
    assert report["model_status_observed"] == "PASS"
    assert report["responsibility"] == "DETERMINISTIC_GUARD"
    assert report["contract_hash"] == get_semantic_contract().contract_hash
    assert report["findings"]
    assert all(item["source"] == "DETERMINISTIC_GUARD" for item in report["findings"])
    assert all(item["rule_id"] for item in report["findings"])


def test_legacy_guard_apply_is_non_mutating_and_does_not_merge_findings() -> None:
    guard = ProposalQualityGuard()
    output = {
        "status": "PASS",
        "result": {"verdict": "ACCEPT"},
        "findings": [],
        "warnings": [],
        "user_questions": [],
    }

    checked = guard.apply("P-WRITE-BLUEPRINT-CRITIC", _invalid_blueprint_envelope(), output)

    assert checked == output
    assert checked is not output
    assert checked["status"] == "PASS"
    assert checked["result"]["verdict"] == "ACCEPT"
    assert checked["findings"] == []


def test_executor_exposes_guard_report_as_a_separate_channel() -> None:
    executor = PromptExecutor.__new__(PromptExecutor)
    executor.quality_guard_enabled = True
    executor.quality_guard = ProposalQualityGuard()
    output = {
        "status": "PASS",
        "result": {"verdict": "ACCEPT"},
        "findings": [],
        "warnings": [],
        "user_questions": [],
    }
    original = deepcopy(output)

    report = executor._observe_guard(
        "P-WRITE-BLUEPRINT-CRITIC",
        _invalid_blueprint_envelope(),
        output,
    )

    assert output == original
    assert report is not None
    assert report["status"] == "REVISE"


def _pass_guard_report() -> dict:
    contract = get_semantic_contract()
    return {
        "schema_version": "1.0",
        "status": "PASS",
        "responsibility": "DETERMINISTIC_GUARD",
        "contract_version": contract.version,
        "contract_rule_registry_version": contract.rule_registry_version,
        "contract_hash": contract.contract_hash,
        "findings": [],
    }


def test_decision_record_preserves_immutable_source_snapshots_and_ownership() -> None:
    from app.decision_arbiter import DecisionArbiter

    critic = {
        "status": "PASS",
        "findings": [{"code": "C-QUALITY", "description": "qualitative observation"}],
    }
    guard = _pass_guard_report()
    record = DecisionArbiter().arbitrate(critic, guard, prompt_id="P-WRITE-BLUEPRINT-CRITIC")

    critic["status"] = "BLOCK"
    critic["findings"][0]["description"] = "mutated"
    guard["status"] = "BLOCK"

    payload = record.to_dict()
    assert payload["raw_critic_output"]["status"] == "PASS"
    assert payload["raw_critic_output"]["findings"][0]["description"] == "qualitative observation"
    assert payload["guard_report"]["status"] == "PASS"
    assert payload["contract"]["sha256"] == get_semantic_contract().contract_hash
    assert payload["responsibility_protocol"] == {
        "version": "1.0",
        "critic": "QUALITATIVE_LLM_CRITIC",
        "guard": "DETERMINISTIC_GUARD",
    }


def _guard_report(*findings: dict, status: str | None = None) -> dict:
    report = _pass_guard_report()
    report["findings"] = list(findings)
    report["status"] = status or ("REVISE" if findings else "PASS")
    return report


def test_arbitration_ignores_critic_restatement_of_deterministic_rule() -> None:
    from app.decision_arbiter import DecisionArbiter

    critic = {
        "status": "REVISE",
        "findings": [
            {
                "rule_id": "SC-ARGUMENT-ROLE-COMPATIBILITY",
                "code": "QG_BLUEPRINT_REQUIRED_ROLES_MISSING",
                "blocking": True,
                "description": "critic repeated a deterministic role check",
            }
        ],
        "user_questions": [],
    }

    record = DecisionArbiter().arbitrate(
        critic,
        _guard_report(),
        prompt_id="P-WRITE-BLUEPRINT-CRITIC",
    )

    assert record.decision == "PASS"
    assert record.contract_conflict is False
    basis = record.decision_basis
    assert basis["critic_owned_blocking_findings"] == []
    assert basis["ignored_critic_findings"][0]["reason"] == "OUTSIDE_QUALITATIVE_CRITIC_RESPONSIBILITY"


def test_arbitration_keeps_qualitative_critic_finding_actionable() -> None:
    from app.decision_arbiter import DecisionArbiter

    critic_finding = {
        "code": "CRITIC_ARGUMENT_CHAIN_WEAK",
        "category": "ARGUMENT_CHAIN",
        "blocking": True,
        "description": "paragraphs do not form a causal argument",
    }
    record = DecisionArbiter().arbitrate(
        {"status": "REVISE", "findings": [critic_finding], "user_questions": []},
        _guard_report(),
        prompt_id="P-WRITE-BLUEPRINT-CRITIC",
    )

    assert record.decision == "REVISE"
    assert record.decision_basis["actionable_findings"] == [
        {"source": "QUALITATIVE_LLM_CRITIC", "finding": critic_finding}
    ]


def test_arbitration_applies_guard_blocker_when_critic_passes() -> None:
    from app.decision_arbiter import DecisionArbiter

    guard_finding = {
        "rule_id": "SC-EVIDENCE-SELF-REFERENCE",
        "responsibility": "DETERMINISTIC_GUARD",
        "source": "DETERMINISTIC_GUARD",
        "code": "QG_BLUEPRINT_SELF_EVIDENCE",
        "blocking": True,
        "severity": "P1",
    }
    record = DecisionArbiter().arbitrate(
        {"status": "PASS", "findings": [], "user_questions": []},
        _guard_report(guard_finding),
        prompt_id="P-WRITE-BLUEPRINT-CRITIC",
    )

    assert record.decision == "REVISE"
    assert record.contract_conflict is False
    assert record.decision_basis["actionable_findings"][0]["source"] == "DETERMINISTIC_GUARD"


def test_contract_conflict_only_reports_identity_or_responsibility_mismatch() -> None:
    from app.decision_arbiter import DecisionArbiter

    guard = _guard_report()
    guard["contract_hash"] = "0" * 64
    record = DecisionArbiter().arbitrate(
        {"status": "PASS", "findings": [], "user_questions": []},
        guard,
        prompt_id="P-WRITE-BLUEPRINT-CRITIC",
    )

    assert record.decision == "CONTRACT_CONFLICT"
    assert record.contract_conflict is True
    assert "contract_hash" in record.decision_basis["protocol_errors"][0]


def test_effective_result_uses_arbiter_decision_without_mutating_raw_output() -> None:
    from app.decision_arbiter import DecisionArbiter
    from app.workflows import WorkflowEngine

    raw = {
        "status": "PASS",
        "findings": [],
        "result": {"verdict": "ACCEPT"},
    }
    guard_finding = {
        "rule_id": "SC-EVIDENCE-SELF-REFERENCE",
        "responsibility": "DETERMINISTIC_GUARD",
        "source": "DETERMINISTIC_GUARD",
        "code": "QG_BLUEPRINT_SELF_EVIDENCE",
        "severity": "P1",
        "category": "EVIDENCE",
        "target_type": "BLUEPRINT",
        "target_path_or_span": "paragraphs.required_evidence_ids",
        "description": "self evidence",
        "evidence_refs": [],
        "repairable": True,
        "repair_instruction": "remove self reference",
        "suggested_route": "WRITING_AGENT",
        "blocking": True,
    }
    decision = DecisionArbiter().arbitrate(
        raw,
        _guard_report(guard_finding),
        prompt_id="P-WRITE-BLUEPRINT-CRITIC",
    ).to_dict()

    status, derived = WorkflowEngine._effective_critic_result(
        {"status": "PASS", "output": raw}, decision
    )

    assert status == "REVISE"
    assert raw["status"] == "PASS"
    assert raw["findings"] == []
    assert derived["status"] == "REVISE"
    assert derived["findings"][0]["code"] == "QG_BLUEPRINT_SELF_EVIDENCE"
    assert "rule_id" not in derived["findings"][0]
