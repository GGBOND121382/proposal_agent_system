from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

import pytest

from app.config import Settings
from app.runtime_api import ContextBuilder
from app.llm import LLMResult
from app.executor import PromptExecutionError
from app.db import Database
from app.documents import parse_document
from app.runtime_api import PromptExecutor
from app.runtime_api import DocxExporter
from app.runtime_api import ModelGateway
from app.pack import PromptPack
from app.research import PublicResearchService
from app.security import RoutingDenied, SecurityRouter
from app.util import new_id, sha256_json, utc_now
from app.runtime_api import WorkflowEngine
from app.agent_prompt_kernel import _substantive_numeric_tokens
from app.workflow_input import APPLICATION_GUIDE_INPUT, WorkflowInputRequired


@pytest.fixture()
def runtime(tmp_path: Path, monkeypatch):
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
    research = PublicResearchService(settings)
    engine = WorkflowEngine(db, pack, builder, executor, research)
    exporter = DocxExporter(db, settings)
    return settings, pack, db, router, builder, executor, engine, exporter


def create_project(db: Database, *, internet: bool = True) -> str:
    project_id = new_id("project")
    now = utc_now()
    config = {
        "internet_access_allowed": internet,
        "anonymized_external_processing_allowed": internet,
        "allowed_public_topics": ["公开政策"],
        "prohibited_external_fields": ["真实项目名称"],
        "recipient_scope": ["内部用户"],
        "allowed_model_endpoint_ids": ["offline-primary", "online-public-primary"],
        "retention_days": 365,
        "task_instruction": None,
    }
    db.execute(
        "INSERT INTO projects(id,name,description,security_level,config_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        (project_id, "测试项目", "", "INTERNAL", json.dumps(config, ensure_ascii=False), now, now),
    )
    return project_id


def add_standard_materials(settings: Settings, db: Database, project_id: str, *, current_sections: list[str] | None = None) -> None:
    materials = [
        ("guide.md", "APPLICATION_GUIDE", "# 申报指南\n本项目按科研项目申请书评审，主文不超过35页，突出研究问题、方法、创新、验证和研究基础。"),
        ("brief.md", "PROJECT_BRIEF", "# 项目任务\n研究动态运输优化中的约束映射与低扰动增量重规划，原型仅作为验证载体。"),
        ("reference.md", "REFERENCE_PROPOSAL", "# 立项依据\n从代表工作能力边界推出具体差距。\n# 研究方案\n每个问题分别绑定方法、基线和实验。"),
        ("evidence.md", "EVIDENCE_MATERIAL", "# 前期成果\n团队已完成组合优化原型和动态调度实验代码，形成可复现实验记录与初步对照结果，可支撑本项目模型和实验。"),
    ]
    titles = current_sections or ["立项依据", "研究目标", "研究内容", "研究方案", "创新点", "研究基础", "参考文献"]
    draft = "# 全文\n待完善。\n" + "\n".join(f"# {title}\n待编写。" for title in titles)
    materials.append(("draft.md", "CURRENT_PROPOSAL", draft))
    for filename, role, text in materials:
        raw = text.encode("utf-8")
        parsed = parse_document(filename, raw, role, "INTERNAL")
        path = settings.uploads_dir / filename
        path.write_bytes(raw)
        db.execute(
            "INSERT INTO documents(id,project_id,filename,role,security_level,document_hash,file_path,parsed_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (parsed["document_id"], project_id, filename, role, "INTERNAL", parsed["document_hash"], str(path), json.dumps(parsed, ensure_ascii=False), utc_now()),
        )


async def finish_workflow(engine: WorkflowEngine, project_id: str, workflow_type: str, *, max_steps: int = 500):
    wf = engine.start(project_id, workflow_type)
    for _ in range(max_steps):
        wf = await engine.advance(wf["id"])
        if wf["status"] == "WAITING_GATE":
            gate = next(g for g in engine.list_gates(workflow_id=wf["id"]) if g["status"] == "OPEN")
            action = "APPROVE" if "APPROVE" in gate["allowed_actions"] else "CONFIRM"
            engine.decide_gate(gate["id"], action=action, decided_by="pytest", decided_role=gate["required_role"])
            continue
        if wf["status"] in {"COMPLETED", "BLOCKED", "CANCELLED"}:
            break
    return wf


def test_prompt_pack_and_all_normal_replays(runtime):
    _, pack, *_ = runtime
    assert len(pack.prompt_ids()) == 30
    for prompt_id in pack.prompt_ids():
        case = pack.replay_case(prompt_id, "normal")
        assert pack.validate(prompt_id, "input", case["input"]) == []
        assert pack.validate(prompt_id, "output", case["expected_output"]) == []
        inlined = pack.inlined_schema(prompt_id, "output")
        assert "$ref" not in json.dumps(inlined)


def test_scheme_profile_allows_unknown_year_and_duration(runtime):
    _, pack, *_ = runtime
    output = pack.replay_output("P-SCHEME-EXTRACT", "normal")
    output["result"]["scheme_profile"]["application_year"] = None
    output["result"]["scheme_profile"]["duration_months"] = None

    assert pack.validate("P-SCHEME-EXTRACT", "output", output) == []


def test_scheme_output_enriches_compact_source_refs_from_input(runtime):
    _, pack, *_ = runtime
    envelope = pack.replay_input("P-SCHEME-EXTRACT")
    output = pack.replay_output("P-SCHEME-EXTRACT", "normal")
    document = envelope["payload"]["guide_documents"][0]
    section = document["sections"][0]
    rule = output["result"]["scheme_profile"]["rules"][0]
    rule["source_refs"] = [{
        "source_id": document["document_id"],
        "source_type": document["document_role"],
        "section_id": section["section_id"],
        "authority_rank": document["authority_rank"],
        "security_level": document["security_level"],
    }]
    output["result"]["scheme_profile"]["profile_hash"] = "0" * 64

    normalized = PromptExecutor._normalize_scheme_output(output, envelope)
    source_ref = normalized["result"]["scheme_profile"]["rules"][0]["source_refs"][0]

    assert source_ref["document_version_id"] == document["document_version_id"]
    assert source_ref["quoted_text"] == section["text"]
    assert source_ref["source_hash"] == section["text_hash"]
    assert source_ref["span_start"] == 0
    assert source_ref["span_end"] == len(section["text"])
    assert normalized["result"]["scheme_profile"]["profile_hash"] != "0" * 64
    assert pack.validate("P-SCHEME-EXTRACT", "output", normalized) == []


def test_scheme_output_maps_project_brief_source_type_alias(runtime):
    _, pack, _, _, _, executor, *_ = runtime
    envelope = pack.replay_input("P-SCHEME-EXTRACT")
    output = pack.replay_output("P-SCHEME-EXTRACT", "normal")
    document = envelope["payload"]["guide_documents"][0]
    document["document_role"] = "PROJECT_BRIEF"
    section = document["sections"][0]
    source_ref = output["result"]["scheme_profile"]["rules"][0]["source_refs"][0]
    source_ref.update({
        "source_id": document["document_id"],
        "source_type": "PROJECT_BRIEF",
        "section_id": section["section_id"],
    })
    output["source_refs"].append({
        "source_id": document["document_id"],
        "source_type": "PROJECT_BRIEF",
        "section_id": section["section_id"],
        "authority_rank": document["authority_rank"],
        "security_level": document["security_level"],
    })

    normalized = executor._normalize_output("P-SCHEME-EXTRACT", output, envelope)

    assert normalized["result"]["scheme_profile"]["rules"][0]["source_refs"][0]["source_type"] == "HISTORICAL_DOCUMENT"
    assert normalized["source_refs"][-1]["source_type"] == "HISTORICAL_DOCUMENT"
    assert pack.validate("P-SCHEME-EXTRACT", "output", normalized) == []


def test_scheme_output_preserves_content_validation_constraint_when_rules_empty(runtime):
    _, pack, *_ = runtime
    envelope = pack.replay_input("P-SCHEME-EXTRACT")
    output = pack.replay_output("P-SCHEME-EXTRACT", "normal")
    output["status"] = "NEED_USER_INPUT"
    output["result"]["scheme_profile"]["rules"] = []
    output["result"]["extraction_coverage"] = []

    normalized = PromptExecutor._normalize_scheme_output(output, envelope)

    rule = normalized["result"]["scheme_profile"]["rules"][0]
    assert rule["rule_id"] == "rule-system-content-validation-only"
    assert rule["mandatory"] is True
    assert normalized["result"]["extraction_coverage"][0]["covered_rule_ids"] == [rule["rule_id"]]
    assert rule["source_refs"][0]["quoted_text"]
    assert pack.validate("P-SCHEME-EXTRACT", "output", normalized) == []


