from __future__ import annotations

import asyncio
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pytest
from types import SimpleNamespace

from app.argument_two_stage_orchestration import (
    ARGUMENT_DESIGN_STAGE,
    ARGUMENT_SKELETON_STAGE,
    ArgumentStageContractError,
    argument_stage_desired_output_tokens,
    argument_stage_prompt_text,
    orchestrate_argument_architecture_two_stage,
)
from app.deterministic_repair import apply_deterministic_contract_repairs
from app.executor import PromptExecutor
from app.llm import LLMResult
from app.model_semantic_contracts import (
    _argument_quantified_support,
    _critic_chain_checks,
    build_argument_architecture_critic_model_input,
    build_argument_architecture_model_input,
    build_argument_skeleton_model_input,
    argument_skeleton_model_output_errors,
    build_argument_design_model_input,
    argument_design_model_output_errors,
    argument_design_model_reference_errors,
    assemble_argument_authored_thread,
    assemble_argument_authored_state,
    split_argument_authored_state,
    build_targeted_repair_model_input,
    expand_argument_architecture_critic_model_output,
    expand_argument_architecture_model_output,
    project_argument_authoritative_state,
    expand_targeted_repair_model_output,
    semantic_model_reference_errors,
    targeted_repair_semantic_errors,
    targeted_repair_structural_blockers,
)
from app.pack import PromptPack
from app.security import SecurityRouter

ROOT = Path(__file__).resolve().parents[1]
PACK = PromptPack(ROOT / "prompt_pack")


def _available_evidence_ids(envelope: dict) -> list[str]:
    return [card["evidence_id"] for card in build_argument_architecture_model_input(envelope)["evidence_cards"][:1]]






def _source_ref(source_id: str = "src-shared", quoted_text: str = "可核验证据") -> dict:
    return {
        "source_id": source_id,
        "source_type": "EVIDENCE_MATERIAL",
        "document_version_id": None,
        "section_id": None,
        "span_start": None,
        "span_end": None,
        "quoted_text": quoted_text,
        "source_hash": "b" * 64,
        "authority_rank": 90,
        "security_level": "INTERNAL",
    }


def _claim(claim_id: str, text: str, *, source_id: str = "src-shared", quoted_text: str | None = None) -> dict:
    return {
        "claim_id": claim_id,
        "claim_text": text,
        "claim_type": "FACT",
        "subject_id": None,
        "temporal_status": "TIME_INDEPENDENT",
        "qualifiers": [],
        "numeric_values": [],
        "source_refs": [_source_ref(source_id, quoted_text or text)],
        "knowledge_status": "DOCUMENT_EXTRACTED",
        "security_level": "INTERNAL",
    }


def _argument_envelope_with_evidence() -> dict:
    envelope = copy.deepcopy(PACK.replay_input("P-ARGUMENT-ARCHITECTURE"))
    envelope["payload"]["confirmed_facts"] = [
        _claim("E1", "已有材料支持动态优化、比较基线和原型研究基础。")
    ]
    return envelope


def _critic_semantic_all_pass(model_input: dict) -> dict:
    dims = [
        "CENTRAL_THESIS",
        "ARGUMENT_CHAIN",
        "EVIDENCE_SUPPORT",
        "METHOD_SUBSTANCE",
        "INNOVATION_BASELINE",
        "FEASIBILITY_FOUNDATION",
        "METRIC_JUSTIFICATION",
    ]
    return {
        "quality_dimensions": [
            {
                "dimension": d,
                "score": 4,
                "passed": True,
                "evidence": [f"{d}满足要求"],
                "required_action": None,
            }
            for d in dims
        ],
        "reviewed_unit_keys": [
            u["unit_key"] for u in model_input["candidate"]["review_units"]
        ],
        "issues": [],
        "user_questions": [],
    }
def _semantic_argument_output(envelope: dict) -> dict:
    evidence_ids = _available_evidence_ids(envelope)
    return {
        "central_proposition": {
            "statement": "通过显式建模事件影响范围和计划稳定性，可在动态场景中同时降低重规划时延与非必要方案扰动。",
            "proposition_type": "TECHNICAL_PRINCIPLE",
            "falsifiable_or_comparable": True,
            "boundary_conditions": ["动态订单、交通变化和有限运力场景"],
            "evidence_ids": evidence_ids,
        },
        "scope": {
            "in_scope": ["运输任务分配、路径与动态重规划"],
            "out_of_scope": ["部署运维细节作为主文研究内容"],
        },
        "research_threads": [
            {
                "gap": {
                    "statement": "动态环境下静态优化难以同时控制方案质量、响应时间和计划扰动。",
                    "limitation_mechanism": {
                        "statement": "全量重算没有显式区分受事件影响与未受影响的决策子结构。",
                        "evidence_ids": evidence_ids,
                    },
                    "evidence_ids": evidence_ids,
                },
                "question": {
                    "statement": "如何在动态事件下联合控制求解时延、方案质量和计划扰动？",
                    "question_type": "SCIENTIFIC",
                    "answerability": "COMPARABLE",
                    "success_evidence": ["与滚动优化和全量重算基线比较"],
                },
                "objective": {
                    "statement": "建立面向动态事件的低扰动运输方案优化方法。",
                    "evidence_ids": evidence_ids,
                },
                "thread_assumptions": ["事件影响可被映射到有限的业务对象与约束集合。"],
                "work_packages": [
                    {
                        "statement": "研究动态事件下的增量求解与低扰动重规划。",
                        "evidence_ids": evidence_ids,
                        "methods": [
                            {
                                "statement": "构建含时效、成本、资源和扰动代价的多目标组合优化模型。",
                                "method_type": "FORMAL_MODEL",
                                "evidence_ids": evidence_ids,
                                "assumptions": ["输入事件在一次滚动窗口内保持一致。"],
                                "theoretical_properties": [
                                    {
                                        "statement": "研究局部重规划在给定约束下的可行性保持条件。",
                                        "evidence_ids": evidence_ids,
                                    }
                                ],
                                "evaluations": [
                                    {
                                        "statement": "在静态、动态和故障场景下与代表性基线比较。",
                                        "evidence_ids": evidence_ids,
                                        "baselines": [
                                            {
                                                "statement": "采用全量重算与滚动优化作为比较基线。",
                                                "evidence_ids": evidence_ids,
                                            }
                                        ],
                                        "ablations": ["移除影响子图限制，仅保留稳定性代价。"],
                                        "success_criteria": ["在方案质量不下降的条件下降低重规划时延和非必要扰动。"],
                                    }
                                ],
                            }
                        ],
                    }
                ],
                "innovations": [
                    {
                        "statement": "通过影响子图和稳定性代价联合约束重规划范围。",
                        "evidence_ids": evidence_ids,
                        "contribution": "形成兼顾响应效率与计划稳定性的动态重规划机制。",
                        "closest_prior_work": [
                            {
                                "statement": "现有滚动优化可处理事件更新，但对跨任务扰动传播的统一控制不足。",
                                "evidence_ids": evidence_ids,
                            }
                        ],
                        "evaluation_refs": [
                            {
                                "work_package_index": 0,
                                "method_index": 0,
                                "evaluation_index": 0,
                            }
                        ],
                    }
                ],
                "foundation": [
                    {
                        "statement": "已有相关优化算法与原型验证材料。",
                        "evidence_ids": evidence_ids,
                        "supports": [
                            {
                                "work_package_index": 0,
                                "method_index": 0,
                            }
                        ],
                    }
                ],
                "falsification_or_comparison_rule": "若相对滚动优化和全量重算不能同时降低时延与非必要扰动，则中心命题不成立。",
            }
        ],
        "evidence_gaps": [],
        "user_questions": [],
        "cannot_proceed_reason": None,
    }


def test_argument_model_input_is_semantic_minimum_not_runtime_envelope():
    envelope=PACK.replay_input("P-ARGUMENT-ARCHITECTURE")
    model_input=build_argument_architecture_model_input(envelope)
    assert PACK.validate_model("P-ARGUMENT-ARCHITECTURE","input",model_input)==[]
    assert set(model_input)=={"project_task","constraints","evidence_cards","design_seed","revision_issues","human_resolutions"}
    assert "specific_requirements" in model_input["project_task"]
    assert "priority_order" in model_input["project_task"]
    rendered=json.dumps(model_input,ensure_ascii=False)
    for forbidden in ("current_sections","template_context","trusted_source_catalog","source_hash","document_version_id","project_id","task_id","prompt_id"):
        assert forbidden not in rendered
    assert len(rendered)<len(json.dumps(envelope,ensure_ascii=False))*0.35


def test_argument_model_output_runtime_derives_graph_matrix_and_ids():
    envelope=PACK.replay_input("P-ARGUMENT-ARCHITECTURE"); semantic=_semantic_argument_output(envelope)
    assert PACK.validate_model("P-ARGUMENT-ARCHITECTURE","output",semantic)==[]
    schema_text=json.dumps(PACK.model_schema("P-ARGUMENT-ARCHITECTURE","output"))
    for machine_field in ("node_id","edge_id","graph_id","research_design_matrix","changed_paths","source_hash",'"status"','"findings"'):
        assert machine_field not in schema_text
    canonical=expand_argument_architecture_model_output(envelope,semantic)
    assert PACK.validate("P-ARGUMENT-ARCHITECTURE","output",canonical)==[]
    graph=canonical["result"]["argument_architecture"]
    defined={n["node_id"] for n in graph["nodes"]}|{q["node_id"] for q in graph["research_questions"]}
    for row in canonical["result"]["research_design_matrix"]:
        assert row["research_question_id"] in defined
        for field in ("gap_ids","objective_ids","work_package_ids","method_ids","evaluation_ids","innovation_ids","foundation_evidence_ids","closest_prior_work_ids"):
            assert set(row[field])<=defined


def test_choice_answer_schema_is_runtime_derived():
    envelope=PACK.replay_input("P-ARGUMENT-ARCHITECTURE"); semantic=_semantic_argument_output(envelope)
    semantic["user_questions"]=[{"target_area":"METRIC_JUSTIFICATION","question_type":"CHOICE","question":"指标阈值采用哪一种来源？","reason":"该选择影响验收规则。","answer_shape":"STRING","allowed_values":["预实验","指南","平台约束"],"blocking":True,"priority":"P0"}]
    canonical=expand_argument_architecture_model_output(envelope,semantic)
    assert canonical["user_questions"][0]["answer_schema"]=={"type":"ENUM","allowed_values":["预实验","指南","平台约束"]}
    assert canonical["status"]=="NEED_USER_INPUT"


def test_boolean_choice_projects_to_the_same_boolean_gate_contract_as_confirmation():
    envelope=PACK.replay_input("P-ARGUMENT-ARCHITECTURE"); semantic=_semantic_argument_output(envelope)
    semantic["user_questions"]=[{"target_area":"RESEARCH_DESIGN","question_type":"CHOICE","question":"是否确认采用该方案？","reason":"需要明确确认。","answer_shape":"BOOLEAN","allowed_values":[True,False],"blocking":True,"priority":"P0"}]

    canonical=expand_argument_architecture_model_output(envelope,semantic)

    assert canonical["user_questions"][0]["answer_schema"]=={"type":"BOOLEAN","allowed_values":[]}
    assert PACK.validate("P-ARGUMENT-ARCHITECTURE","output",canonical)==[]


@pytest.mark.parametrize(
    ("question_type", "allowed_values", "question"),
    [
        (
            "CONFIRMATION",
            [],
            "是否具备人员实验条件？若不具备，是否仅进行专家评估与离线回放？",
        ),
        ("CONFIRMATION", [], "如果没有正式数据，是否改用专家评估？"),
        ("CONFIRMATION", [], "验收基线采用内部基线还是公开基线？"),
        (
            "CHOICE",
            [True, False],
            "Do we have approval? If not, should we use offline evaluation?",
        ),
    ],
)
def test_compound_or_conditional_boolean_question_deterministically_uses_text(
    question_type, allowed_values, question
):
    envelope = PACK.replay_input("P-ARGUMENT-ARCHITECTURE")
    semantic = _semantic_argument_output(envelope)
    semantic["user_questions"] = [{
        "target_area": "RESEARCH_DESIGN",
        "question_type": question_type,
        "question": question,
        "reason": "需要用户完整回答，不得将多个命题压缩成一个布尔值。",
        "answer_shape": "BOOLEAN",
        "allowed_values": allowed_values,
        "blocking": True,
        "priority": "P0",
    }]

    canonical = expand_argument_architecture_model_output(envelope, semantic)

    assert canonical["user_questions"][0]["answer_schema"] == {"type": "STRING"}
    assert PACK.validate("P-ARGUMENT-ARCHITECTURE", "output", canonical) == []


@pytest.mark.parametrize(
    "question",
    [
        "是否确认采用当前研究范围？",
        "Can the current baseline be accepted?",
        "Is software available?",
    ],
)
def test_single_proposition_confirmation_remains_boolean(question):
    envelope = PACK.replay_input("P-ARGUMENT-ARCHITECTURE")
    semantic = _semantic_argument_output(envelope)
    semantic["user_questions"] = [{
        "target_area": "PROJECT_SCOPE",
        "question_type": "CONFIRMATION",
        "question": question,
        "reason": "只确认一个命题。",
        "answer_shape": "BOOLEAN",
        "allowed_values": [],
        "blocking": True,
        "priority": "P0",
    }]

    canonical = expand_argument_architecture_model_output(envelope, semantic)

    assert canonical["user_questions"][0]["answer_schema"] == {
        "type": "BOOLEAN",
        "allowed_values": [],
    }


@pytest.mark.parametrize(
    ("question_type", "answer_shape", "question", "allowed_values"),
    [
        (
            "CONFIRMATION",
            "OBJECT",
            "软件模块的名称与状态是什么？请给出语料规模区间。",
            [],
        ),
        (
            "CHOICE",
            "ARRAY",
            "哪些指标仅采用离线回放，哪些保留人员实验？",
            ["速度", "覆盖度", "质量", "可信性", "人员", "工程"],
        ),
    ],
)
def test_authored_composite_answer_shape_wins_over_question_type(
    question_type, answer_shape, question, allowed_values
):
    envelope = PACK.replay_input("P-ARGUMENT-ARCHITECTURE")
    semantic = _semantic_argument_output(envelope)
    semantic["user_questions"] = [{
        "target_area": "RESEARCH_DESIGN",
        "question_type": question_type,
        "question": question,
        "reason": "该问题需要完整文本回答。",
        "answer_shape": answer_shape,
        "allowed_values": allowed_values,
        "blocking": True,
        "priority": "P0",
    }]

    canonical = expand_argument_architecture_model_output(envelope, semantic)

    assert canonical["user_questions"][0]["answer_schema"] == {"type": "STRING"}
    assert PACK.validate("P-ARGUMENT-ARCHITECTURE", "output", canonical) == []


@pytest.mark.parametrize("answer_shape", ["OBJECT", "ARRAY"])
def test_unstructured_composite_question_projects_to_string_gate_answer(answer_shape):
    envelope=PACK.replay_input("P-ARGUMENT-ARCHITECTURE"); semantic=_semantic_argument_output(envelope)
    semantic["user_questions"]=[{"target_area":"RESEARCH_DESIGN","question_type":"MISSING_INFORMATION","question":"请补充详细信息。","reason":"需要用户确认。","answer_shape":answer_shape,"allowed_values":[],"blocking":True,"priority":"P0"}]

    canonical=expand_argument_architecture_model_output(envelope,semantic)

    assert canonical["user_questions"][0]["answer_schema"]=={"type":"STRING"}
    assert PACK.validate("P-ARGUMENT-ARCHITECTURE","output",canonical)==[]


def test_critic_model_only_judges_semantic_quality_runtime_builds_receipts():
    envelope=PACK.replay_input("P-ARGUMENT-ARCHITECTURE-CRITIC")
    model_input=build_argument_architecture_critic_model_input(envelope)
    assert PACK.validate_model("P-ARGUMENT-ARCHITECTURE-CRITIC","input",model_input)==[]
    dims=["CENTRAL_THESIS","ARGUMENT_CHAIN","EVIDENCE_SUPPORT","METHOD_SUBSTANCE","INNOVATION_BASELINE","FEASIBILITY_FOUNDATION","METRIC_JUSTIFICATION"]
    semantic={
        "quality_dimensions":[{"dimension":d,"score":4,"passed":True,"evidence":[f"{d}满足要求"],"required_action":None} for d in dims],
        "reviewed_unit_keys":[u["unit_key"] for u in model_input["candidate"]["review_units"]],
        "issues":[],
        "user_questions":[],
    }
    canonical=expand_argument_architecture_critic_model_output(envelope,semantic)
    assert PACK.validate("P-ARGUMENT-ARCHITECTURE-CRITIC","output",canonical)==[]
    assert len(canonical["result"]["chain_checks"])==8
    assert canonical["result"]["checked_node_ids"]


def test_targeted_repair_model_sees_write_target_and_read_context_not_original_object():
    envelope=PACK.replay_input("P-TARGETED-REPAIR")
    model_input=build_targeted_repair_model_input(envelope)
    assert PACK.validate_model("P-TARGETED-REPAIR","input",model_input)==[]
    assert model_input["repair_targets"][0]["path"]=="/text"
    rendered=json.dumps(model_input,ensure_ascii=False)
    for forbidden in ("original_object","protected_hashes","object_hash","source_hash","inherited_source_catalog"):
        assert forbidden not in rendered


def test_targeted_repair_operation_runtime_applies_and_receipts():
    envelope=PACK.replay_input("P-TARGETED-REPAIR")
    semantic={"decision":"APPLY","changes":[{"path":"/text","value":"有来源支持的修订内容"}],"escalation_reason":None}
    assert targeted_repair_semantic_errors(envelope,semantic)==[]
    canonical=expand_targeted_repair_model_output(envelope,semantic)
    assert canonical["result"]["changed_paths"]==["/content/text"]
    assert PACK.validate("P-TARGETED-REPAIR","output",canonical)==[]
    PromptExecutor._validate_output_semantics("P-TARGETED-REPAIR",envelope,canonical)


def test_structural_repair_is_rejected_or_escalated():
    envelope=copy.deepcopy(PACK.replay_input("P-TARGETED-REPAIR"))
    envelope["payload"]["original_object"]["content"]={"status":"REVISE","result":{"argument_architecture":{"research_questions":[],"nodes":[{"node_id":"WP-1","node_type":"WORK_PACKAGE","statement":"现有工作包","status":"PLANNED"}]},"research_design_matrix":[{"method_ids":["METHOD-MISSING"]}]},"findings":[],"user_questions":[]}
    envelope["payload"]["findings_to_repair"][0]["target_path_or_span"]="/result/research_design_matrix/0/method_ids/0"
    envelope["payload"]["allowed_paths"]=["/content/result/research_design_matrix/0/method_ids/0"]
    envelope["payload"]["protected_paths"]=[]; envelope["payload"]["protected_hashes"]=[]
    blockers=targeted_repair_structural_blockers(envelope)
    assert any("no existing METHOD entity" in x for x in blockers)


def test_deterministic_rules_need_no_model():
    candidate={"status":"REVISE","findings":[{"blocking":True,"suggested_route":"USER","repairable":True}],"user_questions":[{"question_type":"MISSING_INFORMATION","blocking":True,"answer_schema":{"type":"CHOICE","allowed_values":["A","B"]}}]}
    errors=["/findings/0/repairable: blocking USER-routed Finding cannot be marked repairable","/user_questions/0/answer_schema/type: 'CHOICE' is not allowed"]
    repaired=apply_deterministic_contract_repairs(candidate,errors,["/findings/0/repairable","/user_questions/0/answer_schema/type","/status"])
    assert repaired.candidate["findings"][0]["repairable"] is False
    assert repaired.candidate["user_questions"][0]["answer_schema"]["type"]=="ENUM"
    assert repaired.candidate["status"]=="NEED_USER_INPUT"


def test_deterministic_question_type_repair_does_not_create_multi_select_enum():
    candidate = {
        "status": "NEED_USER_INPUT",
        "findings": [],
        "user_questions": [{
            "question_id": "question-multi",
            "question_type": "CHOICE",
            "question": "哪些指标采用离线回放，哪些保留人员实验？",
            "answer_schema": {
                "type": "CHOICE",
                "allowed_values": ["速度", "覆盖度", "人员"],
            },
            "blocking": True,
        }],
    }
    repaired = apply_deterministic_contract_repairs(
        candidate,
        ["/user_questions/0/answer_schema/type: 'CHOICE' is not allowed"],
        ["/user_questions/0/answer_schema/type"],
    )

    assert repaired.candidate["user_questions"][0]["answer_schema"]["type"] == "STRING"


