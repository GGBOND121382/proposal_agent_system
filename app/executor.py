from __future__ import annotations

import copy
import json
import re
import time
from typing import Any

from .llm import LLMError, ModelGateway, ProviderError
from .background_research import WF3B_RUNTIME_OWNED_SOURCE_REFS_PROMPTS
from .model_semantic_contracts import (
    SEMANTIC_MODEL_CONTRACT_VERSION,
    apply_semantic_model_output_defaults,
    apply_wf1_survey_intake_defaults,
    build_semantic_model_input,
    expand_semantic_model_output,
    semantic_model_reference_errors,
    supports_semantic_model_contract,
)
from .json_pointer import is_ancestor_or_same, join_pointer, paths_overlap
from .gate_answer_contract import widen_gate_questions
from .contract_registry import (
    normalize_registered_enum_aliases_against_schema,
    normalize_exact_null_literals,
    repair_field_ownership_against_schema,
    required_null_container_errors,
    synchronize_required_mirrored_arrays,
)
from .privacy import OutboundPrivacyError, assert_online_payload_safe, load_project_config, sanitize_safe_online_package
from .contracts import ReferenceSemantic, get_semantic_contract
from .output_integrity import (
    TRUSTED_SOURCE_CATALOG_VERSION,
    attach_trusted_source_catalog,
    bind_trusted_source_refs,
    canonicalize_protocol_refs,
    drop_finding_self_reference_evidence,
    normalize_reference_id_aliases,
    rebuild_scheme_extraction_coverage,
    scrub_code_owned_fields_for_repair_diff,
    validate_reference_ids,
)
from .proposal_quality import ProposalQualityGuard, SECTION_FUNCTION_ROLE_ALIASES
from .runtime_failures import ProviderFailureKind
from .secret_redaction import redact_secret_text
from .quality_guard import (
    QualityGuardContractError,
    QualityGuardObserver,
    disabled_guard_report,
    ensure_quality_guard_observer,
    observe_guard,
)
from .security import RoutingDenied, SecurityRouter
from .status_ontology import (
    implied_temporal_status_from_claim_alias,
    normalize_claim_type,
    normalize_knowledge_status,
    normalize_temporal_status,
)
from .util import new_id, sha256_json, utc_now
from .wf3_contracts import (
    canonicalize_wf3_critic_control,
    canonicalize_wf3_machine_fields,
    canonicalize_wf3_producer_status,
    compact_wf3_research_envelope,
    enforce_wf3_pre_model_guards,
    wf3_output_semantic_errors,
    wf3_provider_request_budget_report,
)


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
OUTPUT_NORMALIZER_VERSION = "2026-09-10.v60-manual-document-type"
MODEL_CONTEXT_PROJECTION_VERSION = "2026-09-04.v5-wf3b-claim-bound-import-sources"
MODEL_SYSTEM_PROMPT_VERSION = "2026-08-13.v4-wf3-contract-retry-feedback"

_PROVIDER_SOURCE_REF_OMIT_FIELDS = frozenset({
    "document_version_id",
    "span_start",
    "span_end",
    "source_hash",
})


def _schema_source_type(value: Any) -> Any:
    return SOURCE_TYPE_ALIASES.get(str(value), value)


class PromptExecutionError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        validation_errors: list[str] | None = None,
        run_id: str | None = None,
    ):
        super().__init__(message)
        self.validation_errors = validation_errors or []
        self.run_id = run_id


