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
from app.quality_guard import build_guard_report, disabled_guard_report
from app.research import PublicResearchService
from app.security import RoutingDenied, SecurityRouter
from app.simulated_llm import SimulatedLLM
from app.util import new_id, sha256_json, utc_now
from app.runtime_api import WorkflowEngine
from app.workflow_status import should_pause_automatic_advancement
from app.agent_prompt_kernel import _substantive_numeric_tokens
from app.output_integrity import attach_trusted_source_catalog
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
        if should_pause_automatic_advancement(wf["status"]):
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
    _, pack, _, _, _, executor, *_ = runtime
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

    normalized = executor._normalize_output("P-SCHEME-EXTRACT", output, envelope)
    source_ref = normalized["result"]["scheme_profile"]["rules"][0]["source_refs"][0]

    assert source_ref["document_version_id"] == document["document_version_id"]
    assert source_ref["quoted_text"] == section["text"]
    assert source_ref["source_hash"] == section["text_hash"]
    assert source_ref["span_start"] == 0
    assert source_ref["span_end"] == len(section["text"])
    # Provenance enrichment is structural; it must not silently rewrite the
    # provider-owned semantic profile hash.
    assert normalized["result"]["scheme_profile"]["profile_hash"] == "0" * 64
    assert pack.validate("P-SCHEME-EXTRACT", "output", normalized) == []


def test_simulated_scheme_output_binds_replay_sources_to_current_input(runtime):
    _, pack, _, _, _, executor, *_ = runtime
    envelope = pack.replay_input("P-SCHEME-EXTRACT")
    document = envelope["payload"]["guide_documents"][0]
    document["document_id"] = "document-live"
    document["document_version_id"] = "document-version-live"
    document["sections"][0]["section_id"] = "section-live"

    output = SimulatedLLM(pack).invoke("P-SCHEME-EXTRACT", envelope)
    normalized = executor._normalize_output("P-SCHEME-EXTRACT", output, envelope)
    source_ref = normalized["result"]["scheme_profile"]["rules"][0]["source_refs"][0]

    assert source_ref["source_id"] == "section-live"
    assert source_ref["document_version_id"] == "document-version-live"
    assert source_ref["section_id"] == "section-live"


def test_simulated_source_binding_does_not_rewrite_public_sources(runtime):
    _, pack, *_ = runtime
    envelope = pack.replay_input("P-SCHEME-EXTRACT")
    output = {
        "source_refs": [{
            "source_id": "public-source-live",
            "source_type": "PUBLIC_SOURCE",
            "document_version_id": None,
            "section_id": None,
            "span_start": None,
            "span_end": None,
            "quoted_text": "公开检索结果",
            "source_hash": "b" * 64,
            "authority_rank": 50,
            "security_level": "PUBLIC",
        }]
    }

    bound = SimulatedLLM(pack)._bind_replay_source_refs(copy.deepcopy(output), envelope)

    assert bound == output


def test_revision_plan_receives_approved_argument_graph(runtime):
    settings, pack, db, _, _, _, engine, _ = runtime
    project_id = create_project(db, internet=False)
    add_standard_materials(settings, db, project_id)

    async def run_until_plan():
        for workflow_type in ["WF-1_PROJECT_INTAKE", "WF-2_TEMPLATE_EXTRACTION"]:
            workflow = await finish_workflow(engine, project_id, workflow_type)
            assert workflow["status"] == "COMPLETED", workflow["state"].get("last_error")
        workflow = await finish_workflow(engine, project_id, "WF-4_PROPOSAL_AUTHORING")
        return workflow

    asyncio.run(run_until_plan())
    row = db.fetchone(
        "SELECT input_json FROM prompt_runs WHERE project_id=? AND prompt_id='P-REVISION-PLAN' ORDER BY created_at DESC LIMIT 1",
        (project_id,),
    )
    assert row is not None
    payload = json.loads(row["input_json"])["payload"]
    graph = payload.get("argument_graph") or {}
    assert graph.get("central_proposition", {}).get("node_id")
    assert graph.get("research_questions")

    graph_ids = {
        str(graph["central_proposition"]["node_id"]),
        *(str(item["node_id"]) for item in graph.get("research_questions") or []),
        *(str(item["node_id"]) for item in graph.get("nodes") or []),
    }
    plan_output = SimulatedLLM(pack).invoke("P-REVISION-PLAN", json.loads(row["input_json"]))
    referenced_ids = {
        str(value)
        for contract in plan_output["result"]["revision_plan"]["narrative_architecture"]["section_contracts"]
        for field in ("must_advance_claim_ids", "must_use_evidence_ids")
        for value in contract.get(field) or []
    }
    assert referenced_ids <= graph_ids


def test_simulated_content_traces_primary_claim_without_self_evidence(runtime):
    _, pack, *_ = runtime
    envelope = pack.replay_input("P-WRITE-CONTENT")
    blueprint_paragraph = envelope["payload"]["approved_blueprint"]["paragraphs"][0]
    blueprint_paragraph["required_evidence_ids"] = []
    blueprint_paragraph["fact_slots"] = []
    primary_claim_id = blueprint_paragraph["primary_claim_id"]

    output = SimulatedLLM(pack).invoke("P-WRITE-CONTENT", envelope)
    paragraph = output["result"]["paragraphs"][0]
    traces = {
        trace["trace_id"]: trace
        for trace in output["result"]["trace_links"]
    }

    assert primary_claim_id not in paragraph["evidence_ids"]
    assert paragraph["trace_link_ids"]
    assert any(
        traces[trace_id]["source_id"] == primary_claim_id
        for trace_id in paragraph["trace_link_ids"]
    )


def test_simulated_integration_critic_uses_approved_argument_ids(runtime):
    _, pack, _, _, _, executor, *_ = runtime
    envelope = pack.replay_input("P-INTEGRATION-CRITIC")
    replacements = {
        "prop-001": "prop-live",
        "rq-001": "question-live-a",
        "rq-002": "question-live-b",
        "gap-001": "gap-live",
        "obj-001": "objective-live",
        "wp-001": "work-live-a",
        "wp-002": "work-live-b",
        "method-001": "method-live",
        "exp-001": "experiment-live",
        "innov-001": "innovation-live",
    }

    def replace_ids(value):
        if isinstance(value, list):
            return [replace_ids(item) for item in value]
        if isinstance(value, dict):
            return {key: replace_ids(item) for key, item in value.items()}
        return replacements.get(value, value)

    payload = envelope["payload"]
    payload["argument_graph"] = replace_ids(payload["argument_graph"])
    payload["narrative_architecture"] = replace_ids(payload["narrative_architecture"])
    payload["candidate_sections"] = replace_ids(payload["candidate_sections"])

    output = SimulatedLLM(pack).invoke("P-INTEGRATION-CRITIC", envelope)
    normalized = executor._normalize_output("P-INTEGRATION-CRITIC", output, envelope)
    graph = payload["argument_graph"]
    trusted_graph_ids = {
        str(graph["central_proposition"]["node_id"]),
        *(str(item["node_id"]) for item in graph.get("research_questions") or []),
        *(str(item["node_id"]) for item in graph.get("nodes") or []),
    }
    chain_ids = {
        str(value)
        for chain in normalized["result"]["argument_chain_checks"]
        for field in ("source_ids", "target_ids")
        for value in chain[field]
    }

    assert len(normalized["result"]["argument_chain_checks"]) >= 4
    assert chain_ids <= trusted_graph_ids
    assert not ({"rq-001", "rq-002", "prop-001"} & chain_ids)
    assert normalized["result"]["central_proposition_coverage"]["central_proposition_id"] == "prop-live"
    assert pack.validate("P-INTEGRATION-CRITIC", "output", normalized) == []


def test_simulated_project_definition_entity_references_are_closed(runtime):
    _, pack, _, _, _, executor, *_ = runtime
    envelope = pack.replay_input("P-PROJECT-DEFINITION-EXTRACT")

    output = SimulatedLLM(pack).invoke("P-PROJECT-DEFINITION-EXTRACT", envelope)
    normalized = executor._normalize_output(
        "P-PROJECT-DEFINITION-EXTRACT",
        output,
        envelope,
    )

    item_ids = {
        item["item_id"]
        for item in normalized["result"]["project_definition"]["items"]
    }
    referenced_method_ids = {
        method_id
        for item in normalized["result"]["project_definition"]["items"]
        if item["item_type"] == "WORK_PACKAGE"
        for method_id in item["content"]["methods"]
    }
    assert referenced_method_ids <= item_ids


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


def test_scheme_output_does_not_fabricate_rule_when_model_reports_missing_rules(runtime):
    _, pack, _, _, _, executor, *_ = runtime
    envelope = pack.replay_input("P-SCHEME-EXTRACT")
    output = pack.replay_output("P-SCHEME-EXTRACT", "normal")
    output["status"] = "REVISE"
    output["result"]["scheme_profile"]["rules"] = []
    output["result"]["extraction_coverage"] = []

    normalized = executor._normalize_output("P-SCHEME-EXTRACT", output, envelope)

    assert normalized["status"] == "REVISE"
    assert normalized["result"]["scheme_profile"]["rules"] == []
    assert normalized["result"]["extraction_coverage"] == []
    errors = pack.validate("P-SCHEME-EXTRACT", "output", normalized)
    assert any(
        error.startswith("/result/scheme_profile/rules:")
        and "non-empty" in error
        for error in errors
    )


def test_need_user_input_gate_without_questions_rejects_empty_confirmation(runtime):
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

    with pytest.raises(ValueError, match="没有可回答的问题"):
        engine.decide_gate(
            gate_id,
            action="CONFIRM",
            decided_by="pytest",
            decided_role="PROJECT_OWNER",
        )

    updated = engine.get(workflow["id"])
    assert updated["current_step"] == 0
    assert updated["state"]["step_results"]["0"]["run_id"] == "run-accepted-test"
    assert "superseded_step_results" not in updated["state"]
    assert engine._gate(gate_id)["status"] == "OPEN"


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
    assert normalized["findings"][0]["evidence_refs"] == ["domain_readiness[10]"]
    assert normalized["findings"][0]["category"] == "EVIDENCE"
    assert normalized["status"] == "REVISE"
    errors = pack.validate("P-SCHEME-CRITIC", "output", normalized)
    assert not any(error.startswith("/findings/0/category:") for error in errors)
    assert any(error.startswith("/findings/0/evidence_refs/0:") for error in errors)