def test_failed_critic_dimension_requires_corresponding_issue():
    envelope = PACK.replay_input("P-ARGUMENT-ARCHITECTURE-CRITIC")
    model_input = build_argument_architecture_critic_model_input(envelope)
    semantic = _critic_semantic_all_pass(model_input)
    semantic["quality_dimensions"][1] = {
        "dimension": "ARGUMENT_CHAIN",
        "score": 1,
        "passed": False,
        "evidence": ["存在链路缺失"],
        "required_action": "补齐工作包到方法的论证关系。",
    }
    errors = semantic_model_reference_errors(
        "P-ARGUMENT-ARCHITECTURE-CRITIC", envelope, semantic
    )
    assert any("requires at least one issue explicitly tagged with that dimension" in x for x in errors)


def test_critic_missing_review_unit_is_rejected():
    envelope = PACK.replay_input("P-ARGUMENT-ARCHITECTURE-CRITIC")
    model_input = build_argument_architecture_critic_model_input(envelope)
    semantic = _critic_semantic_all_pass(model_input)
    semantic["reviewed_unit_keys"] = semantic["reviewed_unit_keys"][:-1]
    errors = semantic_model_reference_errors(
        "P-ARGUMENT-ARCHITECTURE-CRITIC", envelope, semantic
    )
    assert any("did not explicitly cover review unit" in x for x in errors)