def test_approved_need_user_input_gate_advances_accepted_step(runtime):
    _, _, _, _, _, _, engine, _ = runtime
    project_id = create_project(engine.db)
    workflow = engine.start(project_id, "WF-1_PROJECT_INTAKE")
    workflow["state"]["step_results"]["0"] = {
        "prompt_id": "P-SECURITY-CLASSIFY",
        "run_id": "run-accepted-test",
        "status": "NEED_USER_INPUT",
    }
    engine._update(workflow, state=workflow["state"])
    gate_id = engine._create_gate(
        engine.get(workflow["id"]),
        "PROJECT_GAP_RESOLUTION",
        target_id="run-accepted-test",
        questions=[],
    )

    engine.decide_gate(
        gate_id,
        action="CONFIRM",
        decided_by="pytest",
        decided_role="PROJECT_OWNER",
    )

    updated = engine.get(workflow["id"])
    assert updated["current_step"] == 1
    assert updated["state"]["accepted_step_results"]["0"]["run_id"] == "run-accepted-test"


def test_stage_completion_accepts_gated_model_findings_but_not_qg(runtime):
    _, _, _, _, _, _, engine, _ = runtime
    state = {
        "accepted_step_results": {
            "4": {
                "run_id": "run-accepted",
                "status": "NEED_USER_INPUT",
                "gate_id": "gate-accepted",
            }
        }
    }
    accepted_model_finding = {
        "finding_id": "qf-model",
        "finding": {"code": "EVIDENCE_GAP"},
        "lifecycle": {"opened_by": {"run_id": "run-accepted"}},
    }
    accepted_qg_finding = {
        "finding_id": "qf-qg",
        "finding": {"code": "QG_SOURCE_BINDING"},
        "lifecycle": {"opened_by": {"run_id": "run-accepted"}},
    }
    unrelated_finding = {
        "finding_id": "qf-other",
        "finding": {"code": "FALSE_READINESS"},
        "lifecycle": {"opened_by": {"run_id": "run-other"}},
    }

    remaining, accepted = engine._unaccepted_completion_blockers(
        [accepted_model_finding, accepted_qg_finding, unrelated_finding],
        state,
    )

    assert [item["finding_id"] for item in accepted] == ["qf-model"]
    assert [item["finding_id"] for item in remaining] == ["qf-qg", "qf-other"]


def test_normalizer_removes_schema_keyword_emitted_as_instance_data(runtime):
    _, pack, _, _, _, executor, _, _ = runtime
    output = pack.replay_output("P-SCHEME-CRITIC", "normal")
    output["status"] = "REVISE"
    output["result"]["verdict"] = "REVISE"
    output["result"]["additionalProperties"] = False
    output["findings"] = [{
        "code": "SCHEME_TEST_FINDING",
        "severity": "P0",
        "category": "EVIDENCE",
        "target_type": "SCHEME_PROFILE",
        "target_path_or_span": "result",
        "description": "Test finding.",
        "evidence_refs": ["domain_readiness[10]"],
        "repairable": True,
        "repair_instruction": "Review the referenced readiness entry.",
        "suggested_route": "USER",
        "blocking": True,
    }]

    normalized = executor._normalize_output("P-SCHEME-CRITIC", output)

    assert "additionalProperties" not in normalized["result"]
    assert normalized["findings"][0]["evidence_refs"] == ["domain_readiness.10"]
    assert normalized["findings"][0]["category"] == "SOURCE"
    assert normalized["status"] == "NEED_USER_INPUT"
    assert pack.validate("P-SCHEME-CRITIC", "output", normalized) == []


def test_normalizer_merges_nested_response_warnings_when_top_level_exists(runtime):
    _, pack, _, _, _, executor, _, _ = runtime
    output = pack.replay_output("P-WRITE-BLUEPRINT-CRITIC", "normal")
    output["warnings"] = ["top-level warning"]
    output["result"]["warnings"] = ["nested warning"]

    normalized = executor._normalize_output("P-WRITE-BLUEPRINT-CRITIC", output)

    assert "warnings" not in normalized["result"]
    assert normalized["warnings"][:2] == ["top-level warning", "nested warning"]
    assert pack.validate("P-WRITE-BLUEPRINT-CRITIC", "output", normalized) == []


def test_normalizer_maps_source_preservation_action_alias(runtime):
    _, pack, _, _, _, executor, _, _ = runtime
    output = pack.replay_output("P-WRITE-CONTENT", "normal")
    output["result"]["source_preservation_summary"][0]["action"] = "DISTRIBUTED"

    normalized = executor._normalize_output("P-WRITE-CONTENT", output)

    assert normalized["result"]["source_preservation_summary"][0]["action"] == "REPHRASED"
    assert pack.validate("P-WRITE-CONTENT", "output", normalized) == []


def test_normalizer_maps_confirmed_fact_source_aliases(runtime):
    _, pack, _, _, _, executor, _, _ = runtime
    output = pack.replay_output("P-WRITE-CONTENT", "normal")
    output["result"]["trace_links"][0]["source_kind"] = "CONFIRMED_FACT"
    output["source_refs"] = [{
        "source_id": "F-001",
        "source_type": "CONFIRMED_FACT",
        "document_version_id": None,
        "section_id": None,
        "span_start": None,
        "span_end": None,
        "quoted_text": None,
        "source_hash": "a" * 63,
        "authority_rank": 80,
        "security_level": "INTERNAL",
    }]

    normalized = executor._normalize_output("P-WRITE-CONTENT", output)

    assert normalized["result"]["trace_links"][0]["source_kind"] == "FACT"
    assert normalized["source_refs"][0]["source_type"] == "EVIDENCE_MATERIAL"
    assert normalized["source_refs"][0]["source_hash"] is None
    assert pack.validate("P-WRITE-CONTENT", "output", normalized) == []


def test_content_normalizer_accepts_only_explicit_nonblocking_test_deferrals(runtime):
    _, pack, _, _, _, executor, _, _ = runtime
    output = pack.replay_output("P-WRITE-CONTENT", "normal")
    output["status"] = "REVISE"
    output["result"]["candidate_text"] += (
        "\n[测试数据，待替换] [测试占位符，正式申报前替换]"
    )
    output["findings"] = [
        {
            "code": "QUALITY_DIMENSION_FAILED",
            "severity": "P2",
            "category": "CONTENT",
            "target_type": "PARAGRAPH",
            "target_path_or_span": "paragraphs[0].text",
            "description": "测试占位符已正确标注，但正式申报前仍需替换。",
            "evidence_refs": ["F-077"],
            "repairable": True,
            "repair_instruction": "正式申报前替换为真实材料。",
            "suggested_route": "ORIGINAL_PRODUCER",
            "blocking": False,
        },
        {
            "code": "REPAIR_RECEIPT",
            "severity": "P3",
            "category": "CONTENT",
            "target_type": "PARAGRAPH",
            "target_path_or_span": "paragraphs[0].text",
            "description": "命题绑定已修复。",
            "evidence_refs": ["para-001"],
            "repairable": False,
            "repair_instruction": "无需修复。",
            "suggested_route": "ORIGINAL_PRODUCER",
            "blocking": False,
        },
    ]
    output["unresolved_items"] = [
        {
            "item_id": "URI-TEST-001",
            "type": "OUT_OF_SCOPE",
            "description": "正式申报前替换测试占位材料。",
            "target_paths": ["paragraphs[0].text"],
            "required_action": "替换为真实材料。",
            "blocking": False,
        }
    ]

    normalized = executor._normalize_output("P-WRITE-CONTENT", output)

    assert normalized["status"] == "PASS"
    assert [finding["code"] for finding in normalized["findings"]] == [
        "QUALITY_DIMENSION_FAILED",
    ]
    assert normalized["unresolved_items"] == output["unresolved_items"]
    assert any("explicitly labelled test placeholders" in item for item in normalized["warnings"])


