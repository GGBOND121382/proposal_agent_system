from __future__ import annotations

import asyncio
import copy
import json
from collections import Counter

from app.quality import QualityLifecycleManager
from app.track_b import TrackBAgentPromptValidator
from app.workflows import WorkflowEngine
from tests.test_runtime import add_standard_materials, create_project, finish_workflow, runtime


THREE_SECTION_OPTIONS = {
    "three_section_cross_chapter": True,
    "integration_scope": "THREE_SECTION_CROSS_CHAPTER",
}


def _approve_open_gate(engine: WorkflowEngine, workflow_id: str) -> None:
    gate = next(item for item in engine.list_gates(workflow_id=workflow_id) if item["status"] == "OPEN")
    action = "APPROVE" if "APPROVE" in gate["allowed_actions"] else "CONFIRM"
    engine.decide_gate(
        gate["id"],
        action=action,
        decided_by="g2-three-section-test",
        decided_role=gate["required_role"],
    )


def _inject_one_cross_section_conflict(executor):
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
        output["result"]["routing_actions"] = [
            {
                "finding_code": "G2_TECHNICAL_ROUTE_TERM_CONFLICT",
                "route": "WRITING_AGENT",
                "reason": "技术路线章节使用的核心术语与冻结的跨章合同不一致，应由原写作责任章节定向重写。",
            }
        ]
        output["findings"] = [
            {
                "code": "G2_TECHNICAL_ROUTE_TERM_CONFLICT",
                "severity": "P1",
                "category": "INTEGRATION",
                "target_type": "SECTION_CANDIDATE",
                "target_path_or_span": f"candidate_sections.{target_id}.paragraphs",
                "description": "技术路线章节对低扰动增量优化的表述偏离背景和研究内容中已冻结的中心术语。",
                "evidence_refs": [target_id],
                "repairable": True,
                "repair_instruction": "仅重写技术路线章节中对应段落，统一中心术语并保持事实、数字和章节合同不变。",
                "suggested_route": "WRITING_AGENT",
                "blocking": True,
            }
        ]
        return output

    simulator._handle_integration_critic = injected
    return calls


def test_three_section_chain_rejects_missing_required_role(runtime):
    settings, _, db, _, _, _, engine, _ = runtime
    project_id = create_project(db, internet=False)
    add_standard_materials(settings, db, project_id, current_sections=["立项依据", "研究内容"])

    async def scenario():
        for workflow_type in ["WF-1_PROJECT_INTAKE", "WF-2_TEMPLATE_EXTRACTION"]:
            workflow = await finish_workflow(engine, project_id, workflow_type)
            assert workflow["status"] == "COMPLETED", workflow["state"].get("last_error")
        workflow = engine.start(project_id, "WF-4_PROPOSAL_AUTHORING", THREE_SECTION_OPTIONS)
        for _ in range(50):
            workflow = await engine.advance(workflow["id"])
            if workflow["status"] == "WAITING_GATE":
                _approve_open_gate(engine, workflow["id"])
                continue
            if workflow["status"] in {"BLOCKED", "COMPLETED"}:
                return workflow
        return workflow

    workflow = asyncio.run(scenario())
    assert workflow["status"] == "BLOCKED"
    assert "缺少章节角色：TECHNICAL_ROUTE" in workflow["state"]["last_error"]


