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
    normalize_registered_enum_aliases_against_schema,
    repair_field_ownership_against_schema,
    required_null_container_errors,
    synchronize_required_mirrored_arrays,
)
from .privacy import OutboundPrivacyError, assert_online_payload_safe, load_project_config, sanitize_safe_online_package
from .contracts import get_semantic_contract
from .output_integrity import (
    TRUSTED_SOURCE_CATALOG_VERSION,
    attach_trusted_source_catalog,
    bind_trusted_source_refs,
    normalize_reference_id_aliases,
    trusted_source_prompt_contract,
    validate_reference_ids,
)
from .proposal_quality import ProposalQualityGuard, SECTION_FUNCTION_ROLE_ALIASES
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
OUTPUT_NORMALIZER_VERSION = "2026-07-31.v43-colon-reference-path-aliases"


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
    def _observe_guard(
        self,
        prompt_id: str,
        envelope: dict[str, Any],
        output: dict[str, Any],
    ) -> dict[str, Any] | None:
        if not self.quality_guard_enabled:
            return None
        observer = getattr(self.quality_guard, "observe", None)
        if not callable(observer):
            raise PromptExecutionError(
                f"Quality guard {type(self.quality_guard).__name__} does not expose the non-mutating observe contract"
            )
        snapshot = copy.deepcopy(output)
        report = observer(prompt_id, envelope, snapshot)
        if snapshot != output:
            raise PromptExecutionError(
                "Deterministic guard mutated the model output while producing guard_report"
            )
        if not isinstance(report, dict):
            raise PromptExecutionError("Deterministic guard did not return an object guard_report")
        return copy.deepcopy(report)

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
        """Normalize aliases within their declared semantic field only.

        This is representation normalization: it never infers one field from
        another and never upgrades knowledge, claim, or temporal semantics.
        Unknown values remain untouched for strict schema/contract rejection.
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

            if "knowledge_status" in node:
                decision = normalize_knowledge_status(
                    node.get("knowledge_status"),
                    source_refs=node.get("source_refs"),
                )
                if decision.normalized:
                    node["knowledge_status"] = decision.canonical_status
                    changes.append(
                        f"{path or '/'} /knowledge_status: "
                        f"{decision.original_status}->{decision.canonical_status}"
                    )
            if "claim_type" in node:
                decision = normalize_claim_type(node.get("claim_type"))
                if decision.normalized:
                    node["claim_type"] = decision.canonical_value
                    changes.append(
                        f"{path or '/'} /claim_type: "
                        f"{decision.original_value}->{decision.canonical_value}"
                    )
            if "temporal_status" in node:
                decision = normalize_temporal_status(node.get("temporal_status"))
                if decision.normalized:
                    node["temporal_status"] = decision.canonical_value
                    changes.append(
                        f"{path or '/'} /temporal_status: "
                        f"{decision.original_value}->{decision.canonical_value}"
                    )
            for key, value in list(node.items()):
                visit(value, f"{path}/{key}")

        visit(normalized, "")
        if changes:
            normalized.setdefault("warnings", []).append(
                "SYSTEM_REPRESENTATION_ENUM_NORMALIZATION: " + "; ".join(changes[:20])
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
        """Apply representation-only normalization and deterministic validation.

        The method may repair protocol representation (registered enum aliases,
        authoritative envelope constants, unambiguous field ownership, and exact
        trusted-reference aliases).  It must not create, delete, or rewrite any
        business entity, claim, paragraph, finding, verdict, status, slot, key,
        budget, graph node, or graph edge.
        """
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
            raise PromptExecutionError(
                "Output container structure validation failed",
                validation_errors=[f"/: expected object, received {type(output).__name__}"],
            )

        schema_reader = getattr(self.pack, "inlined_schema", None)
        if not callable(schema_reader):
            schema_reader = getattr(self.pack, "schema", None)
        output_schema = schema_reader(prompt_id, "output") if callable(schema_reader) else {}

        required_null_errors = required_null_container_errors(output, output_schema)
        if required_null_errors:
            raise PromptExecutionError(
                "Required output container is null",
                validation_errors=required_null_errors,
            )

        normalized = copy.deepcopy(output)
        normalized, ownership_report = repair_field_ownership_against_schema(
            normalized,
            output_schema,
            contract_id=f"prompt-pack:{prompt_id}:field-ownership",
        )
        invalid_misplacements = list(ownership_report.get("invalid_misplacements") or [])
        if invalid_misplacements:
            validation_errors = []
            for item in invalid_misplacements:
                details = "; ".join(str(value) for value in item.get("validation_errors") or [])
                validation_errors.append(
                    f"{item.get('source_path')}: misplaced field {item.get('field')!r} "
                    f"belongs at {item.get('target_path')} but has an invalid value"
                    + (f" ({details})" if details else "")
                )
            raise PromptExecutionError(
                "Misplaced response-envelope field has an invalid value",
                validation_errors=validation_errors,
            )

        ownership_changes = list(ownership_report.get("changes") or [])
        if ownership_changes:
            normalized.setdefault("warnings", []).append(
                "SYSTEM_FIELD_OWNERSHIP_NORMALIZATION: "
                + "; ".join(
                    f"{item.get('source_path')}/{item.get('field')}"
                    f"->{item.get('target_path')}/{item.get('field')}"
                    for item in ownership_changes[:12]
                )
            )

        normalized, mirror_report = synchronize_required_mirrored_arrays(
            normalized,
            output_schema,
            contract_id=f"prompt-pack:{prompt_id}:required-mirrors",
        )
        if mirror_report.get("synchronized_count"):
            normalized.setdefault("warnings", []).append(
                "SYSTEM_REQUIRED_MIRROR_SYNCHRONIZATION: "
                + ", ".join(
                    str(item.get("field"))
                    for item in mirror_report.get("changes") or []
                )
            )

        protocol_changes: list[str] = []
        for field in ("schema_version", "prompt_id", "prompt_version"):
            expected = ((output_schema.get("properties") or {}).get(field) or {}).get("const")
            if expected is not None and normalized.get(field) != expected:
                normalized[field] = expected
                protocol_changes.append(field)
        if protocol_changes:
            normalized.setdefault("warnings", []).append(
                "SYSTEM_PROTOCOL_CONSTANT_NORMALIZATION: " + ", ".join(protocol_changes)
            )

        normalized = self._normalize_semantic_enum_tree(normalized)
        normalized, enum_alias_report = normalize_registered_enum_aliases_against_schema(
            normalized,
            output_schema,
            contract_id=f"prompt-pack:{prompt_id}:registered-enum-aliases",
            protected_paths=(
                "$/status",
                "$/findings",
                "$/user_questions",
                "$/unresolved_items",
                "$/result/verdict",
            ),
        )
        if enum_alias_report.get("normalized_count"):
            normalized.setdefault("warnings", []).append(
                "SYSTEM_REGISTERED_ENUM_ALIAS_NORMALIZATION: "
                + "; ".join(
                    f"{item.get('path')}:{item.get('before')}->{item.get('after')}"
                    for item in (enum_alias_report.get("changes") or [])[:20]
                )
            )

        removed_instance_schema_keywords = 0
        source_alias_changes = 0
        trace_alias_changes = 0

        def normalize_representation(node: Any) -> None:
            nonlocal removed_instance_schema_keywords, source_alias_changes, trace_alias_changes
            if isinstance(node, list):
                for item in node:
                    normalize_representation(item)
                return
            if not isinstance(node, dict):
                return
            if isinstance(node.get("additionalProperties"), bool):
                node.pop("additionalProperties")
                removed_instance_schema_keywords += 1
            if "source_type" in node:
                mapped = _schema_source_type(node.get("source_type"))
                if mapped != node.get("source_type"):
                    node["source_type"] = mapped
                    source_alias_changes += 1
            if "source_kind" in node:
                mapped = TRACE_SOURCE_KIND_ALIASES.get(str(node.get("source_kind")), node.get("source_kind"))
                if mapped != node.get("source_kind"):
                    node["source_kind"] = mapped
                    trace_alias_changes += 1
            for value in list(node.values()):
                normalize_representation(value)

        normalize_representation(normalized)
        if removed_instance_schema_keywords:
            normalized.setdefault("warnings", []).append(
                f"SYSTEM_INSTANCE_SCHEMA_KEYWORD_REMOVAL: {removed_instance_schema_keywords}"
            )
        if source_alias_changes or trace_alias_changes:
            normalized.setdefault("warnings", []).append(
                "SYSTEM_SOURCE_ENUM_ALIAS_NORMALIZATION: "
                f"source_type={source_alias_changes}, source_kind={trace_alias_changes}"
            )

        if prompt_id == "P-SAFE-ONLINE-PACKAGE" and envelope:
            safe_package_changes = self._normalize_safe_package_source_refs(normalized, envelope)
            if safe_package_changes:
                normalized.setdefault("warnings", []).append(
                    "SYSTEM_SAFE_PACKAGE_SOURCE_NORMALIZATION: "
                    f"{safe_package_changes}"
                )

        if envelope:
            normalized, alias_report = normalize_reference_id_aliases(normalized, envelope)
            if alias_report.get("normalized_count"):
                normalized.setdefault("warnings", []).append(
                    "SYSTEM_REFERENCE_ID_ALIAS_NORMALIZATION: "
                    f"{alias_report.get('normalized_count')}"
                )
            normalized, provenance_report = bind_trusted_source_refs(
                normalized, envelope, db=getattr(self, "db", None)
            )
            provenance_errors = list(provenance_report.get("errors") or [])
            if provenance_errors:
                raise PromptExecutionError(
                    "Output provenance is not backed by the trusted input envelope",
                    validation_errors=provenance_errors,
                )
            if provenance_report.get("normalized_count"):
                normalized.setdefault("warnings", []).append(
                    "SYSTEM_TRUSTED_SOURCE_REF_NORMALIZATION: "
                    f"{provenance_report.get('normalized_count')}"
                )
            reference_errors = validate_reference_ids(normalized, envelope)
            if reference_errors:
                raise PromptExecutionError(
                    "Output reference integrity validation failed",
                    validation_errors=reference_errors,
                )
        return normalized

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
        guard_report: dict[str, Any] | None = None
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
            guard_report = self._observe_guard(
                prompt_id, quality_context_envelope, output
            )
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
                "guard_report": guard_report,
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
                    "项目属性", "项目背景", "中心命题", "研究问题",
                    "研究目标", "研究内容", "关键问题", "技术路线",
                    "创新", "指标", "实验", "研究基础", "执行约束",
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
                            ("研究问题", 4),
                            ("研究目标", 3),
                            ("研究内容", 3),
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
        semantic_contract = get_semantic_contract()
        base_prompt = semantic_contract.inject_into_prompt(base_prompt)
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