def test_blueprint_normalizer_drops_unbound_metric_labels_and_maps_known_aliases(runtime):
    _, pack, _, _, _, executor, _, _ = runtime
    output = pack.replay_output("P-WRITE-BLUEPRINT", "normal")
    envelope = pack.replay_input("P-WRITE-BLUEPRINT")
    envelope["payload"]["metric_inputs"] = [{
        "metric_id": "METRIC-001",
        "name": "known metric label",
    }]
    envelope["payload"]["technical_inputs"] = [{
        "object_id": "M-001",
        "object_type": "METHOD",
    }]
    paragraph = output["result"]["blueprint"]["paragraphs"][0]
    paragraph["primary_claim_id"] = "M-001"
    paragraph["project_item_slots"] = ["M-001"]
    paragraph["technical_slots"] = ["多链联合建模"]
    paragraph["metric_slots"] = [
        "known metric label",
        "首次可行方案形成速度",
        "METRIC-EXISTING",
    ]

    normalized = executor._normalize_output("P-WRITE-BLUEPRINT", output, envelope)

    assert normalized["result"]["blueprint"]["paragraphs"][0]["metric_slots"] == [
        "METRIC-001",
        "METRIC-EXISTING",
    ]
    assert normalized["result"]["blueprint"]["paragraphs"][0]["technical_slots"] == [
        "M-001",
    ]
    assert any("removed 1 metric label" in item for item in normalized["warnings"])
    assert pack.validate("P-WRITE-BLUEPRINT", "output", normalized) == []


def test_targeted_repair_normalizer_bounds_paragraph_budgets_to_contract_limit(runtime):
    _, pack, _, _, _, executor, _, _ = runtime
    output = pack.replay_output("P-TARGETED-REPAIR", "normal")
    envelope = pack.replay_input("P-TARGETED-REPAIR")
    paragraphs = [
        {
            "paragraph_id": f"para-tr-00{index}",
            "novel_content_key": f"route-key-{index}",
            "word_budget": budget,
        }
        for index, budget in enumerate((600, 500, 400), start=1)
    ]
    envelope["payload"]["original_object"]["content"] = {
        "paragraphs": [dict(item) for item in paragraphs],
    }
    envelope["payload"]["findings_to_repair"] = [{
        "code": "WORD_BUDGET_EXCEED",
        "description": "合同规定总字数为1000字，但当前总计1500字。",
    }]
    output["result"]["repaired_object"]["content"] = {
        "paragraphs": [dict(item) for item in paragraphs],
    }
    output["result"]["resolved_finding_codes"] = ["WORD_BUDGET_EXCEED"]
    output["result"]["unresolved_finding_codes"] = []

    normalized = executor._normalize_output("P-TARGETED-REPAIR", output, envelope)
    repaired = normalized["result"]["repaired_object"]["content"]["paragraphs"]

    assert sum(item["word_budget"] for item in repaired) == 1000
    assert len(normalized["result"]["changed_paths"]) >= 3
    assert any("1000-word section limit" in item for item in normalized["warnings"])


def test_targeted_repair_normalizer_enforces_scope_and_maps_receipts(runtime):
    _, pack, _, _, _, executor, _, _ = runtime
    output = pack.replay_output("P-TARGETED-REPAIR", "normal")
    envelope = pack.replay_input("P-TARGETED-REPAIR")
    original = {
        "candidate_id": "candidate-scope",
        "paragraphs": [
            {"paragraph_id": "para-001", "text": "one", "primary_claim_id": "C-1"},
            {"paragraph_id": "para-002", "text": "two", "primary_claim_id": "C-2"},
            {"paragraph_id": "para-003", "text": "three", "primary_claim_id": "C-3"},
        ],
    }
    envelope["payload"]["original_object"]["content"] = original
    envelope["payload"]["allowed_paths"] = [
        "content.paragraphs[1].text; paragraphs[2].text",
    ]
    envelope["payload"]["findings_to_repair"] = [{
        "code": "QUALITY_DIMENSION_FAILED",
        "target_path_or_span": "paragraphs[1].text; paragraphs[2].text",
    }]
    repaired = copy.deepcopy(original)
    repaired["paragraphs"][0]["text"] = "unauthorized"
    repaired["paragraphs"][1]["text"] = "two-fixed"
    repaired["paragraphs"][2]["text"] = "three-fixed"
    output["status"] = "REVISE"
    output["result"]["repaired_object"]["content"] = repaired
    output["result"]["changed_paths"] = [
        "content.paragraphs[0].text",
        "content.paragraphs[1].text",
        "content.paragraphs[2].text",
    ]
    output["result"]["resolved_finding_codes"] = [
        "QUALITY_DIMENSION_FAILED-EXPERIMENT-DESIGN",
    ]
    output["result"]["unresolved_finding_codes"] = []
    output["findings"] = [{
        "code": "REPAIR_COMPLETED",
        "blocking": False,
    }]

    normalized = executor._normalize_output("P-TARGETED-REPAIR", output, envelope)

    paragraphs = normalized["result"]["repaired_object"]["content"]["paragraphs"]
    assert paragraphs[0]["text"] == "one"
    assert paragraphs[1]["text"] == "two-fixed"
    assert paragraphs[2]["text"] == "three-fixed"
    assert normalized["result"]["resolved_finding_codes"] == [
        "QUALITY_DIMENSION_FAILED",
    ]
    assert normalized["result"]["changed_paths"] == [
        "content.paragraphs[1].text",
        "content.paragraphs[2].text",
    ]
    assert normalized["findings"] == []
    assert normalized["status"] == "PASS"


def test_normalizer_canonicalizes_common_critic_aliases(runtime):
    _, _, _, _, _, executor, _, _ = runtime
    output = {
        "status": "NEED_USER_INPUT",
        "findings": [
            {
                "category": "EVIDENCE",
                "reason": "The supporting material is missing.",
            }
        ],
        "user_questions": [
            {
                "answer_schema": {
                    "type": "STRING",
                    "allowed_values": None,
                    "properties": {"unexpected": {"type": "string"}},
                    "required": ["unexpected"],
                }
            }
        ],
        "unresolved_items": [{"type": "CHOICE"}],
        "result": {
            "domain_scores": [
                {
                    "domain": "OBJECTIVES",
                }
            ],
            "critical_readiness_checks": [
                {"dimension": "TEAM_AND_IMPLEMENTATION"},
                {"dimension": "RESOURCES_BUDGET_RISK_COMPLIANCE"},
            ],
        },
    }

    normalized = executor._normalize_output("P-PROJECT-READINESS-CRITIC", output)

    assert normalized["findings"][0]["category"] == "SOURCE"
    assert normalized["findings"][0]["description"] == "The supporting material is missing."
    assert "reason" not in normalized["findings"][0]
    assert normalized["user_questions"][0]["answer_schema"] == {
        "type": "STRING",
        "allowed_values": [],
    }
    assert normalized["unresolved_items"][0]["type"] == "UNCERTAIN"
    assert normalized["result"]["domain_scores"][0]["missing_item_types"] == []
    assert [
        item["dimension"]
        for item in normalized["result"]["critical_readiness_checks"]
    ] == ["RESEARCH_FOUNDATION", "SCOPE_AND_PAGE_BUDGET"]


def test_normalizer_fills_omitted_quality_dimension_required_actions(runtime):
    _, pack, _, _, _, executor, _, _ = runtime
    output = pack.replay_output("P-WRITE-CRITIC", "normal")
    dimensions = output["result"]["quality_dimensions"]
    dimensions[0].pop("required_action")
    dimensions[1]["passed"] = False
    dimensions[1].pop("required_action")

    normalized = executor._normalize_output("P-WRITE-CRITIC", output)

    assert normalized["result"]["quality_dimensions"][0]["required_action"] is None
    assert normalized["result"]["quality_dimensions"][1]["required_action"]
    assert pack.validate("P-WRITE-CRITIC", "output", normalized) == []
    assert any("quality-dimension required action" in item for item in normalized["warnings"])