def test_normalizer_merges_nested_response_warnings_when_top_level_exists(runtime):
    _, pack, _, _, _, executor, _, _ = runtime
    output = pack.replay_output("P-WRITE-BLUEPRINT-CRITIC", "normal")
    output["warnings"] = ["top-level warning"]
    output["result"]["warnings"] = ["nested warning"]

    normalized = executor._normalize_output("P-WRITE-BLUEPRINT-CRITIC", output)

    assert "warnings" not in normalized["result"]
    assert normalized["warnings"][:2] == ["top-level warning", "nested warning"]
    assert pack.validate("P-WRITE-BLUEPRINT-CRITIC", "output", normalized) == []


def test_normalizer_does_not_guess_unregistered_source_preservation_alias(runtime):
    _, pack, _, _, _, executor, _, _ = runtime
    output = pack.replay_output("P-WRITE-CONTENT", "normal")
    output["result"]["source_preservation_summary"][0]["action"] = "DISTRIBUTED"

    normalized = executor._normalize_output("P-WRITE-CONTENT", output)

    assert normalized["result"]["source_preservation_summary"][0]["action"] == "DISTRIBUTED"
    errors = pack.validate("P-WRITE-CONTENT", "output", normalized)
    assert any(
        error.startswith("/result/source_preservation_summary/0/action:")
        for error in errors
    )


def test_expression_normalizer_restores_polished_lineage_action_from_input(runtime):
    _, pack, _, _, _, executor, _, _ = runtime
    envelope = pack.replay_input("P-EXPRESSION-POLISH")
    output = pack.replay_output("P-EXPRESSION-POLISH", "normal")
    envelope["payload"]["content_candidate"]["source_preservation_summary"][0][
        "action"
    ] = "PRESERVED"
    output["result"]["source_preservation_summary"][0]["action"] = "POLISHED"

    normalized = executor._normalize_output(
        "P-EXPRESSION-POLISH",
        output,
        envelope,
    )

    assert normalized["result"]["source_preservation_summary"][0]["action"] == "PRESERVED"
    assert any(
        warning.startswith("SYSTEM_EXPRESSION_SOURCE_LINEAGE_ACTION_NORMALIZATION:")
        for warning in normalized["warnings"]
    )
    assert pack.validate("P-EXPRESSION-POLISH", "output", normalized) == []


def test_expression_normalizer_refuses_polished_action_when_lineage_identity_differs(runtime):
    _, pack, _, _, _, executor, _, _ = runtime
    envelope = pack.replay_input("P-EXPRESSION-POLISH")
    output = pack.replay_output("P-EXPRESSION-POLISH", "normal")
    output["result"]["source_preservation_summary"][0]["action"] = "POLISHED"
    output["result"]["source_preservation_summary"][0]["paragraph_id"] = "p-other"

    with pytest.raises(PromptExecutionError) as exc_info:
        executor._normalize_output(
            "P-EXPRESSION-POLISH",
            output,
            envelope,
        )

    assert any(
        error.startswith("/result/source_preservation_summary/0/paragraph_id:")
        and "p-other" in error
        for error in exc_info.value.validation_errors
    )


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
    assert normalized["source_refs"][0]["source_hash"] == "a" * 63
    errors = pack.validate("P-WRITE-CONTENT", "output", normalized)
    assert any(error.startswith("/source_refs/0/source_hash:") for error in errors)


def test_argument_repair_context_reconciles_explicit_rq_rc_source_mapping():
    repaired = {
        "argument_architecture": {
            "graph_id": "AG-001",
            "nodes": [
                {
                    "node_id": f"OBJ-{index:03d}",
                    "node_type": "OBJECTIVE",
                    "statement": f"Objective {index}",
                    "status": "PLANNED",
                    "source_refs": [],
                }
                for index in range(1, 5)
            ] + [{
                "node_id": "RC-001",
                "node_type": "WORK_PACKAGE",
                "statement": "Work package 1",
                "status": "PLANNED",
                "source_refs": [],
            }],
            "edges": [],
        },
        "research_design_matrix": [
            {
                "research_question_id": f"RQ-{index:03d}",
                "work_package_ids": ["RC-001"],
            }
            for index in range(1, 5)
        ],
    }
    sections = [{
        "section_id": "sec-closed-loop",
        "text": "\n".join(
            f"| `RQ-{index}` | `OBJ-{index}` | `RC-{index}` |"
            for index in range(1, 5)
        ) + "\n" + "\n".join(
            f"**`RC-{index}` Work package {index}**: definition {index}"
            for index in range(1, 5)
        ) + "\n- `BASE-2` Existing software: `UNKNOWN`",
    }]

    canonical = ContextBuilder._canonicalize_argument_result_from_sections(
        repaired,
        sections,
    )

    graph = canonical["argument_architecture"]
    node_ids = {node["node_id"] for node in graph["nodes"]}
    assert {"RC-001", "RC-002", "RC-003", "RC-004", "BASE-002"} <= node_ids
    assert [
        row["work_package_ids"]
        for row in canonical["research_design_matrix"]
    ] == [["RC-001"], ["RC-002"], ["RC-003"], ["RC-004"]]
    assert {
        (edge["source_id"], edge["target_id"])
        for edge in graph["edges"]
    } >= {
        ("OBJ-001", "RC-001"),
        ("OBJ-002", "RC-002"),
        ("OBJ-003", "RC-003"),
        ("OBJ-004", "RC-004"),
    }


def test_argument_context_binds_exact_approved_fact_evidence():
    argument_result = {
        "argument_architecture": {
            "nodes": [{
                "node_id": "claim-001",
                "node_type": "CLOSEST_PRIOR_WORK",
                "statement": "Prior work",
                "status": "UNKNOWN",
                "source_refs": [],
            }],
        },
    }
    source_ref = {
        "source_id": "public-source-001",
        "source_type": "PUBLIC_SOURCE",
        "authority_rank": 85,
        "security_level": "PUBLIC",
    }
    facts = [{
        "claim_id": "claim-001",
        "knowledge_status": "DOCUMENT_EXTRACTED",
        "source_refs": [source_ref],
    }]

    bound = ContextBuilder._bind_argument_result_evidence(argument_result, facts)

    node = bound["argument_architecture"]["nodes"][0]
    assert node["status"] == "SUPPORTED"
    assert node["source_refs"] == [source_ref]


def test_revision_plan_normalizer_preserves_model_authored_evidence_refs(runtime):
    _, pack, _, _, _, executor, _, _ = runtime
    output = pack.replay_output("P-REVISION-PLAN", "normal")
    output["findings"] = [{
        "code": "SECTION_CONTRACT_GENERIC",
        "severity": "P2",
        "category": "PLAN",
        "target_type": "PROPOSAL_CONTRACT",
        "target_path_or_span": "proposal_contract.mandatory_sections",
        "description": "A required section needs revision.",
        "evidence_refs": [
            "proposal_contract.mandatory_sections",
            "section-001",
        ],
        "repairable": True,
        "repair_instruction": "Revise the section plan.",
        "suggested_route": "PLANNING_AGENT",
        "blocking": False,
    }]

    normalized = executor._normalize_output("P-REVISION-PLAN", output)

    assert normalized["findings"][0]["evidence_refs"] == [
        "proposal_contract.mandatory_sections",
        "section-001",
    ]
    assert pack.validate("P-REVISION-PLAN", "output", normalized) == []


def test_content_normalizer_preserves_explicit_nonblocking_test_deferrals(runtime):
    _, pack, _, _, _, executor, _, _ = runtime
    output = pack.replay_output("P-WRITE-CONTENT", "normal")
    output["status"] = "REVISE"
    output["result"]["candidate_text"] += (
        "\n[测试数据，待替换] [测试占位符，正式申报前替换]"
    )
    output["findings"] = [
        {
            "code": "CONTENT_UNSUPPORTED_CLAIM",
            "severity": "P2",
            "category": "CONTENT",
            "target_type": "PARAGRAPH",
            "target_path_or_span": "/result/paragraphs/0/text",
            "description": "测试占位符已正确标注，但正式申报前仍需替换。",
            "evidence_refs": ["F-077"],
            "repairable": True,
            "repair_instruction": "正式申报前替换为真实材料。",
            "suggested_route": "ORIGINAL_PRODUCER",
            "blocking": False,
        },
        {
            "code": "TRACE_SOURCE_UNKNOWN",
            "severity": "P3",
            "category": "CONTENT",
            "target_type": "PARAGRAPH",
            "target_path_or_span": "/result/paragraphs/0/text",
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
            "target_paths": ["/result/paragraphs/0/text"],
            "required_action": "替换为真实材料。",
            "blocking": False,
        }
    ]

    normalized = executor._normalize_output("P-WRITE-CONTENT", output)

    assert normalized["status"] == "REVISE"
    assert normalized["findings"] == output["findings"]
    assert normalized["unresolved_items"] == output["unresolved_items"]
    assert pack.validate("P-WRITE-CONTENT", "output", normalized) == []


def test_blueprint_normalizer_rejects_unbound_business_labels_without_rewriting(runtime):
    _, pack, _, _, _, executor, _, _ = runtime
    output = pack.replay_output("P-WRITE-BLUEPRINT", "normal")
    original = copy.deepcopy(output)
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
    original = copy.deepcopy(output)

    with pytest.raises(PromptExecutionError) as exc_info:
        executor._normalize_output("P-WRITE-BLUEPRINT", output, envelope)

    errors = exc_info.value.validation_errors
    assert any("known metric label" in error for error in errors)
    assert any("首次可行方案形成速度" in error for error in errors)
    assert any("多链联合建模" in error for error in errors)
    assert output == original

