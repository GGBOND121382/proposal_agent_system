from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

from app.diagram_enrichment import DiagramEnrichmentService
from app.documents import parse_document
from app.executor import PromptExecutor
from app.pack import PromptPack
from app.proposal_quality import ProposalQualityGuard
from app.simulated_llm import SimulatedLLM
from tests.test_runtime import add_standard_materials, create_project, finish_workflow, runtime


ROOT = Path(__file__).resolve().parents[1]


def _pack_sim_guard():
    pack = PromptPack(ROOT / "prompt_pack")
    return pack, SimulatedLLM(pack), ProposalQualityGuard()


def _foundation_document():
    return parse_document(
        "evidence.md",
        "# 前期成果\n团队已完成相关优化原型、实验代码、数据集和可复现实验记录，并形成初步对照结果。".encode(),
        "EVIDENCE_MATERIAL",
        "INTERNAL",
    )


def _valid_project_argument_readiness():
    pack, sim, guard = _pack_sim_guard()
    project_env = pack.replay_input("P-PROJECT-DEFINITION-EXTRACT")
    project_env["payload"]["source_documents"] = [_foundation_document()]
    project_output = sim.invoke("P-PROJECT-DEFINITION-EXTRACT", project_env)

    argument_env = pack.replay_input("P-ARGUMENT-ARCHITECTURE")
    argument_env["payload"]["argument_graph_seed"] = project_output["result"]["argument_graph_seed"]
    argument_output = sim.invoke("P-ARGUMENT-ARCHITECTURE", argument_env)

    readiness_env = pack.replay_input("P-PROJECT-READINESS-CRITIC")
    readiness_env["payload"]["project_definition"] = project_output["result"]["project_definition"]
    readiness_env["payload"]["argument_graph"] = argument_output["result"]["argument_architecture"]
    readiness_env["payload"]["readiness_stage"] = "READY_FOR_SECTION_PLANNING"
    readiness_output = sim.invoke("P-PROJECT-READINESS-CRITIC", readiness_env)
    return pack, sim, guard, project_env, project_output, argument_env, argument_output, readiness_env, readiness_output


def _codes(output):
    return {item.get("code") for item in output.get("findings", [])}


def test_valid_synthetic_argument_pipeline_passes_quality_guard():
    _, _, guard, p_env, p_out, a_env, a_out, r_env, r_out = _valid_project_argument_readiness()
    assert guard.apply("P-PROJECT-DEFINITION-EXTRACT", p_env, copy.deepcopy(p_out))["status"] == "PASS"
    assert guard.apply("P-ARGUMENT-ARCHITECTURE", a_env, copy.deepcopy(a_out))["status"] == "PASS"
    assert guard.apply("P-PROJECT-READINESS-CRITIC", r_env, copy.deepcopy(r_out))["status"] == "PASS"


def test_shallow_project_definition_is_rejected():
    _, _, guard, env, output, *_ = _valid_project_argument_readiness()
    candidate = copy.deepcopy(output)
    project = candidate["result"]["project_definition"]
    project["items"] = [item for item in project["items"] if item["item_type"] == "OBJECTIVE"]
    project["relations"] = []
    checked = guard.apply("P-PROJECT-DEFINITION-EXTRACT", env, candidate)
    assert checked["status"] == "REVISE"
    assert {"QG_PROJECT_GRAPH_INCOMPLETE", "QG_PROJECT_GRAPH_TOO_SHALLOW"}.issubset(_codes(checked))


def test_foundation_cannot_be_confirmed_by_guide_or_user_statement():
    _, _, guard, env, output, *_ = _valid_project_argument_readiness()
    candidate = copy.deepcopy(output)
    for item in candidate["result"]["project_definition"]["items"]:
        if item["item_type"] in {"ACHIEVEMENT", "CAPABILITY"}:
            item["knowledge_status"] = "CONFIRMED"
            item["source_refs"] = [{
                "source_id": "guide-foundation-claim",
                "source_type": "APPLICATION_GUIDE",
                "quoted_text": "申请团队应具备相关研究基础。",
                "source_hash": "1" * 64,
                "authority_rank": 90,
                "security_level": "INTERNAL",
            }]
    checked = guard.apply("P-PROJECT-DEFINITION-EXTRACT", env, candidate)
    assert checked["status"] == "REVISE"
    assert "QG_FOUNDATION_STATUS_EXCEEDS_EVIDENCE" in _codes(checked)


def test_false_section_planning_readiness_without_foundation_is_rejected():
    _, _, guard, _, project_output, _, argument_output, env, output = _valid_project_argument_readiness()
    project = copy.deepcopy(project_output["result"]["project_definition"])
    for item in project["items"]:
        if item["item_type"] in {"ACHIEVEMENT", "CAPABILITY"}:
            item["knowledge_status"] = "UNKNOWN"
            item["source_refs"] = []
    graph = copy.deepcopy(argument_output["result"]["argument_architecture"])
    for node in graph["nodes"]:
        if node["node_type"] == "TEAM_EVIDENCE":
            node["status"] = "UNKNOWN"
            node["source_refs"] = []
    env = copy.deepcopy(env)
    env["payload"]["project_definition"] = project
    env["payload"]["argument_graph"] = graph
    candidate = copy.deepcopy(output)
    candidate["result"]["ready_for_section_planning"] = True
    candidate["result"]["writeable_section_profiles"] = sorted(ProposalQualityGuard.REQUIRED_SECTION_PROFILES)
    checked = guard.apply("P-PROJECT-READINESS-CRITIC", env, candidate)
    assert checked["status"] == "REVISE"
    assert "QG_FOUNDATION_FALSE_READY" in _codes(checked)