def test_exact_evidence_binding_survives_shared_source_id_roundtrip():
    producer_envelope = copy.deepcopy(PACK.replay_input("P-ARGUMENT-ARCHITECTURE"))
    producer_envelope["payload"]["confirmed_facts"] = [
        _claim("E1", "证据卡一", source_id="src-same", quoted_text="同一来源中的证据片段一"),
        _claim("E2", "证据卡二", source_id="src-same", quoted_text="同一来源中的证据片段二"),
    ]
    semantic = _semantic_argument_output(producer_envelope)

    def bind_e2(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "evidence_ids":
                    node[key] = ["E2"]
                else:
                    bind_e2(value)
        elif isinstance(node, list):
            for value in node:
                bind_e2(value)

    bind_e2(semantic)
    canonical = expand_argument_architecture_model_output(producer_envelope, semantic)
    critic_envelope = copy.deepcopy(PACK.replay_input("P-ARGUMENT-ARCHITECTURE-CRITIC"))
    critic_envelope["payload"]["architecture_candidate"] = canonical["result"]
    critic_envelope["payload"]["confirmed_facts"] = copy.deepcopy(
        producer_envelope["payload"]["confirmed_facts"]
    )
    critic_input = build_argument_architecture_critic_model_input(critic_envelope)
    rendered = json.dumps(critic_input["candidate"], ensure_ascii=False)
    assert '"E2"' in rendered
    assert '"E1"' not in rendered
    assert critic_input["candidate"]["central_proposition"]["evidence_ids"] == ["E2"]
    assert critic_input["candidate"]["research_threads"][0]["gap"]["evidence_ids"] == ["E2"]



def test_foundation_unknown_status_with_qualified_source_cannot_pass():
    envelope = _argument_envelope_with_evidence()
    envelope["payload"]["confirmed_facts"][0]["knowledge_status"] = "UNKNOWN"
    semantic = _semantic_argument_output(envelope)
    canonical = expand_argument_architecture_model_output(envelope, semantic)
    assert canonical["status"] == "REVISE"
    foundation_nodes = [
        node
        for node in canonical["result"]["argument_architecture"]["nodes"]
        if node.get("node_type") == "TEAM_EVIDENCE"
    ]
    assert foundation_nodes
    assert all(node["status"] == "UNKNOWN" for node in foundation_nodes)


def test_evidence_status_and_source_cannot_be_spliced_across_records():
    envelope = _argument_envelope_with_evidence()
    supported_without_source = _claim("E1", "可信状态但无来源")
    supported_without_source["source_refs"] = []
    unknown_with_source = _claim("E2", "有来源但状态未知", source_id="src-unknown")
    unknown_with_source["knowledge_status"] = "UNKNOWN"
    envelope["payload"]["confirmed_facts"] = [
        supported_without_source,
        unknown_with_source,
    ]
    semantic = _semantic_argument_output(envelope)
    semantic["research_threads"][0]["foundation"][0]["evidence_ids"] = ["E1", "E2"]
    canonical = expand_argument_architecture_model_output(envelope, semantic)
    assert canonical["status"] == "REVISE"
    assert any(
        gap["required_node_type"] == "TEAM_EVIDENCE"
        for gap in canonical["result"]["evidence_gap_report"]
    )


def test_duplicate_statement_keeps_distinct_evidence_identity():
    envelope = _argument_envelope_with_evidence()
    envelope["payload"]["confirmed_facts"] = [
        _claim("E1", "相同证据文本", source_id="src-one"),
        _claim("E2", "相同证据文本", source_id="src-two"),
    ]
    cards = build_argument_architecture_model_input(envelope)["evidence_cards"]
    assert [card["evidence_id"] for card in cards] == ["E1", "E2"]


def test_foundation_without_support_link_cannot_pass_producer_readiness():
    envelope = _argument_envelope_with_evidence()
    semantic = _semantic_argument_output(envelope)
    semantic["research_threads"][0]["foundation"][0]["supports"] = []
    canonical = expand_argument_architecture_model_output(envelope, semantic)
    assert canonical["status"] == "REVISE"
    assert canonical["result"]["readiness"]["ready"] is False
    assert any(
        gap.get("semantic_component") == "FOUNDATION"
        and gap.get("required_node_type") == "TEAM_EVIDENCE"
        and gap.get("suggested_route") == "ORIGINAL_PRODUCER"
        for gap in canonical["result"]["evidence_gap_report"]
    )


def test_central_proposition_without_supported_evidence_cannot_pass():
    envelope = _argument_envelope_with_evidence()
    semantic = _semantic_argument_output(envelope)
    semantic["central_proposition"]["evidence_ids"] = []
    canonical = expand_argument_architecture_model_output(envelope, semantic)
    assert canonical["status"] == "REVISE"
    assert canonical["result"]["readiness"]["ready"] is False
    assert any(
        "中心研究命题缺少" in gap["reason"]
        for gap in canonical["result"]["evidence_gap_report"]
    )


def test_blocking_user_question_and_cannot_proceed_are_mutually_exclusive_authoritative_state():
    envelope = _argument_envelope_with_evidence()
    semantic = _semantic_argument_output(envelope)
    semantic["cannot_proceed_reason"] = "缺少一项当前无法由现有材料确定的信息。"
    semantic["user_questions"] = [
        {
            "target_area": "FOUNDATION_EVIDENCE",
            "question_type": "MISSING_INFORMATION",
            "question": "请补充该研究基础对应的可核验材料。",
            "reason": "用户可以提供该信息，因此应进入人工补充而不是终止型阻塞。",
            "answer_shape": "STRING",
            "allowed_values": [],
            "blocking": True,
            "priority": "P0",
        }
    ]
    with pytest.raises(ValueError, match="invalid Argument authoritative state"):
        expand_argument_architecture_model_output(envelope, semantic)


def test_missing_foundation_evidence_cannot_pass():
    envelope = _argument_envelope_with_evidence()
    semantic = _semantic_argument_output(envelope)
    semantic["research_threads"][0]["foundation"][0]["evidence_ids"] = []
    canonical = expand_argument_architecture_model_output(envelope, semantic)
    assert canonical["status"] == "REVISE"
    assert canonical["result"]["readiness"]["ready"] is False
    assert any(
        gap["required_node_type"] == "TEAM_EVIDENCE"
        for gap in canonical["result"]["evidence_gap_report"]
    )


def test_user_input_resolution_without_blocking_question_is_rejected():
    envelope = PACK.replay_input("P-ARGUMENT-ARCHITECTURE-CRITIC")
    model_input = build_argument_architecture_critic_model_input(envelope)
    semantic = _critic_semantic_all_pass(model_input)
    semantic["quality_dimensions"][5] = {
        "dimension": "FEASIBILITY_FOUNDATION",
        "score": 1,
        "passed": False,
        "evidence": ["基础材料缺失"],
        "required_action": "由用户补充基础材料。",
    }
    semantic["issues"] = [
        {
            "code": "FOUNDATION_EVIDENCE_MISSING",
            "dimension": "FEASIBILITY_FOUNDATION",
            "severity": "P1",
            "target": {
                "component": "FOUNDATION",
                "thread_index": 0,
                "item_index": 0,
            },
            "description": "缺少研究基础材料。",
            "evidence_ids": [],
            "repair_instruction": "补充能够证明已有能力的材料。",
            "resolution": "USER_INPUT",
        }
    ]
    semantic["user_questions"] = []
    errors = semantic_model_reference_errors(
        "P-ARGUMENT-ARCHITECTURE-CRITIC", envelope, semantic
    )
    assert any("USER_INPUT resolution requires" in x for x in errors)


def test_blocking_evidence_gap_without_question_does_not_create_need_user_input_status():
    envelope = _argument_envelope_with_evidence()
    semantic = _semantic_argument_output(envelope)
    semantic["evidence_gaps"] = [
        {
            "kind": "FOUNDATION",
            "thread_index": 0,
            "reason": "仍需补充另一份可核验材料。",
            "blocking": True,
            "suggested_question": "补充材料。",
        }
    ]
    semantic["user_questions"] = []
    canonical = expand_argument_architecture_model_output(envelope, semantic)
    assert canonical["status"] == "REVISE"
    assert canonical["user_questions"] == []



def test_producer_schema_rejects_terminal_reason_with_blocking_question():
    envelope = _argument_envelope_with_evidence()
    semantic = _semantic_argument_output(envelope)
    semantic["cannot_proceed_reason"] = "当前无法继续。"
    semantic["user_questions"] = [
        {
            "target_area": "FOUNDATION_EVIDENCE",
            "question_type": "MISSING_INFORMATION",
            "question": "请补充基础材料。",
            "reason": "该信息可由用户提供。",
            "answer_shape": "STRING",
            "allowed_values": [],
            "blocking": True,
            "priority": "P0",
        }
    ]
    assert PACK.validate_model("P-ARGUMENT-ARCHITECTURE", "output", semantic)


def test_critic_schema_exposes_dimension_code_mapping():
    envelope = PACK.replay_input("P-ARGUMENT-ARCHITECTURE-CRITIC")
    model_input = build_argument_architecture_critic_model_input(envelope)
    semantic = _critic_semantic_all_pass(model_input)
    method_key = next(
        unit["unit_key"]
        for unit in model_input["candidate"]["review_units"]
        if unit["component"] in {"FORMAL_MODEL", "ALGORITHM", "ANALYTICAL_METHOD", "ENGINEERING_METHOD"}
    )
    semantic["issues"] = [
        {
            "code": "FOUNDATION_EVIDENCE_MISSING",
            "dimension": "METHOD_SUBSTANCE",
            "severity": "P1",
            "target": {
                "component": "METHOD",
                "thread_index": 0,
                "item_index": 0,
                "review_unit_key": method_key,
            },
            "description": "错误的静态组合。",
            "evidence_ids": [],
            "repair_instruction": "修复。",
            "resolution": "LOCAL_EDIT",
        }
    ]
    assert PACK.validate_model("P-ARGUMENT-ARCHITECTURE-CRITIC", "output", semantic)


def test_critic_schema_requires_review_key_for_precise_target():
    envelope = PACK.replay_input("P-ARGUMENT-ARCHITECTURE-CRITIC")
    model_input = build_argument_architecture_critic_model_input(envelope)
    semantic = _critic_semantic_all_pass(model_input)
    semantic["issues"] = [
        {
            "code": "ARGUMENT_METHOD_SUBSTANCE_WEAK",
            "dimension": "METHOD_SUBSTANCE",
            "severity": "P1",
            "target": {
                "component": "METHOD",
                "thread_index": 0,
                "item_index": 0,
                "review_unit_key": None,
            },
            "description": "方法机制不足。",
            "evidence_ids": [],
            "repair_instruction": "补充机制。",
            "resolution": "LOCAL_EDIT",
        }
    ]
    assert PACK.validate_model("P-ARGUMENT-ARCHITECTURE-CRITIC", "output", semantic)


def test_runtime_preserves_thread_assumptions_without_inferred_objective_edge():
    producer_envelope = _argument_envelope_with_evidence()
    semantic = _semantic_argument_output(producer_envelope)
    canonical = expand_argument_architecture_model_output(producer_envelope, semantic)
    graph = canonical["result"]["argument_architecture"]

    proposition_id = graph["central_proposition"]["node_id"]
    work_package_ids = {
        node["node_id"]
        for node in graph["nodes"]
        if node.get("node_type") == "WORK_PACKAGE"
    }
    assert not any(
        edge.get("source_id") == proposition_id
        and edge.get("relation") == "SUPPORTED_BY"
        and edge.get("target_id") in work_package_ids
        for edge in graph["edges"]
    )

    objective_ids = {
        node["node_id"]
        for node in graph["nodes"]
        if node.get("node_type") == "OBJECTIVE"
    }
    thread_assumption_ids = {
        node["node_id"]
        for node in graph["nodes"]
        if node.get("node_type") == "ASSUMPTION"
        and node.get("statement") in semantic["research_threads"][0]["thread_assumptions"]
    }
    assert not any(
        edge.get("source_id") in objective_ids
        and edge.get("relation") == "ASSUMES"
        and edge.get("target_id") in thread_assumption_ids
        for edge in graph["edges"]
    )

    critic_envelope = copy.deepcopy(PACK.replay_input("P-ARGUMENT-ARCHITECTURE-CRITIC"))
    critic_envelope["payload"]["architecture_candidate"] = canonical["result"]
    critic_envelope["payload"]["confirmed_facts"] = copy.deepcopy(
        producer_envelope["payload"]["confirmed_facts"]
    )
    critic_input = build_argument_architecture_critic_model_input(critic_envelope)
    assert critic_input["candidate"]["research_threads"][0]["thread_assumptions"] == (
        semantic["research_threads"][0]["thread_assumptions"]
    )


def test_deterministic_critic_failure_creates_routable_machine_finding():
    producer_envelope = _argument_envelope_with_evidence()
    semantic = _semantic_argument_output(producer_envelope)
    semantic["research_threads"][0]["foundation"][0]["supports"] = []
    canonical = expand_argument_architecture_model_output(producer_envelope, semantic)

    critic_envelope = copy.deepcopy(PACK.replay_input("P-ARGUMENT-ARCHITECTURE-CRITIC"))
    critic_envelope["payload"]["architecture_candidate"] = canonical["result"]
    critic_envelope["payload"]["confirmed_facts"] = copy.deepcopy(
        producer_envelope["payload"]["confirmed_facts"]
    )
    model_input = build_argument_architecture_critic_model_input(critic_envelope)
    critic_semantic = _critic_semantic_all_pass(model_input)
    critic_output = expand_argument_architecture_critic_model_output(
        critic_envelope, critic_semantic
    )

    assert PACK.validate(
        "P-ARGUMENT-ARCHITECTURE-CRITIC", "output", critic_output
    ) == []
    assert critic_output["status"] == "REVISE"
    assert critic_output["findings"]
    machine_findings = [
        finding
        for finding in critic_output["findings"]
        if str(finding.get("finding_instance_id") or "").startswith("F-ARG-DETERMINISTIC-")
    ]
    assert machine_findings
    assert any(
        finding["code"] == "RESEARCH_DESIGN_INCOMPLETE"
        and finding["suggested_route"] == "ORIGINAL_PRODUCER"
        and finding["repairable"] is False
        for finding in machine_findings
    )


def _argument_repair_envelope_for_node() -> dict:
    producer_envelope = _argument_envelope_with_evidence()
    canonical = expand_argument_architecture_model_output(
        producer_envelope, _semantic_argument_output(producer_envelope)
    )
    envelope = copy.deepcopy(PACK.replay_input("P-TARGETED-REPAIR"))
    envelope["payload"]["original_object"]["object_type"] = "ARGUMENT_ARCHITECTURE"
    envelope["payload"]["original_object"]["content"] = canonical["result"]
    target = "/authored_state/research_threads/0/work_packages/0/methods/0"
    envelope["payload"]["findings_to_repair"][0]["target_path_or_span"] = target
    envelope["payload"]["allowed_paths"] = ["/content" + target]
    envelope["payload"]["protected_paths"] = []
    envelope["payload"]["protected_hashes"] = []
    return envelope


def test_argument_repair_exposes_only_semantic_leaf_fields():
    envelope = _argument_repair_envelope_for_node()
    model_input = build_targeted_repair_model_input(envelope)
    assert [target["path"] for target in model_input["repair_targets"]] == [
        "/authored_state/research_threads/0/work_packages/0/methods/0/statement"
    ]


def test_argument_repair_cannot_change_machine_managed_node_id():
    envelope = _argument_repair_envelope_for_node()
    semantic = {
        "decision": "APPLY",
        "changes": [
            {
                "path": "/result/argument_architecture/nodes/0/node_id",
                "value": "arg-hijacked-999",
            }
        ],
        "escalation_reason": None,
    }
    errors = targeted_repair_semantic_errors(envelope, semantic)
    assert any("outside authorized repair targets" in error for error in errors)


def test_argument_repair_cannot_replace_node_object_to_change_machine_fields():
    envelope = _argument_repair_envelope_for_node()
    semantic = {
        "decision": "APPLY",
        "changes": [
            {
                "path": "/result/argument_architecture/nodes/0",
                "value": {
                    "node_id": "arg-hijacked-999",
                    "node_type": "FORMAL_MODEL",
                    "statement": "更新后的模型",
                    "status": "SUPPORTED",
                    "source_refs": [],
                },
            }
        ],
        "escalation_reason": None,
    }
    errors = targeted_repair_semantic_errors(envelope, semantic)
    assert any("outside authorized repair targets" in error for error in errors)


def test_argument_repair_can_change_authorized_statement():
    envelope = _argument_repair_envelope_for_node()
    semantic = {
        "decision": "APPLY",
        "changes": [
            {
                "path": "/authored_state/research_threads/0/work_packages/0/methods/0/statement",
                "value": "补充输入、核心机制与可验证输出后的模型描述",
            }
        ],
        "escalation_reason": None,
    }
    assert targeted_repair_semantic_errors(envelope, semantic) == []


def test_existing_entity_reference_list_growth_is_local_repair():
    envelope = copy.deepcopy(PACK.replay_input("P-TARGETED-REPAIR"))
    envelope["payload"]["original_object"]["content"] = {
        "status": "REVISE",
        "result": {
            "argument_architecture": {
                "research_questions": [],
                "nodes": [
                    {
                        "node_id": "M-1",
                        "node_type": "ALGORITHM",
                        "statement": "现有算法",
                        "status": "PLANNED",
                    }
                ],
            },
            "research_design_matrix": [{"method_ids": []}],
        },
        "findings": [],
        "user_questions": [],
    }
    envelope["payload"]["findings_to_repair"][0]["target_path_or_span"] = (
        "/result/research_design_matrix/0/method_ids"
    )
    envelope["payload"]["allowed_paths"] = [
        "/content/result/research_design_matrix/0/method_ids"
    ]
    envelope["payload"]["protected_paths"] = []
    envelope["payload"]["protected_hashes"] = []
    semantic = {
        "decision": "APPLY",
        "changes": [
            {
                "path": "/result/research_design_matrix/0/method_ids",
                "value": ["M-1"],
            }
        ],
        "escalation_reason": None,
    }
    assert targeted_repair_semantic_errors(envelope, semantic) == []


def test_inventing_new_method_reference_requires_regeneration():
    envelope = copy.deepcopy(PACK.replay_input("P-TARGETED-REPAIR"))
    envelope["payload"]["original_object"]["content"] = {
        "status": "REVISE",
        "result": {
            "argument_architecture": {
                "research_questions": [],
                "nodes": [
                    {
                        "node_id": "M-1",
                        "node_type": "FORMAL_MODEL",
                        "statement": "现有模型",
                        "status": "PLANNED",
                    }
                ],
            },
            "research_design_matrix": [{"method_ids": []}],
        },
        "findings": [],
        "user_questions": [],
    }
    envelope["payload"]["findings_to_repair"][0]["target_path_or_span"] = (
        "/result/research_design_matrix/0/method_ids"
    )
    envelope["payload"]["allowed_paths"] = [
        "/content/result/research_design_matrix/0/method_ids"
    ]
    envelope["payload"]["protected_paths"] = []
    envelope["payload"]["protected_hashes"] = []
    semantic = {
        "decision": "APPLY",
        "changes": [
            {
                "path": "/result/research_design_matrix/0/method_ids",
                "value": ["M-NEW"],
            }
        ],
        "escalation_reason": None,
    }
    errors = targeted_repair_semantic_errors(envelope, semantic)
    assert any("creating or inventing" in x for x in errors)


def test_runtime_does_not_cartesian_connect_evaluations_innovations_or_foundation():
    envelope = _argument_envelope_with_evidence()
    semantic = _semantic_argument_output(envelope)
    method = semantic["research_threads"][0]["work_packages"][0]["methods"][0]
    method["evaluations"].append(
        {
            "statement": "第二个独立验证方案。",
            "evidence_ids": ["E1"],
            "baselines": [{"statement": "第二基线。", "evidence_ids": ["E1"]}],
            "ablations": [],
            "success_criteria": ["第二判据。"],
        }
    )
    semantic["research_threads"][0]["innovations"] = [
        {
            "statement": "创新一。",
            "evidence_ids": ["E1"],
            "contribution": "贡献一。",
            "closest_prior_work": [{"statement": "最近工作一。", "evidence_ids": ["E1"]}],
            "evaluation_refs": [
                {"work_package_index": 0, "method_index": 0, "evaluation_index": 0}
            ],
        },
        {
            "statement": "创新二。",
            "evidence_ids": ["E1"],
            "contribution": "贡献二。",
            "closest_prior_work": [{"statement": "最近工作二。", "evidence_ids": ["E1"]}],
            "evaluation_refs": [
                {"work_package_index": 0, "method_index": 0, "evaluation_index": 1}
            ],
        },
    ]
    semantic["research_threads"][0]["foundation"][0]["supports"] = [
        {"work_package_index": 0, "method_index": 0}
    ]
    canonical = expand_argument_architecture_model_output(envelope, semantic)
    edges = canonical["result"]["argument_architecture"]["edges"]
    evidences = [e for e in edges if e["relation"] == "EVIDENCES"]
    supports = [e for e in edges if e["relation"] == "SUPPORTS"]
    assert len(evidences) == 2
    assert len({(e["source_id"], e["target_id"]) for e in evidences}) == 2
    assert len(supports) == 1


def test_revision_card_preserves_required_action_without_json_pointer():
    envelope = copy.deepcopy(PACK.replay_input("P-ARGUMENT-ARCHITECTURE"))
    envelope["payload"]["revision_findings"] = [
        {
            "code": "ARGUMENT_METHOD_SUBSTANCE_WEAK",
            "dimension": "METHOD_SUBSTANCE",
            "severity": "P1",
            "description": "方法只有名称，没有说明机制。",
            "target_path_or_span": "/result/argument_architecture/nodes/3",
            "repair_instruction": "补充方法的输入、核心机制和可验证输出。",
            "semantic_component": "METHOD",
            "semantic_thread": 0,
            "semantic_review_unit_key": "FORMAL_MODEL:1",
            "evidence_refs": ["E-OLD"],
        }
    ]
    model_input = build_argument_architecture_model_input(envelope)
    rendered = json.dumps(model_input["revision_issues"], ensure_ascii=False)
    assert "/result/" not in rendered
    assert model_input["revision_issues"][0]["required_action"].startswith("补充方法")
    assert model_input["revision_issues"][0]["component"] == "METHOD"
    assert model_input["revision_issues"][0]["thread"] == 0
    assert model_input["revision_issues"][0]["review_unit_key"] == "FORMAL_MODEL:1"
    assert model_input["revision_issues"][0]["code"] == "ARGUMENT_METHOD_SUBSTANCE_WEAK"
    assert model_input["revision_issues"][0]["blocking"] is True
    assert model_input["revision_issues"][0]["route"] == "ORIGINAL_PRODUCER"


def test_argument_human_resolutions_preserve_question_identity_and_typed_answer():
    envelope = copy.deepcopy(PACK.replay_input("P-ARGUMENT-ARCHITECTURE"))
    envelope["payload"]["human_resolutions"] = [
        {
            "question_id": "UQ-001",
            "question": "是否允许使用公开资料补充证据？",
            "target_paths": ["/payload/shared_target"],
            "answer": True,
        },
        {
            "question_id": "UQ-002",
            "question": "是否接受当前研究范围？",
            "target_paths": ["/payload/shared_target"],
            "answer": False,
        },
    ]

    model_input = build_argument_architecture_model_input(envelope)

    assert model_input["human_resolutions"] == [
        {
            "target": "/payload/shared_target",
            "answer": True,
            "question_id": "UQ-001",
            "question": "是否允许使用公开资料补充证据？",
        },
        {
            "target": "/payload/shared_target",
            "answer": False,
            "question_id": "UQ-002",
            "question": "是否接受当前研究范围？",
        },
    ]
    assert PACK.validate_model(
        "P-ARGUMENT-ARCHITECTURE", "input", model_input
    ) == []


def test_compact_design_seed_preserves_known_business_relations_without_ids():
    envelope = copy.deepcopy(PACK.replay_input("P-ARGUMENT-ARCHITECTURE"))
    base = envelope["payload"]["project_subgraph"]
    base["items"] = [
        {
            "item_id": "OBJ-1",
            "item_type": "OBJECTIVE",
            "domain": "OBJECTIVES",
            "content": {"statement": "目标A"},
            "knowledge_status": "CONFIRMED",
            "owner_ref": None,
            "source_refs": [],
            "security_level": "INTERNAL",
            "locked": True,
            "confidence": "HIGH",
            "item_hash": "c" * 64,
        },
        {
            "item_id": "WP-1",
            "item_type": "WORK_PACKAGE",
            "domain": "RESEARCH_DESIGN",
            "content": {"statement": "工作包A"},
            "knowledge_status": "CONFIRMED",
            "owner_ref": None,
            "source_refs": [],
            "security_level": "INTERNAL",
            "locked": True,
            "confidence": "HIGH",
            "item_hash": "d" * 64,
        },
        {
            "item_id": "M-1",
            "item_type": "METHOD",
            "domain": "METHODS",
            "content": {"statement": "方法A"},
            "knowledge_status": "CONFIRMED",
            "owner_ref": None,
            "source_refs": [],
            "security_level": "INTERNAL",
            "locked": True,
            "confidence": "HIGH",
            "item_hash": "e" * 64,
        },
        {
            "item_id": "EXP-1",
            "item_type": "EXPERIMENT",
            "domain": "EVALUATION",
            "content": {"statement": "实验A"},
            "knowledge_status": "CONFIRMED",
            "owner_ref": None,
            "source_refs": [],
            "security_level": "INTERNAL",
            "locked": True,
            "confidence": "HIGH",
            "item_hash": "f" * 64,
        },
    ]
    def rel(rid, sid, st, rt, tid, tt):
        return {
            "relation_id": rid,
            "source_item_id": sid,
            "source_item_type": st,
            "relation_type": rt,
            "target_item_id": tid,
            "target_item_type": tt,
            "status": "CONFIRMED",
            "confidence": "HIGH",
            "source_refs": [],
            "security_level": "INTERNAL",
            "relation_hash": "a" * 64,
        }
    base["relations"] = [
        rel("R1", "OBJ-1", "OBJECTIVE", "DECOMPOSES_TO", "WP-1", "WORK_PACKAGE"),
        rel("R2", "WP-1", "WORK_PACKAGE", "USES", "M-1", "METHOD"),
        rel("R3", "M-1", "METHOD", "VALIDATED_BY", "EXP-1", "EXPERIMENT"),
    ]
    base["item_ids"] = [x["item_id"] for x in base["items"]]
    base["relation_ids"] = [x["relation_id"] for x in base["relations"]]
    model_input = build_argument_architecture_model_input(envelope)
    relations = model_input["design_seed"]["existing_relations"]
    assert [r["relation"] for r in relations] == ["DECOMPOSES_TO", "USES", "VALIDATED_BY"]
    rendered = json.dumps(relations, ensure_ascii=False)
    for forbidden in ("OBJ-1", "WP-1", "M-1", "EXP-1", "relation_hash"):
        assert forbidden not in rendered


def test_critic_projection_preserves_nested_method_evaluation_and_prior_work_relations():
    producer_envelope = _argument_envelope_with_evidence()
    semantic = _semantic_argument_output(producer_envelope)
    canonical = expand_argument_architecture_model_output(producer_envelope, semantic)
    critic_envelope = copy.deepcopy(PACK.replay_input("P-ARGUMENT-ARCHITECTURE-CRITIC"))
    critic_envelope["payload"]["architecture_candidate"] = canonical["result"]
    critic_envelope["payload"]["confirmed_facts"] = copy.deepcopy(
        producer_envelope["payload"]["confirmed_facts"]
    )
    model_input = build_argument_architecture_critic_model_input(critic_envelope)
    thread = model_input["candidate"]["research_threads"][0]
    assert thread["work_packages"][0]["methods"][0]["evaluations"][0]["statement"]
    assert thread["innovations"][0]["closest_prior_work"][0]["statement"]
    assert thread["innovations"][0]["evaluation_refs"] == [
        {"work_package_index": 0, "method_index": 0, "evaluation_index": 0}
    ]
    assert "methods" not in thread
    assert "evaluations" not in thread
    assert "closest_prior_work" not in thread


def test_argument_semantic_schema_contains_p1_research_objects():
    schema = PACK.model_schema("P-ARGUMENT-ARCHITECTURE", "output")
    rendered = json.dumps(schema, ensure_ascii=False)
    for required_semantic in (
        "limitation_mechanism",
        "thread_assumptions",
        "method_type",
        "theoretical_properties",
        "baselines",
        "ablations",
        "success_criteria",
        "contribution",
        "evaluation_refs",
        "supports",
    ):
        assert required_semantic in rendered


def test_need_user_input_without_blocking_question_fails_global_gate_contract():
    output = {
        "status": "NEED_USER_INPUT",
        "findings": [],
        "user_questions": [],
    }
    errors = PromptExecutor._human_gate_contract_errors(output)
    assert any("NEED_USER_INPUT requires" in x for x in errors)


class _NoopSemanticE2EDB:
    """In-memory persistence boundary for semantic executor tests."""

    def __init__(self) -> None:
        self.events: list[tuple] = []

    def fetchone(self, query, params=()):
        if "COALESCE(MAX(version)" in query:
            return {"v": 0}
        if "SELECT config_json FROM projects" in query:
            return None
        return None

    def execute(self, query, params=()):
        self.events.append(("execute", str(query)[:80]))

    def audit(self, event_type, **kwargs):
        self.events.append(("audit", event_type))


class _LiveEquivalentSemanticGateway:
    """Fake gateway: exercises the LIVE semantic boundary without any I/O."""

    def __init__(self, producer_output: dict, critic_output_builder=None) -> None:
        self.settings = SimpleNamespace(runtime_mode="LIVE")
        self.producer_output = copy.deepcopy(producer_output)
        self.critic_output_builder = critic_output_builder
        self.calls: list[dict] = []

    async def invoke(
        self,
        route,
        prompt_id,
        system_prompt,
        envelope,
        output_schema,
        *,
        direct_tool_arguments=False,
    ):
        self.calls.append(
            {
                "prompt_id": prompt_id,
                "direct_tool_arguments": direct_tool_arguments,
                "envelope": copy.deepcopy(envelope),
            }
        )
        if prompt_id == "P-ARGUMENT-ARCHITECTURE":
            output = copy.deepcopy(self.producer_output)
        elif prompt_id == "P-ARGUMENT-ARCHITECTURE-CRITIC":
            if self.critic_output_builder is None:
                output = _critic_semantic_all_pass(envelope)
            else:
                output = self.critic_output_builder(envelope)
        else:  # pragma: no cover - protects the fake from accidental scope growth.
            raise AssertionError(f"unexpected fake-gateway prompt: {prompt_id}")
        return LLMResult(
            output=output,
            raw_text=json.dumps(output, ensure_ascii=False),
            model_id=route.model_id,
            endpoint_id=route.endpoint_id,
        )


def test_live_equivalent_fake_gateway_producer_to_critic_end_to_end():
    """Exercise the real executor semantic projection/expansion boundary with no provider."""

    async def scenario():
        producer_envelope = _argument_envelope_with_evidence()
        gateway = _LiveEquivalentSemanticGateway(
            _semantic_argument_output(producer_envelope)
        )
        executor = PromptExecutor(
            _NoopSemanticE2EDB(),
            PACK,
            SecurityRouter(PACK),
            gateway,
            quality_guard_enabled=False,
        )

        producer_run = await executor.execute(
            "P-ARGUMENT-ARCHITECTURE",
            producer_envelope,
            project_id="project-semantic-e2e",
        )
        assert producer_run["status"] == "PASS"
        produced = producer_run["output"]["result"]
        assert produced["authored_evidence_bindings"]
        assert all(
            edge["relation"] != "EVIDENCES"
            or edge["source_id"].startswith("arg-eval-")
            for edge in produced["argument_architecture"]["edges"]
        )

        critic_envelope = copy.deepcopy(
            PACK.replay_input("P-ARGUMENT-ARCHITECTURE-CRITIC")
        )
        critic_envelope["payload"]["architecture_candidate"] = copy.deepcopy(produced)
        critic_envelope["payload"]["confirmed_facts"] = copy.deepcopy(
            producer_envelope["payload"]["confirmed_facts"]
        )
        expected_review_units = build_argument_architecture_critic_model_input(
            critic_envelope
        )["candidate"]["review_units"]

        critic_run = await executor.execute(
            "P-ARGUMENT-ARCHITECTURE-CRITIC",
            critic_envelope,
            project_id="project-semantic-e2e",
        )
        assert critic_run["status"] == "PASS"
        critic_result = critic_run["output"]["result"]
        assert len(critic_result["checked_node_ids"]) == len(expected_review_units)
        assert all(item["complete"] for item in critic_result["chain_checks"])
        assert all(item["complete"] for item in critic_result["design_matrix_checks"])
        assert all(item["supported"] for item in critic_result["evidence_checks"])

        assert [call["prompt_id"] for call in gateway.calls] == [
            "P-ARGUMENT-ARCHITECTURE",
            "P-ARGUMENT-ARCHITECTURE-CRITIC",
        ]
        assert all(call["direct_tool_arguments"] is True for call in gateway.calls)

    # Run in an isolated local event loop so the test is deterministic even
    # when the outer test host already owns an event loop (e.g. notebooks).
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(lambda: asyncio.run(scenario())).result()

def test_unknown_semantic_slots_may_be_empty_but_cannot_pass():
    envelope = _argument_envelope_with_evidence()
    semantic = _semantic_argument_output(envelope)
    semantic["research_threads"] = []

    assert PACK.validate_model(
        "P-ARGUMENT-ARCHITECTURE", "output", semantic
    ) == []

    canonical = expand_argument_architecture_model_output(envelope, semantic)
    assert canonical["status"] == "REVISE"
    assert canonical["result"]["readiness"]["ready"] is False
    assert canonical["result"]["research_design_matrix"] == []
    assert canonical["result"]["evidence_gap_report"]
    assert PACK.validate(
        "P-ARGUMENT-ARCHITECTURE", "output", canonical
    ) == []


def test_empty_foundation_is_allowed_when_no_foundation_is_claimed():
    envelope = _argument_envelope_with_evidence()
    semantic = _semantic_argument_output(envelope)
    semantic["research_threads"][0]["foundation"] = []

    canonical = expand_argument_architecture_model_output(envelope, semantic)

    assert canonical["status"] == "PASS"
    assert canonical["result"]["readiness"]["ready"] is True
    assert canonical["result"]["research_design_matrix"][0][
        "foundation_evidence_ids"
    ] == []
    assert not any(
        item["required_node_type"] == "TEAM_EVIDENCE"
        for item in canonical["result"]["evidence_gap_report"]
    )
    assert PACK.validate(
        "P-ARGUMENT-ARCHITECTURE", "output", canonical
    ) == []


def test_limitation_mechanism_preserves_its_own_authored_evidence_binding():
    producer_envelope = _argument_envelope_with_evidence()
    producer_envelope["payload"]["confirmed_facts"].append(
        _claim(
            "E2",
            "另一条证据专门支持限制机制。",
            source_id="src-shared",
            quoted_text="限制机制的独立证据。",
        )
    )
    semantic = _semantic_argument_output(producer_envelope)
    semantic["research_threads"][0]["gap"]["evidence_ids"] = ["E1"]
    semantic["research_threads"][0]["gap"]["limitation_mechanism"][
        "evidence_ids"
    ] = ["E2"]

    canonical = expand_argument_architecture_model_output(
        producer_envelope, semantic
    )
    limitation = next(
        node
        for node in canonical["result"]["argument_architecture"]["nodes"]
        if node["node_type"] == "LIMITATION_MECHANISM"
    )
    binding = next(
        item
        for item in canonical["result"]["authored_evidence_bindings"]
        if item["node_id"] == limitation["node_id"]
    )
    assert binding["evidence_ids"] == ["E2"]

    critic_envelope = copy.deepcopy(
        PACK.replay_input("P-ARGUMENT-ARCHITECTURE-CRITIC")
    )
    critic_envelope["payload"]["architecture_candidate"] = canonical["result"]
    critic_envelope["payload"]["confirmed_facts"] = copy.deepcopy(
        producer_envelope["payload"]["confirmed_facts"]
    )
    critic_input = build_argument_architecture_critic_model_input(
        critic_envelope
    )
    projected = critic_input["candidate"]["research_threads"][0][
        "gap"
    ]["limitation_mechanism"]
    assert projected["evidence_ids"] == ["E2"]


def test_critic_issue_requires_precise_review_unit_and_matching_component():
    producer_envelope = _argument_envelope_with_evidence()
    canonical = expand_argument_architecture_model_output(
        producer_envelope, _semantic_argument_output(producer_envelope)
    )
    critic_envelope = copy.deepcopy(
        PACK.replay_input("P-ARGUMENT-ARCHITECTURE-CRITIC")
    )
    critic_envelope["payload"]["architecture_candidate"] = canonical["result"]
    critic_envelope["payload"]["confirmed_facts"] = copy.deepcopy(
        producer_envelope["payload"]["confirmed_facts"]
    )
    model_input = build_argument_architecture_critic_model_input(
        critic_envelope
    )
    output = _critic_semantic_all_pass(model_input)
    method_dimension = next(
        item
        for item in output["quality_dimensions"]
        if item["dimension"] == "METHOD_SUBSTANCE"
    )
    method_dimension.update(
        {
            "score": 2,
            "passed": False,
            "required_action": "补充方法机制和可验证输出。",
        }
    )
    output["issues"] = [
        {
            "code": "ARGUMENT_METHOD_SUBSTANCE_WEAK",
            "dimension": "METHOD_SUBSTANCE",
            "severity": "P1",
            "target": {
                "component": "METHOD",
                "thread_index": 0,
                "item_index": 0,
                "review_unit_key": None,
            },
            "description": "方法机制不足。",
            "evidence_ids": [],
            "repair_instruction": "补充方法机制。",
            "resolution": "LOCAL_EDIT",
        }
    ]
    errors = semantic_model_reference_errors(
        "P-ARGUMENT-ARCHITECTURE-CRITIC",
        critic_envelope,
        output,
    )
    assert any("requires a precise semantic review-unit target" in e for e in errors)

    gap_key = next(
        u["unit_key"]
        for u in model_input["candidate"]["review_units"]
        if u["component"] == "RESEARCH_GAP"
    )
    output["issues"][0]["target"]["review_unit_key"] = gap_key
    errors = semantic_model_reference_errors(
        "P-ARGUMENT-ARCHITECTURE-CRITIC",
        critic_envelope,
        output,
    )
    assert any("does not match review unit" in e for e in errors)

    method_key = next(
        u["unit_key"]
        for u in model_input["candidate"]["review_units"]
        if u["component"] in {
            "FORMAL_MODEL",
            "ALGORITHM",
            "ANALYTICAL_METHOD",
            "ENGINEERING_METHOD",
        }
    )
    output["issues"][0]["target"]["review_unit_key"] = method_key
    errors = semantic_model_reference_errors(
        "P-ARGUMENT-ARCHITECTURE-CRITIC",
        critic_envelope,
        output,
    )
    assert not any("/issues/0/target" in e for e in errors)


def test_issue_code_cannot_coexist_with_corresponding_dimension_marked_pass():
    producer_envelope = _argument_envelope_with_evidence()
    canonical = expand_argument_architecture_model_output(
        producer_envelope, _semantic_argument_output(producer_envelope)
    )
    critic_envelope = copy.deepcopy(
        PACK.replay_input("P-ARGUMENT-ARCHITECTURE-CRITIC")
    )
    critic_envelope["payload"]["architecture_candidate"] = canonical["result"]
    critic_envelope["payload"]["confirmed_facts"] = copy.deepcopy(
        producer_envelope["payload"]["confirmed_facts"]
    )
    model_input = build_argument_architecture_critic_model_input(
        critic_envelope
    )
    output = _critic_semantic_all_pass(model_input)
    method_key = next(
        u["unit_key"]
        for u in model_input["candidate"]["review_units"]
        if u["component"] == "FORMAL_MODEL"
    )
    output["issues"] = [
        {
            "code": "ARGUMENT_METHOD_SUBSTANCE_WEAK",
            "dimension": "METHOD_SUBSTANCE",
            "severity": "P1",
            "target": {
                "component": "METHOD",
                "thread_index": 0,
                "item_index": 0,
                "review_unit_key": method_key,
            },
            "description": "方法机制不足。",
            "evidence_ids": [],
            "repair_instruction": "补充方法机制。",
            "resolution": "LOCAL_EDIT",
        }
    ]
    errors = semantic_model_reference_errors(
        "P-ARGUMENT-ARCHITECTURE-CRITIC",
        critic_envelope,
        output,
    )
    assert any("must correspond to a failed quality dimension" in e for e in errors)


def test_evaluation_to_innovation_requires_explicit_semantic_relation():
    producer_envelope = _argument_envelope_with_evidence()
    semantic = _semantic_argument_output(producer_envelope)
    semantic["research_threads"][0]["innovations"][0][
        "evaluation_refs"
    ] = []
    candidate = expand_argument_architecture_model_output(
        producer_envelope, semantic
    )

    critic_envelope = copy.deepcopy(
        PACK.replay_input("P-ARGUMENT-ARCHITECTURE-CRITIC")
    )
    critic_envelope["payload"]["architecture_candidate"] = candidate["result"]
    critic_envelope["payload"]["confirmed_facts"] = copy.deepcopy(
        producer_envelope["payload"]["confirmed_facts"]
    )
    model_input = build_argument_architecture_critic_model_input(
        critic_envelope
    )
    critic_output = expand_argument_architecture_critic_model_output(
        critic_envelope,
        _critic_semantic_all_pass(model_input),
    )
    link_check = next(
        item
        for item in critic_output["result"]["chain_checks"]
        if item["chain_type"] == "EVALUATION_TO_INNOVATION"
    )
    assert link_check["complete"] is False
    assert critic_output["status"] == "REVISE"


def test_argument_stage_requirements_exclude_only_presentation_constraints():
    envelope = copy.deepcopy(PACK.replay_input("P-ARGUMENT-ARCHITECTURE"))
    envelope["payload"]["task_instruction"]["specific_requirements"] = [
        "必须研究动态重规划的稳定性与时效权衡。",
        "正文字体采用小四号。",
    ]
    envelope["payload"]["task_instruction"]["priority_order"] = [
        "优先保证研究问题与评价闭环。",
        "最终导出格式为 PDF。",
    ]
    model_input = build_argument_architecture_model_input(envelope)
    assert model_input["project_task"]["specific_requirements"] == [
        "必须研究动态重规划的稳定性与时效权衡。"
    ]
    assert model_input["project_task"]["priority_order"] == [
        "优先保证研究问题与评价闭环。"
    ]

def test_live_equivalent_fake_gateway_routes_local_and_structural_critic_findings():
    """Exercise LOCAL_EDIT and REGENERATE through the real semantic executor boundary."""

    async def run_case(resolution):
        producer_envelope = _argument_envelope_with_evidence()

        def critic_builder(model_envelope):
            output = _critic_semantic_all_pass(model_envelope)
            method_key = next(
                unit["unit_key"]
                for unit in model_envelope["candidate"]["review_units"]
                if unit["component"] in {
                    "FORMAL_MODEL",
                    "ALGORITHM",
                    "ANALYTICAL_METHOD",
                    "ENGINEERING_METHOD",
                }
            )
            method_dimension = next(
                item
                for item in output["quality_dimensions"]
                if item["dimension"] == "METHOD_SUBSTANCE"
            )
            method_dimension.update(
                {
                    "score": 2,
                    "passed": False,
                    "required_action": "补充方法的输入、核心机制和可验证输出。",
                }
            )
            output["issues"] = [
                {
                    "code": "ARGUMENT_METHOD_SUBSTANCE_WEAK",
            "dimension": "METHOD_SUBSTANCE",
                    "severity": "P1",
                    "target": {
                        "component": "METHOD",
                        "thread_index": 0,
                        "item_index": 0,
                        "review_unit_key": method_key,
                    },
                    "description": "方法机制没有形成可检验的输入—机制—输出闭环。",
                    "evidence_ids": [],
                    "repair_instruction": "补充方法机制和可验证输出。",
                    "resolution": resolution,
                }
            ]
            return output

        gateway = _LiveEquivalentSemanticGateway(
            _semantic_argument_output(producer_envelope),
            critic_output_builder=critic_builder,
        )
        executor = PromptExecutor(
            _NoopSemanticE2EDB(),
            PACK,
            SecurityRouter(PACK),
            gateway,
            quality_guard_enabled=False,
        )
        producer_run = await executor.execute(
            "P-ARGUMENT-ARCHITECTURE",
            producer_envelope,
            project_id=f"project-route-{resolution.lower()}",
        )
        assert producer_run["status"] == "PASS"

        critic_envelope = copy.deepcopy(
            PACK.replay_input("P-ARGUMENT-ARCHITECTURE-CRITIC")
        )
        critic_envelope["payload"]["architecture_candidate"] = copy.deepcopy(
            producer_run["output"]["result"]
        )
        critic_envelope["payload"]["confirmed_facts"] = copy.deepcopy(
            producer_envelope["payload"]["confirmed_facts"]
        )
        critic_run = await executor.execute(
            "P-ARGUMENT-ARCHITECTURE-CRITIC",
            critic_envelope,
            project_id=f"project-route-{resolution.lower()}",
        )
        assert critic_run["status"] == "REVISE"
        finding = critic_run["output"]["findings"][0]
        assert finding["semantic_component"] == "METHOD"
        assert finding["semantic_review_unit_key"]
        return finding, gateway.calls

    # asyncio.gather must be created inside the local event loop.
    async def combined():
        return await asyncio.gather(
            run_case("LOCAL_EDIT"),
            run_case("REGENERATE"),
        )

    with ThreadPoolExecutor(max_workers=1) as pool:
        (local_finding, local_calls), (
            structural_finding,
            structural_calls,
        ) = pool.submit(lambda: asyncio.run(combined())).result()

    assert local_finding["repairable"] is True
    assert local_finding["suggested_route"] == "ARGUMENT_ARCHITECTURE_AGENT"
    assert structural_finding["repairable"] is False
    assert structural_finding["suggested_route"] == "ORIGINAL_PRODUCER"
    assert [call["prompt_id"] for call in local_calls] == [
        "P-ARGUMENT-ARCHITECTURE",
        "P-ARGUMENT-ARCHITECTURE-CRITIC",
    ]
    assert [call["prompt_id"] for call in structural_calls] == [
        "P-ARGUMENT-ARCHITECTURE",
        "P-ARGUMENT-ARCHITECTURE-CRITIC",
    ]

def test_unrelated_evaluation_does_not_need_innovation_edge():
    producer_envelope = _argument_envelope_with_evidence()
    semantic = _semantic_argument_output(producer_envelope)
    method = semantic["research_threads"][0]["work_packages"][0]["methods"][0]
    method["evaluations"].append(
        {
            "statement": "独立验证算法在输入扰动下的鲁棒性，不用于证明创新点。",
            "evidence_ids": ["E1"],
            "baselines": [
                {
                    "statement": "采用固定窗口滚动优化作为鲁棒性比较基线。",
                    "evidence_ids": ["E1"],
                }
            ],
            "ablations": [],
            "success_criteria": ["扰动场景下保持可行且目标值退化受控。"],
        }
    )

    canonical = expand_argument_architecture_model_output(producer_envelope, semantic)
    assert canonical["status"] == "PASS"

    critic_envelope = copy.deepcopy(PACK.replay_input("P-ARGUMENT-ARCHITECTURE-CRITIC"))
    critic_envelope["payload"]["architecture_candidate"] = canonical["result"]
    critic_envelope["payload"]["confirmed_facts"] = copy.deepcopy(
        producer_envelope["payload"]["confirmed_facts"]
    )
    model_input = build_argument_architecture_critic_model_input(critic_envelope)
    critic_semantic = _critic_semantic_all_pass(model_input)
    critic_output = expand_argument_architecture_critic_model_output(
        critic_envelope, critic_semantic
    )

    eval_chain = next(
        check
        for check in critic_output["result"]["chain_checks"]
        if check["chain_type"] == "EVALUATION_TO_INNOVATION"
    )
    assert eval_chain["complete"] is True
    assert critic_output["status"] == "PASS"


def test_thread_assumption_binding_reads_current_node_statement():
    producer_envelope = _argument_envelope_with_evidence()
    semantic = _semantic_argument_output(producer_envelope)
    canonical = expand_argument_architecture_model_output(producer_envelope, semantic)

    bindings = canonical["result"]["authored_thread_assumptions"]
    assert bindings
    assumption_ids = bindings[0]["assumption_node_ids"]
    assert assumption_ids
    assert "assumptions" not in bindings[0]

    repaired_statement = "修复后：事件影响只需映射到当前滚动窗口内的有限业务对象。"
    authored_state = copy.deepcopy(canonical["result"]["authored_state"])
    authored_state["research_threads"][0]["thread_assumptions"][0] = repaired_statement
    canonical = project_argument_authoritative_state(producer_envelope, authored_state)

    critic_envelope = copy.deepcopy(PACK.replay_input("P-ARGUMENT-ARCHITECTURE-CRITIC"))
    critic_envelope["payload"]["architecture_candidate"] = canonical["result"]
    critic_envelope["payload"]["confirmed_facts"] = copy.deepcopy(
        producer_envelope["payload"]["confirmed_facts"]
    )
    critic_input = build_argument_architecture_critic_model_input(critic_envelope)

    assert critic_input["candidate"]["research_threads"][0]["thread_assumptions"] == [
        repaired_statement
    ]


def test_critic_evidence_check_uses_current_record_status():
    producer_envelope = _argument_envelope_with_evidence()
    semantic = _semantic_argument_output(producer_envelope)
    canonical = expand_argument_architecture_model_output(producer_envelope, semantic)
    assert canonical["status"] == "PASS"

    critic_envelope = copy.deepcopy(PACK.replay_input("P-ARGUMENT-ARCHITECTURE-CRITIC"))
    critic_envelope["payload"]["architecture_candidate"] = canonical["result"]
    critic_envelope["payload"]["confirmed_facts"] = copy.deepcopy(
        producer_envelope["payload"]["confirmed_facts"]
    )
    critic_envelope["payload"]["confirmed_facts"][0]["knowledge_status"] = "UNKNOWN"

    model_input = build_argument_architecture_critic_model_input(critic_envelope)
    assert model_input["evidence_cards"][0]["knowledge_status"] == "UNKNOWN"
    critic_semantic = _critic_semantic_all_pass(model_input)
    critic_output = expand_argument_architecture_critic_model_output(
        critic_envelope, critic_semantic
    )

    proposition_id = canonical["result"]["argument_architecture"]["central_proposition"]["node_id"]
    proposition_check = next(
        check
        for check in critic_output["result"]["evidence_checks"]
        if check["node_id"] == proposition_id
    )
    assert proposition_check["supported"] is False
    assert critic_output["status"] == "REVISE"
    assert any(
        finding["code"] == "ARGUMENT_EVIDENCE_UNSUPPORTED"
        and finding["suggested_route"] == "ORIGINAL_PRODUCER"
        for finding in critic_output["findings"]
    )


def test_deterministic_finding_overrides_same_local_model_finding():
    producer_envelope = _argument_envelope_with_evidence()
    semantic = _semantic_argument_output(producer_envelope)
    semantic["research_threads"][0]["foundation"][0]["supports"] = []
    canonical = expand_argument_architecture_model_output(producer_envelope, semantic)

    critic_envelope = copy.deepcopy(PACK.replay_input("P-ARGUMENT-ARCHITECTURE-CRITIC"))
    critic_envelope["payload"]["architecture_candidate"] = canonical["result"]
    critic_envelope["payload"]["confirmed_facts"] = copy.deepcopy(
        producer_envelope["payload"]["confirmed_facts"]
    )
    model_input = build_argument_architecture_critic_model_input(critic_envelope)
    foundation_key = next(
        unit["unit_key"]
        for unit in model_input["candidate"]["review_units"]
        if unit["component"] == "TEAM_EVIDENCE"
    )
    critic_semantic = _critic_semantic_all_pass(model_input)
    for dimension in critic_semantic["quality_dimensions"]:
        if dimension["dimension"] == "ARGUMENT_CHAIN":
            dimension.update(
                {
                    "score": 1,
                    "passed": False,
                    "evidence": ["研究基础支撑链缺失。"],
                    "required_action": "明确研究基础实际支撑的工作包或方法。",
                }
            )
    critic_semantic["issues"] = [
        {
            "code": "RESEARCH_DESIGN_INCOMPLETE",
            "dimension": "ARGUMENT_CHAIN",
            "severity": "P1",
            "target": {
                "component": "FOUNDATION",
                "thread_index": 0,
                "item_index": 0,
                "review_unit_key": foundation_key,
            },
            "description": "研究基础没有明确支撑任何现有工作包或方法。",
            "evidence_ids": [],
            "repair_instruction": "局部补写研究基础支撑关系。",
            "resolution": "LOCAL_EDIT",
        }
    ]

    critic_output = expand_argument_architecture_critic_model_output(
        critic_envelope, critic_semantic
    )
    matching = [
        finding
        for finding in critic_output["findings"]
        if finding.get("code") == "RESEARCH_DESIGN_INCOMPLETE"
        and finding.get("semantic_review_unit_key") == foundation_key
    ]
    assert len(matching) == 2
    assert {finding["defect_namespace"] for finding in matching} == {"MACHINE_DEFECT", "SEMANTIC_OBSERVATION"}
    assert {finding["suggested_route"] for finding in matching} == {"ORIGINAL_PRODUCER", "ARGUMENT_ARCHITECTURE_AGENT"}


def test_argument_repair_local_context_hides_machine_fields():
    envelope = _argument_repair_envelope_for_node()
    model_input = build_targeted_repair_model_input(envelope)
    rendered = json.dumps(
        model_input["repair_targets"][0]["local_context"],
        ensure_ascii=False,
    )
    for forbidden in (
        "node_id",
        "node_type",
        "status",
        "source_refs",
        "source_id",
        "target_id",
        "relation",
    ):
        assert forbidden not in rendered




def test_v5_invariant_blocking_deterministic_receipt_can_never_pass():
    producer_envelope = _argument_envelope_with_evidence()
    semantic = _semantic_argument_output(producer_envelope)
    semantic["research_threads"][0]["foundation"][0]["supports"] = []
    canonical = expand_argument_architecture_model_output(producer_envelope, semantic)

    critic_envelope = copy.deepcopy(PACK.replay_input("P-ARGUMENT-ARCHITECTURE-CRITIC"))
    critic_envelope["payload"]["architecture_candidate"] = canonical["result"]
    critic_envelope["payload"]["confirmed_facts"] = copy.deepcopy(
        producer_envelope["payload"]["confirmed_facts"]
    )
    model_input = build_argument_architecture_critic_model_input(critic_envelope)
    output = expand_argument_architecture_critic_model_output(
        critic_envelope, _critic_semantic_all_pass(model_input)
    )

    receipts = output["result"]["deterministic_receipts"]
    assert receipts
    assert all(receipt["blocking"] for receipt in receipts)
    assert output["status"] != "PASS"
    finding_keys = {finding["defect_key"] for finding in output["findings"]}
    assert {receipt["defect_key"] for receipt in receipts} <= finding_keys


def test_v5_mutation_cross_thread_relation_cannot_satisfy_thread_contract():
    producer_envelope = _argument_envelope_with_evidence()
    semantic = _semantic_argument_output(producer_envelope)
    second = copy.deepcopy(semantic["research_threads"][0])
    second["gap"]["statement"] = "第二研究线程具有独立研究差距。"
    second["question"]["statement"] = "第二线程如何验证线程隔离下的独立创新？"
    semantic["research_threads"].append(second)
    canonical = expand_argument_architecture_model_output(producer_envelope, semantic)
    assert canonical["status"] == "PASS"

    matrix = canonical["result"]["research_design_matrix"]
    graph = canonical["result"]["argument_architecture"]
    thread0_evaluation = matrix[0]["evaluation_ids"][0]
    thread1_evaluation = matrix[1]["evaluation_ids"][0]
    thread1_innovation = matrix[1]["innovation_ids"][0]
    graph["edges"] = [
        edge
        for edge in graph["edges"]
        if not (
            edge.get("relation") == "EVIDENCES"
            and edge.get("source_id") == thread1_evaluation
            and edge.get("target_id") == thread1_innovation
        )
    ]
    graph["edges"].append(
        {
            "edge_id": "arg-edge-cross-thread-mutation",
            "source_id": thread0_evaluation,
            "relation": "EVIDENCES",
            "target_id": thread1_innovation,
            "rationale": "故意构造的跨线程关系，不能冒充第二线程内部支撑。",
        }
    )

    critic_envelope = copy.deepcopy(PACK.replay_input("P-ARGUMENT-ARCHITECTURE-CRITIC"))
    critic_envelope["payload"]["architecture_candidate"] = canonical["result"]
    critic_envelope["payload"]["confirmed_facts"] = copy.deepcopy(
        producer_envelope["payload"]["confirmed_facts"]
    )
    model_input = build_argument_architecture_critic_model_input(critic_envelope)
    output = expand_argument_architecture_critic_model_output(
        critic_envelope, _critic_semantic_all_pass(model_input)
    )

    receipts = [
        receipt
        for receipt in output["result"]["deterministic_receipts"]
        if receipt["rule_id"] == "SC-ARGUMENT-DETERMINISTIC-CHAINS"
        and receipt["thread_index"] == 1
        and receipt["missing_relation"] == "EVIDENCES"
    ]
    # Derived graph cache has no authority in v8; Critic reprojects the valid
    # thread-local relation from authored_state before deterministic checks.
    assert receipts == []
    assert output["status"] == "PASS"


def test_v5_mutation_evidence_status_bypass_breaks_every_registered_requirement():
    from app.contracts import get_semantic_contract

    producer_envelope = _argument_envelope_with_evidence()
    semantic = _semantic_argument_output(producer_envelope)
    supported = expand_argument_architecture_model_output(producer_envelope, semantic)
    assert supported["status"] == "PASS"

    producer_unknown = copy.deepcopy(producer_envelope)
    producer_unknown["payload"]["confirmed_facts"][0]["knowledge_status"] = "UNKNOWN"
    producer_result = expand_argument_architecture_model_output(producer_unknown, semantic)

    registry = get_semantic_contract().rule("SC-ARGUMENT-EVIDENCE-REQUIREMENTS").config
    requirement_ids = {
        str(item.get("requirement_id"))
        for item in registry.get("requirements") or ()
    }
    assert "EVALUATION_BASELINE_SUPPORT" in requirement_ids
    assert producer_result["status"] != "PASS"
    assert len(producer_result["unresolved_items"]) == len(requirement_ids)

    critic_envelope = copy.deepcopy(PACK.replay_input("P-ARGUMENT-ARCHITECTURE-CRITIC"))
    critic_envelope["payload"]["architecture_candidate"] = supported["result"]
    critic_envelope["payload"]["confirmed_facts"] = copy.deepcopy(
        producer_unknown["payload"]["confirmed_facts"]
    )
    model_input = build_argument_architecture_critic_model_input(critic_envelope)
    critic_result = expand_argument_architecture_critic_model_output(
        critic_envelope, _critic_semantic_all_pass(model_input)
    )
    assert len(critic_result["result"]["evidence_checks"]) == len(requirement_ids)
    assert all(not check["supported"] for check in critic_result["result"]["evidence_checks"])
    evidence_receipts = [
        receipt
        for receipt in critic_result["result"]["deterministic_receipts"]
        if receipt["rule_id"] == "SC-ARGUMENT-EVIDENCE-REQUIREMENTS"
    ]
    assert len(evidence_receipts) == len(requirement_ids)
    assert critic_result["status"] == "REVISE"


def test_v5_mutation_deterministic_route_overrides_model_block_before_final_status():
    producer_envelope = _argument_envelope_with_evidence()
    semantic = _semantic_argument_output(producer_envelope)
    semantic["research_threads"][0]["foundation"][0]["supports"] = []
    canonical = expand_argument_architecture_model_output(producer_envelope, semantic)

    critic_envelope = copy.deepcopy(PACK.replay_input("P-ARGUMENT-ARCHITECTURE-CRITIC"))
    critic_envelope["payload"]["architecture_candidate"] = canonical["result"]
    critic_envelope["payload"]["confirmed_facts"] = copy.deepcopy(
        producer_envelope["payload"]["confirmed_facts"]
    )
    model_input = build_argument_architecture_critic_model_input(critic_envelope)
    foundation_key = next(
        unit["unit_key"]
        for unit in model_input["candidate"]["review_units"]
        if unit["component"] == "TEAM_EVIDENCE"
    )
    critic_semantic = _critic_semantic_all_pass(model_input)
    for dimension in critic_semantic["quality_dimensions"]:
        if dimension["dimension"] == "ARGUMENT_CHAIN":
            dimension.update(
                {
                    "score": 0,
                    "passed": False,
                    "evidence": ["模型将同一缺陷误判为必须 BLOCK。"],
                    "required_action": "BLOCK",
                }
            )
    critic_semantic["issues"] = [
        {
            "code": "RESEARCH_DESIGN_INCOMPLETE",
            "dimension": "ARGUMENT_CHAIN",
            "severity": "P0",
            "target": {
                "component": "FOUNDATION",
                "thread_index": 0,
                "item_index": 0,
                "review_unit_key": foundation_key,
            },
            "description": "模型认为该缺陷必须阻断。",
            "evidence_ids": [],
            "repair_instruction": "阻断工作流。",
            "resolution": "BLOCK",
        }
    ]

    output = expand_argument_architecture_critic_model_output(
        critic_envelope, critic_semantic
    )
    matching = [
        finding
        for finding in output["findings"]
        if finding.get("semantic_review_unit_key") == foundation_key
        and finding.get("code") == "RESEARCH_DESIGN_INCOMPLETE"
    ]
    assert len(matching) == 2
    assert {finding["defect_namespace"] for finding in matching} == {"MACHINE_DEFECT", "SEMANTIC_OBSERVATION"}
    assert {finding["suggested_route"] for finding in matching} == {"ORIGINAL_PRODUCER", "BLOCK"}
    assert output["status"] == "BLOCK"
    assert output["result"]["verdict"] == "BLOCK"


def test_v5_invariant_machine_managed_fields_never_enter_argument_repair_writable_set():
    from app.contracts import get_semantic_contract
    from app.model_semantic_contracts import _argument_repair_writable_paths

    envelope = _argument_repair_envelope_for_node()
    content = envelope["payload"]["original_object"]["content"]
    writable = _argument_repair_writable_paths(
        content, ["/authored_state/research_threads/0/work_packages/0/methods/0"]
    )
    policy = get_semantic_contract().rule("SC-ARGUMENT-TARGETED-REPAIR-POLICY").config
    machine_fields = {str(value) for value in policy.get("machine_fields") or ()}
    machine_suffixes = tuple(str(value) for value in policy.get("machine_suffixes") or ())

    assert writable
    assert "/authored_state/research_threads/0/work_packages/0/methods/0/statement" in writable
    for path in writable:
        field = path.rsplit("/", 1)[-1]
        assert field not in machine_fields
        assert not any(field.endswith(suffix) for suffix in machine_suffixes)


def test_evidence_quantifier_truth_table_is_explicit():
    assert _argument_quantified_support([], presence="REQUIRED", coverage="ALL") is False
    assert _argument_quantified_support([], presence="IF_PRESENT", coverage="ALL") is True
    assert _argument_quantified_support([True], presence="REQUIRED", coverage="ALL") is True
    assert _argument_quantified_support([False], presence="REQUIRED", coverage="ALL") is False
    assert _argument_quantified_support([True, True], presence="REQUIRED", coverage="ALL") is True
    assert _argument_quantified_support([True, False], presence="REQUIRED", coverage="ALL") is False
    assert _argument_quantified_support([False, False], presence="REQUIRED", coverage="ALL") is False
    assert _argument_quantified_support([True, False], presence="REQUIRED", coverage="ANY") is True
    assert _argument_quantified_support([False, False], presence="REQUIRED", coverage="ANY") is False


def _critic_output_for_canonical(
    canonical: dict,
    producer_envelope: dict,
) -> dict:
    critic_envelope = copy.deepcopy(PACK.replay_input("P-ARGUMENT-ARCHITECTURE-CRITIC"))
    critic_envelope["payload"]["architecture_candidate"] = canonical["result"]
    critic_envelope["payload"]["confirmed_facts"] = copy.deepcopy(
        producer_envelope["payload"]["confirmed_facts"]
    )
    model_input = build_argument_architecture_critic_model_input(critic_envelope)
    return expand_argument_architecture_critic_model_output(
        critic_envelope,
        _critic_semantic_all_pass(model_input),
    )


def test_mixed_closest_prior_work_support_cannot_pass_collection():
    envelope = _argument_envelope_with_evidence()
    semantic = _semantic_argument_output(envelope)
    prior = semantic["research_threads"][0]["innovations"][0]["closest_prior_work"]
    prior.append(
        {
            "statement": "第二个已声明最近工作没有可核验证据。",
            "evidence_ids": [],
        }
    )

    canonical = expand_argument_architecture_model_output(envelope, semantic)
    assert canonical["status"] == "REVISE"

    prior_nodes = [
        node
        for node in canonical["result"]["argument_architecture"]["nodes"]
        if node.get("node_type") == "CLOSEST_PRIOR_WORK"
    ]
    assert [node["status"] for node in prior_nodes] == ["SUPPORTED", "UNKNOWN"]

    critic_output = _critic_output_for_canonical(canonical, envelope)
    assert critic_output["status"] == "REVISE"
    receipts = [
        receipt
        for receipt in critic_output["result"]["deterministic_receipts"]
        if receipt["rule_id"] == "SC-ARGUMENT-EVIDENCE-REQUIREMENTS"
        and receipt["finding_code"] == "INNOVATION_BASELINE_MISSING"
    ]
    assert len(receipts) == 1
    assert receipts[0]["semantic_object_id"] == prior_nodes[1]["node_id"]


def test_mixed_evaluation_baseline_support_cannot_pass_collection():
    envelope = _argument_envelope_with_evidence()
    semantic = _semantic_argument_output(envelope)
    baselines = (
        semantic["research_threads"][0]["work_packages"][0]["methods"][0]
        ["evaluations"][0]["baselines"]
    )
    baselines.append(
        {
            "statement": "第二个已声明比较基线没有可核验证据。",
            "evidence_ids": [],
        }
    )

    canonical = expand_argument_architecture_model_output(envelope, semantic)
    assert canonical["status"] == "REVISE"

    baseline_nodes = [
        node
        for node in canonical["result"]["argument_architecture"]["nodes"]
        if node.get("node_type") == "BASELINE"
    ]
    assert [node["status"] for node in baseline_nodes] == ["SUPPORTED", "UNKNOWN"]

    critic_output = _critic_output_for_canonical(canonical, envelope)
    assert critic_output["status"] == "REVISE"
    receipts = [
        receipt
        for receipt in critic_output["result"]["deterministic_receipts"]
        if receipt["rule_id"] == "SC-ARGUMENT-EVIDENCE-REQUIREMENTS"
        and receipt["finding_code"] == "ARGUMENT_METRIC_JUSTIFICATION_MISSING"
    ]
    assert len(receipts) == 1
    assert receipts[0]["semantic_object_id"] == baseline_nodes[1]["node_id"]


def test_v8_deterministic_registry_does_not_claim_model_semantic_observations():
    from app.contracts import get_semantic_contract

    families = get_semantic_contract().rule(
        "SC-ARGUMENT-DETERMINISTIC-DEFECTS"
    ).config["families"]
    assert all("model_issue_owners" not in family for family in families.values())
    ownership = get_semantic_contract().rule("SC-ARGUMENT-STATE-OWNERSHIP").config
    assert ownership["overlap_policy"] == "COEXIST"
    assert ownership["namespace_writers"]["MACHINE_DEFECT"] == "DETERMINISTIC_RUNTIME"
    assert ownership["namespace_writers"]["SEMANTIC_OBSERVATION"] == "ARGUMENT_CRITIC_RUNTIME_FROM_MODEL_ISSUES"


def test_deterministic_defect_key_distinguishes_failure_family_on_same_row():
    envelope = _argument_envelope_with_evidence()
    semantic = _semantic_argument_output(envelope)
    canonical = expand_argument_architecture_model_output(envelope, semantic)
    candidate = copy.deepcopy(canonical["result"])
    row = candidate["research_design_matrix"][0]
    row["objective_ids"] = []
    row["work_package_ids"] = []

    _, receipts = _critic_chain_checks(candidate, with_receipts=True)
    same_row = [
        receipt
        for receipt in receipts
        if receipt["target_path"] == "/result/research_design_matrix/0"
        and receipt["defect_family"]
        in {"CHAIN_SOURCE_SET_MISSING", "CHAIN_TARGET_SET_MISSING"}
    ]
    assert {receipt["defect_family"] for receipt in same_row} == {
        "CHAIN_SOURCE_SET_MISSING",
        "CHAIN_TARGET_SET_MISSING",
    }
    assert len({receipt["defect_key"] for receipt in same_row}) == len(same_row)



def _flat_skeleton_output(envelope: dict) -> dict:
    evidence_ids = _available_evidence_ids(envelope)
    return {
        "central_proposition": {
            "statement": "通过显式建模事件影响范围和计划稳定性，可降低动态重规划时延与非必要扰动。",
            "proposition_type": "TECHNICAL_PRINCIPLE",
            "falsifiable_or_comparable": True,
            "boundary_conditions": ["动态订单、交通变化和有限运力场景"],
            "evidence_ids": evidence_ids,
        },
        "scope": {
            "in_scope": ["运输任务分配、路径与动态重规划"],
            "out_of_scope": ["部署运维细节作为主文研究内容"],
        },
        "research_threads": [
            {
                "gap_statement": "静态优化难以同时控制方案质量、响应时间和计划扰动。",
                "gap_evidence_ids": evidence_ids,
                "limitation_mechanism_statement": "全量重算没有区分受事件影响与未受影响的决策子结构。",
                "limitation_mechanism_evidence_ids": evidence_ids,
                "question_statement": "如何在动态事件下联合控制求解时延、方案质量和计划扰动？",
                "question_type": "SCIENTIFIC",
                "answerability": "COMPARABLE",
                "success_evidence": ["与滚动优化和全量重算基线比较"],
                "objective_statement": "建立面向动态事件的低扰动运输方案优化方法。",
                "objective_evidence_ids": evidence_ids,
                "assumptions": ["事件影响可映射到有限的业务对象与约束集合。"],
                "falsification_or_comparison_rule": "若不能同时降低时延与非必要扰动，则中心命题不成立。",
            }
        ],
        "evidence_gaps": [],
        "user_questions": [],
        "cannot_proceed_reason": None,
    }


def test_argument_skeleton_contract_is_flat_and_unregistered():
    envelope = _argument_envelope_with_evidence()
    model_input = build_argument_skeleton_model_input(envelope)
    assert set(model_input) == {
        "project_task",
        "constraints",
        "evidence_cards",
        "skeleton_seed",
        "revision_issues",
        "human_resolutions",
    }
    assert set(model_input["skeleton_seed"] or {}) == {
        "central_proposition",
        "boundary_conditions",
        "scope_in",
        "scope_out",
        "existing_chains",
    }
    assert "existing_components" not in (model_input["skeleton_seed"] or {})
    assert "existing_relations" not in (model_input["skeleton_seed"] or {})
    assert argument_skeleton_model_output_errors(_flat_skeleton_output(envelope)) == []


def test_argument_skeleton_contract_rejects_old_nested_design_tree_and_item_wrappers():
    envelope = _argument_envelope_with_evidence()
    skeleton = _flat_skeleton_output(envelope)
    nested = copy.deepcopy(skeleton)
    nested["research_threads"][0]["work_packages"] = []
    errors = argument_skeleton_model_output_errors(nested)
    assert any("Additional properties are not allowed" in error for error in errors)

    wrapped = copy.deepcopy(skeleton)
    wrapped["research_threads"] = {"item": wrapped["research_threads"]}
    errors = argument_skeleton_model_output_errors(wrapped)
    assert any("is not of type 'array'" in error for error in errors)


def test_argument_skeleton_schema_has_no_machine_identity_fields_and_bounded_depth():
    schema = json.loads(
        (ROOT / "prompt_pack/schemas/model/argument_skeleton_model_output.schema.json").read_text(
            encoding="utf-8"
        )
    )
    forbidden = {
        "node_id",
        "relation_id",
        "source_id",
        "target_id",
        "projection_meta",
        "source_hash",
        "context_hash",
        "json_pointer",
    }

    max_depth = 0
    property_names: set[str] = set()

    def visit(node, depth=0):
        nonlocal max_depth
        max_depth = max(max_depth, depth)
        if isinstance(node, dict):
            props = node.get("properties")
            if isinstance(props, dict):
                property_names.update(str(key) for key in props)
            for key, child in node.items():
                if key in {"properties", "items", "allOf", "anyOf", "oneOf"}:
                    visit(child, depth + 1)
                else:
                    visit(child, depth)
        elif isinstance(node, list):
            for child in node:
                visit(child, depth + 1)

    visit(schema)
    assert not (property_names & forbidden)
    assert max_depth <= 6



def _flat_design_output(envelope: dict) -> dict:
    evidence_ids = _available_evidence_ids(envelope)
    return {
        "work_packages": [
            {"thread_index": 0, "work_package_index": 0, "statement": "识别事件影响范围并构造局部重规划问题。", "evidence_ids": evidence_ids}
        ],
        "methods": [
            {"thread_index": 0, "work_package_index": 0, "method_index": 0, "statement": "构建影响子图上的增量优化方法。", "evidence_ids": evidence_ids, "method_type": "ALGORITHM", "assumptions": ["影响范围可从业务依赖关系中识别。"]}
        ],
        "theoretical_properties": [
            {"thread_index": 0, "work_package_index": 0, "method_index": 0, "property_index": 0, "statement": "分析局部更新与全量重算的解质量差异。", "evidence_ids": evidence_ids}
        ],
        "evaluations": [
            {"thread_index": 0, "work_package_index": 0, "method_index": 0, "evaluation_index": 0, "statement": "在动态扰动场景比较重规划时延和计划扰动。", "evidence_ids": evidence_ids, "success_criteria": ["在方案质量可比条件下降低重规划时延和非必要扰动"]}
        ],
        "baselines": [
            {"thread_index": 0, "work_package_index": 0, "method_index": 0, "evaluation_index": 0, "baseline_index": 0, "statement": "全量重新优化基线", "evidence_ids": evidence_ids}
        ],
        "ablations": [
            {"thread_index": 0, "work_package_index": 0, "method_index": 0, "evaluation_index": 0, "ablation_index": 0, "statement": "去除影响范围筛选机制"}
        ],
        "innovations": [
            {"thread_index": 0, "innovation_index": 0, "statement": "面向影响范围的低扰动增量重规划", "evidence_ids": evidence_ids, "contribution": "将变化传播范围显式引入动态运输优化。"}
        ],
        "innovation_prior_work": [
            {"thread_index": 0, "innovation_index": 0, "prior_work_index": 0, "statement": "传统滚动优化通常对完整问题重复求解。", "evidence_ids": evidence_ids}
        ],
        "innovation_evaluation_refs": [
            {"thread_index": 0, "innovation_index": 0, "work_package_index": 0, "method_index": 0, "evaluation_index": 0}
        ],
        "foundation": [],
        "foundation_supports": [],
        "evidence_gaps": [],
        "user_questions": [],
        "cannot_proceed_reason": None,
    }


def test_argument_design_contract_is_flat_frozen_skeleton_input_and_unregistered():
    envelope = _argument_envelope_with_evidence()
    skeleton = _flat_skeleton_output(envelope)
    model_input = build_argument_design_model_input(envelope, skeleton)
    assert set(model_input) == {
        "project_task", "constraints", "evidence_cards", "frozen_skeleton",
        "design_seed", "revision_issues", "human_resolutions",
    }
    assert model_input["frozen_skeleton"] == skeleton
    assert model_input["frozen_skeleton"] is not skeleton
    assert set(model_input["design_seed"] or {}) == {"existing_components", "existing_relations"}
    design = _flat_design_output(envelope)
    assert argument_design_model_output_errors(design) == []
    assert argument_design_model_reference_errors(design, skeleton) == []


def test_argument_design_seed_excludes_skeleton_owned_semantics_and_keeps_design_hints():
    envelope = _argument_envelope_with_evidence()
    skeleton = _flat_skeleton_output(envelope)
    subgraph = envelope["payload"]["project_subgraph"]
    subgraph["items"].extend([
        {
            "item_id": "WP-SEED",
            "item_type": "WORK_PACKAGE",
            "content": {"statement": "工作包种子"},
            "knowledge_status": "CONFIRMED",
        },
        {
            "item_id": "METHOD-SEED",
            "item_type": "METHOD",
            "content": {"statement": "方法种子"},
            "knowledge_status": "CONFIRMED",
        },
    ])
    subgraph["relations"] = [
        {
            "source_item_id": "item-001",
            "relation_type": "DECOMPOSES_TO",
            "target_item_id": "WP-SEED",
            "status": "CONFIRMED",
        },
        {
            "source_item_id": "WP-SEED",
            "relation_type": "USES",
            "target_item_id": "METHOD-SEED",
            "status": "CONFIRMED",
        },
    ]

    model_input = build_argument_design_model_input(envelope, skeleton)
    seed = model_input["design_seed"]
    component_types = {item["component_type"] for item in seed["existing_components"]}
    assert "OBJECTIVE" not in component_types
    assert {"WORK_PACKAGE", "METHOD", "FORMAL_MODEL", "EXPERIMENT_DESIGN", "NOVEL_MECHANISM", "TEAM_EVIDENCE"} <= component_types
    assert [item["relation"] for item in seed["existing_relations"]] == ["DECOMPOSES_TO", "USES"]
    assert seed["existing_relations"][0]["source_type"] == "OBJECTIVE"
    assert seed["existing_relations"][0]["target_type"] == "WORK_PACKAGE"
    assert all(
        item["source_type"] != "PROBLEM" and item["target_type"] != "PROBLEM"
        for item in seed["existing_relations"]
    )


def test_argument_design_contract_rejects_nested_tree_item_wrappers_and_skeleton_redefinition():
    envelope = _argument_envelope_with_evidence()
    design = _flat_design_output(envelope)
    nested = copy.deepcopy(design)
    nested["work_packages"][0]["methods"] = []
    errors = argument_design_model_output_errors(nested)
    assert any("Additional properties are not allowed" in error for error in errors)

    wrapped = copy.deepcopy(design)
    wrapped["methods"] = {"item": wrapped["methods"]}
    errors = argument_design_model_output_errors(wrapped)
    assert any("is not of type 'array'" in error for error in errors)

    redefined = copy.deepcopy(design)
    redefined["central_proposition"] = {"statement": "Stage B must not redefine Stage A"}
    errors = argument_design_model_output_errors(redefined)
    assert any("Additional properties are not allowed" in error for error in errors)


def test_argument_design_reference_validation_is_local_and_fail_closed():
    envelope = _argument_envelope_with_evidence()
    skeleton = _flat_skeleton_output(envelope)
    design = _flat_design_output(envelope)

    broken_thread = copy.deepcopy(design)
    broken_thread["work_packages"][0]["thread_index"] = 9
    assert any("thread_index: out of range" in error for error in argument_design_model_reference_errors(broken_thread, skeleton))

    broken_parent = copy.deepcopy(design)
    broken_parent["methods"][0]["work_package_index"] = 7
    assert any("unresolved parent index" in error for error in argument_design_model_reference_errors(broken_parent, skeleton))

    duplicate = copy.deepcopy(design)
    duplicate["methods"].append(copy.deepcopy(duplicate["methods"][0]))
    assert any("duplicate local index" in error for error in argument_design_model_reference_errors(duplicate, skeleton))


def test_argument_design_schema_has_only_flat_local_indexes_and_bounded_depth():
    schema = json.loads(
        (ROOT / "prompt_pack/schemas/model/argument_design_model_output.schema.json").read_text(encoding="utf-8")
    )
    forbidden = {
        "node_id", "relation_id", "source_id", "target_id", "projection_meta",
        "source_hash", "context_hash", "json_pointer", "central_proposition",
        "scope", "research_threads", "work_packages_nested", "methods_nested",
    }
    max_depth = 0
    property_names: set[str] = set()

    def visit(node, depth=0):
        nonlocal max_depth
        max_depth = max(max_depth, depth)
        if isinstance(node, dict):
            props = node.get("properties")
            if isinstance(props, dict):
                property_names.update(str(key) for key in props)
            for key, child in node.items():
                if key in {"properties", "items", "allOf", "anyOf", "oneOf"}:
                    visit(child, depth + 1)
                else:
                    visit(child, depth)
        elif isinstance(node, list):
            for child in node:
                visit(child, depth + 1)

    visit(schema)
    assert not (property_names & forbidden)
    assert max_depth <= 5


def test_argument_thread_assembler_reconstructs_legacy_authored_thread_without_machine_fields():
    envelope = _argument_envelope_with_evidence()
    skeleton = _flat_skeleton_output(envelope)
    design = _flat_design_output(envelope)
    thread = assemble_argument_authored_thread(skeleton, design, 0)

    assert set(thread) == {
        "gap", "question", "objective", "thread_assumptions",
        "work_packages", "innovations", "foundation",
        "falsification_or_comparison_rule",
    }
    assert thread["foundation"] == []
    assert thread["work_packages"][0]["methods"][0]["evaluations"][0]["baselines"][0]["statement"] == "全量重新优化基线"
    assert thread["innovations"][0]["evaluation_refs"] == [
        {"work_package_index": 0, "method_index": 0, "evaluation_index": 0}
    ]

    foundation_design = copy.deepcopy(design)
    foundation_design["foundation"] = [{
        "thread_index": 0, "foundation_index": 0,
        "statement": "已有优化原型可支撑工作包实施。",
        "evidence_ids": _available_evidence_ids(envelope),
    }]
    foundation_design["foundation_supports"] = [{
        "thread_index": 0, "foundation_index": 0,
        "work_package_index": 0, "method_index": None,
    }]
    supported = assemble_argument_authored_thread(skeleton, foundation_design, 0)
    assert supported["foundation"][0]["supports"] == [
        {"work_package_index": 0, "method_index": None}
    ]
    rendered = json.dumps(thread, ensure_ascii=False)
    for forbidden in ("node_id", "edge_id", "graph_id", "projection_meta", "thread_index"):
        assert forbidden not in rendered


def test_argument_thread_assembler_rebases_provider_keys_to_nested_positions():
    """Replay the index topologies from the passed and failed 2026-08-25 calls.

    ``call-b90ff459...`` used work-package key 0 independently in every
    thread. ``call-886f5fb...`` used keys 0, 1, 2, and 3 across the four
    threads. Both flat outputs are self-consistent and valid. Their authored
    nested references must therefore be identical and positional.
    """

    envelope = _argument_envelope_with_evidence()
    base_skeleton = _flat_skeleton_output(envelope)
    base_design = _flat_design_output(envelope)
    skeleton = copy.deepcopy(base_skeleton)
    skeleton["research_threads"] = [
        copy.deepcopy(base_skeleton["research_threads"][0]) for _ in range(4)
    ]

    def four_thread_design(*, provider_global_keys: bool) -> dict:
        design = {
            key: [] if isinstance(value, list) else copy.deepcopy(value)
            for key, value in base_design.items()
        }
        thread_collections = (
            "work_packages",
            "methods",
            "theoretical_properties",
            "evaluations",
            "baselines",
            "ablations",
            "innovations",
            "innovation_prior_work",
            "innovation_evaluation_refs",
        )
        for thread_index in range(4):
            work_package_key = thread_index if provider_global_keys else 0
            for collection in thread_collections:
                for source in base_design[collection]:
                    row = copy.deepcopy(source)
                    row["thread_index"] = thread_index
                    if "work_package_index" in row:
                        row["work_package_index"] = work_package_key
                    design[collection].append(row)
        return design

    passed_topology = four_thread_design(provider_global_keys=False)
    failed_topology = four_thread_design(provider_global_keys=True)

    for design in (passed_topology, failed_topology):
        assert argument_design_model_reference_errors(design, skeleton) == []
        authored = assemble_argument_authored_state(skeleton, design)
        assert [
            thread["innovations"][0]["evaluation_refs"][0]
            for thread in authored["research_threads"]
        ] == [
            {
                "work_package_index": 0,
                "method_index": 0,
                "evaluation_index": 0,
            }
        ] * 4
        assert PACK.validate_model(
            "P-ARGUMENT-ARCHITECTURE", "output", authored
        ) == []


def test_argument_thread_assembler_rebases_sparse_foundation_support_keys():
    envelope = _argument_envelope_with_evidence()
    skeleton = _flat_skeleton_output(envelope)
    design = _flat_design_output(envelope)
    for collection in (
        "work_packages",
        "methods",
        "theoretical_properties",
        "evaluations",
        "baselines",
        "ablations",
        "innovation_evaluation_refs",
    ):
        for row in design[collection]:
            row["work_package_index"] = 7
            if "method_index" in row and row["method_index"] is not None:
                row["method_index"] = 11
            if "evaluation_index" in row:
                row["evaluation_index"] = 13
    design["foundation"] = [{
        "thread_index": 0,
        "foundation_index": 4,
        "statement": "已有优化原型可支撑工作包实施。",
        "evidence_ids": _available_evidence_ids(envelope),
    }]
    design["foundation_supports"] = [
        {
            "thread_index": 0,
            "foundation_index": 4,
            "work_package_index": 7,
            "method_index": None,
        },
        {
            "thread_index": 0,
            "foundation_index": 4,
            "work_package_index": 7,
            "method_index": 11,
        },
    ]

    assert argument_design_model_reference_errors(design, skeleton) == []
    thread = assemble_argument_authored_thread(skeleton, design, 0)

    assert thread["innovations"][0]["evaluation_refs"] == [
        {"work_package_index": 0, "method_index": 0, "evaluation_index": 0}
    ]
    assert thread["foundation"][0]["supports"] == [
        {"work_package_index": 0, "method_index": None},
        {"work_package_index": 0, "method_index": 0},
    ]


def test_argument_thread_assembler_output_is_compatible_with_existing_model_contract_and_projector():
    envelope = _argument_envelope_with_evidence()
    skeleton = _flat_skeleton_output(envelope)
    design = _flat_design_output(envelope)
    semantic = {
        "central_proposition": copy.deepcopy(skeleton["central_proposition"]),
        "scope": copy.deepcopy(skeleton["scope"]),
        "research_threads": [assemble_argument_authored_thread(skeleton, design, 0)],
        "evidence_gaps": copy.deepcopy(skeleton["evidence_gaps"] + design["evidence_gaps"]),
        "user_questions": copy.deepcopy(skeleton["user_questions"] + design["user_questions"]),
        "cannot_proceed_reason": design["cannot_proceed_reason"] or skeleton["cannot_proceed_reason"],
    }
    assert PACK.validate_model("P-ARGUMENT-ARCHITECTURE", "output", semantic) == []
    canonical = expand_argument_architecture_model_output(envelope, semantic)
    assert PACK.validate("P-ARGUMENT-ARCHITECTURE", "output", canonical) == []


def test_argument_thread_assembler_is_index_ordered_pure_and_fail_closed():
    envelope = _argument_envelope_with_evidence()
    skeleton = _flat_skeleton_output(envelope)
    design = _flat_design_output(envelope)
    design["work_packages"].append({
        "thread_index": 0, "work_package_index": 2,
        "statement": "第三工作包", "evidence_ids": _available_evidence_ids(envelope),
    })
    design["work_packages"].append({
        "thread_index": 0, "work_package_index": 1,
        "statement": "第二工作包", "evidence_ids": _available_evidence_ids(envelope),
    })
    original_skeleton = copy.deepcopy(skeleton)
    original_design = copy.deepcopy(design)
    thread = assemble_argument_authored_thread(skeleton, design, 0)
    assert [item["statement"] for item in thread["work_packages"]] == [
        "识别事件影响范围并构造局部重规划问题。", "第二工作包", "第三工作包"
    ]
    assert skeleton == original_skeleton
    assert design == original_design

    broken = copy.deepcopy(design)
    broken["methods"][0]["work_package_index"] = 99
    with pytest.raises(ValueError, match="Invalid Argument Design references"):
        assemble_argument_authored_thread(skeleton, broken, 0)
    with pytest.raises(ValueError, match="thread_index out of range"):
        assemble_argument_authored_thread(skeleton, design, 9)



def test_argument_authored_state_assembler_reconstructs_complete_legacy_semantics_and_projector_accepts_it():
    envelope = _argument_envelope_with_evidence()
    skeleton = _flat_skeleton_output(envelope)
    design = _flat_design_output(envelope)
    authored = assemble_argument_authored_state(skeleton, design)

    assert authored["central_proposition"] == skeleton["central_proposition"]
    assert authored["scope"] == skeleton["scope"]
    assert authored["research_threads"] == [
        assemble_argument_authored_thread(skeleton, design, 0)
    ]
    assert authored["evidence_gaps"] == skeleton["evidence_gaps"] + design["evidence_gaps"]
    assert authored["user_questions"] == skeleton["user_questions"] + design["user_questions"]
    assert PACK.validate_model("P-ARGUMENT-ARCHITECTURE", "output", authored) == []
    canonical = expand_argument_architecture_model_output(envelope, authored)
    assert PACK.validate("P-ARGUMENT-ARCHITECTURE", "output", canonical) == []


def test_argument_authored_state_split_is_lossless_without_schema_changes():
    envelope = _argument_envelope_with_evidence()
    skeleton = _flat_skeleton_output(envelope)
    design = _flat_design_output(envelope)
    design["evidence_gaps"] = [{
        "kind": "OTHER",
        "thread_index": 0,
        "reason": "Optional follow-up evidence.",
        "blocking": False,
        "suggested_question": None,
    }]
    authored = assemble_argument_authored_state(skeleton, design)

    recovered_skeleton, recovered_design = split_argument_authored_state(authored)

    assert assemble_argument_authored_state(
        recovered_skeleton, recovered_design
    ) == authored
    assert argument_skeleton_model_output_errors(recovered_skeleton) == []
    assert argument_design_model_output_errors(recovered_design) == []
    assert argument_design_model_reference_errors(
        recovered_design, recovered_skeleton
    ) == []


def test_argument_advisory_gap_does_not_block_stage_zero_but_hard_gap_does():
    envelope = _argument_envelope_with_evidence()
    skeleton = _flat_skeleton_output(envelope)
    design = _flat_design_output(envelope)
    design["evidence_gaps"] = [{
        "kind": "OTHER",
        "thread_index": 0,
        "reason": "Optional follow-up evidence.",
        "blocking": False,
        "suggested_question": None,
    }]
    advisory = expand_argument_architecture_model_output(
        envelope, assemble_argument_authored_state(skeleton, design)
    )
    assert advisory["status"] == "PASS"
    assert advisory["unresolved_items"][0]["blocking"] is False

    incomplete = copy.deepcopy(design)
    incomplete["evaluations"] = []
    incomplete["baselines"] = []
    incomplete["ablations"] = []
    incomplete["innovation_evaluation_refs"] = []
    incomplete["user_questions"] = [{
        "target_area": "METRIC_JUSTIFICATION",
        "question_type": "MISSING_INFORMATION",
        "question": "Optional metric follow-up?",
        "reason": "The model marked this follow-up as advisory.",
        "answer_shape": "STRING",
        "allowed_values": [],
        "blocking": False,
        "priority": "P2",
    }]
    hard = expand_argument_architecture_model_output(
        envelope, assemble_argument_authored_state(skeleton, incomplete)
    )
    assert hard["status"] == "REVISE"
    assert all(item["blocking"] is False for item in hard["user_questions"])
    deterministic = [
        item
        for item in hard["result"]["evidence_gap_report"]
        if item["defect_family"] != "MODEL_DECLARED_GAP"
    ]
    assert deterministic
    assert all(item["blocking"] is True for item in deterministic)


def test_argument_authored_state_assembler_preserves_all_skeleton_threads_without_design_invention():
    envelope = _argument_envelope_with_evidence()
    skeleton = _flat_skeleton_output(envelope)
    second = copy.deepcopy(skeleton["research_threads"][0])
    second["gap_statement"] = "第二研究线程缺口"
    second["question_statement"] = "第二研究线程问题？"
    second["objective_statement"] = "第二研究线程目标"
    skeleton["research_threads"].append(second)
    design = _flat_design_output(envelope)

    original_skeleton = copy.deepcopy(skeleton)
    original_design = copy.deepcopy(design)
    authored = assemble_argument_authored_state(skeleton, design)
    assert len(authored["research_threads"]) == 2
    assert authored["research_threads"][1]["gap"]["statement"] == "第二研究线程缺口"
    assert authored["research_threads"][1]["work_packages"] == []
    assert authored["research_threads"][1]["innovations"] == []
    assert authored["research_threads"][1]["foundation"] == []
    assert skeleton == original_skeleton
    assert design == original_design


def test_argument_authored_state_assembler_merges_stage_local_blockers_without_silent_conflict_resolution():
    envelope = _argument_envelope_with_evidence()
    skeleton = _flat_skeleton_output(envelope)
    design = _flat_design_output(envelope)
    skeleton["cannot_proceed_reason"] = "Skeleton blocker"
    design["cannot_proceed_reason"] = "Design blocker"
    with pytest.raises(ValueError, match="Conflicting cannot_proceed_reason"):
        assemble_argument_authored_state(skeleton, design)

    design["cannot_proceed_reason"] = "Skeleton blocker"
    skeleton["evidence_gaps"] = [{
        "kind": "OTHER", "thread_index": None, "reason": "Skeleton gap",
        "blocking": False, "suggested_question": None,
    }]
    design["evidence_gaps"] = [{
        "kind": "METRIC_JUSTIFICATION", "thread_index": 0, "reason": "Design gap",
        "blocking": True, "suggested_question": "请补充指标依据。",
    }]
    authored = assemble_argument_authored_state(skeleton, design)
    assert authored["cannot_proceed_reason"] == "Skeleton blocker"
    assert [item["reason"] for item in authored["evidence_gaps"]] == ["Skeleton gap", "Design gap"]


class _FakeArgumentStageGateway:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def invoke_stage(
        self,
        stage,
        model_input,
        output_schema,
        *,
        retry_context=None,
        desired_output_tokens,
    ):
        self.calls.append({
            "stage": stage,
            "model_input": copy.deepcopy(model_input),
            "output_schema": copy.deepcopy(output_schema),
            "retry_context": copy.deepcopy(retry_context),
            "desired_output_tokens": desired_output_tokens,
        })
        response = self.responses[len(self.calls) - 1]
        if callable(response):
            response = response(stage, model_input, output_schema)
        return copy.deepcopy(response)


def _stage_repair_response(*changes: tuple[str, object]) -> dict:
    return {
        "decision": "APPLY",
        "changes": [
            {"path": path, "value": copy.deepcopy(value)}
            for path, value in changes
        ],
        "escalation_reason": None,
    }


def test_argument_two_stage_step4a_happy_path_projects_existing_canonical_output():
    envelope = _argument_envelope_with_evidence()
    skeleton = _flat_skeleton_output(envelope)
    design = _flat_design_output(envelope)
    gateway = _FakeArgumentStageGateway([skeleton, design])

    result = asyncio.run(orchestrate_argument_architecture_two_stage(
        envelope, stage_gateway=gateway, pack=PACK
    ))

    assert [call["stage"] for call in gateway.calls] == [
        ARGUMENT_SKELETON_STAGE, ARGUMENT_DESIGN_STAGE
    ]
    assert [call["desired_output_tokens"] for call in gateway.calls] == [
        8_192, 65_536
    ]
    assert gateway.calls[1]["model_input"]["frozen_skeleton"] == result["skeleton"]
    assert result["skeleton"] == skeleton
    assert result["design"] == design
    assert result["authored_state"] == assemble_argument_authored_state(skeleton, design)
    assert PACK.validate("P-ARGUMENT-ARCHITECTURE", "output", result["canonical_output"]) == []


def test_argument_semantic_regeneration_freezes_skeleton_and_accepts_only_improvement():
    envelope = _argument_envelope_with_evidence()
    skeleton = _flat_skeleton_output(envelope)
    complete_design = _flat_design_output(envelope)
    baseline_design = copy.deepcopy(complete_design)
    baseline_design["evaluations"] = []
    baseline_design["baselines"] = []
    baseline_design["ablations"] = []
    baseline_design["innovation_evaluation_refs"] = []
    baseline_authored = assemble_argument_authored_state(
        skeleton, baseline_design
    )
    envelope["payload"]["revision_findings"] = [{
        "description": "The method lacks a validation/evaluation closure.",
        "repair_instruction": "Add the missing evaluation closure.",
        "severity": "P1",
        "semantic_component": "EVALUATION",
        "semantic_thread": 0,
    }]
    gateway = _FakeArgumentStageGateway([complete_design])

    result = asyncio.run(orchestrate_argument_architecture_two_stage(
        envelope,
        stage_gateway=gateway,
        pack=PACK,
        regeneration_baseline_authored_state=baseline_authored,
    ))

    assert [call["stage"] for call in gateway.calls] == [ARGUMENT_DESIGN_STAGE]
    retry = gateway.calls[0]["retry_context"]
    assert retry["previous_candidate"] == baseline_design
    assert retry["validation_errors"]
    assert result["skeleton"] == skeleton
    assert result["design"] == complete_design
    assert result["regeneration_merge"]["accepted"] is True
    assert result["regeneration_merge"]["reason"] == (
        "STRICT_NON_REGRESSIVE_HARD_GAP_IMPROVEMENT"
    )
    assert result["regeneration_merge"]["blocking_after"] == 0
    assert result["canonical_output"]["status"] == "PASS"


def test_argument_semantic_regeneration_contract_names_complete_output_and_exact_baseline_target():
    envelope = _argument_envelope_with_evidence()
    skeleton = _flat_skeleton_output(envelope)
    complete_design = _flat_design_output(envelope)
    baseline_design = copy.deepcopy(complete_design)
    baseline_design["baselines"] = []
    baseline_authored = assemble_argument_authored_state(skeleton, baseline_design)
    envelope["payload"]["revision_findings"] = [{
        "code": "ARGUMENT_METRIC_JUSTIFICATION_MISSING",
        "defect_key": "ARGUMENT:EVALUATION_BASELINE_SUPPORT:0",
        "description": "The evaluation lacks an evidence-backed baseline.",
        "repair_instruction": "Add the missing representative baseline.",
        "severity": "P1",
        "semantic_component": "EVALUATION",
        "semantic_thread": 0,
    }]
    gateway = _FakeArgumentStageGateway([complete_design])

    result = asyncio.run(orchestrate_argument_architecture_two_stage(
        envelope,
        stage_gateway=gateway,
        pack=PACK,
        regeneration_baseline_authored_state=baseline_authored,
    ))

    retry = gateway.calls[0]["retry_context"]
    assert retry["mode"] == "WHOLE_DESIGN_REVISE"
    assert retry["required_output"] == "COMPLETE_DESIGN"
    assert retry["required_thread_indices"] == [0]
    assert retry["previous_candidate"] == baseline_design
    assert retry["baseline_inventory"]["totals"]["evaluations"] == 1
    assert retry["baseline_inventory"]["totals"]["baselines"] == 0
    assert retry["exact_revision_targets"] == [{
        "target_kind": "EVALUATION_BASELINE_SUPPORT",
        "thread_index": 0,
        "work_package_index": 0,
        "method_index": 0,
        "evaluation_index": 0,
        "required_change": (
            "Add at least one representative baseline with non-empty evidence_ids for this evaluation."
        ),
        "blocking": True,
        "route": "ORIGINAL_PRODUCER",
    }]
    assert retry["validation_errors"] == [
        "evaluation(thread=0,work_package=0,method=0,evaluation=0): add at least one evidence-backed baseline"
    ]
    assert result["design"] == complete_design
    assert result["canonical_output"]["status"] == "PASS"


def test_argument_semantic_revision_retry_reuses_accepted_baseline_not_failed_response():
    envelope = _argument_envelope_with_evidence()
    skeleton = _flat_skeleton_output(envelope)
    complete_design = _flat_design_output(envelope)
    baseline_design = copy.deepcopy(complete_design)
    baseline_design["baselines"] = []
    baseline_authored = assemble_argument_authored_state(skeleton, baseline_design)
    envelope["payload"]["revision_findings"] = [{
        "code": "ARGUMENT_METRIC_JUSTIFICATION_MISSING",
        "defect_key": "ARGUMENT:EVALUATION_BASELINE_SUPPORT:0",
        "description": "The evaluation lacks an evidence-backed baseline.",
        "repair_instruction": "Add the missing representative baseline.",
        "severity": "P1",
        "semantic_component": "EVALUATION",
        "semantic_thread": 0,
    }]
    partial_response = copy.deepcopy(complete_design)
    partial_response["work_packages"] = []
    gateway = _FakeArgumentStageGateway([partial_response, complete_design])

    result = asyncio.run(orchestrate_argument_architecture_two_stage(
        envelope,
        stage_gateway=gateway,
        pack=PACK,
        max_stage_attempts=2,
        regeneration_baseline_authored_state=baseline_authored,
    ))

    first, retry = [item["retry_context"] for item in gateway.calls]
    assert first["previous_candidate"] == baseline_design
    assert retry["mode"] == "WHOLE_DESIGN_REVISE"
    assert retry["recovery_mode"] == "WHOLE_DESIGN_REVISE_RETRY"
    assert retry["previous_candidate"] == baseline_design
    assert retry["previous_candidate"] != partial_response
    assert any("work_packages" in item for item in retry["validation_errors"])
    assert result["design"] == complete_design
    assert result["canonical_output"]["status"] == "PASS"


def test_historical_partial_semantic_revise_response_keeps_four_thread_baseline_whole():
    """Replay call-cb9ec738, which returned only thread 0 during REVISE."""

    envelope = _argument_envelope_with_evidence()
    one_thread_skeleton = _flat_skeleton_output(envelope)
    one_thread_design = _flat_design_output(envelope)
    skeleton = copy.deepcopy(one_thread_skeleton)
    skeleton["research_threads"] = [
        copy.deepcopy(one_thread_skeleton["research_threads"][0])
        for _ in range(4)
    ]
    design = {
        key: [] if isinstance(value, list) else copy.deepcopy(value)
        for key, value in one_thread_design.items()
    }
    for thread_index in range(4):
        for collection, rows in one_thread_design.items():
            if collection in {"evidence_gaps", "user_questions"} or not isinstance(rows, list):
                continue
            for source in rows:
                row = copy.deepcopy(source)
                row["thread_index"] = thread_index
                design[collection].append(row)
    design["baselines"] = [
        item for item in design["baselines"] if item["thread_index"] == 0
    ]
    baseline_authored = assemble_argument_authored_state(skeleton, design)
    envelope["payload"]["revision_findings"] = [
        {
            "code": "ARGUMENT_METRIC_JUSTIFICATION_MISSING",
            "defect_key": (
                f"ARGUMENT:EVALUATION_BASELINE_SUPPORT:{thread_index}"
            ),
            "description": "The evaluation lacks an evidence-backed baseline.",
            "repair_instruction": "Add the missing representative baseline.",
            "severity": "P1",
            "semantic_component": "EVALUATION",
            "semantic_thread": thread_index,
        }
        for thread_index in (1, 2, 3)
    ]
    historical_response = json.loads(
        (
            Path(__file__).resolve().parent
            / "fixtures"
            / "stage0_call_cb9ec738_partial_revise_response.json"
        ).read_text(encoding="utf-8")
    )
    assert len(historical_response["work_packages"]) == 1
    gateway = _FakeArgumentStageGateway([historical_response])

    result = asyncio.run(orchestrate_argument_architecture_two_stage(
        envelope,
        stage_gateway=gateway,
        pack=PACK,
        max_stage_attempts=1,
        regeneration_baseline_authored_state=baseline_authored,
    ))

    assert result["authored_state"] == baseline_authored
    assert result["design"] == design
    assert result["regeneration_merge"]["accepted"] is False
    assert result["regeneration_merge"]["stage_rejection"]["phase"] in {
        "structure_validation",
        "cross_stage_validation",
        "whole_design_revision_validation",
    }
    retry = gateway.calls[0]["retry_context"]
    assert retry["required_thread_indices"] == [0, 1, 2, 3]
    assert [
        target["thread_index"] for target in retry["exact_revision_targets"]
    ] == [1, 2, 3]


def test_argument_semantic_regeneration_rejects_unimproved_whole_candidate():
    envelope = _argument_envelope_with_evidence()
    skeleton = _flat_skeleton_output(envelope)
    complete_design = _flat_design_output(envelope)
    baseline_design = copy.deepcopy(complete_design)
    baseline_design["evaluations"] = []
    baseline_design["baselines"] = []
    baseline_design["ablations"] = []
    baseline_design["innovation_evaluation_refs"] = []
    baseline_authored = assemble_argument_authored_state(
        skeleton, baseline_design
    )
    envelope["payload"]["revision_findings"] = [{
        "description": "The method lacks a validation/evaluation closure.",
        "repair_instruction": "Add the missing evaluation closure.",
        "severity": "P1",
        "semantic_component": "EVALUATION",
        "semantic_thread": 0,
    }]
    gateway = _FakeArgumentStageGateway([baseline_design])

    result = asyncio.run(orchestrate_argument_architecture_two_stage(
        envelope,
        stage_gateway=gateway,
        pack=PACK,
        regeneration_baseline_authored_state=baseline_authored,
    ))

    assert result["design"] == baseline_design
    assert result["regeneration_merge"]["accepted"] is False
    assert result["regeneration_merge"]["reason"] == (
        "NO_STRICT_NON_REGRESSIVE_HARD_GAP_IMPROVEMENT"
    )
    assert result["canonical_output"]["status"] == "REVISE"


def test_argument_semantic_regeneration_rejects_whole_candidate_shrinkage():
    """Replay the 6-to-18-gap regression without adopting any hybrid state."""

    envelope = _argument_envelope_with_evidence()
    skeleton = _flat_skeleton_output(envelope)
    complete_design = _flat_design_output(envelope)
    baseline_design = copy.deepcopy(complete_design)
    baseline_design["evaluations"] = []
    baseline_design["baselines"] = []
    baseline_design["ablations"] = []
    baseline_design["innovation_evaluation_refs"] = []
    baseline_authored = assemble_argument_authored_state(
        skeleton, baseline_design
    )
    envelope["payload"]["revision_findings"] = [{
        "description": "The method lacks a validation/evaluation closure.",
        "repair_instruction": "Regenerate the complete Design.",
        "severity": "P1",
        "semantic_component": "EVALUATION",
        "semantic_thread": 0,
    }]
    degraded_design = copy.deepcopy(baseline_design)
    for collection in (
        "work_packages",
        "methods",
        "theoretical_properties",
        "innovations",
        "innovation_prior_work",
    ):
        degraded_design[collection] = []
    gateway = _FakeArgumentStageGateway([degraded_design] * 3)

    result = asyncio.run(
        orchestrate_argument_architecture_two_stage(
            envelope,
            stage_gateway=gateway,
            pack=PACK,
            regeneration_baseline_authored_state=baseline_authored,
        )
    )

    assert result["authored_state"] == baseline_authored
    assert result["design"] == baseline_design
    assert result["regeneration_merge"]["accepted"] is False
    assert result["regeneration_merge"]["regressed_collections"] == []
    stage_rejection = result["regeneration_merge"]["stage_rejection"]
    assert stage_rejection["phase"] == "whole_design_revision_validation"
    assert any(
        "work_packages" in error
        for error in stage_rejection["validation_errors"]
    )
    assert all(
        call["retry_context"]["previous_candidate"] == baseline_design
        for call in gateway.calls
    )
    assert [
        call["retry_context"].get("recovery_mode") for call in gateway.calls
    ] == [None, "WHOLE_DESIGN_REVISE_RETRY", "WHOLE_DESIGN_REVISE_RETRY"]


def test_argument_two_stage_normalizes_exact_null_string_without_mutating_provider_candidate():
    envelope = _argument_envelope_with_evidence()
    skeleton = _flat_skeleton_output(envelope)
    design = _flat_design_output(envelope)
    design["cannot_proceed_reason"] = "null"
    gateway = _FakeArgumentStageGateway([skeleton, design])

    result = asyncio.run(orchestrate_argument_architecture_two_stage(
        envelope, stage_gateway=gateway, pack=PACK, max_stage_attempts=1
    ))

    assert design["cannot_proceed_reason"] == "null"
    assert result["design"]["cannot_proceed_reason"] is None
    assert result["authored_state"]["cannot_proceed_reason"] is None
    assert (
        result["canonical_output"]["result"]["authored_state"][
            "cannot_proceed_reason"
        ]
        is None
    )
    assert [call["stage"] for call in gateway.calls] == [
        ARGUMENT_SKELETON_STAGE,
        ARGUMENT_DESIGN_STAGE,
    ]


def test_argument_two_stage_fills_only_determined_missing_question_defaults():
    envelope = _argument_envelope_with_evidence()
    skeleton = _flat_skeleton_output(envelope)
    skeleton["user_questions"] = [{
        "target_area": "RESEARCH_DESIGN",
        "question_type": "MISSING_INFORMATION",
        "question": "请补充验收阈值。",
        "reason": "验收阈值尚未确认。",
        "answer_shape": "OBJECT",
        "blocking": True,
        "priority": "P0",
    }]
    del skeleton["cannot_proceed_reason"]
    raw_skeleton = copy.deepcopy(skeleton)
    design = _flat_design_output(envelope)
    gateway = _FakeArgumentStageGateway([skeleton, design])

    result = asyncio.run(orchestrate_argument_architecture_two_stage(
        envelope, stage_gateway=gateway, pack=PACK, max_stage_attempts=1
    ))

    assert skeleton == raw_skeleton
    assert "cannot_proceed_reason" not in skeleton
    assert "allowed_values" not in skeleton["user_questions"][0]
    assert result["skeleton"]["cannot_proceed_reason"] is None
    assert result["skeleton"]["user_questions"][0]["allowed_values"] == []
    assert result["canonical_output"]["status"] == "NEED_USER_INPUT"


def test_argument_two_stage_does_not_invent_missing_choice_options():
    envelope = _argument_envelope_with_evidence()
    skeleton = _flat_skeleton_output(envelope)
    skeleton["user_questions"] = [{
        "target_area": "RESEARCH_DESIGN",
        "question_type": "CHOICE",
        "question": "请选择验收口径。",
        "reason": "验收口径尚未确认。",
        "answer_shape": "STRING",
        "blocking": True,
        "priority": "P0",
    }]
    design = _flat_design_output(envelope)
    gateway = _FakeArgumentStageGateway([skeleton, design])

    result = asyncio.run(orchestrate_argument_architecture_two_stage(
        envelope, stage_gateway=gateway, pack=PACK, max_stage_attempts=1
    ))

    question = result["skeleton"]["user_questions"][0]
    assert question["question_type"] == "MISSING_INFORMATION"
    assert question["answer_shape"] == "STRING"
    assert question["allowed_values"] == []


def test_argument_two_stage_does_not_invent_missing_reason_without_blocking_question():
    envelope = _argument_envelope_with_evidence()
    skeleton = _flat_skeleton_output(envelope)
    del skeleton["cannot_proceed_reason"]
    design = _flat_design_output(envelope)
    gateway = _FakeArgumentStageGateway([skeleton, design])

    result = asyncio.run(orchestrate_argument_architecture_two_stage(
        envelope, stage_gateway=gateway, pack=PACK, max_stage_attempts=1
    ))

    assert result["skeleton"]["cannot_proceed_reason"] is None


def test_argument_two_stage_prunes_only_orphan_design_leaf_without_retry_or_raw_mutation():
    envelope = _argument_envelope_with_evidence()
    skeleton = _flat_skeleton_output(envelope)
    design = _flat_design_output(envelope)
    orphan = copy.deepcopy(design["baselines"][0])
    orphan["evaluation_index"] = 99
    orphan["baseline_index"] = 99
    design["baselines"].append(orphan)
    raw_design = copy.deepcopy(design)
    gateway = _FakeArgumentStageGateway([skeleton, design])

    result = asyncio.run(orchestrate_argument_architecture_two_stage(
        envelope, stage_gateway=gateway, pack=PACK, max_stage_attempts=1
    ))

    assert design == raw_design
    assert result["design"]["baselines"] == raw_design["baselines"][:-1]
    assert [call["stage"] for call in gateway.calls] == [
        ARGUMENT_SKELETON_STAGE,
        ARGUMENT_DESIGN_STAGE,
    ]


def test_argument_two_stage_dedupes_design_questions_and_gaps_without_retry():
    envelope = _argument_envelope_with_evidence()
    skeleton = _flat_skeleton_output(envelope)
    question = {
        "target_area": "RESEARCH_DESIGN",
        "question_type": "MISSING_INFORMATION",
        "question": "请补充验收阈值。",
        "reason": "验收阈值尚未确认。",
        "answer_shape": "OBJECT",
        "allowed_values": [],
        "blocking": True,
        "priority": "P0",
    }
    gap = {
        "kind": "METRIC_JUSTIFICATION",
        "thread_index": 0,
        "reason": "正式验收阈值缺失。",
        "blocking": True,
        "suggested_question": "请补充验收阈值。",
    }
    skeleton["user_questions"] = [copy.deepcopy(question)]
    skeleton["evidence_gaps"] = [copy.deepcopy(gap)]
    design = _flat_design_output(envelope)
    design["user_questions"] = [copy.deepcopy(question), copy.deepcopy(question)]
    design["evidence_gaps"] = [copy.deepcopy(gap), copy.deepcopy(gap)]
    raw_design = copy.deepcopy(design)
    gateway = _FakeArgumentStageGateway([skeleton, design])

    result = asyncio.run(orchestrate_argument_architecture_two_stage(
        envelope, stage_gateway=gateway, pack=PACK, max_stage_attempts=1
    ))

    assert design == raw_design
    assert result["design"]["user_questions"] == []
    assert result["design"]["evidence_gaps"] == []
    assert result["authored_state"]["user_questions"] == result["skeleton"]["user_questions"]
    assert result["authored_state"]["evidence_gaps"] == skeleton["evidence_gaps"]
    assert [call["stage"] for call in gateway.calls] == [
        ARGUMENT_SKELETON_STAGE,
        ARGUMENT_DESIGN_STAGE,
    ]


@pytest.mark.parametrize("value", ["NULL", "Null", " null "])
def test_argument_two_stage_preserves_non_exact_null_strings(value):
    envelope = _argument_envelope_with_evidence()
    skeleton = _flat_skeleton_output(envelope)
    design = _flat_design_output(envelope)
    design["cannot_proceed_reason"] = value
    gateway = _FakeArgumentStageGateway([skeleton, design])

    result = asyncio.run(orchestrate_argument_architecture_two_stage(
        envelope, stage_gateway=gateway, pack=PACK, max_stage_attempts=1
    ))

    assert result["design"]["cannot_proceed_reason"] == value


@pytest.mark.parametrize("broken", ["item_wrapper", "lifted_field", "wrong_field", "cardinality"])
def test_argument_two_stage_step4a_skeleton_failures_are_stage_local_and_never_call_design(broken):
    envelope = _argument_envelope_with_evidence()
    skeleton = _flat_skeleton_output(envelope)
    if broken == "item_wrapper":
        candidate = {"item": [skeleton]}
    else:
        candidate = copy.deepcopy(skeleton)
        if broken == "lifted_field":
            value = candidate["research_threads"][0].pop("gap_statement")
            candidate["gap_statement"] = value
        elif broken == "wrong_field":
            candidate["research_threads"][0]["gap"] = candidate["research_threads"][0].pop("gap_statement")
        elif broken == "cardinality":
            candidate["research_threads"] = [
                copy.deepcopy(candidate["research_threads"][0]) for _ in range(5)
            ]
    gateway = _FakeArgumentStageGateway([candidate, _flat_design_output(envelope)])

    with pytest.raises(ArgumentStageContractError) as raised:
        asyncio.run(orchestrate_argument_architecture_two_stage(
            envelope, stage_gateway=gateway, pack=PACK, max_stage_attempts=1
        ))

    assert raised.value.stage == ARGUMENT_SKELETON_STAGE
    assert raised.value.phase == "structure_validation"
    assert len(gateway.calls) == 1


def test_argument_two_stage_step4a_skeleton_unknown_evidence_is_dropped_deterministically():
    envelope = _argument_envelope_with_evidence()
    skeleton = _flat_skeleton_output(envelope)
    skeleton["central_proposition"]["evidence_ids"] = ["EV-NOT-PRESENT"]
    gateway = _FakeArgumentStageGateway([skeleton, _flat_design_output(envelope)])

    result = asyncio.run(orchestrate_argument_architecture_two_stage(
        envelope, stage_gateway=gateway, pack=PACK, max_stage_attempts=1
    ))

    assert result["skeleton"]["central_proposition"]["evidence_ids"] == []
    assert len(gateway.calls) == 2


def test_argument_two_stage_step4a_design_reference_failure_keeps_skeleton_frozen():
    envelope = _argument_envelope_with_evidence()
    skeleton = _flat_skeleton_output(envelope)
    design = _flat_design_output(envelope)
    design["methods"][0]["work_package_index"] = 9
    original_skeleton = copy.deepcopy(skeleton)
    gateway = _FakeArgumentStageGateway([skeleton, design])

    with pytest.raises(ArgumentStageContractError) as raised:
        asyncio.run(orchestrate_argument_architecture_two_stage(
            envelope, stage_gateway=gateway, pack=PACK, max_stage_attempts=1
        ))

    assert raised.value.stage == ARGUMENT_DESIGN_STAGE
    assert raised.value.phase == "reference_validation"
    assert len(gateway.calls) == 2
    assert gateway.calls[1]["model_input"]["frozen_skeleton"] == original_skeleton
    assert skeleton == original_skeleton


def test_argument_two_stage_step4a_design_unknown_evidence_is_dropped_deterministically():
    envelope = _argument_envelope_with_evidence()
    skeleton = _flat_skeleton_output(envelope)
    design = _flat_design_output(envelope)
    design["methods"][0]["evidence_ids"] = ["EV-NOT-PRESENT"]
    gateway = _FakeArgumentStageGateway([skeleton, design])

    result = asyncio.run(orchestrate_argument_architecture_two_stage(
        envelope, stage_gateway=gateway, pack=PACK, max_stage_attempts=1
    ))

    assert result["design"]["methods"][0]["evidence_ids"] == []
    assert len(gateway.calls) == 2


def test_argument_two_stage_step4a_conflicting_stage_blockers_fail_at_design_boundary():
    envelope = _argument_envelope_with_evidence()
    skeleton = _flat_skeleton_output(envelope)
    design = _flat_design_output(envelope)
    skeleton["cannot_proceed_reason"] = "Skeleton blocker"
    design["cannot_proceed_reason"] = "Design blocker"
    gateway = _FakeArgumentStageGateway([skeleton, design])

    with pytest.raises(ArgumentStageContractError) as raised:
        asyncio.run(orchestrate_argument_architecture_two_stage(
            envelope, stage_gateway=gateway, pack=PACK, max_stage_attempts=1
        ))

    assert raised.value.stage == ARGUMENT_DESIGN_STAGE
    assert raised.value.phase == "cross_stage_validation"
    assert any("conflicts with frozen_skeleton" in error for error in raised.value.errors)
    assert len(gateway.calls) == 2


def test_argument_two_stage_step4a_skeleton_full_retry_does_not_call_design_early():
    envelope = _argument_envelope_with_evidence()
    valid_skeleton = _flat_skeleton_output(envelope)
    broken_skeleton = copy.deepcopy(valid_skeleton)
    broken_skeleton["research_threads"] = {"item": broken_skeleton["research_threads"]}
    design = _flat_design_output(envelope)
    gateway = _FakeArgumentStageGateway([
        broken_skeleton,
        valid_skeleton,
        design,
    ])

    result = asyncio.run(orchestrate_argument_architecture_two_stage(
        envelope, stage_gateway=gateway, pack=PACK, max_stage_attempts=2
    ))

    assert [call["stage"] for call in gateway.calls] == [
        ARGUMENT_SKELETON_STAGE,
        ARGUMENT_SKELETON_STAGE,
        ARGUMENT_DESIGN_STAGE,
    ]
    retry = gateway.calls[1]["retry_context"]
    assert retry["attempt"] == 2
    assert retry["recovery_mode"] == "FULL_STAGE_RETRY"
    assert retry["validation_errors"]
    assert "previous_candidate" not in retry
    assert "repair_targets" not in gateway.calls[1]["model_input"]
    assert gateway.calls[2]["model_input"]["frozen_skeleton"] == valid_skeleton
    assert result["skeleton"] == valid_skeleton


def test_argument_two_stage_step4a_design_full_retry_freezes_skeleton_and_carries_errors():
    envelope = _argument_envelope_with_evidence()
    skeleton = _flat_skeleton_output(envelope)
    broken_design = _flat_design_output(envelope)
    broken_design["methods"][0]["work_package_index"] = 9
    valid_design = _flat_design_output(envelope)
    gateway = _FakeArgumentStageGateway([
        skeleton,
        broken_design,
        valid_design,
    ])

    result = asyncio.run(orchestrate_argument_architecture_two_stage(
        envelope, stage_gateway=gateway, pack=PACK, max_stage_attempts=2
    ))

    assert [call["stage"] for call in gateway.calls] == [
        ARGUMENT_SKELETON_STAGE,
        ARGUMENT_DESIGN_STAGE,
        ARGUMENT_DESIGN_STAGE,
    ]
    assert gateway.calls[1]["model_input"]["frozen_skeleton"] == skeleton
    retry = gateway.calls[2]["retry_context"]
    assert retry["attempt"] == 2
    assert retry["recovery_mode"] == "FULL_STAGE_RETRY"
    assert "previous_candidate" not in retry
    assert gateway.calls[2]["model_input"]["frozen_skeleton"] == skeleton
    assert any(
        "unresolved parent index" in error
        for error in retry["validation_errors"]
    )
    assert result["design"] == valid_design


def test_argument_two_stage_full_design_retry_never_merges_the_failed_draft():
    envelope = _argument_envelope_with_evidence()
    skeleton = _flat_skeleton_output(envelope)
    broken_design = _flat_design_output(envelope)
    broken_design["work_packages"][0]["statement"] = "Keep the valid prior work package."
    broken_design["methods"][0]["work_package_index"] = 9

    regenerated_design = _flat_design_output(envelope)
    regenerated_design["work_packages"][0]["statement"] = "Do not overwrite prior valid content."
    regenerated_design["methods"][0]["statement"] = "Use the repaired method row."
    gateway = _FakeArgumentStageGateway(
        [
            skeleton,
            broken_design,
            regenerated_design,
        ]
    )

    result = asyncio.run(
        orchestrate_argument_architecture_two_stage(envelope, stage_gateway=gateway, pack=PACK, max_stage_attempts=2)
    )

    assert result["design"] == regenerated_design
    assert result["design"]["work_packages"][0]["statement"] == (
        "Do not overwrite prior valid content."
    )


def test_argument_two_stage_full_design_retry_replaces_the_failed_draft_whole():
    envelope = _argument_envelope_with_evidence()
    skeleton = _flat_skeleton_output(envelope)
    broken_design = _flat_design_output(envelope)
    broken_design["work_packages"] = []
    regenerated_design = _flat_design_output(envelope)
    regenerated_design["foundation"] = []
    regenerated_design["foundation_supports"] = []
    gateway = _FakeArgumentStageGateway(
        [
            skeleton,
            broken_design,
            regenerated_design,
        ]
    )

    result = asyncio.run(
        orchestrate_argument_architecture_two_stage(envelope, stage_gateway=gateway, pack=PACK, max_stage_attempts=2)
    )

    assert result["design"] == regenerated_design


def test_argument_skeleton_retry_accepts_a_later_complete_candidate_whole():
    """Replay the failed-Draft/correct-response ordering from call-b4307."""

    envelope = _argument_envelope_with_evidence()
    broken = _flat_skeleton_output(envelope)
    broken["evidence_gaps"] = [
        {
            "kind": "FOUNDATION",
            "thread_index": 0,
            "reason": "Known valid gap.",
            "blocking": True,
            "suggested_question": "Please provide foundation evidence.",
        },
        {
            "kind": "METRIC_JUSTIFICATION",
            "thread_index": 4,
            "reason": "Invalid prior Draft gap.",
            "blocking": True,
            "suggested_question": "Please provide metric definitions.",
        },
    ]
    corrected = _flat_skeleton_output(envelope)
    corrected["evidence_gaps"] = [
        {
            "kind": "METRIC_JUSTIFICATION",
            "thread_index": 0,
            "reason": "Complete regenerated metric gap.",
            "blocking": True,
            "suggested_question": "Please confirm metric definitions.",
        },
        {
            "kind": "FOUNDATION",
            "thread_index": 0,
            "reason": "Complete regenerated foundation gap.",
            "blocking": True,
            "suggested_question": "Please confirm foundation evidence.",
        },
    ]
    design = _flat_design_output(envelope)
    gateway = _FakeArgumentStageGateway([broken, corrected, design])

    result = asyncio.run(
        orchestrate_argument_architecture_two_stage(
            envelope, stage_gateway=gateway, pack=PACK, max_stage_attempts=2
        )
    )

    assert result["skeleton"]["research_threads"] == corrected["research_threads"]
    assert result["skeleton"]["evidence_gaps"] == corrected["evidence_gaps"]
    assert all(
        item.get("reason") != "Invalid prior Draft gap."
        for item in result["skeleton"]["evidence_gaps"]
    )
    assert result["skeleton"]["evidence_gaps"][0]["thread_index"] == 0
    assert gateway.calls[1]["retry_context"]["validation_errors"] == [
        "/evidence_gaps/1/thread_index: out of range"
    ]


def test_argument_two_stage_later_invalid_full_response_cannot_corrupt_valid_rows():
    envelope = _argument_envelope_with_evidence()
    skeleton = _flat_skeleton_output(envelope)
    broken_design = _flat_design_output(envelope)
    broken_design["work_packages"][0]["statement"] = "Stable valid content."
    broken_design["methods"][0]["work_package_index"] = 9

    still_invalid = _flat_design_output(envelope)
    still_invalid["work_packages"][0]["statement"] = "Rejected overwrite."
    still_invalid["methods"][0]["work_package_index"] = 8
    valid_design = _flat_design_output(envelope)
    valid_design["work_packages"][0]["statement"] = "Another rejected overwrite."
    gateway = _FakeArgumentStageGateway(
        [
            skeleton,
            broken_design,
            still_invalid,
            valid_design,
        ]
    )

    result = asyncio.run(
        orchestrate_argument_architecture_two_stage(envelope, stage_gateway=gateway, pack=PACK, max_stage_attempts=3)
    )

    assert result["design"] == valid_design
    assert result["design"]["work_packages"][0]["statement"] == (
        "Another rejected overwrite."
    )


def test_argument_two_stage_step4a_exhausted_skeleton_retry_never_calls_design():
    envelope = _argument_envelope_with_evidence()
    broken = {"item": [_flat_skeleton_output(envelope)]}
    gateway = _FakeArgumentStageGateway([
        broken,
        broken,
        _flat_design_output(envelope),
    ])

    with pytest.raises(ArgumentStageContractError) as raised:
        asyncio.run(orchestrate_argument_architecture_two_stage(
            envelope, stage_gateway=gateway, pack=PACK, max_stage_attempts=2
        ))

    assert raised.value.stage == ARGUMENT_SKELETON_STAGE
    assert [call["stage"] for call in gateway.calls] == [
        ARGUMENT_SKELETON_STAGE, ARGUMENT_SKELETON_STAGE
    ]
    assert gateway.calls[1]["retry_context"]["recovery_mode"] == "FULL_STAGE_RETRY"
    assert "previous_candidate" not in gateway.calls[1]["retry_context"]
    assert raised.value.candidate == {
        "evidence_gaps": [],
        "user_questions": [],
        "cannot_proceed_reason": None,
    }

def test_argument_two_stage_step4a_stage_budgets_are_local_and_bounded():
    assert argument_stage_desired_output_tokens(ARGUMENT_SKELETON_STAGE) == 8_192
    assert argument_stage_desired_output_tokens(ARGUMENT_DESIGN_STAGE) == 65_536
    with pytest.raises(ValueError, match="Unknown Argument stage"):
        argument_stage_desired_output_tokens("FULL_ARGUMENT")


def test_argument_two_stage_step4a_flat_contract_cardinality_limits_are_explicit():
    skeleton_schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "prompt_pack"
            / "schemas"
            / "model"
            / "argument_skeleton_model_output.schema.json"
        ).read_text(encoding="utf-8")
    )
    design_schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "prompt_pack"
            / "schemas"
            / "model"
            / "argument_design_model_output.schema.json"
        ).read_text(encoding="utf-8")
    )

    assert skeleton_schema["properties"]["research_threads"]["maxItems"] == 4
    assert {
        name: design_schema["properties"][name]["maxItems"]
        for name in (
            "work_packages",
            "methods",
            "theoretical_properties",
            "evaluations",
            "baselines",
            "ablations",
            "innovations",
            "foundation",
        )
    } == {
        "work_packages": 24,
        "methods": 48,
        "theoretical_properties": 96,
        "evaluations": 96,
        "baselines": 192,
        "ablations": 192,
        "innovations": 32,
        "foundation": 32,
    }