def test_targeted_repair_normalizer_does_not_rewrite_business_budgets(runtime):
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
    envelope["payload"]["allowed_paths"] = [
        f"/content/paragraphs/{index}/word_budget"
        for index in range(len(paragraphs))
    ]
    envelope["payload"]["findings_to_repair"] = [{
        "finding_instance_id": "finding-word-budget-001",
        "code": "WORD_BUDGET_EXCEED",
        "description": "合同规定总字数为1000字，但当前总计1500字。",
    }]
    output["result"]["repaired_object"]["content"] = {
        "paragraphs": [dict(item) for item in paragraphs],
    }
    output["result"]["changed_paths"] = list(envelope["payload"]["allowed_paths"])
    output["result"]["resolved_finding_ids"] = ["finding-word-budget-001"]
    output["result"]["unresolved_finding_ids"] = []

    normalized = executor._normalize_output("P-TARGETED-REPAIR", output, envelope)
    repaired = normalized["result"]["repaired_object"]["content"]["paragraphs"]

    assert [item["word_budget"] for item in repaired] == [600, 500, 400]
    assert normalized["result"]["changed_paths"] == envelope["payload"]["allowed_paths"]
    assert not any("1000-word section limit" in item for item in normalized.get("warnings", []))


def test_targeted_repair_normalizer_preserves_model_scope_receipt_for_guard(runtime):
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
        "/content/paragraphs/1/text",
        "/content/paragraphs/2/text",
    ]
    envelope["payload"]["findings_to_repair"] = [{
        "finding_instance_id": "finding-quality-dimension-001",
        "code": "QUALITY_DIMENSION_FAILED",
        "target_path_or_span": "/content/paragraphs/1/text",
    }]
    repaired = copy.deepcopy(original)
    repaired["paragraphs"][0]["text"] = "unauthorized"
    repaired["paragraphs"][1]["text"] = "two-fixed"
    repaired["paragraphs"][2]["text"] = "three-fixed"
    output["status"] = "REVISE"
    output["result"]["repaired_object"]["content"] = repaired
    output["result"]["changed_paths"] = [
        "/content/paragraphs/0/text",
        "/content/paragraphs/1/text",
        "/content/paragraphs/2/text",
    ]
    output["result"]["resolved_finding_ids"] = [
        "finding-quality-dimension-001",
    ]
    output["result"]["unresolved_finding_ids"] = []
    output["findings"] = [{
        "code": "REPAIR_COMPLETED",
        "blocking": False,
    }]

    normalized = executor._normalize_output("P-TARGETED-REPAIR", output, envelope)

    assert normalized["result"]["repaired_object"]["content"] == repaired
    assert normalized["result"]["resolved_finding_ids"] == [
        "finding-quality-dimension-001",
    ]
    assert normalized["result"]["changed_paths"] == output["result"]["changed_paths"]
    assert normalized["findings"] == output["findings"]
    assert normalized["status"] == "REVISE"


def test_normalizer_does_not_infer_critic_business_aliases(runtime):
    _, pack, _, _, _, executor, _, _ = runtime
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

    assert normalized["findings"][0] == output["findings"][0]
    assert "description" not in normalized["findings"][0]
    assert normalized["user_questions"][0]["answer_schema"] == output["user_questions"][0]["answer_schema"]
    assert normalized["unresolved_items"][0]["type"] == "CHOICE"
    assert "missing_item_types" not in normalized["result"]["domain_scores"][0]
    assert [
        item["dimension"]
        for item in normalized["result"]["critical_readiness_checks"]
    ] == ["TEAM_AND_IMPLEMENTATION", "RESOURCES_BUDGET_RISK_COMPLIANCE"]
    assert pack.validate("P-PROJECT-READINESS-CRITIC", "output", normalized)

def test_normalizer_does_not_invent_quality_dimension_required_actions(runtime):
    _, pack, _, _, _, executor, _, _ = runtime
    output = pack.replay_output("P-WRITE-CRITIC", "normal")
    dimensions = output["result"]["quality_dimensions"]
    dimensions[0].pop("required_action")
    dimensions[1]["passed"] = False
    dimensions[1].pop("required_action")

    normalized = executor._normalize_output("P-WRITE-CRITIC", output)

    assert "required_action" not in normalized["result"]["quality_dimensions"][0]
    assert "required_action" not in normalized["result"]["quality_dimensions"][1]
    errors = pack.validate("P-WRITE-CRITIC", "output", normalized)
    assert any("required_action" in error for error in errors)

def test_write_critic_normalizer_preserves_deferred_test_decision(runtime):
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
            "code": "QUALITY_DIMENSION_FAILED",
            "severity": "P3",
            "category": "CONTENT",
            "target_type": "PARAGRAPH",
            "target_path_or_span": "/result/candidate_text",
            "description": "测试占位材料正式申报前替换。",
            "evidence_refs": ["gap-001"],
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
            "target_paths": ["/result/candidate_text"],
            "required_action": "替换测试材料。",
            "blocking": False,
        }
    ]
    output["result"]["unsupported_trace_ids"] = []
    output["result"]["blueprint_deviation_paragraph_ids"] = []
    output["result"]["scope_violations"] = []

    normalized = executor._normalize_output("P-WRITE-CRITIC", output, envelope)

    assert normalized["result"]["quality_dimensions"][0]["passed"] is False
    assert normalized["status"] == "REVISE"
    assert normalized["result"]["verdict"] == "REVISE"
    assert normalized["findings"] == output["findings"]
    assert normalized["unresolved_items"] == output["unresolved_items"]
    assert pack.validate("P-WRITE-CRITIC", "output", normalized) == []

def test_write_critic_normalizer_preserves_model_blueprint_deviation(runtime):
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
            "target_path_or_span": "/payload/content_candidate/paragraphs/0/primary_claim_id",
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

    assert normalized["findings"] == output["findings"]
    assert normalized["result"]["blueprint_deviation_paragraph_ids"] == [paragraph_id]
    assert pack.validate("P-WRITE-CRITIC", "output", normalized) == []

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


def test_revision_plan_normalizer_does_not_invent_information_key_qualifiers(runtime):
    _, pack, _, _, _, executor, _, _ = runtime
    output = pack.replay_output("P-REVISION-PLAN", "normal")
    contract = output["result"]["revision_plan"]["narrative_architecture"]["section_contracts"][0]
    contract["unique_information_keys"] = ["短键"]

    normalized = executor._normalize_output("P-REVISION-PLAN", output)

    assert normalized["result"]["revision_plan"]["narrative_architecture"]["section_contracts"][0]["unique_information_keys"] == ["短键"]
    errors = pack.validate("P-REVISION-PLAN", "output", normalized)
    assert any("unique_information_keys" in error for error in errors)

def test_argument_normalizer_rejects_unresolved_prior_work_without_materializing_nodes(runtime):
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
        ]
    }
    output["result"]["research_design_matrix"][0]["closest_prior_work_ids"] = ["EA-009"]
    output["result"]["readiness"]["blocking_node_ids"] = ["TEST-TEAM-A成果"]
    original = copy.deepcopy(output)

    with pytest.raises(PromptExecutionError) as exc_info:
        executor._normalize_output("P-ARGUMENT-ARCHITECTURE", output, envelope)

    errors = exc_info.value.validation_errors
    assert any("EA-009" in error for error in errors)
    assert any("TEST-TEAM-A成果" in error for error in errors)
    assert output == original

def test_argument_normalizer_rejects_status_sentinels_as_entity_references(runtime):
    _, pack, _, _, _, executor, *_ = runtime
    output = pack.replay_output("P-ARGUMENT-ARCHITECTURE", "normal")
    envelope = pack.replay_input("P-ARGUMENT-ARCHITECTURE")
    for row in output["result"]["research_design_matrix"]:
        row["method_ids"] = ["TO_BE_SELECTED"]
        row["closest_prior_work_ids"] = ["UNKNOWN"]
    original = copy.deepcopy(output)

    with pytest.raises(PromptExecutionError) as exc_info:
        executor._normalize_output("P-ARGUMENT-ARCHITECTURE", output, envelope)

    errors = exc_info.value.validation_errors
    assert any("TO_BE_SELECTED" in error for error in errors)
    assert any("UNKNOWN" in error for error in errors)
    assert output == original

def test_argument_normalizer_rejects_dangling_design_candidates(runtime):
    _, pack, _, _, _, executor, *_ = runtime
    output = pack.replay_output("P-ARGUMENT-ARCHITECTURE", "normal")
    envelope = pack.replay_input("P-ARGUMENT-ARCHITECTURE")
    row = output["result"]["research_design_matrix"][0]
    row["method_ids"] = ["METHOD-PROPOSED"]
    row["evaluation_ids"] = ["EXP-PROPOSED"]
    original = copy.deepcopy(output)

    with pytest.raises(PromptExecutionError) as exc_info:
        executor._normalize_output("P-ARGUMENT-ARCHITECTURE", output, envelope)

    errors = exc_info.value.validation_errors
    assert any("METHOD-PROPOSED" in error for error in errors)
    assert any("EXP-PROPOSED" in error for error in errors)
    assert output == original

def test_argument_normalizer_does_not_restore_entities_from_free_text(runtime):
    _, pack, _, _, _, executor, *_ = runtime
    output = pack.replay_output("P-ARGUMENT-ARCHITECTURE", "normal")
    envelope = pack.replay_input("P-ARGUMENT-ARCHITECTURE")
    output["result"]["research_design_matrix"][0]["research_question_id"] = "rq-002"
    output["result"]["research_design_matrix"][0]["rq_ids"] = ["rq-002"]
    envelope["payload"]["current_sections"] = [{
        "section_id": "section-loop-test",
        "text": "`RC-2` dynamic teaming package\n`BASE-2` available software remains UNKNOWN",
    }]
    envelope["payload"]["human_resolutions"] = [{
        "resolution_id": "human-method-test",
        "gate_id": "gate-method-test",
        "answer": {"RC-2": "finite-state dynamic teaming test method"},
        "decided_by": "pytest",
        "decided_role": "PROJECT_OWNER",
    }]
    original_node_ids = {
        node["node_id"]
        for node in output["result"]["argument_architecture"]["nodes"]
    }

    normalized = executor._normalize_output("P-ARGUMENT-ARCHITECTURE", output, envelope)

    normalized_node_ids = {
        node["node_id"]
        for node in normalized["result"]["argument_architecture"]["nodes"]
    }
    assert normalized_node_ids == original_node_ids
    assert {"RC-002", "BASE-002", "method-RC-002"}.isdisjoint(normalized_node_ids)
    assert normalized["result"]["research_design_matrix"][0]["rq_ids"] == ["rq-002"]
    errors = pack.validate("P-ARGUMENT-ARCHITECTURE", "output", normalized)
    assert any("rq_ids" in error for error in errors)

