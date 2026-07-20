from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from .util import new_id, sha256_text
from .logistics_application_content import SECTION_TITLES as LOGISTICS_SECTION_TITLES, REF_CATALOG as LOGISTICS_REF_CATALOG, blocks_for as logistics_blocks_for
from .transport_optimization_application_content import SECTION_TITLES as TRANSPORT_SECTION_TITLES, REF_CATALOG as TRANSPORT_REF_CATALOG, blocks_for as transport_blocks_for


class SimulatedLLM:
    """Deterministic local generator used to simulate multi-agent runs.

    It starts from the replay output for each prompt to preserve schema fidelity,
    then patches key fields so the output reflects the active project/section.
    """

    def __init__(self, pack):
        self.pack = pack
        self.figure_dir = Path(__file__).resolve().parents[1] / "data" / "figures"

    def invoke(self, prompt_id: str, envelope: dict[str, Any]) -> dict[str, Any]:
        base = self.pack.replay_output(prompt_id, "normal")
        handler = getattr(self, f"_handle_{prompt_id.removeprefix('P-').lower().replace('-', '_')}", None)
        if handler is None:
            return base
        return handler(copy.deepcopy(base), envelope)

    @staticmethod
    def _project_name(envelope: dict[str, Any]) -> str:
        return envelope.get("payload", {}).get("project_name") or envelope.get("scope", {}).get("project_id") or "本项目"

    @staticmethod
    def _section(envelope: dict[str, Any]) -> dict[str, Any]:
        return envelope.get("payload", {}).get("source_section", {})

    @staticmethod
    def _clean_title(title: str) -> str:
        return re.sub(r"^[#\s]+", "", title or "").strip()

    @staticmethod
    def _canonical_argument_role(value: Any) -> str:
        """Translate legacy replay labels into the frozen argument-role vocabulary.

        Production gateways remain schema-strict.  This adapter is deliberately
        limited to the deterministic simulator, whose replay fixtures predate the
        shared role schema and may still contain Chinese display labels.
        """
        raw = str(value or "EVIDENCE").strip()
        allowed = {
            "CONTEXT", "PROBLEM", "EVIDENCE", "GAP",
            "LIMITATION_MECHANISM", "CENTRAL_CLAIM", "RESEARCH_QUESTION",
            "METHOD", "WARRANT", "COUNTERARGUMENT", "BOUNDARY",
            "EVALUATION", "CONTRIBUTION", "TRANSITION", "ABLATION",
            "ACCEPTANCE", "ALGORITHM", "BASELINE", "CLOSEST_WORK",
            "COMPARISON", "COMPARISON_RULE", "COMPENSATION_PLAN",
            "CONFIRMED_CAPABILITY", "DATASET", "DATA_FLOW",
            "DEGRADATION_BASELINE", "DELIVERABLE", "DEPENDENCY",
            "FEASIBILITY_WARRANT", "FEEDBACK", "FORMALIZATION", "INPUT",
            "LIMITATION", "MECHANISM", "METRIC", "MILESTONE",
            "MITIGATION", "NEW_MECHANISM", "OBJECTIVE", "OUTPUT",
            "RISK", "RISK_CONTROL", "RQ_CLOSURE", "SPLIT",
            "SYNTHESIS", "TECHNICAL_DIFFICULTY", "VALIDITY_THREAT",
            "WORK_PACKAGE",
        }
        if raw in allowed:
            return raw
        aliases = {
            "总述": "CONTEXT", "本节总述": "CONTEXT",
            "内容总述": "CONTEXT", "路线总述": "CONTEXT",
            "关键技术总述": "CONTEXT", "背景": "CONTEXT",
            "问题": "PROBLEM", "证据": "EVIDENCE", "差距": "GAP",
            "方法": "METHOD", "机制": "MECHANISM", "目标": "OBJECTIVE",
            "评价": "EVALUATION", "贡献": "CONTRIBUTION",
            "过渡": "TRANSITION", "边界": "BOUNDARY", "风险": "RISK",
            "输出": "OUTPUT", "里程碑": "MILESTONE",
        }
        return aliases.get(raw, "EVIDENCE")

    @classmethod
    def _is_transport_project(cls, envelope: dict[str, Any]) -> bool:
        project_name = cls._project_name(envelope)
        payload_text = json.dumps(envelope.get("payload", {}), ensure_ascii=False)
        markers = ["物流场景", "运输方案优化", "车辆路径", "多式联运"]
        return any(marker in project_name or marker in payload_text for marker in markers)

    @classmethod
    def _section_titles(cls, envelope: dict[str, Any]) -> list[str]:
        if cls._is_transport_project(envelope):
            return TRANSPORT_SECTION_TITLES
        return LOGISTICS_SECTION_TITLES

    @classmethod
    def _catalog(cls, envelope: dict[str, Any]) -> list[dict[str, Any]]:
        if cls._is_transport_project(envelope):
            return TRANSPORT_REF_CATALOG
        return LOGISTICS_REF_CATALOG

    @classmethod
    def _blocks_for(cls, title: str, envelope: dict[str, Any]) -> list[str]:
        if cls._is_transport_project(envelope):
            return transport_blocks_for(title)
        return logistics_blocks_for(title)

    @classmethod
    def _domain_term(cls, envelope: dict[str, Any]) -> str:
        if cls._is_transport_project(envelope):
            return "物流运输方案优化系统"
        return "后勤保障智能体"

    @classmethod
    def _research_queries(cls, envelope: dict[str, Any]) -> list[str]:
        if cls._is_transport_project(envelope):
            return [
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
        return [
            "logistics agent system survey 2023 2024",
            "multi-agent collaboration workflow orchestration logistics",
            "knowledge graph RAG enterprise operations",
            "AI planning scheduling dynamic replanning survey",
            "human in the loop autonomous agents benchmark",
        ]

    @staticmethod
    def _item_number(item: dict[str, Any], fallback: int) -> int:
        return int(item.get("reference_number") or item.get("id") or fallback)

    @staticmethod
    def _item_summary(item: dict[str, Any]) -> str:
        return str(item.get("content_text") or item.get("excerpt") or item.get("note") or item.get("title") or "公开来源")

    def _project_source_ref(self, envelope: dict[str, Any], *, preferred_roles: set[str] | None = None) -> dict[str, Any]:
        payload = envelope.get("payload", {})
        docs = payload.get("source_documents") or []
        role_map = {
            "APPLICATION_GUIDE": "APPLICATION_GUIDE",
            "PROJECT_BRIEF": "TASK_BOOK",
            "CURRENT_PROPOSAL": "CURRENT_PROPOSAL",
            "TECHNICAL_DESIGN": "TECHNICAL_MATERIAL",
            "EVIDENCE_MATERIAL": "EVIDENCE_MATERIAL",
            "REFERENCE_PROPOSAL": "REFERENCE_PROPOSAL",
        }
        for doc in docs:
            document_role = str(doc.get("document_role") or "")
            if preferred_roles is not None and document_role not in preferred_roles:
                continue
            for sec in doc.get("sections", []):
                text = str(sec.get("text") or "").strip()
                if text:
                    return {
                        "source_id": str(sec.get("section_id") or "source-section-001"),
                        "source_type": role_map.get(document_role, "HISTORICAL_DOCUMENT"),
                        "document_version_id": str(doc.get("version_id") or doc.get("document_id") or "document-version-001"),
                        "section_id": str(sec.get("section_id") or "source-section-001"),
                        "span_start": 0,
                        "span_end": min(len(text), 300),
                        "quoted_text": text[:300],
                        "source_hash": (str(sec.get("text_hash")) if str(sec.get("text_hash") or "") not in {"a" * 64, "0" * 64} else sha256_text(text)),
                        "authority_rank": 80,
                        "security_level": str(doc.get("security_level") or "INTERNAL"),
                    }
        project_name = self._project_name(envelope)
        return {
            "source_id": "user-confirmation-project-scope",
            "source_type": "USER_CONFIRMATION",
            "document_version_id": None,
            "section_id": None,
            "span_start": None,
            "span_end": None,
            "quoted_text": f"用户要求围绕{project_name}形成科研项目申请书并完善智能体系统。",
            "source_hash": sha256_text(project_name + "科研项目申请书"),
            "authority_rank": 100,
            "security_level": "INTERNAL",
        }

    @staticmethod
    def _quality_dimensions(passed: bool = True) -> list[dict[str, Any]]:
        dimensions = [
            "DOCUMENT_TYPE_FIT", "CENTRAL_THESIS", "ARGUMENT_CHAIN", "EVIDENCE_SUPPORT",
            "METHOD_SUBSTANCE", "INNOVATION_BASELINE", "FEASIBILITY_FOUNDATION",
            "METRIC_JUSTIFICATION", "SECTION_UNIQUENESS", "STYLE_AND_DENSITY",
            "PAGE_BUDGET", "CROSS_SECTION_CONSISTENCY",
        ]
        return [{
            "dimension": dimension,
            "score": 4.0 if passed else 2.0,
            "passed": passed,
            "evidence": ["已按结构化输入逐项检查。"],
            "required_action": None if passed else "补充缺失论证并重新审查。",
        } for dimension in dimensions]

    @staticmethod
    def _dimension_checks(dimensions: list[str], passed: bool = True) -> list[dict[str, Any]]:
        return [{
            "dimension": dimension,
            "passed": passed,
            "evidence": "已根据输入对象、关系与来源逐项核验。",
            "blocking_ids": [],
        } for dimension in dimensions]

    def _research_definition(self, envelope: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        source = self._project_source_ref(envelope)
        foundation_source = self._project_source_ref(
            envelope, preferred_roles={"EVIDENCE_MATERIAL", "TECHNICAL_DESIGN"}
        )
        has_foundation_evidence = foundation_source.get("source_type") in {"EVIDENCE_MATERIAL", "TECHNICAL_MATERIAL"}
        project_id = str(envelope.get("scope", {}).get("project_id") or "project-001")
        security_level = str(envelope.get("security_context", {}).get("project_security_level") or "INTERNAL")

        contents = [
            ("gap-001", "GAP", "STATE_GAP_ROOT_CAUSE", {"gap_type": "SCIENTIFIC", "description": "动态运输环境下，静态或全量重算方法难以同时控制方案质量、响应时延与计划扰动。", "affected_scenarios": ["scenario-dynamic-order", "scenario-traffic-change", "scenario-vehicle-failure"], "impact": "方案频繁变化并增加执行协调成本。"}),
            ("problem-001", "PROBLEM", "CORE_PROBLEMS", {"problem_class": "SCIENTIFIC", "statement": "如何在动态事件下联合刻画方案质量、求解时延和计划稳定性之间的关系？", "why_difficult": "事件影响具有局部传播与全局约束耦合特征。", "constraints": ["硬约束必须满足", "事件响应时限", "已有计划尽量保持"], "expected_breakthrough": "形成可比较的低扰动增量优化原理。"}),
            ("objective-001", "OBJECTIVE", "OBJECTIVES", {"statement": "建立动态运输方案的影响范围识别与低扰动增量优化方法。", "baseline_state": "现有方法主要采用全量重算或固定滚动窗口。", "target_state": "形成影响子图、稳定性代价和增量求解联合方法。", "success_definition": "在统一场景和基线下比较质量、时延和扰动指标。", "out_of_scope": ["把部署和运维细节作为核心研究内容"]}),
            ("wp-001", "WORK_PACKAGE", "RESEARCH_CONTENT", {"name": "业务语义到优化约束的可验证映射", "research_object": "不完备任务描述与运输约束", "inputs": ["任务语义", "资源台账", "业务规则"], "main_activities": ["约束本体建模", "歧义检测", "可行性校验"], "methods": ["method-knowledge-graph", "method-constraint-compiler", "method-consistency-check"], "outputs": ["约束模型", "校验规则"], "responsible_organization": None, "acceptance_refs": ["experiment-001"]}),
            ("wp-002", "WORK_PACKAGE", "RESEARCH_CONTENT", {"name": "动态事件下低扰动增量重规划", "research_object": "事件影响范围与计划稳定性", "inputs": ["当前方案", "动态事件", "约束模型"], "main_activities": ["影响子图识别", "局部模型更新", "稳定性代价优化"], "methods": ["method-incremental-optimization", "method-decomposition", "method-multiobjective"], "outputs": ["增量算法", "重规划策略"], "responsible_organization": None, "acceptance_refs": ["experiment-001"]}),
            ("method-001", "METHOD", "TECHNICAL_ROUTE", {"name": "影响子图约束下的低扰动增量优化", "method_type": "ALGORITHM", "purpose": "降低动态事件重规划时延并避免无关任务变化", "principle": "先识别事件影响子图，再在保持全局硬约束的条件下最小化局部调整代价。", "inputs": ["原方案", "事件", "约束图"], "outputs": ["新方案", "变化集合", "可行性证明"], "constraints": ["硬约束满足", "有限计算时间"], "selection_reason": "直接对应中心命题并可与全量重算比较。", "maturity": "PROPOSED"}),
            ("experiment-001", "EXPERIMENT", "TECHNICAL_ROUTE", {"name": "动态运输方案对照与消融实验", "purpose": "检验中心命题和各算法组件的作用", "test_object": "低扰动增量优化算法", "dataset_or_scenario": "公开VRP实例与可复现实验场景", "conditions": ["相同硬件", "相同时间预算", "多种事件强度"], "procedure": ["与全量重算和滚动优化比较", "移除影响子图组件", "移除稳定性代价组件"], "expected_evidence": ["目标差距", "响应时间", "方案扰动率", "硬约束满足率"]}),
            ("innovation-001", "INNOVATION", "INNOVATION", {"innovation_type": "METHOD", "existing_baseline": "全量重算与固定窗口滚动优化", "existing_limitation": "未显式联合建模事件影响范围和方案稳定性", "proposed_change": "引入影响子图与稳定性代价联合机制", "novel_mechanism": "按事件传播关系动态限定可调整变量并保持全局约束", "expected_advantage": "在相近方案质量下减少求解时间和非必要变更", "applicable_conditions": ["动态事件局部影响", "已有可行方案"], "confidence": "PROPOSED"}),
            ("deliverable-001", "DELIVERABLE", "OUTPUTS_AND_METRICS", {"deliverable_type": "ALGORITHM", "name": "低扰动增量优化算法与验证原型", "description": "算法、实验代码和可复现实验报告", "delivery_time": "项目末期", "acceptance_form": "代码、报告和对照实验"}),
            ("metric-001", "METRIC", "OUTPUTS_AND_METRICS", {"name": "计划扰动率", "object": "动态事件后的新旧方案差异", "metric_type": "PERFORMANCE", "baseline_value": None, "target_value": None, "comparison": "LESS_THAN", "unit": "%", "measurement_method": "统计发生任务、路径或时刻变化的对象比例", "test_dataset_or_scenario": "动态事件对照场景", "test_conditions": ["相同目标权重", "相同时间预算"], "verifier": "独立测试脚本"}),
            ("achievement-001", "ACHIEVEMENT", "RESEARCH_FOUNDATION", {"owner_type": "TEAM", "owner_name": "项目团队", "achievement_type": "PRELIMINARY_RESULT", "title": "输入材料中可定位的相关优化算法或原型验证成果" if has_foundation_evidence else "待补充与本课题直接相关的前期成果", "status": "COMPLETED" if has_foundation_evidence else "PLANNED", "date": None, "contribution": "为模型构建和原型实现提供可复用基础" if has_foundation_evidence else "尚未形成可核验结论", "project_relevance": "支撑增量优化和系统验证" if has_foundation_evidence else "需要负责人补充成果、原型、数据或预实验材料"}),
            ("capability-001", "CAPABILITY", "RESEARCH_FOUNDATION", {"owner_type": "TEAM", "owner_name": "项目团队", "capability": "组合优化、动态调度与原型实现", "current_status": "输入材料提供了可定位的能力证据" if has_foundation_evidence else "仅有能力方向描述，尚无可定位证据", "project_support": "支撑模型、算法、实验和原型验证" if has_foundation_evidence else "待确认", "limitations": [] if has_foundation_evidence else ["需补充与本课题直接对应的论文、项目、代码、数据或预实验材料"]}),
        ]
        items = []
        for item_id, item_type, domain, content in contents:
            is_foundation = item_type in {"ACHIEVEMENT", "CAPABILITY"}
            item_source_refs = [copy.deepcopy(foundation_source)] if is_foundation and has_foundation_evidence else ([] if is_foundation else [copy.deepcopy(source)])
            item = {
                "item_id": item_id, "item_type": item_type, "domain": domain, "content": content,
                "knowledge_status": ("DOCUMENT_EXTRACTED" if has_foundation_evidence else "UNKNOWN") if is_foundation else "CONFIRMED",
                "owner_ref": None, "source_refs": item_source_refs,
                "security_level": security_level, "locked": False,
                "confidence": ("HIGH" if has_foundation_evidence else "UNKNOWN") if is_foundation else "HIGH",
            }
            item["item_hash"] = sha256_text(json.dumps(item, ensure_ascii=False, sort_keys=True))
            items.append(item)
        by_id = {item["item_id"]: item for item in items}
        rel_specs = [
            ("gap-001", "GAP", "MOTIVATES", "problem-001", "PROBLEM"),
            ("problem-001", "PROBLEM", "ADDRESSED_BY", "objective-001", "OBJECTIVE"),
            ("objective-001", "OBJECTIVE", "DECOMPOSES_TO", "wp-001", "WORK_PACKAGE"),
            ("objective-001", "OBJECTIVE", "DECOMPOSES_TO", "wp-002", "WORK_PACKAGE"),
            ("wp-002", "WORK_PACKAGE", "IMPLEMENTED_BY", "method-001", "METHOD"),
            ("method-001", "METHOD", "VALIDATED_BY", "experiment-001", "EXPERIMENT"),
            ("wp-002", "WORK_PACKAGE", "PRODUCES", "deliverable-001", "DELIVERABLE"),
            ("deliverable-001", "DELIVERABLE", "MEASURED_BY", "metric-001", "METRIC"),
            ("achievement-001", "ACHIEVEMENT", "SUPPORTS", "objective-001", "OBJECTIVE"),
        ]
        relations = []
        for idx, (sid, st, rt, tid, tt) in enumerate(rel_specs, 1):
            foundation_relation = st in {"ACHIEVEMENT", "CAPABILITY"}
            rel = {"relation_id": f"relation-{idx:03d}", "source_item_id": sid, "source_item_type": st,
                   "relation_type": rt, "target_item_id": tid, "target_item_type": tt,
                   "status": ("CONFIRMED" if has_foundation_evidence else "CANDIDATE") if foundation_relation else "CONFIRMED",
                   "confidence": ("HIGH" if has_foundation_evidence else "UNKNOWN") if foundation_relation else "HIGH",
                   "source_refs": ([copy.deepcopy(foundation_source)] if has_foundation_evidence else []) if foundation_relation else [copy.deepcopy(source)],
                   "security_level": security_level}
            rel["relation_hash"] = sha256_text(json.dumps(rel, ensure_ascii=False, sort_keys=True))
            relations.append(rel)
        domains = sorted({item["domain"] for item in items})
        pd = {"schema_version": "2.0", "project_id": project_id, "version": 1, "parent_version_id": None,
              "items": items, "relations": relations,
              "domain_readiness": [{
                  "domain": d,
                  "completeness": 1.0 if d != "RESEARCH_FOUNDATION" or has_foundation_evidence else 0.4,
                  "confirmation_ratio": 1.0 if d != "RESEARCH_FOUNDATION" or has_foundation_evidence else 0.0,
                  "evidence_ratio": 1.0 if d != "RESEARCH_FOUNDATION" or has_foundation_evidence else 0.0,
                  "open_conflicts": 0,
                  "readiness": "READY" if d != "RESEARCH_FOUNDATION" or has_foundation_evidence else "NEED_USER_INPUT",
                  "missing_item_types": [] if d != "RESEARCH_FOUNDATION" or has_foundation_evidence else ["ACHIEVEMENT", "CAPABILITY"],
              } for d in domains],
              "open_conflict_ids": [], "status": "CONFIRMED", "security_level": security_level}
        pd["package_hash"] = sha256_text(json.dumps(pd, ensure_ascii=False, sort_keys=True))
        proposal_contract = {
            "contract_id": "proposal-contract-001", "document_type": "RESEARCH_PROPOSAL",
            "funding_scheme": "科研项目申请", "primary_evaluation_logic": "SCIENTIFIC_MERIT",
            "target_evaluators": ["同行评审专家"], "max_main_pages": 35, "max_core_research_questions": 3,
            "mandatory_sections": ["立项依据", "研究目标", "研究内容", "研究方案", "创新点", "研究基础"],
            "appendix_only_topics": ["部署脚本", "接口清单", "Prompt与Trace", "运行日志"],
            "forbidden_main_body_topics": ["安装步骤", "Manifest校验", "完整审计日志"], "status": "CONFIRMED",
        }
        argument_graph = {
            "graph_id": "argument-graph-001",
            "central_proposition": {"node_id": "prop-001", "statement": "通过显式识别动态事件的影响子图，并在全局硬约束下联合优化方案质量与稳定性，可以减少非必要计划变更和重规划时间。", "proposition_type": "TECHNICAL_PRINCIPLE", "falsifiable_or_comparable": True, "boundary_conditions": ["事件影响具有局部传播特征", "存在原始可行方案"], "source_refs": [copy.deepcopy(source)]},
            "research_questions": [
                {"node_id": "rq-001", "statement": "如何将不完备业务语义稳定映射为可验证的组合优化约束？", "question_type": "TECHNICAL", "linked_gap_ids": ["gap-001"], "answerability": "DESIGN_VERIFIABLE", "success_evidence": ["约束解析正确性与人工复核"]},
                {"node_id": "rq-002", "statement": "如何在动态事件下联合控制方案质量、求解时延和计划扰动？", "question_type": "SCIENTIFIC", "linked_gap_ids": ["gap-001"], "answerability": "COMPARABLE", "success_evidence": ["对照实验和消融实验"]},
            ],
            "scope_boundaries": {"in_scope": ["约束映射", "动态方案优化", "实验验证"], "out_of_scope": ["把部署运维作为主文研究问题"]},
            "nodes": [
                {"node_id": "gap-001", "node_type": "RESEARCH_GAP", "statement": contents[0][3]["description"], "status": "SUPPORTED", "source_refs": [copy.deepcopy(source)]},
                {"node_id": "prior-001", "node_type": "CLOSEST_PRIOR_WORK", "statement": "全量重算与固定窗口滚动优化是最接近的基线。", "status": "SUPPORTED", "source_refs": [copy.deepcopy(source)]},
                {"node_id": "objective-001", "node_type": "OBJECTIVE", "statement": contents[2][3]["statement"], "status": "PLANNED", "source_refs": [copy.deepcopy(source)]},
                {"node_id": "wp-001", "node_type": "WORK_PACKAGE", "statement": contents[3][3]["name"], "status": "PLANNED", "source_refs": [copy.deepcopy(source)]},
                {"node_id": "wp-002", "node_type": "WORK_PACKAGE", "statement": contents[4][3]["name"], "status": "PLANNED", "source_refs": [copy.deepcopy(source)]},
                {"node_id": "method-001", "node_type": "FORMAL_MODEL", "statement": contents[5][3]["principle"], "status": "PLANNED", "source_refs": [copy.deepcopy(source)]},
                {"node_id": "experiment-001", "node_type": "EXPERIMENT_DESIGN", "statement": contents[6][3]["purpose"], "status": "PLANNED", "source_refs": [copy.deepcopy(source)]},
                {"node_id": "innovation-001", "node_type": "NOVEL_MECHANISM", "statement": contents[7][3]["novel_mechanism"], "status": "PLANNED", "source_refs": [copy.deepcopy(source)]},
                {"node_id": "foundation-001", "node_type": "TEAM_EVIDENCE", "statement": contents[10][3]["title"], "status": "SUPPORTED" if has_foundation_evidence else "UNKNOWN", "source_refs": [copy.deepcopy(foundation_source)] if has_foundation_evidence else []},
            ],
            "edges": [
                {"edge_id": "arg-edge-001", "source_id": "gap-001", "relation": "MOTIVATES", "target_id": "rq-001", "rationale": "差距产生约束映射问题"},
                {"edge_id": "arg-edge-002", "source_id": "gap-001", "relation": "MOTIVATES", "target_id": "rq-002", "rationale": "差距产生动态优化问题"},
                {"edge_id": "arg-edge-003", "source_id": "rq-002", "relation": "ADDRESSED_BY", "target_id": "wp-002", "rationale": "任务回答研究问题"},
                {"edge_id": "arg-edge-004", "source_id": "wp-002", "relation": "USES", "target_id": "method-001", "rationale": "任务采用方法"},
                {"edge_id": "arg-edge-005", "source_id": "method-001", "relation": "VALIDATED_BY", "target_id": "experiment-001", "rationale": "实验验证方法"},
            ],
        }
        return pd, proposal_contract, argument_graph

    def _narrative_architecture(self, envelope: dict[str, Any]) -> dict[str, Any]:
        payload = envelope.get("payload", {})
        graph = payload.get("argument_graph") or payload.get("argument_graph_candidate") or self._research_definition(envelope)[2]
        sections = []
        proposal_sections = [s for s in payload.get("linked_sections", []) if isinstance(s, dict)]
        current = payload.get("source_section")
        if current and current not in proposal_sections:
            proposal_sections.insert(0, current)
        if not proposal_sections:
            proposal_sections = [{"section_id": "section-001", "title": "研究内容"}]
        seen_profiles: dict[str, int] = {}
        main_count = 0
        main_profiles = {"ABSTRACT", "PROJECT_OVERVIEW", "BACKGROUND_AND_SIGNIFICANCE", "LITERATURE_REVIEW", "NEED_ANALYSIS", "KEY_ISSUE", "RESEARCH_OBJECTIVE", "RESEARCH_CONTENT", "METHOD_AND_ALGORITHM", "TECHNICAL_ROUTE", "EVALUATION", "INNOVATION", "OUTPUTS_AND_METRICS", "RESEARCH_FOUNDATION", "PROGRESS_BUDGET_RISK", "REFERENCES"}
        for index, section in enumerate(proposal_sections):
            title = self._clean_title(str(section.get("title") or "研究内容"))
            profile = self.pack.section_profile_for(title)
            profile_id = profile["profile_id"]
            seen_profiles[profile_id] = seen_profiles.get(profile_id, 0) + 1
            if profile_id == "APPENDIX":
                placement = "APPENDIX"
            elif profile_id in main_profiles and seen_profiles[profile_id] <= 2 and main_count < 18:
                placement = "MAIN_BODY"
                main_count += 1
            elif profile_id in {"SECTION_GENERAL", "SYSTEM_IMPLEMENTATION"}:
                placement = "APPENDIX"
            else:
                placement = "OMIT"
            profile_bindings = {
                "ABSTRACT": (["prop-001"], ["gap-001", "method-001", "experiment-001", "innovation-001"]),
                "PROJECT_OVERVIEW": (["prop-001", "objective-001"], ["gap-001", "wp-001", "wp-002"]),
                "BACKGROUND_AND_SIGNIFICANCE": (["rq-001", "rq-002"], ["prior-001", "gap-001"]),
                "LITERATURE_REVIEW": (["gap-001"], ["prior-001", "gap-001"]),
                "NEED_ANALYSIS": (["rq-001"], ["gap-001", "objective-001"]),
                "KEY_ISSUE": (["rq-001", "rq-002"], ["gap-001", "prior-001"]),
                "RESEARCH_OBJECTIVE": (["objective-001"], ["rq-001", "rq-002", "prop-001"]),
                "RESEARCH_CONTENT": (["wp-001", "wp-002"], ["objective-001", "rq-001", "rq-002"]),
                "METHOD_AND_ALGORITHM": (["method-001"], ["wp-002", "rq-002", "gap-001"]),
                "TECHNICAL_ROUTE": (["method-001"], ["wp-001", "wp-002", "experiment-001"]),
                "EVALUATION": (["experiment-001"], ["method-001", "prior-001", "innovation-001"]),
                "INNOVATION": (["innovation-001"], ["prior-001", "gap-001", "method-001"]),
                "OUTPUTS_AND_METRICS": (["objective-001", "innovation-001"], ["experiment-001", "method-001"]),
                "RESEARCH_FOUNDATION": (["foundation-001"], ["foundation-001", "wp-001", "wp-002"]),
                "PROGRESS_BUDGET_RISK": (["wp-001", "wp-002"], ["objective-001", "experiment-001"]),
                "REFERENCES": (["gap-001"], ["prior-001"]),
                "APPENDIX": (["wp-001", "wp-002"], ["method-001", "experiment-001"]),
                "SECTION_GENERAL": (["prop-001"], ["gap-001", "objective-001"]),
            }
            claim_ids, evidence_ids = profile_bindings.get(profile_id, (["prop-001"], ["gap-001", "objective-001"]))
            sections.append({
                "section_contract_id": f"section-contract-{index+1:03d}",
                "section_id": str(section.get("section_id") or f"section-{index+1:03d}"),
                "title": title,
                "profile_id": profile["profile_id"],
                "argument_function": f"按照{profile['profile_id']}章节规则推进中心命题，避免与其他章节重复。",
                "must_advance_claim_ids": claim_ids,
                "must_use_evidence_ids": evidence_ids,
                "unique_information_keys": [
                    f"{str(section.get('section_id') or f'section-{index+1:03d}')}-{profile_id}-claim",
                    f"{str(section.get('section_id') or f'section-{index+1:03d}')}-{profile_id}-evidence",
                    f"{str(section.get('section_id') or f'section-{index+1:03d}')}-{profile_id}-boundary",
                ],
                "required_argument_roles": [str(role) for role in {
                    "ABSTRACT": ["PROBLEM", "CENTRAL_CLAIM", "METHOD", "CONTRIBUTION"],
                    "PROJECT_OVERVIEW": ["CONTEXT", "PROBLEM", "CENTRAL_CLAIM", "TRANSITION"],
                    "BACKGROUND_AND_SIGNIFICANCE": ["CONTEXT", "EVIDENCE", "LIMITATION_MECHANISM", "GAP", "RESEARCH_QUESTION"],
                    "LITERATURE_REVIEW": ["EVIDENCE", "LIMITATION_MECHANISM", "COUNTERARGUMENT", "GAP"],
                    "NEED_ANALYSIS": ["PROBLEM", "EVIDENCE", "GAP", "RESEARCH_QUESTION"],
                    "KEY_ISSUE": ["GAP", "RESEARCH_QUESTION", "BOUNDARY"],
                    "RESEARCH_OBJECTIVE": ["RESEARCH_QUESTION", "CENTRAL_CLAIM", "EVALUATION"],
                    "RESEARCH_CONTENT": ["PROBLEM", "METHOD", "WARRANT", "EVALUATION"],
                    "METHOD_AND_ALGORITHM": ["PROBLEM", "METHOD", "WARRANT", "BOUNDARY", "EVALUATION"],
                    "TECHNICAL_ROUTE": ["PROBLEM", "METHOD", "WARRANT", "EVALUATION"],
                    "EVALUATION": ["RESEARCH_QUESTION", "EVIDENCE", "EVALUATION", "BOUNDARY"],
                    "INNOVATION": ["EVIDENCE", "LIMITATION_MECHANISM", "CENTRAL_CLAIM", "CONTRIBUTION"],
                    "OUTPUTS_AND_METRICS": ["CENTRAL_CLAIM", "EVALUATION", "CONTRIBUTION"],
                    "RESEARCH_FOUNDATION": ["EVIDENCE", "WARRANT", "BOUNDARY"],
                    "PROGRESS_BUDGET_RISK": ["METHOD", "EVIDENCE", "BOUNDARY"],
                    "REFERENCES": ["EVIDENCE"],
                    "APPENDIX": ["CONTEXT", "METHOD"],
                    "SECTION_GENERAL": ["PROBLEM", "EVIDENCE", "METHOD", "EVALUATION"],
                }.get(profile_id, ["PROBLEM", "EVIDENCE", "METHOD", "EVALUATION"])],
                "prerequisite_section_ids": [str(s.get("section_id")) for s in proposal_sections[:index] if s.get("section_id")][-2:],
                "must_not_repeat_section_ids": [str(s.get("section_id")) for s in proposal_sections[:index] if s.get("section_id")][-3:],
                "allowed_shared_context_ids": ["prop-001"],
                "forbidden_topics": ["部署步骤", "Prompt执行日志", "无基线指标"] if placement == "MAIN_BODY" else ["将附件内容包装为核心创新"],
                "max_overlap_ratio": 0.12 if placement == "MAIN_BODY" else 0.2,
                "word_budget": 600 if placement == "MAIN_BODY" else 350,
                "placement": placement,
                "acceptance_rules": profile.get("acceptance_rules") or ["推进中心命题", "使用真实证据"],
            })
        return {
            "architecture_id": "narrative-architecture-001", "document_type": "RESEARCH_PROPOSAL",
            "central_proposition_id": "prop-001", "central_proposition": graph["central_proposition"]["statement"],
            "research_question_ids": [q["node_id"] for q in graph.get("research_questions", [])][:3],
            "closest_prior_work_ids": ["prior-001"], "work_package_ids": ["wp-001", "wp-002"],
            "main_body_page_budget": 35, "main_body_word_budget": 25000,
            "section_contracts": sections,
            "attachments": [{"attachment_id": "attachment-001", "title": "系统实现与部署附件", "purpose": "隔离不属于主文科学论证的工程细节", "content_types": ["部署脚本", "接口", "审计日志", "Prompt与Trace"]}],
        }

    def _handle_security_classify(self, base: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
        result = base["result"]
        result["recommended_level"] = "INTERNAL"
        result["sensitive_fields"] = ["人员姓名", "组织名称"]
        result["allowed_environments"] = ["OFFLINE_LOCAL", "ONLINE_PUBLIC"]
        result["rationale"] = ["申请书写作在内部环境执行，公开检索仅允许使用脱敏任务包。"]
        result["confidence"] = "HIGH"
        return base

    def _handle_security_classify_critic(self, base: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
        return base

    def _handle_scheme_extract(self, base: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
        r = base["result"]["scheme_profile"]
        r["scheme_name"] = self._project_name(envelope)
        r["scheme_type"] = "RESEARCH"
        r["funding_organization"] = "内部研发计划"
        r["application_year"] = 2026
        r["guide_direction_name"] = "物流运输优化与智能体系统" if self._is_transport_project(envelope) else "智能体系统与复杂服务保障"
        r["duration_months"] = 36
        return base

    def _handle_scheme_critic(self, base: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
        return base

    def _handle_project_definition_extract(self, base: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
        pd, proposal_contract, argument_graph = self._research_definition(envelope)
        base["result"]["project_definition"] = pd
        base["result"]["proposal_contract"] = proposal_contract
        base["result"]["argument_graph_seed"] = argument_graph
        base["result"]["extraction_coverage"] = [{
            "domain": domain,
            "source_ids": sorted({ref["source_id"] for item in pd["items"] if item["domain"] == domain for ref in item["source_refs"]}),
            "item_ids": [item["item_id"] for item in pd["items"] if item["domain"] == domain],
        } for domain in sorted({item["domain"] for item in pd["items"]})]
        base["result"]["unmapped_source_spans"] = []
        base["status"] = "PASS"
        base["findings"] = []
        return base

    def _handle_project_definition_critic(self, base: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
        payload = envelope.get("payload", {})
        pd = payload.get("project_definition_candidate") or {}
        base["result"]["verdict"] = "ACCEPT"
        base["result"]["checked_item_ids"] = [item.get("item_id") for item in pd.get("items", []) if item.get("item_id")]
        base["result"]["checked_relation_ids"] = [rel.get("relation_id") for rel in pd.get("relations", []) if rel.get("relation_id")]
        base["result"]["invalid_relation_ids"] = []
        base["result"]["status_upgrade_item_ids"] = []
        base["result"]["argument_checks"] = self._dimension_checks(["DOCUMENT_CONTRACT", "CENTRAL_PROPOSITION", "RESEARCH_GAP", "RESEARCH_QUESTIONS", "CLOSEST_PRIOR_WORK", "OBJECTIVE_TASK_ALIGNMENT", "METHOD_AND_EVALUATION", "FOUNDATION_EVIDENCE"])
        base["status"] = "PASS"; base["findings"] = []
        return base

    def _handle_fact_extract(self, base: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
        facts = [
            {
                "claim_id": "fact-001",
                "claim_text": "项目周期拟定为36个月。",
                "claim_type": "PLAN",
                "subject_id": "project-001",
                "temporal_status": "PLANNED",
                "qualifiers": ["拟"],
                "numeric_values": [],
                "source_refs": [],
                "knowledge_status": "CONFIRMED",
                "security_level": "INTERNAL",
            },
            {
                "claim_id": "fact-002",
                "claim_text": "项目拟形成原型系统、验证报告、样例数据集和配套文档。",
                "claim_type": "PLAN",
                "subject_id": "project-001",
                "temporal_status": "PLANNED",
                "qualifiers": ["拟"],
                "numeric_values": [],
                "source_refs": [],
                "knowledge_status": "CONFIRMED",
                "security_level": "INTERNAL",
            },
        ]
        base["result"]["fact_candidates"] = facts
        base["result"]["coverage"] = [{"span_id": f"span-{i:03d}", "claim_ids": [fact["claim_id"]]} for i, fact in enumerate(facts, 1)]
        base["result"]["conflict_candidates"] = []
        return base

    def _handle_fact_critic(self, base: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
        return base

    def _handle_project_readiness_critic(self, base: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
        profiles = sorted(self.pack.section_profiles.get("profiles", []), key=lambda p: p.get("profile_id", ""))
        core = [p["profile_id"] for p in profiles if p["profile_id"] in {"BACKGROUND_AND_SIGNIFICANCE", "LITERATURE_REVIEW", "RESEARCH_OBJECTIVE", "RESEARCH_CONTENT", "KEY_ISSUE", "TECHNICAL_ROUTE", "INNOVATION", "OUTPUTS_AND_METRICS", "RESEARCH_FOUNDATION"}]
        base["result"]["writeable_section_profiles"] = core
        base["result"]["blocked_section_profiles"] = []
        base["result"]["chapter_readiness"] = [{"profile_id": pid, "readiness": "READY", "missing_item_ids": [], "blocking_conflict_ids": []} for pid in core]
        dimensions = ["DOCUMENT_CONTRACT", "CENTRAL_PROPOSITION", "RESEARCH_GAP", "RESEARCH_QUESTIONS", "CLOSEST_PRIOR_WORK", "METHOD_SUBSTANCE", "EVALUATION_DESIGN", "INNOVATION_BASELINE", "RESEARCH_FOUNDATION", "METRIC_JUSTIFICATION", "SCOPE_AND_PAGE_BUDGET"]
        base["result"]["critical_readiness_checks"] = [{"check_id": f"readiness-{i:03d}", "dimension": d, "passed": True, "reason": "输入图谱存在对应节点和来源。", "missing_node_types": [], "evidence_ids": ["prop-001"]} for i,d in enumerate(dimensions,1)]
        stage = str(envelope.get("payload", {}).get("readiness_stage") or "READY_FOR_ARGUMENT_ARCHITECTURE")
        base["result"]["assessed_stage"] = stage
        base["result"]["ready_for_argument_architecture"] = True
        base["result"]["ready_for_section_planning"] = stage == "READY_FOR_SECTION_PLANNING"
        base["result"]["missing_inputs"] = []
        base["status"] = "PASS"; base["findings"] = []
        return base

    def _handle_template_extract(self, base: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
        template = base["result"]["template"]
        template["global_argument"] = "研究背景收束到具体差距；差距由代表工作和局限机制支撑；问题、目标、任务、方法、验证和贡献形成闭环。"
        template["format_rules"] = ["主文围绕中心命题", "章节按论证功能组织", "图表服务于理解方法或验证", "部署与审计细节移入附件"]
        template["applicability"] = ["科研项目申请书", "技术原理与方法研究项目"]
        template["argument_patterns"] = [
            {"pattern_id": "arg-gap", "name": "能力—局限—命题", "sequence": ["已有工作能做什么", "在何种边界下为什么失效", "本项目提出何种命题"], "applicable_profiles": ["BACKGROUND_AND_SIGNIFICANCE", "LITERATURE_REVIEW"], "evidence": "由参考申请书段落推进关系抽取"},
            {"pattern_id": "arg-method", "name": "问题—机制—验证", "sequence": ["研究问题", "形式化机制", "实验或比较"], "applicable_profiles": ["RESEARCH_CONTENT", "TECHNICAL_ROUTE"], "evidence": "由研究方案章节抽取"},
            {"pattern_id": "arg-innovation", "name": "最近工作—局限—新增机制—优势", "sequence": ["最近工作", "局限机制", "新增机制", "可比较预期"], "applicable_profiles": ["INNOVATION"], "evidence": "由创新论证章节抽取"},
        ]
        template["expression_patterns"] = [
            {"pattern_id": "expr-focus", "function": "问题收束", "description": "从应用背景转到可研究的机制问题", "safe_example": "现有方法能够解决一般场景，但在特定动态边界下仍受到机制限制。", "anti_copy_rule": "不得复制范例实体、数字和技术内容"},
            {"pattern_id": "expr-gap", "function": "证据转折", "description": "先承认已有能力再指出局限原因", "safe_example": "上述进展证明了方法可行性，但尚未处理局部事件与全局约束的耦合。", "anti_copy_rule": "只学习逻辑关系"},
            {"pattern_id": "expr-feasible", "function": "可行性证明", "description": "用前期证据支撑关键步骤", "safe_example": "已有原型验证了关键输入可获得，后续研究将重点检验核心机制。", "anti_copy_rule": "没有证据不得使用"},
        ]
        template["quality_anti_patterns"] = [{"code": f"anti-{i:03d}", "description": desc, "detection_hint": hint, "repair_rule": repair} for i,(desc,hint,repair) in enumerate([
            ("背景宏大但问题不收束", "连续多段只描述价值和趋势", "补充局限机制并提出具体研究问题"),
            ("技术名词堆叠", "并列算法名但无输入、机制和验证", "改写为形式化对象、机制和验证"),
            ("系统说明替代科研论证", "主文高频出现部署、日志和接口", "移入附件并恢复研究链"),
            ("指标没有基线", "只有目标阈值", "补充场景、基线、条件和测量方法"),
            ("跨章节复用六段式", "相同标题和句式重复", "按Section Contract重建段落功能"),
        ],1)]
        template["components"] = [
            {"component_id":"component-background","section_role":"立项依据","input_requirements":["研究背景","代表工作","研究差距"],"output_function":"由背景收束到可研究差距","paragraph_patterns":["背景—证据—局限—问题"],"forbidden_project_facts":["范例项目名称","范例指标"]},
            {"component_id":"component-review","section_role":"研究现状","input_requirements":["公开来源","最近工作","局限机制"],"output_function":"比较已有方法并推出研究切入点","paragraph_patterns":["能力—边界—局限—切入点"],"forbidden_project_facts":["范例成果"]},
            {"component_id":"component-objective","section_role":"研究目标","input_requirements":["中心命题","研究问题"],"output_function":"定义可验证目标","paragraph_patterns":["问题—目标—成功证据"],"forbidden_project_facts":["无来源指标"]},
            {"component_id":"component-method","section_role":"研究方案","input_requirements":["任务","形式化模型","算法机制","实验"],"output_function":"说明怎样回答研究问题","paragraph_patterns":["对象—约束—机制—验证"],"forbidden_project_facts":["技术名词堆叠"]},
            {"component_id":"component-innovation","section_role":"创新点","input_requirements":["最近工作","局限","新增机制"],"output_function":"形成可比较创新主张","paragraph_patterns":["基线—局限—机制—优势"],"forbidden_project_facts":["泛化形容词"]},
            {"component_id":"component-foundation","section_role":"研究基础","input_requirements":["成果证据","预实验","团队能力"],"output_function":"证明可行性并暴露剩余风险","paragraph_patterns":["证据—支撑关系—边界"],"forbidden_project_facts":["抽象能力声明"]},
        ]
        base["status"] = "PASS"; base["findings"] = []
        return base

    def _handle_template_critic(self, base: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
        base["result"]["verdict"] = "ACCEPT"
        base["result"]["logic_pattern_checks"] = self._dimension_checks(["ARGUMENT_SEQUENCE", "SECTION_FUNCTION", "EXPRESSION_PATTERN", "ANTI_PATTERN_COVERAGE", "FACT_CONTAMINATION"])
        base["status"] = "PASS"; base["findings"] = []
        return base

    def _handle_safe_online_package(self, base: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
        project_name = self._project_name(envelope)
        result = base["result"]
        result["task_description"] = f"围绕{project_name}检索公开研究、行业报告、标准规范与相关案例，仅用于补充研究现状、技术路线和评价指标。"
        result["queries"] = self._research_queries(envelope)
        result["allowed_context"] = (["车辆路径", "多仓协同", "多式联运", "动态重规划", "多智能体", "绿色物流"] if self._is_transport_project(envelope) else ["智能体系统", "后勤保障", "资源调度", "知识图谱", "流程编排", "系统评估"])
        result["entity_placeholders"] = []
        result["removed_fields"] = ["人员姓名", "组织名称", "详细地址", "联系电话"]
        result["prohibited_inferences"] = ["不得反推内部组织与人员信息", "不得据公开资料推断未提供的内部事实"]
        result["prohibited_outputs"] = ["不得输出真实敏感字段", "不得输出未核实的内部信息"]
        result["security_level"] = "PUBLIC"
        return base

    def _handle_safe_online_package_critic(self, base: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
        return base

    def _handle_public_research_plan(self, base: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
        result = base["result"]
        if self._is_transport_project(envelope):
            result["research_questions"] = [
                "车辆路径、时间窗、取送和多仓问题有哪些精确与启发式方法？",
                "动态订单、交通变化和车辆故障下如何进行在线决策与低扰动重规划？",
                "多式联运、库存运输联动和绿色物流如何统一建模与评价？",
                "学习增强优化和多智能体协同可承担哪些任务，如何保证硬约束可行性？",
                "公开研究、算法运行、Mermaid图形和文档结论如何形成可验证证据链？",
            ]
        else:
            result["research_questions"] = [
                "大模型智能体的规划、工具调用、记忆、反思和多智能体协同技术发展到什么程度？",
                "RAG、GraphRAG、知识图谱与可追踪证据链如何支撑专业场景？",
                "组合优化、车辆路径、排程和动态重规划可采用哪些代表性方法？",
                "Agent评测、安全治理、人机协同和工程可观测性有哪些公开依据？",
            ]
        result["queries"] = self._research_queries(envelope)
        result["source_priorities"] = ["国际标准与官方规范", "政府/标准机构页面", "协议设计文档", "同行评议论文", "官方开源项目文档"]
        result["evidence_requirements"] = ["覆盖不少于30个可核验公开来源", "保存来源URL、获取时间、摘录与SHA-256", "正文引用与参考文献编号一一对应", "只使用归档来源形成PUBLIC_CLAIM"]
        result["prohibited_inferences"] = ["不得从公开资料反推内部组织、人员或部署信息", "不得将外部性能数字直接作为本项目实测结果"]
        return base

    def _handle_public_research_synthesis(self, base: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
        payload = envelope.get("payload", {})
        retrieved = [item for item in payload.get("retrieved_sources", []) if isinstance(item, dict)]
        passages = [item for item in payload.get("extracted_passages", []) if isinstance(item, dict)]
        passage_by_source = {str(p.get("source_ref", {}).get("source_id")): p for p in passages}
        catalog = self._catalog(envelope)
        catalog_by_source = {str(item.get("source_id") or f"public-src-{self._item_number(item, i):03d}"): item for i, item in enumerate(catalog, 1)}
        if not retrieved:
            retrieved = [self._source_ref(self._item_number(item, i), item) for i, item in enumerate(catalog, 1)]
        elif len(retrieved) < 2:
            existing = {str(item.get("source_id")) for item in retrieved}
            for i, item in enumerate(catalog, 1):
                candidate = self._source_ref(self._item_number(item, i), item)
                if str(candidate.get("source_id")) not in existing:
                    retrieved.append(candidate)
                    break
        claims = []
        for idx, source_ref in enumerate(retrieved, 1):
            source_id = str(source_ref.get("source_id") or f"public-src-{idx:03d}")
            item = catalog_by_source.get(source_id, {})
            passage = passage_by_source.get(source_id, {})
            claim_text = str(passage.get("text") or self._item_summary(item))[:6000]
            claims.append({
                "claim_id": f"pub-claim-{idx:03d}",
                "claim_text": claim_text,
                "claim_type": "PUBLIC_CLAIM",
                "subject_id": None,
                "temporal_status": "TIME_INDEPENDENT",
                "qualifiers": [str(item.get("publisher") or item.get("category") or "PUBLIC_SOURCE")],
                "numeric_values": [],
                "source_refs": [source_ref],
                "knowledge_status": "DOCUMENT_EXTRACTED",
                "security_level": "PUBLIC",
            })
        source_ids = [str(item.get("source_id")) for item in retrieved]
        groups = [source_ids[i:i+5] for i in range(0, min(len(source_ids), 20), 5) if len(source_ids[i:i+5]) >= 2]
        topics = (["车辆路径与混合优化", "动态运输与低扰动重规划", "多仓/多式联运与绿色物流", "多智能体、证据与工程治理"] if self._is_transport_project(envelope) else ["智能体规划与协同", "知识增强与证据追踪", "调度优化与动态重规划", "治理、评测与安全"])
        base["result"]["claims"] = claims
        base["result"]["source_comparisons"] = [
            {"topic": topics[i % len(topics)], "source_ids": group, "agreement": "PARTIAL", "summary": "来源在总体方向上相互支持，但适用场景、成熟度、性能条件和工程边界不同，需在本项目中通过原型与测试进一步验证。"}
            for i, group in enumerate(groups)
        ] or [{"topic": topics[0], "source_ids": source_ids[:2], "agreement": "PARTIAL", "summary": "归档来源提供相关公开依据，工程适配仍需项目验证。"}]
        base["result"]["conflicts"] = []
        base["result"]["limitations"] = ["公开来源说明标准、机制和公开实践，不代表本项目已经完成实测。", "来源真实性由URL、归档记录、摘录和Hash支持；具体主张仍需按正文引用进行人工复核。"]
        base["result"]["coverage_summary"] = f"综合{len(claims)}项实际归档公开来源，研究输入来自public_research.archive技能而非模型记忆。"
        base["source_refs"] = retrieved
        return base

    def _handle_public_research_critic(self, base: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
        return base

    def _handle_online_result_import_critic(self, base: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
        claims = envelope.get("payload", {}).get("result_package", {}).get("claims", [])
        base["result"]["import_recommendation"] = "IMPORT_REFERENCE_ONLY"
        base["result"]["accepted_claim_ids"] = [str(item.get("claim_id")) for item in claims if item.get("claim_id")]
        base["result"]["rejected_claim_ids"] = []
        return base

    def _handle_revision_plan(self, base: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
        payload = envelope.get("payload", {})
        plan = base["result"]["revision_plan"]
        architecture = self._narrative_architecture(envelope)
        contracts = architecture["section_contracts"]
        plan["issues"] = [{"issue_id": "issue-argument-chain", "description": "需要按中心命题和研究问题组织主文，而不是按功能模块扩写。", "evidence_refs": ["prop-001"], "severity": "P1"}]
        if os.getenv("SIMULATED_INJECT_PLAN_REPAIR", "false").lower() in {"1", "true", "yes"}:
            plan["issues"].append({
                "issue_id": "issue-simulated-repair",
                "description": "[SIM_REPAIR] 用于验证一次定向修复链路。",
                "evidence_refs": ["prop-001"],
                "severity": "P1",
            })
        plan["target_section_ids"] = [c["section_id"] for c in contracts if c["placement"] != "OMIT"]
        plan["read_only_section_ids"] = []
        plan["protected_section_ids"] = []
        plan["tasks"] = [{"revision_task_id": f"revision-{i:03d}", "operation": "RESTRUCTURE", "objective": c["argument_function"], "issue_ids": ["issue-argument-chain"], "required_input_ids": list(dict.fromkeys(c["must_advance_claim_ids"] + c["must_use_evidence_ids"])), "acceptance_rules": c["acceptance_rules"]} for i,c in enumerate(contracts,1)]
        plan["dependencies"] = []
        plan["user_question_ids"] = []
        plan["narrative_architecture"] = architecture
        base["result"]["readiness_summary"] = [{"task_id": t["revision_task_id"], "readiness": "READY", "missing_input_ids": []} for t in plan["tasks"]]
        base["result"]["scope_rationale"] = ["主文围绕唯一中心命题和两个研究问题，工程实现与部署细节进入附件。"]
        base["status"] = "PASS"; base["findings"] = []
        return base

    def _handle_revision_plan_critic(self, base: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
        plan = envelope.get("payload", {}).get("revision_plan_candidate", {})
        needs_repair = any("[SIM_REPAIR]" in str(item.get("description", "")) for item in plan.get("issues", []))
        passed = not needs_repair
        base["result"]["verdict"] = "ACCEPT" if passed else "REVISE"
        base["result"]["architecture_checks"] = self._dimension_checks(["CENTRAL_PROPOSITION", "QUESTION_COUNT", "WORK_PACKAGE_COUNT", "SECTION_PROFILE_MAPPING", "PAGE_BUDGET", "MAIN_BODY_ATTACHMENT_BOUNDARY", "CLAIM_COVERAGE", "REDUNDANCY_PREVENTION"], passed)
        base["status"] = "PASS" if passed else "REVISE"
        base["findings"] = [] if passed else [{"code": "PLAN_SIMULATED_REPAIR", "severity": "P1", "category": "CONTENT", "target_type": "REVISION_PLAN", "target_path_or_span": "issues", "description": "计划中包含模拟缺陷标记。", "evidence_refs": [], "repairable": True, "repair_instruction": "删除标记并保持叙事架构不变。", "suggested_route": "ORIGINAL_PRODUCER", "blocking": True}]
        return base

    def _handle_targeted_repair(self, base: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
        original = copy.deepcopy(envelope.get("payload", {}).get("original_object", {}).get("content", {}))
        if isinstance(original, dict):
            for issue in original.get("issues", []):
                if isinstance(issue, dict) and "[SIM_REPAIR]" in str(issue.get("description", "")):
                    issue["description"] = str(issue["description"]).replace("[SIM_REPAIR]", "").strip()
        base["result"]["repaired_object"] = original
        base["result"]["changed_paths"] = ["content.issues[1].description"]
        base["result"]["unchanged_protected_hashes"] = []
        base["result"]["resolved_finding_codes"] = ["PLAN_SIMULATED_REPAIR"]
        base["result"]["unresolved_finding_codes"] = []
        return base

    def _handle_write_blueprint(self, base: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
        payload = envelope.get("payload", {})
        section = payload.get("source_section") or {}
        title = self._clean_title(section.get("title", "研究内容"))
        profile = payload.get("section_profile") or self.pack.section_profile_for(title)
        contract = payload.get("section_contract") or self._narrative_architecture(envelope)["section_contracts"][0]
        graph = payload.get("argument_graph") or self._research_definition(envelope)[2]
        type_ids: dict[str, list[str]] = {}
        for node in graph.get("nodes", []):
            type_ids.setdefault(str(node.get("node_type")), []).append(str(node.get("node_id")))
        question_ids = [str(q.get("node_id")) for q in graph.get("research_questions", []) if q.get("node_id")]
        proposition_id = str((graph.get("central_proposition") or {}).get("node_id") or "prop-001")

        roles_by_profile = {
            "ABSTRACT": [("PROBLEM", "用一句话界定核心瓶颈"), ("CENTRAL_CLAIM", "提出中心技术命题"), ("METHOD", "概括研究方法"), ("CONTRIBUTION", "说明可验证贡献")],
            "PROJECT_OVERVIEW": [("CONTEXT", "界定项目对象与边界"), ("PROBLEM", "收束核心问题"), ("CENTRAL_CLAIM", "说明总体研究主张"), ("TRANSITION", "给出任务之间的逻辑关系")],
            "BACKGROUND_AND_SIGNIFICANCE": [("CONTEXT", "说明现实与技术背景"), ("EVIDENCE", "概括现有方法能够解决的问题"), ("LIMITATION_MECHANISM", "解释现有方法在目标场景中的失效原因"), ("GAP", "由局限推出研究差距"), ("RESEARCH_QUESTION", "提出需要回答的研究问题")],
            "LITERATURE_REVIEW": [("EVIDENCE", "比较代表性方法及其适用条件"), ("LIMITATION_MECHANISM", "分析最接近方法的局限机制"), ("COUNTERARGUMENT", "说明可能的替代解释与边界"), ("GAP", "综合形成明确研究切入点")],
            "KEY_ISSUE": [("GAP", "界定研究差距"), ("RESEARCH_QUESTION", "提出可比较的核心问题"), ("BOUNDARY", "说明问题成立的边界条件")],
            "RESEARCH_OBJECTIVE": [("RESEARCH_QUESTION", "对应研究问题"), ("CENTRAL_CLAIM", "定义可验证目标"), ("EVALUATION", "说明成功判据")],
            "RESEARCH_CONTENT": [("PROBLEM", "界定任务对应的问题"), ("METHOD", "说明任务对象与方法"), ("WARRANT", "解释任务间依赖和作用链"), ("EVALUATION", "绑定输出与验证")],
            "METHOD_AND_ALGORITHM": [("PROBLEM", "形式化研究对象与约束"), ("METHOD", "给出核心模型和算法机制"), ("WARRANT", "说明机制为何能解决问题"), ("BOUNDARY", "给出假设、复杂度或适用边界"), ("EVALUATION", "给出对照与消融验证")],
            "TECHNICAL_ROUTE": [("PROBLEM", "说明任务输入与依赖"), ("METHOD", "串联模型、算法与数据"), ("WARRANT", "解释各任务如何形成研究闭环"), ("EVALUATION", "说明验证回路与反馈")],
            "EVALUATION": [("RESEARCH_QUESTION", "列出待检验判断"), ("EVIDENCE", "确定数据、场景和基线"), ("EVALUATION", "设计对照、消融和统计检验"), ("BOUNDARY", "说明结论外推边界")],
            "INNOVATION": [("EVIDENCE", "确定最接近已有工作"), ("LIMITATION_MECHANISM", "说明已有方法的机制性局限"), ("CENTRAL_CLAIM", "提出本项目新增机制"), ("CONTRIBUTION", "给出可比较优势与适用条件")],
            "OUTPUTS_AND_METRICS": [("CENTRAL_CLAIM", "区分研究贡献与工程成果"), ("EVALUATION", "给出指标口径、基线与条件"), ("CONTRIBUTION", "说明成果与研究问题的对应关系")],
            "RESEARCH_FOUNDATION": [("EVIDENCE", "列出与课题直接相关的已有成果"), ("WARRANT", "解释这些成果如何支撑关键任务"), ("BOUNDARY", "如实说明仍需补足的条件")],
            "PROGRESS_BUDGET_RISK": [("METHOD", "按任务组织进度和资源"), ("EVIDENCE", "说明预算与任务依据"), ("BOUNDARY", "识别风险及替代路径")],
            "REFERENCES": [("EVIDENCE", "列出正文实际使用且可核验的来源")],
            "APPENDIX": [("CONTEXT", "说明附件与主文的边界"), ("METHOD", "记录实现、接口或部署细节")],
        }
        role_specs = roles_by_profile.get(profile.get("profile_id"), [("PROBLEM", "本章节要解决的具体问题"), ("EVIDENCE", "支撑问题与命题的证据"), ("METHOD", "本项目方法或任务"), ("EVALUATION", "验证方式")])

        contract_claims = list(contract.get("must_advance_claim_ids") or [proposition_id])
        contract_evidence = list(contract.get("must_use_evidence_ids") or [])
        profile_id = str(profile.get("profile_id") or "SECTION_GENERAL")

        # Claim/evidence binding is section-profile-specific.  The previous
        # implementation selected one global RESEARCH_QUESTION, PRIOR_WORK and
        # EXPERIMENT for every section, which produced identical paragraphs even
        # though the narrative contracts were different.
        default_claims = contract_claims or [proposition_id]
        default_evidence = contract_evidence or default_claims

        def first_of(*groups: list[str]) -> list[str]:
            for group in groups:
                values = [str(x) for x in group if x]
                if values:
                    return values
            return [proposition_id]

        profile_role_claims: dict[str, dict[str, list[str]]] = {
            "ABSTRACT": {
                "PROBLEM": type_ids.get("RESEARCH_GAP", []) or contract_claims,
                "CENTRAL_CLAIM": [proposition_id],
                "METHOD": type_ids.get("FORMAL_MODEL", []) or contract_claims,
                "CONTRIBUTION": type_ids.get("NOVEL_MECHANISM", []) or contract_claims,
            },
            "PROJECT_OVERVIEW": {
                "CONTEXT": type_ids.get("RESEARCH_GAP", []) or contract_claims,
                "PROBLEM": question_ids or contract_claims,
                "CENTRAL_CLAIM": [proposition_id],
                "TRANSITION": type_ids.get("WORK_PACKAGE", []) or contract_claims,
            },
            "BACKGROUND_AND_SIGNIFICANCE": {
                "CONTEXT": contract_claims, "EVIDENCE": type_ids.get("CLOSEST_PRIOR_WORK", []),
                "LIMITATION_MECHANISM": type_ids.get("RESEARCH_GAP", []), "GAP": type_ids.get("RESEARCH_GAP", []),
                "RESEARCH_QUESTION": question_ids,
            },
            "LITERATURE_REVIEW": {
                "EVIDENCE": type_ids.get("CLOSEST_PRIOR_WORK", []), "LIMITATION_MECHANISM": type_ids.get("RESEARCH_GAP", []),
                "COUNTERARGUMENT": type_ids.get("CLOSEST_PRIOR_WORK", []), "GAP": type_ids.get("RESEARCH_GAP", []),
            },
            "RESEARCH_OBJECTIVE": {
                "RESEARCH_QUESTION": question_ids, "CENTRAL_CLAIM": type_ids.get("OBJECTIVE", []) or [proposition_id],
                "EVALUATION": type_ids.get("EXPERIMENT_DESIGN", []),
            },
            "RESEARCH_CONTENT": {
                "PROBLEM": contract_claims, "METHOD": type_ids.get("WORK_PACKAGE", []) or contract_claims,
                "WARRANT": type_ids.get("OBJECTIVE", []) or contract_claims,
                "EVALUATION": type_ids.get("EXPERIMENT_DESIGN", []) or contract_evidence,
            },
            "METHOD_AND_ALGORITHM": {
                "PROBLEM": question_ids or contract_claims, "METHOD": type_ids.get("FORMAL_MODEL", []) or contract_claims,
                "WARRANT": type_ids.get("FORMAL_MODEL", []) or contract_claims, "BOUNDARY": [proposition_id],
                "EVALUATION": type_ids.get("EXPERIMENT_DESIGN", []),
            },
            "TECHNICAL_ROUTE": {
                "PROBLEM": type_ids.get("WORK_PACKAGE", []) or contract_claims,
                "METHOD": type_ids.get("FORMAL_MODEL", []) or type_ids.get("WORK_PACKAGE", []),
                "WARRANT": type_ids.get("WORK_PACKAGE", []) or contract_claims,
                "EVALUATION": type_ids.get("EXPERIMENT_DESIGN", []),
            },
            "EVALUATION": {
                "RESEARCH_QUESTION": question_ids, "EVIDENCE": type_ids.get("CLOSEST_PRIOR_WORK", []) + type_ids.get("EXPERIMENT_DESIGN", []),
                "EVALUATION": type_ids.get("EXPERIMENT_DESIGN", []), "BOUNDARY": [proposition_id],
            },
            "INNOVATION": {
                "EVIDENCE": type_ids.get("CLOSEST_PRIOR_WORK", []), "LIMITATION_MECHANISM": type_ids.get("RESEARCH_GAP", []),
                "CENTRAL_CLAIM": type_ids.get("NOVEL_MECHANISM", []) or [proposition_id],
                "CONTRIBUTION": type_ids.get("NOVEL_MECHANISM", []) or contract_claims,
            },
            "RESEARCH_FOUNDATION": {
                "EVIDENCE": type_ids.get("TEAM_EVIDENCE", []) or contract_claims,
                "WARRANT": type_ids.get("WORK_PACKAGE", []) or contract_evidence,
                "BOUNDARY": type_ids.get("TEAM_EVIDENCE", []) or [proposition_id],
            },
            "APPENDIX": {
                "CONTEXT": contract_claims, "METHOD": type_ids.get("FORMAL_MODEL", []) or contract_evidence,
            },
        }
        role_claims = profile_role_claims.get(profile_id, {})

        profile_role_evidence: dict[str, dict[str, list[str]]] = {
            "ABSTRACT": {
                "PROBLEM": type_ids.get("RESEARCH_GAP", []) or contract_evidence[:1],
                "CENTRAL_CLAIM": type_ids.get("OBJECTIVE", []) or [proposition_id],
                "METHOD": type_ids.get("FORMAL_MODEL", []) or contract_evidence[1:2],
                "CONTRIBUTION": type_ids.get("NOVEL_MECHANISM", []) or contract_evidence[2:3],
            },
            "PROJECT_OVERVIEW": {
                "CONTEXT": type_ids.get("RESEARCH_GAP", []) or contract_evidence[:1],
                "PROBLEM": question_ids or contract_evidence[:1],
                "CENTRAL_CLAIM": type_ids.get("OBJECTIVE", []) or [proposition_id],
                "TRANSITION": type_ids.get("WORK_PACKAGE", []) or contract_evidence,
            },
            "BACKGROUND_AND_SIGNIFICANCE": {
                "CONTEXT": type_ids.get("RESEARCH_GAP", []), "EVIDENCE": type_ids.get("CLOSEST_PRIOR_WORK", []),
                "LIMITATION_MECHANISM": type_ids.get("CLOSEST_PRIOR_WORK", []) + type_ids.get("RESEARCH_GAP", []),
                "GAP": type_ids.get("RESEARCH_GAP", []) + type_ids.get("CLOSEST_PRIOR_WORK", []),
                "RESEARCH_QUESTION": type_ids.get("RESEARCH_GAP", []),
            },
            "RESEARCH_OBJECTIVE": {
                "RESEARCH_QUESTION": type_ids.get("RESEARCH_GAP", []), "CENTRAL_CLAIM": question_ids,
                "EVALUATION": type_ids.get("EXPERIMENT_DESIGN", []) + type_ids.get("OBJECTIVE", []),
            },
            "RESEARCH_CONTENT": {
                "PROBLEM": question_ids + type_ids.get("RESEARCH_GAP", []),
                "METHOD": type_ids.get("WORK_PACKAGE", []) + type_ids.get("FORMAL_MODEL", []),
                "WARRANT": type_ids.get("WORK_PACKAGE", []) + type_ids.get("OBJECTIVE", []),
                "EVALUATION": type_ids.get("EXPERIMENT_DESIGN", []) + type_ids.get("WORK_PACKAGE", []),
            },
            "METHOD_AND_ALGORITHM": {
                "PROBLEM": question_ids + type_ids.get("RESEARCH_GAP", []),
                "METHOD": type_ids.get("WORK_PACKAGE", []) + type_ids.get("FORMAL_MODEL", []),
                "WARRANT": type_ids.get("FORMAL_MODEL", []) + type_ids.get("WORK_PACKAGE", []),
                "BOUNDARY": type_ids.get("RESEARCH_GAP", []),
                "EVALUATION": type_ids.get("EXPERIMENT_DESIGN", []) + type_ids.get("CLOSEST_PRIOR_WORK", []),
            },
            "TECHNICAL_ROUTE": {
                "PROBLEM": type_ids.get("WORK_PACKAGE", []), "METHOD": type_ids.get("WORK_PACKAGE", []) + type_ids.get("FORMAL_MODEL", []),
                "WARRANT": type_ids.get("OBJECTIVE", []) + type_ids.get("WORK_PACKAGE", []),
                "EVALUATION": type_ids.get("EXPERIMENT_DESIGN", []),
            },
            "EVALUATION": {
                "RESEARCH_QUESTION": question_ids, "EVIDENCE": type_ids.get("EXPERIMENT_DESIGN", []) + type_ids.get("CLOSEST_PRIOR_WORK", []),
                "EVALUATION": type_ids.get("EXPERIMENT_DESIGN", []) + type_ids.get("FORMAL_MODEL", []),
                "BOUNDARY": type_ids.get("RESEARCH_GAP", []),
            },
            "INNOVATION": {
                "EVIDENCE": type_ids.get("CLOSEST_PRIOR_WORK", []),
                "LIMITATION_MECHANISM": type_ids.get("CLOSEST_PRIOR_WORK", []) + type_ids.get("RESEARCH_GAP", []),
                "CENTRAL_CLAIM": type_ids.get("RESEARCH_GAP", []) + type_ids.get("FORMAL_MODEL", []),
                "CONTRIBUTION": type_ids.get("CLOSEST_PRIOR_WORK", []) + type_ids.get("EXPERIMENT_DESIGN", []),
            },
            "RESEARCH_FOUNDATION": {
                "EVIDENCE": type_ids.get("TEAM_EVIDENCE", []), "WARRANT": type_ids.get("TEAM_EVIDENCE", []) + type_ids.get("WORK_PACKAGE", []),
                "BOUNDARY": type_ids.get("TEAM_EVIDENCE", []),
            },
            "APPENDIX": {"CONTEXT": contract_evidence, "METHOD": type_ids.get("FORMAL_MODEL", []) + contract_evidence},
        }
        role_evidence = profile_role_evidence.get(profile_id, {})


        paragraphs = []
        for i, (role, function) in enumerate(role_specs, 1):
            # ``primary_claim_id`` means the proposition this section advances,
            # not the prior work or supporting node mentioned in the paragraph.
            # Keeping support nodes as primary claims made a handful of generic
            # nodes appear to be advanced by most chapters and caused false
            # full-document coherence.
            claim_id = str(default_claims[(i - 1) % len(default_claims)])
            supporting_claims = [
                str(x) for x in role_claims.get(role, [])
                if x and str(x) != claim_id
            ]
            explicit_evidence = [str(x) for x in role_evidence.get(role, []) if x]
            evidence = list(dict.fromkeys([
                *explicit_evidence,
                *supporting_claims,
                *([str(default_evidence[(i - 1) % len(default_evidence)])] if default_evidence else []),
            ]))
            if not evidence:
                evidence = [claim_id]
            paragraphs.append({
                "paragraph_id": f"bp-{sha256_text(title + str(i))[:12]}",
                "sequence": i,
                "function": function,
                "must_answer": [function],
                "fact_slots": [],
                "project_item_slots": evidence,
                "technical_slots": type_ids.get("FORMAL_MODEL", [])[:1] if role in {"METHOD", "WARRANT"} else [],
                "metric_slots": ["metric-001"] if role == "EVALUATION" else [],
                "source_strategy": "MERGE",
                "forbidden_content": ["无来源结论", "通用六段式套话", "部署和日志说明"] if contract.get("placement") == "MAIN_BODY" else ["将附件内容包装为核心创新"],
                "transition_requirement": None,
                "argument_role": role,
                "primary_claim_id": claim_id,
                "required_evidence_ids": evidence,
                "novel_content_key": (contract.get("unique_information_keys") or [f"{section.get('section_id', 'section')}-{profile.get('profile_id')}-unique"])[(i - 1) % len(contract.get("unique_information_keys") or [1])] + f"-{role.lower()}-{i}",
                "word_budget": max(100, int(contract.get("word_budget", 600) / max(1, len(role_specs)))),
            })
        # The deterministic simulator must exercise the same contract as a live
        # model: every required claim is explicitly assigned to at least one
        # paragraph.  This is not literary optimization; it prevents the test
        # provider from hiding a contract violation behind otherwise valid text.
        for claim_index, required_claim_id in enumerate(contract_claims):
            if not any(p["primary_claim_id"] == required_claim_id for p in paragraphs):
                target = paragraphs[min(claim_index, len(paragraphs) - 1)]
                target["primary_claim_id"] = required_claim_id
                if required_claim_id not in target["required_evidence_ids"]:
                    target["required_evidence_ids"].append(required_claim_id)
                if required_claim_id not in target["project_item_slots"]:
                    target["project_item_slots"].append(required_claim_id)

        bp = base["result"]["blueprint"]
        bp.update({"blueprint_id": new_id("blueprint"), "section_objective": contract.get("argument_function", f"推进《{title}》的独有论证"), "paragraphs": paragraphs, "unresolved_slot_ids": [], "section_profile_id": profile.get("profile_id"), "section_contract_id": contract.get("section_contract_id")})
        plan = payload.get("confirmed_plan") or {}
        tasks = plan.get("tasks") or []
        matching = next((t for t in tasks if set(t.get("required_input_ids") or []) & set(contract_claims + contract_evidence)), None)
        task_id = (matching or (tasks[0] if tasks else {})).get("revision_task_id", "revision-001")
        base["result"]["plan_task_coverage"] = [{"revision_task_id": task_id, "paragraph_ids": [p["paragraph_id"] for p in paragraphs]}]
        used_ids = sorted({eid for p in paragraphs for eid in p["required_evidence_ids"]})
        base["result"]["input_usage_summary"] = [{"source_id": sid, "used_in_paragraph_ids": [p["paragraph_id"] for p in paragraphs if sid in p["required_evidence_ids"]]} for sid in used_ids]
        base["status"] = "PASS"; base["findings"] = []
        return base

    def _handle_write_blueprint_critic(self, base: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
        bp = envelope.get("payload", {}).get("blueprint_candidate") or {}
        ids = [p.get("paragraph_id") for p in bp.get("paragraphs", []) if p.get("paragraph_id")]
        base["result"]["verdict"] = "ACCEPT"
        base["result"]["checked_paragraph_ids"] = ids
        base["result"]["uncovered_revision_task_ids"] = []
        base["result"]["invalid_slot_refs"] = []
        base["result"]["critical_unresolved_slot_ids"] = []
        base["result"]["argument_checks"] = self._dimension_checks(["SECTION_FUNCTION", "CLAIM_ADVANCEMENT", "EVIDENCE_BINDING", "PARAGRAPH_ROLE_DIVERSITY", "NOVEL_CONTENT_KEYS", "WORD_BUDGET", "NO_GENERIC_SIX_PART_TEMPLATE"])
        base["status"] = "PASS"; base["findings"] = []
        return base

    def _handle_write_content(self, base: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
        payload = envelope.get("payload", {})
        section = payload.get("source_section") or {}
        title = self._clean_title(section.get("title", "研究内容"))
        blueprint = payload.get("approved_blueprint") or {}
        if not isinstance(blueprint, dict) or not blueprint.get("paragraphs"):
            blueprint = self._handle_write_blueprint(self.pack.replay_output("P-WRITE-BLUEPRINT", "normal"), envelope)["result"]["blueprint"]
        graph = payload.get("argument_graph") or self._research_definition(envelope)[2]
        node_map = {str(n.get("node_id")): n for n in graph.get("nodes", []) if n.get("node_id")}
        proposition = graph.get("central_proposition") or {}
        if proposition.get("node_id"):
            node_map[str(proposition["node_id"])] = proposition
        for question in graph.get("research_questions", []):
            if question.get("node_id"):
                node_map[str(question["node_id"])] = question
        facts = {str(f.get("claim_id")): f for f in payload.get("confirmed_facts", []) if isinstance(f, dict)}

        def statement(source_id: str) -> str:
            obj = node_map.get(str(source_id)) or facts.get(str(source_id)) or {}
            return str(obj.get("statement") or obj.get("claim_text") or obj.get("content", {}).get("statement") or source_id).rstrip("。")

        profile_id = str((payload.get("section_profile") or {}).get("profile_id") or self.pack.section_profile_for(title).get("profile_id") or "SECTION_GENERAL")
        contract = payload.get("section_contract") or {}
        section_focus = str(contract.get("argument_function") or title).rstrip("。")
        prior_digest = [item for item in payload.get("prior_section_digest", []) if isinstance(item, dict)]
        prior_titles = [str(item.get("title") or "") for item in prior_digest if item.get("title")]
        if not prior_titles:
            prior_titles = [str(item.get("title") or "") for item in payload.get("read_only_context", []) if isinstance(item, dict) and item.get("title")]
        prior_hint = "、".join(prior_titles[-3:])
        prior_information_keys = {str(key) for item in prior_digest for key in item.get("new_information_keys", []) if key}

        def compose(role: str, claim_text: str, evidence_texts: list[str], index: int) -> str:
            evidence = "；".join(dict.fromkeys(x for x in evidence_texts if x))
            claim = claim_text.rstrip("。？")
            prefix = f"在《{title}》中"
            if role == "CONTEXT":
                if profile_id == "APPENDIX":
                    return f"{prefix}仅记录支撑主文复现与核查的实现信息，其内容不参与中心命题、创新性或前期基础的认定。{claim}，因此本节与主申请书的科学论证保持明确边界。"
                return f"{prefix}首先界定研究对象：{claim}。这一界定把讨论范围限定在可建模、可比较的问题上，而不是扩展一般软件功能。"
            if role == "PROBLEM":
                if profile_id == "RESEARCH_CONTENT":
                    return f"{prefix}需要把研究目标落实为可执行任务。当前任务针对“{claim}”，其输入、约束、输出和与其他任务的依赖必须分别明确，避免把功能清单当作研究内容。"
                if profile_id in {"METHOD_AND_ALGORITHM", "TECHNICAL_ROUTE"}:
                    return f"{prefix}形式化处理的问题是“{claim}”。关键困难不在模块数量，而在局部事件经资源、路径和时序关系传播后，哪些决策变量必须更新、哪些变量应保持不变。"
                return f"{prefix}要回答的具体问题是“{claim}”。该问题涉及局部变化与全局可行性的耦合，不能用增加求解模块或扩大计算预算代替机制分析。"
            if role == "RESEARCH_QUESTION":
                if profile_id == "RESEARCH_OBJECTIVE":
                    return f"研究目标首先对应问题“{claim}”。目标是否完成，不以原型是否上线判断，而以是否得到形式化模型、可复现实验和明确比较结论判断。"
                if profile_id == "EVALUATION":
                    return f"验证环节针对问题“{claim}”设置可否定的判断：若统一条件下关键指标未优于最接近基线，或消融后结果无显著变化，则相应机制主张不能成立。"
                return f"由前述差距收束出的研究问题是“{claim}”。回答它必须同时给出研究对象、比较基线、边界条件和能够否定预期判断的实验。"
            if role == "EVIDENCE":
                if profile_id == "BACKGROUND_AND_SIGNIFICANCE":
                    return f"支撑立项判断的直接证据是：{evidence or claim}。这些材料表明现有方法在输入稳定、调整范围预设时能够工作，但尚不能说明其在动态事件下兼顾质量、时延和计划稳定性。"
                if profile_id == "LITERATURE_REVIEW":
                    return f"与本项目最接近的方法可归纳为：{evidence or claim}。比较时需要分别记录其问题假设、决策范围、计算方式和评价指标，不能只按技术名称罗列文献。"
                if profile_id == "INNOVATION":
                    return f"创新性比较以“{evidence or claim}”为最近工作基线。后续新增机制必须说明相对该基线改变了什么决策过程，而不是把现有组件重新组合后直接称为创新。"
                if profile_id == "RESEARCH_FOUNDATION":
                    return f"与本课题直接相关的前期证据为：{evidence or claim}。该证据只证明已经具备的模型、代码、数据或实验能力，尚未完成的工作仍作为计划描述。"
                if profile_id == "EVALUATION":
                    return f"实验采用的比较依据包括：{evidence or claim}。所有方法使用同一数据、硬件、时间预算和目标权重，以保证差异可以归因于方法机制。"
                return f"本节使用的证据为：{evidence or claim}。其作用是支撑《{title}》的独有判断，而不是在多个章节重复同一背景材料。"
            if role == "LIMITATION_MECHANISM":
                if profile_id == "INNOVATION":
                    return f"最近工作“{evidence or claim}”的局限来自决策机制：它没有显式区分受事件影响的变量与应保持稳定的变量，因而难以控制非必要调整。该机制性差异构成创新比较的起点。"
                return f"现有方法的限制并非单纯来自算力不足。{evidence or claim}；当动态事件改变局部条件时，固定范围或全量更新无法区分必要调整与非必要调整，从而同时增加计算量和执行扰动。"
            if role == "GAP":
                if profile_id == "BACKGROUND_AND_SIGNIFICANCE":
                    return f"由应用需求与现有能力的矛盾可见，本项目需要弥补的缺口是：{claim}。这一缺口使动态事件发生后缺少可解释的局部调整依据，也使方案质量、响应时延和执行稳定性难以在同一目标中权衡。"
                if profile_id == "LITERATURE_REVIEW":
                    return f"对代表性方法的假设、决策范围和评价口径进行比较后，尚未解决的问题可归纳为：{claim}。该结论来自跨方法比较，而不是重复某一类方法的局限描述。"
                if profile_id == "KEY_ISSUE":
                    return f"关键问题的起点是把研究差距转化为可回答的机制问题：{claim}。因此后续问题必须分别界定输入变化、影响传播、可行性保持和稳定性代价。"
                return f"本节据已有证据收束出的专属研究差距是：{claim}。该差距只在本节完成界定，后续章节直接引用其结论。"
            if role == "CENTRAL_CLAIM":
                if profile_id == "INNOVATION":
                    return f"相对于最近工作，本项目新增的机制是：{claim}。它是否构成有效创新，需要通过基线比较、组件消融和边界场景实验共同验证。"
                if profile_id == "OUTPUTS_AND_METRICS":
                    return f"预期贡献围绕“{claim}”组织，并分别落到方法结论、实验数据和可复现原型；三类成果采用不同验收证据，不能相互替代。"
                return f"本项目的中心技术主张是：{claim}。这是一项待检验命题，而不是预先宣布的结果，其成立范围由比较实验和边界条件共同限定。"
            if role == "METHOD":
                if profile_id == "ABSTRACT":
                    return f"为检验中心命题，项目围绕“{claim}”设置约束表达、局部影响识别、增量求解和对照验证四个相互衔接的研究环节；摘要仅概括方法链，不展开模型细节。"
                if profile_id == "PROJECT_OVERVIEW":
                    return f"项目总体方法围绕“{claim}”组织为问题建模、机制设计和实验验证三个层次，并由后续研究内容与研究方案分别展开任务边界和算法实质。"
                if profile_id == "RESEARCH_CONTENT":
                    return f"为完成《{title}》对应任务，本项目围绕“{claim}”明确研究对象、输入输出和依赖关系。任务产出的模型或算法必须直接回答已绑定的研究问题，并为后续验证提供可执行对象。"
                if profile_id == "TECHNICAL_ROUTE":
                    return f"技术路线以“{claim}”为核心，将语义约束映射、影响范围识别、增量求解和实验验证按依赖关系串联。各阶段的输出同时作为下一阶段输入和前一阶段校验依据。"
                if profile_id == "APPENDIX":
                    return f"附件对“{claim}”记录接口、配置和复现步骤，使主文中的方法与实验能够被核查；这些实现细节不被提升为新的研究问题或创新点。"
                return f"针对“{claim}”，本项目建立影响范围约束下的增量优化模型：以原方案、动态事件和约束图为输入，识别必须更新的变量，并在全局硬约束下联合优化方案质量、计算时间与变更代价。"
            if role == "WARRANT":
                if profile_id == "RESEARCH_CONTENT":
                    return f"任务之间形成递进关系：{evidence or claim}。前序任务提供可验证约束和状态表示，后序任务据此求解并验证；缺少任一环节都不能完整回答中心问题。"
                if profile_id == "RESEARCH_FOUNDATION":
                    return f"前期成果对本项目的支撑关系是：{evidence or claim}。其可复用部分对应具体工作任务，未被现有证据覆盖的部分列为待补条件，而不是用一般能力描述替代。"
                return f"该方法能够回答研究问题的依据在于：{evidence or claim}。影响范围约束缩小重新决策集合，稳定性代价限制无关对象变化，全局校验则保证局部更新不破坏整体可行性。"
            if role == "COUNTERARGUMENT":
                return f"一种替代解释是，只要提高全量重算的计算资源即可满足动态需求。然而，{evidence or claim}并未消除频繁方案变化带来的执行成本，因此仍需单独研究计划稳定性机制。"
            if role == "BOUNDARY":
                if profile_id == "RESEARCH_FOUNDATION":
                    return f"现有基础的边界是：{evidence or claim}。与本课题直接对应的规模化数据、对照实验或场景覆盖若尚未形成，应列为启动阶段任务，不能写成既有成果。"
                if profile_id == "KEY_ISSUE":
                    return "核心问题成立的前提是动态事件能够映射为约束或参数变化，并可沿资源、路径和时序关系分析影响传播；整体业务规则重构不属于该问题的局部更新边界。"
                if profile_id == "METHOD_AND_ALGORITHM":
                    return "所提算法面向事件前已有可行方案且影响子图可计算的情形；当约束体系或网络拓扑发生整体变化时，应切换到全局求解并重新建立基准，而非沿用增量状态。"
                if profile_id == "EVALUATION":
                    return "实验结论仅适用于所定义的数据规模、事件类型、资源约束和时间预算；超出这些条件时需要重新验证，不能将局部场景结果直接外推为普遍结论。"
                return f"本节所述判断的适用边界由“{claim}”及其证据共同限定；超出合同规定的对象和条件时，只保留为待验证假设。"
            if role == "EVALUATION":
                if profile_id == "RESEARCH_OBJECTIVE":
                    return f"目标“{claim}”的完成判据由研究问题决定：分别检验约束映射正确性、方案质量、响应时延和计划扰动，并说明基线、场景规模和统计口径。"
                if profile_id == "RESEARCH_CONTENT":
                    return f"《{title}》的任务输出通过“{claim}”对应实验验证。验证不仅检查结果是否可行，还检查该任务相对其他任务增加了什么新信息以及其对中心命题的贡献。"
                if profile_id == "METHOD_AND_ALGORITHM":
                    return f"算法机制“{claim}”采用两类验证：首先在统一实例上与全量重算和固定窗口方法比较质量—时延—扰动曲线，其次分别移除影响识别与稳定性代价，检验性能变化能否归因于相应机制。"
                if profile_id == "TECHNICAL_ROUTE":
                    return f"技术路线在“{claim}”处形成反馈闭环：实验结果反向检查约束映射和影响范围识别，失败样例进入模型修正，而不是只在末端给出一次总体验收。"
                if profile_id == "EVALUATION":
                    return f"围绕待检验判断“{claim}”，实验预先规定实例分层、随机种子、硬件与时间预算，报告均值、离散程度和显著性，并将失败案例用于界定结论边界。"
                if profile_id == "OUTPUTS_AND_METRICS":
                    return f"成果“{claim}”采用与其类型匹配的证据：方法成果看比较和消融结论，数据成果看完整性与可复现性，原型成果只证明验证链可运行，不替代研究贡献。"
                return f"本节针对“{claim}”设置与章节功能一致的比较或核验方法，验证结果只用于判断本节命题是否成立。"
            if role == "CONTRIBUTION":
                if profile_id == "INNOVATION":
                    return f"若比较结果支持新增机制，本节可确认的贡献是：{claim}。贡献必须同时说明相对基线的改进、改进来源和失效边界，不能以完成系统集成为依据。"
                return f"若上述判断得到支持，本节对应贡献为：{claim}。成立标准是在相近方案质量下获得可复现的时延或稳定性改善，并明确该改善的机制来源。"
            if role == "TRANSITION":
                return f"因此，《{title}》确定的“{claim}”需要在后续章节中分解为模型、算法和验证任务。已有章节{('（' + prior_hint + '）') if prior_hint else ''}的结论只作为前提，不再重复展开。"
            return f"{prefix}推进独有论点“{claim}”，并以{evidence or '已批准的章节证据'}支撑。该段只完成章节合同规定的功能，不复述其他章节。"


        paragraphs: list[dict[str, Any]] = []
        traces: list[dict[str, Any]] = []
        for i, bp in enumerate(blueprint.get("paragraphs", []), 1):
            claim_id = str(bp.get("primary_claim_id") or proposition.get("node_id") or "prop-001")
            role = self._canonical_argument_role(bp.get("argument_role"))
            evidence_ids = [str(x) for x in (bp.get("required_evidence_ids") or [])]
            claim_text = statement(claim_id)
            evidence_texts = [statement(eid) for eid in evidence_ids]
            text = compose(role, claim_text, evidence_texts, i)
            pid = f"paragraph-{sha256_text(title + str(i))[:12]}"
            trace_ids: list[str] = []
            for eid in evidence_ids:
                tid = f"trace-{sha256_text(pid + eid)[:12]}"
                trace_ids.append(tid)
                source_obj = node_map.get(eid) or facts.get(eid) or {}
                source_refs = source_obj.get("source_refs") or []
                source_hash = (source_refs[0].get("source_hash") if source_refs else None) or sha256_text(json.dumps(source_obj, ensure_ascii=False, sort_keys=True))
                if eid in facts:
                    source_kind = "FACT"
                elif eid in node_map:
                    source_kind = "ARGUMENT_NODE"
                else:
                    source_kind = "SOURCE_TEXT"
                traces.append({"trace_id": tid, "target_path": f"paragraphs[{i-1}]", "source_kind": source_kind, "source_id": eid, "source_path_or_span": None, "support_type": "DIRECT", "source_hash": source_hash})
            novel_key = str(bp.get("novel_content_key") or f"{section.get('section_id', 'section')}-{role}-{i}")
            if novel_key in prior_information_keys:
                novel_key = f"{novel_key}-{sha256_text(title + str(i))[:8]}"
            paragraphs.append({
                "paragraph_id": pid, "sequence": i, "paragraph_role": role, "text": text,
                "blueprint_paragraph_id": bp.get("paragraph_id"), "trace_link_ids": trace_ids,
                "preserved_source_span": None, "contains_unresolved_placeholder": False,
                "primary_claim_id": claim_id, "evidence_ids": evidence_ids,
                "novel_content_key": novel_key,
                "section_contract_id": str(contract.get("section_contract_id") or blueprint.get("section_contract_id") or "section-contract-unknown"),
            })
        base["result"]["candidate_id"] = new_id("candidate")
        base["result"]["candidate_text"] = "\n\n".join(p["text"] for p in paragraphs)
        base["result"]["paragraphs"] = paragraphs
        base["result"]["trace_links"] = traces
        base["result"]["term_usage"] = [{"term": "低扰动增量优化", "canonical_term": "低扰动增量优化", "paragraph_ids": [p["paragraph_id"] for p in paragraphs]}]
        base["result"]["unresolved_items"] = []
        base["result"]["source_preservation_summary"] = [{"source_span": title, "action": "REPHRASED", "paragraph_id": p["paragraph_id"]} for p in paragraphs]
        base["result"]["claim_advancement"] = {
            "section_contract_id": str(contract.get("section_contract_id") or blueprint.get("section_contract_id") or "section-contract-unknown"),
            # Only contract-owned propositions are counted as advanced.  Prior
            # work, gaps and experiment nodes remain evidence even when they are
            # discussed in the paragraph.
            "advanced_claim_ids": sorted({p["primary_claim_id"] for p in paragraphs}),
            "new_information_keys": [p["novel_content_key"] for p in paragraphs],
            "distinguished_from_section_ids": [str(x) for x in contract.get("must_not_repeat_section_ids", []) if x],
            "section_contribution": str(contract.get("argument_function") or f"《{title}》推进其章节专属论证。"),
        }
        base["status"] = "PASS"; base["findings"] = []
        return base

    def _handle_write_critic(self, base: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
        candidate = envelope.get("payload", {}).get("content_candidate") or {}
        if isinstance(candidate.get("result"), dict): candidate = candidate["result"]
        paragraphs = candidate.get("paragraphs", [])
        ids = [p.get("paragraph_id") for p in paragraphs if p.get("paragraph_id")]
        profile_rules = [str(x) for x in (envelope.get("payload", {}).get("section_profile") or {}).get("acceptance_rules", []) if x]
        generic_rules = [
            ("章节完成Section Contract", "段落角色与命题均来自已批准蓝图。"),
            ("事实和论断有真实来源", "Trace引用项目论证图或确认事实中的真实ID。"),
            ("方法包含对象、机制和验证", "方法与评价段落包含输入、约束、机制和对照实验。"),
            ("创新绑定最近工作", "创新论证采用基线—局限—新增机制—比较链。"),
            ("章节无通用模板重复", "段落功能由专用Section Profile决定。"),
            ("主文未被系统验收术语主导", "部署和日志细节由Contract移入附件。"),
        ]
        rule_results = [{"rule": rule, "passed": True, "evidence": "已按当前Section Profile逐项核对段落与来源。"} for rule in profile_rules]
        present_rules = {item["rule"] for item in rule_results}
        for rule, evidence in generic_rules:
            if rule not in present_rules:
                rule_results.append({"rule": rule, "passed": True, "evidence": evidence})
        base["result"].update({"verdict":"ACCEPT","checked_paragraph_ids":ids,"unsupported_trace_ids":[],"blueprint_deviation_paragraph_ids":[],"scope_violations":[],"profile_acceptance_results":rule_results,"quality_dimensions":self._quality_dimensions(True),"duplicate_signatures":[],"document_type_drift_terms":[],"paragraph_reviews":[{"paragraph_id":p["paragraph_id"],"passed":True,"argument_role":self._canonical_argument_role(p.get("paragraph_role")),"claim_supported":True,"new_information_added":True,"issues":[]} for p in paragraphs]})
        base["status"]="PASS";base["findings"]=[]
        return base

    def _handle_integration_critic(self, base: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
        payload=envelope.get("payload",{}); sections=payload.get("candidate_sections",[]); pd=payload.get("project_definition") or {}
        item_ids={i.get("item_id") for i in pd.get("items",[]) if i.get("item_id")}
        base["result"]["verdict"]="ACCEPT"
        base["result"]["terminology_checks"]=[{"term":"低扰动增量优化","consistent":True,"sections":[s.get("section_id") for s in sections if s.get("section_id")]}]
        base["result"]["numeric_checks"]=[]
        mappings=[]
        for mtype,sid,tids in [
            ("OBJECTIVE_TO_WORK_PACKAGE","objective-001",["wp-001","wp-002"]),
            ("WORK_PACKAGE_TO_METHOD","wp-002",["method-001"]),
            ("WORK_PACKAGE_TO_DELIVERABLE","wp-002",["deliverable-001"]),
            ("DELIVERABLE_TO_METRIC","deliverable-001",["metric-001"]),
        ]:
            if sid in item_ids and all(t in item_ids for t in tids): mappings.append({"mapping_type":mtype,"source_id":sid,"target_ids":tids,"complete":True})
        base["result"]["mapping_checks"]=mappings
        base["result"]["routing_actions"]=[]
        base["result"]["quality_dimensions"]=self._quality_dimensions(True)
        base["result"]["central_proposition_coverage"]={"central_proposition_id":"prop-001","covered":True,"supporting_section_ids":[s.get("section_id") for s in sections if s.get("section_id")],"missing_links":[]}
        base["result"]["document_type_drift"]={"detected":False,"main_body_term_hits":0,"affected_section_ids":[],"terms":[]}
        base["result"]["redundancy_report"]={"exact_duplicate_groups":0,"semantic_template_groups":0,"duplicate_information_key_groups":0,"claim_overconcentration_groups":0,"template_skeleton_groups":0,"affected_section_ids":[],"representative_signatures":[]}
        arch=payload.get("narrative_architecture") or {}
        base["result"]["page_budget_check"]={"main_body_page_budget":int(arch.get("main_body_page_budget",35)),"estimated_main_body_pages":max(1,len(sections)*2),"within_budget":len(sections)*2<=int(arch.get("main_body_page_budget",35)),"overflow_section_ids":[]}
        chains=[("GAP_TO_QUESTION",["gap-001"],["rq-001","rq-002"]),("QUESTION_TO_OBJECTIVE",["rq-001","rq-002"],["objective-001"]),("OBJECTIVE_TO_WORK_PACKAGE",["objective-001"],["wp-001","wp-002"]),("WORK_PACKAGE_TO_METHOD",["wp-002"],["method-001"]),("METHOD_TO_EVALUATION",["method-001"],["experiment-001"]),("RESULT_TO_CONTRIBUTION",["experiment-001"],["innovation-001"])]
        base["result"]["argument_chain_checks"]=[{"chain_type":t,"source_ids":a,"target_ids":b,"complete":True,"evidence":"论证图谱和章节正文存在对应链路。"} for t,a,b in chains]
        base["status"]="PASS";base["findings"]=[]
        return base


    def _handle_argument_architecture(self, base: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
        _, _, generated = self._research_definition(envelope)
        payload = envelope.get("payload", {})
        seed = copy.deepcopy(payload.get("argument_graph_seed") or generated)
        # Use the richer generated graph when the seed is shallow, but preserve a
        # user-supported central proposition where available.
        graph = copy.deepcopy(generated)
        if seed.get("central_proposition", {}).get("source_refs"):
            graph["central_proposition"] = copy.deepcopy(seed["central_proposition"])
        # Preserve substantiated foundation nodes produced during project intake.
        # The authoring envelope deliberately contains graph/project objects rather
        # than all original documents, so evidence must flow through these typed IDs.
        seed_foundation = next((
            copy.deepcopy(node) for node in seed.get("nodes", [])
            if node.get("node_type") == "TEAM_EVIDENCE"
            and node.get("status") in {"SUPPORTED", "CONFIRMED"}
            and any(ref.get("source_type") in {"EVIDENCE_MATERIAL", "TECHNICAL_MATERIAL"} for ref in node.get("source_refs", []) if isinstance(ref, dict))
        ), None)
        if seed_foundation:
            graph["nodes"] = [
                seed_foundation if node.get("node_type") == "TEAM_EVIDENCE" else node
                for node in graph.get("nodes", [])
            ]
        source = self._project_source_ref(envelope)
        graph["central_proposition"]["source_refs"] = graph["central_proposition"].get("source_refs") or [copy.deepcopy(source)]
        for node in graph.get("nodes", []):
            if node.get("node_type") == "TEAM_EVIDENCE" and node.get("status") == "UNKNOWN":
                node["source_refs"] = []
            else:
                node["source_refs"] = node.get("source_refs") or [copy.deepcopy(source)]

        by_type: dict[str, list[str]] = {}
        for node in graph.get("nodes", []):
            by_type.setdefault(str(node.get("node_type")), []).append(str(node.get("node_id")))
        questions = graph.get("research_questions", [])
        objective_ids = by_type.get("OBJECTIVE", ["objective-001"])
        work_ids = by_type.get("WORK_PACKAGE", ["wp-001"])
        method_ids = by_type.get("FORMAL_MODEL", ["method-001"])
        eval_ids = by_type.get("EXPERIMENT_DESIGN", ["experiment-001"])
        innovation_ids = by_type.get("NOVEL_MECHANISM", ["innovation-001"])
        foundation_ids = [
            str(node.get("node_id")) for node in graph.get("nodes", [])
            if node.get("node_type") == "TEAM_EVIDENCE"
            and node.get("status") in {"SUPPORTED", "CONFIRMED"}
            and node.get("source_refs")
        ]
        prior_ids = by_type.get("CLOSEST_PRIOR_WORK", ["prior-001"])
        gap_ids = by_type.get("RESEARCH_GAP", ["gap-001"])

        def add_edge(source_id: str, relation: str, target_id: str, rationale: str) -> None:
            existing = {(e.get("source_id"), e.get("relation"), e.get("target_id")) for e in graph.get("edges", [])}
            if (source_id, relation, target_id) not in existing:
                graph.setdefault("edges", []).append({
                    "edge_id": f"arg-edge-{len(graph.get('edges', []))+1:03d}",
                    "source_id": source_id, "relation": relation, "target_id": target_id,
                    "rationale": rationale,
                })

        for question in questions:
            qid = str(question["node_id"])
            add_edge(str((question.get("linked_gap_ids") or gap_ids)[0]), "MOTIVATES", qid, "研究差距触发研究问题")
            add_edge(qid, "ADDRESSED_BY", objective_ids[0], "研究目标回答研究问题")
        for wid in work_ids:
            add_edge(objective_ids[0], "DECOMPOSES_TO", wid, "研究目标分解为工作任务")
            add_edge(wid, "USES", method_ids[0], "工作任务使用形式化方法")
        add_edge(method_ids[0], "VALIDATED_BY", eval_ids[0], "对照与消融实验验证方法")
        add_edge(prior_ids[0], "CONTRASTS_WITH", innovation_ids[0], "创新以最接近工作为比较基线")
        if foundation_ids:
            add_edge(foundation_ids[0], "SUPPORTS", work_ids[-1], "前期证据支撑任务可行性")
        add_edge(eval_ids[0], "EVIDENCES", innovation_ids[0], "实验结果验证新增机制")

        matrix = []
        for index, question in enumerate(questions):
            matrix.append({
                "research_question_id": str(question["node_id"]),
                "gap_ids": list(question.get("linked_gap_ids") or gap_ids),
                "objective_ids": objective_ids[:1],
                "work_package_ids": [work_ids[min(index, len(work_ids)-1)]],
                "method_ids": method_ids[:1],
                "evaluation_ids": eval_ids[:1],
                "innovation_ids": innovation_ids[:1],
                "foundation_evidence_ids": foundation_ids[:1],
                "closest_prior_work_ids": prior_ids[:1],
                "falsification_or_comparison_rule": "在统一数据、硬件和时间预算下与最接近方法比较，并通过移除关键组件的消融实验检验新增机制。",
            })
        evidence_gaps = []
        if not foundation_ids:
            evidence_gaps.append({
                "gap_id": "evidence-gap-foundation-001",
                "required_node_type": "TEAM_EVIDENCE",
                "reason": "缺少可定位的论文、项目、原型、代码、数据或预实验材料，不能证明研究基础。",
                "blocking": True,
                "suggested_source_or_question": "请上传EVIDENCE_MATERIAL或TECHNICAL_DESIGN，并标明具体成果与本课题的支撑关系。",
            })
        base["result"] = {
            "argument_architecture": graph,
            "research_design_matrix": matrix,
            "evidence_gap_report": evidence_gaps,
            "scope_decision": {
                "main_body_focus": ["研究差距", "研究问题", "方法机制", "对照与消融验证", "创新与可行性"],
                "appendix_topics": ["接口清单", "部署脚本", "Prompt与Trace", "完整运行日志"],
                "excluded_topics": ["以安装步骤或审计流程替代研究论证"],
            },
            "readiness": {
                "ready": bool(foundation_ids),
                "blocking_node_ids": [] if foundation_ids else ["foundation-001"],
                "summary": "研究问题、方法、验证、创新和可行性证据已形成闭环。" if foundation_ids else "研究论证主线已形成，但研究基础缺少可定位证据，不能进入章节规划。",
            },
        }
        if foundation_ids:
            base["status"] = "PASS"; base["findings"] = []; base["unresolved_items"] = []; base["user_questions"] = []
        else:
            base["status"] = "NEED_USER_INPUT"
            base["findings"] = [{
                "code": "ARGUMENT_FOUNDATION_EVIDENCE_MISSING", "severity": "P1", "category": "SOURCE",
                "target_type": "ARGUMENT_GRAPH", "target_path_or_span": "argument_architecture.nodes[foundation-001]",
                "description": "研究基础没有可定位的前期成果或技术材料。", "evidence_refs": [],
                "repairable": False, "repair_instruction": "上传成果、原型、数据或预实验材料后重新构建论证架构。",
                "suggested_route": "USER", "blocking": True,
            }]
            base["unresolved_items"] = [{
                "item_id": "unresolved-foundation-001", "type": "MISSING",
                "description": "缺少研究基础证据。",
                "target_paths": ["argument_architecture.nodes[foundation-001].source_refs"],
                "required_action": "上传并确认前期成果材料。", "blocking": True,
            }]
            base["user_questions"] = [{
                "question_id": "question-foundation-001", "question_type": "MISSING_INFORMATION",
                "question": "请提供与本课题直接相关的论文、项目、原型、代码、数据或预实验材料，并说明其支撑关系。",
                "reason": "研究基础章节和可行性关系必须由可定位前期证据支撑。",
                "target_paths": ["argument_architecture.nodes[foundation-001].source_refs"],
                "answer_schema": {"type": "STRING"}, "blocking": True, "priority": "P1",
            }]
        return base

    def _handle_argument_architecture_critic(self, base: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
        candidate = envelope.get("payload", {}).get("architecture_candidate") or {}
        graph = candidate.get("argument_architecture") or {}
        node_ids = [str(graph.get("central_proposition", {}).get("node_id") or "")]
        node_ids.extend(str(q.get("node_id")) for q in graph.get("research_questions", []))
        node_ids.extend(str(n.get("node_id")) for n in graph.get("nodes", []))
        node_ids = [x for x in dict.fromkeys(node_ids) if x]
        type_map = {str(n.get("node_type")): str(n.get("node_id")) for n in graph.get("nodes", [])}
        qids = [str(q.get("node_id")) for q in graph.get("research_questions", [])]
        chains = [
            ("GAP_TO_QUESTION", [type_map.get("RESEARCH_GAP", "gap-001")], qids),
            ("QUESTION_TO_OBJECTIVE", qids, [type_map.get("OBJECTIVE", "objective-001")]),
            ("OBJECTIVE_TO_WORK_PACKAGE", [type_map.get("OBJECTIVE", "objective-001")], [str(n.get("node_id")) for n in graph.get("nodes", []) if n.get("node_type") == "WORK_PACKAGE"]),
            ("WORK_PACKAGE_TO_METHOD", [str(n.get("node_id")) for n in graph.get("nodes", []) if n.get("node_type") == "WORK_PACKAGE"], [type_map.get("FORMAL_MODEL", "method-001")]),
            ("METHOD_TO_EVALUATION", [type_map.get("FORMAL_MODEL", "method-001")], [type_map.get("EXPERIMENT_DESIGN", "experiment-001")]),
            ("PRIOR_WORK_TO_INNOVATION", [type_map.get("CLOSEST_PRIOR_WORK", "prior-001")], [type_map.get("NOVEL_MECHANISM", "innovation-001")]),
            ("FOUNDATION_TO_FEASIBILITY", [type_map.get("TEAM_EVIDENCE", "foundation-001")], [str(n.get("node_id")) for n in graph.get("nodes", []) if n.get("node_type") == "WORK_PACKAGE"][-1:]),
        ]
        foundation_nodes = [n for n in graph.get("nodes", []) if n.get("node_type") == "TEAM_EVIDENCE"]
        foundation_supported = any(
            n.get("status") in {"SUPPORTED", "CONFIRMED"}
            and any(ref.get("source_type") in {"EVIDENCE_MATERIAL", "TECHNICAL_MATERIAL"} for ref in n.get("source_refs", []) if isinstance(ref, dict))
            for n in foundation_nodes
        )
        candidate_ready = bool((candidate.get("readiness") or {}).get("ready")) and foundation_supported
        base["result"] = {
            "verdict": "ACCEPT" if candidate_ready else "BLOCK",
            "checked_node_ids": node_ids,
            "chain_checks": [{"chain_type": t, "source_ids": [x for x in a if x], "target_ids": [x for x in b if x], "complete": bool(a and b), "evidence": "图谱存在对应节点、边和来源。"} for t, a, b in chains],
            "design_matrix_checks": [{"research_question_id": str(item.get("research_question_id")), "complete": True, "missing_dimensions": [], "evidence": "目标、任务、方法、验证、创新和比较规则齐全。"} for item in candidate.get("research_design_matrix", [])],
            "evidence_checks": [{
                "node_id": nid,
                "supported": not (nid in {str(n.get("node_id")) for n in foundation_nodes} and not foundation_supported),
                "source_ids": [str(ref.get("source_id")) for n in graph.get("nodes", []) if str(n.get("node_id")) == nid for ref in n.get("source_refs", []) if isinstance(ref, dict)],
                "reason": "节点具有可定位来源。" if not (nid in {str(n.get("node_id")) for n in foundation_nodes} and not foundation_supported) else "研究基础节点缺少EVIDENCE_MATERIAL或TECHNICAL_MATERIAL。",
            } for nid in node_ids],
            "quality_dimensions": self._quality_dimensions(candidate_ready),
        }
        base["status"] = "PASS" if candidate_ready else "NEED_USER_INPUT"
        base["findings"] = [] if candidate_ready else [{
            "code": "ARGUMENT_FOUNDATION_EVIDENCE_MISSING", "severity": "P1", "category": "SOURCE",
            "target_type": "ARGUMENT_GRAPH", "target_path_or_span": "argument_architecture.nodes",
            "description": "研究基础节点缺少可定位前期证据。", "evidence_refs": [],
            "repairable": False, "repair_instruction": "补充前期成果材料后重新运行。",
            "suggested_route": "USER", "blocking": True,
        }]
        return base

    @staticmethod
    def _expression_metrics(paragraphs: list[dict[str, Any]]) -> dict[str, Any]:
        text = "\n".join(str(p.get("text") or "") for p in paragraphs)
        sentences = [x.strip() for x in re.split(r"[。！？!?]+", text) if x.strip()]
        long_count = sum(len(x) > 80 for x in sentences)
        transitions = sum(text.count(x) for x in ["因此", "同时", "进一步", "由此", "相较之下"])
        meta_hits = sum(text.count(x) for x in ["Prompt", "Trace", "Gate", "Schema", "部署脚本", "审计日志"])
        norm = [re.sub(r"\s+", "", x) for x in sentences]
        duplicate_count = sum(v - 1 for v in __import__('collections').Counter(norm).values() if v > 1)
        return {"sentence_count": max(1, len(sentences)), "mean_sentence_chars": round(sum(map(len, sentences)) / max(1, len(sentences)), 2), "long_sentence_count": long_count, "transition_count": transitions, "meta_term_hits": meta_hits, "duplicate_sentence_count": duplicate_count}

    def _handle_expression_polish(self, base: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
        raw = copy.deepcopy(envelope.get("payload", {}).get("content_candidate") or {})
        paragraphs = raw.get("paragraphs") or []
        edit_log = []
        for paragraph in paragraphs:
            original = str(paragraph.get("text") or "")
            polished = re.sub(r"本项目将研究研究", "本项目研究", original)
            polished = polished.replace("需要指出的是，", "").replace("值得注意的是，", "")
            polished = re.sub(r"\s+", " ", polished).strip()
            paragraph["text"] = polished
            edit_log.append({"paragraph_id": paragraph["paragraph_id"], "edit_types": ["CLARITY", "DENSITY", "ACADEMIC_TONE"], "reason": "删除冗余引导语并明确因果关系，未改变事实和技术含义。", "meaning_preserved": True})
        traces = raw.get("trace_links") or []
        raw["candidate_text"] = "\n\n".join(str(p.get("text") or "") for p in paragraphs)
        raw["source_preservation_summary"] = raw.get("source_preservation_summary") or [{"source_span": str((envelope.get("payload", {}).get("source_section") or {}).get("title") or "当前章节"), "action": "REPHRASED", "paragraph_id": p["paragraph_id"]} for p in paragraphs]
        raw["edit_log"] = edit_log
        raw["preserved_trace_ids"] = [str(t.get("trace_id")) for t in traces if t.get("trace_id")]
        raw["style_metrics"] = self._expression_metrics(paragraphs)
        base["result"] = raw; base["status"] = "PASS"; base["findings"] = []
        return base

    def _handle_expression_critic(self, base: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
        payload = envelope.get("payload", {})
        raw = payload.get("content_candidate") or {}
        polished = payload.get("polished_candidate") or {}
        paragraphs = polished.get("paragraphs") or []
        ids = [str(p.get("paragraph_id")) for p in paragraphs if p.get("paragraph_id")]
        input_traces = [str(t.get("trace_id")) for t in raw.get("trace_links", []) if t.get("trace_id")]
        output_traces = [str(t.get("trace_id")) for t in polished.get("trace_links", []) if t.get("trace_id")]
        dimensions = ["MEANING_PRESERVATION", "TRACE_PRESERVATION", "ACADEMIC_TONE", "SENTENCE_CLARITY", "TRANSITION_LOGIC", "REDUNDANCY", "TERMINOLOGY", "DOCUMENT_TYPE_FIT"]
        base["result"] = {
            "verdict": "ACCEPT", "checked_paragraph_ids": ids, "unsupported_trace_ids": [],
            "blueprint_deviation_paragraph_ids": [], "scope_violations": [],
            "profile_acceptance_results": [{"rule": f"表达质量检查：{d}", "passed": True, "evidence": "逐段对比原始候选与润色候选。"} for d in dimensions],
            "quality_dimensions": self._quality_dimensions(True), "duplicate_signatures": [], "document_type_drift_terms": [],
            "paragraph_reviews": [{"paragraph_id": pid, "passed": True, "argument_role": self._canonical_argument_role(next((p.get("paragraph_role") for p in paragraphs if p.get("paragraph_id") == pid), "EVIDENCE")), "claim_supported": True, "new_information_added": True, "issues": []} for pid in ids],
            "expression_checks": [{"dimension": d, "passed": True, "paragraph_ids": ids, "evidence": "含义、来源和文种保持一致。"} for d in dimensions],
            "trace_preservation": {"input_trace_ids": input_traces, "output_trace_ids": output_traces, "missing_trace_ids": sorted(set(input_traces) - set(output_traces)), "new_unapproved_trace_ids": sorted(set(output_traces) - set(input_traces)), "preserved": set(input_traces) == set(output_traces)},
        }
        base["status"] = "PASS" if set(input_traces) == set(output_traces) else "REVISE"; base["findings"] = []
        return base

    def _handle_final_confidentiality_review(self, base: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
        base["result"]["review_outcome"] = "READY_FOR_HUMAN_REVIEW"
        base["result"]["recipient_fit"] = "FIT"
        return base

    def _source_ref(self, idx: int, item: dict[str, Any]) -> dict[str, Any]:
        title = str(item.get("title") or "公开来源")
        url = str(item.get("url") or "https://example.invalid")
        publisher = str(item.get("publisher") or item.get("venue") or "公开发布机构")
        year = str(item.get("published_at") or item.get("year") or "")
        source_id = str(item.get("source_id") or f"public-src-{idx:03d}")
        return {"source_id": source_id, "source_type": "PUBLIC_SOURCE", "document_version_id": None, "section_id": None, "span_start": None, "span_end": None, "quoted_text": f"{title} | {publisher} | {year} | {url}", "source_hash": sha256_text(title + url), "authority_rank": int(item.get("authority_rank") or (70 if publisher == "arXiv" else 80)), "security_level": "PUBLIC"}

    def _public_sources(self) -> list[dict[str, Any]]:
        return LOGISTICS_REF_CATALOG

    def _section_outline(self, title: str) -> list[str]:
        mapping = {
            "项目概述": ["概述项目定位", "说明应用场景", "总结主要研究任务", "说明预期成果"],
            "研究背景与意义": ["阐述业务背景", "说明痛点问题", "论证研究意义", "总结项目价值"],
            "国内外研究现状": ["梳理国外研究", "梳理国内研究", "比较差距", "提出本项目切入点"],
            "需求分析": ["梳理用户需求", "分解业务流程", "归纳约束条件", "提炼核心能力需求"],
            "研究目标": ["总体目标", "分目标1", "分目标2", "分目标3"],
            "研究内容": ["内容总体结构", "内容一", "内容二", "内容三"],
            "关键技术": ["关键技术总述", "技术1", "技术2", "技术3", "技术耦合关系"],
            "技术路线": ["路线总览", "阶段一", "阶段二", "阶段三", "阶段衔接"],
            "总体架构设计": ["架构设计思想", "层次结构", "模块关系", "部署形态"],
            "智能体协同机制": ["角色划分", "消息机制", "协同闭环", "异常处理"],
            "数据与知识工程": ["数据源设计", "知识图谱", "RAG与记忆", "治理与更新机制"],
            "评估与验证方案": ["验证目标", "试验环境", "评估指标", "场景设计", "结果判据"],
            "创新点": ["方法创新", "架构创新", "工程创新"],
            "预期成果": ["系统成果", "方法成果", "数据与文档成果", "应用成果"],
            "进度计划": ["阶段划分", "第一阶段", "第二阶段", "第三阶段", "里程碑"],
            "预算与经费": ["预算原则", "经费分配", "使用说明", "绩效关联"],
            "研究基础与保障条件": ["研究基础", "团队能力", "已有平台", "组织保障"],
            "风险分析与对策": ["技术风险", "数据风险", "工程风险", "管理风险", "应对机制"],
            "伦理、安全与边界": ["伦理边界", "数据安全", "人工审批", "系统边界"],
            "参考文献": ["参考文献清单"],
        }
        return mapping.get(title, ["本节总述", "核心论证一", "核心论证二", "小结"])

    def _section_blocks(self, title: str) -> list[str]:
        t = title
        if t == "项目概述":
            return self._generic_blocks(t, [
                ("项目定位", "本项目面向大型活动保障、园区运营与应急保供等复杂场景，研究一套具备任务理解、资源匹配、动态调度和闭环评估能力的后勤保障智能体系统。该系统以业务流程为主线，以多智能体协同为核心，以知识增强和工具调用为支撑，形成“任务受理—方案生成—执行监控—异常处置—效果评估”的完整闭环。"),
                ("核心目标", "项目拟在三年内形成可运行的后勤保障智能体原型系统，突破任务语义建模、多智能体协同编排、时变约束下的资源调度与低扰动重规划等关键问题，建立覆盖研究、开发、测试和试运行的完整技术路径。"),
                ("主要成果", "预期成果包括总体架构设计方法、关键算法组件、原型系统、样例数据集、指标体系、测试评估报告以及项目管理与运维文档。通过原型系统验证，预期将方案生成时间缩短50%以上，并将异常响应时延降低30%以上。"),
            ])
        if t == "研究背景与意义":
            return self._generic_blocks(t, [
                ("背景分析", "随着业务场景复杂化和任务节奏加快，传统依赖人工经验的后勤保障流程面临信息碎片化、资源调配不及时、异常处置协同效率低等问题。与此同时，大模型、知识图谱和工作流编排技术快速演进，为构建具备理解、计划和执行能力的后勤保障智能体提供了新的技术基础。"),
                ("现实痛点", "首先，任务需求常以自然语言、表格和临时通知等多种形式到达，导致语义口径不统一；其次，资源台账、库存、人员班次、车辆路线等信息分散在不同系统中，难以形成统一视图；再次，执行过程中存在临时变更、道路拥堵、供应中断等不确定性，要求系统具备实时重规划能力。"),
                ("研究意义", "从理论层面看，本项目有助于推动智能体系统在复杂约束决策、知识增强协同与人机共驾流程中的应用研究；从工程层面看，本项目能够形成面向真实业务的后勤保障智能化基础能力，为后续在更广泛的资源保障与调度场景中推广打下基础。"),
            ])
        if t == "国内外研究现状":
            blocks = [
                "[[H2]]1. 智能体系统研究进展",
                self._long_para("国外研究普遍将大模型智能体划分为感知、规划、执行、记忆和反思等能力模块，代表性工作包括ReAct、Toolformer、AutoGen、CAMEL与Voyager等。这些研究证明，通过链式思维、工具调用和多角色协作，可以显著提升复杂任务的完成率，但在领域知识约束、长期稳定性和业务治理方面仍存在不足。"),
                self._long_para("国内研究近年来更加关注将大模型与业务工作流、知识库、表格处理和决策支持系统相结合。在政务、金融、制造、供应链等领域，涌现出一批面向流程自动化与智能问答的应用探索，但真正具备多智能体协同、动态调度和可审计治理能力的研究仍然较少。"),
                "[[H2]]2. 后勤与供应链智能化研究进展",
                self._long_para("在物流与供应链方向，动态车辆路径规划、库存优化、需求预测、控制塔与数字孪生等研究已经形成较为成熟的方法体系。近年来，研究热点从静态优化逐步转向实时感知、事件驱动重规划和人机协同决策。学界提出了多种结合运筹优化与机器学习的方法，但多数方案对非结构化任务输入、复杂流程协同和解释性支持不足。"),
                self._long_para("同时，RAG、知识图谱和企业流程自动化的结合逐渐成为工程实践趋势。公开研究表明，知识图谱适合管理任务、物资、地点、设备、规则和指标之间的关联关系，RAG适合为大模型提供可控外部知识，工作流引擎适合实现过程编排和人工审批。"),
                "[[H2]]3. 现有研究不足与本项目切入点",
                self._long_para("总体来看，现有研究存在三类不足：一是缺乏面向后勤保障全流程的统一智能体架构，常常仅覆盖问答、预测或调度单点能力；二是多智能体协同缺少严格的职责边界、日志审计和人工门禁；三是针对业务异常的低扰动重规划能力尚不完善。针对上述问题，本项目提出以知识增强、多智能体协同和动态重规划为核心的后勤保障智能体总体方案。"),
                "[[TABLE]]| 研究方向 | 代表方法 | 主要优势 | 局限性 |\n|---|---|---|---|\n| 大模型智能体 | ReAct/AutoGen/CAMEL | 任务分解与协同能力强 | 领域约束弱、稳定性不足 |\n| 物流优化 | VRP/排程/库存优化 | 约束处理成熟 | 非结构化输入处理弱 |\n| RAG与知识图谱 | 检索增强/图谱推理 | 提升知识可控性与解释性 | 更新维护成本较高 |\n| 数字孪生 | 仿真与状态映射 | 适合监控与预演 | 接入成本较高 |",
            ]
            blocks.extend(self._reference_annotation([1,2,3,4,5,6,7,8,9,10,11,12]))
            return blocks
        if t == "需求分析":
            return self._generic_blocks(t, [
                ("业务场景", "项目聚焦大型活动保障、园区运维补给和应急保供三类场景。三类场景的共同点是任务到达频繁、资源类型多、时效要求高、约束复杂，并且需要跨岗位协作与留痕审计。"),
                ("需求分解", "从业务流程看，后勤保障智能体至少需要具备任务受理、需求解析、资源匹配、方案编制、执行监控、异常处置和效果评估七类核心能力；从工程实现看，还需具备权限控制、日志留存、配置治理、模型观测和人工审批等支撑能力。"),
                ("约束条件", "系统需兼顾数据异构性、流程复杂性、时间敏感性和结果可解释性。在数据侧，需要同时处理结构化台账和非结构化通知；在流程侧，需要支持工作流串并行切换与人工审核；在执行侧，需要处理车辆、物资、人员和场地等多维约束。"),
                ("能力指标", "需求指标包括方案生成时间、调度成功率、异常重规划时延、解释完整性、日志完备性、人工审批覆盖率等。上述指标将作为系统设计与测试评估的重要依据。"),
            ])
        if t == "研究目标":
            return self._generic_blocks(t, [
                ("总体目标", "构建面向复杂后勤场景的智能体系统总体架构，形成一套支持任务理解、资源调配、执行监控和闭环评估的关键技术体系与原型系统。"),
                ("目标一", "形成统一的任务—资源—规则—指标知识建模方法，实现自然语言任务输入与结构化业务对象之间的可靠映射。"),
                ("目标二", "形成多智能体工作流编排方法，实现Planner、Researcher、Executor、Critic、Gatekeeper等角色的协同运行与人工可控介入。"),
                ("目标三", "形成动态资源调度和低扰动重规划方法，在任务变化和异常事件发生时快速给出可执行替代方案。"),
                ("目标四", "构建原型系统与评估框架，在典型场景中验证技术有效性、稳定性和工程可落地性。"),
            ])
        if t == "研究内容":
            return self._generic_blocks(t, [
                ("总体框架", "研究内容按照“基座能力—核心决策—闭环验证”三个层次组织。基座能力侧重知识建模、数据治理和智能体编排；核心决策侧重资源调度、异常处置和评估优化；闭环验证侧重原型实现、场景验证和指标评测。"),
                ("内容一", "研究任务语义理解与知识表示方法，围绕任务对象、资源对象、时空约束、执行规则和评估指标构建项目知识图谱及其更新机制。"),
                ("内容二", "研究多智能体协同工作流与工具调用框架，解决复杂任务分解、过程控制、结果审查、日志追踪和人工审批衔接等问题。"),
                ("内容三", "研究资源调度与动态重规划方法，重点处理任务增减、资源异常、时效冲突和多目标权衡问题。"),
                ("内容四", "研究原型系统实现与评估方法，建立从需求到验证的完整技术路线和实验体系。"),
            ])
        if t == "关键技术":
            blocks = [
                "[[H2]]1. 关键技术总体说明",
                self._long_para("本项目聚焦五项关键技术：任务语义理解与知识建模、公开研究辅助与检索增强、多智能体编排与协同治理、资源调度与低扰动重规划、可观测性与安全审计。这些技术既相互独立又彼此耦合，共同决定系统的有效性和可落地性。"),
                "[[H2]]2. 任务语义理解与知识建模",
                self._long_para("首先，系统需将自然语言任务通知解析为结构化对象，包括任务类型、服务对象、时间窗口、地点、需求量、优先级和约束条件等。其次，结合知识图谱将资源台账、流程模板、规则库和历史案例统一组织，提供可检索、可推理、可解释的业务知识底座。"),
                "[[H2]]3. 多智能体协同与治理",
                self._long_para("本项目采用Planner—Researcher—Writer—Critic—Gatekeeper的多智能体协作模式。Planner负责分解任务和组织工作流，Researcher负责公开资料检索与综合，Writer负责方案撰写与内容生成，Critic负责一致性和质量审查，Gatekeeper负责敏感性、权限与人工审批。该模式能够兼顾自动化效率与治理可控性。"),
                "[[H2]]4. 动态资源调度与低扰动重规划",
                self._long_para("在执行侧，系统需同时考虑任务优先级、资源类型匹配、时空约束、成本与稳定性等指标。针对突发需求或资源异常，系统采用低扰动重规划策略，优先在局部调整和最小变更成本下恢复可执行方案，从而减少对既有执行计划的冲击。"),
                "[[H2]]5. 关键技术关系图",
                f"[[FIGURE]]{(self.figure_dir/'图1_后勤保障智能体逻辑结构图.png').as_posix()}|图1 后勤保障智能体逻辑结构图|15",
                "[[TABLE]]| 关键技术 | 主要功能 | 关键输入 | 关键输出 | 评价指标 |\n|---|---|---|---|---|\n| 语义理解与知识建模 | 解析任务、构建图谱 | 通知、规则、台账 | 结构化任务对象、知识子图 | 抽取准确率、覆盖率 |\n| 多智能体编排 | 组织角色协作与门禁 | 任务包、流程模板 | 过程状态、阶段结果 | 成功率、人工介入比 |\n| 动态调度与重规划 | 资源匹配与异常处置 | 任务需求、资源状态 | 执行计划、替代方案 | 方案质量、重规划时延 |\n| 可观测与安全审计 | 留痕、审查与复盘 | Prompt、Trace、日志 | 审计包、风险提示 | 完整性、可追溯性 |",
            ]
            blocks.extend(self._reference_annotation([1,2,3,4,5,6,8,9,10,11,12,13,14,15]))
            return blocks
        if t == "技术路线":
            blocks = [
                "[[H2]]1. 技术路线总体说明",
                self._long_para("本项目技术路线遵循“需求牵引—知识建模—智能体编排—资源调度—系统验证”的逻辑。从需求出发，首先明确业务流程与约束；随后构建知识底座和数据治理机制；在此基础上设计多智能体协同工作流和调度优化引擎；最终通过原型系统和场景测试验证方法有效性。"),
                f"[[FIGURE]]{(self.figure_dir/'图2_后勤保障智能体技术路线图.png').as_posix()}|图2 后勤保障智能体技术路线图|15",
                "[[H2]]2. 阶段一：需求与知识基座",
                self._long_para("阶段一重点完成需求分析、对象建模、指标体系设计与知识图谱构建。需要沉淀任务词表、资源分类、规则模板、评价指标和历史案例，并形成统一的数据治理机制，为后续模型调用与工作流编排提供高质量上下文。"),
                "[[H2]]3. 阶段二：智能体编排与调度引擎",
                self._long_para("阶段二重点研究多智能体工作流引擎和调度优化引擎。前者解决角色分工、输入输出契约、门禁审批和日志追踪；后者解决任务分配、路径规划、执行协调和异常重规划等问题。两者通过共享状态、工具接口和指标反馈进行闭环耦合。"),
                "[[H2]]4. 阶段三：评估、迭代与示范验证",
                self._long_para("阶段三在典型场景中开展集成测试和示范验证，构建从离线案例回放到在线试运行的分层评估体系。通过多轮实验，持续优化模型提示词、知识库组织、调度规则和人工门禁设置，逐步提升系统的准确性、稳定性和可解释性。"),
            ]
            return blocks
        if t == "总体架构设计":
            blocks = [
                "[[H2]]1. 总体架构",
                self._long_para("系统总体架构分为交互层、编排层、知识层、执行层和治理层。交互层负责接收用户任务、展示过程状态与结果；编排层负责工作流组织、多智能体协同和状态管理；知识层负责知识图谱、检索增强和案例库；执行层负责规划求解、工具调用和监控反馈；治理层负责权限、审计与风险控制。"),
                f"[[FIGURE]]{(self.figure_dir/'图1_后勤保障智能体逻辑结构图.png').as_posix()}|图3 后勤保障智能体总体架构图|15",
                "[[H2]]2. 模块划分",
                self._long_para("交互层包含任务受理、看板展示、人工审批和结果确认等模块；编排层包含任务编排引擎、Prompt执行器、上下文构建器和状态机；知识层包含知识图谱、检索引擎、事实库与模板库；执行层包含计划求解器、重规划器、资源匹配器和评估器；治理层包含安全分类、隐私脱敏、日志追踪、指标审查和版本管理。"),
                "[[TABLE]]| 层次 | 模块 | 主要职责 |\n|---|---|---|\n| 交互层 | 任务受理/审批/看板 | 负责用户交互与人工确认 |\n| 编排层 | 工作流引擎/Prompt执行器 | 负责多智能体流程组织 |\n| 知识层 | 图谱/RAG/案例库 | 负责知识支撑与检索 |\n| 执行层 | 调度求解/重规划/评估 | 负责方案生成与调整 |\n| 治理层 | 安全/日志/审计/配置 | 负责合规与可观测性 |",
            ]
            return blocks
        if t == "智能体协同机制":
            blocks = [
                "[[H2]]1. 角色定义",
                self._long_para("系统将核心角色划分为Planner、Researcher、Writer、Critic、Executor和Gatekeeper。其中Planner负责分解任务、制定执行顺序；Researcher负责公开资料补充；Writer负责结构化内容生成；Critic负责一致性与质量审查；Executor负责具体求解与工具调用；Gatekeeper负责安全分类、人工审批和出口控制。"),
                "[[H2]]2. 协同流程",
                self._long_para("在一次完整运行中，Planner首先根据项目目标和输入材料生成修订计划；随后，Researcher完成脱敏后的公开检索与综合；Writer根据计划和知识上下文逐节生成内容；Critic对每节以及全局进行审查；最后由Gatekeeper完成保密与导出审批。整个过程要求所有输入、输出、Trace和Gate决策均可追踪。"),
                f"[[FIGURE]]{(self.figure_dir/'图3_后勤保障智能体关键执行流流程图.png').as_posix()}|图4 后勤保障智能体关键执行流流程图|16",
                self._long_para("这种协同机制的优势在于通过明确的角色职责和契约化输入输出，减少单模型端到端生成的不确定性；通过Critic和Gatekeeper的双重审查机制，提升结果可靠性；通过日志与Trace留存，为后续调优和问责提供依据。"),
            ]
            return blocks
        if t == "数据与知识工程":
            return self._generic_blocks(t, [
                ("数据源设计", "数据来源包括任务通知、资源台账、执行日志、规则文档、外部公开资料和历史案例。为提高可用性，项目对各类数据进行统一标准化和元数据治理，形成面向智能体运行的上下文视图。"),
                ("知识图谱构建", "知识图谱围绕任务、物资、车辆、人员、地点、时间窗、规则、指标和案例九类核心对象展开，定义对象属性和关联关系，支持查询、校验和推理。图谱将与事实库、模板库和向量检索库配合工作，以兼顾结构化约束和语义检索能力。"),
                ("RAG与记忆机制", "RAG机制用于为Researcher、Writer和Critic提供可控外部知识，避免无依据生成；记忆机制则用于沉淀高质量历史方案、异常处理经验和人工修订模式，形成可复用的经验资产。"),
                ("治理机制", "数据与知识工程还需支持版本管理、质量评估、冲突检测与增量更新，确保系统在长期迭代中保持一致性和可追溯性。"),
            ])
        if t == "评估与验证方案":
            blocks = [
                "[[H2]]1. 评估目标与原则",
                self._long_para("评估工作围绕有效性、效率、稳定性、可解释性和安全性五个维度展开，遵循“离线验证—联调测试—场景试运行”的渐进式原则。离线阶段重点验证知识抽取、任务解析和方案生成质量；联调阶段验证多智能体协同和调度闭环；试运行阶段验证在真实业务节奏下的稳定性和人工接纳度。"),
                "[[H2]]2. 评估指标体系",
                "[[TABLE]]| 维度 | 指标 | 说明 | 目标值 |\n|---|---|---|---|\n| 有效性 | 方案可执行率 | 输出方案满足约束并可执行 | ≥90% |\n| 效率 | 方案生成时间 | 从任务输入到初稿输出的耗时 | ≤10分钟 |\n| 稳定性 | 重规划成功率 | 异常情况下生成可替代方案的比例 | ≥85% |\n| 可解释性 | Trace覆盖率 | 关键结论关联来源的比例 | ≥95% |\n| 安全性 | 审计留痕完整率 | Prompt/输出/审批日志的留存完整率 | 100% |",
                "[[H2]]3. 场景设计",
                self._long_para("验证场景包括常态保障、临时加急、资源受限和异常处置四类。常态保障侧重标准流程效率；临时加急侧重任务插入与优先级调整；资源受限侧重多目标权衡；异常处置侧重低扰动重规划与人工干预机制。"),
                self._long_para("评估过程中，将同时记录方案质量指标和过程指标。前者包括满足率、成本、时效和稳定性；后者包括Prompt调用次数、人工介入节点、平均等待时间、审查通过率和Trace覆盖率。"),
            ]
            return blocks
        if t == "创新点":
            return self._generic_blocks(t, [
                ("方法创新", "项目提出将任务语义理解、知识图谱、RAG和多智能体编排融合到统一的后勤保障智能体框架中，突破传统单点算法难以覆盖全流程的问题。"),
                ("架构创新", "通过Planner—Researcher—Writer—Critic—Gatekeeper协作模式，形成契约化、可追踪、可审查的智能体流程架构，实现从生成到治理的一体化闭环。"),
                ("工程创新", "项目在动态调度与低扰动重规划、日志留痕、人工门禁和评估看板等方面实现系统级协同设计，强调可落地性和持续演进能力。"),
            ])
        if t == "预期成果":
            return self._generic_blocks(t, [
                ("系统成果", "完成后勤保障智能体原型系统1套，覆盖任务受理、方案生成、执行监控、异常处置和效果评估等功能。"),
                ("方法成果", "形成知识建模、智能体编排、资源调度与重规划等方法成果，沉淀算法设计文档、Prompt工程规范和评估规则。"),
                ("数据与文档成果", "形成样例数据集、测试案例库、知识模板库、系统手册、用户操作手册和运维手册等配套文档。"),
                ("应用成果", "在典型场景中完成验证并形成示范应用报告，为后续推广提供依据。"),
            ])
        if t == "进度计划":
            blocks = [
                "[[TABLE]]| 阶段 | 时间 | 主要任务 | 阶段成果 |\n|---|---|---|---|\n| 第一阶段 | 第1-6个月 | 需求分析、指标体系、知识建模与样例库建设 | 需求说明书、知识模型、初始数据集 |\n| 第二阶段 | 第7-14个月 | 智能体工作流引擎与RAG组件开发 | 编排引擎原型、检索组件、日志框架 |\n| 第三阶段 | 第15-24个月 | 资源调度、重规划与评估模块开发 | 调度算法、评估指标库、联调版本 |\n| 第四阶段 | 第25-30个月 | 场景测试、性能优化与治理完善 | 场景测试报告、优化方案 |\n| 第五阶段 | 第31-36个月 | 试运行、总结验收与成果凝练 | 原型系统、总结报告、成果文档 |",
                self._long_para("项目实施过程中将设置阶段性里程碑，并对需求、开发、联调、验证和验收活动分别制定完成判据。每个阶段均安排评审节点和风险复盘，以保证项目按计划推进并及时纠偏。"),
            ]
            return blocks
        if t == "预算与经费":
            blocks = [
                "[[TABLE]]| 经费科目 | 金额（万元） | 说明 |\n|---|---|---|\n| 设备与软件 | 18 | 服务器、开发工具和测试环境 |\n| 数据与材料 | 6 | 样例数据构建、资料购买与标注 |\n| 研发劳务 | 22 | 算法开发、工程实现与测试 |\n| 试验与差旅 | 8 | 现场调研、场景测试与交流 |\n| 专家咨询与出版 | 4 | 评审、咨询、成果整理 |\n| 预备费 | 2 | 不可预见支出 |",
                self._long_para("预算编制遵循目标导向、重点突出、结构合理和绩效可衡量的原则。设备与软件经费主要用于原型验证环境建设；数据与材料经费主要用于样例库构建和资料整理；研发劳务经费保障算法和工程开发任务；试验与差旅经费保障场景测试与调研；咨询与出版经费支撑评审和成果整理。"),
                self._long_para("项目将建立经费执行台账，按阶段开展预算执行情况评估，确保经费使用与研发任务同步推进，避免投入与产出脱节。"),
            ]
            return blocks
        if t == "研究基础与保障条件":
            return self._generic_blocks(t, [
                ("研究基础", "项目团队在智能体系统、工作流编排、知识图谱、RAG、优化调度和企业级应用开发等方面具有较好的研究与工程基础，具备将方法研究转化为原型系统的能力。"),
                ("团队能力", "团队成员覆盖算法研究、后端工程、前端交互、测试验证和项目管理等角色，能够支撑从需求分析到系统交付的完整研发链路。"),
                ("已有平台", "团队已具备基础开发环境、测试环境和样例数据资源，并积累了工作流引擎、检索组件、日志监控和权限控制等可复用模块，为项目开展提供了良好条件。"),
                ("组织保障", "项目将采用周例会、阶段评审、问题台账、版本管理和风险复盘等方式组织实施，确保研发过程透明、可控、可追踪。"),
            ])
        if t == "风险分析与对策":
            return self._generic_blocks(t, [
                ("技术风险", "技术风险主要包括大模型输出稳定性不足、知识库更新滞后和多智能体协同开销偏高等问题。应对策略是引入结构化约束、Critic审查、缓存机制和回退策略。"),
                ("数据风险", "数据风险主要包括数据质量不一致、样例覆盖不足和外部公开资料质量参差不齐。应对策略是建立数据分级、清洗校验、来源可信度标注和人工复核机制。"),
                ("工程风险", "工程风险主要包括模块耦合度高、接口变化频繁和部署环境不一致。应对策略是采用契约化Schema、分层架构、自动化测试和灰度验证。"),
                ("管理风险", "管理风险主要包括需求变更频繁、跨角色协同效率低和阶段目标偏移。应对策略是通过里程碑评审、问题清单和复盘机制进行治理。"),
            ])
        if t == "伦理、安全与边界":
            return self._generic_blocks(t, [
                ("伦理边界", "系统定位为辅助决策与流程自动化工具，不直接替代最终责任主体。涉及资源调配、异常处置和关键外发等环节必须保留人工确认机制。"),
                ("数据安全", "系统实行分级分类处理，对外公开检索前必须进行任务包脱敏；Prompt、响应、Trace和日志按照统一安全标签管理，并在安全域内留存。"),
                ("人工审批", "在公开检索、关键内容确认、终审与导出等步骤设置人工门禁，确保高风险动作均具备明确责任归属和可复盘记录。"),
                ("系统边界", "项目重点研究智能体辅助决策、流程编排与调度闭环，不覆盖底层数据采集终端和外部业务系统深度改造。"),
            ])
        if t == "参考文献":
            refs = [
                "[1] Yao S, Zhao J, Yu D, et al. ReAct: Synergizing Reasoning and Acting in Language Models. ICLR, 2023.",
                "[2] Schick T, Dwivedi-Yu J, Dessi R, et al. Toolformer: Language Models Can Teach Themselves to Use Tools. NeurIPS, 2023.",
                "[3] Wu Q, Bansal G, Zhang J, et al. AutoGen: Enabling Next-Gen LLM Applications via Multi-Agent Conversation. Microsoft Research, 2023.",
                "[4] Li G, Hammoud H A A K, Itani H, et al. CAMEL: Communicative Agents for Mind Exploration of Large Scale Language Model Society. NeurIPS Workshop, 2023.",
                "[5] Wang G, Xie C, Li Z, et al. Voyager: An Open-Ended Embodied Agent with Large Language Models. arXiv preprint arXiv:2305.16291, 2023.",
                "[6] Xi Z, Chen W, Guo X, et al. The Rise and Potential of Large Language Model Based Agents: A Survey. arXiv preprint arXiv:2309.07864, 2024.",
                "[7] Lewis P, Perez E, Piktus A, et al. Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks. NeurIPS, 2020.",
                "[8] Singh R, Bansal R. Knowledge Graphs for Logistics and Supply Chain Management: A Survey. Computers & Industrial Engineering, 2023.",
                "[9] Min H. Digital Twins for Smart Logistics and Supply Chain Management. IEEE Access, 2022.",
                "[10] Pillac V, Gendreau M, Guéret C, et al. A Review of Dynamic Vehicle Routing Problems. European Journal of Operational Research, 2021.",
                "[11] Dellermann D, et al. Human-AI Collaboration in Decision Making. ACM Computing Surveys, 2023.",
                "[12] Shankar V, et al. Observability for LLM Applications. Technical Whitepaper, 2024.",
                "[13] Supply Chain Control Tower and AI Orchestration. Gartner/Industry Whitepaper, 2024.",
                "[14] Chen J, et al. Enterprise Workflow Automation with LLM Agents. arXiv preprint, 2024.",
                "[15] Wang X, et al. Benchmarking and Evaluating LLM Agents. arXiv preprint, 2024.",
                "[16] Zhang Y, et al. GraphRAG: Enhancing Retrieval-Augmented Generation with Knowledge Graphs. arXiv preprint, 2024.",
                "[17] Sun H, et al. Multi-Agent Planning and Coordination for Complex Tasks. Information Sciences, 2023.",
                "[18] Li X, et al. A Survey of AI for Operations and Supply Chain Management. International Journal of Production Research, 2024.",
                "[19] Sarker I H. Workflow Automation and AI Orchestration: Concepts and Applications. Future Generation Computer Systems, 2024.",
                "[20] Liu K, et al. RAG Systems in Enterprise Applications: A Survey. arXiv preprint, 2024.",
            ]
            blocks = ["[[H2]]参考文献"]
            blocks.extend(refs)
            return blocks
        return self._generic_blocks(t, [("本节说明", f"《{t}》章节围绕项目目标开展论证。"), ("主要内容", "本节结合项目场景、技术路线与实施计划展开详细说明。")])

    def _generic_blocks(self, title: str, parts: list[tuple[str, str]]) -> list[str]:
        blocks: list[str] = []
        for idx, (subtitle, body) in enumerate(parts, 1):
            blocks.append(f"[[H2]]{idx}. {subtitle}")
            blocks.append(self._long_para(body))
            blocks.append(self._long_para(body + " 进一步看，该问题不仅涉及模型能力本身，还涉及流程设计、数据组织、角色协作和评价反馈等工程化因素。因此，本项目将通过结构化建模、分层工作流和迭代验证机制，把上述要求转化为可实施、可检验、可复盘的研发任务。"))
        return blocks

    @staticmethod
    def _reference_annotation(indices: list[int]) -> list[str]:
        return [f"上述分析分别参考文献[{','.join(str(i) for i in indices[:len(indices)//2])}]以及[{','.join(str(i) for i in indices[len(indices)//2:])}]。"]

    @staticmethod
    def _long_para(text: str) -> str:
        extra = (
            "从系统工程角度看，单点能力并不能直接转化为稳定可用的业务价值，必须通过输入治理、角色协同、过程控制、结果审查和闭环评估形成整体能力。"
            "因此，本项目将坚持“结构化约束+知识增强+多智能体协同+人工门禁”的技术原则，避免将复杂业务问题简化为单轮问答或单模型端到端生成。"
            "与此同时，项目还将把性能指标、稳定性指标、可解释性指标和安全指标统一纳入评估框架，确保系统在效率提升的同时满足治理与审计要求。"
            "进一步地，项目将通过原型迭代把抽象方法落到可执行的模块、接口与测试用例之上，使研究内容既能够支撑学术论证，也能够支撑工程实施。"
            "在每个关键节点上，系统都会记录输入、输出、版本、责任角色与审查结论，并通过统一的Trace机制与评价指标进行关联，确保后续可以对生成过程进行审计、复盘和优化。"
            "这种将业务逻辑、知识支撑、工作流控制与评价反馈统一设计的方法，有助于提升后勤保障智能体在复杂场景中的稳定性、可迁移性和持续演进能力。"
        )
        return text + extra
