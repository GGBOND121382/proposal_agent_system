from __future__ import annotations

import asyncio
import json
from pathlib import Path

from app.config import Settings
from app.context import ContextBuilder
from app.db import Database
from app.documents import parse_document
from app.executor import PromptExecutor
from app.llm import ModelGateway
from app.pack import PromptPack
from app.research import PublicResearchService
from app.security import SecurityRouter
from app.simulated_llm import SimulatedLLM
from app.util import new_id, utc_now
from app.workflows import WorkflowEngine


def _runtime(tmp_path: Path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("MODEL_RUNTIME_MODE", "SIMULATED")
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PROMPT_PACK_DIR", str(root / "prompt_pack"))
    settings = Settings.load()
    pack = PromptPack(settings.prompt_pack_dir)
    db = Database(settings.db_path)
    router = SecurityRouter(pack)
    gateway = ModelGateway(settings, pack)
    builder = ContextBuilder(db, pack)
    executor = PromptExecutor(db, pack, router, gateway)
    engine = WorkflowEngine(db, pack, builder, executor, PublicResearchService(settings))
    return settings, pack, db, builder, executor, engine


def _project(db: Database) -> str:
    project_id = new_id("project")
    now = utc_now()
    config = {
        "internet_access_allowed": True,
        "anonymized_external_processing_allowed": True,
        "allowed_public_topics": ["智能体系统", "资源调度"],
        "prohibited_external_fields": ["人员姓名", "组织名称"],
        "recipient_scope": ["内部测试"],
        "allowed_model_endpoint_ids": ["offline-primary", "online-public-primary"],
        "external_redaction_entities": [
            {"value": "陈远航", "entity_type": "PERSON", "placeholder": "[PERSON_1]", "field_label": "人员姓名"},
            {"value": "星舟智能系统研究室", "entity_type": "ORG", "placeholder": "[ORG_1]", "field_label": "组织名称"},
        ],
        "retention_days": 365,
        "task_instruction": None,
    }
    db.execute(
        "INSERT INTO projects(id,name,description,security_level,config_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        (project_id, "后勤保障智能体测试", "", "INTERNAL", json.dumps(config, ensure_ascii=False), now, now),
    )
    return project_id


def _draft(settings: Settings, db: Database, project_id: str) -> None:
    raw = "# 全文\n测试。\n# 项目摘要\n待编写。\n# 参考文献\n待编写。\n".encode("utf-8")
    parsed = parse_document("draft.md", raw, "CURRENT_PROPOSAL", "INTERNAL")
    path = settings.uploads_dir / "draft.md"
    path.write_bytes(raw)
    db.execute(
        "INSERT INTO documents(id,project_id,filename,role,security_level,document_hash,file_path,parsed_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (parsed["document_id"], project_id, path.name, "CURRENT_PROPOSAL", "INTERNAL", parsed["document_hash"], str(path), json.dumps(parsed, ensure_ascii=False), utc_now()),
    )


def test_all_26_simulated_outputs_remain_schema_valid(tmp_path, monkeypatch):
    _, pack, *_ = _runtime(tmp_path, monkeypatch)
    simulator = SimulatedLLM(pack)
    assert len(pack.prompt_ids()) == 26
    for prompt_id in pack.prompt_ids():
        envelope = pack.replay_input(prompt_id)
        output = simulator.invoke(prompt_id, envelope)
        assert pack.validate(prompt_id, "output", output) == [], prompt_id


def test_targeted_repair_is_really_executed_and_trace_is_complete(tmp_path, monkeypatch):
    settings, _, db, _, _, engine = _runtime(tmp_path, monkeypatch)
    project_id = _project(db)
    _draft(settings, db, project_id)

    async def finish():
        wf = engine.start(project_id, "WF-4_PROPOSAL_AUTHORING")
        for _ in range(200):
            wf = await engine.advance(wf["id"])
            if wf["status"] == "WAITING_GATE":
                gate = next(g for g in engine.list_gates(workflow_id=wf["id"]) if g["status"] == "OPEN")
                action = "APPROVE" if "APPROVE" in gate["allowed_actions"] else "CONFIRM"
                engine.decide_gate(gate["id"], action=action, decided_by="pytest", decided_role=gate["required_role"])
                continue
            if wf["status"] in {"COMPLETED", "BLOCKED"}:
                break
        assert wf["status"] == "COMPLETED", wf["state"].get("last_error")

    asyncio.run(finish())
    repair_runs = db.fetchall("SELECT * FROM prompt_runs WHERE project_id=? AND prompt_id='P-TARGETED-REPAIR'", (project_id,))
    assert len(repair_runs) == 1
    critic_runs = db.fetchall("SELECT status FROM prompt_runs WHERE project_id=? AND prompt_id='P-REVISION-PLAN-CRITIC' ORDER BY created_at,id", (project_id,))
    assert [row["status"] for row in critic_runs] == ["REVISE", "PASS"]

    run_count = db.fetchone("SELECT COUNT(*) AS n FROM prompt_runs WHERE project_id=?", (project_id,))["n"]
    traces = db.fetchall("SELECT content_json,status FROM artifacts WHERE project_id=? AND artifact_type='PROMPT_TRACE'", (project_id,))
    assert len(traces) == run_count
    for row in traces:
        payload = json.loads(row["content_json"])
        for key in ["prompt_id", "status", "duration_ms", "system_prompt", "input_envelope", "output_schema", "raw_response_text", "environment", "model_id", "endpoint_id"]:
            assert payload.get(key) is not None, (payload.get("prompt_id"), key)


def test_public_research_claims_enter_writing_context_without_private_values(tmp_path, monkeypatch):
    settings, pack, db, builder, executor, engine = _runtime(tmp_path, monkeypatch)
    project_id = _project(db)
    _draft(settings, db, project_id)

    async def finish_hybrid():
        wf = engine.start(project_id, "WF-3_HYBRID_ONLINE_ASSIST")
        for _ in range(100):
            wf = await engine.advance(wf["id"])
            if wf["status"] == "WAITING_GATE":
                gate = next(g for g in engine.list_gates(workflow_id=wf["id"]) if g["status"] == "OPEN")
                action = "APPROVE" if "APPROVE" in gate["allowed_actions"] else "CONFIRM"
                engine.decide_gate(gate["id"], action=action, decided_by="pytest", decided_role=gate["required_role"])
                continue
            if wf["status"] in {"COMPLETED", "BLOCKED"}:
                break
        assert wf["status"] == "COMPLETED", wf["state"].get("last_error")

    asyncio.run(finish_hybrid())
    online_runs = db.fetchall("SELECT input_json FROM prompt_runs WHERE project_id=?", (project_id,))
    public_inputs = [json.loads(r["input_json"]) for r in online_runs if json.loads(r["input_json"])["security_context"]["required_environment"] == "ONLINE_PUBLIC"]
    serialized = json.dumps(public_inputs, ensure_ascii=False)
    assert "陈远航" not in serialized
    assert "星舟智能系统研究室" not in serialized

    envelope = builder.build("P-WRITE-BLUEPRINT", project_id)
    facts = envelope["payload"].get("confirmed_facts", [])
    assert any(item.get("claim_type") == "PUBLIC_CLAIM" for item in facts)
    assert pack.validate("P-WRITE-BLUEPRINT", "input", envelope) == []
