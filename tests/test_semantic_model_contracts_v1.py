from __future__ import annotations

import asyncio
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pytest
from types import SimpleNamespace

from app.deterministic_repair import apply_deterministic_contract_repairs
from app.executor import PromptExecutor
from app.llm import LLMResult
from app.model_semantic_contracts import (
    _argument_quantified_support,
    _critic_chain_checks,
    build_argument_architecture_critic_model_input,
    build_argument_architecture_model_input,
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

