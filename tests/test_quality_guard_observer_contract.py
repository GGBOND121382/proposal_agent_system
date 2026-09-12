from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.decision_arbiter import DecisionArbiter
from app.executor import PromptExecutionError, PromptExecutor
from app.pack import PromptPack
from app.quality_guard import (
    QualityGuardContractError,
    build_guard_report,
    disabled_guard_report,
    require_guard_report,
    validate_guard_report,
)
from app.track_b import TrackBAgentPromptValidator
from app.workflows import WorkflowEngine


ROOT = Path(__file__).resolve().parents[1]


def _invalid_blueprint(pack: PromptPack):
    envelope = pack.replay_input("P-WRITE-BLUEPRINT")
    output = pack.replay_output("P-WRITE-BLUEPRINT")
    paragraph = output["result"]["blueprint"]["paragraphs"][0]
    paragraph["information_keys"] = ["outside-contract-key"]
    paragraph["required_evidence_ids"] = [paragraph["primary_claim_id"]]
    return envelope, output


def test_track_b_composes_base_and_track_findings_without_mutation():
    pack = PromptPack(ROOT / "prompt_pack")
    guard = TrackBAgentPromptValidator(pack)
    envelope, output = _invalid_blueprint(pack)
    envelope["payload"]["section_profile"]["profile_id"] = "CONCLUSION"
    envelope["payload"]["argument_graph"] = {
        "central_proposition": {"node_id": "central-guard-test"},
        "research_questions": [{"node_id": "rq-guard-test"}],
        "nodes": [],
    }
    before = copy.deepcopy(output)

    report = guard.observe("P-WRITE-BLUEPRINT", envelope, output)

    assert output == before
    assert report["status"] == "REVISE"
    assert len(report["components"]) == 2
    assert report["components"][0]["observer"] == "FullProposalQualityGuard"
    codes = {item["code"] for item in report["findings"]}
    assert "QG_BLUEPRINT_SELF_EVIDENCE" in codes
    assert "QG_CONCLUSION_QUESTIONS_UNANSWERED" in codes
    assert validate_guard_report(
        report,
        prompt_id="P-WRITE-BLUEPRINT",
        model_output=output,
    ) == []


def test_track_b_legacy_apply_is_non_mutating_and_not_a_decision_channel():
    pack = PromptPack(ROOT / "prompt_pack")
    guard = TrackBAgentPromptValidator(pack)
    envelope, output = _invalid_blueprint(pack)
    before = copy.deepcopy(output)

    copied = guard.apply("P-WRITE-BLUEPRINT", envelope, output)

    assert output == before
    assert copied == before
    assert copied is not output
    assert copied["status"] == output["status"]
    assert copied["findings"] == output["findings"]


def test_disabled_guard_has_explicit_valid_report():
    output = {"status": "PASS", "findings": [], "result": {}}
    report = disabled_guard_report("P-WRITE-CRITIC", output)

    assert report["observation_status"] == "DISABLED"
    assert report["status"] == "PASS"
    assert validate_guard_report(
        report,
        prompt_id="P-WRITE-CRITIC",
        model_output=output,
        expected_observation_status="DISABLED",
    ) == []


def test_enabled_guard_missing_report_fails_closed_before_arbitration():
    engine = object.__new__(WorkflowEngine)
    engine.executor = SimpleNamespace(quality_guard_enabled=True)
    wf = {"id": "wf-test", "project_id": "project-test", "current_step": 1, "status": "RUNNING"}
    result = {
        "status": "PASS",
        "output": {
            "status": "PASS",
            "result": {"verdict": "ACCEPT"},
            "findings": [],
            "user_questions": [],
        },
        "quality_guard_enabled": True,
    }

    with pytest.raises(PromptExecutionError, match="has no guard_report"):
        engine._record_decision(wf, {}, "P-WRITE-CRITIC", result)


def test_require_guard_report_rejects_enabled_disabled_mismatch():
    output = {"status": "PASS", "findings": [], "result": {}}
    report = disabled_guard_report("P-WRITE-CRITIC", output)
    with pytest.raises(QualityGuardContractError, match="observation_status"):
        require_guard_report(
            {
                "quality_guard_enabled": True,
                "guard_report": report,
            },
            prompt_id="P-WRITE-CRITIC",
            model_output=output,
            default_guard_enabled=True,
        )


def test_executor_rejects_observer_that_mutates_its_private_input_at_execution_boundary():
    class MutatingObserver:
        def observe(self, prompt_id, envelope, output):
            output["status"] = "REVISE"
            return disabled_guard_report(prompt_id, output)

    pack = PromptPack(ROOT / "prompt_pack")
    executor = PromptExecutor(
        None,
        pack,
        None,
        None,
        quality_guard=MutatingObserver(),
        quality_guard_enabled=True,
    )
    output = {"status": "PASS", "findings": [], "result": {}}

    with pytest.raises(PromptExecutionError, match="mutated the model output"):
        executor._observe_guard("P-WRITE-CRITIC", {"payload": {}}, output)


