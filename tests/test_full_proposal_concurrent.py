from __future__ import annotations

import asyncio
import copy
import json
from collections import Counter
from datetime import datetime

import pytest

from app.full_proposal_workers import FullProposalWorkersMixin
from app.quality import QualityLifecycleManager
from app.repair_ledger import RepairLedger
from app.runtime_api import WorkflowEngine
from app.workflow_repair import WorkflowRepairMixin
from app.workflow_status import is_recoverable_block
from tests.test_runtime import add_standard_materials, create_project, finish_workflow, runtime
from tests.test_single_section_chain import ChainHarness, SECTION


FULL_PROPOSAL_OPTIONS = {
    "full_proposal_concurrent": True,
    "integration_scope": "FULL_PROPOSAL_CONCURRENT",
}
FULL_PROPOSAL_TITLES = [
    "项目摘要",
    "立项依据",
    "国内外研究现状",
    "关键科学问题",
    "研究目标",
    "研究内容",
    "关键技术",
    "技术路线",
    "实验方案",
    "创新点",
    "预期成果",
    "研究基础",
    "进度安排",
    "参考文献",
]
EXPECTED_GROUPS = {
    "GROUP_1_BACKGROUND_AND_PROBLEM",
    "GROUP_2_OBJECTIVES_AND_TASKS",
    "GROUP_3_METHOD_AND_VALIDATION",
    "GROUP_4_IMPLEMENTATION_AND_ASSURANCE",
    "GROUP_5_FIGURES_AND_REFERENCES",
}


def _approve_open_gate(engine: WorkflowEngine, workflow_id: str) -> None:
    gate = next(item for item in engine.list_gates(workflow_id=workflow_id) if item["status"] == "OPEN")
    action = "APPROVE" if "APPROVE" in gate["allowed_actions"] else "CONFIRM"
    engine.decide_gate(
        gate["id"],
        action=action,
        decided_by="full-proposal-test",
        decided_role=gate["required_role"],
    )


async def _prepare(engine: WorkflowEngine, project_id: str) -> None:
    for workflow_type in ["WF-1_PROJECT_INTAKE", "WF-2_TEMPLATE_EXTRACTION"]:
        workflow = await finish_workflow(engine, project_id, workflow_type)
        assert workflow["status"] == "COMPLETED", workflow["state"].get("last_error")


async def _run_parent(engine: WorkflowEngine, workflow: dict, max_steps: int = 500) -> dict:
    current = workflow
    for _ in range(max_steps):
        current = await engine.advance(current["id"])
        if current["status"] == "WAITING_GATE":
            _approve_open_gate(engine, current["id"])
            continue
        if current["status"] in {"COMPLETED", "CANCELLED"} or is_recoverable_block(current["status"]):
            return current
    return current


def _prompt_counts_by_section(db, project_id: str, workflow_ids: list[str], prompt_id: str) -> Counter:
    placeholders = ",".join("?" for _ in workflow_ids)
    rows = db.fetchall(
        f"SELECT input_json FROM prompt_runs WHERE project_id=? AND workflow_id IN ({placeholders}) AND prompt_id=? AND status='PASS'",
        (project_id, *workflow_ids, prompt_id),
    )
    return Counter(
        str((json.loads(row["input_json"]).get("payload") or {}).get("source_section", {}).get("title") or "")
        for row in rows
    )


