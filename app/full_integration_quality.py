from __future__ import annotations

from typing import Any

from .proposal_quality import ProposalQualityGuard as BaseProposalQualityGuard
from .proposal_quality import QualityFinding


class FullIntegrationQualityMixin:
    """Deterministic whole-proposal checks layered over the existing quality guard."""

    def _audit_document(self, payload: dict[str, Any], output: dict[str, Any]) -> list[QualityFinding]:
        findings = list(super()._audit_document(payload, output))
        sections = payload.get("candidate_sections") or []
        section_map = payload.get("document_section_map") or []
        # Three-section and legacy integration tests retain their established
        # REVISE semantics. The stronger identity/evidence hard gate applies only
        # to a complete proposal-sized candidate set.
        if len(section_map) < 8:
            return findings
        mapped_candidate_ids = {
            str(item.get("section_id")): str(item.get("candidate_id") or "")
            for item in section_map
            if isinstance(item, dict) and item.get("section_id")
        }
        candidate_objects = {
            str(item.get("section_id")): (item.get("candidate") or {})
            for item in sections
            if isinstance(item, dict) and item.get("section_id")
        }
        candidate_id_values = [
            str(candidate.get("candidate_id") or "") for candidate in candidate_objects.values()
        ]
        mismatched_candidates = sorted(
            section_id
            for section_id, candidate in candidate_objects.items()
            if mapped_candidate_ids.get(section_id) != str(candidate.get("candidate_id") or "")
        )
        duplicate_candidate_ids = sorted({
            candidate_id
            for candidate_id in candidate_id_values
            if candidate_id and candidate_id_values.count(candidate_id) > 1
        })
        if mismatched_candidates or duplicate_candidate_ids:
            findings.append(QualityFinding(
                "QG_INTEGRATION_CANDIDATE_IDENTITY_INVALID", "P0", "INTEGRATION", "CANDIDATE_DOCUMENT",
                "candidate_sections,document_section_map",
                f"候选身份不一致章节={mismatched_candidates}，重复candidate_id={duplicate_candidate_ids}。",
                None, "INTEGRATION_AGENT",
            ))

        result = output.get("result") or {}
        graph = payload.get("argument_graph") or {}
        graph_ids = {
            str(item.get("node_id"))
            for item in graph.get("nodes") or []
            if isinstance(item, dict) and item.get("node_id")
        }
        central = graph.get("central_proposition") or {}
        if central.get("node_id"):
            graph_ids.add(str(central.get("node_id")))
        graph_ids.update(
            str(item.get("node_id"))
            for item in graph.get("research_questions") or []
            if isinstance(item, dict) and item.get("node_id")
        )
        fabricated_chain_ids = sorted({
            str(node_id)
            for item in result.get("argument_chain_checks") or []
            if isinstance(item, dict)
            for node_id in [*(item.get("source_ids") or []), *(item.get("target_ids") or [])]
            if node_id and str(node_id) not in graph_ids
        })
        if fabricated_chain_ids:
            findings.append(QualityFinding(
                "QG_ARGUMENT_CHAIN_ID_UNKNOWN", "P0", "ARGUMENT", "INTEGRATION_REPORT",
                "result.argument_chain_checks",
                f"全文Critic的论证链引用了不存在的图谱ID：{fabricated_chain_ids}。",
                None, "INTEGRATION_AGENT",
            ))

        architecture = payload.get("narrative_architecture") or {}
        contracts = {
            str(item.get("section_id")): item
            for item in architecture.get("section_contracts") or []
            if isinstance(item, dict) and item.get("section_id")
        }
        node_types = {
            str(item.get("node_id")): str(item.get("node_type") or "")
            for item in graph.get("nodes") or []
            if isinstance(item, dict) and item.get("node_id")
        }
        central_id = str((graph.get("central_proposition") or {}).get("node_id") or "")
        innovation_ids = {node_id for node_id, node_type in node_types.items() if node_type == "NOVEL_MECHANISM"}
        prior_ids = {node_id for node_id, node_type in node_types.items() if node_type == "CLOSEST_PRIOR_WORK"}
        foundation_ids = {node_id for node_id, node_type in node_types.items() if node_type == "TEAM_EVIDENCE"}
        metric_ids = {
            node_id for node_id, node_type in node_types.items()
            if node_type in {"METRIC_JUSTIFICATION", "EXPERIMENT_DESIGN"}
        }

        blocking_unresolved_sections = sorted(
            section_id
            for section_id, candidate in candidate_objects.items()
            if any(
                isinstance(item, dict) and item.get("blocking", True)
                for item in candidate.get("unresolved_items") or []
            )
        )
        if blocking_unresolved_sections:
            findings.append(QualityFinding(
                "QG_DOCUMENT_CONTAINS_BLOCKING_UNRESOLVED_ITEMS", "P1", "CONTENT", "CANDIDATE_DOCUMENT",
                "candidate_sections.unresolved_items",
                f"以下章节仍含阻断性未决项：{blocking_unresolved_sections}。",
                "返回责任章节补充事实或明确保留UNKNOWN；不得由全文Critic自行补写。",
                "WRITING_AGENT",
            ))

        def evidence_ids(candidate: dict[str, Any]) -> set[str]:
            return {
                str(value)
                for paragraph in candidate.get("paragraphs") or []
                if isinstance(paragraph, dict)
                for value in paragraph.get("evidence_ids") or []
                if value
            }

        def advanced_ids(candidate: dict[str, Any]) -> set[str]:
            return {
                str(value)
                for value in (candidate.get("claim_advancement") or {}).get("advanced_claim_ids") or []
                if value
            }

        question_ids = {
            str(item.get("node_id"))
            for item in graph.get("research_questions") or []
            if isinstance(item, dict) and item.get("node_id")
        }
        conclusion_sections = [
            section_id for section_id, contract in contracts.items()
            if contract.get("profile_id") == "CONCLUSION"
        ]
        for section_id in conclusion_sections:
            candidate = candidate_objects.get(section_id) or {}
            required = ({central_id} if central_id else set()) | question_ids | innovation_ids
            missing = sorted(required - (advanced_ids(candidate) | evidence_ids(candidate)))
            if missing:
                findings.append(QualityFinding(
                    "QG_CONCLUSION_DOES_NOT_CLOSE_ARGUMENT", "P1", "ARGUMENT", "SECTION_CANDIDATE",
                    f"candidate_sections.{section_id}",
                    f"结论章节未回扣中心命题或贡献节点：{missing}。",
                    "仅重写结论章节，逐项回答研究问题并回扣中心命题和经验证的贡献；不得引入新方法。",
                    "WRITING_AGENT",
                ))

        innovation_sections = [
            section_id for section_id, contract in contracts.items()
            if contract.get("profile_id") == "INNOVATION"
        ]
        for section_id in innovation_sections:
            used = evidence_ids(candidate_objects.get(section_id) or {}) | advanced_ids(candidate_objects.get(section_id) or {})
            if not prior_ids or not innovation_ids:
                findings.append(QualityFinding(
                    "QG_INNOVATION_GRAPH_EVIDENCE_INCOMPLETE", "P1", "ARGUMENT", "ARGUMENT_GRAPH",
                    "argument_graph.nodes",
                    "论证图缺少最近工作或新增机制节点，无法证明创新比较关系。",
                    "返回论证架构阶段补齐最近工作、局限机制、新增机制及验证关系。",
                    "ARGUMENT_ARCHITECTURE_AGENT",
                ))
                break
            if not (used & prior_ids) or not (used & innovation_ids):
                findings.append(QualityFinding(
                    "QG_INNOVATION_SECTION_LACKS_BASELINE_BINDING", "P1", "CONTENT", "SECTION_CANDIDATE",
                    f"candidate_sections.{section_id}.paragraphs.evidence_ids",
                    "创新章节没有同时绑定最接近工作与新增机制。",
                    "仅重写创新章节，明确最近工作、机制性局限、本项目新增机制和可比较验证。",
                    "WRITING_AGENT",
                ))

        foundation_sections = [
            section_id for section_id, contract in contracts.items()
            if contract.get("profile_id") == "RESEARCH_FOUNDATION"
        ]
        for section_id in foundation_sections:
            if not foundation_ids:
                findings.append(QualityFinding(
                    "QG_FOUNDATION_GRAPH_EVIDENCE_MISSING", "P1", "ARGUMENT", "ARGUMENT_GRAPH",
                    "argument_graph.nodes",
                    "论证图中没有可定位的前期证据节点。",
                    "返回项目知识或论证架构阶段补充论文、项目、代码、数据、原型或预实验来源。",
                    "PROJECT_KNOWLEDGE_AGENT",
                ))
                break
            if not (evidence_ids(candidate_objects.get(section_id) or {}) & foundation_ids):
                findings.append(QualityFinding(
                    "QG_FOUNDATION_SECTION_NOT_BOUND_TO_EVIDENCE", "P1", "CONTENT", "SECTION_CANDIDATE",
                    f"candidate_sections.{section_id}.paragraphs.evidence_ids",
                    "研究基础章节未绑定论证图中的前期证据节点。",
                    "仅重写研究基础章节，逐项说明已有成果如何支撑具体任务，并如实保留缺口。",
                    "WRITING_AGENT",
                ))

        metric_sections = [
            section_id for section_id, contract in contracts.items()
            if contract.get("profile_id") == "OUTPUTS_AND_METRICS"
        ]
        for section_id in metric_sections:
            if metric_ids and not (evidence_ids(candidate_objects.get(section_id) or {}) & metric_ids):
                findings.append(QualityFinding(
                    "QG_METRIC_SECTION_LACKS_BASELINE_EVIDENCE", "P1", "CONTENT", "SECTION_CANDIDATE",
                    f"candidate_sections.{section_id}.paragraphs.evidence_ids",
                    "成果与指标章节未绑定实验设计或指标依据节点。",
                    "仅重写成果与指标章节，补充基线、条件、数据来源、统计口径和成功判据。",
                    "WRITING_AGENT",
                ))
        return findings

    @staticmethod
    def _merge_findings(output: dict[str, Any], findings: list[QualityFinding]) -> None:
        BaseProposalQualityGuard._merge_findings(output, findings)
        result = output.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("routing_actions"), list):
            return
        allowed_routes = {
            "PROJECT_KNOWLEDGE_AGENT", "SECURITY_REVIEW_AGENT", "PLANNING_AGENT",
            "WRITING_AGENT", "USER", "BLOCK", "ARGUMENT_ARCHITECTURE_AGENT",
            "EXPRESSION_EDITOR_AGENT", "INTEGRATION_AGENT",
        }
        actions = result["routing_actions"]
        action_codes = {
            str(item.get("finding_code")) for item in actions if isinstance(item, dict)
        }
        for item in output.get("findings") or []:
            if not isinstance(item, dict) or not item.get("blocking", True):
                continue
            code = str(item.get("code") or "")
            if not code or code in action_codes:
                continue
            route = str(item.get("suggested_route") or "BLOCK")
            if route == "ORIGINAL_PRODUCER":
                route = "WRITING_AGENT"
            if route not in allowed_routes:
                route = "BLOCK"
            actions.append({
                "finding_code": code,
                "route": route,
                "reason": str(
                    item.get("repair_instruction")
                    or item.get("description")
                    or "阻断问题必须返回责任阶段处理。"
                ),
            })
            action_codes.add(code)


class FullProposalQualityGuard(FullIntegrationQualityMixin, BaseProposalQualityGuard):
    """Baseline proposal checks plus complete-document integration checks."""


__all__ = ["FullProposalQualityGuard"]
