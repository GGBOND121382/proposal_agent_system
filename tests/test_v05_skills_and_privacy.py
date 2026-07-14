from app.privacy import find_sensitive_values


def test_hash_fields_do_not_trigger_phone_false_positive():
    value = {"source_hash": "abc13812345678def", "nested": {"content_hash": "13900005726"}}
    assert find_sensitive_values(value, {}, include_generic_patterns=True) == []


def test_content_fields_still_detect_phone_and_email():
    value = {"text": "联系人139-0000-5726，邮箱zhou.yunzhou@example.test"}
    kinds = {m.entity_type for m in find_sensitive_values(value, {}, include_generic_patterns=True)}
    assert kinds == {"PHONE", "EMAIL"}

import json
from dataclasses import replace
from pathlib import Path

from app.config import Settings
from app.skills.base import SkillContext
from app.skills.public_research import PublicResearchArchiveError, PublicResearchArchiveSkill


def _settings_for(tmp_path: Path, connector_file: Path) -> Settings:
    base = Settings.load()
    return replace(
        base,
        data_dir=tmp_path,
        db_path=tmp_path / "proposal_agents.sqlite3",
        uploads_dir=tmp_path / "uploads",
        exports_dir=tmp_path / "exports",
        public_search_provider="connector",
        public_research_connector_file=str(connector_file),
        public_search_max_results=40,
    )


def test_connector_research_archives_all_agent_queries(tmp_path):
    connector = Path(__file__).resolve().parents[1] / "data" / "research_catalog" / "transport_optimization_connector_response.json"
    payload = json.loads(connector.read_text(encoding="utf-8"))
    queries = payload["agent_generated_queries"]
    settings = _settings_for(tmp_path, connector)
    result = PublicResearchArchiveSkill(settings).run(
        {"provider": "connector", "plan": {"queries": queries}, "max_results": 40},
        SkillContext(project_id="transport-project", workflow_id="research-workflow", data_dir=tmp_path, security_level="PUBLIC"),
    )
    assert result.status == "PASS"
    assert result.output["mode"] == "LIVE_CONNECTOR_ARCHIVE"
    assert len(result.output["sources"]) == 39  # one duplicate URL is intentionally deduplicated
    manifest = json.loads(Path(result.output["archive_manifest"]).read_text(encoding="utf-8"))
    assert manifest["queries"] == queries
    assert manifest["connector_response"]
    assert all(Path(item[key]).exists() for item in manifest["records"] for key in ("raw_path", "text_path", "metadata_path"))
    assert all(item["snapshot_sha256"] and item["text_sha256"] for item in manifest["records"])


def test_connector_research_rejects_missing_query(tmp_path):
    source = Path(__file__).resolve().parents[1] / "data" / "research_catalog" / "transport_optimization_connector_response.json"
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["responses"] = payload["responses"][:-1]
    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    settings = _settings_for(tmp_path, broken)
    try:
        PublicResearchArchiveSkill(settings).run(
            {"provider": "connector", "plan": {"queries": payload["agent_generated_queries"]}},
            SkillContext(project_id="transport-project", workflow_id="research-workflow", data_dir=tmp_path, security_level="PUBLIC"),
        )
    except PublicResearchArchiveError as exc:
        assert "do not cover planned queries" in str(exc)
    else:
        raise AssertionError("missing query coverage must be rejected")


def test_public_research_plan_inherits_transport_safe_package():
    from app.pack import PromptPack
    from app.simulated_llm import SimulatedLLM

    settings = Settings.load()
    pack = PromptPack(settings.prompt_pack_dir)
    envelope = pack.replay_input("P-PUBLIC-RESEARCH-PLAN")
    approved_queries = [
        "vehicle routing problem survey heuristics exact methods time windows",
        "dynamic vehicle routing online stochastic requests review",
        "multi depot inventory routing warehouse transportation optimization",
        "multimodal freight transport optimization timetable intermodal survey",
        "multi agent reinforcement learning logistics transportation scheduling",
        "learning to route neural combinatorial optimization vehicle routing",
        "digital twin logistics transportation real time optimization",
        "green vehicle routing carbon emissions sustainable logistics review",
        "large language model agents operations research optimization tool use",
        "OR-Tools vehicle routing CP-SAT official documentation",
    ]
    envelope["payload"]["safe_online_package_content"] = {
        "package_id": "pkg-transport",
        "task_type": "PUBLIC_RESEARCH",
        "task_description": "检索车辆路径、多仓协同、多式联运和动态重规划公开资料。",
        "queries": approved_queries,
        "allowed_context": ["车辆路径", "多仓协同", "多式联运", "动态重规划"],
        "prohibited_inferences": ["不得推断内部项目"],
        "prohibited_outputs": ["不得输出内部信息"],
        "security_level": "PUBLIC",
    }
    assert pack.validate("P-PUBLIC-RESEARCH-PLAN", "input", envelope) == []
    output = SimulatedLLM(pack).invoke("P-PUBLIC-RESEARCH-PLAN", envelope)
    assert output["result"]["queries"] == approved_queries
    assert "车辆路径、时间窗、取送和多仓问题有哪些精确与启发式方法？" in output["result"]["research_questions"]


def test_machine_identifier_fields_do_not_trigger_generic_phone_detection():
    value = {
        "source_id": "public-src-13900005726",
        "claim_id": "claim-13812345678",
        "trace_ids": ["trace-13700001111"],
        "quoted_text": "公开正文不含联系方式。",
    }
    assert find_sensitive_values(value, {}, include_generic_patterns=True) == []


def test_configured_prohibited_value_is_still_detected_inside_identifier():
    value = {"source_id": "prefix-project-secret-suffix"}
    config = {"prohibited_external_values": ["project-secret"]}
    matches = find_sensitive_values(value, config, include_generic_patterns=True)
    assert len(matches) == 1
    assert matches[0].entity_type == "CUSTOM"


def test_vertical_figure_is_scaled_to_printable_page(tmp_path):
    from PIL import Image
    from docx import Document
    from app.exporter_render import ExportRenderMixin

    image_path = tmp_path / "vertical.png"
    Image.new("RGB", (600, 2400), "white").save(image_path)
    document = Document()
    renderer = ExportRenderMixin()
    renderer._append_figure(document, f"{image_path}|竖向流程图|15.5")
    shape = document.inline_shapes[0]
    assert shape.height.cm <= 16.51
    assert shape.width.cm < 15.5
    output = tmp_path / "scaled.docx"
    document.save(output)
    assert output.exists()
