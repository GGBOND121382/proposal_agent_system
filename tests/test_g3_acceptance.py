from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx

from app.g3_acceptance import g3_preflight
from app.skills.base import SkillContext
from app.skills.crossref_public_research import CrossrefPublicResearchArchiveSkill


class _Pack:
    endpoints = {
        "endpoints": [
            {"endpoint_id": "offline-primary", "base_url": "https://models.example/inference"},
            {"endpoint_id": "online-public-primary", "base_url": "https://models.example/inference"},
        ]
    }
    models = {
        "models": [
            {"model_id": "offline-general-primary", "provider_model_name": "provider/general"},
            {"model_id": "offline-critic-primary", "provider_model_name": "provider/critic"},
            {"model_id": "online-public-primary", "provider_model_name": "provider/public"},
        ]
    }


def _settings(tmp_path: Path):
    return SimpleNamespace(
        runtime_mode="LIVE",
        public_search_provider="crossref",
        public_search_base_url="",
        public_research_record_file="",
        public_research_connector_file="",
        public_search_max_results=10,
        research_fetch_timeout_seconds=5,
        research_max_source_bytes=1024 * 1024,
        mermaid_browser_executable="/bin/true",
    )


def test_g3_preflight_reports_presence_without_exposing_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPABILITY_ACCEPTANCE_MODE", "true")
    monkeypatch.setenv("OFFLINE_LLM_API_KEY", "private-offline-value")
    monkeypatch.setenv("ONLINE_LLM_API_KEY", "private-online-value")
    monkeypatch.setattr("app.g3_acceptance.shutil.which", lambda name: "/usr/bin/" + name)
    report = g3_preflight(_settings(tmp_path), _Pack()).as_dict()
    assert report["status"] == "READY"
    encoded = json.dumps(report)
    assert "private-offline-value" not in encoded
    assert "private-online-value" not in encoded
    assert report["summary"]["credentials_reported_as_presence_only"] is True


def test_g3_preflight_blocks_non_live_and_missing_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPABILITY_ACCEPTANCE_MODE", "false")
    monkeypatch.delenv("OFFLINE_LLM_API_KEY", raising=False)
    monkeypatch.delenv("ONLINE_LLM_API_KEY", raising=False)
    settings = _settings(tmp_path)
    settings.runtime_mode = "SIMULATED"
    settings.public_search_provider = "recorded"
    report = g3_preflight(settings, _Pack()).as_dict()
    assert report["status"] == "BLOCKED_CONFIGURATION"
    assert set(report["missing"]) >= {
        "capability_mode_enabled",
        "runtime_is_live",
        "direct_live_research_provider",
        "offline_credential_available",
        "online_credential_available",
    }


def test_crossref_provider_archives_live_metadata(tmp_path, monkeypatch):
    payload = {
        "message": {
            "items": [
                {
                    "DOI": "10.1000/dynamic-review",
                    "title": ["Dynamic vehicle routing benchmark review and limitations"],
                    "abstract": "<jats:p>A recent benchmark comparison discusses limitations and open challenges.</jats:p>",
                    "author": [{"given": "A", "family": "Researcher"}],
                    "publisher": "Example Journal",
                    "published": {"date-parts": [[2025, 3, 1]]},
                    "URL": "https://doi.org/10.1000/dynamic-review",
                    "type": "journal-article",
                    "subject": ["dynamic vehicle routing", "benchmark comparison"],
                    "container-title": ["Journal of Dynamic Optimization"],
                }
            ]
        }
    }

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return payload

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, *args, **kwargs):
            return _Response()

    monkeypatch.setattr(httpx, "Client", _Client)
    settings = _settings(tmp_path)
    skill = CrossrefPublicResearchArchiveSkill(settings)
    plan = {
        "plan_id": "g3-plan",
        "task_type": "PUBLIC_RESEARCH",
        "research_questions": ["What are the recent dynamic vehicle routing benchmark baselines and limitations?"],
        "queries": ["dynamic vehicle routing benchmark review limitations 2021 2026"],
        "source_priorities": ["同行评议论文"],
        "time_scope": "2021-01-01/2026-12-31",
        "evidence_requirements": ["最近工作", "基线", "局限"],
        "prohibited_inferences": ["不得推断内部项目"],
    }
    result = skill.run(
        {"provider": "crossref", "require_structured_plan": True, "plan": plan, "max_results": 5},
        SkillContext(project_id="g3-project", workflow_id="g3-wf", security_level="PUBLIC", data_dir=str(tmp_path)),
    )
    assert result.status == "PASS"
    assert result.output["mode"] == "LIVE_CROSSREF"
    assert result.output["archive_verification"]["status"] == "PASS"
    assert result.output["coverage"]["status"] == "PASS"
    assert result.output["source_catalog"][0]["doi"] == "10.1000/dynamic-review"


def test_github_model_selector_prefers_low_tier_text_model():
    from scripts.select_github_model import choose_model

    selected = choose_model(
        [
            {"id": "vendor/large", "task": "chat-completion", "rate_limit_tier": "high", "max_input_tokens": 100000},
            {"id": "openai/gpt-4o-mini", "task": "chat-completion", "rate_limit_tier": "low", "max_input_tokens": 128000},
            {"id": "vendor/embed", "task": "embeddings", "rate_limit_tier": "low"},
        ]
    )
    assert selected["id"] == "openai/gpt-4o-mini"


def test_compact_schema_prompt_is_smaller_and_non_live_input_is_unchanged(tmp_path, monkeypatch):
    from app.config import Settings
    from app.g3_runtime_executor import G3RuntimePromptExecutor as RuntimeExecutor
    from app.pack import PromptPack

    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MODEL_SCHEMA_PROMPT_MODE", "compact")
    settings = Settings.load()
    pack = PromptPack(settings.prompt_pack_dir)
    executor = RuntimeExecutor.__new__(RuntimeExecutor)
    executor.pack = pack
    executor.gateway = SimpleNamespace(settings=settings)
    executor.policy = SimpleNamespace(enabled=False)
    schema = pack.inlined_schema("P-INTEGRATION-CRITIC", "output")
    compact_prompt = executor._system_prompt("P-INTEGRATION-CRITIC", schema)
    full_size = len(pack.shared_prompt) + len(pack.prompt_text("P-INTEGRATION-CRITIC")) + len(json.dumps(schema, ensure_ascii=False))
    assert len(compact_prompt) < full_size
    envelope = pack.replay_input("P-WRITE-CONTENT")
    model_envelope, metadata = executor._prepare_model_envelope("P-WRITE-CONTENT", envelope)
    assert model_envelope == envelope
    assert metadata is None