def test_format_only_template_is_rejected():
    pack, sim, guard = _pack_sim_guard()
    env = pack.replay_input("P-TEMPLATE-EXTRACT")
    output = sim.invoke("P-TEMPLATE-EXTRACT", env)
    template = output["result"]["template"]
    template["components"] = template["components"][:2]
    template["argument_patterns"] = []
    template["expression_patterns"] = []
    checked = guard.apply("P-TEMPLATE-EXTRACT", env, output)
    assert checked["status"] == "REVISE"
    assert "QG_TEMPLATE_ONLY_FORMAT" in _codes(checked)


def test_fifty_four_cloned_tasks_and_document_bloat_are_rejected():
    pack, sim, guard = _pack_sim_guard()
    env = pack.replay_input("P-REVISION-PLAN")
    output = sim.invoke("P-REVISION-PLAN", env)
    plan = output["result"]["revision_plan"]
    plan["target_section_ids"] = [f"section-{i:03d}" for i in range(54)]
    base_task = copy.deepcopy(plan["tasks"][0])
    plan["tasks"] = []
    for i in range(54):
        task = copy.deepcopy(base_task)
        task["revision_task_id"] = f"task-{i:03d}"
        task["objective"] = f"补充《章节{i:03d}》的定位、问题、方法、指标和输出。"
        plan["tasks"].append(task)
    checked = guard.apply("P-REVISION-PLAN", env, output)
    assert checked["status"] == "REVISE"
    assert {"QG_PLAN_DOCUMENT_BLOAT", "QG_PLAN_TASKS_TEMPLATE_CLONED"}.issubset(_codes(checked))


def test_wrong_section_profile_and_generic_blueprint_are_rejected():
    pack, sim, guard = _pack_sim_guard()
    env = pack.replay_input("P-WRITE-BLUEPRINT")
    env["payload"]["source_section"]["title"] = "创新点"
    env["payload"]["section_profile"]["profile_id"] = "RESEARCH_CONTENT"
    output = sim.invoke("P-WRITE-BLUEPRINT", env)
    generic = [
        "1. 本节定位与研究目标", "2. 核心问题与约束", "3. 方法与技术方案",
        "4. 工程实施要点", "5. 指标与验收方法", "6. 预期输出及与其他任务的关系",
    ]
    for index, paragraph in enumerate(output["result"]["blueprint"]["paragraphs"]):
        paragraph["function"] = generic[index % len(generic)]
    checked = guard.apply("P-WRITE-BLUEPRINT", env, output)
    assert checked["status"] == "REVISE"
    assert {"QG_WRONG_SECTION_PROFILE", "QG_BLUEPRINT_GENERIC_SIX_PART_TEMPLATE"}.issubset(_codes(checked))


def test_write_critic_must_read_every_paragraph():
    pack, sim, guard = _pack_sim_guard()
    env = pack.replay_input("P-WRITE-CRITIC")
    output = sim.invoke("P-WRITE-CRITIC", env)
    output["result"]["checked_paragraph_ids"] = []
    checked = guard.apply("P-WRITE-CRITIC", env, output)
    assert checked["status"] == "REVISE"
    assert "QG_CRITIC_DID_NOT_READ_ALL_PARAGRAPHS" in _codes(checked)


def test_integration_rejects_partial_candidate_set_and_unknown_mapping_ids():
    pack, sim, guard = _pack_sim_guard()
    env = pack.replay_input("P-INTEGRATION-CRITIC")
    original = copy.deepcopy(env["payload"]["candidate_sections"][0])
    env["payload"]["candidate_sections"] = [original]
    env["payload"]["document_section_map"] = [
        {"section_id": "section-001", "title": "立项依据", "level": 1, "candidate_id": original["candidate"]["candidate_id"]},
        {"section_id": "section-002", "title": "研究方案", "level": 1, "candidate_id": "candidate-002"},
    ]
    output = sim.invoke("P-INTEGRATION-CRITIC", env)
    output["result"]["mapping_checks"] = [{
        "mapping_type": "OBJECTIVE_TO_WORK_PACKAGE",
        "source_id": "nonexistent-objective",
        "target_ids": ["nonexistent-task"],
        "complete": True,
    }]
    checked = guard.apply("P-INTEGRATION-CRITIC", env, output)
    assert checked["status"] == "REVISE"
    assert {"QG_INTEGRATION_CANDIDATE_SET_INCOMPLETE", "QG_INTEGRATION_FABRICATED_MAPPING"}.issubset(_codes(checked))


def test_expression_editor_cannot_drop_trace_links():
    pack, sim, guard = _pack_sim_guard()
    env = pack.replay_input("P-EXPRESSION-POLISH")
    output = sim.invoke("P-EXPRESSION-POLISH", env)
    assert env["payload"]["content_candidate"]["trace_links"]
    output["result"]["trace_links"] = []
    checked = guard.apply("P-EXPRESSION-POLISH", env, output)
    assert checked["status"] == "REVISE"
    assert "QG_EXPRESSION_TRACE_CHANGED" in _codes(checked)