class PromptExecutor:
    def __init__(
        self,
        db,
        pack,
        router: SecurityRouter,
        gateway: ModelGateway,
        *,
        quality_guard: QualityGuardObserver | None = None,
        quality_guard_enabled: bool = True,
    ):
        self.db = db
        self.pack = pack
        self.router = router
        self.gateway = gateway
        self.quality_guard: QualityGuardObserver = quality_guard or ProposalQualityGuard()
        self.quality_guard_enabled = bool(quality_guard_enabled)
        if self.quality_guard_enabled:
            try:
                ensure_quality_guard_observer(self.quality_guard)
            except QualityGuardContractError as exc:
                raise PromptExecutionError(str(exc)) from exc

    @staticmethod
    def _contract_retry_feedback_prompt(validation_errors: list[str]) -> str:
        """Render bounded validation feedback without replaying a bad candidate."""

        errors = [
            redact_secret_text(str(item)).strip()[:500]
            for item in validation_errors
            if str(item).strip()
        ]
        errors = list(dict.fromkeys(errors))[:12]
        if not errors:
            return ""
        rendered = "\n".join(f"- {item}" for item in errors)
        return (
            "\n\n# 上一轮输出的精确契约错误\n"
            "上一轮候选已被拒绝，不是可复用基线。请重新返回完整对象，并只修正下列错误；"
            "不要复制上一轮候选，也不要删除与错误无关的业务内容。\n"
            f"{rendered}"
        )

    @staticmethod
    def _provider_contract_failure(
        message: str,
        raw_response_text: str | None,
        *,
        phase: str,
        validation_errors: list[str] | None = None,
    ) -> ProviderError:
        """Type a provider-authored business-object contract failure.

        The response remains immutable.  The typed failure lets the workflow
        request a bounded new whole object with a fresh attempt identity rather
        than blocking as if an internal workflow invariant had failed.
        """

        return ProviderError(
            message,
            kind=ProviderFailureKind.RESPONSE_SHAPE,
            phase=phase,
            response_excerpt=str(raw_response_text or "")[:1000],
            retryable_hint=False,
            validation_errors=validation_errors,
        )

    def _observe_guard(
        self,
        prompt_id: str,
        envelope: dict[str, Any],
        output: dict[str, Any],
    ) -> dict[str, Any]:
        if not self.quality_guard_enabled:
            return disabled_guard_report(prompt_id, output)
        try:
            return observe_guard(self.quality_guard, prompt_id, envelope, output)
        except QualityGuardContractError as exc:
            raise PromptExecutionError(str(exc)) from exc

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

    @staticmethod
    def _normalize_human_gate_status(output: dict[str, Any]) -> dict[str, Any]:
        """Derive ``NEED_USER_INPUT`` only from explicit model-authored questions.

        Every prompt shares the same status vocabulary.  A blocking, directly
        answerable ``user_question`` is therefore a workflow fact rather than a
        prompt-specific convention: PASS/REVISE cannot advance while that question
        is open.  Findings alone are deliberately insufficient because converting
        a Finding into a human question would invent business content.
        """

        normalized = copy.deepcopy(output)
        if str(normalized.get("status") or "").upper() not in {"PASS", "REVISE"}:
            return normalized
        if any(
            isinstance(item, dict) and bool(item.get("blocking"))
            for item in normalized.get("user_questions") or []
        ):
            normalized["status"] = "NEED_USER_INPUT"
        return normalized

    @staticmethod
    def _human_gate_contract_errors(output: dict[str, Any]) -> list[str]:
        """Return contradictions between USER-routed blockers and gate payload."""

        blocking_questions = [
            item
            for item in output.get("user_questions") or []
            if isinstance(item, dict) and bool(item.get("blocking"))
        ]
        errors: list[str] = []
        if str(output.get("status") or "").upper() == "NEED_USER_INPUT" and not blocking_questions:
            errors.append(
                "/status: NEED_USER_INPUT requires at least one blocking, directly answerable "
                "user_question; runtime will not create an empty human Gate"
            )
        for index, finding in enumerate(output.get("findings") or []):
            if not isinstance(finding, dict):
                continue
            if not bool(finding.get("blocking")):
                continue
            if str(finding.get("suggested_route") or "").upper() != "USER":
                continue
            if bool(finding.get("repairable")):
                errors.append(
                    f"/findings/{index}/repairable: blocking USER-routed Finding "
                    "cannot be marked repairable by an automated producer"
                )
            if not blocking_questions:
                errors.append(
                    f"/findings/{index}: blocking USER-routed Finding requires at "
                    "least one blocking, directly answerable user_question; runtime "
                    "will not invent that question"
                )
        return errors

    def _normalize_protocol_reference_values(
        self,
        prompt_id: str,
        output: dict[str, Any],
        envelope: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], list[str]]:
        """Bind protocol-owned scalar identifiers to authoritative context values.

        This is intentionally narrower than business reference normalization.
        At present only ``project_id`` is input-owned: whenever an output schema
        exposes it, the value must mirror ``scope.project_id`` from the current
        envelope. Section ``profile_id`` values are validated separately against
        the prompt pack registry because choosing a profile is business logic.
        """
        normalized = copy.deepcopy(output)
        expected_project_id = str(
            (((envelope or {}).get("scope") or {}).get("project_id") or "")
            if isinstance((envelope or {}).get("scope"), dict)
            else ""
        ).strip()
        if not expected_project_id:
            return normalized, []

        contract = get_semantic_contract()
        changes: list[str] = []

        def visit(node: Any, path: tuple[Any, ...]) -> None:
            if isinstance(node, list):
                for index, item in enumerate(node):
                    visit(item, (*path, index))
                return
            if not isinstance(node, dict):
                return
            for key, value in list(node.items()):
                current = (*path, key)
                semantic = contract.field_semantic(
                    key,
                    prompt_id=prompt_id,
                    path=current,
                )
                if (
                    semantic is ReferenceSemantic.PROTOCOL_REF
                    and key == "project_id"
                    and isinstance(value, str)
                    and value != expected_project_id
                ):
                    node[key] = expected_project_id
                    changes.append(
                        f"{'/'.join(str(part) for part in current)}:"
                        f"{value}->{expected_project_id}"
                    )
                    value = expected_project_id
                visit(value, current)

        visit(normalized, ())
        return normalized, changes

    def _protocol_reference_contract_errors(
        self,
        prompt_id: str,
        output: dict[str, Any],
        envelope: dict[str, Any] | None,
    ) -> list[str]:
        """Validate scalar protocol references against authoritative registries."""

        contract = get_semantic_contract()
        scope = (envelope or {}).get("scope")
        expected_project_id = (
            str(scope.get("project_id") or "").strip()
            if isinstance(scope, dict)
            else ""
        )
        section_profiles = getattr(self.pack, "section_profiles", {}) or {}
        allowed_profile_ids = {
            str(item.get("profile_id") or "").strip()
            for item in section_profiles.get("profiles") or []
            if isinstance(item, dict) and str(item.get("profile_id") or "").strip()
        }
        default_profile = section_profiles.get("default_profile")
        if isinstance(default_profile, dict) and str(default_profile.get("profile_id") or "").strip():
            allowed_profile_ids.add(str(default_profile["profile_id"]).strip())

        errors: list[str] = []

        def pointer(path: tuple[Any, ...]) -> str:
            return "/" + "/".join(str(part) for part in path)

        def visit(node: Any, path: tuple[Any, ...]) -> None:
            if isinstance(node, list):
                for index, item in enumerate(node):
                    visit(item, (*path, index))
                return
            if not isinstance(node, dict):
                return
            for key, value in node.items():
                current = (*path, key)
                semantic = contract.field_semantic(
                    key,
                    prompt_id=prompt_id,
                    path=current,
                )
                if semantic is ReferenceSemantic.PROTOCOL_REF and isinstance(value, str):
                    if key == "project_id" and expected_project_id and value != expected_project_id:
                        errors.append(
                            f"{pointer(current)}: project_id {value!r} does not match "
                            f"authoritative scope.project_id {expected_project_id!r}"
                        )
                    elif (
                        key == "profile_id"
                        and allowed_profile_ids
                        and value not in allowed_profile_ids
                    ):
                        errors.append(
                            f"{pointer(current)}: profile_id {value!r} is not registered "
                            "in prompt_pack/knowledge/section_profiles.yaml"
                        )
                visit(value, current)

        visit(output, ())
        return errors

    @staticmethod
    def _normalize_wf3b_synthesis_representation(
        output: dict[str, Any],
    ) -> list[str]:
        """Normalize two unambiguous WF-3B response representation errors."""

        profile_aliases = {
            "APPLICATION_SCENARIO": "PROJECT_OVERVIEW",
            "STAKEHOLDER_AND_PAIN": "NEED_ANALYSIS",
            "INDUSTRY_SCALE_AND_TREND": "BACKGROUND_AND_SIGNIFICANCE",
            "POLICY_STANDARD_AND_PROGRAM": "BACKGROUND_AND_SIGNIFICANCE",
            "REPRESENTATIVE_CASE": "LITERATURE_REVIEW",
            "CURRENT_ADOPTION": "LITERATURE_REVIEW",
            "OPERATIONAL_CONSTRAINT": "KEY_ISSUE",
            "RESEARCH_SIGNIFICANCE": "BACKGROUND_AND_SIGNIFICANCE",
            # Survey-report technical dimensions: map onto registered section
            # profiles so dimension labels used as profile hints stay valid.
            "OBJECT_AND_EVOLUTION": "PROJECT_OVERVIEW",
            "FUNCTION_AND_ARCHITECTURE": "TECHNICAL_ROUTE",
            "WORKFLOW_AND_INTERACTION": "METHOD_AND_ALGORITHM",
            "TECHNOLOGY_AND_IMPLEMENTATION": "METHOD_AND_ALGORITHM",
            "EVALUATION_AND_EFFECT": "EVALUATION",
            "LIMITATIONS_AND_GAPS": "KEY_ISSUE",
        }
        changes: list[str] = []
        result = output.get("result") if isinstance(output.get("result"), dict) else {}
        for claim_index, claim in enumerate(result.get("claims") or []):
            if not isinstance(claim, dict):
                continue
            # The schema constrains subject_id to a slug, but the synthesis
            # prompt only shows that pattern inside the embedded schema dump,
            # so the model keeps answering with free text ("DASH experiment").
            # Slugify deterministically instead of burning the retry budget.
            subject_id = claim.get("subject_id")
            if isinstance(subject_id, str):
                slug = re.sub(r"[^A-Za-z0-9._:-]+", "_", subject_id.strip())
                slug = re.sub(r"^[^A-Za-z0-9]+", "", slug)[:128]
                normalized_subject = slug or None
                if normalized_subject != subject_id:
                    changes.append(
                        f"/result/claims/{claim_index}/subject_id:"
                        f"{subject_id!r}->{normalized_subject!r}"
                    )
                    claim["subject_id"] = normalized_subject
            # Every WF-3B synthesis claim is built from public retrieval by
            # construction; the deterministic claim validator rejects any other
            # claim_type, so a model-authored "FACT" label is a mislabel, not
            # content.  Coerce it instead of blocking the workflow.
            claim_type = claim.get("claim_type")
            if isinstance(claim_type, str) and claim_type != "PUBLIC_CLAIM":
                changes.append(
                    f"/result/claims/{claim_index}/claim_type:"
                    f"{claim_type!r}->'PUBLIC_CLAIM'"
                )
                claim["claim_type"] = "PUBLIC_CLAIM"
            profiles = claim.get("target_section_profiles")
            if not isinstance(profiles, list):
                continue
            normalized_profiles: list[Any] = []
            for profile in profiles:
                normalized = profile_aliases.get(str(profile), profile)
                if normalized != profile:
                    changes.append(
                        f"/result/claims/{claim_index}/target_section_profiles:"
                        f"{profile}->{normalized}"
                    )
                if normalized not in normalized_profiles:
                    normalized_profiles.append(normalized)
            claim["target_section_profiles"] = normalized_profiles

        def strip_redundant_span_id(node: Any, path: str = "") -> None:
            if isinstance(node, list):
                for index, item in enumerate(node):
                    strip_redundant_span_id(item, f"{path}/{index}")
                return
            if not isinstance(node, dict):
                return
            if "source_id" in node and "span_id" in node:
                node.pop("span_id", None)
                changes.append(f"{path}/span_id:removed")
            for key, value in list(node.items()):
                strip_redundant_span_id(value, f"{path}/{key}")

        strip_redundant_span_id(output)
        return changes

    def _normalize_output(
        self,
        prompt_id: str,
        output: Any,
        envelope: dict[str, Any] | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """Apply representation-only normalization and deterministic validation.

        The method may repair protocol representation (registered enum aliases,
        authoritative envelope constants, unambiguous field ownership, and exact
        trusted-reference aliases) and derive a workflow status from already-authored
        blockers.  It must not create, delete, or rewrite any business entity, claim,
        paragraph, finding, verdict, slot, key, budget, graph node, or graph edge.
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
        protocol_envelope = envelope or {}
        if project_id and isinstance(protocol_envelope, dict):
            # The runtime knows the authoritative project identity even when
            # the model-facing envelope does not carry it; make it visible so
            # protocol-owned fields can be canonicalized deterministically.
            protocol_envelope = {**protocol_envelope, "project_id": project_id}
        normalized, protocol_ref_report = canonicalize_protocol_refs(
            normalized, protocol_envelope
        )
        if protocol_ref_report.get("changes"):
            normalized.setdefault("warnings", []).append(
                "SYSTEM_PROTOCOL_REF_CANONICALIZATION: "
                + "; ".join(protocol_ref_report["changes"][:12])
            )
        normalized, self_ref_report = drop_finding_self_reference_evidence(normalized)
        if self_ref_report.get("changes"):
            normalized.setdefault("warnings", []).append(
                "SYSTEM_FINDING_SELF_REFERENCE_DROP: "
                + "; ".join(self_ref_report["changes"][:12])
            )
        if prompt_id == "P-BACKGROUND-RESEARCH-PLAN":
            from .background_research import merge_background_followup_plan

            feedback = (envelope.get("payload") or {}).get("retrieval_feedback") or {}
            normalized["result"] = merge_background_followup_plan(normalized.get("result") or {}, feedback)
        if prompt_id == "P-BACKGROUND-RESEARCH-SYNTHESIS":
            wf3b_representation_changes = (
                self._normalize_wf3b_synthesis_representation(normalized)
            )
            if wf3b_representation_changes:
                normalized.setdefault("warnings", []).append(
                    "SYSTEM_WF3B_SYNTHESIS_REPRESENTATION_NORMALIZATION: "
                    + "; ".join(wf3b_representation_changes[:20])
                )
        normalized, null_literal_report = normalize_exact_null_literals(
            normalized, output_schema
        )
        if null_literal_report.get("normalized_count"):
            normalized.setdefault("warnings", []).append(
                "SYSTEM_EXACT_NULL_LITERAL_NORMALIZATION: "
                + ", ".join(null_literal_report.get("paths") or [])
            )
        normalized, wf3_machine_report = canonicalize_wf3_machine_fields(
            prompt_id, normalized, envelope
        )
        if wf3_machine_report:
            normalized.setdefault("warnings", []).append(
                "SYSTEM_WF3_MACHINE_FIELD_PROJECTION: "
                f"{wf3_machine_report['change_count']}"
            )
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

        normalized, protocol_ref_changes = self._normalize_protocol_reference_values(
            prompt_id,
            normalized,
            envelope,
        )
        if protocol_ref_changes:
            normalized.setdefault("warnings", []).append(
                "SYSTEM_PROTOCOL_REFERENCE_NORMALIZATION: "
                + "; ".join(protocol_ref_changes[:12])
            )
        protocol_ref_errors = self._protocol_reference_contract_errors(
            prompt_id,
            normalized,
            envelope,
        )
        if protocol_ref_errors:
            raise PromptExecutionError(
                "Output protocol reference validation failed",
                validation_errors=protocol_ref_errors,
            )

        receipt_changes = self._complete_deterministic_protocol_receipts(
            prompt_id,
            normalized,
            envelope,
        )
        if receipt_changes:
            normalized.setdefault("warnings", []).append(
                "SYSTEM_PROTOCOL_RECEIPT_COMPLETION: "
                + "; ".join(receipt_changes[:20])
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

        if prompt_id == "P-EXPRESSION-POLISH" and envelope:
            lineage_changes = self._normalize_expression_source_lineage_actions(
                normalized,
                envelope,
            )
            if lineage_changes:
                normalized.setdefault("warnings", []).append(
                    "SYSTEM_EXPRESSION_SOURCE_LINEAGE_ACTION_NORMALIZATION: "
                    + "; ".join(lineage_changes[:20])
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

        if prompt_id in WF3B_RUNTIME_OWNED_SOURCE_REFS_PROMPTS:
            # These WF-3B nodes are not enrolled in canonicalize_wf3_machine_fields
            # yet.  Their top-level provenance is runtime-owned and the provider
            # never sees trusted catalog IDs, so any provider-authored source_ref
            # is necessarily fabricated.  Clear it deterministically instead of
            # exhausting provider retries on an unfixable provenance error.
            background_refs = normalized.get("source_refs")
            if isinstance(background_refs, list) and background_refs:
                normalized["source_refs"] = []
                normalized.setdefault("warnings", []).append(
                    "SYSTEM_WF3B_SOURCE_REFS_RUNTIME_OWNED: "
                    f"cleared {len(background_refs)} provider-authored refs"
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
            if prompt_id == "P-SCHEME-EXTRACT":
                # Derived from the rules' final, alias- and provenance-normalized
                # source_refs, so it must run after both binding passes above.
                normalized, coverage_report = rebuild_scheme_extraction_coverage(normalized)
                if coverage_report.get("changes"):
                    normalized.setdefault("warnings", []).append(
                        "SYSTEM_EXTRACTION_COVERAGE_REBUILD: "
                        + "; ".join(coverage_report["changes"][:4])
                    )
            reference_errors = validate_reference_ids(normalized, envelope)
            if reference_errors:
                raise PromptExecutionError(
                    "Output reference integrity validation failed",
                    validation_errors=reference_errors,
                )
        if envelope:
            questions = normalized.get("user_questions")
            if isinstance(questions, list):
                normalized["user_questions"] = widen_gate_questions(questions)
            normalized, _ = canonicalize_wf3_producer_status(prompt_id, normalized)
            normalized, _ = canonicalize_wf3_critic_control(prompt_id, normalized)
            normalized = self._normalize_human_gate_status(normalized)
            human_gate_errors = self._human_gate_contract_errors(normalized)
            if human_gate_errors:
                raise PromptExecutionError(
                    "Output human-gate contract validation failed",
                    validation_errors=human_gate_errors,
                )
        return normalized

    @staticmethod
    def _complete_deterministic_protocol_receipts(
        prompt_id: str,
        output: dict[str, Any],
        envelope: dict[str, Any] | None,
    ) -> list[str]:
        """Complete only guard-owned or input-derived protocol receipts.

        These fields are not business content.  They either mirror a complete
        partition that can be derived from the immutable request, or are
        compatibility placeholders whose schema explicitly requires an empty
        list because the deterministic guard owns the corresponding findings.

        Existing values, including ``null`` and malformed containers, are
        never overwritten; strict schema validation must still reject those.
        """

        result = output.get("result")
        if not isinstance(result, dict):
            return []

        changes: list[str] = []
        if prompt_id == "P-TARGETED-REPAIR":
            if "unresolved_finding_ids" in result or not isinstance(envelope, dict):
                return changes
            payload = envelope.get("payload")
            findings = (
                payload.get("findings_to_repair")
                if isinstance(payload, dict)
                else None
            )
            resolved = result.get("resolved_finding_ids")
            if not isinstance(findings, list) or not isinstance(resolved, list):
                return changes

            requested_ids: list[str] = []
            for item in findings:
                if not isinstance(item, dict):
                    return changes
                finding_id = item.get("finding_instance_id")
                if not isinstance(finding_id, str) or not finding_id.strip():
                    return changes
                requested_ids.append(finding_id.strip())
            if not requested_ids or len(set(requested_ids)) != len(requested_ids):
                return changes

            resolved_ids: list[str] = []
            for finding_id in resolved:
                if not isinstance(finding_id, str) or not finding_id.strip():
                    return changes
                resolved_ids.append(finding_id.strip())
            if len(set(resolved_ids)) != len(resolved_ids):
                return changes
            requested_set = set(requested_ids)
            if any(finding_id not in requested_set for finding_id in resolved_ids):
                return changes

            resolved_set = set(resolved_ids)
            result["unresolved_finding_ids"] = [
                finding_id
                for finding_id in requested_ids
                if finding_id not in resolved_set
            ]
            changes.append(
                "$/result/unresolved_finding_ids derived from "
                "$/payload/findings_to_repair minus $/result/resolved_finding_ids"
            )
            return changes

        if prompt_id == "P-WRITE-BLUEPRINT-CRITIC":
            guard_owned_placeholders = (
                "uncovered_revision_task_ids",
                "invalid_slot_refs",
                "critical_unresolved_slot_ids",
            )
            for field in guard_owned_placeholders:
                if field not in result:
                    result[field] = []
                    changes.append(f"$/result/{field}=[]")
        return changes

    @staticmethod
    def _normalize_expression_source_lineage_actions(
        output: dict[str, Any],
        envelope: dict[str, Any],
    ) -> list[str]:
        """Restore an unambiguous Stage-6 vocabulary collision.

        ``source_preservation_summary[*].action`` records the lineage action
        already established by ``P-WRITE-CONTENT``.  It is immutable during
        expression polishing.  The separate Stage-6A--6D file pipeline uses
        the same field name ``action`` for the *current editing operation* and
        allows ``POLISHED``.  A model can therefore emit ``POLISHED`` here even
        though that token is not part of the source-lineage contract.

        Only that one foreign token is repaired, and only when the emitted
        entry can be paired by both ``source_span`` and ``paragraph_id`` with
        the authoritative input entry.  Valid-but-different lineage actions and
        every other unknown token remain untouched for the quality guard or
        strict schema validator to reject.
        """

        payload = envelope.get("payload")
        if not isinstance(payload, dict):
            return []
        content_candidate = payload.get("content_candidate")
        result = output.get("result")
        if not isinstance(content_candidate, dict) or not isinstance(result, dict):
            return []
        authoritative = content_candidate.get("source_preservation_summary")
        emitted = result.get("source_preservation_summary")
        if not isinstance(authoritative, list) or not isinstance(emitted, list):
            return []
        if len(authoritative) != len(emitted):
            return []

        allowed_lineage_actions = {
            "PRESERVED",
            "REPHRASED",
            "REPLACED",
            "REMOVED",
        }
        changes: list[str] = []
        for index, (source_item, output_item) in enumerate(zip(authoritative, emitted)):
            if not isinstance(source_item, dict) or not isinstance(output_item, dict):
                continue
            if (
                source_item.get("source_span") != output_item.get("source_span")
                or source_item.get("paragraph_id") != output_item.get("paragraph_id")
            ):
                continue
            before = str(output_item.get("action") or "").strip()
            source_action = str(source_item.get("action") or "").strip()
            if before.upper() != "POLISHED" or source_action not in allowed_lineage_actions:
                continue
            output_item["action"] = source_action
            changes.append(
                f"$/result/source_preservation_summary/{index}/action:"
                f"{before}->{source_action}"
            )
        return changes

    @staticmethod
    def _targeted_repair_diff_paths(
        before: Any,
        after: Any,
        path_tokens: tuple[Any, ...] = (),
    ) -> list[str]:
        """Return minimal RFC 6901 paths whose JSON values actually changed."""

        if type(before) is not type(after):
            return [join_pointer(*path_tokens)]
        if isinstance(before, dict):
            paths: list[str] = []
            for key in sorted(set(before) | set(after), key=str):
                child_tokens = (*path_tokens, key)
                if key not in before or key not in after:
                    paths.append(join_pointer(*child_tokens))
                else:
                    paths.extend(
                        PromptExecutor._targeted_repair_diff_paths(
                            before[key], after[key], child_tokens
                        )
                    )
            return paths
        if isinstance(before, list):
            if len(before) != len(after):
                return [join_pointer(*path_tokens)]
            paths: list[str] = []
            for index, (left, right) in enumerate(zip(before, after)):
                paths.extend(
                    PromptExecutor._targeted_repair_diff_paths(
                        left, right, (*path_tokens, index)
                    )
                )
            return paths
        return [] if before == after else [join_pointer(*path_tokens)]

    @staticmethod
    def _validate_output_semantics(
        prompt_id: str,
        envelope: dict[str, Any],
        output: dict[str, Any],
    ) -> None:
        """Validate cross-field business invariants without mutating output."""

        wf3_errors = wf3_output_semantic_errors(prompt_id, envelope, output)
        if wf3_errors:
            raise PromptExecutionError(
                "WF-3 output failed cross-list identity validation",
                validation_errors=wf3_errors,
            )
        if prompt_id != "P-TARGETED-REPAIR":
            return
        requested = [
            str(item.get("finding_instance_id") or "")
            for item in (envelope.get("payload") or {}).get("findings_to_repair") or []
            if isinstance(item, dict)
        ]
        result = output.get("result") or {}
        resolved = [str(item) for item in result.get("resolved_finding_ids") or []]
        unresolved = [
            str(item) for item in result.get("unresolved_finding_ids") or []
        ]
        errors: list[str] = []
        if not requested or any(not item for item in requested):
            errors.append(
                "/payload/findings_to_repair: every finding requires finding_instance_id"
            )
        if len(set(requested)) != len(requested):
            errors.append(
                "/payload/findings_to_repair: finding_instance_id values must be unique"
            )
        overlap = sorted(set(resolved) & set(unresolved))
        if overlap:
            errors.append(
                "/result: resolved_finding_ids and unresolved_finding_ids overlap: "
                + ", ".join(overlap)
            )
        requested_set = set(requested)
        classified_set = set(resolved) | set(unresolved)
        unknown = sorted(classified_set - requested_set)
        missing = sorted(requested_set - classified_set)
        if unknown:
            errors.append(
                "/result: finding ids not present in findings_to_repair: "
                + ", ".join(unknown)
            )
        if missing:
            errors.append(
                "/result: findings not classified as resolved or unresolved: "
                + ", ".join(missing)
            )
        if str(output.get("status") or "").upper() == "PASS" and unresolved:
            errors.append(
                "/status: PASS requires unresolved_finding_ids to be empty"
            )
        payload = envelope.get("payload") or {}
        original_object = payload.get("original_object") or {}
        original_content = original_object.get("content")
        repaired_object = result.get("repaired_object")
        if not isinstance(original_content, dict) or not isinstance(
            repaired_object, dict
        ):
            errors.append(
                "/result/repaired_object: targeted repair requires an object matching original_object.content"
            )
        else:
            repaired_document = (
                repaired_object
                if isinstance(repaired_object.get("content"), dict)
                else {"content": repaired_object}
            )
            original_document = {"content": original_content}
            # Compare both sides on the model-owned semantic projection: the
            # candidate passed the full normalization chain (trusted source
            # binding, protocol canonicalization, hash recomputation) while
            # the frozen baseline did not.  Scrubbing code-owned fields from
            # both sides keeps the closure diff symmetric, so runtime-completed
            # metadata is never reported as a model-made change.
            actual_paths = PromptExecutor._targeted_repair_diff_paths(
                scrub_code_owned_fields_for_repair_diff(original_document),
                scrub_code_owned_fields_for_repair_diff(repaired_document),
            )
            allowed_paths = [str(item) for item in payload.get("allowed_paths") or []]
            protected_paths = [
                str(item) for item in payload.get("protected_paths") or []
            ]
            declared_paths = [
                str(item) for item in result.get("changed_paths") or []
            ]
            for path in actual_paths:
                if not any(
                    is_ancestor_or_same(allowed, path)
                    for allowed in allowed_paths
                ):
                    errors.append(
                        f"/result/repaired_object: actual changed path {path!r} is outside allowed_paths"
                    )
                if any(paths_overlap(protected, path) for protected in protected_paths):
                    errors.append(
                        f"/result/repaired_object: actual changed path {path!r} overlaps protected_paths"
                    )
                if not any(
                    is_ancestor_or_same(declared, path)
                    for declared in declared_paths
                ):
                    errors.append(
                        f"/result/changed_paths: actual changed path {path!r} was not declared"
                    )
            for path in declared_paths:
                if not any(is_ancestor_or_same(path, actual) for actual in actual_paths):
                    errors.append(
                        f"/result/changed_paths: declared path {path!r} has no corresponding object diff"
                    )
            expected_protected = [
                {
                    "path": str(item.get("path") or ""),
                    "hash": str(item.get("hash") or ""),
                }
                for item in payload.get("protected_hashes") or []
                if isinstance(item, dict)
            ]
            reported_protected = [
                {
                    "path": str(item.get("path") or ""),
                    "hash": str(item.get("hash") or ""),
                }
                for item in result.get("unchanged_protected_hashes") or []
                if isinstance(item, dict)
            ]
            if reported_protected != expected_protected:
                errors.append(
                    "/result/unchanged_protected_hashes: must exactly echo payload.protected_hashes"
                )
        if errors:
            raise PromptExecutionError(
                "Targeted repair finding closure validation failed",
                validation_errors=errors,
            )

    async def execute(
        self,
        prompt_id: str,
        envelope: dict[str, Any],
        *,
        project_id: str,
        workflow_id: str | None = None,
        original_environment: str | None = None,
        contract_retry_feedback: list[str] | None = None,
    ) -> dict[str, Any]:
        run_id = new_id("run")
        started = time.perf_counter()
        quality_context_envelope = envelope
        model_envelope, input_compaction = self._prepare_model_envelope(prompt_id, envelope)
        # ``model_envelope`` remains the full deterministic validation context.
        # The provider sees a smaller projection that excludes runtime-only
        # provenance/catalog material but preserves business semantics.
        model_envelope = attach_trusted_source_catalog(model_envelope)
        provider_contract_envelope, provider_contract_projection = (
            self._prepare_provider_contract_envelope(model_envelope)
        )
        provider_envelope, provider_business_projection = self._prepare_provider_envelope(
            provider_contract_envelope
        )
        # Keep the exact provider-visible payload separate from the full
        # validation envelope so both success and failure traces are auditable.
        provider_call_envelope = provider_envelope
        provider_projection = {
            "strategy": "TWO_STAGE_PROVIDER_BUSINESS_PROJECTION",
            "projection_version": MODEL_CONTEXT_PROJECTION_VERSION,
            "validation_context_chars": provider_contract_projection["validation_context_chars"],
            "provider_contract_chars": provider_contract_projection["provider_contract_chars"],
            "provider_envelope_chars": provider_business_projection["provider_envelope_chars"],
            "saved_chars": (
                provider_contract_projection["validation_context_chars"]
                - provider_business_projection["provider_envelope_chars"]
            ),
            "saved_ratio": (
                (
                    provider_contract_projection["validation_context_chars"]
                    - provider_business_projection["provider_envelope_chars"]
                )
                / provider_contract_projection["validation_context_chars"]
                if provider_contract_projection["validation_context_chars"]
                else 0.0
            ),
            "contract_projection": provider_contract_projection,
            "business_projection": provider_business_projection,
            "validation_uses_full_trusted_context": True,
        }
        input_compaction = self._merge_input_compaction(
            input_compaction, provider_projection
        )
        semantic_model_contract = (
            str(getattr(self.gateway.settings, "runtime_mode", "")).upper() == "LIVE"
            and supports_semantic_model_contract(prompt_id)
            and bool(getattr(self.pack, "has_model_contract", lambda _pid: False)(prompt_id))
        )
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
                    raise PromptExecutionError("Compacted validation input schema failed", validation_errors=model_input_errors)
            provider_input_errors = self.pack.validate(
                prompt_id, "input", provider_contract_envelope
            )
            if provider_input_errors:
                raise PromptExecutionError(
                    "Provider contract projection failed schema validation",
                    validation_errors=provider_input_errors,
                )
            route = self.router.route(prompt_id, model_envelope, original_environment=original_environment)
            project_config = load_project_config(self.db, project_id)

            if semantic_model_contract:
                enforce_wf3_pre_model_guards(prompt_id, model_envelope)
                provider_call_envelope = build_semantic_model_input(prompt_id, model_envelope)
                semantic_input_errors = self.pack.validate_model(prompt_id, "input", provider_call_envelope)
                if semantic_input_errors:
                    raise PromptExecutionError(
                        "Semantic model input projection failed validation",
                        validation_errors=semantic_input_errors,
                    )
                output_schema = self.pack.inlined_model_schema(prompt_id, "output")
                input_compaction = {
                    **(input_compaction or {}),
                    "semantic_model_contract": {
                        "version": SEMANTIC_MODEL_CONTRACT_VERSION,
                        "canonical_provider_chars": len(json.dumps(provider_envelope, ensure_ascii=False, separators=(",", ":"))),
                        "semantic_provider_chars": len(json.dumps(provider_call_envelope, ensure_ascii=False, separators=(",", ":"))),
                    },
                }
            else:
                output_schema = self.pack.inlined_schema(prompt_id, "output")

            if route.environment == "ONLINE_PUBLIC":
                assert_online_payload_safe(provider_call_envelope, project_config)
            system_prompt = self._system_prompt(
                prompt_id, output_schema, provider_call_envelope,
                semantic_model_contract=semantic_model_contract,
            )
            if contract_retry_feedback:
                system_prompt += self._contract_retry_feedback_prompt(
                    contract_retry_feedback
                )
            wf3_budget = wf3_provider_request_budget_report(
                prompt_id, system_prompt, provider_call_envelope
            )
            if wf3_budget:
                input_compaction = {
                    **(input_compaction or {}),
                    "wf3_request_budget": wf3_budget,
                }
                if not wf3_budget["within_budget"]:
                    raise PromptExecutionError(
                        "WF-3 provider request exceeds its deterministic node budget",
                        validation_errors=[
                            "/provider_request: "
                            f"{wf3_budget['provider_visible_chars']} chars exceeds "
                            f"{wf3_budget['limit_chars']} for {prompt_id}"
                        ],
                    )
            result = await self.gateway.invoke(
                route, prompt_id, system_prompt, provider_call_envelope, output_schema,
                direct_tool_arguments=semantic_model_contract,
            )
            raw_response_text = result.raw_text
            model_null_literal_paths: list[str] = []
            survey_default_paths: list[str] = []
            try:
                provider_output = result.output
                if semantic_model_contract:
                    provider_output = apply_semantic_model_output_defaults(
                        self.pack.model_schema(prompt_id, "output"), provider_output
                    )
                    provider_output, model_null_literal_report = normalize_exact_null_literals(
                        provider_output, self.pack.model_schema(prompt_id, "output")
                    )
                    model_null_literal_paths = list(model_null_literal_report.get("paths") or [])
                    provider_output, survey_default_report = apply_wf1_survey_intake_defaults(
                        prompt_id, model_envelope, provider_output
                    )
                    survey_default_paths = [
                        str(item.get("path")) for item in survey_default_report
                    ]
                    semantic_output_errors = self.pack.validate_model(prompt_id, "output", provider_output)
                    semantic_output_errors.extend(semantic_model_reference_errors(prompt_id, model_envelope, provider_output))
                    if semantic_output_errors:
                        raise PromptExecutionError(
                            "Semantic model output validation failed",
                            validation_errors=semantic_output_errors,
                        )
                    provider_output = expand_semantic_model_output(prompt_id, model_envelope, provider_output)
                output = self._normalize_output(prompt_id, provider_output, model_envelope, project_id=project_id)
                if model_null_literal_paths:
                    output.setdefault("warnings", []).append(
                        "SYSTEM_EXACT_NULL_LITERAL_NORMALIZATION(model-output): "
                        + ", ".join(model_null_literal_paths[:12])
                    )
                if survey_default_paths:
                    output.setdefault("warnings", []).append(
                        "SYSTEM_WF1_SURVEY_INTAKE_DEFAULT: "
                        + ", ".join(survey_default_paths[:12])
                    )
            except PromptExecutionError as exc:
                raise ProviderError(
                    f"Provider output contract validation failed: {exc}",
                    kind=ProviderFailureKind.RESPONSE_SHAPE,
                    phase="output_structure_validation",
                    response_excerpt=str(raw_response_text or "")[:1000],
                    retryable_hint=False,
                    validation_errors=exc.validation_errors,
                ) from exc
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
                    raise ProviderError(
                        "Provider output failed post-normalization container validation",
                        kind=ProviderFailureKind.RESPONSE_SHAPE,
                        phase="output_structure_validation",
                        response_excerpt=str(raw_response_text or "")[:1000],
                        retryable_hint=False,
                        validation_errors=post_structure_errors,
                    )
            output_errors = self.pack.validate(prompt_id, "output", output)
            if output_errors:
                raise ProviderError(
                    "Provider output failed strict schema validation",
                    kind=ProviderFailureKind.RESPONSE_SHAPE,
                    phase="output_schema_validation",
                    response_excerpt=str(raw_response_text or "")[:1000],
                    retryable_hint=False,
                    validation_errors=output_errors,
                )
            try:
                self._validate_output_semantics(prompt_id, model_envelope, output)
            except PromptExecutionError as exc:
                raise ProviderError(
                    f"Provider output failed semantic contract validation: {exc}",
                    kind=ProviderFailureKind.RESPONSE_SHAPE,
                    phase="output_semantic_validation",
                    response_excerpt=str(raw_response_text or "")[:1000],
                    retryable_hint=False,
                    validation_errors=exc.validation_errors,
                ) from exc
            status = output.get("status", "ERROR")
            duration_ms = int((time.perf_counter() - started) * 1000)
            self._save_run(run_id, project_id, workflow_id, prompt_id, status, result.model_id, result.endpoint_id, input_hash, model_envelope, output, None, duration_ms)
            self._save_artifact(
                project_id, workflow_id, prompt_id, output, model_envelope, system_prompt,
                raw_response_text, output_schema, route.environment if route else None,
                result.model_id, result.endpoint_id, duration_ms, status, None,
                quality_context_envelope=quality_context_envelope if input_compaction else None,
                input_compaction=input_compaction,
                provider_request_envelope=provider_call_envelope,
            )
            return {
                "run_id": run_id,
                "prompt_id": prompt_id,
                "status": status,
                "route": {"environment": route.environment, "model_id": result.model_id, "endpoint_id": result.endpoint_id},
                "output": output,
                "guard_report": guard_report,
                "quality_guard_enabled": self.quality_guard_enabled,
                "guard_observation_status": guard_report.get("observation_status"),
            }
        except (PromptExecutionError, RoutingDenied, OutboundPrivacyError, LLMError, KeyError, ValueError) as exc:
            duration_ms = int((time.perf_counter() - started) * 1000)
            details = [redact_secret_text(str(item)) for item in (getattr(exc, "validation_errors", []) or [])]
            error = redact_secret_text(str(exc) + ((" | " + "; ".join(details[:20])) if details else ""))
            self._save_run(run_id, project_id, workflow_id, prompt_id, "ERROR", route.model_id if route else None, route.endpoint_id if route else None, input_hash, model_envelope, output, error, duration_ms)
            self._save_trace(
                project_id, workflow_id, prompt_id, model_envelope, system_prompt,
                raw_response_text, output_schema, route.environment if route else None,
                route.model_id if route else None, route.endpoint_id if route else None,
                duration_ms, "ERROR", error,
                quality_context_envelope=quality_context_envelope if input_compaction else None,
                input_compaction=input_compaction,
                provider_request_envelope=provider_call_envelope,
            )
            raise PromptExecutionError(
                error, validation_errors=details, run_id=run_id
            ) from exc
        except (AttributeError, TypeError, IndexError) as exc:
            # Last-resort execution boundary.  Declared model-output container
            # mismatches should already be reported by the structure preflight;
            # reaching this branch therefore indicates an internal contract
            # processing defect.  Persist it as an explicit workflow error
            # instead of allowing an untracked exception to escape.
            duration_ms = int((time.perf_counter() - started) * 1000)
            error = redact_secret_text(
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
                provider_request_envelope=provider_call_envelope,
            )
            raise PromptExecutionError(error, run_id=run_id) from exc

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
        wf3_envelope, wf3_compaction = compact_wf3_research_envelope(
            prompt_id, envelope
        )
        if wf3_compaction is not None:
            return wf3_envelope, wf3_compaction
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

    @staticmethod
    def _prepare_provider_contract_envelope(
        validation_envelope: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Create the schema-valid provider contract envelope.

        This first projection removes bulky trusted-catalog/provenance material
        that is already retained in the deterministic validation envelope while
        preserving the original prompt input schema.  The result is validated
        against that schema before the second, model-facing business projection
        is created.
        """

        provider = copy.deepcopy(validation_envelope)
        before_chars = len(json.dumps(provider, ensure_ascii=False, separators=(",", ":")))
        catalog = provider.pop("trusted_source_catalog", None)
        removed_catalog_entries = len(catalog) if isinstance(catalog, list) else 0
        removed_fields: dict[str, int] = {}
        nulled_inherited_fields: dict[str, int] = {}

        def strip_source_ref(ref: Any) -> None:
            if not isinstance(ref, dict):
                return
            for field in _PROVIDER_SOURCE_REF_OMIT_FIELDS:
                if field in ref:
                    ref.pop(field, None)
                    removed_fields[field] = removed_fields.get(field, 0) + 1

        def visit(node: Any) -> None:
            if isinstance(node, list):
                for item in node:
                    visit(item)
                return
            if not isinstance(node, dict):
                return
            for key, value in list(node.items()):
                if key == "source_ref":
                    strip_source_ref(value)
                elif key == "source_refs" and isinstance(value, list):
                    for ref in value:
                        strip_source_ref(ref)
                elif key == "inherited_source_catalog" and isinstance(value, list):
                    # Keep this intermediate envelope schema-valid.  The
                    # model-facing business projection removes these machine
                    # provenance fields entirely after the schema check.
                    for entry in value:
                        if not isinstance(entry, dict):
                            continue
                        for field in ("document_version_id", "source_hash"):
                            if entry.get(field) is not None:
                                entry[field] = None
                                nulled_inherited_fields[field] = (
                                    nulled_inherited_fields.get(field, 0) + 1
                                )
                visit(value)

        visit(provider)
        after_chars = len(json.dumps(provider, ensure_ascii=False, separators=(",", ":")))
        return provider, {
            "strategy": "SCHEMA_VALID_PROVIDER_CONTRACT_PROJECTION",
            "validation_context_chars": before_chars,
            "provider_contract_chars": after_chars,
            "saved_chars": before_chars - after_chars,
            "removed_trusted_source_catalog_entries": removed_catalog_entries,
            "removed_source_ref_fields": removed_fields,
            "nulled_inherited_catalog_fields": nulled_inherited_fields,
        }

    @staticmethod
    def _prepare_provider_envelope(
        provider_contract_envelope: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Create the actual model-visible business envelope.

        The full input and the schema-valid provider contract projection have
        already been validated before this object is sent to the model.  Remove
        deterministic integrity material that the model neither needs to reason
        about nor should copy: runtime-only hashes and wrapper IDs.  A tiny
        exception remains for hashes that the current output contract explicitly
        requires the model to preserve verbatim (trace lineage and protected-path
        receipts).  Semantic IDs used to connect business entities remain.
        """

        provider = copy.deepcopy(provider_contract_envelope)
        before_chars = len(json.dumps(provider, ensure_ascii=False, separators=(",", ":")))
        removed_hash_fields: dict[str, int] = {}
        retained_contract_hash_fields: dict[str, int] = {}
        removed_runtime_ids: dict[str, int] = {}
        removed_machine_metadata_fields: dict[str, int] = {}
        removed_containers: dict[str, int] = {}

        runtime_id_paths = {
            ("prompt_id",),
            ("task", "task_id"),
            ("scope", "project_id"),
            ("payload", "task_instruction", "task_instruction_id"),
        }

        # These fields are deterministic container/transport metadata.  They are
        # useful to the runtime but do not change the model's business reasoning.
        current_section_machine_fields = {
            "block_ids",
            "contains_table",
            "contains_formula",
            "contains_image",
            "contains_comment",
            "contains_revision",
            "security_level",
        }
        project_item_machine_fields = {"owner_ref", "security_level"}

        def is_machine_metadata(path: tuple[str, ...], key: str) -> bool:
            if (
                path == ("payload", "current_sections", "[]")
                and key in current_section_machine_fields
            ):
                return True
            if (
                path == ("payload", "project_subgraph", "items", "[]")
                and key in project_item_machine_fields
            ):
                return True
            if path == ("payload", "project_subgraph") and key in {"item_ids", "relation_ids"}:
                return True
            return False

        def hash_is_model_required(path: tuple[str, ...], key: str) -> bool:
            # These are not free-form integrity hints.  Existing output
            # contracts require the model to preserve them verbatim, so they
            # are part of the current transport contract rather than useless
            # generation context.
            if key == "source_hash" and "trace_links" in path:
                return True
            if key == "hash" and len(path) >= 2 and path[:2] == ("payload", "protected_hashes"):
                return True
            return False

        def visit(node: Any, path: tuple[str, ...] = ()) -> None:
            if isinstance(node, list):
                for item in node:
                    visit(item, path + ("[]",))
                return
            if not isinstance(node, dict):
                return

            for key in list(node.keys()):
                child_path = path + (key,)
                value = node.get(key)

                if (
                    key == "document_version_id"
                    and len(path) >= 3
                    and path[:2] == ("payload", "inherited_source_catalog")
                ):
                    node.pop(key, None)
                    removed_runtime_ids["/payload/inherited_source_catalog[]/document_version_id"] = (
                        removed_runtime_ids.get(
                            "/payload/inherited_source_catalog[]/document_version_id", 0
                        )
                        + 1
                    )
                    continue

                if is_machine_metadata(path, key):
                    node.pop(key, None)
                    metadata_path = "/" + "/".join(path + (key,))
                    removed_machine_metadata_fields[metadata_path] = (
                        removed_machine_metadata_fields.get(metadata_path, 0) + 1
                    )
                    continue

                if key == "hash" or key.endswith("_hash"):
                    if hash_is_model_required(path, key):
                        retained_contract_hash_fields[key] = (
                            retained_contract_hash_fields.get(key, 0) + 1
                        )
                    else:
                        node.pop(key, None)
                        removed_hash_fields[key] = removed_hash_fields.get(key, 0) + 1
                        continue

                if child_path in runtime_id_paths:
                    node.pop(key, None)
                    path_text = "/" + "/".join(child_path)
                    removed_runtime_ids[path_text] = removed_runtime_ids.get(path_text, 0) + 1
                    continue

                visit(value, child_path)

            # ``freshness`` contains deterministic digest attestations only.
            # Once those hashes are removed, do not send an empty wrapper.
            if path == () and isinstance(node.get("freshness"), dict) and not node["freshness"]:
                node.pop("freshness", None)
                removed_containers["/freshness"] = 1

        visit(provider)
        after_chars = len(json.dumps(provider, ensure_ascii=False, separators=(",", ":")))
        return provider, {
            "strategy": "MODEL_FACING_BUSINESS_PROJECTION",
            "projection_version": MODEL_CONTEXT_PROJECTION_VERSION,
            "provider_contract_chars": before_chars,
            "provider_envelope_chars": after_chars,
            "saved_chars": before_chars - after_chars,
            "saved_ratio": (before_chars - after_chars) / before_chars if before_chars else 0.0,
            "removed_hash_fields": removed_hash_fields,
            "retained_contract_hash_fields": retained_contract_hash_fields,
            "removed_runtime_ids": removed_runtime_ids,
            "removed_machine_metadata_fields": removed_machine_metadata_fields,
            "removed_containers": removed_containers,
            "validation_uses_full_trusted_context": True,
            "provider_contract_was_schema_validated": True,
        }

    @staticmethod
    def _merge_input_compaction(
        existing: dict[str, Any] | None,
        provider_projection: dict[str, Any],
    ) -> dict[str, Any]:
        if existing:
            return {**existing, "provider_projection": provider_projection}
        return {
            "strategy": "MODEL_FACING_SOURCE_PROJECTION",
            "provider_projection": provider_projection,
        }

    def _system_prompt(
        self,
        prompt_id: str,
        output_schema: dict[str, Any],
        envelope: dict[str, Any] | None = None,
        *,
        semantic_model_contract: bool | None = None,
    ) -> str:
        entry = self.pack.entry(prompt_id) if hasattr(self.pack, "entry") else {}
        if semantic_model_contract is None:
            semantic_model_contract = bool(
                str(entry.get("model_contract_mode") or "").upper() == "SEMANTIC"
                and getattr(self.pack, "has_model_contract", lambda _pid: False)(prompt_id)
            )
        if semantic_model_contract:
            if hasattr(self.pack, "shared_prompt_for"):
                shared_source = self.pack.shared_prompt_for(prompt_id)
            else:
                shared_source = self.pack.shared_prompt
            return str(shared_source).strip() + "\n\n" + self.pack.prompt_text(prompt_id).strip() + "\n"
        schema_properties = output_schema.get("properties") or {}
        prompt_version = str(
            ((schema_properties.get("prompt_version") or {}).get("const"))
            or entry.get("prompt_version")
            or ((envelope or {}).get("prompt_version"))
            or ""
        )
        schema_version = str(
            ((schema_properties.get("schema_version") or {}).get("const"))
            or ((envelope or {}).get("schema_version"))
            or ""
        )
        protocol_identity = (
            "# 本次运行时协议身份\n"
            f"- `prompt_id`固定为`{prompt_id}`。\n"
            f"- `prompt_version`固定为`{prompt_version}`。\n"
            f"- `schema_version`固定为`{schema_version}`。"
        )
        runtime_boundary = (
            "# 运行时契约边界\n"
            "你只生成当前任务的业务候选。Schema、枚举、字段归属、引用完整性、"
            "来源绑定、状态/Gate和语义契约由运行时确定性校验；不要复述、模拟或逐项自检这些规则。\n"
            "引用已有对象时只使用输入中可见的ID；source_refs.source_id只复制输入中可见的source_id。"
            "版本、Hash和Span等可信来源元数据由运行时绑定，不要猜测或计算。"
        )
        human_boundary = (
            "# 人工输入约束\n"
            "payload.human_resolutions若非空即为已确认回答，只能在其target_paths和当前任务范围内使用。"
        )
        wf3_boundary = ""
        if prompt_id in {
            "P-SAFE-ONLINE-PACKAGE",
            "P-SAFE-ONLINE-PACKAGE-CRITIC",
            "P-PUBLIC-RESEARCH-PLAN",
            "P-PUBLIC-RESEARCH-SYNTHESIS",
            "P-PUBLIC-RESEARCH-CRITIC",
            "P-ONLINE-RESULT-IMPORT-CRITIC",
        }:
            wf3_boundary = (
                "# WF-3 字段所有权\n"
                "只判断研究、安全和证据语义。顶层source_refs返回[]；新建ID填runtime。"
                "这些字段及协议常量、来源元数据、优先级和控制状态均由运行时覆盖。"
                "不要计算Hash或把路径当source_id；Finding.evidence_refs可使用payload.<字段>路径。"
            )
        if prompt_id == "P-SAFE-ONLINE-PACKAGE":
            wf3_boundary += (
                "payload.allowed_topics非空时即为本工作流已确认的外发主题边界；"
                "外发审批由下一固定Gate处理，不得询问是否进入该Gate。"
            )
        if prompt_id == "P-PUBLIC-RESEARCH-CRITIC":
            wf3_boundary += (
                "本节点只审查四类语义：来源是否实质支持论断、是否过度概括、"
                "是否遗漏关键反证、研究问题是否真正得到回答。"
                "不要复查ID存在性、Hash、年份边界、重复项、查询覆盖、manifest或安全标签；"
                "这些由输入中的确定性报告和运行时校验负责。Critic PASS只表示语义审查通过，"
                "不表示文献覆盖已经饱和。"
            )
        if prompt_id == "P-PUBLIC-RESEARCH-SYNTHESIS":
            wf3_boundary += (
                "result.claims[].source_refs只需逐字复制retrieved_sources或extracted_passages中的source_id；"
                "其余来源元数据由运行时覆盖。"
            )
        if prompt_id == "P-ONLINE-RESULT-IMPORT-CRITIC":
            wf3_boundary += (
                "只判断已验证公开Claim的导入语义；不得重新执行文献覆盖审查。"
                "Claim ID分区、required confirmation ID及安全控制结果由运行时校验或构造。"
            )
        if hasattr(self.pack, "shared_prompt_for"):
            shared_source = self.pack.shared_prompt_for(prompt_id)
        else:
            shared_source = self.pack.shared_prompt
        shared_prompt = str(shared_source).replace(
            "{{SEMANTIC_CONTRACT_RUNTIME}}", ""
        ).strip()
        return (
            shared_prompt
            + "\n\n"
            + protocol_identity
            + "\n\n"
            + self.pack.prompt_text(prompt_id)
            + "\n\n"
            + runtime_boundary
            + "\n\n"
            + human_boundary
            + ("\n\n" + wf3_boundary if wf3_boundary else "")
            + "\n\n# 运行时强制输出Schema\n"
            + json.dumps(output_schema, ensure_ascii=False, separators=(",", ":"))
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

    def _save_artifact(self, project_id: str, workflow_id: str | None, prompt_id: str, output: dict[str, Any], envelope: dict[str, Any], system_prompt: str | None, raw_response_text: str | None, output_schema: dict[str, Any] | None, environment: str | None, model_id: str | None, endpoint_id: str | None, duration_ms: int, status: str, error: str | None, *, quality_context_envelope: dict[str, Any] | None = None, input_compaction: dict[str, Any] | None = None, provider_request_envelope: dict[str, Any] | None = None) -> None:
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
            provider_request_envelope=provider_request_envelope,
        )

    def _save_trace(self, project_id: str, workflow_id: str | None, prompt_id: str, envelope: dict[str, Any], system_prompt: str | None, raw_response_text: str | None, output_schema: dict[str, Any] | None, environment: str | None, model_id: str | None, endpoint_id: str | None, duration_ms: int, status: str, error: str | None, *, version: int | None = None, output: dict[str, Any] | None = None, quality_context_envelope: dict[str, Any] | None = None, input_compaction: dict[str, Any] | None = None, provider_request_envelope: dict[str, Any] | None = None) -> None:
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
            # ``input_envelope`` is kept for compatibility. Its explicit label
            # prevents it from being mistaken for the smaller provider payload.
            "input_envelope": envelope,
            "input_envelope_kind": "VALIDATION_ENVELOPE",
            "validation_envelope": envelope,
            "provider_request_envelope": provider_request_envelope,
            "provider_request_hash": (
                sha256_json(provider_request_envelope)
                if provider_request_envelope is not None
                else None
            ),
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