def test_write_critic_normalizer_aligns_passing_deferred_test_dimension(runtime):
    _, pack, _, _, _, executor, _, _ = runtime
    output = pack.replay_output("P-WRITE-CRITIC", "normal")
    envelope = pack.replay_input("P-WRITE-CRITIC")
    output["status"] = "REVISE"
    output["result"]["verdict"] = "REVISE"
    output["result"]["quality_dimensions"][0].update(
        {
            "score": 3,
            "passed": False,
            "required_action": "正式申报前替换测试材料，当前阶段无需修改。",
        }
    )
    output["findings"] = [
        {
            "code": "TEST_PLACEHOLDER_DEFERRED",
            "severity": "P3",
            "category": "CONTENT",
            "target_type": "PARAGRAPH",
            "target_path_or_span": "paragraphs[0].text",
            "description": "测试占位材料正式申报前替换。",
            "evidence_refs": ["F-077"],
            "repairable": True,
            "repair_instruction": "当前阶段保持标注，正式申报前替换。",
            "suggested_route": "ORIGINAL_PRODUCER",
            "blocking": False,
        }
    ]
    output["unresolved_items"] = [
        {
            "item_id": "URI-TEST-DEFERRED",
            "type": "OUT_OF_SCOPE",
            "description": "正式申报前替换测试材料。",
            "target_paths": ["paragraphs[0].text"],
            "required_action": "替换测试材料。",
            "blocking": False,
        }
    ]
    output["result"]["unsupported_trace_ids"] = []
    output["result"]["blueprint_deviation_paragraph_ids"] = []
    output["result"]["scope_violations"] = []

    normalized = executor._normalize_output("P-WRITE-CRITIC", output, envelope)

    assert normalized["result"]["quality_dimensions"][0]["passed"] is True
    assert normalized["status"] == "PASS"
    assert normalized["result"]["verdict"] == "ACCEPT"
    assert normalized["unresolved_items"] == output["unresolved_items"]


def test_write_critic_normalizer_removes_stale_primary_claim_deviation(runtime):
    _, pack, _, _, _, executor, _, _ = runtime
    output = pack.replay_output("P-WRITE-CRITIC", "normal")
    envelope = pack.replay_input("P-WRITE-CRITIC")
    candidate = envelope["payload"]["content_candidate"]
    blueprint = envelope["payload"]["approved_blueprint"]
    paragraph_id = candidate["paragraphs"][0]["paragraph_id"]
    blueprint["paragraphs"][0]["paragraph_id"] = paragraph_id
    blueprint["paragraphs"][0]["primary_claim_id"] = "M-001"
    candidate["paragraphs"][0]["primary_claim_id"] = "M-001"
    output["findings"] = [
        {
            "code": "BLUEPRINT_DEVIATION",
            "severity": "P2",
            "category": "BLUEPRINT",
            "target_type": "PARAGRAPH",
            "target_path_or_span": "paragraphs[0].primary_claim_id",
            "description": "Primary claim differs from the blueprint.",
            "evidence_refs": [paragraph_id],
            "repairable": True,
            "repair_instruction": "Align the primary claim.",
            "suggested_route": "ORIGINAL_PRODUCER",
            "blocking": False,
        }
    ]
    output["result"]["blueprint_deviation_paragraph_ids"] = [paragraph_id]

    normalized = executor._normalize_output("P-WRITE-CRITIC", output, envelope)

    assert normalized["findings"] == []
    assert normalized["result"]["blueprint_deviation_paragraph_ids"] == []
    assert any("stale blueprint-deviation" in item for item in normalized["warnings"])


def test_template_normalizer_moves_pattern_collections_into_template(runtime):
    _, pack, _, _, _, executor, _, _ = runtime
    output = pack.replay_output("P-TEMPLATE-EXTRACT", "normal")
    result = output["result"]
    template = result["template"]
    for field in ("argument_patterns", "expression_patterns", "quality_anti_patterns"):
        result[field] = template.pop(field)

    normalized = executor._normalize_output("P-TEMPLATE-EXTRACT", output)

    assert pack.validate("P-TEMPLATE-EXTRACT", "output", normalized) == []
    for field in ("argument_patterns", "expression_patterns", "quality_anti_patterns"):
        assert field in normalized["result"]["template"]
        assert field not in normalized["result"]


def test_planning_template_context_uses_revision_plan_shape(runtime):
    _, pack, _, _, builder, _, _, _ = runtime
    template = pack.replay_output("P-TEMPLATE-EXTRACT", "normal")["result"]["template"]

    compact = builder._planning_template_context(template)

    assert compact["template_id"] == template["template_id"]
    assert compact["component_ids"] == [
        component["component_id"] for component in template["components"]
    ]
    assert compact["rules"]
    assert set(compact) == {"template_id", "component_ids", "rules"}


def test_revision_plan_normalizer_qualifies_short_information_keys(runtime):
    _, pack, _, _, _, executor, _, _ = runtime
    output = pack.replay_output("P-REVISION-PLAN", "normal")
    contract = output["result"]["revision_plan"]["narrative_architecture"]["section_contracts"][0]
    contract["unique_information_keys"] = ["短键"]

    normalized = executor._normalize_output("P-REVISION-PLAN", output)
    normalized_key = normalized["result"]["revision_plan"]["narrative_architecture"]["section_contracts"][0]["unique_information_keys"][0]

    assert normalized_key.startswith("短键:")
    assert len(normalized_key) >= 8
    assert pack.validate("P-REVISION-PLAN", "output", normalized) == []


def test_argument_normalizer_materializes_prior_work_and_team_evidence(runtime):
    _, pack, _, _, _, executor, _, _ = runtime
    output = pack.replay_output("P-ARGUMENT-ARCHITECTURE", "normal")
    envelope = pack.replay_input("P-ARGUMENT-ARCHITECTURE")
    envelope["payload"]["project_definition"] = {
        "items": [
            {
                "item_id": "existing-test-1",
                "item_type": "EXISTING_APPROACH",
                "content": {"statement": "测试既有工作基线"},
                "knowledge_status": "DOCUMENT_EXTRACTED",
                "source_refs": [],
            },
            {
                "item_id": "capability-test-1",
                "item_type": "CAPABILITY",
                "content": {"statement": "测试团队能力证据"},
                "knowledge_status": "DOCUMENT_EXTRACTED",
                "source_refs": [],
            },
            {
                "item_id": "innovation-test-1",
                "item_type": "INNOVATION",
                "content": {"existing_baseline": "测试创新对应基线"},
                "knowledge_status": "USER_ASSERTED",
                "source_refs": [],
            },
            {
                "item_id": "gap-test-1",
                "item_type": "GAP",
                "content": {"statement": "测试研究差距"},
                "knowledge_status": "USER_ASSERTED",
                "source_refs": [],
            },
            {
                "item_id": "objective-test-1",
                "item_type": "OBJECTIVE",
                "content": {"statement": "测试研究目标"},
                "knowledge_status": "USER_ASSERTED",
                "source_refs": [],
            },
        ]
    }
    envelope["payload"]["confirmed_facts"] = [{
        "claim_id": "fact-baseline-9",
        "claim_text": "测试基线B9的有来源方法边界",
        "subject_id": "B9",
        "source_refs": [],
    }]
    output["result"]["research_design_matrix"][0]["closest_prior_work_ids"] = []
    fact_backed_row = dict(output["result"]["research_design_matrix"][0])
    fact_backed_row["closest_prior_work_ids"] = ["EA-009"]
    output["result"]["research_design_matrix"].append(fact_backed_row)
    output["result"]["readiness"]["blocking_node_ids"] = ["TEST-TEAM-A成果"]

    normalized = executor._normalize_output("P-ARGUMENT-ARCHITECTURE", output, envelope)
    nodes = normalized["result"]["argument_architecture"]["nodes"]
    by_id = {node["node_id"]: node for node in nodes}
    prior_ids = normalized["result"]["research_design_matrix"][0]["closest_prior_work_ids"]

    assert by_id["existing-test-1"]["node_type"] == "CLOSEST_PRIOR_WORK"
    assert by_id["existing-test-1"]["status"] == "SUPPORTED"
    assert by_id["capability-test-1"]["node_type"] == "TEAM_EVIDENCE"
    assert by_id["capability-test-1"]["status"] == "SUPPORTED"
    assert by_id["gap-test-1"]["node_type"] == "RESEARCH_GAP"
    assert by_id["objective-test-1"]["node_type"] == "OBJECTIVE"
    assert by_id["innovation-test-1"]["node_type"] == "NOVEL_MECHANISM"
    assert "team-evidence-unknown" not in by_id
    assert {"existing-test-1", "closest-innovation-test-1"} <= set(prior_ids)
    assert normalized["result"]["readiness"]["blocking_node_ids"] == ["TEST-TEAM-A"]
    assert by_id["EA-009"]["node_type"] == "CLOSEST_PRIOR_WORK"
    assert by_id["EA-009"]["status"] == "UNKNOWN"
    assert "测试基线B9" in by_id["EA-009"]["statement"]