def test_integration_rejects_cross_section_template_repetition():
    pack, sim, guard = _pack_sim_guard()
    env = pack.replay_input("P-INTEGRATION-CRITIC")
    repeated = "现有方法能够处理一般输入，但在动态事件下仍缺少影响范围与稳定性联合机制，因此需要建立新的增量优化方法。"
    sections = []
    section_map = []
    for i in range(4):
        sid = f"section-{i:03d}"
        cid = f"candidate-{i:03d}"
        sections.append({
            "section_id": sid,
            "candidate": {
                "candidate_id": cid,
                "candidate_text": repeated,
                "paragraphs": [{"paragraph_id": f"paragraph-{i:03d}", "text": repeated}],
                "trace_links": [], "term_usage": [], "unresolved_items": [],
            },
        })
        section_map.append({"section_id": sid, "title": f"章节{i}", "level": 1, "candidate_id": cid})
    env["payload"]["candidate_sections"] = sections
    env["payload"]["document_section_map"] = section_map
    output = sim.invoke("P-INTEGRATION-CRITIC", env)
    checked = guard.apply("P-INTEGRATION-CRITIC", env, output)
    assert checked["status"] == "REVISE"
    assert "QG_DOCUMENT_TEMPLATE_REPETITION" in _codes(checked)
    assert set(checked["result"]["redundancy_report"]["affected_section_ids"]) == {f"section-{i:03d}" for i in range(4)}


def test_integration_ignores_repeated_table_rendering_syntax():
    pack, sim, guard = _pack_sim_guard()
    env = pack.replay_input("P-INTEGRATION-CRITIC")
    sections = []
    section_map = []
    for i in range(4):
        sid = f"section-table-{i:03d}"
        cid = f"candidate-table-{i:03d}"
        table = (
            f"[[TABLE]]| 本节对象{i} | 本节结论{i} | 本节指标{i} | 本节边界{i} |\n"
            "|---|---|---|---|\n"
            f"| 对象{i} | 结论{i} | 指标{i} | 边界{i} |"
        )
        sections.append({
            "section_id": sid,
            "candidate": {
                "candidate_id": cid,
                "candidate_text": table,
                "paragraphs": [{"paragraph_id": f"paragraph-table-{i:03d}", "text": table}],
                "trace_links": [], "term_usage": [], "unresolved_items": [],
                "claim_advancement": {
                    "section_contract_id": f"contract-table-{i}",
                    "advanced_claim_ids": [f"claim-table-{i}"],
                    "new_information_keys": [f"information-table-{i}"],
                    "distinguished_from_section_ids": [],
                    "section_contribution": f"表格章节{i}",
                },
            },
        })
        section_map.append({"section_id": sid, "title": f"表格章节{i}", "level": 1, "candidate_id": cid})
    env["payload"]["candidate_sections"] = sections
    env["payload"]["document_section_map"] = section_map
    output = sim.invoke("P-INTEGRATION-CRITIC", env)
    checked = guard.apply("P-INTEGRATION-CRITIC", env, output)
    assert "QG_DOCUMENT_TEMPLATE_REPETITION" not in _codes(checked)


def test_blueprint_rejects_reuse_of_prior_section_information_key():
    pack, sim, guard = _pack_sim_guard()
    env = pack.replay_input("P-WRITE-BLUEPRINT")
    output = sim.invoke("P-WRITE-BLUEPRINT", env)
    reused_key = output["result"]["blueprint"]["paragraphs"][0]["novel_content_key"]
    env["payload"]["prior_section_digest"] = [{
        "section_id": "section-prior",
        "title": "前文章节",
        "advanced_claim_ids": ["prop-001"],
        "new_information_keys": [reused_key],
        "paragraph_roles": ["PROBLEM"],
        "sentence_signatures": ["signature-prior-001"],
    }]
    checked = guard.apply("P-WRITE-BLUEPRINT", env, output)
    assert checked["status"] == "REVISE"
    assert "QG_BLUEPRINT_REUSES_PRIOR_INFORMATION" in _codes(checked)


def test_content_rejects_inconsistent_claim_advancement_summary():
    pack, sim, guard = _pack_sim_guard()
    env = pack.replay_input("P-WRITE-CONTENT")
    output = sim.invoke("P-WRITE-CONTENT", env)
    output["result"]["claim_advancement"]["new_information_keys"] = ["unrelated-information-key"]
    checked = guard.apply("P-WRITE-CONTENT", env, output)
    assert checked["status"] == "REVISE"
    assert "QG_CONTENT_ADVANCEMENT_SUMMARY_INCONSISTENT" in _codes(checked)


