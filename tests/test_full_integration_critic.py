from __future__ import annotations

import asyncio
import copy
import json
import os
from pathlib import Path

import pytest

from app.config import Settings
from app.context import ContextBuilder
from app.db import Database
from app.executor import PromptExecutor
from app.exporter import DocxExporter
from app.llm import ModelGateway
from app.pack import PromptPack
from app.research import PublicResearchService
from app.security import SecurityRouter
from app.util import utc_now
from app.workflows import WorkflowEngine
from tests.test_full_proposal_concurrent import (
    FULL_PROPOSAL_OPTIONS,
    FULL_PROPOSAL_TITLES,
    _prepare,
    _run_parent,
)
from tests.test_runtime import add_standard_materials, create_project


def _build_runtime(data_dir: Path):
    root = Path(__file__).resolve().parents[1]
    os.environ["MODEL_RUNTIME_MODE"] = "SIMULATED"
    os.environ["APP_DATA_DIR"] = str(data_dir)
    os.environ["PROMPT_PACK_DIR"] = str(root / "prompt_pack")
    settings = Settings.load()
    pack = PromptPack(settings.prompt_pack_dir)
    db = Database(settings.db_path)
    router = SecurityRouter(pack)
    gateway = ModelGateway(settings, pack)
    builder = ContextBuilder(db, pack)
    executor = PromptExecutor(db, pack, router, gateway)
    engine = WorkflowEngine(db, pack, builder, executor, PublicResearchService(settings))
    exporter = DocxExporter(db, settings)
    return settings, pack, db, router, builder, executor, engine, exporter


@pytest.fixture(scope="module")
def completed_full_integration(tmp_path_factory):
    """Generate the 14-section baseline once; tests only inspect or restore mutations."""
    previous = {name: os.environ.get(name) for name in ("MODEL_RUNTIME_MODE", "APP_DATA_DIR", "PROMPT_PACK_DIR")}
    runtime = _build_runtime(tmp_path_factory.mktemp("full-integration") / "data")
    settings, _, db, _, _, _, engine, _ = runtime
    project_id = create_project(db, internet=False)
    add_standard_materials(settings, db, project_id, current_sections=FULL_PROPOSAL_TITLES)

    async def scenario():
        await _prepare(engine, project_id)
        workflow = engine.start(project_id, "WF-4_PROPOSAL_AUTHORING", FULL_PROPOSAL_OPTIONS)
        return await _run_parent(engine, workflow)

    completed = asyncio.run(asyncio.wait_for(scenario(), timeout=150))
    assert completed["status"] == "COMPLETED", completed["state"].get("last_error")
    yield runtime, project_id, completed
    for name, value in previous.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def _latest_integration_run(db, workflow_id: str) -> dict:
    row = db.fetchone(
        """SELECT * FROM prompt_runs
           WHERE workflow_id=? AND prompt_id='P-INTEGRATION-CRITIC'
           ORDER BY created_at DESC,id DESC LIMIT 1""",
        (workflow_id,),
    )
    assert row
    row["input"] = json.loads(row["input_json"])
    row["output"] = json.loads(row["output_json"])
    return row


def test_full_integration_review_records_complete_provenance(completed_full_integration):
    runtime, project_id, completed = completed_full_integration
    _, _, _, _, _, _, engine, _ = runtime
    history = completed["state"]["full_proposal_review_history"]
    assert len(history) == 1
    review = history[0]
    assert review["status"] == "PASS"
    assert review["section_count"] == len(FULL_PROPOSAL_TITLES)
    assert len(review["section_manifest"]) == len(FULL_PROPOSAL_TITLES)
    assert len(review["child_workflow_ids"]) == 5
    assert all(review["checks"].values())
    assert review["input_hash"] and review["output_hash"]
    assert review["model_id"] and review["endpoint_id"]
    assert all(item["polish_run_id"] != item["expression_critic_run_id"] for item in review["section_manifest"])
    assert engine.quality_manager.quality_matrix(project_id, workflow_id=completed["id"])["open_blockers"] == 0


def test_full_integration_candidate_identity_and_hash_are_deterministic(completed_full_integration):
    runtime, _, completed = completed_full_integration
    _, _, db, _, _, _, engine, _ = runtime
    run = _latest_integration_run(db, completed["id"])
    state_a = copy.deepcopy(completed["state"])
    state_b = copy.deepcopy(completed["state"])
    engine._validate_full_proposal_integration_envelope(state_a, copy.deepcopy(run["input"]))
    engine._validate_full_proposal_integration_envelope(state_b, copy.deepcopy(run["input"]))
    assert state_a["full_integration_input_snapshot"]["candidate_set_hash"] == state_b["full_integration_input_snapshot"]["candidate_set_hash"]

    broken = copy.deepcopy(run["input"])
    broken["payload"]["document_section_map"][0]["candidate_id"] = "candidate-tampered"
    with pytest.raises(ValueError, match="candidate_id"):
        engine._validate_full_proposal_integration_envelope(copy.deepcopy(completed["state"]), broken)