def test_project_definition_normalizes_deterministic_model_fields(runtime):
    _, pack, _, _, _, executor, *_ = runtime
    output = pack.replay_output("P-PROJECT-DEFINITION-EXTRACT", "normal")
    project_definition = output["result"]["project_definition"]
    project_definition["items"][0]["item_hash"] = "model-placeholder"
    project_definition["package_hash"] = "model-placeholder"
    project_definition["domain_readiness"][0]["missing_item_types"] = [
        "OBJECTIVE",
        "CLOSEST_PRIOR_WORK",
    ]
    questions = output["result"]["argument_graph_seed"]["research_questions"]
    output["result"]["argument_graph_seed"]["research_questions"] = questions * 3
    output["source_refs"] = [dict(project_definition["items"][0]["source_refs"][0])]
    output["source_refs"][0]["section_id"] = "含非规范字符的章节"

    normalized = executor._normalize_output(
        "P-PROJECT-DEFINITION-EXTRACT",
        output,
        {},
    )

    assert len(normalized["result"]["argument_graph_seed"]["research_questions"]) == 4
    assert normalized["result"]["project_definition"]["domain_readiness"][0]["missing_item_types"] == [
        "OBJECTIVE",
        "EXISTING_APPROACH",
    ]
    assert normalized["source_refs"][0]["section_id"] is None
    assert normalized["result"]["project_definition"]["items"][0]["item_hash"] != "model-placeholder"
    assert pack.validate("P-PROJECT-DEFINITION-EXTRACT", "output", normalized) == []


def test_fact_output_canonicalizes_subject_and_explicit_unknown_state(runtime):
    _, pack, *_ = runtime
    output = pack.replay_output("P-FACT-EXTRACT", "normal")
    fact = output["result"]["fact_candidates"][0]
    fact["subject_id"] = "项目"
    fact["claim_type"] = "FACT"
    fact["knowledge_status"] = "UNKNOWN"
    fact["temporal_status"] = "UNKNOWN"

    normalized = PromptExecutor._normalize_fact_output(output)
    normalized_fact = normalized["result"]["fact_candidates"][0]

    assert normalized_fact["subject_id"].startswith("subject-")
    assert normalized_fact["temporal_status"] == "CURRENT"
    assert normalized_fact["knowledge_status"] == "UNKNOWN"
    assert pack.validate("P-FACT-EXTRACT", "output", normalized) == []


def test_project_definition_recovers_null_truncated_graph_and_routes_missing_input(runtime):
    _, pack, *_ = runtime
    output = pack.replay_output("P-PROJECT-DEFINITION-EXTRACT", "normal")
    project_definition = output["result"]["project_definition"]
    project_definition["items"] = [project_definition["items"][0], None]
    project_definition["relations"] = [None]
    output["status"] = "REVISE"
    output["findings"] = [{
        "code": "DOCUMENT_TYPE_UNKNOWN",
        "severity": "P0",
        "category": "SCHEME",
        "target_type": "DOCUMENT",
        "target_path_or_span": "payload",
        "description": "Formal guide is unavailable.",
        "evidence_refs": [],
        "repairable": True,
        "repair_instruction": "Ask the project owner.",
        "suggested_route": "USER",
        "blocking": True,
    }]

    normalized = PromptExecutor._normalize_project_definition_output(output)
    item_types = {
        item["item_type"]
        for item in normalized["result"]["project_definition"]["items"]
    }

    assert normalized["status"] == "NEED_USER_INPUT"
    assert {
        "GAP", "PROBLEM", "OBJECTIVE", "WORK_PACKAGE", "METHOD",
        "EXPERIMENT", "INNOVATION", "DELIVERABLE", "METRIC",
    } <= item_types
    assert None not in normalized["result"]["project_definition"]["items"]
    assert pack.validate("P-PROJECT-DEFINITION-EXTRACT", "output", normalized) == []


def test_unexpected_workflow_exception_is_recorded_as_recoverable_block(runtime, monkeypatch):
    _, _, _, _, _, _, engine, _ = runtime
    project_id = create_project(engine.db)
    workflow = engine.start(project_id, "WF-1_PROJECT_INTAKE")

    async def fail_unexpectedly(*_args, **_kwargs):
        raise AttributeError("unexpected provider shape")

    monkeypatch.setattr(engine.executor, "execute", fail_unexpectedly)
    result = asyncio.run(engine.advance(workflow["id"]))

    assert result["status"] == "BLOCKED"
    assert result["state"]["runtime_recoverable"] is True
    assert "AttributeError" in result["state"]["last_error"]


def test_runtime_call_key_changes_when_execution_spec_changes(runtime):
    _, pack, _, _, _, executor, _, _ = runtime
    args = {
        "prompt_id": "P-PROJECT-DEFINITION-CRITIC",
        "project_id": "project-test",
        "workflow_id": "workflow-test",
        "input_hash": "a" * 64,
        "requested_call_key": None,
    }
    first = executor._call_key(**args)
    profiles = pack.profiles.get("profiles") or pack.profiles
    profiles["critic"]["max_output_tokens"] += 1
    second = executor._call_key(**args)

    assert first != second


def test_runtime_call_key_changes_when_contract_registry_changes(runtime, monkeypatch):
    _, _, _, _, _, executor, _, _ = runtime
    args = {
        "prompt_id": "P-PROJECT-READINESS-CRITIC",
        "project_id": "project-test",
        "workflow_id": "workflow-test",
        "input_hash": "b" * 64,
        "requested_call_key": None,
    }
    first = executor._call_key(**args)
    monkeypatch.setattr(
        "app.runtime_executor.CONTRACT_REGISTRY_VERSION",
        "future-contract-version",
    )
    second = executor._call_key(**args)

    assert first != second


def test_runtime_renormalizes_prior_enum_only_failure_without_model_call(runtime, monkeypatch):
    _, pack, db, _, _, executor, _, _ = runtime
    project_id = create_project(db)
    workflow_id = new_id("wf")
    envelope = pack.replay_input("P-PROJECT-READINESS-CRITIC")
    envelope["scope"]["project_id"] = project_id
    provider_output = pack.replay_output("P-PROJECT-READINESS-CRITIC")
    provider_output["result"]["domain_scores"][0]["missing_item_types"] = [
        "CLOSEST_PRIOR_WORK"
    ]
    input_hash = sha256_json(envelope)
    failed_run_id = new_id("run")
    db.execute(
        """INSERT INTO prompt_runs(
               id,project_id,workflow_id,prompt_id,status,model_id,endpoint_id,
               input_hash,output_hash,input_json,output_json,error,duration_ms,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            failed_run_id,
            project_id,
            workflow_id,
            "P-PROJECT-READINESS-CRITIC",
            "ERROR",
            "offline-critic-primary",
            "offline-primary",
            input_hash,
            sha256_json(provider_output),
            json.dumps(envelope, ensure_ascii=False),
            json.dumps(provider_output, ensure_ascii=False),
            "Output schema validation failed | "
            "/result/domain_scores/0/missing_item_types/0: "
            "'CLOSEST_PRIOR_WORK' is not one of ['EXISTING_APPROACH']",
            100,
            utc_now(),
        ),
    )

    async def model_must_not_be_called(*_args, **_kwargs):
        raise AssertionError("identical enum-only failure must be re-normalized locally")

    monkeypatch.setattr(executor.gateway, "invoke", model_must_not_be_called)
    result = asyncio.run(
        executor.execute(
            "P-PROJECT-READINESS-CRITIC",
            envelope,
            project_id=project_id,
            workflow_id=workflow_id,
        )
    )

    assert result["status"] in {"PASS", "REVISE", "NEED_USER_INPUT"}
    assert result["contract_recovered_from_run_id"] == failed_run_id
    assert result["output"]["result"]["domain_scores"][0]["missing_item_types"] == [
        "EXISTING_APPROACH"
    ]


def test_runtime_recovers_prior_field_ownership_failure_without_model_call(runtime, monkeypatch):
    _, pack, db, _, _, executor, _, _ = runtime
    project_id = create_project(db)
    workflow_id = new_id("wf")
    envelope = pack.replay_input("P-TEMPLATE-EXTRACT")
    envelope["scope"]["project_id"] = project_id
    provider_output = pack.replay_output("P-TEMPLATE-EXTRACT")
    exclusions = provider_output["result"].pop("source_fact_exclusions")
    provider_output["result"]["template"]["source_fact_exclusions"] = exclusions
    model_envelope, _ = executor._prepare_model_envelope(
        "P-TEMPLATE-EXTRACT",
        envelope,
    )
    input_hash = sha256_json(model_envelope)
    failed_run_id = new_id("run")
    db.execute(
        """INSERT INTO prompt_runs(
               id,project_id,workflow_id,prompt_id,status,model_id,endpoint_id,
               input_hash,output_hash,input_json,output_json,error,duration_ms,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            failed_run_id,
            project_id,
            workflow_id,
            "P-TEMPLATE-EXTRACT",
            "ERROR",
            "offline-general-primary",
            "offline-primary",
            input_hash,
            sha256_json(provider_output),
            json.dumps(model_envelope, ensure_ascii=False),
            json.dumps(provider_output, ensure_ascii=False),
            "Output schema validation failed | "
            "/result: 'source_fact_exclusions' is a required property; "
            "/result/template: Additional properties are not allowed "
            "('source_fact_exclusions' was unexpected)",
            107000,
            utc_now(),
        ),
    )

    async def model_must_not_be_called(*_args, **_kwargs):
        raise AssertionError("recoverable field ownership drift must be repaired locally")

    monkeypatch.setattr(executor.gateway, "invoke", model_must_not_be_called)
    result = asyncio.run(
        executor.execute(
            "P-TEMPLATE-EXTRACT",
            envelope,
            project_id=project_id,
            workflow_id=workflow_id,
        )
    )

    assert result["contract_recovered_from_run_id"] == failed_run_id
    assert result["output"]["result"]["source_fact_exclusions"] == exclusions
    assert "source_fact_exclusions" not in result["output"]["result"]["template"]
    assert pack.validate("P-TEMPLATE-EXTRACT", "output", result["output"]) == []