def _inject_one_full_document_conflict(executor):
    simulator = executor.gateway.simulator
    original = simulator._handle_integration_critic
    calls = {"count": 0, "target_section_id": None}

    def injected(base, envelope):
        output = original(copy.deepcopy(base), envelope)
        calls["count"] += 1
        if calls["count"] != 1:
            return output
        payload = envelope.get("payload") or {}
        section_map = {
            str(item.get("title") or ""): str(item.get("section_id") or "")
            for item in payload.get("document_section_map") or []
            if isinstance(item, dict)
        }
        target_id = section_map["技术路线"]
        calls["target_section_id"] = target_id
        output["status"] = "REVISE"
        output["result"]["verdict"] = "REVISE"
        output["result"]["terminology_checks"] = [
            {"term": "低扰动增量优化", "consistent": False, "sections": [target_id]}
        ]
        output["findings"] = [
            {
                "code": "FULL_PROPOSAL_TECHNICAL_ROUTE_CONFLICT",
                "severity": "P1",
                "category": "INTEGRATION",
                "target_type": "SECTION_CANDIDATE",
                "target_path_or_span": f"candidate_sections.{target_id}.paragraphs",
                "description": "技术路线章节与完整申请书冻结术语不一致。",
                "evidence_refs": [target_id],
                "repairable": True,
                "repair_instruction": "仅由技术路线责任写作组重写受影响章节。",
                "suggested_route": "WRITING_AGENT",
                "blocking": True,
            }
        ]
        return output

    simulator._handle_integration_critic = injected
    return calls




class _ChildResetHarness(FullProposalWorkersMixin, WorkflowRepairMixin):
    @staticmethod
    def _update(wf: dict, **updates) -> None:
        for key, value in updates.items():
            wf[key] = value


class _ChildResetDB:
    def __init__(self) -> None:
        self.inserts: list[tuple] = []
        self.audits: list[tuple[str, dict]] = []

    def execute(self, sql: str, params: tuple = ()) -> None:
        assert sql.startswith("INSERT INTO workflows")
        self.inserts.append(params)

    def audit(self, event_type: str, **kwargs) -> None:
        self.audits.append((event_type, kwargs))


def test_concurrent_section_rewrite_resets_only_the_superseded_repair_subject() -> None:
    harness = _ChildResetHarness()
    state = {
        "options": {"target_section_ids": ["s1", "s2"]},
        "section_results": [
            {"section_id": "s1", "status": "COMPLETED"},
            {"section_id": "s2", "status": "COMPLETED"},
        ],
        "section_progress": {"s1": {"phase": "DONE"}, "s2": {"phase": "DONE"}},
        "repair_attempts": {
            "section:s1:P-WRITE-CRITIC": 1,
            "section:s2:P-WRITE-CRITIC": 1,
        },
        "repair_application_artifact_ids": {
            "section:s1:P-WRITE-CONTENT": ["artifact-s1"],
            "section:s2:P-WRITE-CONTENT": ["artifact-s2"],
        },
        "pending_repair_rereviews": {
            "P-WRITE-CRITIC": {
                "repair_attempt_key": "section:s1:P-WRITE-CRITIC",
            }
        },
    }
    for section_id in ("s1", "s2"):
        key = f"section:{section_id}:P-WRITE-CRITIC"
        RepairLedger.applied(
            state,
            key,
            repair_id=f"repair-{section_id}",
            application_artifact_id=f"artifact-{section_id}",
        )
        RepairLedger.rereview_started(
            state,
            key,
            repair_id=f"repair-{section_id}",
            application_artifact_id=f"artifact-{section_id}",
        )

    child = {"id": "wf-child", "state": state, "status": "BLOCKED_CONTENT", "current_step": 9}
    child = harness._reset_full_proposal_child_for_repair(
        child,
        {"integration_repair_findings": [{"code": "REWRITE_S1"}]},
        "wf-parent",
        {"s1"},
    )

    assert [item["section_id"] for item in state["section_results"]] == ["s2"]
    assert "s1" not in state["section_progress"]
    assert state["section_progress"]["s2"]["phase"] == "DONE"
    assert "section:s1:P-WRITE-CRITIC" not in state["repair_attempts"]
    assert state["repair_attempts"]["section:s2:P-WRITE-CRITIC"] == 1
    assert "section:s1:P-WRITE-CONTENT" not in state["repair_application_artifact_ids"]
    assert state["repair_application_artifact_ids"]["section:s2:P-WRITE-CONTENT"] == ["artifact-s2"]
    assert "pending_repair_rereviews" not in state
    assert RepairLedger.count(state, "semantic_repairs", "section:s1:P-WRITE-CRITIC") == 0
    assert RepairLedger.count(state, "semantic_repairs", "section:s2:P-WRITE-CRITIC") == 1
    assert child["status"] == "RUNNING"
    assert child["current_step"] == 5