def test_argument_stage_schema_errors_are_stable_after_sorted_json_replay():
    envelope = _argument_envelope_with_evidence()
    candidate = _flat_skeleton_output(envelope)
    candidate["research_threads"][0]["limitation_mechanism_evidence_ids"] = {
        "item": "E1",
        "nested": {
            "question_statement": "question",
            "answerability": "TESTABLE",
        },
    }
    replayed = json.loads(
        json.dumps(candidate, ensure_ascii=False, sort_keys=True)
    )

    live_errors = argument_skeleton_model_output_errors(candidate)
    replay_errors = argument_skeleton_model_output_errors(replayed)

    assert live_errors == replay_errors
    assert any(
        '"answerability":"TESTABLE"' in error
        and '"question_statement":"question"' in error
        for error in live_errors
    )




def test_argument_two_stage_cross_stage_readiness_conflict_is_normalized_locally():
    envelope = _argument_envelope_with_evidence()
    skeleton = _flat_skeleton_output(envelope)
    skeleton["user_questions"] = [{
        "target_area": "RESEARCH_DESIGN",
        "question_type": "MISSING_INFORMATION",
        "question": "请补充正式验收阈值。",
        "reason": "验收阈值尚未确认。",
        "answer_shape": "OBJECT",
        "allowed_values": [],
        "blocking": True,
        "priority": "P0",
    }]
    broken_design = _flat_design_output(envelope)
    broken_design["cannot_proceed_reason"] = "验收阈值尚未确认，当前不能继续。"
    broken_design["user_questions"] = [copy.deepcopy(skeleton["user_questions"][0])]
    gateway = _FakeArgumentStageGateway([skeleton, broken_design])
    result = asyncio.run(orchestrate_argument_architecture_two_stage(
        envelope, stage_gateway=gateway, pack=PACK, max_stage_attempts=2
    ))

    assert [call["stage"] for call in gateway.calls] == [
        ARGUMENT_SKELETON_STAGE, ARGUMENT_DESIGN_STAGE
    ]
    assert gateway.calls[1]["model_input"]["frozen_skeleton"] == result["skeleton"]
    assert result["design"]["cannot_proceed_reason"] is None
    assert result["design"]["user_questions"] == []
    assert PACK.validate_model("P-ARGUMENT-ARCHITECTURE", "output", result["authored_state"]) == []