def test_project_definition_model_input_uses_minimal_sufficient_compaction(runtime):
    _, pack, _, _, _, executor, _, _ = runtime
    envelope = pack.replay_input("P-PROJECT-DEFINITION-EXTRACT")
    document = envelope["payload"]["source_documents"][0]
    original_section = document["sections"][0]
    document["document_role"] = "CURRENT_PROPOSAL"
    document["sections"] = [
        {
            **original_section,
            "section_id": f"section-{index:03d}",
            "title": f"RC-{index}: 研究内容",
            "text": f"第{index}项研究内容、方法、实验和创新。",
        }
        for index in range(60)
    ]

    compact, metadata = executor._prepare_model_envelope(
        "P-PROJECT-DEFINITION-EXTRACT",
        envelope,
    )

    assert len(envelope["payload"]["source_documents"][0]["sections"]) == 60
    assert len(compact["payload"]["source_documents"][0]["sections"]) == 18
    assert metadata["strategy"] == "PROJECT_DEFINITION_MINIMAL_SUFFICIENT_GRAPH"
    assert metadata["quality_guard_uses_full_context"] is True
    assert "项目对象不超过18个" in compact["payload"]["extraction_scope"][-1]
    assert pack.validate("P-PROJECT-DEFINITION-EXTRACT", "input", compact) == []


def test_fact_model_input_uses_representative_evidence_compaction(runtime):
    _, pack, _, _, _, executor, _, _ = runtime
    envelope = pack.replay_input("P-FACT-EXTRACT")
    base_span = envelope["payload"]["source_spans"][0]
    envelope["payload"]["source_spans"] = [
        {
            **base_span,
            "span_id": f"span-{index:03d}",
            "text": f"第{index}项研究问题、方法、实验和创新测试事实。",
            "source_ref": {
                **base_span["source_ref"],
                "source_id": f"span-{index:03d}",
                "section_id": f"span-{index:03d}",
                "source_type": (
                    "EVIDENCE_MATERIAL" if index < 20 else "CURRENT_PROPOSAL"
                ),
            },
        }
        for index in range(80)
    ]
    base_fact = pack.replay_output(
        "P-FACT-EXTRACT",
        "normal",
    )["result"]["fact_candidates"][0]
    envelope["payload"]["existing_facts"] = [
        {**base_fact, "claim_id": f"existing-fact-{index:03d}"}
        for index in range(40)
    ]

    compact, metadata = executor._prepare_model_envelope("P-FACT-EXTRACT", envelope)

    assert len(envelope["payload"]["source_spans"]) == 80
    assert len(compact["payload"]["source_spans"]) <= 32
    assert len(compact["payload"]["existing_facts"]) == 18
    assert metadata["strategy"] == "FACT_REPRESENTATIVE_EVIDENCE_PACKAGE"
    assert metadata["quality_guard_uses_full_context"] is True
    assert pack.validate("P-FACT-EXTRACT", "input", compact) == []


def test_fact_output_removes_coverage_refs_to_non_output_existing_facts(runtime):
    _, pack, *_ = runtime
    output = pack.replay_output("P-FACT-EXTRACT", "normal")
    output["result"]["coverage"][0]["claim_ids"].append("existing-fact-not-emitted")

    normalized = PromptExecutor._normalize_fact_output(output)

    assert normalized["result"]["coverage"][0]["claim_ids"] == ["fact-001"]
    assert "removed 1 coverage reference" in normalized["warnings"][-1]
    assert pack.validate("P-FACT-EXTRACT", "output", normalized) == []


def test_numeric_guard_ignores_slash_continuations_in_test_identifiers():
    from app import track_b as _track_b  # noqa: F401

    text = (
        "成果TEST-PROTOTYPE-001/002/003和TEST-EXPERIMENT-001/002"
        "均为测试对象，并完成30组测试。"
    )

    assert _substantive_numeric_tokens(text) == ["30"]


def test_document_context_builder_uses_uploaded_material(runtime):
    settings, pack, db, _, builder, *_ = runtime
    project_id = create_project(db)
    parsed = parse_document("guide.md", b"# Guide\nMust include technical route.", "APPLICATION_GUIDE", "INTERNAL")
    path = settings.uploads_dir / "guide.md"
    path.write_bytes(b"x")
    db.execute(
        "INSERT INTO documents(id,project_id,filename,role,security_level,document_hash,file_path,parsed_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (parsed["document_id"], project_id, "guide.md", "APPLICATION_GUIDE", "INTERNAL", parsed["document_hash"], str(path), json.dumps(parsed), utc_now()),
    )
    envelope = builder.build("P-SCHEME-EXTRACT", project_id)
    assert pack.validate("P-SCHEME-EXTRACT", "input", envelope) == []
    assert envelope["payload"]["guide_documents"][0]["title"] == "guide"


def test_live_security_context_uses_uploaded_document_labels(tmp_path: Path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("MODEL_RUNTIME_MODE", "LIVE")
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PROMPT_PACK_DIR", str(root / "prompt_pack"))
    settings = Settings.load()
    pack = PromptPack(settings.prompt_pack_dir)
    db = Database(settings.db_path)
    project_id = create_project(db)
    parsed = parse_document(
        "brief.md",
        "# Project brief\nResearch question and validation plan.".encode(),
        "PROJECT_BRIEF",
        "INTERNAL",
    )
    path = settings.uploads_dir / "brief.md"
    path.write_bytes(b"project material")
    db.execute(
        "INSERT INTO documents(id,project_id,filename,role,security_level,document_hash,file_path,parsed_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            parsed["document_id"],
            project_id,
            "brief.md",
            "PROJECT_BRIEF",
            "INTERNAL",
            parsed["document_hash"],
            str(path),
            json.dumps(parsed),
            utc_now(),
        ),
    )

    envelope = ContextBuilder(db, pack).build("P-SECURITY-CLASSIFY", project_id)

    assert pack.validate("P-SECURITY-CLASSIFY", "input", envelope) == []
    assert envelope["payload"]["existing_labels"] == [
        {
            "object_id": parsed["document_id"],
            "security_level": "INTERNAL",
            "basis": "上传材料登记时指定的安全等级",
        }
    ]

    producer_output = pack.replay_output("P-SECURITY-CLASSIFY", "normal")
    producer_output["result"]["object_id"] = parsed["document_id"]
    db.execute(
        "INSERT INTO artifacts(id,project_id,workflow_id,artifact_type,prompt_id,version,status,security_level,context_hash,content_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            new_id("artifact"),
            project_id,
            None,
            "PROMPT_OUTPUT",
            "P-SECURITY-CLASSIFY",
            1,
            "CANDIDATE",
            "INTERNAL",
            "0" * 64,
            json.dumps(producer_output),
            utc_now(),
        ),
    )
    critic_envelope = ContextBuilder(db, pack).build(
        "P-SECURITY-CLASSIFY-CRITIC",
        project_id,
    )
    assert critic_envelope["payload"]["original_object"]["object_id"] == parsed["document_id"]
    assert critic_envelope["payload"]["original_object"]["content"]["title"] == "brief"
    assert critic_envelope["payload"]["deterministic_findings"] == producer_output["findings"]

    with pytest.raises(WorkflowInputRequired) as exc_info:
        ContextBuilder(db, pack).build("P-SCHEME-EXTRACT", project_id)
    assert exc_info.value.gate_type == APPLICATION_GUIDE_INPUT
    assert "payload.guide_documents" in exc_info.value.missing_paths