def test_completed_concurrent_child_rewrite_creates_replacement_record() -> None:
    harness = _ChildResetHarness()
    harness.db = _ChildResetDB()
    state = {
        "options": {"target_section_ids": ["s1"]},
        "section_results": [{"section_id": "s1", "status": "COMPLETED"}],
        "section_progress": {"s1": {"phase": "DONE"}},
        "repair_attempts": {},
    }
    child = {
        "id": "wf-child-completed",
        "project_id": "project-1",
        "workflow_type": "WF-4_PROPOSAL_AUTHORING",
        "state": state,
        "status": "COMPLETED",
        "current_step": 9,
    }
    record = {"group_id": "GROUP_1", "workflow_id": child["id"], "status": "COMPLETED"}

    replacement = harness._reset_full_proposal_child_for_repair(
        child,
        {"integration_repair_findings": [{"code": "REWRITE_S1"}]},
        "wf-parent",
        {"s1"},
        record=record,
    )

    assert child["status"] == "COMPLETED"
    assert child["state"] == state
    assert replacement["id"] != child["id"]
    assert replacement["status"] == "RUNNING"
    assert replacement["state"]["supersedes_workflow_id"] == child["id"]
    assert replacement["state"]["section_results"] == []
    assert record["workflow_id"] == replacement["id"]
    assert record["superseded_workflow_ids"] == [child["id"]]
    assert len(harness.db.inserts) == 1
    assert harness.db.audits[0][0] == "FULL_PROPOSAL_GROUP_REPLACEMENT_STARTED"

def test_concurrent_child_targeted_repair_is_recorded_on_parent_quality_lifecycle():
    harness = ChainHarness(
        [SECTION],
        {"P-WRITE-BLUEPRINT-CRITIC": ["REVISE", "PASS"]},
    )
    harness.wf["state"]["options"] = {"concurrent_group_child": True}
    harness.wf["state"]["quality_parent_workflow_id"] = "wf-parent"
    result = asyncio.run(harness._write_sections(harness.wf, harness.wf["state"]))
    assert result["status"] == "WAITING_GATE"
    assert harness.quality_manager.repairs
    assert {item["workflow_id"] for item in harness.quality_manager.repairs} == {"wf-parent"}


def test_full_proposal_contract_requires_four_core_groups(runtime):
    _, _, _, _, _, _, engine, _ = runtime
    sections = [
        {"section_id": "s1", "title": "立项依据"},
        {"section_id": "s2", "title": "研究目标"},
        {"section_id": "s3", "title": "研究内容"},
        {"section_id": "s4", "title": "技术路线"},
        {"section_id": "s5", "title": "实验方案"},
        {"section_id": "s6", "title": "创新点"},
        {"section_id": "s7", "title": "参考文献"},
        {"section_id": "s8", "title": "项目概述"},
    ]
    with pytest.raises(ValueError, match="实施与保障"):
        engine._resolve_full_proposal_contract(sections, {"options": FULL_PROPOSAL_OPTIONS})


def test_upstream_revision_invalidates_all_concurrent_child_generations(runtime):
    _, _, _, _, _, _, engine, _ = runtime
    state = {
        "options": FULL_PROPOSAL_OPTIONS,
        "full_proposal_contract": {"contract_hash": "a" * 64},
        "full_proposal_children": {
            "GROUP_1_BACKGROUND_AND_PROBLEM": {
                "workflow_id": "wf-group-old",
                "section_ids": ["s1"],
                "status": "COMPLETED",
            }
        },
        "authoring_child_workflow_ids": ["wf-group-old"],
        "section_progress": {"s1": {"phase": "DONE"}},
        "full_proposal_concurrency": {"mode": "FIVE_GROUP_PARALLEL_SECTION_SERIAL"},
    }
    engine._invalidate_full_proposal_generation(
        state, reason="INTEGRATION_ARGUMENT_ARCHITECTURE_REVISION"
    )
    assert state["full_proposal_children"] == {}
    assert "full_proposal_contract" not in state
    assert "authoring_child_workflow_ids" not in state
    assert "section_progress" not in state
    archived = state["full_proposal_child_generations"]
    assert archived[-1]["children"]["GROUP_1_BACKGROUND_AND_PROBLEM"]["workflow_id"] == "wf-group-old"
    assert archived[-1]["reason"] == "INTEGRATION_ARGUMENT_ARCHITECTURE_REVISION"


