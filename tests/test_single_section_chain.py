from __future__ import annotations

import asyncio
import copy
from collections import defaultdict
from typing import Any

from app.workflow_authoring import WorkflowAuthoringMixin
from app.workflow_repair import WorkflowRepairMixin


class FakeContextBuilder:
    def __init__(self):
        self.results: dict[str, Any] = {}
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
        repair_overrides = state.get("repair_overrides") or {}
        producer_for_critic = {
            "P-WRITE-BLUEPRINT-CRITIC": "P-WRITE-BLUEPRINT",
            "P-WRITE-CRITIC": "P-WRITE-CONTENT",
            "P-EXPRESSION-CRITIC": "P-EXPRESSION-POLISH",
        }
        candidate = None
        producer = producer_for_critic.get(prompt_id)
        if producer:
            candidate = repair_overrides.get(f"section:{section_id}:{producer}")
            if candidate is None:
                candidate = self.results.get(producer)
                if producer == "P-WRITE-BLUEPRINT" and isinstance(candidate, dict):
                    candidate = candidate.get("blueprint")
        envelope = {
            "prompt_id": prompt_id,
            "section_id": section_id,
            "payload": {"candidate": candidate},
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

    async def execute(self, prompt_id: str, envelope: dict[str, Any], **_: Any) -> dict[str, Any]:
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
            repaired = {**original, "repaired": True}
            output["result"] = {
                "repaired_object": repaired,
                "changed_paths": ["content.candidate_text"],
                "unchanged_protected_hashes": [],
                "resolved_finding_codes": ["TEST_REPAIR"],
                "unresolved_finding_codes": [],
            }
        elif status == "REVISE":
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
                "repair_overrides": {},
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

    def _project_level(self, project_id: str) -> str:
        return "INTERNAL"


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
    assert result["status"] == "BLOCKED"
    sequence = [item["prompt_id"] for item in harness.executor.calls]
    assert sequence == [
        "P-WRITE-BLUEPRINT",
        "P-WRITE-BLUEPRINT-CRITIC",
        "P-TARGETED-REPAIR",
        "P-WRITE-BLUEPRINT-CRITIC",
    ]
    assert sequence.count("P-TARGETED-REPAIR") == 1
    assert "禁止二次自动修复" in harness.wf["state"]["last_error"]


def test_expression_critic_revise_blocks_and_never_rewrites_polish():
    harness = ChainHarness([SECTION], {"P-EXPRESSION-CRITIC": ["REVISE"]})
    result = asyncio.run(harness._write_sections(harness.wf, harness.wf["state"]))
    assert result["status"] == "BLOCKED"
    sequence = [item["prompt_id"] for item in harness.executor.calls]
    assert sequence[-1] == "P-EXPRESSION-CRITIC"
    assert "P-TARGETED-REPAIR" not in sequence


def test_single_section_mode_rejects_ambiguous_multi_section_selection():
    harness = ChainHarness([SECTION, {"section_id": "section-2", "title": "技术路线"}])
    result = asyncio.run(harness._write_sections(harness.wf, harness.wf["state"]))
    assert result["status"] == "BLOCKED"
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
