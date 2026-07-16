from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx

from app.g3_acceptance import preflight_environment
from app.skills.base import SkillContext
from app.skills.g3_crossref import G3CrossrefResearchSkill


def test_preflight_blocks_nonlive_configuration(monkeypatch):
    monkeypatch.setenv("CAPABILITY_ACCEPTANCE_MODE", "true")
    monkeypatch.setenv("MODEL_RUNTIME_MODE", "SIMULATED")
    monkeypatch.setenv("PUBLIC_SEARCH_PROVIDER", "recorded")
    report = preflight_environment()
    assert report.status == "BLOCKED_CONFIGURATION"
    assert not report.checks["capability_policy_live"]


def test_preflight_accepts_explicit_live_configuration(monkeypatch):
    values = {
        "CAPABILITY_ACCEPTANCE_MODE": "true", "MODEL_RUNTIME_MODE": "LIVE",
        "PUBLIC_SEARCH_PROVIDER": "crossref", "OFFLINE_LLM_BASE_URL": "http://127.0.0.1:18000/v1",
        "OFFLINE_GENERAL_MODEL": "real-general", "OFFLINE_CRITIC_MODEL": "real-critic",
        "ONLINE_LLM_BASE_URL": "https://model.vendor.test/v1", "ONLINE_PUBLIC_MODEL": "real-public",
        "G3_OPERATOR_ATTESTATION": "USER_REQUESTED", "G3_OPERATOR_ID": "operator",
        "G3_MODEL_PROVENANCE_ATTESTATION": "REAL_MODEL_ENDPOINT",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    assert preflight_environment().status == "PASS"


def test_crossref_skill_archives_live_metadata(tmp_path: Path, monkeypatch):
    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"message": {"items": [{
                "DOI": "10.1000/g3.test", "title": ["Dynamic routing under disruptions"],
                "abstract": "<jats:p>Recent baseline and limitation mechanism.</jats:p>",
                "author": [{"given": "A", "family": "Researcher"}],
                "published": {"date-parts": [[2025, 5, 1]]},
                "container-title": ["Transportation Research"], "publisher": "Test",
                "URL": "https://doi.org/10.1000/g3.test", "type": "journal-article",
            }]}}

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr(httpx, "Client", Client)
    settings = SimpleNamespace(
        public_search_provider="crossref", public_search_base_url="https://api.crossref.org",
        public_research_record_file="", public_research_connector_file="", public_search_max_results=10,
        research_fetch_timeout_seconds=5, research_max_source_bytes=1024 * 1024,
    )
    skill = G3CrossrefResearchSkill(settings)
    plan = {
        "plan_id": "g3-plan", "task_type": "PUBLIC_RESEARCH",
        "research_questions": ["What are recent routing baselines and limitations?"],
        "queries": ["dynamic routing disruption baseline limitations"],
        "source_priorities": ["peer reviewed papers"], "time_scope": "2021-01-01/2026-12-31",
        "evidence_requirements": ["recent work", "baseline", "limitation"],
        "prohibited_inferences": ["do not invent internal results"],
    }
    result = skill.run(
        {"provider": "crossref", "require_structured_plan": True, "plan": plan, "max_results": 10},
        SkillContext(project_id="g3", workflow_id="wf", security_level="PUBLIC", data_dir=str(tmp_path)),
    )
    assert result.status == "PASS"
    assert result.output["mode"] == "LIVE_CROSSREF"
    manifest = json.loads(Path(result.output["archive_manifest"]).read_text(encoding="utf-8"))
    assert manifest["retrieval_mode"] == "LIVE_CROSSREF"
    assert manifest["records"][0]["doi"] == "10.1000/g3.test"
