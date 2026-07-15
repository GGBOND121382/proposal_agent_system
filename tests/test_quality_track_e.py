from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.db import Database
from app.exporter import DocxExporter, ExportDenied
from app.pack import PromptPack
from app.proposal_quality import ProposalQualityGuard
from app.quality import QualityGateBlocked, QualityLifecycleManager
from app.simulated_llm import SimulatedLLM
from app.util import utc_now


ROOT = Path(__file__).resolve().parents[1]


def _runtime_quality():
    pack = PromptPack(ROOT / "prompt_pack")
    return pack, SimulatedLLM(pack), ProposalQualityGuard()


def _db(tmp_path: Path) -> tuple[Database, str, str]:
    db = Database(tmp_path / "quality.sqlite3")
    project_id = "project-e"
    workflow_id = "wf-e"
    now = utc_now()
    db.execute(
        "INSERT INTO projects(id,name,description,security_level,config_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        (project_id, "Track E", "quality acceptance", "INTERNAL", "{}", now, now),
    )
    db.execute(
        "INSERT INTO workflows(id,project_id,workflow_type,status,current_step,state_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (workflow_id, project_id, "WF-4_PROPOSAL_AUTHORING", "RUNNING", 0, "{}", now, now),
    )
    return db, project_id, workflow_id


def _finding(code: str = "QG_TEST_BLOCKER", *, route: str = "PLANNING_AGENT") -> dict:
    return {
        "code": code,
        "severity": "P1",
        "category": "ARGUMENT",
        "target_type": "REVISION_PLAN",
        "target_path_or_span": "result.revision_plan",
        "description": "目标、任务、方法与指标未闭合。",
        "required_action": "返回规划阶段修复并复审。",
        "suggested_route": route,
        "blocking": True,
        "repairable": True,
        "evidence_refs": [],
    }


def _codes(output: dict) -> set[str]:
    return {str(item.get("code")) for item in output.get("findings", []) if isinstance(item, dict)}


def test_e1_relation_fact_metric_and_source_rules_are_deterministic():
    pack, sim, guard = _runtime_quality()

    project_env = pack.replay_input("P-PROJECT-DEFINITION-EXTRACT")
    project_output = sim.invoke("P-PROJECT-DEFINITION-EXTRACT", project_env)
    project = project_output["result"]["project_definition"]
    project["relations"][0]["target_item_type"] = "METRIC"
    metric = next(item for item in project["items"] if item["item_type"] == "METRIC")
    metric["content"]["verifier"] = ""
    checked_project = guard.apply("P-PROJECT-DEFINITION-EXTRACT", project_env, project_output)
    assert {
        "QG_RELATION_MATRIX_DIRECTION_INVALID",
        "QG_METRIC_BASIS_INCOMPLETE",
    }.issubset(_codes(checked_project))

    fact_env = pack.replay_input("P-FACT-EXTRACT")
    fact_output = sim.invoke("P-FACT-EXTRACT", fact_env)
    fact_output["result"]["fact_candidates"][0]["claim_text"] = "项目周期为36个月；项目经费为100万元。"
    fact_output["result"]["coverage"] = []
    checked_fact = guard.apply("P-FACT-EXTRACT", fact_env, fact_output)
    assert {"QG_FACT_NOT_ATOMIC", "QG_FACT_SOURCE_COVERAGE_INCOMPLETE"}.issubset(_codes(checked_fact))


def test_e2_section_gate_uses_profile_specific_responsibility():
    pack, sim, guard = _runtime_quality()
    env = pack.replay_input("P-WRITE-BLUEPRINT")
    env["payload"]["source_section"]["title"] = "创新点"
    env["payload"]["section_profile"]["profile_id"] = "RESEARCH_CONTENT"
    output = sim.invoke("P-WRITE-BLUEPRINT", env)
    checked = guard.apply("P-WRITE-BLUEPRINT", env, output)
    assert "QG_WRONG_SECTION_PROFILE" in _codes(checked)
    finding = next(item for item in checked["findings"] if item["code"] == "QG_WRONG_SECTION_PROFILE")
    assert finding["suggested_route"] in {"PLANNING_AGENT", "WRITING_AGENT"}


def test_e3_e4_integration_checks_conflict_mapping_and_full_argument_chain():
    pack, sim, guard = _runtime_quality()
    env = pack.replay_input("P-INTEGRATION-CRITIC")
    output = sim.invoke("P-INTEGRATION-CRITIC", env)
    output["result"]["terminology_checks"][0]["consistent"] = False
    output["result"]["numeric_checks"].append({
        "value_key": "project_duration_months",
        "values": [36, 48],
        "locations": ["section-001", "section-002"],
        "consistent": False,
        "resolution": None,
    })
    output["result"]["mapping_checks"].append({
        "mapping_type": "OBJECTIVE_TO_WORK_PACKAGE",
        "source_id": "objective-001",
        "target_ids": ["work-package-001"],
        "complete": False,
        "evidence": "missing",
    })
    output["result"]["argument_chain_checks"] = output["result"]["argument_chain_checks"][:-1]
    checked = guard.apply("P-INTEGRATION-CRITIC", env, output)
    assert {
        "QG_CROSS_SECTION_VALUE_CONFLICT",
        "QG_CROSS_SECTION_MAPPING_INCOMPLETE",
        "QG_ARGUMENT_CHAIN_NOT_CLOSED",
    }.issubset(_codes(checked))