def test_security_router_blocks_unapproved_online(runtime):
    _, pack, _, router, *_ = runtime
    envelope = pack.replay_input("P-PUBLIC-RESEARCH-PLAN")
    envelope["security_context"]["input_max_security_level"] = "INTERNAL"
    with pytest.raises(RoutingDenied):
        router.route("P-PUBLIC-RESEARCH-PLAN", envelope)


def test_project_intake_pauses_at_expected_gate(runtime):
    _, _, db, _, _, _, engine, _ = runtime
    project_id = create_project(db)

    async def run():
        wf = engine.start(project_id, "WF-1_PROJECT_INTAKE")
        wf = await engine.advance(wf["id"])
        assert wf["status"] == "WAITING_GATE"
        assert wf["current_step"] == 4
        gates = [g for g in engine.list_gates(workflow_id=wf["id"]) if g["status"] == "OPEN"]
        assert gates[0]["gate_type"] == "SCHEME_CONFIRMATION"

    asyncio.run(run())


def test_technical_block_can_retry_same_uncommitted_step(runtime):
    settings, _, db, _, _, _, engine, _ = runtime
    project_id = create_project(db)
    add_standard_materials(settings, db, project_id)
    workflow = engine.start(project_id, "WF-1_PROJECT_INTAKE")
    state = workflow["state"]
    state["last_error"] = "Output schema validation failed"
    engine._update(workflow, status="BLOCKED", state=state)

    resumed = asyncio.run(engine.advance(workflow["id"]))

    assert resumed["status"] == "WAITING_GATE"
    assert resumed["current_step"] == 4
    assert resumed["state"]["technical_retry_attempts"]["0"] == 1


def test_acceptance_run_allows_additional_technical_recovery(runtime):
    settings, _, db, _, _, _, engine, _ = runtime
    project_id = create_project(db)
    add_standard_materials(settings, db, project_id)
    workflow = engine.start(
        project_id,
        "WF-1_PROJECT_INTAKE",
        {"acceptance_run": True},
    )
    state = workflow["state"]
    state["last_error"] = "Transient provider output failure"
    state["technical_retry_attempts"] = {"0": 4}
    engine._update(workflow, status="BLOCKED", state=state)

    resumed = asyncio.run(engine.advance(workflow["id"]))

    assert resumed["status"] == "WAITING_GATE"
    assert resumed["state"]["technical_retry_attempts"]["0"] == 5


def test_deterministic_quality_block_retries_and_supersedes_step_result(runtime):
    settings, _, db, _, _, _, engine, _ = runtime
    project_id = create_project(db)
    add_standard_materials(settings, db, project_id)
    workflow = engine.start(
        project_id,
        "WF-1_PROJECT_INTAKE",
        {"acceptance_run": True},
    )
    state = workflow["state"]
    state["step_results"]["0"] = {
        "prompt_id": "P-SECURITY-CLASSIFY",
        "run_id": "run-superseded",
        "status": "REVISE",
    }
    engine.quality_manager.observe_prompt_result(
        project_id=project_id,
        workflow_id=workflow["id"],
        prompt_id="P-SECURITY-CLASSIFY",
        run_id="run-superseded",
        status="REVISE",
        output={
            "findings": [{
                "code": "QG_SECURITY_RECHECK",
                "severity": "P1",
                "category": "SECURITY",
                "target_type": "DOCUMENT",
                "target_path_or_span": "payload",
                "description": "Deterministic security post-processing changed.",
                "required_action": "Rerun the producer and its critic.",
                "suggested_route": "SECURITY_CLASSIFICATION_AGENT",
                "blocking": True,
                "repairable": True,
                "evidence_refs": [],
            }],
        },
    )
    engine._update(workflow, status="BLOCKED", state=state)

    resumed = asyncio.run(engine.advance(workflow["id"]))

    assert resumed["status"] == "WAITING_GATE", json.dumps(resumed, ensure_ascii=False)
    assert resumed["state"]["technical_retry_attempts"]["0"] == 1
    assert resumed["state"]["superseded_step_results"]["0"][0]["run_id"] == "run-superseded"


def test_all_workflows_and_docx_export(runtime):
    _, _, db, _, _, _, engine, exporter = runtime
    project_id = create_project(db)
    settings = runtime[0]
    add_standard_materials(settings, db, project_id)

    async def run():
        for workflow_type in [
            "WF-1_PROJECT_INTAKE",
            "WF-2_TEMPLATE_EXTRACTION",
            "WF-3_HYBRID_ONLINE_ASSIST",
            "WF-4_PROPOSAL_AUTHORING",
            "WF-5_SECURITY_REVIEW_AND_EXPORT",
        ]:
            wf = await finish_workflow(engine, project_id, workflow_type)
            assert wf["status"] == "COMPLETED", wf["state"].get("last_error")

    asyncio.run(run())
    path = exporter.export(project_id)
    assert path.exists()
    assert path.stat().st_size > 10_000
    package = exporter.export_package(project_id)
    assert package.exists()
    assert package.stat().st_size > 10_000


def test_targeted_repair_normalizer_enforces_fact_collection_scope(runtime):
    _, pack, _, _, _, executor, _, _ = runtime
    output = pack.replay_output("P-TARGETED-REPAIR", "normal")
    envelope = pack.replay_input("P-TARGETED-REPAIR")
    original = {
        "fact_candidates": [
            {
                "claim_id": "METRIC-PROJ-001",
                "claim_text": "项目拟形成评价指标。",
                "claim_type": "FACT",
            },
            {
                "claim_id": "FACT-PROJ-002",
                "claim_text": "来源材料为项目设计输入。",
                "claim_type": "FACT",
            },
        ]
    }
    envelope["payload"]["original_object"]["content"] = copy.deepcopy(original)
    envelope["payload"]["allowed_paths"] = [
        "content.fact_candidates[claim_id=METRIC-PROJ-001].claim_type",
    ]
    envelope["payload"]["findings_to_repair"] = [{
        "code": "FACT_CRITIC_STATUS_UPGRADE",
        "severity": "P1",
        "category": "FACT",
        "target_type": "FACT_CANDIDATE",
        "target_path_or_span": "fact_candidates[METRIC-PROJ-001].claim_type",
        "description": "项目预期指标被误标为既成事实。",
        "evidence_refs": [],
        "repairable": True,
        "repair_instruction": "将claim_type改为EXPECTED_RESULT。",
        "suggested_route": "ORIGINAL_PRODUCER",
        "blocking": True,
    }]
    assert pack.validate("P-TARGETED-REPAIR", "input", envelope) == []
    repaired = copy.deepcopy(original)
    repaired["fact_candidates"][0]["claim_type"] = "EXPECTED_RESULT"
    repaired["fact_candidates"][0]["claim_text"] = "unauthorized rewrite"
    repaired["fact_candidates"][1]["claim_type"] = "PLAN"
    repaired["fact_candidates"].append({
        "claim_id": "UNAUTHORIZED-003",
        "claim_text": "unauthorized addition",
        "claim_type": "FACT",
    })
    output["status"] = "REVISE"
    output["result"]["repaired_object"]["content"] = repaired
    output["result"]["resolved_finding_codes"] = ["FACT_CRITIC_STATUS_UPGRADE"]
    output["result"]["unresolved_finding_codes"] = []
    output["findings"] = []

    normalized = executor._normalize_output("P-TARGETED-REPAIR", output, envelope)
    assert pack.validate("P-TARGETED-REPAIR", "output", normalized) == []

    facts = normalized["result"]["repaired_object"]["content"]["fact_candidates"]
    assert len(facts) == 2
    assert facts[0]["claim_type"] == "EXPECTED_RESULT"
    assert facts[0]["claim_text"] == original["fact_candidates"][0]["claim_text"]
    assert facts[1] == original["fact_candidates"][1]
    assert normalized["result"]["changed_paths"] == [
        "content.fact_candidates[claim_id=METRIC-PROJ-001].claim_type",
    ]
    assert normalized["status"] == "PASS"
    assert any("outside the critic-authorized path scope" in warning for warning in normalized["warnings"])