def test_full_integration_rejects_candidate_without_expression_critic_provenance(completed_full_integration):
    runtime, _, completed = completed_full_integration
    _, _, db, _, _, _, engine, _ = runtime
    run = _latest_integration_run(db, completed["id"])
    first = run["input"]["payload"]["candidate_sections"][0]
    section_id = first["section_id"]
    candidate_id = first["candidate"]["candidate_id"]
    owner = next(
        item["producer_workflow_id"]
        for item in completed["state"]["full_proposal_review_history"][0]["section_manifest"]
        if item["section_id"] == section_id
    )
    rows = db.fetchall(
        "SELECT * FROM prompt_runs WHERE workflow_id=? AND prompt_id='P-EXPRESSION-CRITIC' AND status='PASS'",
        (owner,),
    )
    target = next(
        row for row in rows
        if str(((json.loads(row["input_json"]).get("payload") or {}).get("polished_candidate") or {}).get("candidate_id") or "") == candidate_id
    )
    db.execute("DELETE FROM prompt_runs WHERE id=?", (target["id"],))
    try:
        with pytest.raises(ValueError, match="Expression Polish"):
            engine._validate_full_proposal_integration_envelope(copy.deepcopy(completed["state"]), copy.deepcopy(run["input"]))
    finally:
        columns = list(target)
        placeholders = ",".join("?" for _ in columns)
        db.execute(
            f"INSERT INTO prompt_runs({','.join(columns)}) VALUES({placeholders})",
            tuple(target[column] for column in columns),
        )


def test_quality_guard_blocks_unknown_chain_ids_and_routes_finding(completed_full_integration):
    runtime, _, completed = completed_full_integration
    _, _, db, _, _, executor, _, _ = runtime
    run = _latest_integration_run(db, completed["id"])
    output = copy.deepcopy(run["output"])
    output["result"]["argument_chain_checks"][0]["source_ids"] = ["fabricated-gap"]
    checked = executor.quality_guard.apply("P-INTEGRATION-CRITIC", copy.deepcopy(run["input"]), output)
    codes = {item["code"] for item in checked["findings"]}
    assert "QG_ARGUMENT_CHAIN_ID_UNKNOWN" in codes
    assert checked["status"] == "BLOCK"
    route = next(item for item in checked["result"]["routing_actions"] if item["finding_code"] == "QG_ARGUMENT_CHAIN_ID_UNKNOWN")
    assert route["route"] == "INTEGRATION_AGENT"


def test_quality_guard_checks_innovation_foundation_and_metric_responsibilities(completed_full_integration):
    runtime, _, completed = completed_full_integration
    _, _, db, _, _, executor, _, _ = runtime
    run = _latest_integration_run(db, completed["id"])
    envelope = copy.deepcopy(run["input"])
    contracts = {
        item["section_id"]: item["profile_id"]
        for item in envelope["payload"]["narrative_architecture"]["section_contracts"]
    }
    for item in envelope["payload"]["candidate_sections"]:
        profile = contracts.get(item["section_id"])
        if profile == "INNOVATION":
            for paragraph in item["candidate"]["paragraphs"]:
                paragraph["evidence_ids"] = [x for x in paragraph.get("evidence_ids", []) if x != "prior-001"]
        if profile == "RESEARCH_FOUNDATION":
            for paragraph in item["candidate"]["paragraphs"]:
                paragraph["evidence_ids"] = [x for x in paragraph.get("evidence_ids", []) if x != "foundation-001"]
        if profile == "OUTPUTS_AND_METRICS":
            for paragraph in item["candidate"]["paragraphs"]:
                paragraph["evidence_ids"] = [x for x in paragraph.get("evidence_ids", []) if x != "experiment-001"]
    checked = executor.quality_guard.apply("P-INTEGRATION-CRITIC", envelope, copy.deepcopy(run["output"]))
    codes = {item["code"] for item in checked["findings"]}
    assert "QG_INNOVATION_SECTION_LACKS_BASELINE_BINDING" in codes
    assert "QG_FOUNDATION_SECTION_NOT_BOUND_TO_EVIDENCE" in codes
    assert "QG_METRIC_SECTION_LACKS_BASELINE_EVIDENCE" in codes
    assert checked["status"] == "REVISE"


def test_pass_after_repair_requires_changed_candidate_set_and_new_review(completed_full_integration):
    runtime, _, completed = completed_full_integration
    _, _, db, _, _, _, engine, _ = runtime
    latest = _latest_integration_run(db, completed["id"])
    state = copy.deepcopy(completed["state"])
    original_review = copy.deepcopy(state["full_proposal_review_history"][-1])
    original_review["status"] = "REVISE"
    state["full_proposal_review_history"] = [original_review]
    state["cross_section_repair_history"] = [{"round": 1, "responsible_section_ids": [state["section_results"][0]["section_id"]]}]
    state["full_integration_input_snapshot"] = {
        "contract_hash": original_review["contract_hash"],
        "candidate_set_hash": original_review["candidate_set_hash"],
        "section_count": original_review["section_count"],
        "child_workflow_ids": original_review["child_workflow_ids"],
        "sections": original_review["section_manifest"],
    }
    new_run_id = "run-independent-unchanged"
    db.execute(
        """INSERT INTO prompt_runs(id,project_id,workflow_id,prompt_id,status,model_id,endpoint_id,input_hash,output_hash,input_json,output_json,error,duration_ms,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            new_run_id, completed["project_id"], completed["id"], "P-INTEGRATION-CRITIC", "PASS",
            latest["model_id"], latest["endpoint_id"], latest["input_hash"], latest["output_hash"],
            latest["input_json"], latest["output_json"], None, 1, utc_now(),
        ),
    )
    try:
        with pytest.raises(ValueError, match="candidate_set_unchanged_after_repair"):
            engine._record_full_integration_review(
                completed,
                state,
                {"run_id": new_run_id, "status": "PASS", "output": copy.deepcopy(latest["output"])},
            )
    finally:
        db.execute("DELETE FROM prompt_runs WHERE id=?", (new_run_id,))