def test_argument_skeleton_prompt_declares_readiness_null_literal_contract():
    prompt = argument_stage_prompt_text(ARGUMENT_SKELETON_STAGE)
    assert "blocking=true" in prompt
    assert "cannot_proceed_reason" in prompt
    assert "JSON 空值" in prompt
    assert "无引号" in prompt
    assert '字符串 `\"null\"`' in prompt


@pytest.mark.parametrize(
    "stage", [ARGUMENT_SKELETON_STAGE, ARGUMENT_DESIGN_STAGE]
)
def test_argument_stage_prompt_requires_single_proposition_boolean_questions(stage):
    prompt = argument_stage_prompt_text(stage)
    assert "布尔用户问题只能确认一个可独立判断的命题" in prompt
    assert "复合问题必须拆分" in prompt
    assert "MISSING_INFORMATION" in prompt
    assert "STRING" in prompt


def test_argument_two_stage_skeleton_readiness_conflict_is_normalized_before_design():
    envelope = _argument_envelope_with_evidence()
    broken = _flat_skeleton_output(envelope)
    broken["cannot_proceed_reason"] = "需要用户补充信息。"
    broken["user_questions"] = [{
        "target_area": "RESEARCH_DESIGN",
        "question_type": "MISSING_INFORMATION",
        "question": "请补充正式验收阈值。",
        "reason": "验收阈值尚未确认。",
        "answer_shape": "OBJECT",
        "allowed_values": [],
        "blocking": True,
        "priority": "P0",
    }]
    design = _flat_design_output(envelope)
    gateway = _FakeArgumentStageGateway([broken, design])

    result = asyncio.run(orchestrate_argument_architecture_two_stage(
        envelope, stage_gateway=gateway, pack=PACK, max_stage_attempts=2
    ))
    assert [call["stage"] for call in gateway.calls] == [
        ARGUMENT_SKELETON_STAGE, ARGUMENT_DESIGN_STAGE
    ]
    assert result["skeleton"]["cannot_proceed_reason"] is None
    assert result["skeleton"]["user_questions"][0]["answer_shape"] == "STRING"
    assert result["skeleton"]["user_questions"][0]["blocking"] is True