def test_full_proposal_five_groups_are_parallel_isolated_and_complete(runtime):
    settings, _, db, _, _, _, engine, _ = runtime
    project_id = create_project(db, internet=False)
    add_standard_materials(settings, db, project_id, current_sections=FULL_PROPOSAL_TITLES)

    async def scenario():
        await _prepare(engine, project_id)
        workflow = engine.start(project_id, "WF-4_PROPOSAL_AUTHORING", FULL_PROPOSAL_OPTIONS)
        return await _run_parent(engine, workflow)

    completed = asyncio.run(asyncio.wait_for(scenario(), timeout=120))
    assert completed["status"] == "COMPLETED", completed["state"].get("last_error")
    state = completed["state"]
    contract = state["full_proposal_contract"]
    assert contract["contract_type"] == "FULL_PROPOSAL_CONCURRENT"
    assert len(contract["sections"]) == len(FULL_PROPOSAL_TITLES)
    assert {item["group_id"] for item in contract["groups"]} == EXPECTED_GROUPS
    assert len({item["section_id"] for item in contract["sections"]}) == len(contract["sections"])
    assert len(state["section_results"]) == len(contract["sections"])

    children = state["full_proposal_children"]
    assert set(children) == EXPECTED_GROUPS
    child_ids = state["authoring_child_workflow_ids"]
    assert len(child_ids) == 5
    assert len(set(child_ids)) == 5
    for group_id, record in children.items():
        child = engine.get(record["workflow_id"])
        assert child["status"] == "COMPLETED"
        assert child["state"]["parent_workflow_id"] == completed["id"]
        assert child["state"]["full_proposal_group_id"] == group_id
        assert {
            item["section_id"] for item in child["state"]["section_results"]
        } == set(record["section_ids"])

    starts = [datetime.fromisoformat(record["started_at"]) for record in children.values()]
    finishes = [datetime.fromisoformat(record["finished_at"]) for record in children.values()]
    assert max(starts) < min(finishes), "five group workers did not overlap"
    assert state["full_proposal_concurrency"]["no_shared_mutable_draft"] is True
    assert state["full_proposal_review_history"][-1]["status"] == "PASS"
    assert state["full_proposal_review_history"][-1]["contract_hash"] == contract["contract_hash"]

    counts = _prompt_counts_by_section(db, project_id, child_ids, "P-EXPRESSION-CRITIC")
    assert counts == Counter({title: 1 for title in FULL_PROPOSAL_TITLES})
    assert engine.quality_manager.quality_matrix(
        project_id, workflow_id=completed["id"]
    )["open_blockers"] == 0


