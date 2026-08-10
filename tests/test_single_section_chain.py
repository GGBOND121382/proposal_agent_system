from __future__ import annotations

import asyncio
import copy

import pytest
from collections import defaultdict
from typing import Any

from app.workflow_authoring import WorkflowAuthoringMixin
from app.workflow_repair import WorkflowRepairMixin


class FakeContextBuilder:
    def __init__(self):
        self.results: dict[str, Any] = {}
        self.repair_applications: dict[str, Any] = {}
        self.envelopes: list[dict[str, Any]] = []

    def _result(self, project_id: str, prompt_id: str, key: str | None = None):
        value = self.results.get(prompt_id)
        if key and isinstance(value, dict):
            return value.get(key)
        return value

    def build(
        self,
        prompt_id: str,
        project_id: str,
        *,
        workflow_id: str | None = None,
        workflow_state: dict[str, Any] | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        state = workflow_state or {}
        section_id = str(state.get("active_section_id") or "")
        repair_index = state.get("repair_application_artifact_ids") or {}
        producer_for_critic = {
            "P-WRITE-BLUEPRINT-CRITIC": "P-WRITE-BLUEPRINT",
            "P-WRITE-CRITIC": "P-WRITE-CONTENT",
            "P-EXPRESSION-CRITIC": "P-EXPRESSION-POLISH",
        }
        candidate = None
        producer = producer_for_critic.get(prompt_id)
        if producer:
            target_key = f"section:{section_id}:{producer}" if section_id else producer
            active_ids = [str(item) for item in repair_index.get(target_key) or []]
            if active_ids:
                candidate = self.repair_applications.get(active_ids[-1])
            if candidate is None:
                candidate = self.results.get(producer)
                if producer == "P-WRITE-BLUEPRINT" and isinstance(candidate, dict):
                    candidate = candidate.get("blueprint")
        envelope = {
            "prompt_id": prompt_id,
            "section_id": section_id,
            "payload": {
                "candidate": candidate,
                "revision_findings": copy.deepcopy(
                    ((state.get("section_revision_findings") or {}).get(section_id))
                    or []
                ),
            },
            "overrides": copy.deepcopy(overrides or {}),
        }
        self.envelopes.append(copy.deepcopy(envelope))
        return envelope


class ScriptedExecutor:
    def __init__(self, context: FakeContextBuilder, statuses: dict[str, list[str]] | None = None):
        self.context = context
        self.statuses = {key: list(values) for key, values in (statuses or {}).items()}
        self.calls: list[dict[str, Any]] = []
        self.counts = defaultdict(int)

    async def execute(self, prompt_id: str, envelope: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        self.counts[prompt_id] += 1
        index = self.counts[prompt_id]
        queue = self.statuses.get(prompt_id) or ["PASS"]
        status = queue.pop(0) if len(queue) > 1 else queue[0]
        self.statuses[prompt_id] = queue
        run_id = f"run-{prompt_id.lower()}-{index}"
        output: dict[str, Any] = {
            "status": status,
            "result": {},
            "findings": [],
            "warnings": [],
            "user_questions": [],
        }
        if prompt_id == "P-WRITE-BLUEPRINT":
            result = {"blueprint_id": f"bp-{index}", "paragraph_plan": ["claim"]}
            output["result"] = {"blueprint": result}
            self.context.results[prompt_id] = {"blueprint": result}
        elif prompt_id == "P-WRITE-CONTENT":
            result = {"candidate_id": f"content-{index}", "candidate_text": "content"}
            output["result"] = result
            self.context.results[prompt_id] = result
        elif prompt_id == "P-EXPRESSION-POLISH":
            result = {"candidate_id": f"polish-{index}", "candidate_text": "polished"}
            output["result"] = result
            self.context.results[prompt_id] = result
        elif prompt_id == "P-TARGETED-REPAIR":
            original = envelope["overrides"]["payload.original_object"]["content"]
            finding_ids = [
                str(item["finding_instance_id"])
                for item in envelope["overrides"]["payload.findings_to_repair"]
            ]
            repaired = {**original, "repaired": True}
            output["result"] = {
                "repaired_object": repaired,
                "changed_paths": ["/content/candidate_text"],
                "unchanged_protected_hashes": [],
                "resolved_finding_ids": finding_ids,
                "unresolved_finding_ids": [],
            }
        if status in {"REVISE", "BLOCK"} and not output["findings"]:
            output["findings"] = [
                {
                    "code": "TEST_REPAIR",
                    "severity": "P1",
                    "category": "CONTENT",
                    "target_type": "SECTION",
                    "target_path_or_span": "candidate_text",
                    "description": "repair this field",
                    "evidence_refs": [],
                    "repairable": True,
                    "suggested_route": "ORIGINAL_PRODUCER",
                    "blocking": True,
                }
            ]
        result = {
            "run_id": run_id,
            "prompt_id": prompt_id,
            "status": status,
            "route": {"environment": "OFFLINE_LOCAL"},
            "output": output,
            "requested_call_key": kwargs.get("call_key"),
        }
        self.calls.append(copy.deepcopy(result))
        return result


class FakeQualityManager:
    def __init__(self):
        self.repairs: list[dict[str, Any]] = []

    def record_targeted_repair(self, **kwargs: Any) -> None:
        self.repairs.append(copy.deepcopy(kwargs))


class ChainHarness(WorkflowAuthoringMixin, WorkflowRepairMixin):
    def __init__(self, sections: list[dict[str, Any]], statuses: dict[str, list[str]] | None = None):
        self.sections = sections
        self.context_builder = FakeContextBuilder()
        self.executor = ScriptedExecutor(self.context_builder, statuses)
        self.quality_manager = FakeQualityManager()
        self.diagram_enrichment = None
        self.gates: list[str] = []
        self.observed: list[tuple[str, str]] = []
        self.wf = {
            "id": "wf-1",
            "project_id": "project-1",
            "status": "RUNNING",
            "current_step": 5,
            "state": {
                "options": {"single_section_complete_chain": True},
                "section_results": [],
                "repair_attempts": {},
            },
        }

    def _target_sections(self, project_id: str, options: dict[str, Any], state: dict[str, Any] | None = None):
        return copy.deepcopy(self.sections)

    def _update(self, wf: dict[str, Any], **updates: Any) -> None:
        for key, value in updates.items():
            if key == "state":
                self.wf["state"] = copy.deepcopy(value)
                wf["state"] = value
            else:
                self.wf[key] = value
                wf[key] = value

    def get(self, workflow_id: str) -> dict[str, Any]:
        return copy.deepcopy(self.wf)

    def _create_gate(self, wf: dict[str, Any], gate_type: str, **_: Any) -> None:
        self.gates.append(gate_type)

    def _observe_quality_result(self, wf: dict[str, Any], state: dict[str, Any], prompt_id: str, result: dict[str, Any]) -> None:
        self.observed.append((prompt_id, result["run_id"]))

    def _persist_repair_application(
        self,
        *,
        wf: dict[str, Any],
        state: dict[str, Any],
        producer_prompt: str,
        repaired_value: Any,
        **_: Any,
    ) -> str:
        artifact_id = f"artifact-repair-{len(self.context_builder.repair_applications) + 1}"
        target_key = self._repair_override_key(producer_prompt, state)
        self.context_builder.repair_applications[artifact_id] = copy.deepcopy(repaired_value)
        state.setdefault("repair_application_artifact_ids", {}).setdefault(
            target_key, []
        ).append(artifact_id)
        return artifact_id

    def _project_level(self, project_id: str) -> str:
        return "INTERNAL"


class ArbitratedChainHarness(ChainHarness):
    """Exercise the production section decision seam without database plumbing."""

    def _record_decision(
        self,
        wf: dict[str, Any],
        state: dict[str, Any],
        prompt_id: str,
        result: dict[str, Any],
    ) -> tuple[dict[str, Any], str, dict[str, Any]]:
        from app.decision_arbiter import DecisionArbiter
        from app.quality_guard import build_guard_report
        from app.workflows import WorkflowEngine

        output = result["output"]
        record = DecisionArbiter().arbitrate(
            output,
            build_guard_report(prompt_id, output, []),
            prompt_id=prompt_id,
        ).to_dict()
        effective_status, effective_output = WorkflowEngine._effective_critic_result(
            result,
            record,
        )
        return record, effective_status, effective_output


SECTION = {"section_id": "section-1", "title": "研究内容"}


def test_single_section_happy_path_runs_exact_chain_and_gate():
    harness = ChainHarness([SECTION])
    result = asyncio.run(harness._write_sections(harness.wf, harness.wf["state"]))
    assert result["status"] == "WAITING_GATE"
    assert harness.gates == ["CANDIDATE_REVIEW"]
    assert [item["prompt_id"] for item in harness.executor.calls] == [
        "P-WRITE-BLUEPRINT",
        "P-WRITE-BLUEPRINT-CRITIC",
        "P-WRITE-CONTENT",
        "P-WRITE-CRITIC",
        "P-EXPRESSION-POLISH",
        "P-EXPRESSION-CRITIC",
    ]
    section_result = harness.wf["state"]["section_results"][0]
    assert section_result["section_id"] == "section-1"
    assert section_result["status"] == "COMPLETED"


def test_section_chain_uses_arbiter_effective_status_and_preserves_raw_run():
    harness = ArbitratedChainHarness(
        [SECTION],
        {"P-WRITE-BLUEPRINT-CRITIC": ["REVISE"]},
    )
    original_execute = harness.executor.execute

    async def execute_with_nonblocking_finding(*args: Any, **kwargs: Any) -> dict[str, Any]:
        result = await original_execute(*args, **kwargs)
        if args[0] == "P-WRITE-BLUEPRINT-CRITIC":
            result["output"]["findings"][0]["blocking"] = False
            harness.executor.calls[-1]["output"]["findings"][0]["blocking"] = False
        return result

    harness.executor.execute = execute_with_nonblocking_finding

    completed = asyncio.run(
        harness._write_sections(harness.wf, harness.wf["state"])
    )

    assert completed["status"] == "WAITING_GATE"
    assert harness.executor.calls[1]["status"] == "REVISE"
    assert harness.executor.calls[1]["output"]["status"] == "REVISE"
    assert all(
        call["prompt_id"] != "P-TARGETED-REPAIR"
        for call in harness.executor.calls
    )
    section_runs = harness.wf["state"]["section_results"][0]["runs"]
    assert section_runs[1]["status"] == "PASS"


def test_blueprint_and_content_revise_each_get_one_targeted_repair_and_rereview():
    harness = ChainHarness(
        [SECTION],
        {
            "P-WRITE-BLUEPRINT-CRITIC": ["REVISE", "PASS"],
            "P-WRITE-CRITIC": ["REVISE", "PASS"],
        },
    )
    result = asyncio.run(harness._write_sections(harness.wf, harness.wf["state"]))
    assert result["status"] == "WAITING_GATE"
    sequence = [item["prompt_id"] for item in harness.executor.calls]
    assert sequence == [
        "P-WRITE-BLUEPRINT",
        "P-WRITE-BLUEPRINT-CRITIC",
        "P-TARGETED-REPAIR",
        "P-WRITE-BLUEPRINT-CRITIC",
        "P-WRITE-CONTENT",
        "P-WRITE-CRITIC",
        "P-TARGETED-REPAIR",
        "P-WRITE-CRITIC",
        "P-EXPRESSION-POLISH",
        "P-EXPRESSION-CRITIC",
    ]
    attempts = harness.wf["state"]["repair_attempts"]
    assert attempts["section:section-1:P-WRITE-BLUEPRINT-CRITIC"] == 1
    assert attempts["section:section-1:P-WRITE-CRITIC"] == 1
    lifecycle_events = [
        item["event"]
        for item in harness.wf["state"]["repair_ledger_v1"]["events"]
    ]
    assert lifecycle_events.count("REREVIEW_STARTED") == 2
    assert lifecycle_events.count("REREVIEW_PASS") == 2
    assert len(harness.quality_manager.repairs) == 2
    critic_envelopes = [
        item for item in harness.context_builder.envelopes
        if item["prompt_id"] in {"P-WRITE-BLUEPRINT-CRITIC", "P-WRITE-CRITIC"}
    ]
    assert critic_envelopes[1]["payload"]["candidate"]["repaired"] is True
    assert critic_envelopes[3]["payload"]["candidate"]["repaired"] is True
    roles = [item.get("role") for item in harness.wf["state"]["section_results"][0]["runs"]]
    assert roles.count("TARGETED_REPAIR") == 2
    assert roles.count("INDEPENDENT_REVIEW") == 2


def test_second_revise_after_targeted_repair_blocks_without_second_repair():
    harness = ChainHarness(
        [SECTION],
        {"P-WRITE-BLUEPRINT-CRITIC": ["REVISE", "REVISE"]},
    )
    result = asyncio.run(harness._write_sections(harness.wf, harness.wf["state"]))
    assert result["status"] == "BLOCKED_CONTENT"
    sequence = [item["prompt_id"] for item in harness.executor.calls]
    assert sequence == [
        "P-WRITE-BLUEPRINT",
        "P-WRITE-BLUEPRINT-CRITIC",
        "P-TARGETED-REPAIR",
        "P-WRITE-BLUEPRINT-CRITIC",
    ]
    assert sequence.count("P-TARGETED-REPAIR") == 1
    lifecycle_events = [
        item["event"]
        for item in harness.wf["state"]["repair_ledger_v1"]["events"]
    ]
    assert lifecycle_events.count("REREVIEW_STARTED") == 1
    assert lifecycle_events.count("REREVIEW_REVISE") == 1
    assert "禁止二次自动修复" in harness.wf["state"]["last_error"]


def test_explicit_bounded_repair_limit_can_converge_after_second_revise():
    harness = ChainHarness(
        [SECTION],
        {"P-WRITE-BLUEPRINT-CRITIC": ["REVISE", "REVISE", "PASS"]},
    )
    harness.wf["state"]["options"]["targeted_repair_limit"] = 2

    result = asyncio.run(harness._write_sections(harness.wf, harness.wf["state"]))

    assert result["status"] == "WAITING_GATE"
    sequence = [item["prompt_id"] for item in harness.executor.calls]
    assert sequence[:6] == [
        "P-WRITE-BLUEPRINT",
        "P-WRITE-BLUEPRINT-CRITIC",
        "P-TARGETED-REPAIR",
        "P-WRITE-BLUEPRINT-CRITIC",
        "P-TARGETED-REPAIR",
        "P-WRITE-BLUEPRINT-CRITIC",
    ]
    assert sequence.count("P-TARGETED-REPAIR") == 2
    assert (
        harness.wf["state"]["repair_attempts"][
            "section:section-1:P-WRITE-BLUEPRINT-CRITIC"
        ]
        == 2
    )


def test_acceptance_run_regenerates_new_candidate_after_repair_recheck():
    harness = ChainHarness(
        [SECTION],
        {"P-WRITE-BLUEPRINT-CRITIC": ["REVISE", "REVISE", "PASS"]},
    )
    harness.wf["state"]["options"]["acceptance_run"] = True

    result = asyncio.run(harness._write_sections(harness.wf, harness.wf["state"]))

    assert result["status"] == "WAITING_GATE"
    sequence = [item["prompt_id"] for item in harness.executor.calls]
    assert sequence[:7] == [
        "P-WRITE-BLUEPRINT",
        "P-WRITE-BLUEPRINT-CRITIC",
        "P-TARGETED-REPAIR",
        "P-WRITE-BLUEPRINT-CRITIC",
        "P-WRITE-BLUEPRINT",
        "P-WRITE-BLUEPRINT-CRITIC",
        "P-WRITE-CONTENT",
    ]
    assert harness.executor.counts["P-WRITE-BLUEPRINT"] == 2
    assert harness.wf["state"]["acceptance_regeneration_rounds"][
        "section:section-1:P-WRITE-BLUEPRINT-CRITIC"
    ] == 1
    assert "section:section-1:P-WRITE-BLUEPRINT" not in (
        harness.wf["state"].get("repair_application_artifact_ids") or {}
    )
    assert "integration_repair_section_ids" not in harness.wf["state"]


def test_acceptance_run_regenerates_producer_rejected_by_quality_guard():
    harness = ChainHarness(
        [SECTION],
        {"P-WRITE-BLUEPRINT": ["REVISE", "PASS"]},
    )
    harness.wf["state"]["options"]["acceptance_run"] = True

    result = asyncio.run(harness._write_sections(harness.wf, harness.wf["state"]))

    assert result["status"] == "WAITING_GATE"
    sequence = [item["prompt_id"] for item in harness.executor.calls]
    assert sequence[:3] == [
        "P-WRITE-BLUEPRINT",
        "P-WRITE-BLUEPRINT",
        "P-WRITE-BLUEPRINT-CRITIC",
    ]
    assert harness.wf["state"]["acceptance_regeneration_rounds"][
        "section:section-1:P-WRITE-BLUEPRINT"
    ] == 1
    assert "integration_repair_section_ids" not in harness.wf["state"]


def test_section_feedback_tracks_latest_decision_and_is_consumed_by_producer_pass():
    harness = ChainHarness([SECTION])
    state = harness.wf["state"]
    state["active_section_id"] = "section-1"

    for code in ("FIRST_CANDIDATE_DEFECT", "LATEST_CANDIDATE_DEFECT"):
        result = {
            "status": "REVISE",
            "output": {
                "status": "REVISE",
                "findings": [{"code": code, "repairable": True}],
            },
        }
        harness._apply_section_decision(
            harness.wf, state, "P-WRITE-BLUEPRINT", result
        )

    assert state["section_revision_findings"]["section-1"] == [
        {"code": "LATEST_CANDIDATE_DEFECT", "repairable": True}
    ]

    harness._apply_section_decision(
        harness.wf,
        state,
        "P-WRITE-BLUEPRINT",
        {"status": "PASS", "output": {"status": "PASS", "findings": []}},
    )

    assert "section-1" not in state["section_revision_findings"]


def test_each_acceptance_producer_regeneration_gets_a_distinct_call_key():
    harness = ChainHarness(
        [SECTION],
        {"P-WRITE-BLUEPRINT": ["REVISE", "REVISE", "PASS"]},
    )
    harness.wf["state"]["options"]["acceptance_run"] = True

    result = asyncio.run(harness._write_sections(harness.wf, harness.wf["state"]))

    assert result["status"] == "WAITING_GATE"
    blueprint_calls = [
        item for item in harness.executor.calls
        if item["prompt_id"] == "P-WRITE-BLUEPRINT"
    ]
    assert len(blueprint_calls) == 3
    assert blueprint_calls[0]["requested_call_key"] is None
    assert blueprint_calls[1]["requested_call_key"]
    assert blueprint_calls[2]["requested_call_key"]
    assert blueprint_calls[1]["requested_call_key"] != blueprint_calls[2]["requested_call_key"]


def test_acceptance_call_key_changes_when_feedback_changes_at_same_round():
    harness = ChainHarness([SECTION])
    state = harness.wf["state"]
    state["active_section_id"] = "section-1"
    state["active_section"] = SECTION
    state["acceptance_candidate_rounds"] = {
        "section:section-1:P-WRITE-BLUEPRINT": 1
    }
    progress = {
        "section_id": "section-1",
        "phase": "BLUEPRINT",
        "status": "RUNNING",
        "runs": [],
    }

    call_keys = []
    for code in ("FIRST_INPUT", "CHANGED_INPUT"):
        state["section_revision_findings"] = {
            "section-1": [{"code": code, "repairable": True}]
        }
        _envelope, result = asyncio.run(
            harness._execute_section_prompt(
                harness.wf,
                state,
                SECTION,
                progress,
                "P-WRITE-BLUEPRINT",
            )
        )
        call_keys.append(result["requested_call_key"])

    assert call_keys[0]
    assert call_keys[1]
    assert call_keys[0] != call_keys[1]


def test_test_acceptance_can_regenerate_fully_repairable_critic_block():
    harness = ChainHarness(
        [SECTION],
        {"P-WRITE-BLUEPRINT-CRITIC": ["BLOCK", "PASS"]},
    )
    harness.wf["state"]["options"].update({
        "acceptance_run": True,
        "allow_repairable_block_regeneration": True,
    })

    result = asyncio.run(harness._write_sections(harness.wf, harness.wf["state"]))

    assert result["status"] == "WAITING_GATE"
    sequence = [item["prompt_id"] for item in harness.executor.calls]
    assert sequence[:4] == [
        "P-WRITE-BLUEPRINT",
        "P-WRITE-BLUEPRINT-CRITIC",
        "P-WRITE-BLUEPRINT",
        "P-WRITE-BLUEPRINT-CRITIC",
    ]
    assert "P-TARGETED-REPAIR" not in sequence


def test_expression_critic_revise_gets_targeted_repair_and_independent_rereview():
    harness = ChainHarness(
        [SECTION],
        {"P-EXPRESSION-CRITIC": ["REVISE", "PASS"]},
    )
    result = asyncio.run(harness._write_sections(harness.wf, harness.wf["state"]))
    assert result["status"] == "WAITING_GATE"
    sequence = [item["prompt_id"] for item in harness.executor.calls]
    assert sequence[-3:] == [
        "P-EXPRESSION-CRITIC",
        "P-TARGETED-REPAIR",
        "P-EXPRESSION-CRITIC",
    ]
    assert (
        harness.wf["state"]["repair_attempts"][
            "section:section-1:P-EXPRESSION-CRITIC"
        ]
        == 1
    )
    assert harness.context_builder.envelopes[-1]["payload"]["candidate"]["repaired"] is True


def test_single_section_mode_rejects_ambiguous_multi_section_selection():
    harness = ChainHarness([SECTION, {"section_id": "section-2", "title": "技术路线"}])
    result = asyncio.run(harness._write_sections(harness.wf, harness.wf["state"]))
    assert result["status"] == "BLOCKED_CONTENT"
    assert harness.executor.calls == []
    assert "精确选择一个章节" in harness.wf["state"]["last_error"]


def test_repair_budget_and_override_are_scoped_per_section():
    sections = [SECTION, {"section_id": "section-2", "title": "技术路线"}]
    harness = ChainHarness(
        sections,
        {"P-WRITE-BLUEPRINT-CRITIC": ["REVISE", "PASS", "REVISE", "PASS"]},
    )
    harness.wf["state"]["options"] = {}
    result = asyncio.run(harness._write_sections(harness.wf, harness.wf["state"]))
    assert result["status"] == "WAITING_GATE"
    attempts = harness.wf["state"]["repair_attempts"]
    assert attempts["section:section-1:P-WRITE-BLUEPRINT-CRITIC"] == 1
    assert attempts["section:section-2:P-WRITE-BLUEPRINT-CRITIC"] == 1
    assert len([call for call in harness.executor.calls if call["prompt_id"] == "P-TARGETED-REPAIR"]) == 2


class SimulatedRereviewCrash(RuntimeError):
    pass


class CrashAfterRereviewStartHarness(ChainHarness):
    def __init__(self, sections, statuses=None):
        super().__init__(sections, statuses)
        self.crash_after_rereview_start = True
        self._crashed = False

    def _update(self, wf: dict[str, Any], **updates: Any) -> None:
        super()._update(wf, **updates)
        events = (
            self.wf["state"].get("repair_ledger_v1", {}).get("events", [])
        )
        if (
            self.crash_after_rereview_start
            and not self._crashed
            and events
            and events[-1].get("event") == "REREVIEW_STARTED"
        ):
            self._crashed = True
            raise SimulatedRereviewCrash("crash after rereview checkpoint")


def test_section_rereview_checkpoint_survives_crash_and_completes_audit_lifecycle():
    harness = CrashAfterRereviewStartHarness(
        [SECTION],
        {"P-WRITE-BLUEPRINT-CRITIC": ["REVISE", "PASS"]},
    )

    with pytest.raises(SimulatedRereviewCrash):
        asyncio.run(harness._write_sections(harness.wf, harness.wf["state"]))

    assert harness.wf["state"]["section_progress"]["section-1"][
        "pending_repair_rereview"
    ]["critic_prompt"] == "P-WRITE-BLUEPRINT-CRITIC"
    harness.crash_after_rereview_start = False

    result = asyncio.run(harness._write_sections(harness.wf, harness.wf["state"]))

    assert result["status"] == "WAITING_GATE"
    ledger_events = [
        item["event"]
        for item in harness.wf["state"]["repair_ledger_v1"]["events"]
    ]
    assert ledger_events.count("REREVIEW_STARTED") == 1
    assert ledger_events.count("REREVIEW_PASS") == 1
    section_runs = harness.wf["state"]["section_results"][0]["runs"]
    critic_roles = [
        item.get("role")
        for item in section_runs
        if item.get("prompt_id") == "P-WRITE-BLUEPRINT-CRITIC"
    ]
    assert critic_roles == ["INITIAL_REVIEW", "INDEPENDENT_REVIEW"]
    assert "pending_repair_rereview" not in harness.wf["state"][
        "section_progress"
    ]["section-1"]


def test_mismatched_persisted_rereview_checkpoint_fails_closed_before_new_prompt():
    harness = ChainHarness([SECTION])
    harness._runtime_configuration_report = lambda *_args, **_kwargs: None
    harness.wf["state"]["section_progress"] = {
        "section-1": {
            "section_id": "section-1",
            "title": "研究内容",
            "phase": "CONTENT",
            "status": "RUNNING",
            "runs": [],
            "pending_repair_rereview": {
                "critic_prompt": "P-WRITE-BLUEPRINT-CRITIC",
                "repair_id": "repair-stale",
                "repair_attempt_key": "section:section-1:P-WRITE-BLUEPRINT-CRITIC",
                "repair_application_artifact_id": "artifact-stale",
            },
        }
    }

    result = asyncio.run(harness._write_sections(harness.wf, harness.wf["state"]))

    assert result["status"] == "BLOCKED_TECHNICAL"
    assert harness.executor.calls == []
    assert "does not match the current section phase" in harness.wf["state"]["last_error"]


class CrashAfterRereviewCompletionHarness(ChainHarness):
    def __init__(self, sections, statuses=None):
        super().__init__(sections, statuses)
        self.crash_after_completion = True
        self._completion_crashed = False

    def _complete_repair_rereview(self, *args: Any, **kwargs: Any) -> int:
        count = super()._complete_repair_rereview(*args, **kwargs)
        if self.crash_after_completion and not self._completion_crashed:
            self._completion_crashed = True
            raise SimulatedRereviewCrash("crash after rereview completion before phase commit")
        return count


def test_section_rereview_checkpoint_is_not_cleared_before_phase_commit():
    harness = CrashAfterRereviewCompletionHarness(
        [SECTION],
        {"P-WRITE-BLUEPRINT-CRITIC": ["REVISE", "PASS"]},
    )

    with pytest.raises(SimulatedRereviewCrash):
        asyncio.run(harness._write_sections(harness.wf, harness.wf["state"]))

    persisted = harness.wf["state"]["section_progress"]["section-1"]
    assert persisted["pending_repair_rereview"]["critic_prompt"] == (
        "P-WRITE-BLUEPRINT-CRITIC"
    )
    persisted_events = harness.wf["state"]["repair_ledger_v1"]["events"]
    assert [item["event"] for item in persisted_events].count("REREVIEW_STARTED") == 1
    assert [item["event"] for item in persisted_events].count("REREVIEW_PASS") == 1

    harness.crash_after_completion = False
    result = asyncio.run(harness._write_sections(harness.wf, harness.wf["state"]))

    assert result["status"] == "WAITING_GATE"
    events = harness.wf["state"]["repair_ledger_v1"]["events"]
    assert [item["event"] for item in events].count("REREVIEW_STARTED") == 1
    assert [item["event"] for item in events].count("REREVIEW_PASS") == 1
    assert "pending_repair_rereview" not in harness.wf["state"][
        "section_progress"
    ]["section-1"]
