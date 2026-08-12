from __future__ import annotations

from app.contracts import get_semantic_contract
from app.executor import PromptExecutor
from app.proposal_quality import ProposalQualityGuard


def test_role_compatibility_is_shared_and_symmetric_for_problem_question() -> None:
    contract = get_semantic_contract()
    assert contract.canonical_role("PROBLEM_SUMMARY") == "PROBLEM"
    assert contract.missing_required_roles(["PROBLEM"], ["RESEARCH_QUESTION"]) == []
    assert contract.missing_required_roles(["RESEARCH_QUESTION"], ["PROBLEM"]) == []


def test_information_key_grammar_accepts_only_declared_root_or_colon_child() -> None:
    contract = get_semantic_contract()
    assert contract.information_key_belongs("K-001", ["K-001"])
    assert contract.information_key_belongs("K-001:detail", ["K-001"])
    assert not contract.information_key_belongs("K-001-detail", ["K-001"])
    assert not contract.information_key_belongs("OTHER:detail", ["K-001"])


def test_claim_coverage_is_not_primary_claim_only() -> None:
    contract = get_semantic_contract()
    paragraph = {
        "primary_claim_id": "CLAIM-1",
        "project_item_slots": ["CLAIM-2"],
        "technical_slots": ["CLAIM-3"],
        "fact_slots": ["FACT-1"],
    }
    assert contract.claim_coverage_ids(paragraph) == {"CLAIM-1", "CLAIM-2", "CLAIM-3"}


def test_self_evidence_is_rejected_by_deterministic_guard() -> None:
    guard = ProposalQualityGuard()
    envelope = {
        "payload": {
            "section_profile": {"profile_id": "ABSTRACT"},
            "section_contract": {
                "section_contract_id": "SC-1",
                "unique_information_keys": ["K-1"],
                "required_argument_roles": ["CENTRAL_CLAIM"],
                "must_advance_claim_ids": ["CLAIM-1"],
            },
            "prior_section_digest": [],
            "source_section": {"title": "摘要"},
        }
    }
    output = {
        "status": "PASS",
        "result": {
            "blueprint": {
                "paragraphs": [
                    {
                        "paragraph_id": "P-1",
                        "argument_role": "CENTRAL_CLAIM",
                        "primary_claim_id": "CLAIM-1",
                        "project_item_slots": [],
                        "technical_slots": [],
                        "fact_slots": [],
                        "metric_slots": [],
                        "required_evidence_ids": ["CLAIM-1"],
                        "novel_content_key": "K-1:central",
                        "function": "提出中心命题",
                    }
                ]
            }
        },
        "findings": [],
        "unresolved_items": [],
        "user_questions": [],
        "source_refs": [],
        "warnings": [],
    }
    report = guard.observe("P-WRITE-BLUEPRINT", envelope, output)
    assert output["status"] == "PASS"
    assert output["findings"] == []
    assert report["status"] == "REVISE"
    assert any(
        item["code"] == "QG_BLUEPRINT_SELF_EVIDENCE"
        for item in report["findings"]
    )


def test_system_prompt_does_not_duplicate_deterministic_semantic_contract(tmp_path) -> None:
    # The semantic contract remains executable code; the provider prompt gets
    # only a compact boundary instead of a second copy of validator rules.
    executor = PromptExecutor.__new__(PromptExecutor)
    class Pack:
        shared_prompt = "shared"
        def prompt_text(self, prompt_id: str) -> str:
            return prompt_id
    executor.pack = Pack()
    prompt = executor._system_prompt(
        "P-WRITE-BLUEPRINT",
        {"type": "object", "properties": {}},
        {"payload": {}, "task": {}, "scope": {}, "security_context": {}, "freshness": {}},
    )
    assert "# 运行时契约边界" in prompt
    assert "统一语义契约（运行时生成）" not in prompt
    assert "A claim must never cite itself as evidence" not in prompt
    assert get_semantic_contract().rule("SC-EVIDENCE-SELF-REFERENCE") is not None


def test_machine_rules_have_stable_ids_and_explicit_owners() -> None:
    from app.contracts.semantic_contract import RuleResponsibility

    contract = get_semantic_contract()
    assert contract.rule_registry_version == "1.3.0"
    assert contract.allows_input_object("required_input_ids")
    assert contract.allows_input_object("evidence_refs")
    assert contract.registered_reference_suffixes("unresolved_slot_ids") == (
        "UNKNOWN",
        "ABSENT-FACT",
    )
    assert contract.rule_ids == {
        "SC-ARGUMENT-ROLE-COMPATIBILITY",
        "SC-INFORMATION-KEY-HIERARCHY",
        "SC-CLAIM-COVERAGE",
        "SC-EVIDENCE-SELF-REFERENCE",
        "SC-EVIDENCE-CONTRACT-COVERAGE",
        "SC-REFERENCE-FIELD-SEMANTICS",
    }
    assert {
        rule.rule_id
        for rule in contract.rules_for(RuleResponsibility.DETERMINISTIC_GUARD)
    } == {
        "SC-ARGUMENT-ROLE-COMPATIBILITY",
        "SC-INFORMATION-KEY-HIERARCHY",
        "SC-CLAIM-COVERAGE",
        "SC-EVIDENCE-SELF-REFERENCE",
        "SC-EVIDENCE-CONTRACT-COVERAGE",
    }
    assert contract.rule("SC-REFERENCE-FIELD-SEMANTICS").responsibility is (
        RuleResponsibility.OUTPUT_INTEGRITY
    )
