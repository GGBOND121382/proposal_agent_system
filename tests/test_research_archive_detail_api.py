from __future__ import annotations

import dataclasses
import json

from fastapi.testclient import TestClient

import app.main as main


class _FakeDB:
    def __init__(self, output: dict) -> None:
        self._output = output

    def fetchone(self, sql, params=()):
        if "prompt_runs" in sql:
            return {"output_json": json.dumps(self._output)}
        return None

    def fetchall(self, sql, params=()):
        return []


def _write_archive(root, session_id: str = "research-test") -> None:
    session = root / "research_archive" / "project-test" / session_id
    (session / "claim_bindings").mkdir(parents=True)
    manifest = {
        "session_id": session_id,
        "project_id": "project-test",
        "workflow_id": "wf-test",
        "retrieval_mode": "LIVE_HYBRID_ACADEMIC_WEB",
        "created_at": "2026-09-07T12:17:02+00:00",
        "queries": ["q1"],
        "research_sufficiency": {"status": "DEGRADED", "coverage_status": "INSUFFICIENT"},
        "research_gaps": [{"gap_id": "g1", "query": "q1", "gap_types": ["FULLTEXT"]}],
        "records": [
            {
                "source_id": "public-src-1",
                "title": "T1",
                "url": "https://a.example.mil/x",
                "domain": "a.example.mil",
                "source_category": "GOVERNMENT",
                "authority_rank": 94,
                "fetch_mode": "SNIPPET_ONLY",
                "fetch_fallback_reason": "DNS resolution failed",
                "full_text_available": False,
                "text_length": 125,
                "matched_query": "q1",
                "snapshot_sha256": "not-exposed",
            }
        ],
    }
    (session / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    binding = {
        "bindings": [
            {"claim_id": "C-1", "source_ids": ["public-src-1"], "evidence_mode": "DIRECT_SOURCE_SUPPORTED"}
        ]
    }
    (session / "claim_bindings" / "claim-binding-1.json").write_text(json.dumps(binding), encoding="utf-8")


def test_research_archive_detail_returns_sources_claims_and_bindings(monkeypatch, tmp_path):
    _write_archive(tmp_path)
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, data_dir=tmp_path))
    output = {
        "result": {
            "claims": [
                {
                    "claim_id": "C-1",
                    "claim_text": "文本",
                    "dimension": "APPLICATION_SCENARIO",
                    "knowledge_status": "SUPPORTED",
                    "subject_id": None,
                }
            ]
        }
    }
    monkeypatch.setattr(main, "db", _FakeDB(output))

    with TestClient(main.app) as client:
        response = client.get("/api/projects/project-test/research-archives/research-test/detail")

    assert response.status_code == 200
    payload = response.json()
    assert payload["session_id"] == "research-test"
    assert payload["workflow_id"] == "wf-test"
    assert payload["sufficiency"]["status"] == "DEGRADED"
    assert payload["gaps"][0]["gap_types"] == ["FULLTEXT"]
    assert payload["sources"] == [
        {
            "source_id": "public-src-1",
            "title": "T1",
            "url": "https://a.example.mil/x",
            "domain": "a.example.mil",
            "source_category": "GOVERNMENT",
            "authority_rank": 94,
            "fetch_mode": "SNIPPET_ONLY",
            "fetch_fallback_reason": "DNS resolution failed",
            "full_text_available": False,
            "text_length": 125,
            "matched_query": "q1",
        }
    ]
    assert payload["claims"] == [
        {
            "claim_id": "C-1",
            "claim_text": "文本",
            "dimension": "APPLICATION_SCENARIO",
            "knowledge_status": "SUPPORTED",
            "subject_id": None,
        }
    ]
    assert payload["claim_bindings"] == [
        {"claim_id": "C-1", "source_ids": ["public-src-1"], "evidence_mode": "DIRECT_SOURCE_SUPPORTED"}
    ]


def test_research_archive_detail_404_for_missing_session(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, data_dir=tmp_path))

    with TestClient(main.app) as client:
        response = client.get("/api/projects/project-test/research-archives/missing/detail")

    assert response.status_code == 404