def test_e5_delivery_findings_route_engineering_and_writing_separately(tmp_path: Path):
    db, project_id, workflow_id = _db(tmp_path)
    manager = QualityLifecycleManager(db)
    records = manager.ingest_delivery_findings(
        project_id=project_id,
        workflow_id=workflow_id,
        validation_run_id="delivery-review-1",
        findings=[
            {
                "code": "PDF_LAYOUT_OVERLAP",
                "category": "FORMAT",
                "target_type": "PDF_PAGE",
                "target_path_or_span": "page:7",
                "description": "图表重叠。",
            },
            {
                "code": "CONCLUSION_MISSING_OBJECTIVE",
                "category": "ARGUMENT",
                "target_type": "SECTION",
                "target_path_or_span": "section:conclusion",
                "description": "结论未回扣研究目标。",
            },
        ],
    )
    routes = {item["finding"]["code"]: item["responsibility"] for item in records}
    assert routes["PDF_LAYOUT_OVERLAP"]["owner"] == "EXPORT_ENGINEERING"
    assert routes["PDF_LAYOUT_OVERLAP"]["owner_kind"] == "ENGINEERING"
    assert routes["CONCLUSION_MISSING_OBJECTIVE"]["owner"] == "WRITING_AGENT"
    assert routes["CONCLUSION_MISSING_OBJECTIVE"]["workflow_type"] == "WF-4_PROPOSAL_AUTHORING"


def test_e6_p1_requires_repair_and_independent_critic_review(tmp_path: Path):
    db, project_id, workflow_id = _db(tmp_path)
    manager = QualityLifecycleManager(db)
    [record] = manager.observe_prompt_result(
        project_id=project_id,
        workflow_id=workflow_id,
        prompt_id="P-REVISION-PLAN-CRITIC",
        run_id="critic-open",
        status="REVISE",
        output={"findings": [_finding()]},
    )
    with pytest.raises(QualityGateBlocked):
        manager.assert_no_open_blockers(project_id)
    with pytest.raises(ValueError, match="repair evidence"):
        manager.verify_finding(
            record["finding_id"],
            project_id=project_id,
            reviewer="P-REVISION-PLAN-CRITIC",
            review_run_id="critic-review",
            review_hash="hash",
        )

    manager.record_targeted_repair(
        project_id=project_id,
        workflow_id=workflow_id,
        repair_run_id="repair-1",
        finding_codes=["QG_TEST_BLOCKER"],
    )
    with pytest.raises(ValueError, match="must be reviewed"):
        manager.verify_finding(
            record["finding_id"],
            project_id=project_id,
            reviewer="P-WRITE-CRITIC",
            review_run_id="critic-review",
            review_hash="hash",
        )
    with pytest.raises(ValueError, match="different runs"):
        manager.verify_finding(
            record["finding_id"],
            project_id=project_id,
            reviewer="P-REVISION-PLAN-CRITIC",
            review_run_id="repair-1",
            review_hash="hash",
        )

    manager.observe_prompt_result(
        project_id=project_id,
        workflow_id=workflow_id,
        prompt_id="P-REVISION-PLAN-CRITIC",
        run_id="critic-review",
        status="PASS",
        output={"findings": []},
    )
    assert manager.open_blockers(project_id) == []
    verified = manager.list_findings(project_id)[0]
    assert verified["lifecycle"]["state"] == "VERIFIED"
    assert verified["lifecycle"]["repair_evidence"][0]["run_id"] == "repair-1"
    assert verified["lifecycle"]["review_evidence"][0]["run_id"] == "critic-review"


def test_e6_export_gate_cannot_override_open_quality_blocker(tmp_path: Path):
    db, project_id, workflow_id = _db(tmp_path)
    manager = QualityLifecycleManager(db)
    manager.observe_prompt_result(
        project_id=project_id,
        workflow_id=workflow_id,
        prompt_id="P-REVISION-PLAN-CRITIC",
        run_id="critic-open",
        status="REVISE",
        output={"findings": [_finding("QG_EXPORT_BLOCKER")]},
    )
    now = utc_now()
    for index, gate_type in enumerate(["FINAL_CONTENT_SECURITY_APPROVAL", "FINAL_EXPORT_APPROVAL"]):
        db.execute(
            """INSERT INTO gates(id,project_id,workflow_id,gate_type,target_id,target_version,context_hash,question_version,
               required_role,allowed_actions_json,questions_json,security_level,status,decision_json,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                f"gate-{index}", project_id, workflow_id, gate_type, workflow_id, 1, "context", 1,
                "EXPORT_APPROVER", json.dumps(["APPROVE"]), "[]", "INTERNAL", "APPROVED",
                json.dumps({"action": "APPROVE"}), now, now,
            ),
        )
    exporter = DocxExporter(db, SimpleNamespace())
    with pytest.raises(ExportDenied, match="独立复审"):
        exporter._authorized_project(project_id)


def test_quality_matrix_is_auditable_and_append_only(tmp_path: Path):
    db, project_id, workflow_id = _db(tmp_path)
    manager = QualityLifecycleManager(db)
    manager.observe_prompt_result(
        project_id=project_id,
        workflow_id=workflow_id,
        prompt_id="P-REVISION-PLAN-CRITIC",
        run_id="critic-open",
        status="REVISE",
        output={"findings": [_finding()]},
    )
    manager.record_targeted_repair(
        project_id=project_id,
        workflow_id=workflow_id,
        repair_run_id="repair-1",
        finding_codes=["QG_TEST_BLOCKER"],
    )
    matrix = manager.quality_matrix(project_id)
    assert matrix["acceptance"] == "BLOCK"
    assert matrix["open_blockers"] == 1
    assert matrix["by_state"] == {"REPAIR_RECORDED": 1}
    rows = db.fetchall("SELECT version,status FROM artifacts WHERE artifact_type='QUALITY_FINDING' ORDER BY version")
    assert [row["version"] for row in rows] == [1, 2]
    assert [row["status"] for row in rows] == ["OPEN", "REPAIR_RECORDED"]