def test_runtime_does_not_reuse_failed_output_from_changed_prompt_contract(runtime, monkeypatch):
    _, pack, db, _, _, executor, _, _ = runtime
    project_id = create_project(db)
    workflow_id = new_id("wf")
    prompt_id = "P-TEMPLATE-EXTRACT"
    envelope = pack.replay_input(prompt_id)
    envelope["scope"]["project_id"] = project_id
    model_envelope, _ = executor._prepare_model_envelope(prompt_id, envelope)
    input_hash = sha256_json(model_envelope)
    failed_run_id = new_id("run")
    provider_output = pack.replay_output(prompt_id)
    exclusions = provider_output["result"].pop("source_fact_exclusions")
    provider_output["result"]["template"]["source_fact_exclusions"] = exclusions
    db.execute(
        """INSERT INTO prompt_runs(
               id,project_id,workflow_id,prompt_id,status,model_id,endpoint_id,
               input_hash,output_hash,input_json,output_json,error,duration_ms,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            failed_run_id, project_id, workflow_id, prompt_id, "ERROR",
            "offline-general-primary", "offline-primary", input_hash,
            sha256_json(provider_output), json.dumps(model_envelope, ensure_ascii=False),
            json.dumps(provider_output, ensure_ascii=False),
            "Output schema validation failed | ownership drift", 100, utc_now(),
        ),
    )
    db.audit(
        "MODEL_CALL_FAILED",
        project_id=project_id,
        object_id="call-old-contract",
        metadata={
            "run_id": failed_run_id,
            "deterministic_recoverable": True,
            "model_request_spec_hash": "old-prompt-contract-hash",
        },
    )
    calls = {"count": 0}

    async def fresh_model(*_args, **_kwargs):
        calls["count"] += 1
        output = pack.replay_output(prompt_id)
        return LLMResult(
            output=output,
            raw_text=json.dumps(output, ensure_ascii=False),
            model_id="offline-general-primary",
            endpoint_id="offline-primary",
        )

    monkeypatch.setattr(executor.gateway, "invoke", fresh_model)
    result = asyncio.run(
        executor.execute(
            prompt_id,
            envelope,
            project_id=project_id,
            workflow_id=workflow_id,
        )
    )

    assert calls["count"] == 1
    assert result["contract_recovered_from_run_id"] is None


def test_runtime_does_not_reuse_non_contract_failure(runtime, monkeypatch):
    _, pack, db, _, _, executor, _, _ = runtime
    project_id = create_project(db)
    workflow_id = new_id("wf")
    prompt_id = "P-TEMPLATE-EXTRACT"
    envelope = pack.replay_input(prompt_id)
    envelope["scope"]["project_id"] = project_id
    model_envelope, _ = executor._prepare_model_envelope(prompt_id, envelope)
    input_hash = sha256_json(model_envelope)
    failed_run_id = new_id("run")
    provider_output = pack.replay_output(prompt_id)
    db.execute(
        """INSERT INTO prompt_runs(
               id,project_id,workflow_id,prompt_id,status,model_id,endpoint_id,
               input_hash,output_hash,input_json,output_json,error,duration_ms,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            failed_run_id, project_id, workflow_id, prompt_id, "ERROR",
            "offline-general-primary", "offline-primary", input_hash,
            sha256_json(provider_output), json.dumps(model_envelope, ensure_ascii=False),
            json.dumps(provider_output, ensure_ascii=False),
            "LLM endpoint returned 500: provider unavailable", 100, utc_now(),
        ),
    )
    calls = {"count": 0}

    async def fresh_model(*_args, **_kwargs):
        calls["count"] += 1
        output = pack.replay_output(prompt_id)
        return LLMResult(
            output=output,
            raw_text=json.dumps(output, ensure_ascii=False),
            model_id="offline-general-primary",
            endpoint_id="offline-primary",
        )

    monkeypatch.setattr(executor.gateway, "invoke", fresh_model)
    result = asyncio.run(
        executor.execute(
            prompt_id,
            envelope,
            project_id=project_id,
            workflow_id=workflow_id,
        )
    )

    assert calls["count"] == 1
    assert result["contract_recovered_from_run_id"] is None


def test_normalization_audit_records_paths_without_copying_values(runtime):
    _, _, _, _, _, executor, _, _ = runtime
    before = {"result": {"template": {"source_fact_exclusions": ["secret"]}}}
    after = {"result": {"source_fact_exclusions": ["secret"], "template": {}}}

    audit = executor._normalization_audit(before, after)

    assert audit["changed"] is True
    paths = {(item["operation"], item["path"]) for item in audit["changes"]}
    assert ("REMOVE", "/result/template/source_fact_exclusions") in paths
    assert ("ADD", "/result/source_fact_exclusions") in paths
    assert all("before_value" not in item and "after_value" not in item for item in audit["changes"])


def test_contract_upgrade_gets_one_recovery_attempt_after_retry_limit(runtime, monkeypatch):
    """A new normalizer can revalidate persisted provider output without another model call."""
    _, pack, db, _, builder, executor, engine, _ = runtime
    project_id = create_project(db)
    workflow = engine.start(project_id, "WF-2_TEMPLATE_EXTRACTION")
    state = workflow["state"]
    state["technical_retry_attempts"] = {"0": 2}
    state["last_error"] = "old output schema validation failed"
    engine._update(workflow, status="BLOCKED", current_step=0, state=state)

    provider_output = pack.replay_output("P-TEMPLATE-EXTRACT")
    db.execute(
        """INSERT INTO prompt_runs(
               id,project_id,workflow_id,prompt_id,status,model_id,endpoint_id,
               input_hash,output_hash,input_json,output_json,error,duration_ms,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            new_id("run"),
            project_id,
            workflow["id"],
            "P-TEMPLATE-EXTRACT",
            "ERROR",
            "offline-general-primary",
            "offline-primary",
            "old-input-hash",
            sha256_json(provider_output),
            "{}",
            json.dumps(provider_output, ensure_ascii=False),
            "old contract failure",
            1,
            utc_now(),
        ),
    )

    def build(prompt_id, project_id_arg, **_kwargs):
        envelope = pack.replay_input(prompt_id)
        envelope["scope"]["project_id"] = project_id_arg
        return envelope

    calls: list[str] = []

    async def execute(prompt_id, envelope, **_kwargs):
        calls.append(prompt_id)
        output = pack.replay_output(prompt_id)
        if prompt_id == "P-TEMPLATE-CRITIC":
            output["status"] = "NEED_USER_INPUT"
            output["user_questions"] = ["请确认模板范围。"]
        return {
            "run_id": new_id("run"),
            "status": output["status"],
            "route": {
                "environment": "OFFLINE_LOCAL",
                "model_id": "test-model",
                "endpoint_id": "test-endpoint",
            },
            "output": output,
        }

    monkeypatch.setattr(builder, "build", build)
    monkeypatch.setattr(executor, "execute", execute)

    advanced = asyncio.run(engine.advance(workflow["id"]))

    assert calls[:2] == ["P-TEMPLATE-EXTRACT", "P-TEMPLATE-CRITIC"]
    assert advanced["status"] == "WAITING_GATE"
    assert advanced["state"]["technical_retry_attempts"]["0"] == 2
    assert advanced["state"]["contract_migration_retry_versions"]["0"] == executor.output_normalizer_version


def test_runtime_surfaces_secondary_error_evidence_persistence_failure(runtime, monkeypatch):
    _, pack, db, _, _, executor, _, _ = runtime
    project_id = create_project(db)
    prompt_id = "P-TEMPLATE-EXTRACT"
    envelope = pack.replay_input(prompt_id)
    envelope["scope"]["project_id"] = project_id

    async def invalid_model_output(*_args, **_kwargs):
        return LLMResult(
            output={},
            raw_text="{}",
            model_id="offline-general-primary",
            endpoint_id="offline-primary",
        )

    monkeypatch.setattr(executor.gateway, "invoke", invalid_model_output)
    monkeypatch.setattr(
        executor,
        "_commit_error",
        lambda **_kwargs: "OSError: simulated disk full",
    )

    with pytest.raises(PromptExecutionError) as captured:
        asyncio.run(
            executor.execute(
                prompt_id,
                envelope,
                project_id=project_id,
                workflow_id=new_id("wf"),
            )
        )

    message = str(captured.value)
    assert message.startswith("Output schema validation failed")
    assert "ERROR_EVIDENCE_PERSISTENCE_FAILED" in message
    assert "simulated disk full" in message