def test_argument_authored_state_assembler_dedupes_only_deterministic_question_and_gap_identity():
    envelope = _argument_envelope_with_evidence()
    skeleton = _flat_skeleton_output(envelope)
    design = _flat_design_output(envelope)
    skeleton["user_questions"] = [{
        "target_area": "RESEARCH_DESIGN",
        "question_type": "MISSING_INFORMATION",
        "question": "请补充正式验收阈值。",
        "reason": "Skeleton owns this question.",
        "answer_shape": "OBJECT",
        "allowed_values": [],
        "blocking": False,
        "priority": "P1",
    }]
    duplicate_question = copy.deepcopy(skeleton["user_questions"][0])
    duplicate_question["target_area"] = "OTHER"
    duplicate_question["reason"] = "Design must not override the same question."
    design["user_questions"] = [duplicate_question]

    skeleton["evidence_gaps"] = [{
        "kind": "METRIC_JUSTIFICATION",
        "thread_index": 0,
        "reason": "正式验收阈值缺失。",
        "blocking": False,
        "suggested_question": "请补充正式验收阈值。",
    }]
    design["evidence_gaps"] = [
        {
            "kind": "METRIC_JUSTIFICATION",
            "thread_index": 0,
            "reason": "同一缺口的重复表述。",
            "blocking": False,
            "suggested_question": "请补充正式验收阈值！",
        },
        {
            "kind": "METRIC_JUSTIFICATION",
            "thread_index": 0,
            "reason": "不同的统计检验信息缺失。",
            "blocking": False,
            "suggested_question": "请补充统计检验方法。",
        },
    ]

    authored = assemble_argument_authored_state(skeleton, design)
    assert authored["user_questions"] == skeleton["user_questions"]
    assert len(authored["evidence_gaps"]) == 2
    assert authored["evidence_gaps"][0] == skeleton["evidence_gaps"][0]
    assert authored["evidence_gaps"][1]["suggested_question"] == "请补充统计检验方法。"



