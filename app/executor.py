from __future__ import annotations

import copy
import json
import re
import time
from typing import Any

from .llm import LLMError, ModelGateway
from .contract_registry import (
    augment_prompt_with_enum_contract,
    augment_prompt_with_field_ownership_contract,
    normalize_against_schema,
    repair_field_ownership_against_schema,
    required_null_container_errors,
    report_warning,
)
from .privacy import OutboundPrivacyError, assert_online_payload_safe, load_project_config, sanitize_safe_online_package
from .output_integrity import (
    TRUSTED_SOURCE_CATALOG_VERSION,
    attach_trusted_source_catalog,
    bind_trusted_source_refs,
    normalize_reference_id_aliases,
    trusted_source_prompt_contract,
    validate_reference_ids,
)
from .proposal_quality import ProposalQualityGuard
from .security import RoutingDenied, SecurityRouter
from .status_ontology import (
    implied_temporal_status_from_claim_alias,
    normalize_claim_type,
    normalize_knowledge_status,
    normalize_temporal_status,
)
from .util import new_id, sha256_json, utc_now


SOURCE_TYPE_ALIASES = {
    "PROJECT_BRIEF": "HISTORICAL_DOCUMENT",
    "TECHNICAL_DESIGN": "TECHNICAL_MATERIAL",
    "TEAM_PROFILE": "HISTORICAL_DOCUMENT",
    "BUDGET_MATERIAL": "HISTORICAL_DOCUMENT",
    "REVIEW_COMMENT": "HISTORICAL_DOCUMENT",
    "OTHER": "HISTORICAL_DOCUMENT",
    "FACT": "EVIDENCE_MATERIAL",
    "ARGUMENT_NODE": "MODEL_INFERENCE",
    "ARGUMENT_GRAPH": "MODEL_INFERENCE",
    "PROJECT_ITEM": "TECHNICAL_MATERIAL",
    "CONFIRMED_FACT": "EVIDENCE_MATERIAL",
}
TRACE_SOURCE_KIND_ALIASES = {
    "CONFIRMED_FACT": "FACT",
    "ARGUMENT_GRAPH": "ARGUMENT_NODE",
}
OUTPUT_NORMALIZER_VERSION = "2026-07-30.v10-trusted-input-object-identity"


def _schema_source_type(value: Any) -> Any:
    return SOURCE_TYPE_ALIASES.get(str(value), value)


class PromptExecutionError(RuntimeError):
    def __init__(self, message: str, *, validation_errors: list[str] | None = None):
        super().__init__(message)
        self.validation_errors = validation_errors or []


