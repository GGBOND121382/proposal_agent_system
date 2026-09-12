"""Manual genre must survive context projection and all WF-1 quality gates."""
from __future__ import annotations

import copy
import json

import pytest

from app.api_models import ProjectDocumentTypeUpdate
from app.model_semantic_contracts import build_semantic_model_input, expand_semantic_model_output
from app.proposal_quality import ProposalQualityGuard, _document_kind_hint
from test_wf1_semantic_boundary import PACK, WF1, _envelope, _pd_extract_output
from test_wf1_quality_gate_adaptation import runtime, create_project, add_standard_materials


@pytest.mark.parametrize("kind, expected", [
    ("SURVEY_REPORT", "RESEARCH_REPORT"),
    ("RESEARCH_PROPOSAL", "APPLICATION"),
    ("ENGINEERING_PROPOSAL", "APPLICATION"),
])
def test_manual_type_overrides_ambiguous_task_book_text(kind, expected):
    payload = {
        "document_type": kind,
        "scheme_profile": {
            "scheme_type": "调研分析报告（项目内部立项任务）",
            "research_attribute": "非指南类任务书，不涉及指南方向属性",
        },
    }
    assert _document_kind_hint(payload) == expected


@pytest.mark.parametrize("prompt_id", WF1)
def test_manual_type_reaches_model_with_valid_contract(prompt_id):
    envelope = _envelope(prompt_id)
    envelope["payload"]["document_type"] = "SURVEY_REPORT"
    assert PACK.validate_structure(prompt_id, "input", envelope) == []
    projected = build_semantic_model_input(prompt_id, envelope)
    assert projected["document_type"] == "SURVEY_REPORT"
    assert PACK.validate_model(prompt_id, "input", projected) == []


def test_report_empty_graph_is_advisory_but_bad_evidence_still_blocks(runtime):
    settings, _, db, _, builder, _, _, _ = runtime
    project_id = create_project(db)
    add_standard_materials(settings, db, project_id)
    envelope = builder.build("P-PROJECT-DEFINITION-EXTRACT", project_id)
    envelope["payload"]["document_type"] = "SURVEY_REPORT"
    # Deliberately contradictory model genre cannot override the manual choice.
    semantic = _pd_extract_output(document_kind="RESEARCH_PROPOSAL", relations=[])
    semantic["items"] = [i for i in semantic["items"] if i["item_type"] != "GAP"]
    semantic["argument_seed"]["research_questions"][0]["gap_keys"] = []
    output = expand_semantic_model_output("P-PROJECT-DEFINITION-EXTRACT", envelope, semantic)
    assert PACK.validate_structure("P-PROJECT-DEFINITION-EXTRACT", "output", output) == []
    assert output["result"]["proposal_contract"]["document_type"] == "TECHNICAL_REPORT"
    guard = ProposalQualityGuard()
    findings = guard.observe("P-PROJECT-DEFINITION-EXTRACT", envelope, output)["findings"]
    assert not any(f["blocking"] for f in findings)
    broken = copy.deepcopy(output)
    item = broken["result"]["project_definition"]["items"][0]
    item["knowledge_status"] = "CONFIRMED"
    item["source_refs"] = []
    findings = guard.observe("P-PROJECT-DEFINITION-EXTRACT", envelope, broken)["findings"]
    assert any(f["blocking"] and f["code"] == "QG_CONFIRMED_ITEM_WITHOUT_EVIDENCE" for f in findings)


def test_readiness_uses_same_manual_type_and_keeps_proposal_strict():
    payload = {"document_type": "SURVEY_REPORT", "project_definition": {"items": [], "relations": []},
               "readiness_stage": "READY_FOR_ARGUMENT_ARCHITECTURE"}
    output = {"result": {"assessed_stage": "READY_FOR_ARGUMENT_ARCHITECTURE",
                         "ready_for_argument_architecture": True, "ready_for_section_planning": False}}
    guard = ProposalQualityGuard()
    assert not any(f.blocking for f in guard._audit_readiness(payload, output))
    payload["document_type"] = "RESEARCH_PROPOSAL"
    assert any(f.blocking and f.code == "QG_PROJECT_GRAPH_INCOMPLETE"
               for f in guard._audit_readiness(payload, output))


@pytest.mark.parametrize("mode", ["SIMULATED", "LIVE"])
def test_project_configuration_reaches_real_context_builder(runtime, mode):
    settings, pack, db, _, builder, _, _, _ = runtime
    project_id = create_project(db)
    add_standard_materials(settings, db, project_id)
    builder.runtime_mode = mode
    config = json.loads(db.fetchone("SELECT config_json FROM projects WHERE id=?", (project_id,))["config_json"])
    config["document_type"] = "SURVEY_REPORT"
    db.execute("UPDATE projects SET config_json=? WHERE id=?", (json.dumps(config), project_id))
    # P-SCHEME-EXTRACT is the first consumer of the manual choice and has no
    # upstream-artifact scaffold, so it is buildable under LIVE with an empty
    # library. PD-extract projection of the same field is covered by
    # test_manual_type_reaches_model_with_valid_contract.
    envelope = builder.build("P-SCHEME-EXTRACT", project_id)
    assert envelope["payload"]["document_type"] == "SURVEY_REPORT"
    assert pack.validate_structure("P-SCHEME-EXTRACT", "input", envelope) == []


def test_live_context_does_not_invent_manual_choice_for_legacy_project(runtime):
    settings, _, db, _, builder, _, _, _ = runtime
    project_id = create_project(db)
    add_standard_materials(settings, db, project_id)
    builder.runtime_mode = "LIVE"
    envelope = builder.build("P-SCHEME-EXTRACT", project_id)
    assert "document_type" not in envelope["payload"]


def test_type_update_rejects_unknown_enum():
    with pytest.raises(ValueError):
        ProjectDocumentTypeUpdate(document_type="AUTO_GUESS")


def test_document_type_api_persists_choice_and_preserves_other_config(runtime, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main

    _, _, db, _, _, _, _, _ = runtime
    monkeypatch.setattr(main, "db", db)
    client = TestClient(main.app)
    response = client.post("/api/projects", json={"name": "DASH 调研", "document_type": "SURVEY_REPORT"})
    assert response.status_code == 200
    project = response.json()
    assert project["config"]["document_type"] == "SURVEY_REPORT"
    previous = project["config"].copy()
    response = client.patch(f"/api/projects/{project['id']}/document-type", json={"document_type": "RESEARCH_PROPOSAL"})
    assert response.status_code == 200
    assert response.json()["config"] == {**previous, "document_type": "RESEARCH_PROPOSAL"}
    assert client.get(f"/api/projects/{project['id']}").json()["config"]["document_type"] == "RESEARCH_PROPOSAL"
    assert client.patch(f"/api/projects/{project['id']}/document-type", json={"document_type": "UNKNOWN"}).status_code == 422
    assert client.patch("/api/projects/nonexistent/document-type", json={"document_type": "SURVEY_REPORT"}).status_code == 404