def test_integration_rejects_duplicate_information_claim_concentration_and_same_skeleton():
    pack, sim, guard = _pack_sim_guard()
    env = pack.replay_input("P-INTEGRATION-CRITIC")
    seed = copy.deepcopy(env["payload"]["candidate_sections"][0]["candidate"])
    sections = []
    section_map = []
    for i in range(4):
        sid = f"section-unique-{i}"
        candidate = copy.deepcopy(seed)
        candidate["candidate_id"] = f"candidate-unique-{i}"
        sentence = f"针对“差异化命题{i}”，本项目建立统一分析机制，以输入、约束和状态为基础识别需要更新的决策变量，并通过对照实验检验机制有效性。"
        candidate["candidate_text"] = sentence
        candidate["paragraphs"] = [copy.deepcopy(candidate["paragraphs"][0])]
        candidate["paragraphs"][0].update({
            "paragraph_id": f"paragraph-unique-{i}",
            "text": sentence,
            "primary_claim_id": "method-shared",
            "novel_content_key": "shared-information-key",
        })
        candidate["claim_advancement"] = {
            "section_contract_id": f"contract-{i}",
            "advanced_claim_ids": ["method-shared"],
            "new_information_keys": ["shared-information-key"],
            "distinguished_from_section_ids": [],
            "section_contribution": f"章节{i}贡献",
        }
        sections.append({"section_id": sid, "candidate": candidate})
        section_map.append({"section_id": sid, "title": f"章节{i}", "level": 1, "candidate_id": candidate["candidate_id"]})
    env["payload"]["candidate_sections"] = sections
    env["payload"]["document_section_map"] = section_map
    env["payload"]["argument_graph"]["central_proposition"]["node_id"] = "prop-001"
    output = sim.invoke("P-INTEGRATION-CRITIC", env)
    checked = guard.apply("P-INTEGRATION-CRITIC", env, output)
    assert checked["status"] == "REVISE"
    assert {
        "QG_DOCUMENT_TEMPLATE_REPETITION",
        "QG_DOCUMENT_DUPLICATE_INFORMATION_KEYS",
        "QG_DOCUMENT_CLAIM_OVERCONCENTRATION",
    }.issubset(_codes(checked))
    report = checked["result"]["redundancy_report"]
    assert report["duplicate_information_key_groups"] == 1
    assert report["claim_overconcentration_groups"] == 1
    assert report["template_skeleton_groups"] >= 1


def test_generated_quality_defect_cannot_be_resolved_by_empty_confirmation():
    output = {
        "findings": [{
            "code": "QG_PLAN_TASKS_TEMPLATE_CLONED",
            "blocking": True,
            "suggested_route": "PLANNING_AGENT",
        }]
    }
    from app.workflows import WorkflowEngine
    assert WorkflowEngine._has_nonconfirmable_quality_failure(output) is True


def test_expression_editor_cannot_change_semantic_identity():
    pack, sim, guard = _pack_sim_guard()
    env = pack.replay_input("P-EXPRESSION-POLISH")
    output = sim.invoke("P-EXPRESSION-POLISH", env)
    assert output["result"]["paragraphs"]
    output["result"]["paragraphs"][0]["primary_claim_id"] = "different-claim-id"
    output["result"]["claim_advancement"]["advanced_claim_ids"] = ["different-claim-id"]
    checked = guard.apply("P-EXPRESSION-POLISH", env, output)
    assert checked["status"] == "REVISE"
    assert "QG_EXPRESSION_SEMANTIC_IDENTITY_CHANGED" in _codes(checked)


def test_integration_revision_findings_reach_rewrite_prompts(runtime):
    settings, _, db, _, builder, _, engine, _ = runtime
    project_id = create_project(db, internet=False)
    add_standard_materials(settings, db, project_id)

    async def prepare():
        for workflow_type in [
            "WF-1_PROJECT_INTAKE",
            "WF-2_TEMPLATE_EXTRACTION",
            "WF-4_PROPOSAL_AUTHORING",
        ]:
            workflow = await finish_workflow(engine, project_id, workflow_type)
            assert workflow["status"] == "COMPLETED", workflow["state"].get("last_error")
        return workflow

    workflow = asyncio.run(prepare())
    section = next(
        item for item in builder.sections(project_id, "CURRENT_PROPOSAL")
        if item.get("title") not in {"全文", ""}
    )
    finding = {
        "code": "DOCUMENT_TEMPLATE_REPETITION",
        "severity": "P1",
        "category": "INTEGRATION",
        "target_type": "SECTION",
        "target_path_or_span": section["section_id"],
        "description": "本章与其他章节使用了相同论证骨架。",
        "evidence_refs": [section["section_id"]],
        "repairable": True,
        "repair_instruction": "根据本章Section Contract重构独有命题和信息键。",
        "suggested_route": "WRITING_AGENT",
        "blocking": True,
    }
    state = {
        "workflow_type": "WF-4_PROPOSAL_AUTHORING",
        "active_section_id": section["section_id"],
        "active_section_title": section.get("title"),
        "integration_repair_section_ids": [section["section_id"]],
        "integration_repair_findings": [finding],
    }
    for prompt_id in ["P-WRITE-BLUEPRINT", "P-WRITE-CONTENT"]:
        envelope = builder.build(
            prompt_id,
            project_id,
            workflow_id=workflow["id"],
            workflow_state=state,
        )
        assert envelope["payload"]["revision_findings"] == [finding]
        assert isinstance(envelope["payload"]["prior_section_digest"], list)
        # Section calls receive a bounded semantic slice rather than the full
        # project, full plan and all previous prose.  This keeps the redesigned
        # workflow usable with short-context models.
        import json
        assert len(json.dumps(envelope, ensure_ascii=False)) < 80000
        assert len(envelope["payload"]["narrative_architecture"]["section_contracts"]) <= 5
        if "confirmed_plan" in envelope["payload"]:
            assert len(envelope["payload"]["confirmed_plan"]["tasks"]) <= 3
        if "read_only_context" in envelope["payload"]:
            assert all(len(item.get("text", "")) < 1500 for item in envelope["payload"]["read_only_context"])