def test_three_section_cross_chapter_repair_review_and_restart(runtime):
    settings, pack, db, _, builder, executor, engine, _ = runtime
    project_id = create_project(db, internet=False)
    add_standard_materials(
        settings,
        db,
        project_id,
        current_sections=["立项依据", "研究内容", "技术路线"],
    )
    calls = _inject_one_cross_section_conflict(executor)

    async def prepare():
        for workflow_type in ["WF-1_PROJECT_INTAKE", "WF-2_TEMPLATE_EXTRACTION"]:
            workflow = await finish_workflow(engine, project_id, workflow_type)
            assert workflow["status"] == "COMPLETED", workflow["state"].get("last_error")

    asyncio.run(prepare())
    executor.quality_guard = TrackBAgentPromptValidator(pack)
    workflow = engine.start(project_id, "WF-4_PROPOSAL_AUTHORING", THREE_SECTION_OPTIONS)
    original_write_sections = engine._write_sections
    interrupted = {"done": False}

    async def interrupt_after_repair_checkpoint(wf, state):
        if state.get("integration_repair_rounds") == 1 and not interrupted["done"]:
            interrupted["done"] = True
            raise RuntimeError("simulated process interruption after cross-section repair scheduling")
        return await original_write_sections(wf, state)

    engine._write_sections = interrupt_after_repair_checkpoint

    async def run_until_interruption():
        current = workflow
        for _ in range(100):
            try:
                current = await engine.advance(current["id"])
            except RuntimeError as exc:
                assert "simulated process interruption" in str(exc)
                return engine.get(current["id"])
            if current["status"] == "WAITING_GATE":
                _approve_open_gate(engine, current["id"])
                continue
            if current["status"] in {"BLOCKED", "COMPLETED"}:
                return current
        return current

    checkpoint = asyncio.run(run_until_interruption())
    assert interrupted["done"] is True
    assert checkpoint["status"] == "RUNNING"
    assert checkpoint["state"]["integration_repair_rounds"] == 1
    assert checkpoint["state"]["integration_repair_section_ids"] == [calls["target_section_id"]]

    restarted = WorkflowEngine(
        db,
        pack,
        builder,
        executor,
        engine.research_service,
        quality_manager=QualityLifecycleManager(db),
    )

    async def resume():
        current = restarted.get(workflow["id"])
        for _ in range(100):
            current = await restarted.advance(current["id"])
            if current["status"] == "WAITING_GATE":
                _approve_open_gate(restarted, current["id"])
                continue
            if current["status"] in {"BLOCKED", "COMPLETED"}:
                return current
        return current

    completed = asyncio.run(resume())
    assert completed["status"] == "COMPLETED", completed["state"].get("last_error")
    contract = completed["state"]["three_section_contract"]
    assert [item["profile_id"] for item in contract["sections"]] == [
        "BACKGROUND_AND_SIGNIFICANCE",
        "RESEARCH_CONTENT",
        "TECHNICAL_ROUTE",
    ]
    assert [item["status"] for item in completed["state"]["cross_section_review_history"]] == [
        "REVISE",
        "PASS",
    ]
    repair = completed["state"]["cross_section_repair_history"]
    assert repair == [
        {
            "round": 1,
            "finding_codes": [
                "G2_TECHNICAL_ROUTE_TERM_CONFLICT",
                "QG_CROSS_SECTION_VALUE_CONFLICT",
            ],
            "responsible_section_ids": [calls["target_section_id"]],
            "route": "WRITING_AGENT",
        }
    ]

    rows = db.fetchall(
        "SELECT input_json FROM prompt_runs WHERE project_id=? AND workflow_id=? AND prompt_id='P-WRITE-CONTENT' AND status='PASS'",
        (project_id, workflow["id"]),
    )
    counts = Counter(
        str((json.loads(row["input_json"]).get("payload") or {}).get("source_section", {}).get("title") or "")
        for row in rows
    )
    assert counts == Counter({"技术路线": 2, "立项依据": 1, "研究内容": 1})

    findings = restarted.quality_manager.list_findings(project_id, workflow_id=workflow["id"])
    target = next(item for item in findings if item["finding"]["code"] == "G2_TECHNICAL_ROUTE_TERM_CONFLICT")
    assert target["lifecycle"]["state"] == "VERIFIED"
    assert target["lifecycle"]["repair_evidence"]
    assert target["lifecycle"]["review_evidence"]
    assert restarted.quality_manager.quality_matrix(project_id, workflow_id=workflow["id"])["open_blockers"] == 0


def test_integrated_api_routes_remain_available(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_RUNTIME_MODE", "SIMULATED")
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "api-data"))
    from app.main import app

    paths = {route.path for route in app.routes}
    required = {
        "/api/workflows",
        "/api/workflows/{workflow_id}/advance",
        "/api/gates",
        "/api/gates/{gate_id}/decide",
        "/api/runs",
        "/api/runs/{run_id}",
        "/api/projects/{project_id}/quality-findings",
        "/api/projects/{project_id}/quality-matrix",
        "/api/projects/{project_id}/export",
        "/api/projects/{project_id}/export-package",
    }
    assert required <= paths