def test_argument_critic_rejects_refs_outside_reviewed_entity_catalog(runtime):
    _, pack, _, _, _, executor, *_ = runtime
    output = pack.replay_output("P-ARGUMENT-ARCHITECTURE-CRITIC", "normal")
    envelope = pack.replay_input("P-ARGUMENT-ARCHITECTURE-CRITIC")
    output["result"]["checked_node_ids"].append("RC-NOT-VISIBLE")
    output["result"]["chain_checks"][0]["source_ids"].append("RC-NOT-VISIBLE")
    original = copy.deepcopy(output)

    with pytest.raises(PromptExecutionError) as exc_info:
        executor._normalize_output("P-ARGUMENT-ARCHITECTURE-CRITIC", output, envelope)

    assert any("RC-NOT-VISIBLE" in error for error in exc_info.value.validation_errors)
    assert output == original

def test_argument_critic_does_not_invent_action_for_failed_quality_dimension(runtime):
    _, pack, _, _, _, executor, *_ = runtime
    output = pack.replay_output("P-ARGUMENT-ARCHITECTURE-CRITIC", "normal")
    envelope = pack.replay_input("P-ARGUMENT-ARCHITECTURE-CRITIC")
    dimension = output["result"]["quality_dimensions"][0]
    dimension["passed"] = False
    dimension["score"] = 2
    dimension["required_action"] = None

    normalized = executor._normalize_output(
        "P-ARGUMENT-ARCHITECTURE-CRITIC",
        output,
        envelope,
    )

    assert normalized["result"]["quality_dimensions"][0]["required_action"] is None
    assert pack.validate("P-ARGUMENT-ARCHITECTURE-CRITIC", "output", normalized) == []

def test_argument_block_with_questions_preserves_model_status(runtime):
    _, pack, _, _, _, executor, *_ = runtime
    output = pack.replay_output("P-ARGUMENT-ARCHITECTURE", "normal")
    output["status"] = "BLOCK"
    output["user_questions"] = [{
        "question_id": "UQ-TEST",
        "question_type": "MISSING_INFORMATION",
        "question": "请补充测试占位输入。",
        "reason": "缺少继续运行所需的测试信息。",
        "target_paths": ["/payload/project_subgraph"],
        "answer_schema": {"type": "OBJECT", "allowed_values": []},
        "blocking": True,
        "priority": "P0",
    }]

    normalized = executor._normalize_output(
        "P-ARGUMENT-ARCHITECTURE",
        output,
        pack.replay_input("P-ARGUMENT-ARCHITECTURE"),
    )

    assert normalized["status"] == "BLOCK"
    expected_question = copy.deepcopy(output["user_questions"][0])
    expected_question["answer_schema"] = {"type": "STRING"}
    assert normalized["user_questions"] == [expected_question]

def test_project_definition_normalizer_only_applies_registered_enum_aliases(runtime):
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

    assert len(normalized["result"]["argument_graph_seed"]["research_questions"]) == 6
    assert normalized["result"]["project_definition"]["domain_readiness"][0]["missing_item_types"] == [
        "OBJECTIVE",
        "EXISTING_APPROACH",
    ]
    assert normalized["source_refs"][0]["section_id"] == "含非规范字符的章节"
    assert normalized["result"]["project_definition"]["items"][0]["item_hash"] == "model-placeholder"
    assert normalized["result"]["project_definition"]["package_hash"] == "model-placeholder"
    assert pack.validate("P-PROJECT-DEFINITION-EXTRACT", "output", normalized)

def test_fact_output_normalizer_preserves_unregistered_business_values(runtime):
    _, pack, _, _, _, executor, *_ = runtime
    output = pack.replay_output("P-FACT-EXTRACT", "normal")
    fact = output["result"]["fact_candidates"][0]
    fact["subject_id"] = "项目"
    fact["claim_type"] = "FACT"
    fact["knowledge_status"] = "UNKNOWN"
    fact["temporal_status"] = "UNKNOWN"

    normalized = executor._normalize_output("P-FACT-EXTRACT", output)
    normalized_fact = normalized["result"]["fact_candidates"][0]

    assert normalized_fact["subject_id"] == "项目"
    assert normalized_fact["claim_type"] == "FACT"
    assert normalized_fact["temporal_status"] == "UNKNOWN"
    assert normalized_fact["knowledge_status"] == "UNKNOWN"
    assert pack.validate("P-FACT-EXTRACT", "output", normalized)

def test_project_definition_normalizer_preserves_null_truncated_graph_for_schema_rejection(runtime):
    _, pack, _, _, _, executor, *_ = runtime
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
        "target_path_or_span": "/payload",
        "description": "Formal guide is unavailable.",
        "evidence_refs": [],
        "repairable": True,
        "repair_instruction": "Ask the project owner.",
        "suggested_route": "USER",
        "blocking": True,
    }]
    original = copy.deepcopy(output)

    normalized = executor._normalize_output(
        "P-PROJECT-DEFINITION-EXTRACT",
        output,
    )

    assert normalized == original
    assert normalized is not output
    errors = pack.validate("P-PROJECT-DEFINITION-EXTRACT", "output", normalized)
    assert any("/items/1" in error or "items" in error for error in errors)
    assert any("/relations/0" in error or "relations" in error for error in errors)


def test_unexpected_workflow_exception_is_recorded_as_recoverable_block(runtime, monkeypatch):
    _, _, _, _, _, _, engine, _ = runtime
    project_id = create_project(engine.db)
    workflow = engine.start(project_id, "WF-1_PROJECT_INTAKE")

    async def fail_unexpectedly(*_args, **_kwargs):
        raise AttributeError("unexpected provider shape")

    monkeypatch.setattr(engine.executor, "execute", fail_unexpectedly)
    result = asyncio.run(engine.advance(workflow["id"]))

    assert result["status"] == "BLOCKED_TECHNICAL"
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
    profiles["critic"]["desired_output_tokens"] += 1
    second = executor._call_key(**args)

    assert first != second



def test_runtime_call_key_changes_when_provider_model_or_capability_changes(runtime):
    _, pack, _, _, _, executor, _, _ = runtime
    args = {
        "prompt_id": "P-PROJECT-DEFINITION-CRITIC",
        "project_id": "project-test",
        "workflow_id": "workflow-test",
        "input_hash": "c" * 64,
        "requested_call_key": None,
    }
    model = next(
        item
        for item in pack.models["models"]
        if item["model_id"] == "offline-critic-primary"
    )

    model["provider_model_name"] = "MiniMax-M3"
    first = executor._call_key(**args)

    model["provider_model_name"] = "MiniMax-M2.7-highspeed"
    second = executor._call_key(**args)
    assert second != first

    model["provider_model_name"] = "MiniMax-M3"
    pack.models["provider_capabilities"]["MiniMax-M3"]["hard_max_output_tokens"] -= 1
    third = executor._call_key(**args)
    assert third != first



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
            recovery_run_id=failed_run_id,
        )
    )

    assert result["status"] in {"PASS", "REVISE", "NEED_USER_INPUT"}
    assert result["contract_recovered_from_run_id"] == failed_run_id
    assert result["output"]["result"]["domain_scores"][0]["missing_item_types"] == [
        "EXISTING_APPROACH"
    ]


def test_runtime_recovers_expression_polish_action_collision_without_model_call(runtime, monkeypatch):
    _, pack, db, _, _, executor, _, _ = runtime
    project_id = create_project(db)
    workflow_id = new_id("wf")
    prompt_id = "P-EXPRESSION-POLISH"
    envelope = pack.replay_input(prompt_id)
    envelope["scope"]["project_id"] = project_id
    provider_output = pack.replay_output(prompt_id)
    provider_output["result"]["source_preservation_summary"][0]["action"] = "POLISHED"

    model_envelope, _ = executor._prepare_model_envelope(prompt_id, envelope)
    model_envelope = attach_trusted_source_catalog(model_envelope)
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
            prompt_id,
            "ERROR",
            "offline-general-primary",
            "offline-primary",
            sha256_json(model_envelope),
            sha256_json(provider_output),
            json.dumps(model_envelope, ensure_ascii=False),
            json.dumps(provider_output, ensure_ascii=False),
            "Provider output failed strict schema validation | "
            "/result/source_preservation_summary/0/action: "
            "'POLISHED' is not one of ['PRESERVED', 'REPHRASED', 'REPLACED', 'REMOVED']",
            100,
            utc_now(),
        ),
    )
    db.audit(
        "MODEL_CALL_FAILED",
        project_id=project_id,
        object_id="call-expression-polish-old-normalizer",
        metadata={
            "run_id": failed_run_id,
            "prompt_id": prompt_id,
            "deterministic_recoverable": False,
            "model_request_spec_hash": executor._model_request_spec_hash(prompt_id),
            "output_normalizer_version": "2026-07-31.v43-colon-reference-path-aliases",
        },
    )

    async def model_must_not_be_called(*_args, **_kwargs):
        raise AssertionError("the prior expression-polish response must be normalized locally")

    monkeypatch.setattr(executor.gateway, "invoke", model_must_not_be_called)
    result = asyncio.run(
        executor.execute(
            prompt_id,
            envelope,
            project_id=project_id,
            workflow_id=workflow_id,
            recovery_run_id=failed_run_id,
        )
    )

    assert result["contract_recovered_from_run_id"] == failed_run_id
    assert result["output"]["result"]["source_preservation_summary"][0]["action"] == "REPHRASED"
    assert any(
        warning.startswith("SYSTEM_EXPRESSION_SOURCE_LINEAGE_ACTION_NORMALIZATION:")
        for warning in result["output"]["warnings"]
    )
    assert pack.validate(prompt_id, "output", result["output"]) == []