def test_argument_two_stage_design_rejects_unqualified_foundation_before_assembly():
    envelope = _argument_envelope_with_evidence()
    envelope["payload"]["confirmed_facts"].append({
        "claim_id": "E-UNKNOWN-FOUNDATION",
        "claim_text": "团队基础信息待补，不得作为已确认团队基础。",
        "claim_type": "FACT",
        "subject_id": None,
        "temporal_status": "TIME_INDEPENDENT",
        "qualifiers": ["UNKNOWN"],
        "numeric_values": [],
        "source_refs": [],
        "knowledge_status": "UNKNOWN",
        "security_level": "INTERNAL",
    })
    skeleton = _flat_skeleton_output(envelope)
    broken_design = _flat_design_output(envelope)
    broken_design["foundation"] = [{
        "thread_index": 0,
        "foundation_index": 0,
        "statement": "团队基础为UNKNOWN。",
        "evidence_ids": ["E-UNKNOWN-FOUNDATION"],
    }]
    broken_design["foundation_supports"] = [{
        "thread_index": 0,
        "foundation_index": 0,
        "work_package_index": 0,
        "method_index": None,
    }]
    valid_design = _flat_design_output(envelope)
    gateway = _FakeArgumentStageGateway([
        skeleton,
        broken_design,
        valid_design,
    ])

    result = asyncio.run(orchestrate_argument_architecture_two_stage(
        envelope, stage_gateway=gateway, pack=PACK, max_stage_attempts=2
    ))
    retry_errors = gateway.calls[2]["retry_context"]["validation_errors"]
    assert any("foundation_eligible_evidence_ids" in error for error in retry_errors)
    assert gateway.calls[2]["stage"] == ARGUMENT_DESIGN_STAGE
    assert "previous_candidate" not in gateway.calls[2]["retry_context"]
    assert gateway.calls[1]["model_input"]["foundation_eligible_evidence_ids"]
    assert "E-UNKNOWN-FOUNDATION" not in gateway.calls[1]["model_input"]["foundation_eligible_evidence_ids"]
    assert result["design"]["foundation"] == []
    assert result["design"]["foundation_supports"] == []