def test_plan_rejects_multiple_information_owners_and_dependency_cycle():
    pack, sim, guard = _pack_sim_guard()
    env = pack.replay_input("P-REVISION-PLAN")
    output = sim.invoke("P-REVISION-PLAN", env)
    plan = output["result"]["revision_plan"]
    first = copy.deepcopy(plan["narrative_architecture"]["section_contracts"][0])
    second = copy.deepcopy(first)
    first["section_contract_id"] = "contract-a"
    first["section_id"] = "section-a"
    first["profile_id"] = "BACKGROUND_AND_SIGNIFICANCE"
    first["prerequisite_section_ids"] = ["section-b"]
    first["must_not_repeat_section_ids"] = ["section-b"]
    first["unique_information_keys"] = ["shared-information-key"]
    second["section_contract_id"] = "contract-b"
    second["section_id"] = "section-b"
    second["profile_id"] = "METHOD_AND_ALGORITHM"
    second["prerequisite_section_ids"] = ["section-a"]
    second["must_not_repeat_section_ids"] = ["section-a"]
    second["unique_information_keys"] = ["shared-information-key"]
    plan["narrative_architecture"]["section_contracts"] = [first, second]
    plan["target_section_ids"] = ["section-a", "section-b"]
    checked = guard.apply("P-REVISION-PLAN", env, output)
    assert checked["status"] == "REVISE"
    assert {
        "QG_PLAN_INFORMATION_KEY_MULTIPLE_OWNERS",
        "QG_PLAN_SECTION_DEPENDENCY_CYCLE",
    }.issubset(_codes(checked))


def test_quality_guard_failure_outputs_remain_schema_valid():
    pack, sim, guard = _pack_sim_guard()

    expression_env = pack.replay_input("P-EXPRESSION-POLISH")
    expression_output = sim.invoke("P-EXPRESSION-POLISH", expression_env)
    expression_output["result"]["paragraphs"][0]["primary_claim_id"] = "changed-claim"
    checked_expression = guard.apply("P-EXPRESSION-POLISH", expression_env, expression_output)
    assert checked_expression["status"] == "REVISE"
    assert pack.validate("P-EXPRESSION-POLISH", "output", checked_expression) == []

    argument_env = pack.replay_input("P-ARGUMENT-ARCHITECTURE")
    argument_output = sim.invoke("P-ARGUMENT-ARCHITECTURE", argument_env)
    argument_output["result"]["argument_architecture"]["central_proposition"]["falsifiable_or_comparable"] = False
    checked_argument = guard.apply("P-ARGUMENT-ARCHITECTURE", argument_env, argument_output)
    assert checked_argument["status"] == "REVISE"
    assert pack.validate("P-ARGUMENT-ARCHITECTURE", "output", checked_argument) == []


def test_section_critic_must_check_profile_rules_and_required_scorecard():
    pack, sim, guard = _pack_sim_guard()
    env = pack.replay_input("P-WRITE-CRITIC")
    env["payload"]["section_profile"]["acceptance_rules"] = ["必须核对独有方法机制", "必须核对实验基线"]
    env["payload"]["section_profile"]["profile_id"] = "METHOD_AND_ALGORITHM"
    output = sim.invoke("P-WRITE-CRITIC", env)
    output["result"]["profile_acceptance_results"] = [
        item for item in output["result"]["profile_acceptance_results"]
        if item["rule"] != "必须核对实验基线"
    ][:6]
    output["result"]["quality_dimensions"] = [
        item for item in output["result"]["quality_dimensions"]
        if item["dimension"] != "METHOD_SUBSTANCE"
    ]
    checked = guard.apply("P-WRITE-CRITIC", env, output)
    assert checked["status"] == "REVISE"
    assert {
        "QG_CRITIC_PROFILE_RULES_NOT_CHECKED",
        "QG_CRITIC_REQUIRED_SCORECARD_MISSING",
    }.issubset(_codes(checked))


def test_integration_critic_must_cover_full_quality_scorecard():
    pack, sim, guard = _pack_sim_guard()
    env = pack.replay_input("P-INTEGRATION-CRITIC")
    output = sim.invoke("P-INTEGRATION-CRITIC", env)
    output["result"]["quality_dimensions"] = [
        item for item in output["result"]["quality_dimensions"]
        if item["dimension"] not in {"FEASIBILITY_FOUNDATION", "METRIC_JUSTIFICATION"}
    ]
    checked = guard.apply("P-INTEGRATION-CRITIC", env, output)
    assert checked["status"] == "REVISE"
    assert "QG_INTEGRATION_SCOPE_TOO_NARROW" in _codes(checked)