def test_runtime_does_not_recover_failed_output_from_another_call_checkpoint(
    runtime,
    monkeypatch,
):
    """A section-level failure cannot be replayed at another call checkpoint."""

    _, pack, db, _, _, executor, _, _ = runtime
    project_id = create_project(db)
    workflow_id = new_id("wf")
    prompt_id = "P-EXPRESSION-POLISH"
    envelope = pack.replay_input(prompt_id)
    envelope["scope"]["project_id"] = project_id
    provider_output = pack.replay_output(prompt_id)
    provider_output["result"]["source_preservation_summary"][0]["action"] = "POLISHED"
    model_envelope, _ = executor._prepare_model_envelope(prompt_id, envelope)
    model_envelope = attach_trusted_source_catalog(model_envelope)
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
            prompt_id,
            "ERROR",
            "offline-general-primary",
            "offline-primary",
            sha256_json(model_envelope),
            sha256_json(provider_output),
            json.dumps(model_envelope, ensure_ascii=False),
            json.dumps(provider_output, ensure_ascii=False),
            "Provider output failed strict schema validation | action drift",
            100,
            utc_now(),
        ),
    )
    db.audit(
        "MODEL_CALL_FAILED",
        project_id=project_id,
        object_id="call-section-a",
        metadata={
            "run_id": failed_run_id,
            "prompt_id": prompt_id,
            "deterministic_recoverable": True,
            "model_request_spec_hash": executor._model_request_spec_hash(prompt_id),
            "output_normalizer_version": "older-normalizer",
            "checkpoint_identity_version": 1,
            "checkpoint_call_key": "call-section-a",
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
            call_key="call-section-b",
        )
    )

    assert calls["count"] == 1
    assert result["contract_recovered_from_run_id"] is None


@pytest.mark.parametrize(
    ("prompt_id", "missing_fields"),
    [
        ("P-TARGETED-REPAIR", ("unresolved_finding_ids",)),
        (
            "P-WRITE-BLUEPRINT-CRITIC",
            (
                "uncovered_revision_task_ids",
                "invalid_slot_refs",
                "critical_unresolved_slot_ids",
            ),
        ),
    ],
)
def test_runtime_recovers_missing_deterministic_receipts_without_model_call(
    runtime,
    monkeypatch,
    prompt_id,
    missing_fields,
):
    _, pack, db, _, _, executor, _, _ = runtime
    project_id = create_project(db)
    workflow_id = new_id("wf")
    envelope = pack.replay_input(prompt_id)
    envelope["scope"]["project_id"] = project_id
    provider_output = pack.replay_output(prompt_id)
    for field in missing_fields:
        provider_output["result"].pop(field)

    model_envelope, _ = executor._prepare_model_envelope(prompt_id, envelope)
    model_envelope = attach_trusted_source_catalog(model_envelope)
    failed_run_id = new_id("run")
    missing_message = ", ".join(missing_fields)
    db.execute(
        """INSERT INTO prompt_runs(
               id,project_id,workflow_id,prompt_id,status,model_id,endpoint_id,
               input_hash,output_hash,input_json,output_json,error,duration_ms,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            failed_run_id,
            project_id,
            workflow_id,
            prompt_id,
            "ERROR",
            "offline-general-primary",
            "offline-primary",
            sha256_json(model_envelope),
            sha256_json(provider_output),
            json.dumps(model_envelope, ensure_ascii=False),
            json.dumps(provider_output, ensure_ascii=False),
            f"Provider output failed strict schema validation | missing {missing_message}",
            100,
            utc_now(),
        ),
    )
    db.audit(
        "MODEL_CALL_FAILED",
        project_id=project_id,
        object_id=f"call-old-{prompt_id}",
        metadata={
            "run_id": failed_run_id,
            "prompt_id": prompt_id,
            "deterministic_recoverable": False,
            "model_request_spec_hash": executor._model_request_spec_hash(prompt_id),
            "output_normalizer_version": "2026-08-03.v44-expression-source-lineage-action",
        },
    )

    async def model_must_not_be_called(*_args, **_kwargs):
        raise AssertionError("the persisted provider response must be normalized locally")

    monkeypatch.setattr(executor.gateway, "invoke", model_must_not_be_called)
    result = asyncio.run(
        executor.execute(
            prompt_id,
            envelope,
            project_id=project_id,
            workflow_id=workflow_id,
            original_environment=(
                "OFFLINE_LOCAL" if prompt_id == "P-TARGETED-REPAIR" else None
            ),
            recovery_run_id=failed_run_id,
        )
    )

    assert result["contract_recovered_from_run_id"] == failed_run_id
    for field in missing_fields:
        assert result["output"]["result"][field] == []
    assert any(
        warning.startswith("SYSTEM_PROTOCOL_RECEIPT_COMPLETION:")
        for warning in result["output"].get("warnings", [])
    )
    assert pack.validate(prompt_id, "output", result["output"]) == []


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
            recovery_run_id=failed_run_id,
        )
    )

    assert result["contract_recovered_from_run_id"] == failed_run_id
    assert result["output"]["result"]["source_fact_exclusions"] == exclusions
    assert "source_fact_exclusions" not in result["output"]["result"]["template"]
    assert pack.validate("P-TEMPLATE-EXTRACT", "output", result["output"]) == []


def test_runtime_recovers_safe_package_scalar_source_ref_drift_without_model_call(
    runtime,
    monkeypatch,
):
    _, pack, db, _, _, executor, _, _ = runtime
    project_id = create_project(db)
    workflow_id = new_id("wf")
    prompt_id = "P-SAFE-ONLINE-PACKAGE"
    envelope = pack.replay_input(prompt_id)
    envelope["scope"]["project_id"] = project_id
    provider_output = pack.replay_output(prompt_id)
    provider_output["source_refs"] = [
        {
            "source_id": envelope["payload"]["research_need"]["need_id"],
            "source_type": "MODEL_INFERENCE",
            "document_version_id": 1,
            "section_id": None,
            "span_start": None,
            "span_end": None,
            "quoted_text": None,
            "source_hash": None,
            "authority_rank": 60,
            "security_level": "INTERNAL",
        },
        {
            "source_id": envelope["payload"]["security_policy"]["profile_id"],
            "source_type": "MODEL_INFERENCE",
            "document_version_id": 2,
            "section_id": None,
            "span_start": None,
            "span_end": None,
            "quoted_text": None,
            "source_hash": envelope["payload"]["security_policy"]["profile_hash"],
            "authority_rank": 100,
            "security_level": "INTERNAL",
        },
    ]
    model_envelope, _ = executor._prepare_model_envelope(prompt_id, envelope)
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
            prompt_id,
            "ERROR",
            "offline-general-primary",
            "offline-primary",
            input_hash,
            sha256_json(provider_output),
            json.dumps(model_envelope, ensure_ascii=False),
            json.dumps(provider_output, ensure_ascii=False),
            "Output container structure validation failed | "
            "/source_refs/0/document_version_id: 1 is not valid under any schema; "
            "/source_refs/1/document_version_id: 2 is not valid under any schema",
            30000,
            utc_now(),
        ),
    )

    async def model_must_not_be_called(*_args, **_kwargs):
        raise AssertionError("trusted source reference drift must be repaired locally")

    monkeypatch.setattr(executor.gateway, "invoke", model_must_not_be_called)
    result = asyncio.run(
        executor.execute(
            prompt_id,
            envelope,
            project_id=project_id,
            workflow_id=workflow_id,
            recovery_run_id=failed_run_id,
        )
    )

    assert result["contract_recovered_from_run_id"] == failed_run_id
    assert [
        ref["document_version_id"] for ref in result["output"]["source_refs"]
    ] == [None, None]
    assert pack.validate(prompt_id, "output", result["output"]) == []


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


def test_fact_output_rejects_coverage_refs_to_unknown_facts(runtime):
    _, pack, _, _, _, executor, *_ = runtime
    output = pack.replay_output("P-FACT-EXTRACT", "normal")
    envelope = pack.replay_input("P-FACT-EXTRACT")
    output["result"]["coverage"][0]["claim_ids"].append("existing-fact-not-emitted")
    original = copy.deepcopy(output)

    with pytest.raises(PromptExecutionError) as exc_info:
        executor._normalize_output("P-FACT-EXTRACT", output, envelope)

    assert any("existing-fact-not-emitted" in error for error in exc_info.value.validation_errors)
    assert output == original

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
            "PASS",
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


def test_legacy_generic_contract_block_does_not_retry_uncommitted_step(runtime):
    settings, _, db, _, _, _, engine, _ = runtime
    project_id = create_project(db)
    add_standard_materials(settings, db, project_id)
    workflow = engine.start(project_id, "WF-1_PROJECT_INTAKE")
    state = workflow["state"]
    state["last_error"] = "Output schema validation failed"
    engine._update(workflow, status="BLOCKED", state=state)

    resumed = asyncio.run(engine.advance(workflow["id"]))

    assert resumed["status"] == "BLOCKED_CONTRACT"
    assert resumed["current_step"] == 0
    assert not resumed["state"].get("technical_retry_attempts")


def test_acceptance_run_does_not_expand_unclassified_legacy_retry_budget(runtime):
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

    assert resumed["status"] == "BLOCKED_TECHNICAL"
    assert resumed["state"]["technical_retry_attempts"]["0"] == 4


def test_deterministic_quality_block_is_classified_before_recheck(runtime):
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

    assert resumed["status"] == "BLOCKED_CONTENT", json.dumps(resumed, ensure_ascii=False)
    assert not resumed["state"].get("technical_retry_attempts")
    assert resumed["state"]["step_results"]["0"]["run_id"] == "run-superseded"


def test_legacy_contract_block_is_classified_before_any_technical_retry(runtime):
    _settings, _pack, db, _router, _builder, _executor, engine, _exporter = runtime
    project_id = create_project(db)
    workflow = engine.start(project_id, "WF-1_PROJECT_INTAKE")
    state = workflow["state"]
    state["last_error"] = "Output schema validation failed"
    engine._update(workflow, status="BLOCKED", state=state)

    migrated = asyncio.run(engine.advance(workflow["id"]))

    assert migrated["status"] == "BLOCKED_CONTRACT"
    assert not migrated["state"].get("technical_retry_attempts")
    assert migrated["state"]["legacy_blocked_status_migration"]["to"] == "BLOCKED_CONTRACT"


def test_legacy_provider_block_is_classified_without_calling_provider(runtime, monkeypatch):
    _settings, _pack, db, _router, _builder, executor, engine, _exporter = runtime
    project_id = create_project(db)
    workflow = engine.start(project_id, "WF-1_PROJECT_INTAKE")
    state = workflow["state"]
    state["last_error"] = "LLM stream completed without message content"
    engine._update(workflow, status="BLOCKED", state=state)
    calls = {"count": 0}

    async def forbidden(*_args, **_kwargs):
        calls["count"] += 1
        raise AssertionError("legacy classification must not call the provider")

    monkeypatch.setattr(executor, "execute", forbidden)
    migrated = asyncio.run(engine.advance(workflow["id"]))

    assert migrated["status"] == "WAITING_PROVIDER"
    assert calls["count"] == 0
    assert not migrated["state"].get("technical_retry_attempts")


def test_legacy_revise_block_is_classified_as_content(runtime):
    _settings, _pack, db, _router, _builder, _executor, engine, _exporter = runtime
    project_id = create_project(db)
    workflow = engine.start(project_id, "WF-1_PROJECT_INTAKE")
    state = workflow["state"]
    state["step_results"]["0"] = {
        "prompt_id": "P-SECURITY-CLASSIFY",
        "run_id": "run-revise",
        "status": "REVISE",
    }
    state["technical_retry_attempts"] = {"0": 1}
    engine._update(workflow, status="BLOCKED", state=state)

    migrated = asyncio.run(engine.advance(workflow["id"]))

    assert migrated["status"] == "BLOCKED_CONTENT"
    assert migrated["state"]["technical_retry_attempts"]["0"] == 1
    assert migrated["state"]["legacy_blocked_status_migration"]["result_status"] == "REVISE"


@pytest.mark.parametrize(
    "persisted_prompt_id",
    ["P-SECURITY-CLASSIFY", "P-REMOVED-LEGACY"],
)
def test_legacy_human_input_block_recreates_exact_gate(
    runtime,
    persisted_prompt_id,
):
    _settings, pack, db, _router, _builder, _executor, engine, _exporter = runtime
    project_id = create_project(db)
    workflow = engine.start(project_id, "WF-1_PROJECT_INTAKE")
    run_id = "run-human-input"
    output = pack.replay_output("P-SECURITY-CLASSIFY")
    output["status"] = "NEED_USER_INPUT"
    output["user_questions"] = [
        {
            "question_id": "q-1",
            "question": "请补充材料密级。",
            "target_paths": ["payload.existing_labels"],
            "answer_schema": {"type": "STRING"},
            "blocking": True,
        }
    ]
    db.execute(
        """INSERT INTO prompt_runs(
               id,project_id,workflow_id,prompt_id,status,model_id,endpoint_id,
               input_hash,output_hash,input_json,output_json,error,duration_ms,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            run_id, project_id, workflow["id"], persisted_prompt_id,
            "PASS", "legacy-model", "legacy-endpoint", "input-hash",
            sha256_json(output), "{}", json.dumps(output, ensure_ascii=False),
            None, 1, utc_now(),
        ),
    )
    state = workflow["state"]
    state["step_results"]["0"] = {
        "prompt_id": persisted_prompt_id,
        "run_id": run_id,
        "status": "NEED_USER_INPUT",
    }
    engine._update(workflow, status="BLOCKED", state=state)

    migrated = asyncio.run(engine.advance(workflow["id"]))
    gates = [
        gate for gate in engine.list_gates(workflow_id=workflow["id"])
        if gate["status"] == "OPEN"
    ]

    assert migrated["status"] == "WAITING_GATE"
    assert len(gates) == 1
    assert gates[0]["target_id"] == run_id
    assert gates[0]["questions"][0]["question_id"] == "q-1"
    if persisted_prompt_id == "P-REMOVED-LEGACY":
        assert gates[0]["gate_type"] == "PROJECT_GAP_RESOLUTION"