def test_executor_rejects_observer_with_wrong_signature_at_construction():
    class WrongSignatureObserver:
        def observe(self, prompt_id, output):
            return {}

    pack = PromptPack(ROOT / "prompt_pack")
    with pytest.raises(
        PromptExecutionError,
        match=r"does not accept \(prompt_id, envelope, output\)",
    ):
        PromptExecutor(
            None,
            pack,
            None,
            None,
            quality_guard=WrongSignatureObserver(),
            quality_guard_enabled=True,
        )


def test_guard_report_rejects_non_boolean_blocking_marker():
    output = {"status": "PASS", "findings": [], "result": {}}
    report = build_guard_report("P-WRITE-CRITIC", output, [])
    report["blocking"] = "false"

    errors = validate_guard_report(
        report,
        prompt_id="P-WRITE-CRITIC",
        model_output=output,
    )

    assert "blocking must be a boolean" in errors


def test_require_guard_report_rejects_string_enabled_marker():
    output = {"status": "PASS", "findings": [], "result": {}}
    report = disabled_guard_report("P-WRITE-CRITIC", output)

    with pytest.raises(
        QualityGuardContractError,
        match="quality_guard_enabled marker must be a boolean",
    ):
        require_guard_report(
            {
                "quality_guard_enabled": "false",
                "guard_report": report,
            },
            prompt_id="P-WRITE-CRITIC",
            model_output=output,
            default_guard_enabled=False,
        )


def test_require_guard_report_rejects_top_level_observation_status_mismatch():
    output = {"status": "PASS", "findings": [], "result": {}}
    report = build_guard_report("P-WRITE-CRITIC", output, [])

    with pytest.raises(
        QualityGuardContractError,
        match="guard_observation_status does not match",
    ):
        require_guard_report(
            {
                "quality_guard_enabled": True,
                "guard_observation_status": "DISABLED",
                "guard_report": report,
            },
            prompt_id="P-WRITE-CRITIC",
            model_output=output,
            default_guard_enabled=True,
        )


def test_non_critic_guard_finding_is_arbitrated_instead_of_silently_ignored():
    engine = object.__new__(WorkflowEngine)
    engine.executor = SimpleNamespace(quality_guard_enabled=True)
    engine.db = None
    engine.decision_arbiter = DecisionArbiter()
    engine.decision_arbiter.persist = lambda *args, **kwargs: (
        "artifact-decision",
        kwargs.get("expected_updated_at") or "test-updated-at",
    )
    engine._project_level = lambda project_id: "INTERNAL"

    output = {
        "status": "PASS",
        "result": {"blueprint": {"paragraphs": []}},
        "findings": [],
        "user_questions": [],
    }
    report = build_guard_report(
        "P-WRITE-BLUEPRINT",
        output,
        [{
            "code": "QG_BLUEPRINT_REQUIRED_ROLES_MISSING",
            "severity": "P1",
            "blocking": True,
            "target_path_or_span": "result.blueprint.paragraphs",
            "description": "The deterministic blueprint contract is incomplete.",
            "suggested_route": "REPAIR",
        }],
    )
    result = {
        "run_id": "run-producer-guard",
        "status": "PASS",
        "output": output,
        "quality_guard_enabled": True,
        "guard_observation_status": "OBSERVED",
        "guard_report": report,
    }
    wf = {
        "id": "wf-producer-guard",
        "project_id": "project-producer-guard",
        "current_step": 5,
        "status": "RUNNING",
    }
    state: dict[str, object] = {}

    decision, effective_status, effective_output = engine._record_decision(
        wf,
        state,
        "P-WRITE-BLUEPRINT",
        result,
    )

    assert decision is not None
    assert decision["decision"] == "REVISE"
    assert effective_status == "REVISE"
    assert effective_output["status"] == "REVISE"
    assert {
        item["code"] for item in effective_output["findings"]
    } == {"QG_BLUEPRINT_REQUIRED_ROLES_MISSING"}
    assert state["step_results"]["5"]["model_status"] == "PASS"
    assert state["step_results"]["5"]["effective_status"] == "REVISE"


def test_require_guard_report_rejects_result_that_downgrades_enabled_guard():
    output = {"status": "PASS", "findings": [], "result": {}}
    report = disabled_guard_report("P-WRITE-CRITIC", output)

    with pytest.raises(
        QualityGuardContractError,
        match="does not match the configured executor state",
    ):
        require_guard_report(
            {
                "quality_guard_enabled": False,
                "guard_observation_status": "DISABLED",
                "guard_report": report,
            },
            prompt_id="P-WRITE-CRITIC",
            model_output=output,
            default_guard_enabled=True,
        )
