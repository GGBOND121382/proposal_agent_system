from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import Settings
from app.db import Database
from app.proposal_quality import ProposalQualityGuard
from app.skill_setup import build_skill_executor
from app.skills.base import SkillContext
from app.skills.mermaid import MermaidRenderError, MermaidRenderSkill
from app.skills.public_research import PublicResearchArchiveSkill

ROOT = Path(__file__).resolve().parents[1]


def settings_for(tmp_path: Path, monkeypatch) -> Settings:
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PROMPT_PACK_DIR", str(ROOT / "prompt_pack"))
    monkeypatch.setenv("MODEL_RUNTIME_MODE", "SIMULATED")
    return Settings.load()


def test_skill_registry_contains_research_and_mermaid(tmp_path: Path, monkeypatch):
    settings = settings_for(tmp_path, monkeypatch)
    executor = build_skill_executor(Database(settings.db_path), settings)
    skill_ids = {item["skill_id"] for item in executor.registry.list()}
    assert {"public_research.archive", "mermaid.render"} <= skill_ids


def test_recorded_research_archives_verifiable_hashes(tmp_path: Path, monkeypatch):
    settings = settings_for(tmp_path, monkeypatch)
    record_file = tmp_path / "recorded.json"
    record_file.write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "source_id": "public-src-f",
                        "title": "Public test source",
                        "url": "https://example.com/f-evidence",
                        "content_text": "This public test source contains enough material for deterministic archive verification.",
                        "matched_query": "F evidence",
                        "authority_rank": 60,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    skill = PublicResearchArchiveSkill(settings)
    result = skill.run(
        {
            "provider": "recorded",
            "record_file": str(record_file),
            "plan": {"queries": [{"query": "F evidence"}]},
            "max_results": 1,
        },
        SkillContext(
            project_id="project-f-components",
            workflow_id="workflow-f-components",
            security_level="PUBLIC",
            data_dir=str(settings.data_dir),
        ),
    )
    assert result.status == "PASS"
    manifest = json.loads(Path(result.output["archive_manifest"]).read_text(encoding="utf-8"))
    assert manifest["source_count"] == 1
    record = manifest["records"][0]
    assert len(record["snapshot_sha256"]) == 64
    assert len(record["text_sha256"]) == 64
    assert Path(record["raw_path"]).is_file()
    assert Path(record["text_path"]).is_file()


def test_mermaid_source_contract_accepts_safe_and_blocks_active_content(tmp_path: Path, monkeypatch):
    settings = settings_for(tmp_path, monkeypatch)
    skill = MermaidRenderSkill(settings)
    try:
        skill._validate_source("flowchart LR\nA[Input] --> B[Evidence]")
        with pytest.raises(MermaidRenderError):
            skill._validate_source("flowchart LR\nA --> B\nclick A javascript:alert(1)")
    finally:
        skill.close()


def test_quality_guard_rejects_shallow_unsupported_project_graph():
    output = {
        "status": "PASS",
        "result": {
            "project_definition": {
                "items": [
                    {
                        "item_id": "objective-1",
                        "item_type": "OBJECTIVE",
                        "knowledge_status": "CONFIRMED",
                        "content": "构建一个系统原型",
                        "source_refs": [],
                    }
                ],
                "relations": [],
            }
        },
        "findings": [],
        "unresolved_items": [],
        "user_questions": [],
        "source_refs": [],
        "warnings": [],
    }
    checked = ProposalQualityGuard().apply(
        "P-PROJECT-DEFINITION-EXTRACT", {"payload": {}}, output
    )
    codes = {item["code"] for item in checked["findings"]}
    assert checked["status"] == "REVISE"
    assert "QG_PROJECT_GRAPH_INCOMPLETE" in codes
    assert "QG_PROJECT_GRAPH_TOO_SHALLOW" in codes
    assert "QG_CONFIRMED_ITEM_WITHOUT_EVIDENCE" in codes