def test_waiting_gate_without_open_gate_fails_closed(runtime):
    _settings, _pack, db, _router, _builder, _executor, engine, _exporter = runtime
    project_id = create_project(db)
    workflow = engine.start(project_id, "WF-1_PROJECT_INTAKE")
    engine._update(workflow, status="WAITING_GATE", state=workflow["state"])

    blocked = asyncio.run(engine.advance(workflow["id"]))

    assert blocked["status"] == "BLOCKED_TECHNICAL"
    assert blocked["state"]["gate_reconciliation_failure"]["code"] == "WAITING_GATE_WITHOUT_OPEN_GATE"
    assert db.fetchone(
        "SELECT COUNT(*) AS n FROM audit_events WHERE object_id=? AND event_type='WORKFLOW_GATE_RECONCILIATION_FAILED'",
        (workflow["id"],),
    )["n"] == 1


def test_terminal_transition_clears_runtime_resume_flags(runtime):
    _settings, _pack, _db, _router, _builder, _executor, engine, _exporter = runtime
    project_id = create_project(_db)
    workflow = engine.start(project_id, "WF-1_PROJECT_INTAKE")
    state = workflow["state"]
    state.update({
        "runtime_recoverable": True,
        "runtime_failure_point": "after_db_transaction",
        "runtime_blocked_at": utc_now(),
    })

    engine._update(workflow, status="COMPLETED", state=state)
    completed = engine.get(workflow["id"])

    assert completed["status"] == "COMPLETED"
    assert "runtime_recoverable" not in completed["state"]
    assert "runtime_failure_point" not in completed["state"]
    assert "runtime_blocked_at" not in completed["state"]


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