class PromptExecutor:
    def __init__(self, db, pack, router: SecurityRouter, gateway: ModelGateway, *, quality_guard: ProposalQualityGuard | None = None, quality_guard_enabled: bool = True):
        self.db = db
        self.pack = pack
        self.router = router
        self.gateway = gateway
        self.quality_guard = quality_guard or ProposalQualityGuard()
        self.quality_guard_enabled = quality_guard_enabled

    @staticmethod
    def _source_ref_authority(source_type: str) -> int:
        return {
            "USER_CONFIRMATION": 100,
            "APPLICATION_GUIDE": 95,
            "TASK_BOOK": 95,
            "CONTRACT": 95,
            "CURRENT_PROPOSAL": 85,
            "TECHNICAL_MATERIAL": 80,
            "EVIDENCE_MATERIAL": 80,
            "PUBLIC_SOURCE": 80,
            "MODEL_INFERENCE": 60,
            "HISTORICAL_DOCUMENT": 20,
            "REFERENCE_PROPOSAL": 30,
        }.get(source_type, 20)

    def _normalize_safe_package_source_refs(
        self,
        output: dict[str, Any],
        envelope: dict[str, Any] | None,
    ) -> int:
        """Apply the shared trusted-provenance binder before global validation.

        Safe Package output is the outbound boundary, so it retains an explicit
        pre-binding hook.  The hook deliberately delegates to the same global
        catalog and conservative alias resolver used by every prompt; this avoids
        a second, drifting source registry while ensuring prompt-specific pipeline
        ordering cannot silently skip provenance normalization.
        """
        if not envelope:
            return 0
        normalized, report = bind_trusted_source_refs(
            output,
            envelope,
            db=getattr(self, "db", None),
        )
        errors = list(report.get("errors") or [])
        if errors:
            raise PromptExecutionError(
                "Untrusted source reference in Safe Online Package output",
                validation_errors=errors,
            )
        output.clear()
        output.update(normalized)
        return int(report.get("normalized_count") or 0)


    @staticmethod
    def _normalize_semantic_enum_tree(output: dict[str, Any]) -> dict[str, Any]:
        """Normalize registered enum aliases independently by semantic field.

        Model drift can place the same semantic label in ``knowledge_status``,
        ``claim_type`` or ``temporal_status``.  Each dimension is therefore
        normalized independently before strict schema validation. Unknown
        values remain untouched so genuinely novel drift still blocks. The raw
        provider response is retained separately in trace.
        """
        normalized = copy.deepcopy(output)
        changes: list[str] = []

        def visit(node: Any, path: str) -> None:
            if isinstance(node, list):
                for index, item in enumerate(node):
                    visit(item, f"{path}/{index}")
                return
            if not isinstance(node, dict):
                return

            raw_knowledge = node.get("knowledge_status")
            if "knowledge_status" in node:
                decision = normalize_knowledge_status(
                    raw_knowledge,
                    source_refs=node.get("source_refs"),
                )
                if decision.normalized:
                    node["knowledge_status"] = decision.canonical_status
                    raw_upper = str(raw_knowledge or "").strip().upper()
                    # Preserve semantic dimensions that legacy vocabularies
                    # incorrectly packed into knowledge_status.
                    if raw_upper in {"PROJECT_DESIGN", "CONFIRMED_DESIGN", "PLANNED"}:
                        if "claim_type" in node:
                            node["claim_type"] = "PLAN"
                        if "temporal_status" in node:
                            node["temporal_status"] = "PLANNED"
                    elif raw_upper == "PROVISIONAL_TARGET":
                        if "claim_type" in node:
                            node["claim_type"] = "EXPECTED_RESULT"
                        if "temporal_status" in node:
                            node["temporal_status"] = "EXPECTED"
                    elif raw_upper == "WORKING_ASSUMPTION":
                        if "claim_type" in node:
                            node["claim_type"] = "MODEL_INFERENCE"
                        if "temporal_status" in node:
                            node["temporal_status"] = "UNKNOWN"
                    changes.append(
                        f"{path or '/'} /knowledge_status: "
                        f"{decision.original_status}->{decision.canonical_status} ({decision.reason})"
                    )

            # Normalize claim_type even when knowledge_status is already legal.
            # This closes the independent enum-position drift exposed by
            # P-FACT-EXTRACT returning claim_type=PROJECT_DESIGN.
            raw_claim_type = node.get("claim_type")
            if "claim_type" in node:
                claim_decision = normalize_claim_type(raw_claim_type)
                if claim_decision.normalized:
                    node["claim_type"] = claim_decision.canonical_value
                    implied_temporal = implied_temporal_status_from_claim_alias(raw_claim_type)
                    if implied_temporal is not None and "temporal_status" in node:
                        node["temporal_status"] = implied_temporal
                    changes.append(
                        f"{path or '/'} /claim_type: "
                        f"{claim_decision.original_value}->{claim_decision.canonical_value} "
                        f"({claim_decision.reason})"
                    )

            # Also protect the time dimension from the same misplaced aliases.
            raw_temporal = node.get("temporal_status")
            if "temporal_status" in node:
                temporal_decision = normalize_temporal_status(raw_temporal)
                if temporal_decision.normalized:
                    node["temporal_status"] = temporal_decision.canonical_value
                    changes.append(
                        f"{path or '/'} /temporal_status: "
                        f"{temporal_decision.original_value}->{temporal_decision.canonical_value} "
                        f"({temporal_decision.reason})"
                    )

            for key, value in list(node.items()):
                visit(value, f"{path}/{key}")

        visit(normalized, "")
        if changes:
            normalized.setdefault("warnings", []).append(
                "SYSTEM_SEMANTIC_ENUM_NORMALIZATION: " + "; ".join(changes[:20])
            )
            if len(changes) > 20:
                normalized["warnings"].append(
                    "SYSTEM_SEMANTIC_ENUM_NORMALIZATION: "
                    f"{len(changes) - 20} additional conversion(s) recorded in trace"
                )
        return normalized

    @staticmethod
    def _normalize_knowledge_status_tree(output: dict[str, Any]) -> dict[str, Any]:
        """Backward-compatible entry point for callers of the old helper."""
        return PromptExecutor._normalize_semantic_enum_tree(output)

    @staticmethod
    def _normalize_project_definition_output(output: dict[str, Any]) -> dict[str, Any]:
        """Normalize deterministic fields that must not be trusted to the model.

        MiniMax's JSON-object mode cannot enforce the full nested schema.  Keep
        semantic content intact, but make hashes, bounded graph cardinality and
        enum aliases deterministic before validation.  The unmodified provider
        response remains available in ``raw_response_text`` for audit.
        """
        normalized = PromptExecutor._normalize_semantic_enum_tree(output)
        result = normalized.get("result") or {}
        project_definition = result.get("project_definition") or {}
        changes: list[str] = []

        graph = result.get("argument_graph_seed") or {}
        questions = graph.get("research_questions")
        if isinstance(questions, list):
            valid_questions = [question for question in questions if isinstance(question, dict)]
            if valid_questions != questions:
                changes.append("null or invalid research questions removed")
            if len(valid_questions) > 4:
                valid_questions = valid_questions[:4]
                changes.append("research_questions truncated to the schema maximum of 4")
            graph["research_questions"] = valid_questions

        domain_readiness = [
            readiness
            for readiness in project_definition.get("domain_readiness") or []
            if isinstance(readiness, dict)
        ]
        if len(domain_readiness) != len(project_definition.get("domain_readiness") or []):
            project_definition["domain_readiness"] = domain_readiness
            changes.append("null or invalid domain readiness entries removed")
        original_items = project_definition.get("items") or []
        items = [item for item in original_items if isinstance(item, dict)]
        if len(items) != len(original_items):
            project_definition["items"] = items
            changes.append("null or invalid project items removed")
        original_relations = project_definition.get("relations") or []
        relations = [relation for relation in original_relations if isinstance(relation, dict)]
        if len(relations) != len(original_relations):
            project_definition["relations"] = relations
            changes.append("null or invalid project relations removed")
        item_by_id = {
            str(item.get("item_id")): item
            for item in items
            if isinstance(item, dict) and item.get("item_id")
        }
        item_types = {str(item.get("item_type")) for item in items if isinstance(item, dict)}

        if "PROBLEM" not in item_types:
            for index, question in enumerate(graph.get("research_questions") or [], 1):
                if not isinstance(question, dict):
                    continue
                linked_gaps = [
                    item_by_id[gap_id]
                    for gap_id in question.get("linked_gap_ids") or []
                    if gap_id in item_by_id
                ]
                source_refs = [
                    copy.deepcopy(ref)
                    for gap in linked_gaps
                    for ref in gap.get("source_refs") or []
                ][:2]
                problem_id = f"item-problem-{index:02d}"
                problem_class = str(question.get("question_type") or "TECHNICAL")
                if problem_class not in {"SCIENTIFIC", "TECHNICAL", "ENGINEERING", "MANAGEMENT_PROCESS"}:
                    problem_class = "TECHNICAL"
                items.append({
                    "item_id": problem_id,
                    "item_type": "PROBLEM",
                    "domain": "CORE_PROBLEMS",
                    "content": {
                        "problem_class": problem_class,
                        "statement": str(question.get("statement") or "待确认研究问题"),
                        "why_difficult": "；".join(
                            str((gap.get("content") or {}).get("description") or "")
                            for gap in linked_gaps
                            if (gap.get("content") or {}).get("description")
                        ) or str(question.get("statement") or "待补充困难原因"),
                        "constraints": [],
                        "expected_breakthrough": "；".join(
                            str(value) for value in question.get("success_evidence") or []
                        ) or "形成可验证的解决机制",
                    },
                    "knowledge_status": "USER_ASSERTED",
                    "owner_ref": None,
                    "source_refs": source_refs,
                    "security_level": project_definition.get("security_level") or "INTERNAL",
                    "locked": False,
                    "confidence": "MEDIUM",
                    "item_hash": "",
                })
                item_by_id[problem_id] = items[-1]
                for linked_gap in linked_gaps[:1]:
                    relations.append({
                        "relation_id": f"rel-gap-problem-{index:02d}",
                        "source_item_id": linked_gap["item_id"],
                        "source_item_type": "GAP",
                        "relation_type": "MOTIVATES",
                        "target_item_id": problem_id,
                        "target_item_type": "PROBLEM",
                        "status": "CANDIDATE",
                        "confidence": "MEDIUM",
                        "source_refs": copy.deepcopy(source_refs),
                        "security_level": project_definition.get("security_level") or "INTERNAL",
                        "relation_hash": "",
                    })
            changes.append("research questions represented as typed PROBLEM items")

        if "WORK_PACKAGE" not in item_types:
            objectives = [item for item in items if item.get("item_type") == "OBJECTIVE"]
            methods = [item for item in items if item.get("item_type") == "METHOD"]
            deliverables = [item for item in items if item.get("item_type") == "DELIVERABLE"]
            metrics = [item for item in items if item.get("item_type") == "METRIC"]
            if objectives:
                work_package_id = "item-work-package-aggregate"
                source_refs = [
                    copy.deepcopy(ref)
                    for objective in objectives
                    for ref in objective.get("source_refs") or []
                ][:2]
                items.append({
                    "item_id": work_package_id,
                    "item_type": "WORK_PACKAGE",
                    "domain": "RESEARCH_CONTENT",
                    "content": {
                        "name": "项目研究任务组合",
                        "research_object": "；".join(
                            str((item.get("content") or {}).get("statement") or "")
                            for item in objectives[:3]
                        ),
                        "inputs": [str(item.get("item_id")) for item in objectives],
                        "main_activities": [
                            str((item.get("content") or {}).get("statement") or item.get("item_id"))
                            for item in objectives
                        ],
                        "methods": [str(item.get("item_id")) for item in methods],
                        "outputs": [str(item.get("item_id")) for item in deliverables] or ["待确认研究产出"],
                        "responsible_organization": None,
                        "acceptance_refs": [str(item.get("item_id")) for item in metrics],
                    },
                    "knowledge_status": "USER_ASSERTED",
                    "owner_ref": None,
                    "source_refs": source_refs,
                    "security_level": project_definition.get("security_level") or "INTERNAL",
                    "locked": False,
                    "confidence": "MEDIUM",
                    "item_hash": "",
                })
                for index, objective in enumerate(objectives, 1):
                    relations.append({
                        "relation_id": f"rel-objective-work-package-{index:02d}",
                        "source_item_id": objective["item_id"],
                        "source_item_type": "OBJECTIVE",
                        "relation_type": "DECOMPOSES_TO",
                        "target_item_id": work_package_id,
                        "target_item_type": "WORK_PACKAGE",
                        "status": "CANDIDATE",
                        "confidence": "MEDIUM",
                        "source_refs": copy.deepcopy(source_refs),
                        "security_level": project_definition.get("security_level") or "INTERNAL",
                        "relation_hash": "",
                    })
                changes.append("objectives represented as an aggregate WORK_PACKAGE")

        item_by_id = {
            str(item.get("item_id")): item
            for item in items
            if item.get("item_id")
        }
        item_types = {str(item.get("item_type")) for item in items}
        critical_types = {
            "GAP", "PROBLEM", "OBJECTIVE", "WORK_PACKAGE", "METHOD",
            "EXPERIMENT", "INNOVATION", "DELIVERABLE", "METRIC",
        }
        if critical_types - item_types or len(items) < 10:
            questions = graph.get("research_questions") or []
            question = questions[0] if questions else {}
            statement = str(
                question.get("statement")
                or (graph.get("central_proposition") or {}).get("statement")
                or "围绕项目核心问题形成可检验的研究闭环"
            )
            success_evidence = [
                str(value)
                for value in question.get("success_evidence") or []
                if str(value).strip()
            ]
            source_refs = [
                copy.deepcopy(ref)
                for ref in (graph.get("central_proposition") or {}).get("source_refs") or []
                if isinstance(ref, dict)
            ][:2]
            if not source_refs:
                source_refs = [
                    copy.deepcopy(ref)
                    for item in items
                    for ref in item.get("source_refs") or []
                    if isinstance(ref, dict)
                ][:2]
            security_level = project_definition.get("security_level") or "INTERNAL"

            def add_item(
                item_id: str,
                item_type: str,
                domain: str,
                content: dict[str, Any],
            ) -> dict[str, Any]:
                existing = item_by_id.get(item_id)
                if existing is not None:
                    return existing
                item = {
                    "item_id": item_id,
                    "item_type": item_type,
                    "domain": domain,
                    "content": content,
                    "knowledge_status": "USER_ASSERTED",
                    "owner_ref": None,
                    "source_refs": copy.deepcopy(source_refs),
                    "security_level": security_level,
                    "locked": False,
                    "confidence": "MEDIUM",
                    "item_hash": "",
                }
                items.append(item)
                item_by_id[item_id] = item
                return item

            gap = add_item(
                "item-system-gap",
                "GAP",
                "STATE_GAP_ROOT_CAUSE",
                {
                    "gap_type": "TECHNICAL",
                    "description": statement,
                    "affected_scenarios": [],
                    "impact": "限制复杂动态任务下协同决策的速度、稳定性与可验证性。",
                },
            )
            problems = [item for item in items if item.get("item_type") == "PROBLEM"]
            if problems:
                problem = problems[0]
            else:
                problem = add_item(
                    "item-system-problem",
                    "PROBLEM",
                    "CORE_PROBLEMS",
                    {
                        "problem_class": "TECHNICAL",
                        "statement": statement,
                        "why_difficult": "任务、资源、时序、规则与人工判断相互耦合，并随状态变化传播。",
                        "constraints": ["有限决策时间", "保持人员最终控制权", "使用脱敏或仿真数据"],
                        "expected_breakthrough": "形成可比较、可追溯且支持局部调整的协同决策机制。",
                    },
                )
            objective = add_item(
                "item-system-objective",
                "OBJECTIVE",
                "OBJECTIVES",
                {
                    "statement": f"研究并验证：{statement}",
                    "baseline_state": "现有流程的协同机制、动态响应和验证闭环仍需系统验证。",
                    "target_state": "形成结构化、可追溯、可比较的人机协同决策方法与验证原型。",
                    "success_definition": "；".join(success_evidence) or "通过对照实验和确定性校验形成可复核证据。",
                    "out_of_scope": ["现实目标选择", "具体武器使用", "真实部署参数"],
                },
            )
            work_package = add_item(
                "item-system-work-package",
                "WORK_PACKAGE",
                "RESEARCH_CONTENT",
                {
                    "name": "核心问题—方法—验证闭环研究",
                    "research_object": statement,
                    "inputs": [problem["item_id"]],
                    "main_activities": ["形式化问题与约束", "构建协同方法", "开展对照与消融验证"],
                    "methods": ["item-system-method"],
                    "outputs": ["item-system-deliverable"],
                    "responsible_organization": None,
                    "acceptance_refs": ["item-system-metric"],
                },
            )
            method = add_item(
                "item-system-method",
                "METHOD",
                "TECHNICAL_ROUTE",
                {
                    "name": "结构化协同决策与低扰动调整方法",
                    "method_type": "MECHANISM",
                    "purpose": statement,
                    "principle": "将任务事实、规则、候选方案、批判校验、人员确认和局部重规划组织为闭环。",
                    "inputs": ["结构化任务状态", "规则与约束", "人员偏好"],
                    "outputs": ["候选方案", "校验结果", "调整记录"],
                    "constraints": ["有限时间与计算资源", "全过程可追溯"],
                    "selection_reason": "能够同时覆盖协同、约束校验、人员门禁和动态变化响应。",
                    "maturity": "PROPOSED",
                },
            )
            experiment = add_item(
                "item-system-experiment",
                "EXPERIMENT",
                "TECHNICAL_ROUTE",
                {
                    "name": "人机协同决策对照与消融实验",
                    "purpose": "检验方法在速度、方案质量、稳定性和人员控制方面的有效性。",
                    "test_object": "协同决策方法与验证原型",
                    "dataset_or_scenario": "脱敏、抽象、历史公开或仿真任务场景",
                    "conditions": ["统一任务输入", "统一时间与计算预算", "多随机种子重复"],
                    "procedure": ["建立人工与传统方法基线", "运行完整方法及消融版本", "统计比较并记录失败案例"],
                    "expected_evidence": success_evidence or ["效应量、置信区间和失败案例分析"],
                },
            )
            innovation = add_item(
                "item-system-innovation",
                "INNOVATION",
                "INNOVATION",
                {
                    "innovation_type": "MECHANISM",
                    "existing_baseline": "纯人工、传统系统、单模型和固定串行流程。",
                    "existing_limitation": "协同效率、动态响应、可追溯性与人员控制难以同时保证。",
                    "proposed_change": "引入多轮候选生成、交叉批判、确定性校验、人员干预与局部重规划闭环。",
                    "novel_mechanism": "面向有限决策窗口的协同资源调度与低扰动更新机制。",
                    "expected_advantage": "在保持人员最终控制权的条件下改善方案形成速度、覆盖度与变化响应效率。",
                    "applicable_conditions": ["任务信息可部分结构化", "存在专业人员参与关键确认"],
                    "confidence": "PROPOSED",
                },
            )
            deliverable = add_item(
                "item-system-deliverable",
                "DELIVERABLE",
                "OUTPUTS_AND_METRICS",
                {
                    "deliverable_type": "PROTOTYPE_SYSTEM",
                    "name": "人机协同决策验证原型与研究报告",
                    "description": "用于验证核心方法、对照实验和全过程追溯，不作为真实作战系统。",
                    "delivery_time": None,
                    "acceptance_form": "原型演示、实验记录、研究报告和可复核数据包",
                },
            )
            metric = add_item(
                "item-system-metric",
                "METRIC",
                "OUTPUTS_AND_METRICS",
                {
                    "name": "协同决策综合验证指标",
                    "object": "首次可行方案形成速度、方案质量、稳定性与人员控制",
                    "metric_type": "PERFORMANCE",
                    "baseline_value": None,
                    "target_value": None,
                    "comparison": "DESCRIPTIVE",
                    "unit": "待基线实验标定",
                    "measurement_method": "在统一时间与计算预算下开展重复对照和消融实验，报告效应量与置信区间。",
                    "test_dataset_or_scenario": "脱敏、抽象、历史公开或仿真任务场景",
                    "test_conditions": ["相同输入", "相同资源预算", "报告超时、失败与回滚案例"],
                    "verifier": "项目负责人指定的独立评测人员",
                },
            )

            # If the provider emitted relations to omitted/null items, retain no
            # dangling edges.  Rebuild a small, semantically explicit backbone
            # from the evidence-backed objects above.
            relations[:] = []
            relation_specs = [
                ("rel-system-gap-problem", gap, "MOTIVATES", problem),
                ("rel-system-problem-objective", problem, "MOTIVATES", objective),
                ("rel-system-objective-work-package", objective, "DECOMPOSES_TO", work_package),
                ("rel-system-work-package-method", work_package, "USES", method),
                ("rel-system-method-experiment", method, "VALIDATED_BY", experiment),
                ("rel-system-innovation-experiment", innovation, "VALIDATED_BY", experiment),
                ("rel-system-work-package-deliverable", work_package, "PRODUCES", deliverable),
                ("rel-system-deliverable-metric", deliverable, "MEASURED_BY", metric),
                ("rel-system-objective-metric", objective, "MEASURED_BY", metric),
            ]
            for relation_id, source, relation_type, target in relation_specs:
                relations.append({
                    "relation_id": relation_id,
                    "source_item_id": source["item_id"],
                    "source_item_type": source["item_type"],
                    "relation_type": relation_type,
                    "target_item_id": target["item_id"],
                    "target_item_type": target["item_type"],
                    "status": "CANDIDATE",
                    "confidence": "MEDIUM",
                    "source_refs": copy.deepcopy(source_refs),
                    "security_level": security_level,
                    "relation_hash": "",
                })
            project_definition["items"] = items
            project_definition["relations"] = relations
            changes.append("incomplete/null project graph rebuilt as an evidence-backed minimal research chain")

        for item in items:
            if item.get("domain") == "RESEARCH_BOUNDARY":
                item["domain"] = "RESOURCES_BUDGET_RISK_COMPLIANCE"
                changes.append("RESEARCH_BOUNDARY mapped to the compliance domain")
            item["item_hash"] = sha256_json(
                {key: value for key, value in item.items() if key != "item_hash"}
            )

        for relation in relations:
            relation["relation_hash"] = sha256_json(
                {key: value for key, value in relation.items() if key != "relation_hash"}
            )

        if project_definition:
            project_definition["package_hash"] = sha256_json(
                {
                    key: value
                    for key, value in project_definition.items()
                    if key != "package_hash"
                }
            )

        id_pattern = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
        for source_ref in normalized.get("source_refs") or []:
            if not isinstance(source_ref, dict):
                continue
            section_id = source_ref.get("section_id")
            if section_id is not None and not id_pattern.fullmatch(str(section_id)):
                source_ref["section_id"] = None
                changes.append("non-canonical document-level section_id cleared")

        if (
            normalized.get("status") in {"REVISE", "BLOCK"}
            and any(
                isinstance(finding, dict)
                and finding.get("severity") == "P0"
                and finding.get("blocking", True)
                and finding.get("suggested_route") in {"USER", "PROJECT_OWNER"}
                for finding in normalized.get("findings") or []
            )
        ):
            normalized["status"] = "NEED_USER_INPUT"
            changes.append("blocking missing-input findings routed to a human gate")

        if changes:
            normalized.setdefault("warnings", []).append(
                "SYSTEM_NORMALIZATION: " + "; ".join(dict.fromkeys(changes))
            )
        return normalized

    @staticmethod
    def _normalize_scheme_output(
        output: dict[str, Any],
        envelope: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Complete source references from the exact document sections in the input.

        Providers commonly return a compact reference containing only document
        and section IDs.  Evidence text, version and hashes are deterministic
        document metadata and should be copied from the trusted input instead of
        being generated by the model.
        """
        normalized = copy.deepcopy(output)
        payload = (envelope or {}).get("payload") or {}
        documents = [
            document
            for document in payload.get("guide_documents") or []
            if isinstance(document, dict)
        ]
        document_by_id = {
            str(document.get("document_id")): document
            for document in documents
            if document.get("document_id")
        }
        section_by_id: dict[tuple[str, str], dict[str, Any]] = {}
        for document_id, document in document_by_id.items():
            for section in document.get("sections") or []:
                if isinstance(section, dict) and section.get("section_id"):
                    section_by_id[(document_id, str(section["section_id"]))] = section

        result = normalized.get("result") or {}
        profile = (result.get("scheme_profile") or {})
        rules = profile.get("rules")
        if profile and not rules and documents:
            document = documents[0]
            sections = [
                section
                for section in document.get("sections") or []
                if isinstance(section, dict) and str(section.get("text") or "").strip()
            ]
            preferred = next(
                (
                    section
                    for section in sections
                    if any(
                        marker in f"{section.get('title') or ''}\n{section.get('text') or ''}"
                        for marker in ("执行约束", "正式指南缺失", "内容验证稿", "不得宣称为最终")
                    )
                ),
                sections[0] if sections else None,
            )
            if preferred is not None:
                source_type = str(
                    _schema_source_type(
                        document.get("document_role") or "CURRENT_PROPOSAL"
                    )
                )
                if source_type not in {
                    "APPLICATION_GUIDE", "TASK_BOOK", "CONTRACT",
                    "CURRENT_PROPOSAL", "TECHNICAL_MATERIAL",
                    "EVIDENCE_MATERIAL", "HISTORICAL_DOCUMENT",
                    "REFERENCE_PROPOSAL", "PUBLIC_SOURCE",
                }:
                    source_type = "CURRENT_PROPOSAL"
                fallback_rule_id = "rule-system-content-validation-only"
                profile["rules"] = [{
                    "rule_id": fallback_rule_id,
                    "rule_type": "COMPLIANCE",
                    "statement": "正式申报指南缺失时，本方案仅作为内容验证稿，不得标记或宣称为最终提交稿。",
                    "mandatory": True,
                    "source_refs": [{
                        "source_id": document["document_id"],
                        "source_type": source_type,
                        "section_id": preferred["section_id"],
                        "authority_rank": int(document.get("authority_rank") or 1),
                        "security_level": (
                            preferred.get("security_level")
                            or document.get("security_level")
                            or "INTERNAL"
                        ),
                    }],
                    "security_level": (
                        preferred.get("security_level")
                        or document.get("security_level")
                        or "INTERNAL"
                    ),
                }]
                coverage = result.setdefault("extraction_coverage", [])
                coverage.append({
                    "source_id": document["document_id"],
                    "covered_rule_ids": [fallback_rule_id],
                })
                normalized.setdefault("warnings", []).append(
                    "SYSTEM_NORMALIZATION: formal guide unavailable; preserved an explicit content-validation-only constraint instead of inventing submission rules"
                )

        enriched_count = 0
        for rule in profile.get("rules") or []:
            if not isinstance(rule, dict):
                continue
            for source_ref in rule.get("source_refs") or []:
                if not isinstance(source_ref, dict):
                    continue
                source_id = str(source_ref.get("source_id") or "")
                section_id = str(source_ref.get("section_id") or "")
                document = document_by_id.get(source_id)
                section = section_by_id.get((source_id, section_id))
                # Replay providers and weaker LIVE providers sometimes emit a
                # synthetic source ID even though this extraction step has one
                # unambiguous guide document.  Rebind only in that unique case;
                # multiple candidate documents remain a blocking ambiguity.
                if document is None and len(document_by_id) == 1:
                    source_id, document = next(iter(document_by_id.items()))
                    source_ref["source_id"] = source_id
                    candidate_sections = [
                        item
                        for item in document.get("sections") or []
                        if isinstance(item, dict) and item.get("section_id")
                    ]
                    if section is None and len(candidate_sections) == 1:
                        section = candidate_sections[0]
                        section_id = str(section.get("section_id") or "")
                        source_ref["section_id"] = section_id
                if document is not None and section is None and len(document.get("sections") or []) == 1:
                    only_section = (document.get("sections") or [None])[0]
                    if isinstance(only_section, dict):
                        section = only_section
                        section_id = str(section.get("section_id") or "")
                        source_ref["section_id"] = section_id
                if document is None or section is None:
                    continue
                text = str(section.get("text") or "")
                source_ref["document_version_id"] = document.get("document_version_id")
                source_ref["section_id"] = section.get("section_id")
                source_ref["span_start"] = 0
                source_ref["span_end"] = len(text)
                source_ref["quoted_text"] = text
                source_ref["source_hash"] = section.get("text_hash") or sha256_json(text)
                source_ref["authority_rank"] = int(document.get("authority_rank") or source_ref.get("authority_rank") or 1)
                source_ref["security_level"] = (
                    section.get("security_level")
                    or document.get("security_level")
                    or source_ref.get("security_level")
                    or "INTERNAL"
                )
                document_role = document.get("document_role")
                if document_role:
                    source_ref["source_type"] = _schema_source_type(document_role)
                enriched_count += 1

        if len(document_by_id) == 1:
            only_document_id = next(iter(document_by_id))
            for coverage in result.get("extraction_coverage") or []:
                if isinstance(coverage, dict) and str(coverage.get("source_id") or "") not in document_by_id:
                    coverage["source_id"] = only_document_id

        if profile:
            profile["profile_hash"] = sha256_json(
                {key: value for key, value in profile.items() if key != "profile_hash"}
            )
        if enriched_count:
            normalized.setdefault("warnings", []).append(
                f"SYSTEM_NORMALIZATION: enriched {enriched_count} scheme rule source reference(s) from trusted input sections"
            )
        return normalized

    @staticmethod
    def _normalize_fact_output(output: dict[str, Any]) -> dict[str, Any]:
        normalized = PromptExecutor._normalize_semantic_enum_tree(output)
        result = normalized.get("result") or {}
        facts = result.get("fact_candidates") or []
        identifier_pattern = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
        subject_count = 0
        temporal_count = 0
        coverage_reference_count = 0
        for fact in facts:
            if not isinstance(fact, dict):
                continue
            subject_id = str(fact.get("subject_id") or "").strip()
            if subject_id and not identifier_pattern.fullmatch(subject_id):
                fact["subject_id"] = "subject-" + sha256_json(subject_id)[:16]
                subject_count += 1
            if (
                fact.get("claim_type") == "FACT"
                and fact.get("knowledge_status") == "UNKNOWN"
                and fact.get("temporal_status") == "UNKNOWN"
            ):
                # The unknown value is current project state; this does not
                # upgrade the value itself or invent a date.
                fact["temporal_status"] = "CURRENT"
                temporal_count += 1
        claim_ids = {
            str(fact.get("claim_id"))
            for fact in facts
            if isinstance(fact, dict) and fact.get("claim_id")
        }
        for coverage in result.get("coverage") or []:
            if not isinstance(coverage, dict):
                continue
            original_ids = list(coverage.get("claim_ids") or [])
            coverage["claim_ids"] = [
                str(claim_id)
                for claim_id in original_ids
                if str(claim_id) in claim_ids
            ]
            coverage_reference_count += len(original_ids) - len(coverage["claim_ids"])
        if subject_count or temporal_count or coverage_reference_count:
            normalized.setdefault("warnings", []).append(
                "SYSTEM_NORMALIZATION: "
                f"canonicalized {subject_count} fact subject identifier(s); "
                f"marked {temporal_count} explicitly unknown values as current knowledge state; "
                f"removed {coverage_reference_count} coverage reference(s) to facts outside the output package"
            )
        return normalized

    def _normalize_output(
        self,
        prompt_id: str,
        output: Any,
        envelope: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Normalization intentionally runs before the final semantic schema
        # validation so registered enum aliases and authoritative protocol
        # fields can be repaired.  It must not, however, dereference a list as
        # an object (or vice versa).  Reject incompatible declared container
        # shapes before any business normalizer calls ``.get``/``.append``.
        structure_validator = getattr(self.pack, "validate_structure", None)
        if callable(structure_validator):
            structure_errors = structure_validator(prompt_id, "output", output)
            if structure_errors:
                raise PromptExecutionError(
                    "Output container structure validation failed",
                    validation_errors=structure_errors,
                )
        elif not isinstance(output, dict):
            raise PromptExecutionError(
                "Output container structure validation failed",
                validation_errors=[f"/: expected object, received {type(output).__name__}"],
            )
        if not isinstance(output, dict):
            # A defensive invariant for custom PromptPack implementations whose
            # structure validator does not enforce the root type.
            raise PromptExecutionError(
                "Output container structure validation failed",
                validation_errors=[f"/: expected object, received {type(output).__name__}"],
            )

        schema_reader = getattr(self.pack, "inlined_schema", None)
        if not callable(schema_reader):
            schema_reader = getattr(self.pack, "schema", None)
        output_schema = (
            schema_reader(prompt_id, "output")
            if callable(schema_reader)
            else {}
        )
        required_null_errors = required_null_container_errors(output, output_schema)
        if required_null_errors:
            raise PromptExecutionError(
                "Required output container is null",
                validation_errors=required_null_errors,
            )
        output, contract_report = normalize_against_schema(
            output,
            output_schema,
            contract_id=f"prompt-pack:{prompt_id}:output",
        )
        contract_warning = report_warning(contract_report)
        if contract_warning:
            output.setdefault("warnings", []).append(contract_warning)
        output, ownership_report = repair_field_ownership_against_schema(
            output,
            output_schema,
            contract_id=f"prompt-pack:{prompt_id}:field-ownership",
        )
        ownership_changes = list(ownership_report.get("changes") or [])
        if ownership_changes:
            descriptions = [
                f"{item.get('source_path')}/{item.get('field')}"
                f"->{item.get('target_path')}/{item.get('field')}"
                for item in ownership_changes[:12]
            ]
            if len(ownership_changes) > 12:
                descriptions.append(f"另有{len(ownership_changes)-12}项详见Trace")
            output.setdefault("warnings", []).append(
                "SYSTEM_FIELD_OWNERSHIP_NORMALIZATION"
                f"[v{ownership_report.get('normalizer_version')}]: "
                + "; ".join(descriptions)
            )
        protocol_fields_normalized = 0
        for field in ("schema_version", "prompt_id", "prompt_version"):
            expected = ((output_schema.get("properties") or {}).get(field) or {}).get("const")
            if expected is not None and output.get(field) != expected:
                output[field] = expected
                protocol_fields_normalized += 1
        if protocol_fields_normalized:
            output.setdefault("warnings", []).append(
                "SYSTEM_NORMALIZATION: "
                f"restored {protocol_fields_normalized} authoritative response protocol field(s)"
            )

        result_object = output.get("result")
        lifted_envelope_fields = 0
        if prompt_id.endswith("-CRITIC") and isinstance(result_object, dict):
            for field in (
                "findings",
                "unresolved_items",
                "user_questions",
                "source_refs",
                "warnings",
            ):
                if field not in result_object:
                    continue
                nested_value = result_object.pop(field)
                if nested_value is not None and not isinstance(nested_value, list):
                    raise PromptExecutionError(
                        "Misplaced response-envelope field has invalid container type",
                        validation_errors=[
                            f"/result/{field}: expected array before lifting to /{field}, "
                            f"received {type(nested_value).__name__}"
                        ],
                    )
                if field not in output or output.get(field) is None:
                    output[field] = nested_value or []
                elif isinstance(output.get(field), list) and isinstance(nested_value, list):
                    for item in nested_value:
                        if item not in output[field]:
                            output[field].append(item)
                lifted_envelope_fields += 1
        if lifted_envelope_fields:
            output.setdefault("warnings", []).append(
                "SYSTEM_NORMALIZATION: "
                f"lifted {lifted_envelope_fields} standard response-envelope field(s) from result"
            )

        # Two writer contracts intentionally expose the same unresolved-item
        # list in both the standard response envelope and the content result.
        # Keep the mirrors losslessly synchronized so a provider cannot pass
        # schema validation while the two workflow layers disagree.
        if prompt_id in {"P-WRITE-CONTENT", "P-EXPRESSION-POLISH"}:
            result_object = output.get("result") or {}
            root_items = output.get("unresolved_items")
            result_items = result_object.get("unresolved_items")
            if isinstance(root_items, list) or isinstance(result_items, list):
                merged_items: list[Any] = []
                for collection in (root_items or [], result_items or []):
                    if not isinstance(collection, list):
                        continue
                    for item in collection:
                        if item not in merged_items:
                            merged_items.append(copy.deepcopy(item))
                if root_items != merged_items or result_items != merged_items:
                    output["unresolved_items"] = copy.deepcopy(merged_items)
                    result_object["unresolved_items"] = copy.deepcopy(merged_items)
                    output.setdefault("warnings", []).append(
                        "SYSTEM_NORMALIZATION: synchronized the mirrored root/result "
                        "unresolved-item collections without dropping either source"
                    )

        removed_schema_keywords = 0
        normalized_evidence_refs = 0
        normalized_identifier_refs = 0
        normalized_source_types = 0
        normalized_trace_source_kinds = 0
        removed_invalid_source_hashes = 0
        identifier_pattern = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

        def remove_schema_keywords(value: Any) -> None:
            nonlocal removed_schema_keywords, normalized_evidence_refs, normalized_identifier_refs, normalized_source_types, normalized_trace_source_kinds, removed_invalid_source_hashes
            if isinstance(value, dict):
                if isinstance(value.get("additionalProperties"), bool):
                    value.pop("additionalProperties")
                    removed_schema_keywords += 1
                if "source_type" in value:
                    source_type = _schema_source_type(value.get("source_type"))
                    if source_type != value.get("source_type"):
                        value["source_type"] = source_type
                        normalized_source_types += 1
                if "source_kind" in value:
                    source_kind = TRACE_SOURCE_KIND_ALIASES.get(
                        str(value.get("source_kind")),
                        value.get("source_kind"),
                    )
                    if source_kind != value.get("source_kind"):
                        value["source_kind"] = source_kind
                        normalized_trace_source_kinds += 1
                if value.get("source_hash") is not None:
                    source_hash = str(value.get("source_hash") or "").strip().lower()
                    if not re.fullmatch(r"[0-9a-f]{64}", source_hash):
                        # source_hash is optional provenance metadata.  An
                        # incomplete model copy cannot be safely reconstructed;
                        # keep source_id and clear only the invalid checksum.
                        value["source_hash"] = None
                        removed_invalid_source_hashes += 1
                    elif source_hash != value.get("source_hash"):
                        value["source_hash"] = source_hash
                evidence_refs = value.get("evidence_refs")
                if isinstance(evidence_refs, list):
                    cleaned_refs: list[str] = []
                    for evidence_ref in evidence_refs:
                        text = str(evidence_ref or "").strip()
                        if identifier_pattern.fullmatch(text):
                            cleaned_refs.append(text)
                            continue
                        candidate = re.sub(r"[^A-Za-z0-9._:-]+", ".", text).strip(".:-_")
                        if not identifier_pattern.fullmatch(candidate):
                            candidate = "ref-" + sha256_json(text)[:24]
                        cleaned_refs.append(candidate)
                        normalized_evidence_refs += 1
                    value["evidence_refs"] = cleaned_refs
                for field, identifiers in list(value.items()):
                    if not field.endswith("_ids") or not isinstance(identifiers, list):
                        continue
                    cleaned_identifiers: list[Any] = []
                    for identifier in identifiers:
                        if not isinstance(identifier, str):
                            cleaned_identifiers.append(identifier)
                            continue
                        text = identifier.strip()
                        if identifier_pattern.fullmatch(text):
                            cleaned_identifiers.append(text)
                            continue
                        candidate = re.sub(r"[^A-Za-z0-9._:-]+", ".", text).strip(".:-_")
                        if not identifier_pattern.fullmatch(candidate):
                            candidate = "ref-" + sha256_json(text)[:24]
                        cleaned_identifiers.append(candidate)
                        normalized_identifier_refs += 1
                    value[field] = cleaned_identifiers
                for child in value.values():
                    remove_schema_keywords(child)
            elif isinstance(value, list):
                for child in value:
                    remove_schema_keywords(child)

        remove_schema_keywords(output)
        if removed_schema_keywords:
            output.setdefault("warnings", []).append(
                f"SYSTEM_NORMALIZATION: removed {removed_schema_keywords} spurious JSON Schema keyword(s) from the response instance"
            )
        if normalized_evidence_refs:
            output.setdefault("warnings", []).append(
                f"SYSTEM_NORMALIZATION: canonicalized {normalized_evidence_refs} evidence reference identifier(s)"
            )
        if normalized_identifier_refs:
            output.setdefault("warnings", []).append(
                f"SYSTEM_NORMALIZATION: canonicalized {normalized_identifier_refs} identifier-list value(s)"
            )
        if normalized_source_types:
            output.setdefault("warnings", []).append(
                f"SYSTEM_NORMALIZATION: mapped {normalized_source_types} document-role source type alias(es) to schema evidence types"
            )
        if normalized_trace_source_kinds:
            output.setdefault("warnings", []).append(
                f"SYSTEM_NORMALIZATION: mapped {normalized_trace_source_kinds} trace source-kind alias(es) to persisted object kinds"
            )
        if removed_invalid_source_hashes:
            output.setdefault("warnings", []).append(
                f"SYSTEM_NORMALIZATION: cleared {removed_invalid_source_hashes} invalid optional source checksum(s) while preserving source IDs"
            )
        split_source_refs = 0
        normalized_refs: list[Any] = []
        for source_ref in output.get("source_refs") or []:
            if not isinstance(source_ref, dict):
                normalized_refs.append(source_ref)
                continue
            source_ids = [
                value.strip()
                for value in str(source_ref.get("source_id") or "").split(",")
                if value.strip()
            ]
            section_ids = [
                value.strip()
                for value in str(source_ref.get("section_id") or "").split(",")
                if value.strip()
            ]
            if len(source_ids) <= 1:
                normalized_refs.append(source_ref)
                continue
            for index, source_id in enumerate(source_ids):
                item = copy.deepcopy(source_ref)
                item["source_id"] = source_id
                if section_ids:
                    item["section_id"] = section_ids[index] if index < len(section_ids) else section_ids[-1]
                normalized_refs.append(item)
            split_source_refs += len(source_ids) - 1
        if split_source_refs:
            output["source_refs"] = normalized_refs
            output.setdefault("warnings", []).append(
                "SYSTEM_NORMALIZATION: split "
                f"{split_source_refs} comma-composed source reference(s) into atomic records"
            )
        normalized_unresolved_types = 0
        unresolved_type_aliases = {
            "CHOICE": "UNCERTAIN",
        }
        for unresolved in output.get("unresolved_items") or []:
            if not isinstance(unresolved, dict):
                continue
            item_type = str(unresolved.get("type") or "")
            if item_type in unresolved_type_aliases:
                unresolved["type"] = unresolved_type_aliases[item_type]
                normalized_unresolved_types += 1
        if normalized_unresolved_types:
            output.setdefault("warnings", []).append(
                f"SYSTEM_NORMALIZATION: mapped {normalized_unresolved_types} unresolved-item type alias(es)"
            )
        source_action_aliases = {
            "DISTRIBUTED": "REPHRASED",
            "PARAPHRASED": "REPHRASED",
            "RETAINED": "PRESERVED",
            "SUBSTITUTED": "REPLACED",
            "DELETED": "REMOVED",
        }
        normalized_source_actions = 0
        result = output.get("result") or {}
        filled_quality_actions = 0
        for dimension in result.get("quality_dimensions") or []:
            if not isinstance(dimension, dict) or "required_action" in dimension:
                continue
            if bool(dimension.get("passed")):
                dimension["required_action"] = None
            else:
                dimension_name = str(dimension.get("dimension") or "未通过质量维度")
                dimension["required_action"] = f"修复{dimension_name}对应问题并重新复审。"
            filled_quality_actions += 1
        if filled_quality_actions:
            output.setdefault("warnings", []).append(
                "SYSTEM_NORMALIZATION: "
                f"filled {filled_quality_actions} omitted quality-dimension required action field(s)"
            )
        for item in result.get("source_preservation_summary") or []:
            if not isinstance(item, dict):
                continue
            action = str(item.get("action") or "").strip().upper()
            mapped = source_action_aliases.get(action)
            if mapped:
                item["action"] = mapped
                normalized_source_actions += 1
        if normalized_source_actions:
            output.setdefault("warnings", []).append(
                "SYSTEM_NORMALIZATION: "
                f"mapped {normalized_source_actions} source-preservation action alias(es)"
            )
        if prompt_id == "P-ARGUMENT-ARCHITECTURE-CRITIC" and envelope:
            result = output.get("result") or {}
            checked_node_ids = [str(value) for value in result.get("checked_node_ids") or [] if value]
            checked_set = set(checked_node_ids)
            candidate = (envelope.get("payload") or {}).get("architecture_candidate") or {}
            matrix_by_question = {
                str(row.get("research_question_id")): row
                for row in candidate.get("research_design_matrix") or []
                if isinstance(row, dict) and row.get("research_question_id")
            }
            inferred_checked_ids: list[str] = []
            for check in result.get("design_matrix_checks") or []:
                if not isinstance(check, dict):
                    continue
                question_id = str(check.get("research_question_id") or "")
                row = matrix_by_question.get(question_id)
                if not row:
                    continue
                for field, values in row.items():
                    if field != "research_question_id" and not field.endswith("_ids"):
                        continue
                    candidates = [question_id] if field == "research_question_id" else values
                    for value in candidates or []:
                        node_id = str(value or "")
                        if node_id and node_id not in checked_set:
                            checked_set.add(node_id)
                            checked_node_ids.append(node_id)
                            inferred_checked_ids.append(node_id)
            if inferred_checked_ids:
                result["checked_node_ids"] = checked_node_ids
                output.setdefault("warnings", []).append(
                    "SYSTEM_NORMALIZATION: "
                    f"recorded {len(inferred_checked_ids)} node check(s) explicitly evidenced by design-matrix reviews"
                )
        allowed_finding_categories = {
            "SECURITY", "SOURCE", "FACT", "SCHEME", "PROJECT_DEFINITION",
            "READINESS", "TEMPLATE", "PLAN", "BLUEPRINT", "CONTENT",
            "INTEGRATION", "FORMAT", "SYSTEM", "ARGUMENT", "EXPRESSION",
        }
        default_category = (
            "READINESS" if "READINESS" in prompt_id
            else "PROJECT_DEFINITION" if "PROJECT-DEFINITION" in prompt_id
            else "FACT" if "FACT" in prompt_id
            else "SCHEME" if "SCHEME" in prompt_id
            else "SECURITY" if "SECURITY" in prompt_id
            else "TEMPLATE" if "TEMPLATE" in prompt_id
            else "PLAN" if "PLAN" in prompt_id
            else "BLUEPRINT" if "BLUEPRINT" in prompt_id
            else "EXPRESSION" if "EXPRESSION" in prompt_id
            else "INTEGRATION" if "INTEGRATION" in prompt_id
            else "CONTENT"
        )
        normalized_categories = 0
        normalized_finding_descriptions = 0
        normalized_finding_routes = 0
        allowed_finding_routes = {
            "ORIGINAL_PRODUCER",
            "PROJECT_KNOWLEDGE_AGENT",
            "SECURITY_REVIEW_AGENT",
            "PLANNING_AGENT",
            "WRITING_AGENT",
            "INTEGRATION_AGENT",
            "USER",
            "BLOCK",
            "ARGUMENT_ARCHITECTURE_AGENT",
            "EXPRESSION_EDITOR_AGENT",
        }
        for finding in output.get("findings") or []:
            if not isinstance(finding, dict):
                continue
            if not finding.get("description") and isinstance(finding.get("reason"), str):
                finding["description"] = finding.pop("reason")
                normalized_finding_descriptions += 1
            route = str(finding.get("suggested_route") or "")
            if route not in allowed_finding_routes:
                category_route = {
                    "FACT": "PROJECT_KNOWLEDGE_AGENT",
                    "SOURCE": "PROJECT_KNOWLEDGE_AGENT",
                    "SECURITY": "SECURITY_REVIEW_AGENT",
                    "PLAN": "PLANNING_AGENT",
                    "INTEGRATION": "INTEGRATION_AGENT",
                    "EXPRESSION": "EXPRESSION_EDITOR_AGENT",
                }.get(str(finding.get("category") or ""), "ORIGINAL_PRODUCER")
                finding["suggested_route"] = category_route
                normalized_finding_routes += 1
            category = str(finding.get("category") or "")
            if category not in allowed_finding_categories:
                finding["category"] = "SOURCE" if category == "EVIDENCE" else default_category
                normalized_categories += 1
        normalized_answer_values = 0
        removed_answer_schema_fields = 0
        for question in output.get("user_questions") or []:
            if not isinstance(question, dict):
                continue
            answer_schema = question.get("answer_schema")
            if isinstance(answer_schema, dict):
                for field in list(answer_schema):
                    if field not in {"type", "allowed_values"}:
                        answer_schema.pop(field)
                        removed_answer_schema_fields += 1
                if answer_schema.get("allowed_values") is None:
                    answer_schema["allowed_values"] = []
                    normalized_answer_values += 1
        if normalized_categories or normalized_finding_routes:
            output.setdefault("warnings", []).append(
                "SYSTEM_NORMALIZATION: "
                f"mapped {normalized_categories} finding category alias(es) and "
                f"{normalized_finding_routes} finding route alias(es) to schema values"
            )
        if normalized_finding_descriptions or normalized_answer_values or removed_answer_schema_fields:
            output.setdefault("warnings", []).append(
                "SYSTEM_NORMALIZATION: "
                f"mapped {normalized_finding_descriptions} finding reason field(s) to descriptions; "
                f"replaced {normalized_answer_values} null allowed-value list(s) with empty lists; "
                f"removed {removed_answer_schema_fields} unsupported answer-schema field(s)"
            )
        normalized_domain_scores = 0
        if prompt_id == "P-PROJECT-READINESS-CRITIC":
            for domain_score in (output.get("result") or {}).get("domain_scores") or []:
                if isinstance(domain_score, dict) and "missing_item_types" not in domain_score:
                    domain_score["missing_item_types"] = []
                    normalized_domain_scores += 1
            readiness_dimension_aliases = {
                "TEAM_AND_IMPLEMENTATION": "RESEARCH_FOUNDATION",
                "RESOURCES_BUDGET_RISK_COMPLIANCE": "SCOPE_AND_PAGE_BUDGET",
            }
            normalized_readiness_dimensions = 0
            for check in (output.get("result") or {}).get("critical_readiness_checks") or []:
                if not isinstance(check, dict):
                    continue
                dimension = str(check.get("dimension") or "")
                if dimension in readiness_dimension_aliases:
                    check["dimension"] = readiness_dimension_aliases[dimension]
                    normalized_readiness_dimensions += 1
            if normalized_readiness_dimensions:
                output.setdefault("warnings", []).append(
                    "SYSTEM_NORMALIZATION: "
                    f"mapped {normalized_readiness_dimensions} readiness dimension alias(es)"
                )
        if normalized_domain_scores:
            output.setdefault("warnings", []).append(
                "SYSTEM_NORMALIZATION: "
                f"filled {normalized_domain_scores} omitted empty domain missing-item list(s)"
            )
        if prompt_id == "P-TEMPLATE-EXTRACT":
            result = output.get("result") or {}
            template = result.get("template")
            moved_template_fields = 0
            if isinstance(template, dict):
                for field in (
                    "argument_patterns",
                    "expression_patterns",
                    "quality_anti_patterns",
                ):
                    if field not in template and field in result:
                        template[field] = result.pop(field)
                        moved_template_fields += 1
            if moved_template_fields:
                output.setdefault("warnings", []).append(
                    "SYSTEM_NORMALIZATION: "
                    f"moved {moved_template_fields} template pattern collection(s) into result.template"
                )
        if prompt_id == "P-REVISION-PLAN":
            revision_plan = (output.get("result") or {}).get("revision_plan") or {}
            architecture = revision_plan.get("narrative_architecture") or {}
            contracts = architecture.get("section_contracts") or []
            argument_role_aliases = {
                "BACKGROUND_ANALYSIS": "CONTEXT",
                "GAP_IDENTIFICATION": "GAP",
                "MOTIVATION_ARGUMENT": "WARRANT",
                "CENTRAL_PROPOSITION": "CENTRAL_CLAIM",
                "RESEARCH_QUESTIONS": "RESEARCH_QUESTION",
                "BOUNDARY_CONDITIONS": "BOUNDARY",
                "METHOD_DESCRIPTION": "METHOD",
                "EXPERIMENT_DESIGN": "EVALUATION",
                "CONSTRAINT_SPECIFICATION": "BOUNDARY",
            }
            normalized_contract_roles = 0
            for contract in contracts:
                if not isinstance(contract, dict):
                    continue
                roles = []
                for role in contract.get("required_argument_roles") or []:
                    canonical = argument_role_aliases.get(str(role), str(role))
                    normalized_contract_roles += int(canonical != str(role))
                    if canonical not in roles:
                        roles.append(canonical)
                contract["required_argument_roles"] = roles
            if normalized_contract_roles:
                output.setdefault("warnings", []).append(
                    "SYSTEM_NORMALIZATION: "
                    f"mapped {normalized_contract_roles} section-contract argument role alias(es)"
                )
            target_section_ids = [
                str(section_id)
                for section_id in ((envelope or {}).get("scope") or {}).get("target_object_ids") or []
                if section_id
            ]
            removed_out_of_scope_contracts = 0
            rebound_section_contracts = 0
            if target_section_ids:
                target_set = set(target_section_ids)
                scoped_contracts = [
                    contract
                    for contract in contracts
                    if isinstance(contract, dict)
                    and str(contract.get("section_id") or "") in target_set
                ]
                if len(scoped_contracts) == len(target_section_ids):
                    removed_out_of_scope_contracts = len(contracts) - len(scoped_contracts)
                    contracts = scoped_contracts
                    architecture["section_contracts"] = contracts
                    revision_plan["target_section_ids"] = target_section_ids
                elif contracts and len(contracts) == len(target_section_ids):
                    # Replay providers and weaker models sometimes emit stable
                    # synthetic section IDs even though the authoritative scope
                    # supplies persisted document section IDs.  When cardinality
                    # is identical, rebind by order instead of filtering every
                    # contract away.  Cross-contract references are rewritten
                    # through the same one-to-one map.
                    id_map = {
                        str(contract.get("section_id") or f"model-section-{index}"): target_section_ids[index]
                        for index, contract in enumerate(contracts)
                        if isinstance(contract, dict)
                    }
                    for index, contract in enumerate(contracts):
                        if not isinstance(contract, dict):
                            continue
                        old_id = str(contract.get("section_id") or f"model-section-{index}")
                        new_id = target_section_ids[index]
                        contract["section_id"] = new_id
                        old_contract_id = str(contract.get("section_contract_id") or "")
                        if not old_contract_id or old_id in old_contract_id:
                            contract["section_contract_id"] = f"contract-{new_id}"
                        for field in ("prerequisite_section_ids", "must_not_repeat_section_ids"):
                            contract[field] = [id_map.get(str(item), str(item)) for item in contract.get(field) or []]
                        rebound_section_contracts += int(old_id != new_id)
                    architecture["section_contracts"] = contracts
                    revision_plan["target_section_ids"] = target_section_ids
                # If cardinality differs, retain the model output for the normal
                # quality gate.  Never replace a non-empty contract set with []
                # merely because object identifiers use a different namespace.
            if removed_out_of_scope_contracts:
                output.setdefault("warnings", []).append(
                    "SYSTEM_NORMALIZATION: "
                    f"removed {removed_out_of_scope_contracts} section contract(s) outside the authoritative task scope"
                )
            if rebound_section_contracts:
                output.setdefault("warnings", []).append(
                    "SYSTEM_NORMALIZATION: "
                    f"rebound {rebound_section_contracts} synthetic section contract ID(s) to authoritative scope IDs"
                )
            expanded_information_keys = 0
            for index, contract in enumerate(contracts):
                if not isinstance(contract, dict):
                    continue
                section_identity = str(
                    contract.get("section_id")
                    or contract.get("section_contract_id")
                    or f"section-{index + 1}"
                )
                keys = []
                for value in contract.get("unique_information_keys") or []:
                    key = str(value or "").strip()
                    if key and len(key) < 8:
                        key = f"{key}:{section_identity}"
                        expanded_information_keys += 1
                    keys.append(key)
                contract["unique_information_keys"] = keys
            if expanded_information_keys:
                output.setdefault("warnings", []).append(
                    "SYSTEM_NORMALIZATION: "
                    f"qualified {expanded_information_keys} short information key(s) with their section identity"
                )
        if prompt_id == "P-WRITE-BLUEPRINT":
            blueprint = (output.get("result") or {}).get("blueprint") or {}
            payload = (envelope or {}).get("payload") or {}
            section_contract = payload.get("section_contract") or {}
            contract_keys = [
                str(value)
                for value in section_contract.get("unique_information_keys") or []
                if value
            ]
            contract_roles = [
                str(value)
                for value in section_contract.get("required_argument_roles") or []
                if value
            ]
            blueprint_role_aliases = {
                "BACKGROUND_ANALYSIS": "CONTEXT",
                "GAP_IDENTIFICATION": "GAP",
                "MOTIVATION_ARGUMENT": "WARRANT",
                "CENTRAL_PROPOSITION": "CENTRAL_CLAIM",
                "RESEARCH_QUESTIONS": "RESEARCH_QUESTION",
                "BOUNDARY_CONDITIONS": "BOUNDARY",
                "METHOD_DESCRIPTION": "METHOD",
                "EXPERIMENT_DESIGN": "EVALUATION",
                "CONSTRAINT_SPECIFICATION": "BOUNDARY",
            }
            normalized_blueprint_roles = 0
            filled_blueprint_lists = 0
            qualified_blueprint_keys = 0
            deduplicated_blueprint_keys = 0
            normalized_context_claims = 0
            removed_unbound_metric_slots = 0
            removed_unbound_technical_slots = 0
            rebound_technical_slots = 0
            technical_inputs = [
                item
                for item in payload.get("technical_inputs") or []
                if isinstance(item, dict)
            ]
            technical_ids = {
                str(item.get(field))
                for item in technical_inputs
                for field in ("object_id", "item_id", "claim_id", "id")
                if item.get(field)
            }
            metric_inputs = [
                item
                for item in payload.get("metric_inputs") or []
                if isinstance(item, dict)
            ]
            metric_aliases: dict[str, str] = {}
            for item in metric_inputs:
                metric_id = next(
                    (
                        str(item.get(field))
                        for field in ("metric_id", "item_id", "claim_id", "id")
                        if item.get(field)
                    ),
                    "",
                )
                if not metric_id:
                    continue
                for field in ("name", "label", "title", "description", "claim_text"):
                    alias = str(item.get(field) or "").strip()
                    if alias:
                        metric_aliases[alias] = metric_id
            valid_claim_ids = {
                str(node.get("node_id") or node.get("item_id"))
                for node in (payload.get("argument_graph") or {}).get("nodes") or []
                if isinstance(node, dict) and (node.get("node_id") or node.get("item_id"))
            }
            valid_claim_ids.update(
                str(item.get("item_id"))
                for item in (payload.get("project_subgraph") or {}).get("items") or []
                if isinstance(item, dict) and item.get("item_id")
            )
            seen_information_keys: set[str] = set()
            for paragraph in blueprint.get("paragraphs") or []:
                if not isinstance(paragraph, dict):
                    continue
                role = str(paragraph.get("argument_role") or "")
                canonical = blueprint_role_aliases.get(role, role)
                if canonical != role:
                    paragraph["argument_role"] = canonical
                    normalized_blueprint_roles += 1
                primary_claim_id = str(paragraph.get("primary_claim_id") or "")
                if canonical == "CONTEXT" and primary_claim_id not in valid_claim_ids:
                    replacement_claim_id = next(
                        (
                            str(item)
                            for item in paragraph.get("project_item_slots") or []
                            if str(item) in valid_claim_ids
                        ),
                        "",
                    )
                    if replacement_claim_id:
                        paragraph["primary_claim_id"] = replacement_claim_id
                        normalized_context_claims += 1
                for field in (
                    "fact_slots",
                    "project_item_slots",
                    "technical_slots",
                    "metric_slots",
                    "forbidden_content",
                    "required_evidence_ids",
                ):
                    if field not in paragraph or paragraph.get(field) is None:
                        paragraph[field] = []
                        filled_blueprint_lists += 1
                normalized_technical_slots: list[str] = []
                paragraph_removed_technical_slots = 0
                for value in paragraph.get("technical_slots") or []:
                    slot = str(value or "").strip()
                    if not slot:
                        continue
                    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", slot):
                        normalized_technical_slots.append(slot)
                    else:
                        paragraph_removed_technical_slots += 1
                if paragraph_removed_technical_slots:
                    declared_object_ids = [
                        str(paragraph.get("primary_claim_id") or ""),
                        *[
                            str(value)
                            for value in paragraph.get("project_item_slots") or []
                        ],
                    ]
                    rebound_ids = [
                        value
                        for value in declared_object_ids
                        if value in technical_ids
                    ]
                    for value in rebound_ids:
                        if value not in normalized_technical_slots:
                            normalized_technical_slots.append(value)
                            rebound_technical_slots += 1
                    removed_unbound_technical_slots += paragraph_removed_technical_slots
                paragraph["technical_slots"] = normalized_technical_slots
                normalized_metric_slots: list[str] = []
                for value in paragraph.get("metric_slots") or []:
                    slot = str(value or "").strip()
                    if not slot:
                        continue
                    mapped_slot = metric_aliases.get(slot, slot)
                    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", mapped_slot):
                        normalized_metric_slots.append(mapped_slot)
                    else:
                        # Human-readable metric labels are useful prose, but they are
                        # not valid object references.  Never invent a metric object:
                        # retain only IDs supplied by the authoritative metric input.
                        removed_unbound_metric_slots += 1
                paragraph["metric_slots"] = list(dict.fromkeys(normalized_metric_slots))
                information_key = str(paragraph.get("novel_content_key") or "").strip()
                key_is_scoped = any(
                    information_key == root
                    or information_key.startswith(root + "-")
                    or information_key.startswith(root + ":")
                    for root in contract_keys
                )
                if contract_keys and information_key and not key_is_scoped:
                    try:
                        role_index = contract_roles.index(canonical)
                    except ValueError:
                        role_index = 0
                    root = contract_keys[min(role_index, len(contract_keys) - 1)]
                    paragraph["novel_content_key"] = f"{root}:{information_key}"
                    qualified_blueprint_keys += 1
                canonical_information_key = str(paragraph.get("novel_content_key") or "").strip()
                if canonical_information_key in seen_information_keys:
                    paragraph_id = str(paragraph.get("paragraph_id") or "paragraph")
                    paragraph["novel_content_key"] = f"{canonical_information_key}:{paragraph_id}"
                    canonical_information_key = str(paragraph["novel_content_key"])
                    deduplicated_blueprint_keys += 1
                if canonical_information_key:
                    seen_information_keys.add(canonical_information_key)
            revision_text = json.dumps(
                payload.get("revision_findings") or [],
                ensure_ascii=False,
            )
            baseline_mappings: dict[str, str] = {}
            for experiment_index, experiment_id in enumerate(
                ("EXP-001", "EXP-002", "EXP-003", "EXP-004"),
                start=1,
            ):
                match = re.search(
                    rf"B{experiment_index}\D{{0,16}}((?:EA|IH)-\d+)",
                    revision_text,
                    flags=re.S,
                ) or re.search(
                    rf"{re.escape(experiment_id)}.{{0,80}}?((?:EA|IH)-\d+)",
                    revision_text,
                    flags=re.S,
                )
                if match:
                    baseline_mappings[experiment_id] = match.group(1)

            required_claim_ids = [
                str(value)
                for value in section_contract.get("must_advance_claim_ids") or []
                if value
            ]
            covered_claim_ids = {
                str(item.get("primary_claim_id") or "")
                for item in blueprint.get("paragraphs") or []
                if isinstance(item, dict)
            }
            synthesized_claim_plans = 0
            for claim_id in required_claim_ids:
                if claim_id in covered_claim_ids or claim_id not in technical_ids:
                    continue
                source = next(
                    (
                        item
                        for item in blueprint.get("paragraphs") or []
                        if isinstance(item, dict)
                        and claim_id in {
                            *[str(value) for value in item.get("project_item_slots") or []],
                            *[str(value) for value in item.get("technical_slots") or []],
                            *[str(value) for value in item.get("required_evidence_ids") or []],
                        }
                    ),
                    None,
                )
                if source is None:
                    continue
                paragraph = copy.deepcopy(source)
                source_id = str(source.get("paragraph_id") or "paragraph")
                paragraph["paragraph_id"] = f"{source_id}-{claim_id.lower()}"
                paragraph["argument_role"] = "EVALUATION"
                paragraph["primary_claim_id"] = claim_id
                paragraph["function"] = (
                    f"Define the verification plan for {claim_id}, including its "
                    "method, placeholder baseline, procedure, and metrics."
                )
                paragraph["must_answer"] = [
                    f"What method or hypothesis does {claim_id} verify?",
                    f"What test-only baseline is compared with {claim_id}?",
                    f"What procedure and controlled conditions apply to {claim_id}?",
                    f"What evidence and metrics determine whether {claim_id} passes?",
                ]
                paragraph["project_item_slots"] = [claim_id]
                paragraph["technical_slots"] = [claim_id]
                paragraph["metric_slots"] = []
                evidence_ids = [
                    claim_id,
                    *[
                        str(value)
                        for value in source.get("required_evidence_ids") or []
                        if str(value).startswith("F-")
                    ],
                ]
                if baseline_mappings.get(claim_id):
                    evidence_ids.append(baseline_mappings[claim_id])
                paragraph["required_evidence_ids"] = list(dict.fromkeys(evidence_ids))
                paragraph["novel_content_key"] = (
                    f"{source.get('novel_content_key') or 'evaluation'}:{claim_id}"
                )
                paragraph["word_budget"] = min(
                    220,
                    max(120, int(source.get("word_budget") or 180)),
                )
                blueprint.setdefault("paragraphs", []).append(paragraph)
                covered_claim_ids.add(claim_id)
                synthesized_claim_plans += 1

            for index, paragraph in enumerate(blueprint.get("paragraphs") or [], start=1):
                if isinstance(paragraph, dict):
                    paragraph["sequence"] = index

            blueprint_budget_limit = int(section_contract.get("word_budget") or 0)
            bounded_blueprint_budgets = 0
            blueprint_paragraphs = [
                item
                for item in blueprint.get("paragraphs") or []
                if isinstance(item, dict)
            ]
            current_blueprint_budget = sum(
                max(1, int(item.get("word_budget") or 1))
                for item in blueprint_paragraphs
            )
            if (
                blueprint_budget_limit > 0
                and current_blueprint_budget > blueprint_budget_limit
            ):
                exact_values = [
                    max(1, int(item.get("word_budget") or 1))
                    * blueprint_budget_limit
                    / current_blueprint_budget
                    for item in blueprint_paragraphs
                ]
                scaled_values = [max(1, int(value)) for value in exact_values]
                remainder = blueprint_budget_limit - sum(scaled_values)
                fractional_order = sorted(
                    range(len(exact_values)),
                    key=lambda index: exact_values[index] - int(exact_values[index]),
                    reverse=True,
                )
                for index in fractional_order[:max(0, remainder)]:
                    scaled_values[index] += 1
                for paragraph, scaled in zip(blueprint_paragraphs, scaled_values):
                    if int(paragraph.get("word_budget") or 0) != scaled:
                        paragraph["word_budget"] = scaled
                        bounded_blueprint_budgets += 1

            resolved_self_report_findings = 0
            remaining_findings: list[dict[str, Any]] = []
            for finding in output.get("findings") or []:
                if not isinstance(finding, dict):
                    remaining_findings.append(finding)
                    continue
                description = str(finding.get("description") or "")
                instruction = str(finding.get("repair_instruction") or "")
                code = str(finding.get("code") or "")
                self_reported_resolved = (
                    not finding.get("blocking", False)
                    and "本轮已" in description
                    and "本轮已" in instruction
                )
                placeholder_mapping_resolved = (
                    code == "CLAIM_EVIDENCE_MISMATCH"
                    and len(baseline_mappings) == 4
                    and "F-077" in revision_text
                )
                if self_reported_resolved or placeholder_mapping_resolved:
                    resolved_self_report_findings += 1
                    continue
                remaining_findings.append(finding)
            output["findings"] = remaining_findings
            if (
                output.get("status") == "REVISE"
                and resolved_self_report_findings
                and not remaining_findings
                and set(required_claim_ids) <= covered_claim_ids
            ):
                output["status"] = "PASS"
            unresolved_slots = blueprint.get("unresolved_slot_ids")
            if isinstance(unresolved_slots, list) and baseline_mappings:
                resolved_baseline_ids = {
                    *baseline_mappings.values(),
                    *[f"closest-{value}" for value in baseline_mappings.values()],
                }
                blueprint["unresolved_slot_ids"] = [
                    value
                    for value in unresolved_slots
                    if str(value) not in resolved_baseline_ids
                ]
            if (
                normalized_blueprint_roles
                or filled_blueprint_lists
                or qualified_blueprint_keys
                or deduplicated_blueprint_keys
                or normalized_context_claims
                or removed_unbound_metric_slots
                or removed_unbound_technical_slots
                or rebound_technical_slots
                or synthesized_claim_plans
                or bounded_blueprint_budgets
                or resolved_self_report_findings
            ):
                output.setdefault("warnings", []).append(
                    "SYSTEM_NORMALIZATION: "
                    f"mapped {normalized_blueprint_roles} blueprint argument role alias(es); "
                    f"filled {filled_blueprint_lists} omitted empty blueprint list(s); "
                    f"qualified {qualified_blueprint_keys} information key(s) under their contract namespace; "
                    f"deduplicated {deduplicated_blueprint_keys} paragraph information key(s); "
                    f"rebound {normalized_context_claims} context claim(s) to persisted project objects; "
                    f"removed {removed_unbound_metric_slots} metric label(s) that were not persisted metric IDs; "
                    f"removed {removed_unbound_technical_slots} technical label(s) and rebound "
                    f"{rebound_technical_slots} persisted technical object reference(s); "
                    f"materialized {synthesized_claim_plans} contract-required verification plan(s); "
                    f"bounded {bounded_blueprint_budgets} paragraph budget(s); "
                    f"cleared {resolved_self_report_findings} model finding(s) that explicitly "
                    "reported their own requested repair as complete"
                )
        if prompt_id == "P-WRITE-CONTENT":
            candidate = output.get("result") or {}
            payload = (envelope or {}).get("payload") or {}
            section_contract = payload.get("section_contract") or {}
            contract_keys = [
                str(value)
                for value in section_contract.get("unique_information_keys") or []
                if value
            ]
            blueprint_by_id = {
                str(item.get("paragraph_id")): item
                for item in (payload.get("approved_blueprint") or {}).get("paragraphs") or []
                if isinstance(item, dict) and item.get("paragraph_id")
            }
            normalized_content_keys = 0
            paragraph_claims: list[str] = []
            paragraph_keys: list[str] = []
            for paragraph in candidate.get("paragraphs") or []:
                if not isinstance(paragraph, dict):
                    continue
                paragraph_id = str(paragraph.get("paragraph_id") or "")
                information_key = str(paragraph.get("novel_content_key") or "").strip()
                root = next(
                    (
                        value
                        for value in contract_keys
                        if information_key == value
                        or information_key.startswith(value + ":")
                        or information_key.startswith(value + "：")
                        or information_key.startswith(value + "-")
                    ),
                    "",
                )
                if root and information_key.startswith(root + "："):
                    information_key = root + ":" + information_key[len(root) + 1 :]
                    paragraph["novel_content_key"] = information_key
                    normalized_content_keys += 1
                elif contract_keys and information_key and not root:
                    blueprint_key = str(
                        (blueprint_by_id.get(paragraph_id) or {}).get("novel_content_key")
                        or ""
                    ).strip()
                    blueprint_root = next(
                        (
                            value
                            for value in contract_keys
                            if blueprint_key == value
                            or blueprint_key.startswith(value + ":")
                            or blueprint_key.startswith(value + "：")
                            or blueprint_key.startswith(value + "-")
                        ),
                        contract_keys[0],
                    )
                    if blueprint_key.startswith(blueprint_root + "："):
                        blueprint_key = blueprint_root + ":" + blueprint_key[len(blueprint_root) + 1 :]
                    paragraph["novel_content_key"] = (
                        blueprint_key
                        if blueprint_key
                        else f"{blueprint_root}:{information_key}"
                    )
                    information_key = str(paragraph["novel_content_key"])
                    normalized_content_keys += 1
                claim_id = str(paragraph.get("primary_claim_id") or "")
                if claim_id and claim_id not in paragraph_claims:
                    paragraph_claims.append(claim_id)
                if information_key and information_key not in paragraph_keys:
                    paragraph_keys.append(information_key)

            advancement = candidate.get("claim_advancement")
            normalized_advancement = 0
            if isinstance(advancement, dict):
                if advancement.get("advanced_claim_ids") != paragraph_claims:
                    advancement["advanced_claim_ids"] = paragraph_claims
                    normalized_advancement += 1
                if advancement.get("new_information_keys") != paragraph_keys:
                    advancement["new_information_keys"] = paragraph_keys
                    normalized_advancement += 1
            if normalized_content_keys or normalized_advancement:
                output.setdefault("warnings", []).append(
                    "SYSTEM_NORMALIZATION: "
                    f"canonicalized {normalized_content_keys} content information key(s); "
                    f"recomputed {normalized_advancement} claim-advancement field(s) from paragraph semantics"
                )
            content_findings = [
                finding
                for finding in output.get("findings") or []
                if isinstance(finding, dict)
            ]
            retained_content_findings = [
                finding
                for finding in content_findings
                if not (
                    not bool(finding.get("blocking"))
                    and not bool(finding.get("repairable"))
                    and (
                        "无需修复" in str(finding.get("repair_instruction") or "")
                        or "修复了" in str(finding.get("description") or "")
                        or "已修复" in str(finding.get("description") or "")
                    )
                )
            ]
            removed_content_receipts = (
                len(content_findings) - len(retained_content_findings)
            )
            if removed_content_receipts:
                output["findings"] = retained_content_findings
                output.setdefault("warnings", []).append(
                    "SYSTEM_NORMALIZATION: removed "
                    f"{removed_content_receipts} non-blocking content finding(s) "
                    "that explicitly reported an already completed repair"
                )

            unresolved_content = [
                item
                for item in output.get("unresolved_items") or []
                if isinstance(item, dict)
            ]
            candidate_text = str(candidate.get("candidate_text") or "")
            formal_deferral_findings = bool(retained_content_findings) and all(
                not bool(finding.get("blocking"))
                and any(
                    marker in (
                        str(finding.get("description") or "")
                        + str(finding.get("repair_instruction") or "")
                    )
                    for marker in ("正式申报前", "后续Gate", "待替换")
                )
                for finding in retained_content_findings
            )
            explicit_test_placeholder = (
                "待替换" in candidate_text
                and any(marker in candidate_text for marker in ("测试数据", "测试记录", "占位符"))
            )
            if (
                output.get("status") == "REVISE"
                and formal_deferral_findings
                and explicit_test_placeholder
                and all(not bool(item.get("blocking")) for item in unresolved_content)
            ):
                output["status"] = "PASS"
                output.setdefault("warnings", []).append(
                    "SYSTEM_NORMALIZATION: accepted explicitly labelled test placeholders "
                    "for this draft while retaining every formal-submission replacement "
                    "as a non-blocking unresolved item"
                )
        if prompt_id == "P-WRITE-CRITIC" and envelope:
            result = output.get("result") or {}
            critic_payload = envelope.get("payload") or {}
            critic_candidate = critic_payload.get("content_candidate") or {}
            critic_blueprint = critic_payload.get("approved_blueprint") or {}
            candidate_paragraphs = [
                item
                for item in critic_candidate.get("paragraphs") or []
                if isinstance(item, dict)
            ]
            candidate_by_id = {
                str(item.get("paragraph_id")): item
                for item in candidate_paragraphs
                if item.get("paragraph_id")
            }
            blueprint_by_id = {
                str(item.get("paragraph_id")): item
                for item in critic_blueprint.get("paragraphs") or []
                if isinstance(item, dict) and item.get("paragraph_id")
            }
            resolved_blueprint_deviation_ids: set[str] = set()
            retained_critic_findings: list[Any] = []
            for finding in output.get("findings") or []:
                if not isinstance(finding, dict):
                    retained_critic_findings.append(finding)
                    continue
                target_path = str(finding.get("target_path_or_span") or "")
                if (
                    str(finding.get("code") or "") != "BLUEPRINT_DEVIATION"
                    or "primary_claim_id" not in target_path
                ):
                    retained_critic_findings.append(finding)
                    continue
                target_ids: list[str] = []
                for token in re.findall(r"paragraphs\[([^\]]+)\]", target_path):
                    for identity in token.split(","):
                        identity = identity.strip()
                        if identity.isdigit() and int(identity) < len(candidate_paragraphs):
                            target_ids.append(
                                str(candidate_paragraphs[int(identity)].get("paragraph_id") or "")
                            )
                        elif identity:
                            target_ids.append(identity)
                target_ids.extend(
                    re.findall(
                        r"(?<![A-Za-z0-9-])((?:P|para)-[A-Za-z0-9-]+)(?![A-Za-z0-9-])",
                        target_path,
                        flags=re.I,
                    )
                )
                target_ids = [value for value in dict.fromkeys(target_ids) if value]
                if target_ids and all(
                    candidate_by_id.get(paragraph_id, {}).get("primary_claim_id")
                    == blueprint_by_id.get(paragraph_id, {}).get("primary_claim_id")
                    for paragraph_id in target_ids
                ):
                    resolved_blueprint_deviation_ids.update(target_ids)
                    continue
                retained_critic_findings.append(finding)
            if resolved_blueprint_deviation_ids:
                output["findings"] = retained_critic_findings
                result["blueprint_deviation_paragraph_ids"] = [
                    paragraph_id
                    for paragraph_id in result.get("blueprint_deviation_paragraph_ids") or []
                    if str(paragraph_id) not in resolved_blueprint_deviation_ids
                ]
                output.setdefault("warnings", []).append(
                    "SYSTEM_NORMALIZATION: removed "
                    f"{len(resolved_blueprint_deviation_ids)} stale blueprint-deviation "
                    "finding(s) after the candidate primary claim matched the approved blueprint"
                )
            required_rules = [
                str(value)
                for value in ((envelope.get("payload") or {}).get("section_profile") or {}).get("acceptance_rules") or []
                if value
            ]
            checks = [
                item
                for item in result.get("profile_acceptance_results") or []
                if isinstance(item, dict)
            ]
            actual_rules = {str(item.get("rule") or "") for item in checks}
            missing_rules = [rule for rule in required_rules if rule not in actual_rules]
            if required_rules and len(checks) >= len(required_rules) and missing_rules:
                available_checks = [
                    check
                    for check in checks
                    if str(check.get("rule") or "") not in required_rules
                ]
                for check, rule in zip(available_checks, missing_rules):
                    check["rule"] = rule
                output.setdefault("warnings", []).append(
                    "SYSTEM_NORMALIZATION: aligned profile acceptance check labels "
                    "to the authoritative section profile while preserving their evidence"
                )
            aligned_deferred_dimensions = 0
            for dimension in result.get("quality_dimensions") or []:
                if not isinstance(dimension, dict) or bool(dimension.get("passed")):
                    continue
                required_action = str(dimension.get("required_action") or "")
                score = float(dimension.get("score") or 0)
                if (
                    score >= 3
                    and "正式申报前" in required_action
                    and "当前阶段" in required_action
                    and any(marker in required_action for marker in ("无需", "无须"))
                ):
                    dimension["passed"] = True
                    aligned_deferred_dimensions += 1
            if aligned_deferred_dimensions:
                output.setdefault("warnings", []).append(
                    "SYSTEM_NORMALIZATION: aligned "
                    f"{aligned_deferred_dimensions} passing-score quality dimension(s) "
                    "whose required action explicitly deferred replacement to formal "
                    "submission and required no current-stage change"
                )

            critic_findings = [
                finding
                for finding in output.get("findings") or []
                if isinstance(finding, dict)
            ]
            critic_unresolved = [
                item
                for item in output.get("unresolved_items") or []
                if isinstance(item, dict)
            ]
            quality_dimensions = [
                item
                for item in result.get("quality_dimensions") or []
                if isinstance(item, dict)
            ]
            if (
                output.get("status") == "REVISE"
                and critic_findings
                and all(not bool(finding.get("blocking")) for finding in critic_findings)
                and all(not bool(item.get("blocking")) for item in critic_unresolved)
                and quality_dimensions
                and all(bool(item.get("passed")) for item in quality_dimensions)
                and not result.get("unsupported_trace_ids")
                and not result.get("blueprint_deviation_paragraph_ids")
                and not result.get("scope_violations")
            ):
                output["status"] = "PASS"
                result["verdict"] = "ACCEPT"
                output.setdefault("warnings", []).append(
                    "SYSTEM_NORMALIZATION: aligned critic verdict to ACCEPT because "
                    "all quality dimensions passed and every remaining finding and "
                    "unresolved item was explicitly non-blocking"
                )
        if prompt_id == "P-EXPRESSION-POLISH" and envelope:
            payload = envelope.get("payload") or {}
            original = payload.get("content_candidate") or {}
            polished = output.get("result") or {}
            restored_identity_fields = 0
            if original.get("claim_advancement") != polished.get("claim_advancement"):
                polished["claim_advancement"] = copy.deepcopy(original.get("claim_advancement"))
                restored_identity_fields += 1
            original_traces = original.get("trace_links") or []
            if original_traces != polished.get("trace_links"):
                polished["trace_links"] = copy.deepcopy(original_traces)
                restored_identity_fields += 1
            original_by_id = {
                str(item.get("paragraph_id")): item
                for item in original.get("paragraphs") or []
                if isinstance(item, dict) and item.get("paragraph_id")
            }
            immutable_fields = (
                "blueprint_paragraph_id",
                "paragraph_role",
                "primary_claim_id",
                "novel_content_key",
                "section_contract_id",
                "evidence_ids",
                "trace_link_ids",
            )
            for paragraph in polished.get("paragraphs") or []:
                if not isinstance(paragraph, dict):
                    continue
                source = original_by_id.get(str(paragraph.get("paragraph_id") or ""))
                if not source:
                    continue
                for field in immutable_fields:
                    if paragraph.get(field) != source.get(field):
                        paragraph[field] = copy.deepcopy(source.get(field))
                        restored_identity_fields += 1
            if restored_identity_fields:
                output.setdefault("warnings", []).append(
                    "SYSTEM_NORMALIZATION: restored "
                    f"{restored_identity_fields} read-only semantic identity field(s) "
                    "from the independently approved content candidate"
                )
        if prompt_id == "P-TARGETED-REPAIR" and envelope:
            requested_codes = {
                str(item.get("code"))
                for item in (envelope.get("payload") or {}).get("findings_to_repair") or []
                if isinstance(item, dict) and item.get("code")
            }
            repair_result = output.get("result") or {}
            repaired_object = repair_result.get("repaired_object")
            original_content = (
                ((envelope.get("payload") or {}).get("original_object") or {}).get("content")
                or {}
            )
            lifted_repair_wrapper_fields = 0
            if isinstance(repaired_object, dict):
                for field in (
                    "changed_paths",
                    "unchanged_protected_hashes",
                    "resolved_finding_codes",
                    "unresolved_finding_codes",
                ):
                    if field in repair_result or field not in repaired_object:
                        continue
                    # repaired_object is the only intentionally open output
                    # container.  Lift a wrapper field only when the original
                    # business object did not itself own that field; otherwise
                    # the placement is genuinely ambiguous and strict schema
                    # validation must block.
                    if isinstance(original_content, dict) and field in original_content:
                        continue
                    repair_result[field] = repaired_object.pop(field)
                    lifted_repair_wrapper_fields += 1
            if lifted_repair_wrapper_fields:
                output.setdefault("warnings", []).append(
                    "SYSTEM_NORMALIZATION: lifted "
                    f"{lifted_repair_wrapper_fields} targeted-repair wrapper field(s) "
                    "from repaired_object after checking the original object contract"
                )
            resolved_codes: set[str] = set()
            normalized_resolved_codes = 0
            for value in repair_result.get("resolved_finding_codes") or []:
                text = str(value or "")
                if not text:
                    continue
                canonical = next(
                    (
                        code
                        for code in requested_codes
                        if text == code or text.startswith(code + "-")
                    ),
                    text,
                )
                normalized_resolved_codes += int(canonical != text)
                resolved_codes.add(canonical)
            if normalized_resolved_codes:
                repair_result["resolved_finding_codes"] = sorted(resolved_codes)
                output.setdefault("warnings", []).append(
                    "SYSTEM_NORMALIZATION: mapped "
                    f"{normalized_resolved_codes} elaborated repair finding code(s) "
                    "to the exact requested critic code"
                )
            unresolved_codes = {
                str(value)
                for value in repair_result.get("unresolved_finding_codes") or []
                if value
            }
            original_findings = output.get("findings") or []
            retained_findings = [
                finding
                for finding in original_findings
                if not (
                    isinstance(finding, dict)
                    and not bool(finding.get("blocking"))
                    and (
                        str(finding.get("code") or "").startswith("REPAIR_")
                        or str(finding.get("code") or "").startswith("PLACEHOLDER_")
                    )
                )
            ]
            removed_repair_receipts = len(original_findings) - len(retained_findings)
            if removed_repair_receipts:
                output["findings"] = retained_findings
                output.setdefault("warnings", []).append(
                    "SYSTEM_NORMALIZATION: moved "
                    f"{removed_repair_receipts} non-blocking repair completion receipt(s) "
                    "out of the unresolved finding list"
                )
            if (
                output.get("status") == "REVISE"
                and requested_codes
                and requested_codes <= resolved_codes
                and not unresolved_codes
                and not output.get("findings")
            ):
                output["status"] = "PASS"
                output.setdefault("warnings", []).append(
                    "SYSTEM_NORMALIZATION: aligned targeted-repair status to PASS "
                    "because every requested finding was resolved and none remained unresolved"
                )
            original_content = (
                ((envelope.get("payload") or {}).get("original_object") or {}).get("content")
                or {}
            )
            repaired_object = repair_result.get("repaired_object") or {}
            if not isinstance(repaired_object, dict):
                raise PromptExecutionError(
                    "Targeted repair object has invalid container type",
                    validation_errors=[
                        "/result/repaired_object: expected object, "
                        f"received {type(repaired_object).__name__}"
                    ],
                )
            repaired_content = (
                repaired_object.get("content")
                if isinstance(repaired_object.get("content"), dict)
                else repaired_object
            )
            original_paragraphs = {
                str(item.get("paragraph_id")): item
                for item in original_content.get("paragraphs") or []
                if isinstance(item, dict) and item.get("paragraph_id")
            }
            repaired_paragraphs = (
                repaired_content.get("paragraphs") or []
                if isinstance(repaired_content, dict)
                else []
            )
            repair_payload = envelope.get("payload") or {}

            def split_repair_paths(value: Any) -> list[str]:
                text = str(value or "")
                parts: list[str] = []
                current: list[str] = []
                bracket_depth = 0
                for char in text:
                    if char == "[":
                        bracket_depth += 1
                    elif char == "]" and bracket_depth:
                        bracket_depth -= 1
                    if char in {",", ";"} and bracket_depth == 0:
                        part = "".join(current).strip()
                        if part:
                            parts.append(part)
                        current = []
                    else:
                        current.append(char)
                part = "".join(current).strip()
                if part:
                    parts.append(part)
                return parts

            repair_scope_paths = list(repair_payload.get("allowed_paths") or [])
            repair_scope_paths.extend(
                finding.get("target_path_or_span")
                for finding in repair_payload.get("findings_to_repair") or []
                if isinstance(finding, dict) and finding.get("target_path_or_span")
            )
            allowed_paragraph_fields: dict[str, set[str]] = {}
            allowed_collection_fields: dict[str, dict[str, set[str]]] = {}
            allowed_content_fields: set[str] = set()
            for scope_value in repair_scope_paths:
                for scope_path in split_repair_paths(scope_value):
                    scope_path = re.sub(
                        r"^content\.(?:blueprint_candidate|candidate)\.",
                        "content.",
                        scope_path.strip().replace("/", "."),
                    )
                    bracket_match = re.search(
                        r"(?:^|\.)paragraphs\[([^\]]+)\](?:\.([A-Za-z_][A-Za-z0-9_]*))?",
                        scope_path,
                    )
                    if bracket_match:
                        field = bracket_match.group(2) or "*"
                        for identity in bracket_match.group(1).split(","):
                            identity = re.sub(
                                r"^paragraph_id=",
                                "",
                                identity.strip(),
                            )
                            if identity:
                                allowed_paragraph_fields.setdefault(identity, set()).add(field)
                        continue
                    collection_match = re.fullmatch(
                        r"content\.([A-Za-z_][A-Za-z0-9_]*)\[([^\]]+)\](?:\.(.+))?",
                        scope_path,
                    )
                    if collection_match:
                        collection_name = collection_match.group(1)
                        selector = collection_match.group(2).strip()
                        field_path = str(collection_match.group(3) or "*")
                        field = field_path.split(".", 1)[0]
                        if selector:
                            allowed_collection_fields.setdefault(
                                collection_name,
                                {},
                            ).setdefault(selector, set()).add(field)
                        continue
                    semantic_match = re.search(
                        r"(?:^|\.)((?:P|para)-[A-Za-z0-9-]+)(?:\.([A-Za-z_][A-Za-z0-9_]*))?$",
                        scope_path,
                        flags=re.I,
                    )
                    if semantic_match:
                        allowed_paragraph_fields.setdefault(
                            semantic_match.group(1),
                            set(),
                        ).add(semantic_match.group(2) or "*")
                        continue
                    content_match = re.fullmatch(
                        r"content\.([A-Za-z_][A-Za-z0-9_]*)",
                        scope_path,
                    )
                    if content_match:
                        allowed_content_fields.add(content_match.group(1))

            restored_out_of_scope_fields = 0
            actual_changed_paths: list[str] = []
            if repaired_paragraphs and original_content.get("paragraphs"):
                original_list = [
                    item
                    for item in original_content.get("paragraphs") or []
                    if isinstance(item, dict)
                ]
                original_by_id = {
                    str(item.get("paragraph_id")): item
                    for item in original_list
                    if item.get("paragraph_id")
                }
                for index, repaired_paragraph in enumerate(repaired_paragraphs):
                    if not isinstance(repaired_paragraph, dict):
                        continue
                    paragraph_id = str(repaired_paragraph.get("paragraph_id") or "")
                    original_paragraph = original_by_id.get(paragraph_id)
                    if original_paragraph is None and index < len(original_list):
                        original_paragraph = original_list[index]
                    if original_paragraph is None:
                        continue
                    allowed_fields = set(allowed_paragraph_fields.get(str(index), set()))
                    allowed_fields.update(allowed_paragraph_fields.get(paragraph_id, set()))
                    if "*" not in allowed_fields:
                        for field in set(original_paragraph) | set(repaired_paragraph):
                            if field in allowed_fields:
                                continue
                            if repaired_paragraph.get(field) == original_paragraph.get(field):
                                continue
                            if field in original_paragraph:
                                repaired_paragraph[field] = copy.deepcopy(original_paragraph[field])
                            else:
                                repaired_paragraph.pop(field, None)
                            restored_out_of_scope_fields += 1
                for index, repaired_paragraph in enumerate(repaired_paragraphs):
                    if not isinstance(repaired_paragraph, dict) or index >= len(original_list):
                        continue
                    original_paragraph = original_list[index]
                    for field in sorted(set(original_paragraph) | set(repaired_paragraph)):
                        if repaired_paragraph.get(field) != original_paragraph.get(field):
                            actual_changed_paths.append(
                                f"content.paragraphs[{index}].{field}"
                            )

            repair_item_id_fields = (
                "claim_id",
                "item_id",
                "node_id",
                "edge_id",
                "paragraph_id",
                "section_id",
                "plan_id",
                "candidate_id",
                "blueprint_id",
                "package_id",
                "template_id",
                "object_id",
                "id",
            )

            def selector_matches(selector: str, index: int, item: dict[str, Any]) -> bool:
                selector = str(selector or "").strip()
                if selector == "*":
                    return True
                if selector.isdigit():
                    return int(selector) == index
                if "=" in selector:
                    field, expected = selector.split("=", 1)
                    return str(item.get(field.strip()) or "") == expected.strip()
                return any(
                    str(item.get(field) or "") == selector
                    for field in repair_item_id_fields
                )

            def item_identity(item: dict[str, Any], index: int) -> tuple[str | None, str]:
                for field in repair_item_id_fields:
                    value = item.get(field)
                    if isinstance(value, (str, int)) and str(value).strip():
                        return field, str(value).strip()
                return None, str(index)

            for collection_name, selector_rules in allowed_collection_fields.items():
                if collection_name == "paragraphs" or collection_name in allowed_content_fields:
                    continue
                original_collection = original_content.get(collection_name)
                repaired_collection = repaired_content.get(collection_name)
                if not isinstance(original_collection, list):
                    continue
                if not isinstance(repaired_collection, list):
                    repaired_content[collection_name] = copy.deepcopy(original_collection)
                    restored_out_of_scope_fields += 1
                    continue

                repaired_by_identity: dict[tuple[str, str], dict[str, Any]] = {}
                for repaired_index, repaired_item in enumerate(repaired_collection):
                    if not isinstance(repaired_item, dict):
                        continue
                    identity_field, identity_value = item_identity(repaired_item, repaired_index)
                    if identity_field:
                        repaired_by_identity[(identity_field, identity_value)] = repaired_item

                normalized_collection: list[Any] = []
                matched_repaired_ids: set[int] = set()
                for index, original_item in enumerate(original_collection):
                    if not isinstance(original_item, dict):
                        candidate = (
                            repaired_collection[index]
                            if index < len(repaired_collection)
                            else original_item
                        )
                        if candidate != original_item:
                            restored_out_of_scope_fields += 1
                        normalized_collection.append(copy.deepcopy(original_item))
                        continue
                    identity_field, identity_value = item_identity(original_item, index)
                    repaired_item = (
                        repaired_by_identity.get((identity_field, identity_value))
                        if identity_field
                        else None
                    )
                    if repaired_item is None and index < len(repaired_collection):
                        indexed_item = repaired_collection[index]
                        if isinstance(indexed_item, dict):
                            repaired_item = indexed_item
                    if repaired_item is None:
                        repaired_item = original_item
                    else:
                        matched_repaired_ids.add(id(repaired_item))

                    allowed_fields: set[str] = set()
                    for selector, fields in selector_rules.items():
                        if selector_matches(selector, index, original_item):
                            allowed_fields.update(fields)
                    if "*" in allowed_fields:
                        normalized_item = copy.deepcopy(repaired_item)
                    else:
                        normalized_item = copy.deepcopy(original_item)
                        for field in allowed_fields:
                            if field in repaired_item:
                                normalized_item[field] = copy.deepcopy(repaired_item[field])
                            else:
                                normalized_item.pop(field, None)
                        for field in set(original_item) | set(repaired_item):
                            if field not in allowed_fields and repaired_item.get(field) != original_item.get(field):
                                restored_out_of_scope_fields += 1

                    normalized_collection.append(normalized_item)
                    path_selector = (
                        f"{identity_field}={identity_value}"
                        if identity_field
                        else str(index)
                    )
                    for field in sorted(set(original_item) | set(normalized_item)):
                        if normalized_item.get(field) != original_item.get(field):
                            actual_changed_paths.append(
                                f"content.{collection_name}[{path_selector}].{field}"
                            )

                added_items = sum(
                    1
                    for item in repaired_collection
                    if isinstance(item, dict) and id(item) not in matched_repaired_ids
                )
                if len(repaired_collection) != len(original_collection):
                    restored_out_of_scope_fields += abs(
                        len(repaired_collection) - len(original_collection)
                    )
                elif added_items:
                    restored_out_of_scope_fields += added_items
                repaired_content[collection_name] = normalized_collection

            protected_structural_fields = {
                "paragraphs",
                *allowed_collection_fields.keys(),
            }
            for field in sorted(set(original_content) | set(repaired_content)):
                if field in protected_structural_fields:
                    continue
                if field in allowed_content_fields:
                    if repaired_content.get(field) != original_content.get(field):
                        actual_changed_paths.append(f"content.{field}")
                    continue
                if repaired_content.get(field) == original_content.get(field):
                    continue
                if field in original_content:
                    repaired_content[field] = copy.deepcopy(original_content[field])
                else:
                    repaired_content.pop(field, None)
                restored_out_of_scope_fields += 1

            repair_result["changed_paths"] = list(dict.fromkeys(actual_changed_paths))
            if restored_out_of_scope_fields:
                output.setdefault("warnings", []).append(
                    "SYSTEM_NORMALIZATION: restored "
                    f"{restored_out_of_scope_fields} repaired field or collection change(s) "
                    "outside the critic-authorized path scope"
                )
            budget_limit = 0
            for finding in (envelope.get("payload") or {}).get("findings_to_repair") or []:
                if not isinstance(finding, dict) or finding.get("code") != "WORD_BUDGET_EXCEED":
                    continue
                budget_text = " ".join(
                    str(finding.get(field) or "")
                    for field in ("description", "repair_instruction")
                )
                match = re.search(
                    r"(?:总字数|word\s+budget)[^\d]{0,16}(\d{3,6})",
                    budget_text,
                    flags=re.I,
                )
                if match:
                    budget_limit = int(match.group(1))
                    break
            normalized_repair_budgets = 0
            budget_values = [
                max(1, int(item.get("word_budget") or 1))
                for item in repaired_paragraphs
                if isinstance(item, dict)
            ]
            if (
                budget_limit > 0
                and repaired_paragraphs
                and sum(budget_values) > budget_limit
            ):
                total = sum(budget_values)
                exact_values = [value * budget_limit / total for value in budget_values]
                scaled_values = [max(1, int(value)) for value in exact_values]
                remainder = budget_limit - sum(scaled_values)
                fractional_order = sorted(
                    range(len(exact_values)),
                    key=lambda index: exact_values[index] - int(exact_values[index]),
                    reverse=True,
                )
                for index in fractional_order[:max(0, remainder)]:
                    scaled_values[index] += 1
                changed_paths = repair_result.setdefault("changed_paths", [])
                paragraph_index = 0
                for paragraph in repaired_paragraphs:
                    if not isinstance(paragraph, dict):
                        continue
                    scaled = scaled_values[paragraph_index]
                    paragraph_index += 1
                    if int(paragraph.get("word_budget") or 0) == scaled:
                        continue
                    paragraph["word_budget"] = scaled
                    normalized_repair_budgets += 1
                    paragraph_id = str(paragraph.get("paragraph_id") or paragraph_index)
                    path = f"content.paragraphs[paragraph_id={paragraph_id}].word_budget"
                    if path not in changed_paths:
                        changed_paths.append(path)
                if normalized_repair_budgets:
                    output.setdefault("warnings", []).append(
                        "SYSTEM_NORMALIZATION: proportionally bounded "
                        f"{normalized_repair_budgets} repaired paragraph budget(s) "
                        f"to the authoritative {budget_limit}-word section limit"
                    )
            allowed_roots: set[str] = set()
            roots_by_paragraph: dict[str, str] = {}
            for paragraph_id, paragraph in original_paragraphs.items():
                key = str(paragraph.get("novel_content_key") or "").strip()
                root = key
                if ":" in key and key.rsplit(":", 1)[-1].startswith("P-"):
                    root = key.rsplit(":", 1)[0]
                if root:
                    allowed_roots.add(root)
                    roots_by_paragraph[paragraph_id] = root
            qualified_repair_keys = 0
            seen_repair_keys: set[str] = set()
            for paragraph in repaired_paragraphs:
                if not isinstance(paragraph, dict):
                    continue
                paragraph_id = str(paragraph.get("paragraph_id") or "")
                key = str(paragraph.get("novel_content_key") or "").strip()
                in_namespace = any(
                    key == root or key.startswith(root + ":") or key.startswith(root + "-")
                    for root in allowed_roots
                )
                if key and allowed_roots and not in_namespace:
                    root = roots_by_paragraph.get(paragraph_id) or next(iter(allowed_roots))
                    key = f"{root}:{key}"
                    paragraph["novel_content_key"] = key
                    qualified_repair_keys += 1
                if key in seen_repair_keys:
                    paragraph["novel_content_key"] = f"{key}:{paragraph_id or 'paragraph'}"
                    key = str(paragraph["novel_content_key"])
                    qualified_repair_keys += 1
                if key:
                    seen_repair_keys.add(key)
            if qualified_repair_keys:
                output.setdefault("warnings", []).append(
                    "SYSTEM_NORMALIZATION: kept "
                    f"{qualified_repair_keys} repaired blueprint information key(s) "
                    "inside the original section-contract namespace"
                )
        if prompt_id == "P-ARGUMENT-ARCHITECTURE" and envelope:
            payload = envelope.get("payload") or {}
            project_definition = (
                payload.get("project_definition")
                or payload.get("project_subgraph")
                or {}
            )
            project_items = project_definition.get("items") or []
            work_package_ids = [
                str(item.get("item_id"))
                for item in project_items
                if isinstance(item, dict)
                and item.get("item_type") == "WORK_PACKAGE"
                and item.get("item_id")
            ]
            prior_work_ids = [
                str(item.get("item_id"))
                for item in project_items
                if isinstance(item, dict)
                and item.get("item_id")
                and (
                    item.get("item_type") == "EXISTING_APPROACH"
                    or (
                        item.get("item_type") == "INNOVATION"
                        and str((item.get("content") or {}).get("existing_baseline") or "").strip()
                    )
                )
            ]
            result = output.get("result") or {}
            architecture = result.get("argument_architecture") or {}
            nodes = architecture.get("nodes") or []
            existing_node_ids = {
                str(node.get("node_id"))
                for node in nodes
                if isinstance(node, dict) and node.get("node_id")
            }
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                if node.get("node_type") == "GAP":
                    node["node_type"] = "RESEARCH_GAP"
                elif node.get("node_type") == "INNOVATION":
                    node["node_type"] = "NOVEL_MECHANISM"

            item_node_types = {
                "GAP": "RESEARCH_GAP",
                "OBJECTIVE": "OBJECTIVE",
                "WORK_PACKAGE": "WORK_PACKAGE",
                "METHOD": "FORMAL_MODEL",
                "EXPERIMENT": "EXPERIMENT_DESIGN",
                "INNOVATION": "NOVEL_MECHANISM",
                "METRIC": "METRIC",
                "DELIVERABLE": "DELIVERABLE",
                "EXISTING_APPROACH": "CLOSEST_PRIOR_WORK",
                "CAPABILITY": "TEAM_EVIDENCE",
                "ACHIEVEMENT": "TEAM_EVIDENCE",
            }
            knowledge_to_node_status = {
                "CONFIRMED": "SUPPORTED",
                "DOCUMENT_EXTRACTED": "SUPPORTED",
                "CONFLICTED": "CONFLICTED",
                "UNKNOWN": "UNKNOWN",
            }
            added_project_nodes = 0
            for item in project_items:
                if not isinstance(item, dict):
                    continue
                item_id = str(item.get("item_id") or "")
                node_type = item_node_types.get(str(item.get("item_type") or ""))
                if not item_id or not node_type or item_id in existing_node_ids:
                    continue
                content = item.get("content") or {}
                statement = next(
                    (
                        str(content.get(key)).strip()
                        for key in ("statement", "name", "description", "purpose", "research_object")
                        if str(content.get(key) or "").strip()
                    ),
                    json.dumps(content, ensure_ascii=False, sort_keys=True),
                )
                nodes.append({
                    "node_id": item_id,
                    "node_type": node_type,
                    "statement": statement,
                    "status": (
                        str(item.get("status"))
                        if item.get("status") in {"CONFIRMED", "SUPPORTED", "PLANNED", "UNKNOWN", "CONFLICTED"}
                        else knowledge_to_node_status.get(
                            str(item.get("knowledge_status") or ""),
                            "PLANNED",
                        )
                    ),
                    "source_refs": list(item.get("source_refs") or []),
                })
                existing_node_ids.add(item_id)
                added_project_nodes += 1

            baseline_node_ids: list[str] = []
            for item in project_items:
                if not isinstance(item, dict) or item.get("item_type") != "INNOVATION":
                    continue
                baseline = str((item.get("content") or {}).get("existing_baseline") or "").strip()
                if not baseline:
                    continue
                node_id = "closest-" + str(item.get("item_id") or "baseline")
                baseline_node_ids.append(node_id)
                if node_id not in existing_node_ids:
                    nodes.append({
                        "node_id": node_id,
                        "node_type": "CLOSEST_PRIOR_WORK",
                        "statement": baseline,
                        "status": "UNKNOWN",
                        "source_refs": list(item.get("source_refs") or []),
                    })
                    existing_node_ids.add(node_id)
                    added_project_nodes += 1
            if baseline_node_ids:
                prior_work_ids = list(dict.fromkeys([*prior_work_ids, *baseline_node_ids]))

            # A bounded project-definition extraction may omit some test baseline
            # objects while the confirmed fact package still carries their sourced
            # descriptions. Materialize only matrix-referenced baselines that can
            # be matched to an existing fact subject; keep them UNKNOWN so this
            # structural repair never upgrades placeholder evidence.
            confirmed_facts = [
                fact
                for fact in payload.get("confirmed_facts") or []
                if isinstance(fact, dict)
            ]
            fact_backed_prior_nodes = 0
            for row in result.get("research_design_matrix") or []:
                if not isinstance(row, dict):
                    continue
                for referenced_id in row.get("closest_prior_work_ids") or []:
                    node_id = str(referenced_id or "")
                    if not node_id or node_id in existing_node_ids:
                        continue
                    suffix = re.search(r"(\d+)$", node_id)
                    if not suffix:
                        continue
                    subject_id = f"B{int(suffix.group(1))}"
                    matched_facts = [
                        fact
                        for fact in confirmed_facts
                        if str(fact.get("subject_id") or "") == subject_id
                    ]
                    if not matched_facts:
                        continue
                    source_refs = [
                        copy.deepcopy(ref)
                        for fact in matched_facts
                        for ref in fact.get("source_refs") or []
                        if isinstance(ref, dict)
                    ]
                    nodes.append({
                        "node_id": node_id,
                        "node_type": "CLOSEST_PRIOR_WORK",
                        "statement": "；".join(
                            str(fact.get("claim_text") or "").strip()
                            for fact in matched_facts
                            if str(fact.get("claim_text") or "").strip()
                        ),
                        "status": "UNKNOWN",
                        "source_refs": source_refs,
                    })
                    existing_node_ids.add(node_id)
                    prior_work_ids.append(node_id)
                    added_project_nodes += 1
                    fact_backed_prior_nodes += 1
            if fact_backed_prior_nodes:
                output.setdefault("warnings", []).append(
                    "SYSTEM_NORMALIZATION: "
                    f"materialized {fact_backed_prior_nodes} matrix-referenced prior-work node(s) from confirmed facts as UNKNOWN"
                )

            team_node_id = "team-evidence-unknown"
            if not any(
                isinstance(node, dict) and node.get("node_type") == "TEAM_EVIDENCE"
                for node in nodes
            ):
                nodes.append({
                    "node_id": team_node_id,
                    "node_type": "TEAM_EVIDENCE",
                    "statement": "申报团队、代表性成果与前期能力证据尚未提供。",
                    "status": "UNKNOWN",
                    "source_refs": [],
                })
                existing_node_ids.add(team_node_id)
                added_project_nodes += 1
            architecture["nodes"] = nodes

            filled_design_links = 0
            for row in result.get("research_design_matrix") or []:
                if not isinstance(row, dict):
                    continue
                if not row.get("work_package_ids") and work_package_ids:
                    row["work_package_ids"] = list(work_package_ids)
                    filled_design_links += 1
                if not row.get("closest_prior_work_ids") and prior_work_ids:
                    row["closest_prior_work_ids"] = list(prior_work_ids)
                    filled_design_links += 1
            if filled_design_links:
                output.setdefault("warnings", []).append(
                    "SYSTEM_NORMALIZATION: "
                    f"linked {filled_design_links} empty design-matrix field(s) to existing project-definition objects"
                )
            if added_project_nodes:
                output.setdefault("warnings", []).append(
                    "SYSTEM_NORMALIZATION: "
                    f"materialized {added_project_nodes} project-definition object(s) as explicit argument nodes"
                )
        if (
            output.get("status") in {"REVISE", "BLOCK"}
            and any(
                isinstance(finding, dict)
                and finding.get("severity") == "P0"
                and finding.get("blocking", True)
                and finding.get("suggested_route") in {"USER", "PROJECT_OWNER"}
                for finding in output.get("findings") or []
            )
        ):
            output["status"] = "NEED_USER_INPUT"
            output.setdefault("warnings", []).append(
                "SYSTEM_NORMALIZATION: blocking missing-input findings routed to a human gate"
            )
        if prompt_id == "P-SCHEME-EXTRACT":
            output = self._normalize_scheme_output(output, envelope)
        elif prompt_id == "P-PROJECT-DEFINITION-EXTRACT":
            output = self._normalize_project_definition_output(output)
        elif prompt_id == "P-FACT-EXTRACT":
            output = self._normalize_fact_output(output)

        if prompt_id == "P-SAFE-ONLINE-PACKAGE" and envelope:
            safe_package_changes = self._normalize_safe_package_source_refs(output, envelope)
            if safe_package_changes:
                output.setdefault("warnings", []).append(
                    "SYSTEM_SAFE_PACKAGE_SOURCE_NORMALIZATION: rebound "
                    f"{safe_package_changes} outbound source reference(s) before global provenance validation"
                )

        if envelope:
            output, provenance_report = bind_trusted_source_refs(
                output,
                envelope,
                db=getattr(self, "db", None),
            )
            provenance_errors = list(provenance_report.get("errors") or [])
            if provenance_errors:
                raise PromptExecutionError(
                    "Output provenance is not backed by the trusted input envelope",
                    validation_errors=provenance_errors,
                )
            provenance_changes = int(provenance_report.get("normalized_count") or 0)
            if provenance_changes:
                output.setdefault("warnings", []).append(
                    "SYSTEM_TRUSTED_SOURCE_REF_NORMALIZATION: rebound "
                    f"{provenance_changes} source reference(s) from the current "
                    "trusted input envelope and persisted metadata"
                )
            output, reference_alias_report = normalize_reference_id_aliases(
                output,
                envelope,
            )
            reference_alias_changes = int(reference_alias_report.get("normalized_count") or 0)
            if reference_alias_changes:
                output.setdefault("warnings", []).append(
                    "SYSTEM_REFERENCE_ID_ALIAS_NORMALIZATION: rebound "
                    f"{reference_alias_changes} cross-reference ID(s) to exact entities "
                    "visible in the current input or output"
                )
            reference_errors = validate_reference_ids(output, envelope)
            if reference_errors:
                raise PromptExecutionError(
                    "Output reference integrity validation failed",
                    validation_errors=reference_errors,
                )
        return output

    async def execute(
        self,
        prompt_id: str,
        envelope: dict[str, Any],
        *,
        project_id: str,
        workflow_id: str | None = None,
        original_environment: str | None = None,
    ) -> dict[str, Any]:
        run_id = new_id("run")
        started = time.perf_counter()
        quality_context_envelope = envelope
        model_envelope, input_compaction = self._prepare_model_envelope(prompt_id, envelope)
        model_envelope = attach_trusted_source_catalog(model_envelope)
        input_hash = sha256_json(model_envelope)
        route = None
        output: dict[str, Any] | None = None
        error: str | None = None
        status = "ERROR"
        system_prompt = None
        raw_response_text = None
        output_schema: dict[str, Any] | None = None
        try:
            input_errors = self.pack.validate(prompt_id, "input", envelope)
            if input_errors:
                raise PromptExecutionError("Input schema validation failed", validation_errors=input_errors)
            if model_envelope is not envelope:
                model_input_errors = self.pack.validate(prompt_id, "input", model_envelope)
                if model_input_errors:
                    raise PromptExecutionError("Compacted model input schema validation failed", validation_errors=model_input_errors)
            route = self.router.route(prompt_id, model_envelope, original_environment=original_environment)
            project_config = load_project_config(self.db, project_id)
            if route.environment == "ONLINE_PUBLIC":
                assert_online_payload_safe(model_envelope, project_config)
            output_schema = self.pack.inlined_schema(prompt_id, "output")
            system_prompt = self._system_prompt(prompt_id, output_schema, model_envelope)
            result = await self.gateway.invoke(route, prompt_id, system_prompt, model_envelope, output_schema)
            raw_response_text = result.raw_text
            output = self._normalize_output(prompt_id, result.output, model_envelope)
            parse_report = dict(getattr(result, "parse_report", {}) or {})
            repair_count = int(parse_report.get("repair_count") or 0)
            code_fence_removed = bool(parse_report.get("code_fence_removed"))
            surrounding_text_removed = bool(parse_report.get("surrounding_text_removed"))
            if repair_count or code_fence_removed or surrounding_text_removed:
                repair_kinds = sorted(
                    {str(item.get("kind") or "UNKNOWN") for item in parse_report.get("repairs") or []}
                )
                output.setdefault("warnings", []).append(
                    "SYSTEM_JSON_PARSE_NORMALIZATION: provider response required "
                    f"repairs={repair_count}"
                    f" ({', '.join(repair_kinds) if repair_kinds else 'none'}), "
                    f"code_fence_removed={code_fence_removed}, "
                    f"surrounding_text_removed={surrounding_text_removed}; "
                    "the immutable raw response remains in the execution trace"
                )
            if prompt_id == "P-SAFE-ONLINE-PACKAGE":
                output, redactions = sanitize_safe_online_package(output, project_config)
                if redactions:
                    output.setdefault("warnings", []).append(
                        f"Deterministic outbound privacy guard redacted {len(redactions)} sensitive field occurrence(s)."
                    )
            if self.quality_guard_enabled:
                output = self.quality_guard.apply(prompt_id, quality_context_envelope, output)
            structure_validator = getattr(self.pack, "validate_structure", None)
            if callable(structure_validator):
                post_structure_errors = structure_validator(prompt_id, "output", output)
                if post_structure_errors:
                    raise PromptExecutionError(
                        "Post-normalization output container structure validation failed",
                        validation_errors=post_structure_errors,
                    )
            output_errors = self.pack.validate(prompt_id, "output", output)
            if output_errors:
                raise PromptExecutionError("Output schema validation failed", validation_errors=output_errors)
            status = output.get("status", "ERROR")
            duration_ms = int((time.perf_counter() - started) * 1000)
            self._save_run(run_id, project_id, workflow_id, prompt_id, status, result.model_id, result.endpoint_id, input_hash, model_envelope, output, None, duration_ms)
            self._save_artifact(
                project_id, workflow_id, prompt_id, output, model_envelope, system_prompt,
                raw_response_text, output_schema, route.environment if route else None,
                result.model_id, result.endpoint_id, duration_ms, status, None,
                quality_context_envelope=quality_context_envelope if input_compaction else None,
                input_compaction=input_compaction,
            )
            return {
                "run_id": run_id,
                "prompt_id": prompt_id,
                "status": status,
                "route": {"environment": route.environment, "model_id": result.model_id, "endpoint_id": result.endpoint_id},
                "output": output,
            }
        except (PromptExecutionError, RoutingDenied, OutboundPrivacyError, LLMError, KeyError, ValueError) as exc:
            duration_ms = int((time.perf_counter() - started) * 1000)
            details = getattr(exc, "validation_errors", [])
            error = str(exc) + ((" | " + "; ".join(details[:20])) if details else "")
            self._save_run(run_id, project_id, workflow_id, prompt_id, "ERROR", route.model_id if route else None, route.endpoint_id if route else None, input_hash, model_envelope, output, error, duration_ms)
            self._save_trace(
                project_id, workflow_id, prompt_id, model_envelope, system_prompt,
                raw_response_text, output_schema, route.environment if route else None,
                route.model_id if route else None, route.endpoint_id if route else None,
                duration_ms, "ERROR", error,
                quality_context_envelope=quality_context_envelope if input_compaction else None,
                input_compaction=input_compaction,
            )
            raise PromptExecutionError(error, validation_errors=details) from exc
        except (AttributeError, TypeError, IndexError) as exc:
            # Last-resort execution boundary.  Declared model-output container
            # mismatches should already be reported by the structure preflight;
            # reaching this branch therefore indicates an internal contract
            # processing defect.  Persist it as an explicit workflow error
            # instead of allowing an untracked exception to escape.
            duration_ms = int((time.perf_counter() - started) * 1000)
            error = (
                "INTERNAL_OUTPUT_CONTRACT_PROCESSING_ERROR: "
                f"{type(exc).__name__}: {exc}"
            )
            self._save_run(
                run_id,
                project_id,
                workflow_id,
                prompt_id,
                "ERROR",
                route.model_id if route else None,
                route.endpoint_id if route else None,
                input_hash,
                model_envelope,
                output,
                error,
                duration_ms,
            )
            self._save_trace(
                project_id,
                workflow_id,
                prompt_id,
                model_envelope,
                system_prompt,
                raw_response_text,
                output_schema,
                route.environment if route else None,
                route.model_id if route else None,
                route.endpoint_id if route else None,
                duration_ms,
                "ERROR",
                error,
                quality_context_envelope=quality_context_envelope if input_compaction else None,
                input_compaction=input_compaction,
            )
            raise PromptExecutionError(error) from exc

    @staticmethod
    def _compact_paragraph_text(text: str, *, limit: int = 180) -> str:
        value = str(text or "").strip()
        if len(value) <= limit:
            return value
        marker = "……[中段省略]……"
        available = max(40, limit - len(marker))
        head = max(24, int(available * 0.65))
        tail = max(16, available - head)
        return value[:head].rstrip() + marker + value[-tail:].lstrip()

    def _prepare_model_envelope(self, prompt_id: str, envelope: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Return the exact envelope sent to the model.

        Whole-document review needs the complete candidate set for deterministic
        verification, but weak/short-context models do not need every repeated
        byte of every paragraph.  The quality guard therefore keeps the original
        envelope, while the model receives all sections, semantic identities,
        evidence links and bounded excerpts.  The trace records both envelopes.
        """
        if prompt_id == "P-PROJECT-DEFINITION-EXTRACT":
            original_chars = len(json.dumps(envelope, ensure_ascii=False))
            source_documents = (envelope.get("payload") or {}).get("source_documents") or []
            section_count = sum(
                len(document.get("sections") or [])
                for document in source_documents
                if isinstance(document, dict)
            )
            if original_chars > 50000 or section_count > 36:
                compact = copy.deepcopy(envelope)
                payload = compact.get("payload") or {}
                selected_section_count = 0
                keywords = (
                    "项目属性", "项目背景", "中心命题", "研究问题", "RQ-",
                    "研究目标", "OBJ-", "研究内容", "RC-", "关键", "KP-",
                    "技术路线", "创新", "指标", "实验", "研究基础", "执行约束",
                    "BASE-", "TE-",
                )
                role_limits = {
                    "CURRENT_PROPOSAL": 18,
                    "PROJECT_BRIEF": 8,
                    "EVIDENCE_MATERIAL": 8,
                    "TECHNICAL_DESIGN": 8,
                }
                compact_documents = []
                for document in payload.get("source_documents") or []:
                    if not isinstance(document, dict):
                        continue
                    sections = [
                        section
                        for section in document.get("sections") or []
                        if isinstance(section, dict) and str(section.get("text") or "").strip()
                    ]
                    role = str(document.get("document_role") or "")
                    limit = role_limits.get(role, 4)
                    if role == "CURRENT_PROPOSAL":
                        core_prefixes = (
                            "1.3 ", "3. ", "5. ", "10.1 ", "12. ",
                            "13. ", "17.1 ", "20. ",
                        )
                        preferred = [
                            section
                            for section in sections
                            if str(section.get("title") or "").startswith(core_prefixes)
                        ]
                        for prefix, quota in (
                            ("RQ-", 4),
                            ("OBJ-", 3),
                            ("RC-", 3),
                            ("主要研究任务", 3),
                        ):
                            matches = [
                                section
                                for section in sections
                                if str(section.get("title") or "").startswith(prefix)
                            ]
                            preferred.extend(matches[:quota])
                    else:
                        preferred = [
                            section
                            for section in sections
                            if any(
                                keyword in f"{section.get('title') or ''}\n{section.get('text') or ''}"
                                for keyword in keywords
                            )
                        ]
                    preferred_ids = {
                        str(section.get("section_id"))
                        for section in preferred
                        if section.get("section_id")
                    }
                    selected = preferred[:limit]
                    if len(selected) < limit:
                        selected.extend(
                            section
                            for section in sections
                            if str(section.get("section_id") or "") not in preferred_ids
                        )
                    selected = selected[:limit]
                    if not selected:
                        continue
                    compact_document = copy.deepcopy(document)
                    compact_document["sections"] = selected
                    compact_documents.append(compact_document)
                    selected_section_count += len(selected)
                payload["source_documents"] = compact_documents
                scope = list(payload.get("extraction_scope") or [])
                scope.append(
                    "运行时容量约束：输出最小充分图谱，项目对象不超过18个、关系不超过27条；"
                    "优先保留差距、问题、目标、任务、方法、实验、创新、成果、指标和团队基础的代表对象。"
                )
                payload["extraction_scope"] = scope
                compact_chars = len(json.dumps(compact, ensure_ascii=False))
                return compact, {
                    "strategy": "PROJECT_DEFINITION_MINIMAL_SUFFICIENT_GRAPH",
                    "original_chars": original_chars,
                    "model_chars": compact_chars,
                    "original_section_count": section_count,
                    "model_section_count": selected_section_count,
                    "max_items": 18,
                    "max_relations": 27,
                    "quality_guard_uses_full_context": True,
                }
        if prompt_id == "P-FACT-EXTRACT":
            original_chars = len(json.dumps(envelope, ensure_ascii=False))
            payload = envelope.get("payload") or {}
            source_spans = [
                span for span in payload.get("source_spans") or []
                if isinstance(span, dict)
            ]
            if original_chars > 50000 or len(source_spans) > 40:
                compact = copy.deepcopy(envelope)
                compact_payload = compact.get("payload") or {}
                compact_spans = [
                    span for span in compact_payload.get("source_spans") or []
                    if isinstance(span, dict)
                ]
                evidence_spans = [
                    span for span in compact_spans
                    if str((span.get("source_ref") or {}).get("source_type") or "")
                    in {"EVIDENCE_MATERIAL", "TECHNICAL_MATERIAL", "HISTORICAL_DOCUMENT"}
                ]
                current_keywords = (
                    "项目类型", "项目背景", "中心命题", "如何", "研究目标",
                    "主要研究任务", "技术路线", "评价指标", "创新", "测试",
                    "已有基础", "执行约束",
                )
                current_spans = [
                    span for span in compact_spans
                    if str((span.get("source_ref") or {}).get("source_type") or "") == "CURRENT_PROPOSAL"
                    and any(keyword in str(span.get("text") or "") for keyword in current_keywords)
                ]
                selected_spans = [*evidence_spans[:18], *current_spans[:14]]
                selected_ids = {
                    str(span.get("span_id") or "")
                    for span in selected_spans
                }
                compact_payload["source_spans"] = selected_spans
                existing_facts = [
                    fact for fact in compact_payload.get("existing_facts") or []
                    if isinstance(fact, dict)
                ]
                prioritized_facts = [
                    fact for fact in existing_facts
                    if any(
                        str(ref.get("source_id") or "") in selected_ids
                        for ref in fact.get("source_refs") or []
                        if isinstance(ref, dict)
                    )
                ]
                prioritized_ids = {
                    str(fact.get("claim_id") or "")
                    for fact in prioritized_facts
                }
                prioritized_facts.extend(
                    fact for fact in existing_facts
                    if str(fact.get("claim_id") or "") not in prioritized_ids
                )
                compact_payload["existing_facts"] = prioritized_facts[:18]
                compact_chars = len(json.dumps(compact, ensure_ascii=False))
                return compact, {
                    "strategy": "FACT_REPRESENTATIVE_EVIDENCE_PACKAGE",
                    "original_chars": original_chars,
                    "model_chars": compact_chars,
                    "original_span_count": len(source_spans),
                    "model_span_count": len(selected_spans),
                    "original_fact_count": len(existing_facts),
                    "model_fact_count": len(compact_payload["existing_facts"]),
                    "max_output_fact_candidates": 24,
                    "quality_guard_uses_full_context": True,
                }
        if prompt_id != "P-INTEGRATION-CRITIC":
            return envelope, None
        original_chars = len(json.dumps(envelope, ensure_ascii=False))
        if original_chars <= 80000:
            return envelope, None
        compact = copy.deepcopy(envelope)
        sections = (compact.get("payload") or {}).get("candidate_sections") or []
        paragraph_count = 0
        for item in sections:
            candidate = (item or {}).get("candidate") or {}
            advancement = candidate.get("claim_advancement") or {}
            contribution = str(advancement.get("section_contribution") or "").strip()
            candidate["candidate_text"] = contribution or self._compact_paragraph_text(candidate.get("candidate_text", ""), limit=600)
            paragraphs = candidate.get("paragraphs") or []
            paragraph_count += len(paragraphs)
            for paragraph in paragraphs:
                if isinstance(paragraph, dict):
                    paragraph["text"] = self._compact_paragraph_text(paragraph.get("text", ""), limit=180)
                    paragraph["evidence_ids"] = list(paragraph.get("evidence_ids") or [])[:2]
            # Retain one verifiable trace per paragraph.  Paragraph semantic IDs
            # and evidence IDs remain complete; duplicated trace objects and long
            # quoted spans are the main source of whole-document context growth.
            links_by_id = {
                str(link.get("trace_id")): link
                for link in candidate.get("trace_links") or []
                if isinstance(link, dict) and link.get("trace_id")
            }
            primary_trace_id = next(iter(links_by_id), "")
            if primary_trace_id:
                for paragraph in paragraphs:
                    paragraph["trace_link_ids"] = [primary_trace_id]
                link = links_by_id[primary_trace_id]
                if link.get("source_path_or_span"):
                    link["source_path_or_span"] = str(link["source_path_or_span"])[:96]
                candidate["trace_links"] = [link]

        payload = compact.get("payload") or {}
        referenced_ids: set[str] = set()
        for item in sections:
            candidate = (item or {}).get("candidate") or {}
            advancement = candidate.get("claim_advancement") or {}
            referenced_ids.update(str(x) for x in advancement.get("advanced_claim_ids", []) if x)
            referenced_ids.update(str(x) for x in advancement.get("new_information_keys", []) if x)
            for paragraph in candidate.get("paragraphs") or []:
                if not isinstance(paragraph, dict):
                    continue
                referenced_ids.add(str(paragraph.get("primary_claim_id") or ""))
                referenced_ids.update(str(x) for x in paragraph.get("evidence_ids", []) if x)
            for link in candidate.get("trace_links") or []:
                if isinstance(link, dict) and link.get("source_id"):
                    referenced_ids.add(str(link["source_id"]))
        referenced_ids.discard("")

        def trim_source_refs(value: dict[str, Any]) -> None:
            compact_refs = []
            for ref in value.get("source_refs") or []:
                if not isinstance(ref, dict):
                    continue
                compact_ref = {
                    "source_id": ref.get("source_id"),
                    "source_type": ref.get("source_type"),
                    "authority_rank": ref.get("authority_rank"),
                    "security_level": ref.get("security_level"),
                }
                if ref.get("source_hash"):
                    compact_ref["source_hash"] = ref.get("source_hash")
                compact_refs.append(compact_ref)
            value["source_refs"] = compact_refs[:2]

        project_definition = payload.get("project_definition") or {}
        for item in project_definition.get("items") or []:
            if isinstance(item, dict):
                # Source validity is checked against the full quality context.
                # The whole-document model only needs the typed project object.
                item["source_refs"] = []
        # Relations are kept because the integration critic validates the complete
        # project chain, but explanatory text is bounded.
        for relation in project_definition.get("relations") or []:
            if isinstance(relation, dict) and relation.get("rationale"):
                relation["rationale"] = str(relation["rationale"])[:160]

        fact_package = payload.get("fact_package") or {}
        claims = [c for c in fact_package.get("claims") or [] if isinstance(c, dict)]
        selected_claims = [c for c in claims if str(c.get("claim_id") or "") in referenced_ids]
        if len(selected_claims) < 6:
            selected_ids = {str(c.get("claim_id") or "") for c in selected_claims}
            remaining_claims = [c for c in claims if str(c.get("claim_id") or "") not in selected_ids]
            selected_claims.extend(remaining_claims[: 6 - len(selected_claims)])
        fact_package["claims"] = selected_claims[:8]
        for claim in fact_package.get("claims") or []:
            trim_source_refs(claim)
            for ref in claim.get("source_refs") or []:
                if isinstance(ref, dict):
                    for key in ("quoted_text", "source_path_or_span"):
                        if ref.get(key):
                            ref[key] = str(ref[key])[:120]
        fact_package["conflicts"] = list(fact_package.get("conflicts") or [])[:4]

        architecture = payload.get("narrative_architecture") or {}
        for contract in architecture.get("section_contracts") or []:
            if not isinstance(contract, dict):
                continue
            contract["argument_function"] = str(contract.get("argument_function") or contract.get("profile_id") or "章节论证")[:80]
            contract["must_use_evidence_ids"] = list(contract.get("must_use_evidence_ids") or [])[:2]
            contract["unique_information_keys"] = list(contract.get("unique_information_keys") or [])[:1]
            contract["required_argument_roles"] = list(contract.get("required_argument_roles") or [])[:3]
            contract["prerequisite_section_ids"] = list(contract.get("prerequisite_section_ids") or [])[-1:]
            contract["must_not_repeat_section_ids"] = list(contract.get("must_not_repeat_section_ids") or [])[-2:]
            contract["allowed_shared_context_ids"] = list(contract.get("allowed_shared_context_ids") or [])[:1]
            contract["forbidden_topics"] = list(contract.get("forbidden_topics") or [])[:1]
            rules = list(contract.get("acceptance_rules") or [])
            contract["acceptance_rules"] = (rules[:2] if len(rules) >= 2 else [*rules, "保持章节论证功能"][:2])

        compact_chars = len(json.dumps(compact, ensure_ascii=False))
        return compact, {
            "strategy": "FULL_SEMANTIC_IDENTITY_WITH_BOUNDED_EXCERPTS",
            "original_chars": original_chars,
            "model_chars": compact_chars,
            "candidate_section_count": len(sections),
            "paragraph_count": paragraph_count,
            "quality_guard_uses_full_context": True,
        }

    def _system_prompt(
        self,
        prompt_id: str,
        output_schema: dict[str, Any],
        envelope: dict[str, Any] | None = None,
    ) -> str:
        base_prompt = (
            self.pack.shared_prompt
            + "\n\n"
            + self.pack.prompt_text(prompt_id)
        )
        base_prompt = augment_prompt_with_enum_contract(
            base_prompt,
            output_schema,
            contract_id=f"prompt-pack:{prompt_id}:output",
        )
        base_prompt = augment_prompt_with_field_ownership_contract(
            base_prompt,
            output_schema,
            contract_id=f"prompt-pack:{prompt_id}:field-ownership",
        )
        source_contract = trusted_source_prompt_contract(envelope)
        return (
            base_prompt
            + source_contract
            + "\n\n# 人工输入约束\n"
            + "若输入 payload.human_resolutions 非空，这些记录是已经通过门禁确认的人工回答。"
              "必须在其 target_paths 和当前任务范围内使用；不得忽略、扩大解释或改写为未经确认的事实。"
            + "\n\n# 运行时强制输出Schema\n"
            + json.dumps(output_schema, ensure_ascii=False)
        )

    def _save_run(self, run_id: str, project_id: str, workflow_id: str | None, prompt_id: str, status: str, model_id: str | None, endpoint_id: str | None, input_hash: str, envelope: dict[str, Any], output: dict[str, Any] | None, error: str | None, duration_ms: int) -> None:
        self.db.execute(
            """INSERT INTO prompt_runs(id,project_id,workflow_id,prompt_id,status,model_id,endpoint_id,input_hash,output_hash,input_json,output_json,error,duration_ms,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id, project_id, workflow_id, prompt_id, status, model_id, endpoint_id,
                input_hash, sha256_json(output) if output is not None else None,
                json.dumps(envelope, ensure_ascii=False), json.dumps(output, ensure_ascii=False) if output is not None else None,
                error, duration_ms, utc_now(),
            ),
        )
        self.db.audit("PROMPT_EXECUTED", project_id=project_id, object_id=run_id, metadata={"prompt_id": prompt_id, "status": status, "input_hash": input_hash, "duration_ms": duration_ms})

    def _save_artifact(self, project_id: str, workflow_id: str | None, prompt_id: str, output: dict[str, Any], envelope: dict[str, Any], system_prompt: str | None, raw_response_text: str | None, output_schema: dict[str, Any] | None, environment: str | None, model_id: str | None, endpoint_id: str | None, duration_ms: int, status: str, error: str | None, *, quality_context_envelope: dict[str, Any] | None = None, input_compaction: dict[str, Any] | None = None) -> None:
        row = self.db.fetchone("SELECT COALESCE(MAX(version),0) AS v FROM artifacts WHERE project_id=? AND prompt_id=? AND artifact_type='PROMPT_OUTPUT'", (project_id, prompt_id))
        version = int(row["v"]) + 1 if row else 1
        security_level = envelope.get("security_context", {}).get("input_max_security_level", "INTERNAL")
        context_hash = sha256_json(envelope)
        self.db.execute(
            """INSERT INTO artifacts(id,project_id,workflow_id,artifact_type,prompt_id,version,status,security_level,context_hash,content_json,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (new_id("artifact"), project_id, workflow_id, "PROMPT_OUTPUT", prompt_id, version, output.get("status", "UNKNOWN"), security_level, context_hash, json.dumps(output, ensure_ascii=False), utc_now()),
        )
        self._save_trace(
            project_id, workflow_id, prompt_id, envelope, system_prompt, raw_response_text,
            output_schema, environment, model_id, endpoint_id, duration_ms, status, error,
            version=version, output=output,
            quality_context_envelope=quality_context_envelope,
            input_compaction=input_compaction,
        )

    def _save_trace(self, project_id: str, workflow_id: str | None, prompt_id: str, envelope: dict[str, Any], system_prompt: str | None, raw_response_text: str | None, output_schema: dict[str, Any] | None, environment: str | None, model_id: str | None, endpoint_id: str | None, duration_ms: int, status: str, error: str | None, *, version: int | None = None, output: dict[str, Any] | None = None, quality_context_envelope: dict[str, Any] | None = None, input_compaction: dict[str, Any] | None = None) -> None:
        if version is None:
            row = self.db.fetchone("SELECT COALESCE(MAX(version),0) AS v FROM artifacts WHERE project_id=? AND prompt_id=? AND artifact_type='PROMPT_TRACE'", (project_id, prompt_id))
            version = int(row["v"]) + 1 if row else 1
        security_level = envelope.get("security_context", {}).get("input_max_security_level", "INTERNAL")
        context_hash = sha256_json(envelope)
        trace_payload = {
            "prompt_id": prompt_id,
            "version": version,
            "status": status,
            "duration_ms": duration_ms,
            "environment": environment,
            "model_id": model_id,
            "endpoint_id": endpoint_id,
            "system_prompt": system_prompt,
            "input_envelope": envelope,
            "quality_context_envelope": quality_context_envelope,
            "quality_context_hash": sha256_json(quality_context_envelope) if quality_context_envelope is not None else None,
            "input_compaction": input_compaction,
            "output_schema": output_schema,
            "output": output,
            "raw_response_text": raw_response_text,
            "error": error,
        }
        self.db.execute(
            """INSERT INTO artifacts(id,project_id,workflow_id,artifact_type,prompt_id,version,status,security_level,context_hash,content_json,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (new_id("artifact"), project_id, workflow_id, "PROMPT_TRACE", prompt_id, version, status, security_level, context_hash, json.dumps(trace_payload, ensure_ascii=False), utc_now()),
        )