def test_argument_two_stage_design_combines_foundation_and_cross_stage_feedback_in_one_retry():
    envelope = _argument_envelope_with_evidence()
    envelope["payload"]["confirmed_facts"].append({
        "claim_id": "E-UNKNOWN-COMBINED",
        "claim_text": "团队基础未知。",
        "claim_type": "FACT",
        "subject_id": None,
        "temporal_status": "TIME_INDEPENDENT",
        "qualifiers": ["UNKNOWN"],
        "numeric_values": [],
        "source_refs": [],
        "knowledge_status": "UNKNOWN",
        "security_level": "INTERNAL",
    })
    skeleton = _flat_skeleton_output(envelope)
    skeleton["user_questions"] = [{
        "target_area": "FOUNDATION_EVIDENCE",
        "question_type": "MISSING_INFORMATION",
        "question": "请补充团队已有基础。",
        "reason": "团队基础尚未确认。",
        "answer_shape": "OBJECT",
        "allowed_values": [],
        "blocking": True,
        "priority": "P0",
    }]
    skeleton["evidence_gaps"] = [{
        "kind": "FOUNDATION",
        "thread_index": 0,
        "reason": "团队基础尚未确认。",
        "blocking": True,
        "suggested_question": "请补充团队已有基础。",
    }]

    broken = _flat_design_output(envelope)
    broken["foundation"] = [{
        "thread_index": 0,
        "foundation_index": 0,
        "statement": "团队基础为UNKNOWN。",
        "evidence_ids": ["E-UNKNOWN-COMBINED"],
    }]
    broken["foundation_supports"] = [{
        "thread_index": 0,
        "foundation_index": 0,
        "work_package_index": 0,
        "method_index": None,
    }]
    broken["cannot_proceed_reason"] = "团队基础尚未确认，当前不能继续。"
    broken["user_questions"] = [copy.deepcopy(skeleton["user_questions"][0])]
    broken["evidence_gaps"] = [copy.deepcopy(skeleton["evidence_gaps"][0])]
    valid = _flat_design_output(envelope)
    gateway = _FakeArgumentStageGateway([
        skeleton,
        broken,
        valid,
    ])

    result = asyncio.run(orchestrate_argument_architecture_two_stage(
        envelope, stage_gateway=gateway, pack=PACK, max_stage_attempts=2
    ))
    retry_errors = gateway.calls[2]["retry_context"]["validation_errors"]
    assert any("foundation_eligible_evidence_ids" in error for error in retry_errors)
    assert gateway.calls[2]["stage"] == ARGUMENT_DESIGN_STAGE
    assert "previous_candidate" not in gateway.calls[2]["retry_context"]
    assert not any("cannot_proceed_reason" in error for error in retry_errors)
    assert not any("duplicate question indexes" in error for error in retry_errors)
    assert not any("duplicate gap indexes" in error for error in retry_errors)
    assert result["design"] == valid


def test_argument_two_stage_design_retry_receives_all_known_shape_and_cross_stage_errors_once():
    envelope = _argument_envelope_with_evidence()
    skeleton = _flat_skeleton_output(envelope)
    skeleton["user_questions"] = [{
        "target_area": "RESEARCH_DESIGN",
        "question_type": "MISSING_INFORMATION",
        "question": "请补充正式验收阈值。",
        "reason": "验收阈值尚未确认。",
        "answer_shape": "OBJECT",
        "allowed_values": [],
        "blocking": True,
        "priority": "P0",
    }]
    skeleton["evidence_gaps"] = [{
        "kind": "METRIC_JUSTIFICATION",
        "thread_index": 0,
        "reason": "正式验收阈值缺失。",
        "blocking": True,
        "suggested_question": "请补充正式验收阈值。",
    }]

    broken = _flat_design_output(envelope)
    # Multiple independent shape defects: enough to exceed the historical six-error slice.
    broken["work_packages"].extend([{}, {}, {}, {}])
    broken["methods"].extend([{}, {}, {}, {}])
    # A separate, structurally well-typed reference defect must be reported in the same attempt.
    broken["methods"][0]["work_package_index"] = 9
    # Simultaneous cross-stage defects already knowable from the same candidate.
    broken["cannot_proceed_reason"] = "验收阈值尚未确认，当前不能继续。"
    broken["user_questions"] = [copy.deepcopy(skeleton["user_questions"][0])]
    broken["evidence_gaps"] = [copy.deepcopy(skeleton["evidence_gaps"][0])]

    valid = _flat_design_output(envelope)
    gateway = _FakeArgumentStageGateway([
        skeleton,
        broken,
        valid,
    ])
    result = asyncio.run(orchestrate_argument_architecture_two_stage(
        envelope, stage_gateway=gateway, pack=PACK, max_stage_attempts=3
    ))

    retry_errors = gateway.calls[2]["retry_context"]["validation_errors"]
    assert len(retry_errors) > 6
    assert any("required property" in error for error in retry_errors)
    assert any("unresolved parent index" in error for error in retry_errors)
    assert gateway.calls[2]["stage"] == ARGUMENT_DESIGN_STAGE
    assert "previous_candidate" not in gateway.calls[2]["retry_context"]
    assert "repair_targets" not in gateway.calls[2]["model_input"]
    assert not any("cannot_proceed_reason" in error for error in retry_errors)
    assert not any("duplicate question indexes" in error for error in retry_errors)
    assert not any("duplicate gap indexes" in error for error in retry_errors)
    assert result["design"] == valid
    assert PACK.validate_model("P-ARGUMENT-ARCHITECTURE", "output", result["authored_state"]) == []


def test_argument_two_stage_skeleton_normalizes_deterministic_defects_without_retry():
    envelope = _argument_envelope_with_evidence()
    broken = _flat_skeleton_output(envelope)
    broken["unexpected_stage_field"] = "shape defect"
    broken["central_proposition"]["evidence_ids"] = ["EV-NOT-PRESENT"]
    question = {
        "target_area": "RESEARCH_DESIGN",
        "question_type": "MISSING_INFORMATION",
        "question": "请补充正式验收阈值。",
        "reason": "验收阈值尚未确认。",
        "answer_shape": "OBJECT",
        "allowed_values": [],
        "blocking": True,
        "priority": "P0",
    }
    broken["cannot_proceed_reason"] = "需要用户补充信息。"
    broken["user_questions"] = [copy.deepcopy(question), copy.deepcopy(question)]

    design = _flat_design_output(envelope)
    gateway = _FakeArgumentStageGateway([broken, design])
    result = asyncio.run(orchestrate_argument_architecture_two_stage(
        envelope, stage_gateway=gateway, pack=PACK, max_stage_attempts=2
    ))

    assert [call["stage"] for call in gateway.calls] == [
        ARGUMENT_SKELETON_STAGE,
        ARGUMENT_DESIGN_STAGE,
    ]
    assert result["skeleton"]["central_proposition"]["evidence_ids"] == []
    assert result["skeleton"]["cannot_proceed_reason"] is None
    assert len(result["skeleton"]["user_questions"]) == 1