def test_targeted_repair_normalizer_does_not_rewrite_fact_collection(runtime):
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
        "/content/fact_candidates/0/claim_type",
    ]
    envelope["payload"]["findings_to_repair"] = [{
        "finding_instance_id": "finding-fact-status-001",
        "code": "FACT_CRITIC_STATUS_UPGRADE",
        "severity": "P1",
        "category": "FACT",
        "target_type": "FACT_CANDIDATE",
        "target_path_or_span": "/content/fact_candidates/0/claim_type",
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
    output["result"]["changed_paths"] = [
        "/content/fact_candidates/0/claim_type",
        "/content/fact_candidates/0/claim_text",
        "/content/fact_candidates/1/claim_type",
        "/content/fact_candidates/2",
    ]
    output["result"]["resolved_finding_ids"] = ["finding-fact-status-001"]
    output["result"]["unresolved_finding_ids"] = []
    output["findings"] = []

    normalized = executor._normalize_output("P-TARGETED-REPAIR", output, envelope)

    assert normalized["result"]["repaired_object"]["content"] == repaired
    assert normalized["result"]["changed_paths"] == output["result"]["changed_paths"]
    assert normalized["status"] == "REVISE"
    assert not any(
        "outside the critic-authorized path scope" in warning
        for warning in normalized.get("warnings", [])
    )


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
    failed_run_id = new_id("run")
    db.execute(
        """INSERT INTO prompt_runs(
               id,project_id,workflow_id,prompt_id,status,model_id,endpoint_id,
               input_hash,output_hash,input_json,output_json,error,duration_ms,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            failed_run_id,
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
    db.audit(
        "MODEL_CALL_FAILED",
        project_id=project_id,
        object_id="call-old-contract",
        metadata={
            "run_id": failed_run_id,
            "output_normalizer_version": "normalizer-v1",
        },
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
            output["user_questions"] = copy.deepcopy(
                pack.replay_output(
                    "P-TEMPLATE-CRITIC", "need_user_input"
                )["user_questions"]
            )
        guard_enabled = bool(executor.quality_guard_enabled)
        guard_report = (
            build_guard_report(prompt_id, output, [])
            if guard_enabled
            else disabled_guard_report(prompt_id, output)
        )
        return {
            "run_id": new_id("run"),
            "status": output["status"],
            "route": {
                "environment": "OFFLINE_LOCAL",
                "model_id": "test-model",
                "endpoint_id": "test-endpoint",
            },
            "output": output,
            "guard_report": guard_report,
            "quality_guard_enabled": guard_enabled,
            "guard_observation_status": guard_report["observation_status"],
        }

    monkeypatch.setattr(builder, "build", build)
    monkeypatch.setattr(executor, "execute", execute)

    advanced = asyncio.run(engine.advance(workflow["id"]))
    assert advanced["status"] == "BLOCKED_CONTRACT"
    assert calls == []
    advanced = asyncio.run(engine.advance(workflow["id"]))

    assert calls[:2] == ["P-TEMPLATE-EXTRACT", "P-TEMPLATE-CRITIC"], advanced["state"].get("last_error")
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
    assert message.startswith("Provider output failed strict schema validation")
    assert "ERROR_EVIDENCE_PERSISTENCE_FAILED" in message
    assert "simulated disk full" in message


def test_runtime_recovers_safe_package_source_prefix_alias_without_model_call(
    runtime,
    monkeypatch,
):
    _, pack, db, _, _, executor, _, _ = runtime
    project_id = create_project(db)
    workflow_id = new_id("wf")
    prompt_id = "P-SAFE-ONLINE-PACKAGE"
    envelope = pack.replay_input(prompt_id)
    envelope["scope"]["project_id"] = project_id
    need = envelope["payload"]["research_need"]
    envelope["payload"]["human_resolutions"] = [{
        "resolution_id": "human-wf3-recovery-001",
        "gate_id": "gate-wf3-recovery-001",
        "prompt_id": prompt_id,
        "question_id": "wf3-research-question",
        "question": "需要联网检索并核验的公开问题是什么？",
        "target_paths": ["/payload/research_need/question"],
        "answer": need["question"],
        "decided_by": "pytest",
        "decided_role": "PROJECT_OWNER",
    }]
    provider_output = pack.replay_output(prompt_id)
    provider_output["source_refs"] = [{
        "source_id": f"source-{need['need_id']}",
        "source_type": "MODEL_INFERENCE",
        "document_version_id": None,
        "section_id": None,
        "span_start": None,
        "span_end": None,
        "quoted_text": None,
        "source_hash": None,
        "authority_rank": 60,
        "security_level": "INTERNAL",
    }]
    model_envelope, _ = executor._prepare_model_envelope(prompt_id, envelope)
    prior_model_envelope = copy.deepcopy(model_envelope)
    prior_model_envelope["payload"].pop("human_resolutions", None)
    input_hash = sha256_json(prior_model_envelope)
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
            prompt_id,
            "ERROR",
            "offline-general-primary",
            "offline-primary",
            input_hash,
            sha256_json(provider_output),
            json.dumps(prior_model_envelope, ensure_ascii=False),
            json.dumps(provider_output, ensure_ascii=False),
            "Output provenance is not backed by the trusted input envelope | "
            f"/source_refs/0/source_id: 'source-{need['need_id']}' is not present in the trusted input envelope",
            33000,
            utc_now(),
        ),
    )
    # Simulate the exact migration scenario: the older runtime persisted a
    # negative recovery classification because source aliases and gate-backed
    # USER_CONFIRMATION provenance were not supported yet.  The current
    # normalizer must be allowed to re-evaluate the immutable provider output.
    db.audit(
        "MODEL_CALL_FAILED",
        project_id=project_id,
        object_id="call-old-source-alias",
        metadata={
            "run_id": failed_run_id,
            "prompt_id": prompt_id,
            "deterministic_recoverable": False,
            "model_request_spec_hash": executor._model_request_spec_hash(prompt_id),
            "output_normalizer_version": "2026-07-29.v8-global-provenance-integrity",
        },
    )

    async def model_must_not_be_called(*_args, **_kwargs):
        raise AssertionError("source prefix alias drift must be rebound from the prior provider output")

    monkeypatch.setattr(executor.gateway, "invoke", model_must_not_be_called)
    result = asyncio.run(
        executor.execute(
            prompt_id,
            envelope,
            project_id=project_id,
            workflow_id=workflow_id,
            recovery_run_id=failed_run_id,
        )
    )

    assert result["contract_recovered_from_run_id"] == failed_run_id
    source_ref = result["output"]["source_refs"][0]
    assert source_ref["source_id"] == need["need_id"]
    assert source_ref["source_type"] == "USER_CONFIRMATION"
    assert source_ref["authority_rank"] == 100
    assert pack.validate(prompt_id, "output", result["output"]) == []


def test_runtime_recovers_safe_package_critic_object_path_sources_without_model_call(
    runtime,
    monkeypatch,
):
    """A legacy critic response may cite input field names instead of stable IDs.

    Adding the trusted-source catalog is a deterministic request-contract
    upgrade, so the immutable prior provider output must be revalidated locally
    rather than sent to the model again.
    """
    _, pack, db, _, _, executor, _, _ = runtime
    project_id = create_project(db)
    workflow_id = new_id("wf")
    prompt_id = "P-SAFE-ONLINE-PACKAGE-CRITIC"
    envelope = pack.replay_input(prompt_id)
    envelope["scope"]["project_id"] = project_id

    provider_output = pack.replay_output(prompt_id)
    provider_output["source_refs"] = [
        {"source_id": "deterministic_scan"},
        {"source_id": "security_policy"},
    ]

    # This is the exact model envelope shape stored by the v9 runtime: the
    # catalog had not yet been added to the model request.
    prior_model_envelope, _ = executor._prepare_model_envelope(prompt_id, envelope)
    assert "trusted_source_catalog" not in prior_model_envelope
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
            prompt_id,
            "ERROR",
            "offline-general-primary",
            "offline-primary",
            sha256_json(prior_model_envelope),
            sha256_json(provider_output),
            json.dumps(prior_model_envelope, ensure_ascii=False),
            json.dumps(provider_output, ensure_ascii=False),
            "Output provenance is not backed by the trusted input envelope | "
            "/source_refs/0/source_id: 'deterministic_scan' is not present in the trusted input envelope | "
            "/source_refs/1/source_id: 'security_policy' is not present in the trusted input envelope",
            33000,
            utc_now(),
        ),
    )
    db.audit(
        "MODEL_CALL_FAILED",
        project_id=project_id,
        object_id="call-old-input-object-identities",
        metadata={
            "run_id": failed_run_id,
            "prompt_id": prompt_id,
            "deterministic_recoverable": False,
            "model_request_spec_hash": "legacy-v9-request-contract",
            "output_normalizer_version": "2026-07-30.v9-source-alias-user-confirmation",
        },
    )

    async def model_must_not_be_called(*_args, **_kwargs):
        raise AssertionError("trusted input object identities must recover the prior provider output locally")

    monkeypatch.setattr(executor.gateway, "invoke", model_must_not_be_called)
    result = asyncio.run(
        executor.execute(
            prompt_id,
            envelope,
            project_id=project_id,
            workflow_id=workflow_id,
            recovery_run_id=failed_run_id,
        )
    )

    assert result["contract_recovered_from_run_id"] == failed_run_id
    refs = {item["source_id"]: item for item in result["output"]["source_refs"]}
    assert "security-001" in refs
    scan_ids = [source_id for source_id in refs if source_id.startswith(
        "input-p-safe-online-package-critic-deterministic-scan-"
    )]
    assert len(scan_ids) == 1
    assert refs[scan_ids[0]]["source_type"] == "EVIDENCE_MATERIAL"
    assert refs["security-001"]["source_type"] == "CONTRACT"
    assert pack.validate(prompt_id, "output", result["output"]) == []


def test_context_replacement_validates_target_field_without_full_envelope(runtime, monkeypatch):
    _, pack, _, _, builder, _, _, _ = runtime
    prompt_id = "P-WRITE-BLUEPRINT"
    envelope = pack.replay_input(prompt_id)
    original_project_id = envelope["scope"]["project_id"]

    def forbidden_full_validation(*_args, **_kwargs):
        raise AssertionError("per-replacement full-envelope validation is forbidden")

    monkeypatch.setattr(pack, "validate", forbidden_full_validation)

    assert builder._set_path_if_valid(
        prompt_id,
        envelope,
        "scope.project_id",
        original_project_id,
    )
    assert not builder._set_path_if_valid(
        prompt_id,
        envelope,
        "scope.project_id",
        {"not": "an identifier"},
    )
    assert envelope["scope"]["project_id"] == original_project_id


def test_targeted_repair_normalizer_derives_missing_unresolved_receipt(runtime):
    _, pack, _, _, _, executor, _, _ = runtime
    envelope = pack.replay_input("P-TARGETED-REPAIR")
    output = pack.replay_output("P-TARGETED-REPAIR", "normal")
    envelope["payload"]["findings_to_repair"].append({
        "finding_instance_id": "finding-replay-002",
        "code": "WRITE_SCOPE_DRIFT",
        "severity": "P1",
        "category": "CONTENT",
        "target_type": "WRITING_CANDIDATE",
        "target_path_or_span": "/content/text",
        "description": "A second requested repair remains unresolved.",
        "evidence_refs": [],
        "repairable": True,
        "repair_instruction": "Keep this finding unresolved in this attempt.",
        "suggested_route": "ORIGINAL_PRODUCER",
        "blocking": True,
    })
    output["result"].pop("unresolved_finding_ids")

    normalized = executor._normalize_output("P-TARGETED-REPAIR", output, envelope)

    assert normalized["result"]["unresolved_finding_ids"] == ["finding-replay-002"]
    assert any(
        warning.startswith("SYSTEM_PROTOCOL_RECEIPT_COMPLETION:")
        for warning in normalized.get("warnings", [])
    )
    assert pack.validate("P-TARGETED-REPAIR", "output", normalized) == []


def test_targeted_repair_normalizer_does_not_overwrite_present_invalid_receipt(runtime):
    _, pack, _, _, _, executor, _, _ = runtime
    envelope = pack.replay_input("P-TARGETED-REPAIR")
    output = pack.replay_output("P-TARGETED-REPAIR", "normal")
    output["result"]["unresolved_finding_ids"] = None

    with pytest.raises(PromptExecutionError, match="Required output container is null"):
        executor._normalize_output("P-TARGETED-REPAIR", output, envelope)


def test_blueprint_critic_normalizer_completes_guard_owned_empty_receipts(runtime):
    _, pack, _, _, _, executor, _, _ = runtime
    output = pack.replay_output("P-WRITE-BLUEPRINT-CRITIC", "normal")
    fields = (
        "uncovered_revision_task_ids",
        "invalid_slot_refs",
        "critical_unresolved_slot_ids",
    )
    for field in fields:
        output["result"].pop(field)

    normalized = executor._normalize_output("P-WRITE-BLUEPRINT-CRITIC", output)

    assert {field: normalized["result"][field] for field in fields} == {
        field: [] for field in fields
    }
    assert pack.validate("P-WRITE-BLUEPRINT-CRITIC", "output", normalized) == []


def test_advance_restores_waiting_gate_when_gate_commit_preceded_status_commit(runtime):
    _, _, _, _, _, _, engine, _ = runtime
    project_id = create_project(engine.db)
    workflow = engine.start(project_id, "WF-1_PROJECT_INTAKE")
    gate_id = engine._create_gate(
        workflow,
        "PROJECT_GAP_RESOLUTION",
        target_id="run-crash-window",
        questions=[],
    )
    assert engine.get(workflow["id"])["status"] == "RUNNING"

    recovered = asyncio.run(engine.advance(workflow["id"]))

    assert recovered["status"] == "WAITING_GATE"
    assert engine._open_gate(workflow["id"])["id"] == gate_id


def test_runtime_does_not_downgrade_old_checkpoint_when_failure_audit_is_older_than_window(
    runtime,
    monkeypatch,
):
    _, pack, db, _, _, executor, _, _ = runtime
    project_id = create_project(db)
    workflow_id = new_id("wf")
    prompt_id = "P-EXPRESSION-POLISH"
    envelope = pack.replay_input(prompt_id)
    envelope["scope"]["project_id"] = project_id
    provider_output = pack.replay_output(prompt_id)
    provider_output["result"]["source_preservation_summary"][0]["action"] = "POLISHED"
    model_envelope, _ = executor._prepare_model_envelope(prompt_id, envelope)
    model_envelope = attach_trusted_source_catalog(model_envelope)
    failed_run_id = new_id("run")
    db.execute(
        """INSERT INTO prompt_runs(
               id,project_id,workflow_id,prompt_id,status,model_id,endpoint_id,
               input_hash,output_hash,input_json,output_json,error,duration_ms,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            failed_run_id, project_id, workflow_id, prompt_id, "ERROR",
            "offline-general-primary", "offline-primary",
            sha256_json(model_envelope), sha256_json(provider_output),
            json.dumps(model_envelope, ensure_ascii=False),
            json.dumps(provider_output, ensure_ascii=False),
            "Provider output failed strict schema validation | action drift",
            1, "2026-01-01T00:00:00+00:00",
        ),
    )
    db.audit(
        "MODEL_CALL_FAILED",
        project_id=project_id,
        object_id="call-section-a",
        metadata={
            "run_id": failed_run_id,
            "prompt_id": prompt_id,
            "deterministic_recoverable": True,
            "model_request_spec_hash": executor._model_request_spec_hash(prompt_id),
            "output_normalizer_version": "older-normalizer",
            "checkpoint_identity_version": 1,
            "checkpoint_call_key": "call-section-a",
        },
    )
    for index in range(205):
        db.audit(
            "MODEL_CALL_FAILED",
            project_id=project_id,
            object_id=f"call-unrelated-{index}",
            metadata={"run_id": f"run-unrelated-{index}"},
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
            call_key="call-section-b",
        )
    )

    assert calls["count"] == 1
    assert result["contract_recovered_from_run_id"] is None


def test_runtime_exact_recovery_run_is_not_hidden_by_fifty_newer_failures(
    runtime,
    monkeypatch,
):
    _, pack, db, _, _, executor, _, _ = runtime
    project_id = create_project(db)
    workflow_id = new_id("wf")
    prompt_id = "P-EXPRESSION-POLISH"
    envelope = pack.replay_input(prompt_id)
    envelope["scope"]["project_id"] = project_id
    provider_output = pack.replay_output(prompt_id)
    provider_output["result"]["source_preservation_summary"][0]["action"] = "POLISHED"
    model_envelope, _ = executor._prepare_model_envelope(prompt_id, envelope)
    model_envelope = attach_trusted_source_catalog(model_envelope)
    target_run_id = "run-exact-old-target"

    def insert_failure(run_id: str, created_at: str, call_key: str) -> None:
        db.execute(
            """INSERT INTO prompt_runs(
                   id,project_id,workflow_id,prompt_id,status,model_id,endpoint_id,
                   input_hash,output_hash,input_json,output_json,error,duration_ms,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id, project_id, workflow_id, prompt_id, "ERROR",
                "offline-general-primary", "offline-primary",
                sha256_json(model_envelope), sha256_json(provider_output),
                json.dumps(model_envelope, ensure_ascii=False),
                json.dumps(provider_output, ensure_ascii=False),
                "Provider output failed strict schema validation | action drift",
                1, created_at,
            ),
        )
        db.audit(
            "MODEL_CALL_FAILED",
            project_id=project_id,
            object_id=call_key,
            metadata={
                "run_id": run_id,
                "prompt_id": prompt_id,
                "deterministic_recoverable": True,
                "model_request_spec_hash": executor._model_request_spec_hash(prompt_id),
                "output_normalizer_version": "older-normalizer",
                "checkpoint_identity_version": 1,
                "checkpoint_call_key": call_key,
            },
        )

    insert_failure(target_run_id, "2026-01-01T00:00:00+00:00", "call-old-target")
    for index in range(60):
        insert_failure(
            f"run-newer-{index}",
            f"2026-02-{(index % 28) + 1:02d}T00:{index % 60:02d}:00+00:00",
            f"call-newer-{index}",
        )

    async def model_must_not_be_called(*_args, **_kwargs):
        raise AssertionError("the exact failed run must be revalidated locally")

    monkeypatch.setattr(executor.gateway, "invoke", model_must_not_be_called)
    result = asyncio.run(
        executor.execute(
            prompt_id,
            envelope,
            project_id=project_id,
            workflow_id=workflow_id,
            call_key="call-current-migration",
            recovery_run_id=target_run_id,
        )
    )

    assert result["contract_recovered_from_run_id"] == target_run_id
    assert result["output"]["result"]["source_preservation_summary"][0][
        "action"
    ] == "REPHRASED"

def test_semantic_producer_blocking_deficiency_regenerates_without_gate(
    runtime, monkeypatch
):
    settings, pack, db, _, _, _, engine, _ = runtime
    project_id = create_project(db, internet=False)
    add_standard_materials(settings, db, project_id)

    async def prepare_prerequisites():
        for workflow_type in [
            "WF-1_PROJECT_INTAKE",
            "WF-2_TEMPLATE_EXTRACTION",
        ]:
            workflow = await finish_workflow(
                engine, project_id, workflow_type
            )
            assert workflow["status"] == "COMPLETED"

    asyncio.run(prepare_prerequisites())

    workflow = engine.start(project_id, "WF-4_PROPOSAL_AUTHORING")
    workflow = engine.get(workflow["id"])
    argument_step = next(
        index
        for index, step in enumerate(workflow["steps"])
        if step.get("prompt_id") == "P-ARGUMENT-ARCHITECTURE"
    )
    state = workflow["state"]
    engine._update(
        workflow,
        status="RUNNING",
        current_step=argument_step,
        state=state,
    )
    workflow = engine.get(workflow["id"])
    state = workflow["state"]
    state.setdefault("step_results", {})[str(argument_step)] = {
        "prompt_id": "P-ARGUMENT-ARCHITECTURE",
        "run_id": "run-exact-stage0-baseline",
        "status": "REVISE",
    }
    engine._update(workflow, state=state)

    revise_output = {
        "result": {
            "evidence_gap_report": [
                {
                    "gap_id": "arg-evidence-gap-001",
                    "defect_key": "EVIDENCE_REQUIREMENT_UNSATISFIED:FOUNDATION_SUPPORT:0:arg-foundation-001",
                    "defect_family": "EVIDENCE_REQUIREMENT_UNSATISFIED",
                    "finding_code": "FOUNDATION_EVIDENCE_MISSING",
                    "semantic_component": "FOUNDATION",
                    "semantic_object_id": "arg-foundation-001",
                    "semantic_review_unit_key": "FOUNDATION:arg-foundation-001",
                    "quality_dimension": "FEASIBILITY_FOUNDATION",
                    "suggested_route": "ORIGINAL_PRODUCER",
                    "required_node_type": "TEAM_EVIDENCE",
                    "thread_index": 0,
                    "reason": "研究线程缺少合格研究基础。",
                    "blocking": True,
                    "suggested_source_or_question": (
                        "利用当前材料补充可核验研究基础；没有则保持未知。"
                    ),
                },
                {
                    "gap_id": "arg-advisory-gap-001",
                    "defect_key": "MODEL:ADVISORY:0",
                    "defect_family": "MODEL_DECLARED_GAP",
                    "finding_code": "RESEARCH_DESIGN_INCOMPLETE",
                    "semantic_component": "RESEARCH_DESIGN",
                    "suggested_route": "ORIGINAL_PRODUCER",
                    "required_node_type": "EVIDENCE",
                    "thread_index": 0,
                    "reason": "Optional supporting material may be added later.",
                    "blocking": False,
                    "suggested_source_or_question": "Optional follow-up.",
                },
            ]
        },
        "user_questions": [
            {
                "question_id": "advisory-only",
                "blocking": False,
                "question": "可选：后续是否补充更多内部材料？",
            }
        ],
    }

    state_before_failed_preflight = copy.deepcopy(state)
    original_build = engine.context_builder.build

    def fail_next_input_preflight(*_args, **_kwargs):
        raise ValueError("synthetic next-round input failure")

    monkeypatch.setattr(
        engine.context_builder, "build", fail_next_input_preflight
    )
    with pytest.raises(ValueError, match="synthetic next-round input failure"):
        engine._prepare_semantic_producer_regeneration(
            workflow,
            state,
            producer_prompt="P-ARGUMENT-ARCHITECTURE",
            output=revise_output,
        )
    assert state == state_before_failed_preflight
    assert engine.get(workflow["id"])["state"] == state_before_failed_preflight
    monkeypatch.setattr(engine.context_builder, "build", original_build)

    first = engine._prepare_semantic_producer_regeneration(
        workflow,
        state,
        producer_prompt="P-ARGUMENT-ARCHITECTURE",
        output=revise_output,
    )
    assert first == "SCHEDULED"

    scheduled = engine.get(workflow["id"])
    assert scheduled["status"] == "RUNNING"
    assert scheduled["current_step"] == argument_step
    assert engine.list_gates(workflow_id=workflow["id"]) == []

    feedback = scheduled["state"]["producer_revision_findings"][
        "P-ARGUMENT-ARCHITECTURE"
    ]
    assert len(feedback) == 1
    assert feedback[0]["code"] == "FOUNDATION_EVIDENCE_MISSING"
    assert feedback[0]["semantic_component"] == "FOUNDATION"
    assert feedback[0]["semantic_thread"] == 0
    assert feedback[0]["repair_instruction"]
    assert pack.validate_common("finding.schema.json", feedback[0]) == []
    next_envelope = engine.context_builder.build(
        "P-ARGUMENT-ARCHITECTURE",
        project_id,
        workflow_id=workflow["id"],
        workflow_state=scheduled["state"],
    )
    assert pack.validate(
        "P-ARGUMENT-ARCHITECTURE", "input", next_envelope
    ) == []

    second = engine._prepare_semantic_producer_regeneration(
        scheduled,
        scheduled["state"],
        producer_prompt="P-ARGUMENT-ARCHITECTURE",
        output=revise_output,
    )
    assert second == "EXHAUSTED"

    exhausted = engine.get(workflow["id"])
    assert exhausted["status"] == "BLOCKED_CONTENT"
    assert engine.list_gates(workflow_id=workflow["id"]) == []
    assert "不转为空问题人工 Gate" in exhausted["state"]["last_error"]