def test_full_proposal_restart_reuses_completed_group_and_does_not_unlock_export(runtime):
    settings, pack, db, _, builder, executor, engine, _ = runtime
    project_id = create_project(db, internet=False)
    add_standard_materials(settings, db, project_id, current_sections=FULL_PROPOSAL_TITLES)

    async def checkpoint_one_group():
        await _prepare(engine, project_id)
        workflow = engine.start(project_id, "WF-4_PROPOSAL_AUTHORING", FULL_PROPOSAL_OPTIONS)
        current = workflow
        # Advance only through the architecture and planning gates.  Stop at the
        # persisted WRITE_SECTIONS boundary before the parent launches all groups.
        for _ in range(30):
            if current["current_step"] == 5 and current["status"] == "RUNNING":
                break
            current = await engine.advance(current["id"])
            if current["status"] == "WAITING_GATE":
                _approve_open_gate(engine, current["id"])
                current = engine.get(current["id"])
        assert current["current_step"] == 5
        assert current["status"] == "RUNNING"

        state = current["state"]
        engine._target_sections(project_id, state.get("options") or {}, state)
        contract = state["full_proposal_contract"]
        group = next(
            item for item in contract["groups"]
            if item["group_id"] == "GROUP_1_BACKGROUND_AND_PROBLEM"
        )
        record = engine._create_full_proposal_child(current, state, group)
        child = await engine._run_full_proposal_group(current, state, record, set())
        assert child["status"] == "COMPLETED"
        # Simulate a process stop after the child transaction is durable but before
        # the coordinator launches or aggregates the remaining groups.
        return engine.get(current["id"]), child

    checkpoint, child = asyncio.run(asyncio.wait_for(checkpoint_one_group(), timeout=90))
    assert checkpoint["status"] == "RUNNING"
    before = db.fetchone(
        "SELECT COUNT(*) AS n FROM prompt_runs WHERE workflow_id=?",
        (child["id"],),
    )["n"]

    premature_export = engine.start(project_id, "WF-5_SECURITY_REVIEW_AND_EXPORT")
    assert premature_export["status"] == "WAITING_PREREQUISITE"
    assert "WF-4_PROPOSAL_AUTHORING" in premature_export["state"]["last_error"]

    restarted = WorkflowEngine(
        db,
        pack,
        builder,
        executor,
        engine.research_service,
        quality_manager=QualityLifecycleManager(db),
    )
    completed = asyncio.run(
        asyncio.wait_for(_run_parent(restarted, restarted.get(checkpoint["id"])), timeout=120)
    )
    assert completed["status"] == "COMPLETED", completed["state"].get("last_error")
    after = db.fetchone(
        "SELECT COUNT(*) AS n FROM prompt_runs WHERE workflow_id=?",
        (child["id"],),
    )["n"]
    assert after == before, "completed group was regenerated after restart"


def test_full_document_finding_rewrites_only_responsible_section(runtime):
    settings, _, db, _, _, executor, engine, _ = runtime
    project_id = create_project(db, internet=False)
    add_standard_materials(settings, db, project_id, current_sections=FULL_PROPOSAL_TITLES)
    calls = _inject_one_full_document_conflict(executor)

    async def scenario():
        await _prepare(engine, project_id)
        workflow = engine.start(project_id, "WF-4_PROPOSAL_AUTHORING", FULL_PROPOSAL_OPTIONS)
        return await _run_parent(engine, workflow)

    completed = asyncio.run(asyncio.wait_for(scenario(), timeout=120))
    assert completed["status"] == "COMPLETED", completed["state"].get("last_error")
    state = completed["state"]
    assert [item["status"] for item in state["full_proposal_review_history"]] == ["REVISE", "PASS"]
    child_ids = sorted({
        workflow_id
        for record in state["full_proposal_children"].values()
        for workflow_id in [
            str(record.get("workflow_id") or ""),
            *(str(item) for item in record.get("superseded_workflow_ids") or []),
        ]
        if workflow_id
    })
    counts = _prompt_counts_by_section(db, project_id, child_ids, "P-WRITE-CONTENT")
    assert counts["技术路线"] == 2
    assert all(counts[title] == 1 for title in FULL_PROPOSAL_TITLES if title != "技术路线")
    history = state["cross_section_repair_history"]
    assert history[0]["responsible_section_ids"] == [calls["target_section_id"]]
    target = next(
        item for item in engine.quality_manager.list_findings(project_id, workflow_id=completed["id"])
        if item["finding"]["code"] == "FULL_PROPOSAL_TECHNICAL_ROUTE_CONFLICT"
    )
    assert target["lifecycle"]["state"] == "VERIFIED"
    assert target["lifecycle"]["repair_evidence"]
    assert target["lifecycle"]["review_evidence"]