def test_quality_finding_runtime_enums_match_schema():
    import ast
    import json
    source = (ROOT / "app" / "proposal_quality.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    categories = set()
    routes = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "QualityFinding":
            if len(node.args) >= 3 and isinstance(node.args[2], ast.Constant):
                categories.add(str(node.args[2].value))
            if len(node.args) >= 8 and isinstance(node.args[7], ast.Constant):
                routes.add(str(node.args[7].value))
    schema = json.loads((ROOT / "prompt_pack" / "schemas" / "common" / "finding.schema.json").read_text(encoding="utf-8"))
    assert categories <= set(schema["properties"]["category"]["enum"])
    assert routes <= set(schema["properties"]["suggested_route"]["enum"])


def test_simulated_multisection_output_has_unique_claim_ownership_and_no_template_repetition():
    """Positive control: the redesigned pipeline can produce a document that passes.

    Historical replay proves the guard rejects old defects.  This test proves the
    simulator also follows the redesigned contracts instead of merely learning
    how to reject prior output.
    """
    pack, sim, guard, _, project_output, _, argument_output, *_ = _valid_project_argument_readiness()
    argument = argument_output["result"]["argument_architecture"]
    titles = [
        "项目摘要", "立项依据", "国内外研究现状", "关键科学问题",
        "研究目标", "研究内容", "研究方案", "技术路线", "实验与评估",
        "创新点", "研究基础", "预期成果", "参考文献", "附录：原型与部署说明",
    ]
    seed_plan_env = pack.replay_input("P-REVISION-PLAN")
    seed_plan_env["payload"]["argument_graph"] = argument
    seed_plan_env["payload"]["linked_sections"] = [
        {
            "section_id": f"section-positive-{index:03d}",
            "section_key": title,
            "title": title,
            "level": 1,
            "text": "待根据论证架构编写。",
            "text_hash": "a" * 64,
            "block_ids": [],
            "contains_table": False,
            "contains_formula": False,
            "contains_image": False,
            "contains_comment": False,
            "contains_revision": False,
            "security_level": "INTERNAL",
        }
        for index, title in enumerate(titles, 1)
    ]
    seed_plan_env["payload"]["source_section"] = copy.deepcopy(seed_plan_env["payload"]["linked_sections"][0])
    plan_output = guard.apply(
        "P-REVISION-PLAN",
        seed_plan_env,
        sim.invoke("P-REVISION-PLAN", seed_plan_env),
    )
    assert plan_output["status"] == "PASS", _codes(plan_output)
    plan = plan_output["result"]["revision_plan"]
    contracts = [
        item for item in plan["narrative_architecture"]["section_contracts"]
        if item["placement"] != "OMIT"
    ]

    prior_digest = []
    candidate_sections = []
    document_section_map = []
    for contract in contracts:
        section = next(
            item for item in seed_plan_env["payload"]["linked_sections"]
            if item["section_id"] == contract["section_id"]
        )
        profile = pack.section_profile_for(section["title"])
        blueprint_env = pack.replay_input("P-WRITE-BLUEPRINT")
        blueprint_env["payload"].update({
            "source_section": section,
            "section_profile": profile,
            "section_contract": contract,
            "argument_graph": argument,
            "confirmed_plan": plan,
            "prior_section_digest": prior_digest,
            "revision_findings": [],
        })
        blueprint_output = guard.apply(
            "P-WRITE-BLUEPRINT",
            blueprint_env,
            sim.invoke("P-WRITE-BLUEPRINT", blueprint_env),
        )
        assert blueprint_output["status"] == "PASS", (section["title"], _codes(blueprint_output))

        content_env = pack.replay_input("P-WRITE-CONTENT")
        content_env["payload"].update({
            "source_section": section,
            "section_profile": profile,
            "section_contract": contract,
            "argument_graph": argument,
            "approved_blueprint": blueprint_output["result"]["blueprint"],
            "prior_section_digest": prior_digest,
            "revision_findings": [],
        })
        content_output = guard.apply(
            "P-WRITE-CONTENT",
            content_env,
            sim.invoke("P-WRITE-CONTENT", content_env),
        )
        assert content_output["status"] == "PASS", (section["title"], _codes(content_output))
        candidate = content_output["result"]
        candidate_sections.append({"section_id": section["section_id"], "candidate": candidate})
        document_section_map.append({
            "section_id": section["section_id"], "title": section["title"],
            "level": 1, "candidate_id": candidate["candidate_id"],
        })
        prior_digest.append({
            "section_id": section["section_id"],
            "title": section["title"],
            "advanced_claim_ids": candidate["claim_advancement"]["advanced_claim_ids"],
            "new_information_keys": candidate["claim_advancement"]["new_information_keys"],
            "paragraph_roles": [item["paragraph_role"] for item in candidate["paragraphs"]],
            "sentence_signatures": [],
        })

    integration_env = pack.replay_input("P-INTEGRATION-CRITIC")
    integration_env["payload"].update({
        "candidate_sections": candidate_sections,
        "document_section_map": document_section_map,
        "argument_graph": argument,
        "narrative_architecture": plan["narrative_architecture"],
        "project_definition": project_output["result"]["project_definition"],
    })
    checked = guard.apply(
        "P-INTEGRATION-CRITIC",
        integration_env,
        sim.invoke("P-INTEGRATION-CRITIC", integration_env),
    )
    assert checked["status"] == "PASS", _codes(checked)
    report = checked["result"]["redundancy_report"]
    assert all(report[key] == 0 for key in [
        "exact_duplicate_groups", "semantic_template_groups",
        "duplicate_information_key_groups", "claim_overconcentration_groups",
        "template_skeleton_groups",
    ])


def test_integration_routes_upstream_planning_defects_before_section_rewrite(runtime):
    settings, _, db, _, _, _, engine, _ = runtime
    project_id = create_project(db, internet=False)
    add_standard_materials(settings, db, project_id)

    async def prepare():
        for workflow_type in ["WF-1_PROJECT_INTAKE", "WF-2_TEMPLATE_EXTRACTION"]:
            workflow = await finish_workflow(engine, project_id, workflow_type)
            assert workflow["status"] == "COMPLETED", workflow["state"].get("last_error")

    asyncio.run(prepare())
    workflow = engine.start(project_id, "WF-4_PROPOSAL_AUTHORING")
    state = workflow["state"]
    output = {
        "result": {"redundancy_report": {"affected_section_ids": ["section-a"]}},
        "findings": [
            {
                "code": "QG_DOCUMENT_TEMPLATE_REPETITION", "blocking": True,
                "category": "INTEGRATION", "suggested_route": "WRITING_AGENT",
            },
            {
                "code": "QG_DOCUMENT_CLAIM_OVERCONCENTRATION", "blocking": True,
                "category": "INTEGRATION", "suggested_route": "PLANNING_AGENT",
            },
        ],
    }
    result = engine._prepare_integration_repair(workflow, state, output)
    assert result == "SCHEDULED"
    refreshed = engine.get(workflow["id"])
    expected_step = next(
        index for index, step in enumerate(refreshed["steps"])
        if step.get("prompt_id") == "P-REVISION-PLAN"
    )
    assert refreshed["current_step"] == expected_step
    assert refreshed["state"]["planning_revision_findings"][0]["code"] == "QG_DOCUMENT_CLAIM_OVERCONCENTRATION"
    assert "integration_repair_section_ids" not in refreshed["state"]


def test_whole_document_model_context_is_compacted_without_losing_schema():
    pack = PromptPack(ROOT / "prompt_pack")
    envelope = pack.replay_input("P-INTEGRATION-CRITIC")
    candidate = envelope["payload"]["candidate_sections"][0]["candidate"]
    long_text = "本段用于验证全篇模型输入压缩，同时保留命题、证据和章节合同身份。" * 1800
    candidate["candidate_text"] = long_text
    candidate["paragraphs"][0]["text"] = long_text
    original_size = len(json.dumps(envelope, ensure_ascii=False))
    executor = PromptExecutor.__new__(PromptExecutor)
    model_envelope, metadata = executor._prepare_model_envelope("P-INTEGRATION-CRITIC", envelope)
    model_size = len(json.dumps(model_envelope, ensure_ascii=False))
    assert metadata is not None
    assert metadata["quality_guard_uses_full_context"] is True
    assert model_size < original_size * 0.25
    assert pack.validate("P-INTEGRATION-CRITIC", "input", model_envelope) == []
    compact_paragraph = model_envelope["payload"]["candidate_sections"][0]["candidate"]["paragraphs"][0]
    assert compact_paragraph["primary_claim_id"] == candidate["paragraphs"][0]["primary_claim_id"]
    assert compact_paragraph["novel_content_key"] == candidate["paragraphs"][0]["novel_content_key"]
    assert len(compact_paragraph["text"]) <= 200


def test_diagram_fallback_requires_explicit_semantic_intent():
    service = DiagramEnrichmentService.__new__(DiagramEnrichmentService)
    output = {
        "status": "PASS",
        "result": {
            "paragraphs": [{
                "paragraph_id": "p-1",
                "sequence": 1,
                "paragraph_role": "METHOD",
                "text": "本段说明方法机制，但未提出图示意图。",
                "blueprint_paragraph_id": "bp-1",
                "trace_link_ids": ["trace-1"],
                "preserved_source_span": None,
                "contains_unresolved_placeholder": False,
                "primary_claim_id": "method-001",
                "evidence_ids": ["evidence-001"],
                "novel_content_key": "method-mechanism-001",
                "section_contract_id": "contract-001",
            }],
            "candidate_text": "本段说明方法机制，但未提出图示意图。",
        },
    }
    result = asyncio.run(service.enrich(
        project_id="project-test",
        workflow_id="workflow-test",
        run_id="run-test",
        section={"section_id": "section-method", "title": "技术路线"},
        output=copy.deepcopy(output),
        security_level="INTERNAL",
    ))
    assert result["result"]["paragraphs"] == output["result"]["paragraphs"]
    assert "warnings" not in result




def test_diagram_enrichment_preserves_approved_argument_role():
    service = DiagramEnrichmentService.__new__(DiagramEnrichmentService)

    class RenderResult:
        output = {"figure_marker": "[[FIGURE]]figure-1|技术路线图|15.0"}

    async def fake_execute(payload, **kwargs):
        assert payload["argument_purpose"] == "RESEARCH_QUESTION"
        return RenderResult()

    service._execute_mermaid = fake_execute
    service._persist_enriched_output = lambda *args, **kwargs: None
    output = {
        "status": "PASS",
        "result": {
            "paragraphs": [{
                "paragraph_id": "p-figure", "sequence": 1,
                "paragraph_role": "RESEARCH_QUESTION",
                "text": "[[MERMAID]]技术路线图|15\nflowchart LR\nA --> B",
                "blueprint_paragraph_id": "bp-figure",
                "trace_link_ids": ["trace-figure"],
                "preserved_source_span": None,
                "contains_unresolved_placeholder": False,
                "primary_claim_id": "rq-001",
                "evidence_ids": ["evidence-001"],
                "novel_content_key": "rq-map-001",
                "section_contract_id": "contract-001",
            }],
            "candidate_text": "[[MERMAID]]技术路线图|15\nflowchart LR\nA --> B",
        },
    }
    result = asyncio.run(service.enrich(
        project_id="project-test", workflow_id="workflow-test", run_id="run-test",
        section={"section_id": "section-route", "title": "总体技术路线"},
        output=copy.deepcopy(output), security_level="PUBLIC",
    ))
    paragraph = result["result"]["paragraphs"][0]
    assert paragraph["paragraph_role"] == "RESEARCH_QUESTION"
    assert paragraph["text"].startswith("[[FIGURE]]")


def test_user_asserted_capability_with_evidence_material_allows_warned_foundation_planning():
    _, _, guard, _, project_output, _, argument_output, env, output = _valid_project_argument_readiness()
    project = copy.deepcopy(project_output["result"]["project_definition"])
    for item in project["items"]:
        if item["item_type"] == "ACHIEVEMENT":
            item["knowledge_status"] = "UNKNOWN"
            item["source_refs"] = []
        elif item["item_type"] == "CAPABILITY":
            item["knowledge_status"] = "USER_ASSERTED"
            item["source_refs"] = [{
                "source_id": "capability-evidence-span",
                "source_type": "EVIDENCE_MATERIAL",
                "quoted_text": "团队具备Python开发、Git仓库分析和自动化测试能力，但尚无直接课题成果。",
                "source_hash": "2" * 64,
                "authority_rank": 80,
                "security_level": "INTERNAL",
            }]
    graph = copy.deepcopy(argument_output["result"]["argument_architecture"])
    for node in graph["nodes"]:
        if node["node_type"] == "TEAM_EVIDENCE":
            node["status"] = "SUPPORTED"
            node["source_refs"] = copy.deepcopy(next(
                item for item in project["items"] if item["item_type"] == "CAPABILITY"
            )["source_refs"])
    env = copy.deepcopy(env)
    env["payload"]["project_definition"] = project
    env["payload"]["argument_graph"] = graph
    candidate = copy.deepcopy(output)
    candidate["result"]["ready_for_section_planning"] = True
    candidate["result"]["writeable_section_profiles"] = sorted(ProposalQualityGuard.REQUIRED_SECTION_PROFILES)
    for item in candidate["result"]["chapter_readiness"]:
        if item["profile_id"] == "RESEARCH_FOUNDATION":
            item["readiness"] = "READY_WITH_WARNINGS"
    checked = guard.apply("P-PROJECT-READINESS-CRITIC", env, candidate)
    assert checked["status"] == "PASS"
    assert "QG_FOUNDATION_FALSE_READY" not in _codes(checked)


def test_user_asserted_capability_must_keep_foundation_warning():
    _, _, guard, _, project_output, _, argument_output, env, output = _valid_project_argument_readiness()
    project = copy.deepcopy(project_output["result"]["project_definition"])
    for item in project["items"]:
        if item["item_type"] == "ACHIEVEMENT":
            item["knowledge_status"] = "UNKNOWN"
            item["source_refs"] = []
        elif item["item_type"] == "CAPABILITY":
            item["knowledge_status"] = "USER_ASSERTED"
            item["source_refs"] = [{
                "source_id": "capability-evidence-span",
                "source_type": "EVIDENCE_MATERIAL",
                "quoted_text": "团队具备Python开发和自动化测试能力，但尚无直接课题成果。",
                "source_hash": "3" * 64,
                "authority_rank": 80,
                "security_level": "INTERNAL",
            }]
    graph = copy.deepcopy(argument_output["result"]["argument_architecture"])
    for node in graph["nodes"]:
        if node["node_type"] == "TEAM_EVIDENCE":
            node["status"] = "SUPPORTED"
            node["source_refs"] = copy.deepcopy(next(
                item for item in project["items"] if item["item_type"] == "CAPABILITY"
            )["source_refs"])
    env = copy.deepcopy(env)
    env["payload"]["project_definition"] = project
    env["payload"]["argument_graph"] = graph
    candidate = copy.deepcopy(output)
    candidate["result"]["ready_for_section_planning"] = True
    candidate["result"]["writeable_section_profiles"] = sorted(ProposalQualityGuard.REQUIRED_SECTION_PROFILES)
    for item in candidate["result"]["chapter_readiness"]:
        if item["profile_id"] == "RESEARCH_FOUNDATION":
            item["readiness"] = "READY"
    checked = guard.apply("P-PROJECT-READINESS-CRITIC", env, candidate)
    assert checked["status"] == "REVISE"
    assert "QG_FOUNDATION_EVIDENCE_STRENGTH_OVERSTATED" in _codes(checked)


def test_plan_accepts_specialized_argument_roles_shared_with_blueprint_schema():
    pack, sim, guard = _pack_sim_guard()
    env = pack.replay_input("P-REVISION-PLAN")
    output = sim.invoke("P-REVISION-PLAN", env)
    contract = output["result"]["revision_plan"]["narrative_architecture"]["section_contracts"][0]
    contract["required_argument_roles"] = ["INPUT", "DATA_FLOW", "FEEDBACK", "MILESTONE"]
    checked = guard.apply("P-REVISION-PLAN", env, output)
    assert "QG_PLAN_ARGUMENT_ROLE_INVALID" not in _codes(checked)
    assert not pack.validate("P-REVISION-PLAN", "output", output)


def test_section_contract_schema_rejects_unknown_argument_role():
    pack, sim, _ = _pack_sim_guard()
    env = pack.replay_input("P-REVISION-PLAN")
    output = sim.invoke("P-REVISION-PLAN", env)
    contract = output["result"]["revision_plan"]["narrative_architecture"]["section_contracts"][0]
    contract["required_argument_roles"] = ["MODEL_SPECIFIC_UNDECLARED_ROLE"]
    errors = pack.validate("P-REVISION-PLAN", "output", output)
    assert errors
