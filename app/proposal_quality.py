from __future__ import annotations

import collections
import math
import re
from dataclasses import dataclass
from typing import Any, Iterable

from .util import sha256_text


HIGH_RISK_META_TERMS = {
    "Prompt", "Trace", "Gate", "Critic", "Repair", "Schema", "Skill", "Worker",
    "提示词", "审计日志", "完整性校验", "离线安装", "Docker", "Mermaid",
}
GENERIC_SECTION_HEADINGS = {
    "1. 本节定位与研究目标", "2. 核心问题与约束", "3. 方法与技术方案",
    "4. 工程实施要点", "5. 指标与验收方法", "6. 预期输出及与其他任务的关系",
    "图形化说明",
}
FAKE_SOURCE_HASHES = {"a" * 64, "0" * 64, "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"}
FOUNDATION_SOURCE_TYPES = {"EVIDENCE_MATERIAL", "TECHNICAL_MATERIAL"}
GENERIC_SOURCE_TEXTS = {"用户确认的示例内容", "示例内容", "围绕示例方向开展研究", "全文"}

QUALITY_DIMENSIONS = {
    "DOCUMENT_TYPE_FIT", "CENTRAL_THESIS", "ARGUMENT_CHAIN", "EVIDENCE_SUPPORT",
    "METHOD_SUBSTANCE", "INNOVATION_BASELINE", "FEASIBILITY_FOUNDATION",
    "METRIC_JUSTIFICATION", "SECTION_UNIQUENESS", "STYLE_AND_DENSITY",
    "PAGE_BUDGET", "CROSS_SECTION_CONSISTENCY",
}
SECTION_COMMON_QUALITY_DIMENSIONS = {
    "DOCUMENT_TYPE_FIT", "ARGUMENT_CHAIN", "EVIDENCE_SUPPORT",
    "SECTION_UNIQUENESS", "STYLE_AND_DENSITY",
}
SECTION_PROFILE_QUALITY_DIMENSIONS = {
    "ABSTRACT": {"CENTRAL_THESIS"},
    "PROJECT_OVERVIEW": {"CENTRAL_THESIS"},
    "BACKGROUND_AND_SIGNIFICANCE": {"CENTRAL_THESIS"},
    "LITERATURE_REVIEW": {"CENTRAL_THESIS", "INNOVATION_BASELINE"},
    "KEY_ISSUE": {"CENTRAL_THESIS"},
    "RESEARCH_OBJECTIVE": {"CENTRAL_THESIS", "METRIC_JUSTIFICATION"},
    "RESEARCH_CONTENT": {"METHOD_SUBSTANCE"},
    "METHOD_AND_ALGORITHM": {"METHOD_SUBSTANCE"},
    "TECHNICAL_ROUTE": {"METHOD_SUBSTANCE", "CROSS_SECTION_CONSISTENCY"},
    "EVALUATION": {"METHOD_SUBSTANCE", "METRIC_JUSTIFICATION"},
    "INNOVATION": {"INNOVATION_BASELINE"},
    "OUTPUTS_AND_METRICS": {"METRIC_JUSTIFICATION"},
    "RESEARCH_FOUNDATION": {"FEASIBILITY_FOUNDATION"},
}


@dataclass(frozen=True)
class QualityFinding:
    code: str
    severity: str
    category: str
    target_type: str
    target_path: str | None
    description: str
    repair_instruction: str | None
    suggested_route: str
    blocking: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "category": self.category,
            "target_type": self.target_type,
            "target_path_or_span": self.target_path,
            "description": self.description,
            "evidence_refs": [],
            "repairable": self.repair_instruction is not None,
            "repair_instruction": self.repair_instruction,
            "suggested_route": self.suggested_route,
            "blocking": self.blocking,
        }


def _texts(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _texts(item)
    elif isinstance(value, list):
        for item in value:
            yield from _texts(item)


def _candidate_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    candidate = payload.get("content_candidate") or {}
    # Historical traces used both the direct result object and the full envelope.
    if isinstance(candidate, dict) and isinstance(candidate.get("result"), dict):
        candidate = candidate["result"]
    return candidate if isinstance(candidate, dict) else {}


def _normalized_sentences(text: str) -> list[str]:
    return [
        re.sub(r"\s+", "", part)
        for part in re.split(r"[。！？!?\n]+", text)
        if len(re.sub(r"\s+", "", part)) >= 18
    ]




def _template_skeleton(text: str) -> str:
    """Normalize a sentence to expose reused rhetorical templates.

    Quoted project-specific content, section titles, identifiers and numbers are
    replaced before comparison.  This catches sentences that differ only by the
    inserted claim or chapter name while preserving genuinely different logic.
    """
    value = re.sub(r"《[^》]{1,80}》", "《SECTION》", text)
    value = re.sub(r'[“\"]([^”\"]{1,120})[”\"]', '“CLAIM”', value)
    value = re.sub(r"\b(?:section|paragraph|claim|trace|item|wp|rq|method|experiment|innovation)[-_:.]?[A-Za-z0-9_-]+\b", "ID", value, flags=re.I)
    value = re.sub(r"\d+(?:\.\d+)?%?", "NUM", value)
    value = re.sub(r"[A-Za-z0-9._:-]{8,}", "ID", value)
    value = re.sub(r"\s+", "", value)
    return value

def _content_text(candidate: dict[str, Any]) -> str:
    text = str(candidate.get("candidate_text") or "")
    if text:
        return text
    return "\n".join(str(item.get("text") or "") for item in candidate.get("paragraphs", []) if isinstance(item, dict))


def _section_title(payload: dict[str, Any]) -> str:
    section = payload.get("source_section") or {}
    return str(section.get("title") or "") if isinstance(section, dict) else ""


def _item_types(project_definition: dict[str, Any]) -> collections.Counter[str]:
    return collections.Counter(
        str(item.get("item_type"))
        for item in project_definition.get("items", [])
        if isinstance(item, dict) and item.get("item_type")
    )


def _has_real_source(item: dict[str, Any]) -> bool:
    refs = item.get("source_refs") or []
    if not refs:
        return False
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        quoted = str(ref.get("quoted_text") or "").strip()
        source_hash = str(ref.get("source_hash") or "")
        if quoted and quoted not in GENERIC_SOURCE_TEXTS and source_hash not in FAKE_SOURCE_HASHES:
            return True
    return False


def _has_qualified_foundation_source(value: dict[str, Any]) -> bool:
    """Return true only for material that may substantiate prior results/capability.

    Project guides, current drafts, public literature and generic user statements may
    define scope or plans, but they cannot prove that the applicant already completed
    work.  The stronger source-type rule is deliberately independent of model wording.
    """
    refs = value.get("source_refs") or []
    for ref in refs:
        if not isinstance(ref, dict) or ref.get("source_type") not in FOUNDATION_SOURCE_TYPES:
            continue
        quoted = str(ref.get("quoted_text") or "").strip()
        source_hash = str(ref.get("source_hash") or "")
        if quoted and quoted not in GENERIC_SOURCE_TEXTS and source_hash not in FAKE_SOURCE_HASHES:
            return True
    return False


def _qualified_foundation_ids(project_definition: dict[str, Any]) -> set[str]:
    return {
        str(item.get("item_id"))
        for item in project_definition.get("items", [])
        if isinstance(item, dict)
        and item.get("item_type") in {"ACHIEVEMENT", "CAPABILITY"}
        and item.get("knowledge_status") in {"CONFIRMED", "DOCUMENT_EXTRACTED"}
        and _has_qualified_foundation_source(item)
    }


class ProposalQualityGuard:
    """Model-independent structural validation for proposal-generation stages.

    This layer does not score literary style. It checks objective structural
    preconditions before a stage can be recorded as qualified.
    """

    CRITICAL_RESEARCH_TYPES = {
        "GAP", "PROBLEM", "OBJECTIVE", "WORK_PACKAGE", "METHOD", "EXPERIMENT",
        "INNOVATION", "DELIVERABLE", "METRIC", "ACHIEVEMENT", "CAPABILITY",
    }

    REQUIRED_SECTION_PROFILES = {
        "BACKGROUND_AND_SIGNIFICANCE", "LITERATURE_REVIEW", "RESEARCH_OBJECTIVE",
        "RESEARCH_CONTENT", "KEY_ISSUE", "TECHNICAL_ROUTE", "INNOVATION",
        "OUTPUTS_AND_METRICS", "RESEARCH_FOUNDATION",
    }

    CRITIC_DIMENSIONS = {
        "DOCUMENT_TYPE_FIT", "CENTRAL_THESIS", "ARGUMENT_CHAIN", "EVIDENCE_SUPPORT",
        "METHOD_SUBSTANCE", "INNOVATION_BASELINE", "FEASIBILITY_FOUNDATION",
        "METRIC_JUSTIFICATION", "SECTION_UNIQUENESS", "STYLE_AND_DENSITY",
    }

    def apply(self, prompt_id: str, envelope: dict[str, Any], output: dict[str, Any]) -> dict[str, Any]:
        payload = envelope.get("payload") or {}
        findings: list[QualityFinding] = []

        if prompt_id in {"P-PROJECT-DEFINITION-EXTRACT", "P-PROJECT-DEFINITION-CRITIC"}:
            pd = (
                output.get("result", {}).get("project_definition")
                if prompt_id == "P-PROJECT-DEFINITION-EXTRACT"
                else payload.get("project_definition_candidate")
            ) or {}
            findings.extend(self._audit_project_definition(pd))

        elif prompt_id == "P-PROJECT-READINESS-CRITIC":
            findings.extend(self._audit_readiness(payload, output))

        elif prompt_id in {"P-TEMPLATE-EXTRACT", "P-TEMPLATE-CRITIC"}:
            template = (
                output.get("result", {}).get("template")
                if prompt_id == "P-TEMPLATE-EXTRACT"
                else payload.get("template_candidate")
            ) or {}
            findings.extend(self._audit_template(template))

        elif prompt_id in {"P-ARGUMENT-ARCHITECTURE", "P-ARGUMENT-ARCHITECTURE-CRITIC"}:
            architecture = (
                output.get("result")
                if prompt_id == "P-ARGUMENT-ARCHITECTURE"
                else payload.get("architecture_candidate")
            ) or {}
            findings.extend(self._audit_argument_architecture(architecture, output if prompt_id.endswith("CRITIC") else None))

        elif prompt_id in {"P-REVISION-PLAN", "P-REVISION-PLAN-CRITIC"}:
            plan = (
                output.get("result", {}).get("revision_plan")
                if prompt_id == "P-REVISION-PLAN"
                else payload.get("revision_plan_candidate")
            ) or {}
            findings.extend(self._audit_plan(plan, payload))

        elif prompt_id in {"P-WRITE-BLUEPRINT", "P-WRITE-BLUEPRINT-CRITIC"}:
            blueprint = (
                output.get("result", {}).get("blueprint")
                if prompt_id == "P-WRITE-BLUEPRINT"
                else payload.get("blueprint_candidate")
            ) or {}
            findings.extend(self._audit_blueprint(blueprint, payload))

        elif prompt_id == "P-WRITE-CONTENT":
            candidate = output.get("result") or {}
            findings.extend(self._audit_section_content(candidate, payload))

        elif prompt_id == "P-WRITE-CRITIC":
            candidate = _candidate_from_payload(payload)
            findings.extend(self._audit_section_content(candidate, payload))
            findings.extend(self._audit_critic_coverage(candidate, output, payload))

        elif prompt_id == "P-EXPRESSION-POLISH":
            findings.extend(self._audit_expression_preservation(payload.get("content_candidate") or {}, output.get("result") or {}))

        elif prompt_id == "P-EXPRESSION-CRITIC":
            findings.extend(self._audit_expression_preservation(payload.get("content_candidate") or {}, payload.get("polished_candidate") or {}))
            findings.extend(self._audit_critic_coverage(payload.get("polished_candidate") or {}, output, None))

        elif prompt_id == "P-INTEGRATION-CRITIC":
            findings.extend(self._audit_document(payload, output))

        self._merge_findings(output, findings)
        return output

    def _audit_project_definition(self, pd: dict[str, Any]) -> list[QualityFinding]:
        findings: list[QualityFinding] = []
        types = _item_types(pd)
        missing = sorted(self.CRITICAL_RESEARCH_TYPES - set(types))
        if missing:
            findings.append(QualityFinding(
                "QG_PROJECT_GRAPH_INCOMPLETE", "P1", "PROJECT_DEFINITION", "PROJECT_DEFINITION",
                "items", f"研究项目知识图谱缺少关键对象类型：{', '.join(missing)}。只有目标或系统功能不能构成可写的科研项目定义。",
                "从材料中分别抽取研究差距、研究问题、目标、任务、方法、实验、创新、成果、指标和研究基础；缺失项保持UNKNOWN并阻断写作。",
                "PROJECT_KNOWLEDGE_AGENT",
            ))
        if sum(types.values()) < 10:
            findings.append(QualityFinding(
                "QG_PROJECT_GRAPH_TOO_SHALLOW", "P1", "PROJECT_DEFINITION", "PROJECT_DEFINITION",
                "items", f"项目定义仅含{sum(types.values())}个对象，无法支撑完整研究论证。",
                "扩展为具有多类型节点和真实关系的项目论证图，而不是用一个OBJECTIVE代表整个项目。",
                "PROJECT_KNOWLEDGE_AGENT",
            ))
        confirmed_without_source = [
            str(item.get("item_id")) for item in pd.get("items", [])
            if isinstance(item, dict)
            and item.get("knowledge_status") in {"CONFIRMED", "DOCUMENT_EXTRACTED"}
            and not _has_real_source(item)
        ]
        if confirmed_without_source:
            findings.append(QualityFinding(
                "QG_CONFIRMED_ITEM_WITHOUT_EVIDENCE", "P1", "SOURCE", "PROJECT_ITEM",
                "items", f"{len(confirmed_without_source)}个已确认对象没有真实来源，不能进入写作事实基线。",
                "将其降级为USER_ASSERTED/UNKNOWN，或补充可解析的文档Span与非占位Hash。",
                "PROJECT_KNOWLEDGE_AGENT",
            ))
        unsupported_foundation = [
            str(item.get("item_id")) for item in pd.get("items", [])
            if isinstance(item, dict)
            and item.get("item_type") in {"ACHIEVEMENT", "CAPABILITY"}
            and item.get("knowledge_status") in {"CONFIRMED", "DOCUMENT_EXTRACTED"}
            and not _has_qualified_foundation_source(item)
        ]
        if unsupported_foundation:
            findings.append(QualityFinding(
                "QG_FOUNDATION_STATUS_EXCEEDS_EVIDENCE", "P1", "SOURCE", "PROJECT_ITEM",
                "items", f"研究基础对象{unsupported_foundation}被标为已确认，但来源不是可定位的前期成果或技术材料。",
                "将对象降级为USER_ASSERTED/UNKNOWN，或补充EVIDENCE_MATERIAL/TECHNICAL_MATERIAL中的具体成果、原型、数据或预实验Span。",
                "PROJECT_KNOWLEDGE_AGENT",
            ))
        objectives = [i for i in pd.get("items", []) if isinstance(i, dict) and i.get("item_type") == "OBJECTIVE"]
        objective_text = " ".join(_texts([i.get("content") for i in objectives]))
        if objectives and re.search(r"构建.*系统|形成.*原型", objective_text) and not any(t in types for t in ["PROBLEM", "INNOVATION", "EXPERIMENT"]):
            findings.append(QualityFinding(
                "QG_ENGINEERING_OBJECTIVE_MASQUERADES_AS_RESEARCH", "P1", "PROJECT_DEFINITION", "OBJECTIVE",
                "items", "项目目标仅描述构建系统/原型，未由研究问题、新机制和验证命题支撑，存在文种漂移。",
                "先形成中心研究命题和可检验研究问题，再把原型系统降为验证载体或成果，而不是研究目标本身。",
                "PROJECT_KNOWLEDGE_AGENT",
            ))
        return findings

    def _audit_readiness(self, payload: dict[str, Any], output: dict[str, Any]) -> list[QualityFinding]:
        findings = self._audit_project_definition(payload.get("project_definition") or {})
        result = output.get("result") or {}
        stage = str(payload.get("readiness_stage") or "READY_FOR_ARGUMENT_ARCHITECTURE")
        if result.get("assessed_stage") != stage:
            findings.append(QualityFinding(
                "QG_READINESS_STAGE_MISMATCH", "P1", "READINESS", "READINESS_REPORT",
                "result.assessed_stage", "准备度报告没有按当前工作流阶段进行评价。",
                "按输入readiness_stage重新评估，不得用前一阶段的结论替代章节规划准备度。",
                "PROJECT_KNOWLEDGE_AGENT",
            ))
        if stage == "READY_FOR_ARGUMENT_ARCHITECTURE":
            if not result.get("ready_for_argument_architecture", False):
                findings.append(QualityFinding(
                    "QG_ARGUMENT_ARCHITECTURE_NOT_READY", "P1", "READINESS", "READINESS_REPORT",
                    "result.ready_for_argument_architecture", "项目事实和初始问题尚不足以进入论证架构阶段。",
                    "补充申报规则、问题边界、已有事实和待研究对象；缺失内容保持未确认。",
                    "PROJECT_KNOWLEDGE_AGENT",
                ))
            return findings

        writable = set(result.get("writeable_section_profiles") or [])
        foundation_ids = _qualified_foundation_ids(payload.get("project_definition") or {})
        graph = payload.get("argument_graph") or {}
        supported_team_nodes = [
            node for node in graph.get("nodes", [])
            if isinstance(node, dict) and node.get("node_type") == "TEAM_EVIDENCE"
            and node.get("status") in {"SUPPORTED", "CONFIRMED"}
        ]
        invalid_team_nodes = [str(node.get("node_id")) for node in supported_team_nodes if not _has_qualified_foundation_source(node)]
        if not foundation_ids or invalid_team_nodes:
            if "RESEARCH_FOUNDATION" in writable or result.get("ready_for_section_planning", False):
                findings.append(QualityFinding(
                    "QG_FOUNDATION_FALSE_READY", "P1", "READINESS", "READINESS_REPORT",
                    "result.writeable_section_profiles",
                    f"研究基础缺少合格前期证据；无效TEAM_EVIDENCE节点：{invalid_team_nodes or '无合格成果对象'}，却被标为可写或允许进入章节规划。",
                    "移除RESEARCH_FOUNDATION可写状态，将相关节点改为UNKNOWN，并向负责人请求成果、原型、数据或预实验材料。",
                    "PROJECT_KNOWLEDGE_AGENT",
                ))
        if not self.REQUIRED_SECTION_PROFILES.issubset(writable) or not result.get("ready_for_section_planning", False):
            findings.append(QualityFinding(
                "QG_FALSE_READINESS", "P1", "READINESS", "READINESS_REPORT",
                "result.writeable_section_profiles", "论证架构尚未覆盖申请书核心章节，却允许进入章节规划。",
                "按章节Profile核对中心命题、研究问题、最近工作、方法、验证、创新和基础；缺失项阻止章节规划。",
                "ARGUMENT_ARCHITECTURE_AGENT",
            ))
        return findings

    def _audit_template(self, template: dict[str, Any]) -> list[QualityFinding]:
        findings: list[QualityFinding] = []
        components = template.get("components") or []
        argument_patterns = template.get("argument_patterns") or []
        expression_patterns = template.get("expression_patterns") or []
        if len(components) < 5 or (not argument_patterns and not expression_patterns):
            findings.append(QualityFinding(
                "QG_TEMPLATE_ONLY_FORMAT", "P1", "TEMPLATE", "TEMPLATE",
                "components", "模板提取仅给出章节顺序或格式规则，没有提炼问题收束、证据推进、段落功能和表达策略。",
                "从多份优秀范例提取论证主线、claim-evidence-warrant模式、章节输入输出、转承方式和反面模式。",
                "ORIGINAL_PRODUCER",
            ))
        format_rules = " ".join(str(x) for x in template.get("format_rules") or [])
        if any(token in format_rules for token in ["章节完整", "包含图表", "参考文献不少于"]):
            findings.append(QualityFinding(
                "QG_TEMPLATE_VOLUME_PROXY", "P2", "TEMPLATE", "TEMPLATE",
                "format_rules", "模板质量规则使用章节数、图表和参考文献数量作为质量代理，容易诱导堆砌。",
                "改为论证闭环、最接近工作对比、方法可检验性、证据密度和篇幅预算。",
                "ORIGINAL_PRODUCER", blocking=False,
            ))
        return findings

    def _audit_argument_architecture(self, architecture: dict[str, Any], critic_output: dict[str, Any] | None = None) -> list[QualityFinding]:
        findings: list[QualityFinding] = []
        graph = architecture.get("argument_architecture") or {}
        matrix = architecture.get("research_design_matrix") or []
        proposition = graph.get("central_proposition") or {}
        questions = graph.get("research_questions") or []
        nodes = graph.get("nodes") or []
        node_ids = {str(n.get("node_id")) for n in nodes if isinstance(n, dict) and n.get("node_id")}
        node_ids.add(str(proposition.get("node_id") or ""))
        node_ids.update(str(q.get("node_id")) for q in questions if isinstance(q, dict) and q.get("node_id"))
        types = collections.Counter(str(n.get("node_type")) for n in nodes if isinstance(n, dict))
        required_types = {"RESEARCH_GAP", "CLOSEST_PRIOR_WORK", "OBJECTIVE", "WORK_PACKAGE", "FORMAL_MODEL", "EXPERIMENT_DESIGN", "NOVEL_MECHANISM", "TEAM_EVIDENCE"}
        missing_types = sorted(required_types - set(types))
        if not proposition.get("falsifiable_or_comparable") or not str(proposition.get("statement") or "").strip():
            findings.append(QualityFinding("QG_ARGUMENT_NO_TESTABLE_PROPOSITION", "P1", "ARGUMENT", "ARGUMENT_GRAPH", "argument_architecture.central_proposition", "中心命题不可比较或不可检验。", "将中心命题改写为具有边界条件、基线和验证方式的技术原理。", "ARGUMENT_ARCHITECTURE_AGENT"))
        if not (1 <= len(questions) <= 4):
            findings.append(QualityFinding("QG_ARGUMENT_QUESTION_COUNT", "P1", "ARGUMENT", "ARGUMENT_GRAPH", "argument_architecture.research_questions", f"研究问题数量为{len(questions)}，不符合1至4个核心问题的收束要求。", "合并功能性问题，只保留由研究差距直接触发的核心问题。", "ARGUMENT_ARCHITECTURE_AGENT"))
        if missing_types:
            findings.append(QualityFinding("QG_ARGUMENT_NODE_TYPES_INCOMPLETE", "P1", "ARGUMENT", "ARGUMENT_GRAPH", "argument_architecture.nodes", f"论证图谱缺少：{', '.join(missing_types)}。", "补齐最近工作、形式化方法、验证、创新和前期证据节点；缺证据时保持未就绪。", "ARGUMENT_ARCHITECTURE_AGENT"))
        matrix_qids = {str(x.get("research_question_id")) for x in matrix if isinstance(x, dict)}
        question_ids = {str(x.get("node_id")) for x in questions if isinstance(x, dict)}
        if matrix_qids != question_ids:
            findings.append(QualityFinding("QG_ARGUMENT_MATRIX_COVERAGE", "P1", "ARGUMENT", "RESEARCH_DESIGN_MATRIX", "research_design_matrix", "研究设计矩阵未一一覆盖全部研究问题。", "每个研究问题分别绑定目标、任务、方法、验证、创新、最近工作和比较规则。", "ARGUMENT_ARCHITECTURE_AGENT"))
        for index, item in enumerate(matrix):
            if not isinstance(item, dict):
                continue
            required = ["gap_ids", "objective_ids", "work_package_ids", "method_ids", "evaluation_ids", "innovation_ids", "closest_prior_work_ids"]
            missing = [k for k in required if not item.get(k)]
            referenced = {str(v) for k in required for v in (item.get(k) or [])}
            unknown = sorted(x for x in referenced if x not in node_ids and x not in question_ids)
            if missing or unknown:
                findings.append(QualityFinding("QG_ARGUMENT_MATRIX_INCOMPLETE", "P1", "ARGUMENT", "RESEARCH_DESIGN_MATRIX", f"research_design_matrix[{index}]", f"研究问题矩阵缺少维度{missing}或引用未知节点{unknown}。", "使用真实图谱节点补齐研究设计，不得生成独立占位ID。", "ARGUMENT_ARCHITECTURE_AGENT"))
        team_nodes = [node for node in nodes if isinstance(node, dict) and node.get("node_type") == "TEAM_EVIDENCE"]
        unsupported_team_nodes = [
            str(node.get("node_id")) for node in team_nodes
            if node.get("status") in {"SUPPORTED", "CONFIRMED"} and not _has_qualified_foundation_source(node)
        ]
        if unsupported_team_nodes:
            findings.append(QualityFinding(
                "QG_ARGUMENT_FOUNDATION_UNSUPPORTED", "P1", "SOURCE", "ARGUMENT_GRAPH",
                "argument_architecture.nodes", f"TEAM_EVIDENCE节点{unsupported_team_nodes}没有合格前期材料，却被作为可行性证据。",
                "将节点状态改为UNKNOWN，清空其作为已证实基础的关系，并生成阻断性证据缺口。",
                "ARGUMENT_ARCHITECTURE_AGENT",
            ))
        blocking = [x for x in architecture.get("evidence_gap_report") or [] if isinstance(x, dict) and x.get("blocking")]
        if blocking and (architecture.get("readiness") or {}).get("ready"):
            findings.append(QualityFinding("QG_ARGUMENT_FALSE_READY", "P1", "READINESS", "ARGUMENT_ARCHITECTURE", "readiness.ready", "存在阻断性证据缺口但论证架构仍标记为可写。", "将readiness设为false，并把缺口转为材料补充任务。", "ARGUMENT_ARCHITECTURE_AGENT"))
        if critic_output is not None:
            result = critic_output.get("result") or {}
            checked = {str(x) for x in result.get("checked_node_ids") or []}
            expected = {x for x in node_ids if x}
            if checked != expected:
                findings.append(QualityFinding("QG_ARGUMENT_CRITIC_PARTIAL", "P1", "ARGUMENT", "ARGUMENT_CRITIC", "result.checked_node_ids", f"论证Critic仅检查{len(checked)}/{len(expected)}个节点。", "逐节点核查全部研究问题、方法、验证、创新和基础节点。", "ORIGINAL_PRODUCER"))
            if len(result.get("chain_checks") or []) < 7:
                findings.append(QualityFinding("QG_ARGUMENT_CRITIC_CHAIN_SCOPE", "P1", "ARGUMENT", "ARGUMENT_CRITIC", "result.chain_checks", "论证Critic没有覆盖七条核心关系链。", "补齐差距到问题、问题到目标、目标到任务、任务到方法、方法到验证、最近工作到创新、基础到可行性检查。", "ORIGINAL_PRODUCER"))
            scorecard = {str(item.get("dimension")): item for item in result.get("quality_dimensions") or [] if isinstance(item, dict)}
            required = {"CENTRAL_THESIS", "ARGUMENT_CHAIN", "EVIDENCE_SUPPORT", "METHOD_SUBSTANCE", "INNOVATION_BASELINE", "FEASIBILITY_FOUNDATION", "METRIC_JUSTIFICATION"}
            invalid = sorted(dim for dim in required if dim not in scorecard or not scorecard[dim].get("passed", False) or float(scorecard[dim].get("score", 0)) < 3)
            if invalid:
                findings.append(QualityFinding(
                    "QG_ARGUMENT_CRITIC_SCORECARD_INCOMPLETE", "P1", "ARGUMENT", "ARGUMENT_CRITIC",
                    "result.quality_dimensions", f"论证Critic缺少或未通过质量维度：{', '.join(invalid)}。",
                    "补齐中心命题、论证链、证据、方法、创新基线、可行性和指标依据审查。",
                    "ORIGINAL_PRODUCER",
                ))
        return findings

    def _audit_expression_preservation(self, original: dict[str, Any], polished: dict[str, Any]) -> list[QualityFinding]:
        findings: list[QualityFinding] = []
        original_ids = {str(p.get("paragraph_id")) for p in original.get("paragraphs", []) if isinstance(p, dict) and p.get("paragraph_id")}
        polished_ids = {str(p.get("paragraph_id")) for p in polished.get("paragraphs", []) if isinstance(p, dict) and p.get("paragraph_id")}
        original_traces = {str(t.get("trace_id")) for t in original.get("trace_links", []) if isinstance(t, dict) and t.get("trace_id")}
        polished_traces = {str(t.get("trace_id")) for t in polished.get("trace_links", []) if isinstance(t, dict) and t.get("trace_id")}
        if original_ids != polished_ids:
            findings.append(QualityFinding("QG_EXPRESSION_PARAGRAPH_ID_CHANGED", "P1", "EXPRESSION", "POLISHED_CANDIDATE", "paragraphs", "表达编辑改变了段落集合或段落ID，无法证明仅修改表达。", "恢复原段落ID；实质性增删必须返回写作阶段。", "EXPRESSION_EDITOR_AGENT"))
        if original_traces != polished_traces:
            findings.append(QualityFinding("QG_EXPRESSION_TRACE_CHANGED", "P1", "SOURCE", "POLISHED_CANDIDATE", "trace_links", "表达编辑新增或丢失了来源关系。", "保持输入输出Trace集合完全一致；需要新增证据时返回项目知识阶段。", "EXPRESSION_EDITOR_AGENT"))
        original_by_id = {str(p.get("paragraph_id")): p for p in original.get("paragraphs", []) if isinstance(p, dict) and p.get("paragraph_id")}
        polished_by_id = {str(p.get("paragraph_id")): p for p in polished.get("paragraphs", []) if isinstance(p, dict) and p.get("paragraph_id")}
        immutable_fields = (
            "blueprint_paragraph_id", "paragraph_role", "primary_claim_id",
            "novel_content_key", "section_contract_id",
        )
        changed_fields: list[str] = []
        for paragraph_id in sorted(original_ids & polished_ids):
            before = original_by_id[paragraph_id]
            after = polished_by_id[paragraph_id]
            for field in immutable_fields:
                if before.get(field) != after.get(field):
                    changed_fields.append(f"{paragraph_id}.{field}")
            if sorted(str(x) for x in before.get("evidence_ids", []) if x) != sorted(str(x) for x in after.get("evidence_ids", []) if x):
                changed_fields.append(f"{paragraph_id}.evidence_ids")
            if sorted(str(x) for x in before.get("trace_link_ids", []) if x) != sorted(str(x) for x in after.get("trace_link_ids", []) if x):
                changed_fields.append(f"{paragraph_id}.trace_link_ids")
        if original.get("claim_advancement") != polished.get("claim_advancement"):
            changed_fields.append("claim_advancement")
        if changed_fields:
            findings.append(QualityFinding(
                "QG_EXPRESSION_SEMANTIC_IDENTITY_CHANGED", "P1", "EXPRESSION", "POLISHED_CANDIDATE",
                ",".join(changed_fields[:20]),
                "表达编辑改变了段落命题、证据、信息键、章节合同或章节推进摘要，已经超出语言润色范围。",
                "恢复全部语义身份字段；需要改变论点或证据时退回蓝图/证据写作阶段重新审查。",
                "EXPRESSION_EDITOR_AGENT",
            ))
        for entry in polished.get("edit_log") or []:
            if isinstance(entry, dict) and not entry.get("meaning_preserved", False):
                findings.append(QualityFinding("QG_EXPRESSION_MEANING_CHANGED", "P1", "EXPRESSION", "POLISHED_CANDIDATE", "edit_log", "表达编辑声明存在含义变化。", "将该段退回证据写作Agent，不得在表达阶段修改实质内容。", "WRITING_AGENT"))
                break
        return findings

    def _audit_plan(self, plan: dict[str, Any], payload: dict[str, Any]) -> list[QualityFinding]:
        findings: list[QualityFinding] = []
        targets = plan.get("target_section_ids") or []
        tasks = plan.get("tasks") or []
        architecture = plan.get("narrative_architecture") or {}
        if len(targets) > 30:
            findings.append(QualityFinding(
                "QG_PLAN_DOCUMENT_BLOAT", "P1", "PLAN", "REVISION_PLAN",
                "target_section_ids", f"计划把{len(targets)}个章节全部作为主申请书正文，没有区分核心正文与技术附件。",
                "依据申报类型设置主文页数预算；部署、接口、数据字典、Skill和审计细节移入附件。",
                "PLANNING_AGENT",
            ))
        issue_text = " ".join(str(item.get("description") or "") for item in plan.get("issues") or [] if isinstance(item, dict))
        if "完整申请书" in issue_text or "覆盖研究现状" in issue_text:
            findings.append(QualityFinding(
                "QG_PLAN_COVERAGE_OVER_ARGUMENT", "P1", "PLAN", "REVISION_PLAN",
                "issues", "计划把“覆盖更多章节”当作首要问题，没有把中心命题和评审说服链作为优化目标。",
                "首先确定唯一中心命题、最多4个研究问题和对应证据/方法/验证，再规划章节。",
                "PLANNING_AGENT",
            ))
        if not architecture:
            findings.append(QualityFinding(
                "QG_PLAN_NO_NARRATIVE_ARCHITECTURE", "P1", "PLAN", "REVISION_PLAN",
                "narrative_architecture", "计划缺少中心命题、研究问题、章节功能、页数预算和附件边界。",
                "生成Narrative Architecture，并在写作前由独立Critic验证问题—目标—任务—方法—实验—成果闭环。",
                "PLANNING_AGENT",
            ))
        objectives = [str(task.get("objective") or "") for task in tasks if isinstance(task, dict)]
        if objectives and len(set(re.sub(r"《[^》]+》", "《章节》", x) for x in objectives)) <= 2:
            findings.append(QualityFinding(
                "QG_PLAN_TASKS_TEMPLATE_CLONED", "P1", "PLAN", "REVISION_PLAN",
                "tasks", "大部分章节任务只替换标题，验收规则与论证功能没有区分。",
                "按章节Profile生成不同任务：现状需比较矩阵，创新需closest-work对比，方法需形式化模型，基础需前期证据。",
                "PLANNING_AGENT",
            ))

        contracts = [item for item in architecture.get("section_contracts") or [] if isinstance(item, dict)]
        contract_ids = [str(item.get("section_contract_id") or "") for item in contracts]
        section_ids = [str(item.get("section_id") or "") for item in contracts]
        if len(contract_ids) != len(set(contract_ids)) or len(section_ids) != len(set(section_ids)):
            findings.append(QualityFinding(
                "QG_PLAN_SECTION_CONTRACT_ID_DUPLICATE", "P1", "PLAN", "REVISION_PLAN",
                "narrative_architecture.section_contracts", "Section Contract存在重复合同ID或重复章节ID，后续无法建立一一映射。",
                "为每个计划章节分配唯一section_contract_id和section_id。", "PLANNING_AGENT",
            ))
        main_contracts = [item for item in contracts if item.get("placement") == "MAIN_BODY"]
        profiles = [str(item.get("profile_id") or "") for item in main_contracts if item.get("profile_id")]
        if len(main_contracts) >= 4 and len(set(profiles)) < 3:
            findings.append(QualityFinding(
                "QG_PLAN_SECTION_PROFILE_HOMOGENEOUS", "P1", "PLAN", "REVISION_PLAN",
                "narrative_architecture.section_contracts.profile_id",
                f"{len(main_contracts)}个主文章节仅使用{len(set(profiles))}类Section Profile，章节功能区分不足。",
                "按立项依据、现状、问题、目标、内容、方法、验证、创新和基础等真实功能分配专用Profile。",
                "PLANNING_AGENT",
            ))
        information_owners: dict[str, list[str]] = collections.defaultdict(list)
        for contract in contracts:
            sid = str(contract.get("section_id") or "")
            for key in contract.get("unique_information_keys") or []:
                if key:
                    information_owners[str(key)].append(sid)
        duplicate_keys = {key: owners for key, owners in information_owners.items() if len(set(owners)) > 1}
        if duplicate_keys:
            findings.append(QualityFinding(
                "QG_PLAN_INFORMATION_KEY_MULTIPLE_OWNERS", "P1", "PLAN", "REVISION_PLAN",
                "narrative_architecture.section_contracts.unique_information_keys",
                f"{len(duplicate_keys)}个新增信息键被分配给多个章节。",
                "为每个新增信息键指定唯一主责章节；其他章节只能通过allowed_shared_context_ids引用。",
                "PLANNING_AGENT",
            ))
        known_sections = {sid for sid in section_ids if sid}
        dependency_graph: dict[str, set[str]] = {}
        invalid_dependencies: list[str] = []
        for contract in contracts:
            sid = str(contract.get("section_id") or "")
            prereqs = {str(x) for x in contract.get("prerequisite_section_ids") or [] if x}
            no_repeat = {str(x) for x in contract.get("must_not_repeat_section_ids") or [] if x}
            dependency_graph[sid] = prereqs
            invalid = sorted((prereqs | no_repeat) - known_sections)
            if sid in prereqs or sid in no_repeat:
                invalid.append(sid)
            invalid_dependencies.extend(f"{sid}->{target}" for target in invalid)
        if invalid_dependencies:
            findings.append(QualityFinding(
                "QG_PLAN_SECTION_REFERENCE_INVALID", "P1", "PLAN", "REVISION_PLAN",
                "narrative_architecture.section_contracts",
                f"章节合同存在{len(invalid_dependencies)}个无效或自指的前置/禁止重复关系。",
                "仅引用本次论证架构中真实存在的其他章节ID。", "PLANNING_AGENT",
            ))

        visiting: set[str] = set()
        visited: set[str] = set()
        has_cycle = False
        def visit(node: str) -> None:
            nonlocal has_cycle
            if node in visiting:
                has_cycle = True
                return
            if node in visited or has_cycle:
                return
            visiting.add(node)
            for parent in dependency_graph.get(node, set()):
                visit(parent)
            visiting.remove(node)
            visited.add(node)
        for node in dependency_graph:
            visit(node)
        if has_cycle:
            findings.append(QualityFinding(
                "QG_PLAN_SECTION_DEPENDENCY_CYCLE", "P1", "PLAN", "REVISION_PLAN",
                "narrative_architecture.section_contracts.prerequisite_section_ids",
                "章节前置关系存在环，无法形成稳定的论证推进顺序。",
                "重排章节合同，使前置关系形成有向无环图。", "PLANNING_AGENT",
            ))
        main_word_budget = sum(int(item.get("word_budget") or 0) for item in main_contracts)
        declared_budget = int(architecture.get("main_body_word_budget") or 0)
        if declared_budget and main_word_budget > declared_budget:
            findings.append(QualityFinding(
                "QG_PLAN_SECTION_BUDGET_OVERFLOW", "P1", "PLAN", "REVISION_PLAN",
                "narrative_architecture.section_contracts.word_budget",
                f"主文章节预算合计{main_word_budget}字，超过全文预算{declared_budget}字。",
                "压缩或附件化次要章节，保持章节预算总和不超过主文预算。", "PLANNING_AGENT",
            ))
        return findings

    def _audit_blueprint(self, blueprint: dict[str, Any], payload: dict[str, Any]) -> list[QualityFinding]:
        findings: list[QualityFinding] = []
        paragraphs = blueprint.get("paragraphs") or []
        functions = [str(p.get("function") or "") for p in paragraphs if isinstance(p, dict)]
        generic = sum(1 for x in functions if x in GENERIC_SECTION_HEADINGS or any(h in x for h in GENERIC_SECTION_HEADINGS))
        if paragraphs and generic / max(1, len(paragraphs)) >= 0.35:
            findings.append(QualityFinding(
                "QG_BLUEPRINT_GENERIC_SIX_PART_TEMPLATE", "P1", "BLUEPRINT", "BLUEPRINT",
                "paragraphs", "蓝图大量复用“定位—问题—方法—实施—指标—输出”通用骨架，未体现章节独有论证功能。",
                "根据section_profile生成claim-evidence-warrant段落，并禁止同一通用骨架跨章节复用。",
                "WRITING_AGENT",
            ))
        slot_signatures = []
        for p in paragraphs:
            if not isinstance(p, dict):
                continue
            slot_signatures.append((tuple(p.get("fact_slots") or []), tuple(p.get("project_item_slots") or [])))
        if len(slot_signatures) >= 4 and len(set(slot_signatures)) <= 1:
            findings.append(QualityFinding(
                "QG_BLUEPRINT_SINGLE_SOURCE_FOR_ALL_PARAGRAPHS", "P1", "BLUEPRINT", "BLUEPRINT",
                "paragraphs", "所有段落绑定同一个事实和项目对象，无法支撑不同论点。",
                "每个段落明确primary_claim、evidence、warrant和反证/边界；没有证据的槽位必须阻断。",
                "WRITING_AGENT",
            ))
        profile = payload.get("section_profile") or {}
        if str(profile.get("profile_id")) == "RESEARCH_CONTENT" and _section_title(payload) not in {"研究内容", "研究内容与任务分解"}:
            findings.append(QualityFinding(
                "QG_WRONG_SECTION_PROFILE", "P1", "BLUEPRINT", "SECTION_PROFILE",
                "section_profile.profile_id", f"章节《{_section_title(payload)}》错误使用RESEARCH_CONTENT Profile。",
                "由确定性标题映射选择LITERATURE_REVIEW/INNOVATION/TECHNICAL_ROUTE/FOUNDATION等专用Profile。",
                "PLANNING_AGENT",
            ))

        contract = payload.get("section_contract") or {}
        contract_id = str(contract.get("section_contract_id") or "")
        contract_keys = [str(x) for x in contract.get("unique_information_keys") or []]
        required_roles = {str(x) for x in contract.get("required_argument_roles") or []}
        actual_roles = {str(p.get("argument_role") or "") for p in paragraphs if isinstance(p, dict)}
        paragraph_keys = [str(p.get("novel_content_key") or "") for p in paragraphs if isinstance(p, dict)]
        paragraph_claims = {str(p.get("primary_claim_id") or "") for p in paragraphs if isinstance(p, dict)}
        required_claims = {str(x) for x in contract.get("must_advance_claim_ids") or []}
        prior_digests = payload.get("prior_section_digest") or []
        prior_keys = {
            str(key)
            for digest in prior_digests if isinstance(digest, dict)
            for key in digest.get("new_information_keys") or []
        }

        if contract_id and not all(paragraph_keys):
            findings.append(QualityFinding(
                "QG_BLUEPRINT_MISSING_INFORMATION_IDENTITY", "P1", "BLUEPRINT", "BLUEPRINT",
                "paragraphs.novel_content_key", "蓝图段落缺少新增信息键，后续无法判断章节是否推进了新内容。",
                "为每个段落指定属于本章节合同的novel_content_key。", "WRITING_AGENT",
            ))
        if len(paragraph_keys) != len(set(paragraph_keys)):
            findings.append(QualityFinding(
                "QG_BLUEPRINT_DUPLICATE_INFORMATION_KEYS", "P1", "BLUEPRINT", "BLUEPRINT",
                "paragraphs.novel_content_key", "同一章节内多个段落复用了相同新增信息键。",
                "每个段落只推进一个独立信息单元，并使用唯一novel_content_key。", "WRITING_AGENT",
            ))
        foreign_keys = sorted(
            key for key in paragraph_keys if key and contract_keys and not any(key == root or key.startswith(root + "-") or key.startswith(root + ":") for root in contract_keys)
        )
        if foreign_keys:
            findings.append(QualityFinding(
                "QG_BLUEPRINT_INFORMATION_KEY_OUTSIDE_CONTRACT", "P1", "BLUEPRINT", "BLUEPRINT",
                "paragraphs.novel_content_key", f"有{len(foreign_keys)}个新增信息键不属于本章节合同。",
                "仅使用section_contract.unique_information_keys及其子键。", "WRITING_AGENT",
            ))
        reused_prior = sorted(set(paragraph_keys) & prior_keys)
        if reused_prior:
            findings.append(QualityFinding(
                "QG_BLUEPRINT_REUSES_PRIOR_INFORMATION", "P1", "BLUEPRINT", "BLUEPRINT",
                "paragraphs.novel_content_key", f"蓝图复用了前文章节的{len(reused_prior)}个信息键。",
                "更换为本章节独有信息键；共享背景只能通过allowed_shared_context_ids引用。", "WRITING_AGENT",
            ))
        missing_roles = sorted(required_roles - actual_roles)
        if missing_roles:
            findings.append(QualityFinding(
                "QG_BLUEPRINT_REQUIRED_ROLES_MISSING", "P1", "BLUEPRINT", "BLUEPRINT",
                "paragraphs.argument_role", f"蓝图缺少章节合同要求的论证角色：{', '.join(missing_roles)}。",
                "补齐章节Profile要求的论证角色，不得用通用段落替代。", "WRITING_AGENT",
            ))
        missing_claims = sorted(required_claims - paragraph_claims)
        if missing_claims:
            findings.append(QualityFinding(
                "QG_BLUEPRINT_REQUIRED_CLAIMS_MISSING", "P1", "BLUEPRINT", "BLUEPRINT",
                "paragraphs.primary_claim_id", f"蓝图没有推进章节合同要求的{len(missing_claims)}个命题。",
                "将must_advance_claim_ids逐项绑定到至少一个段落。", "WRITING_AGENT",
            ))
        return findings

    def _audit_section_content(self, candidate: dict[str, Any], payload: dict[str, Any]) -> list[QualityFinding]:
        findings: list[QualityFinding] = []
        paragraphs = [p for p in candidate.get("paragraphs", []) if isinstance(p, dict)]
        text = _content_text(candidate)
        if not text:
            return [QualityFinding(
                "QG_EMPTY_SECTION", "P1", "CONTENT", "SECTION_CANDIDATE", "candidate_text",
                "章节正文为空。", "重新生成正文。", "WRITING_AGENT",
            )]
        paragraph_texts = [str(p.get("text") or "").strip() for p in paragraphs]
        duplicate_count = sum(count - 1 for text, count in collections.Counter(paragraph_texts).items() if count > 1 and len(text) >= 20)
        sentence_counts = collections.Counter(_normalized_sentences(text))
        repeated_sentences = [(s, n) for s, n in sentence_counts.items() if n >= 2]
        if duplicate_count or repeated_sentences:
            findings.append(QualityFinding(
                "QG_SECTION_INTERNAL_REPETITION", "P1", "CONTENT", "SECTION_CANDIDATE",
                "paragraphs", f"章节存在重复段落/句群（重复段落实例{duplicate_count}，重复长句{len(repeated_sentences)}）。",
                "删除模板化复述，每个段落只承担一个新的论证功能。",
                "WRITING_AGENT",
            ))
        meta_hits = sum(text.count(term) for term in HIGH_RISK_META_TERMS)
        main_profile = str((payload.get("section_profile") or {}).get("profile_id") or "")
        if meta_hits >= 3 and main_profile not in {"SYSTEM_IMPLEMENTATION", "APPENDIX", "DEPLOYMENT"}:
            findings.append(QualityFinding(
                "QG_DOCUMENT_TYPE_DRIFT_TO_SYSTEM_MANUAL", "P1", "CONTENT", "SECTION_CANDIDATE",
                "candidate_text", f"正文出现{meta_hits}次Prompt/Trace/Gate/部署等系统验收术语，研究申请书发生文种漂移。",
                "主申请书只保留与研究方法和验证直接相关的系统内容；流程、部署、审计细节移至附件。",
                "WRITING_AGENT",
            ))
        # Trace existence is not evidence quality. Require source diversity and actual known IDs.
        traces = candidate.get("trace_links") or []
        source_ids = {str(t.get("source_id")) for t in traces if isinstance(t, dict) and t.get("source_id")}
        allowed_ids = {
            str(item.get("item_id")) for item in (payload.get("project_subgraph") or {}).get("items", []) if isinstance(item, dict)
        }
        allowed_ids.update(str(item.get("claim_id")) for item in payload.get("confirmed_facts") or [] if isinstance(item, dict))
        argument_graph = payload.get("argument_graph") or {}
        proposition = argument_graph.get("central_proposition") or {}
        if proposition.get("node_id"):
            allowed_ids.add(str(proposition["node_id"]))
        allowed_ids.update(str(item.get("node_id")) for item in argument_graph.get("research_questions", []) if isinstance(item, dict) and item.get("node_id"))
        allowed_ids.update(str(item.get("node_id")) for item in argument_graph.get("nodes", []) if isinstance(item, dict) and item.get("node_id"))
        section_contract = payload.get("section_contract") or {}
        if section_contract.get("section_contract_id"):
            allowed_ids.add(str(section_contract["section_contract_id"]))
        allowed_ids.add(str((payload.get("source_section") or {}).get("section_id") or ""))
        invalid = sorted(x for x in source_ids if x and x not in allowed_ids)
        if invalid:
            findings.append(QualityFinding(
                "QG_TRACE_POINTS_TO_NONEXISTENT_SOURCE", "P1", "SOURCE", "TRACE_LINK",
                "trace_links", f"Trace引用了{len(invalid)}个上下文中不存在的source_id；生成Hash不能替代真实来源。",
                "仅引用项目图谱、确认事实、公开Claim或原文Span中的真实ID，并校验Hash。",
                "WRITING_AGENT",
            ))

        contract_id = str(section_contract.get("section_contract_id") or "")
        required_claims = {str(x) for x in section_contract.get("must_advance_claim_ids") or []}
        contract_keys = [str(x) for x in section_contract.get("unique_information_keys") or []]
        required_roles = {str(x) for x in section_contract.get("required_argument_roles") or []}
        paragraph_contracts = {str(p.get("section_contract_id") or "") for p in paragraphs}
        paragraph_claims = {str(p.get("primary_claim_id") or "") for p in paragraphs}
        paragraph_keys = [str(p.get("novel_content_key") or "") for p in paragraphs]
        paragraph_roles = {str(p.get("paragraph_role") or "") for p in paragraphs}
        prior_keys = {
            str(key)
            for digest in payload.get("prior_section_digest") or [] if isinstance(digest, dict)
            for key in digest.get("new_information_keys") or []
        }
        advancement = candidate.get("claim_advancement") or {}
        advancement_claims = {str(x) for x in advancement.get("advanced_claim_ids") or []}
        advancement_keys = {str(x) for x in advancement.get("new_information_keys") or []}

        if contract_id and paragraph_contracts != {contract_id}:
            findings.append(QualityFinding(
                "QG_CONTENT_SECTION_CONTRACT_MISMATCH", "P1", "CONTENT", "SECTION_CANDIDATE",
                "paragraphs.section_contract_id", "正文段落没有全部绑定当前章节合同。",
                "所有段落必须保留当前section_contract_id。", "WRITING_AGENT",
            ))
        if required_claims - paragraph_claims:
            findings.append(QualityFinding(
                "QG_CONTENT_REQUIRED_CLAIMS_NOT_ADVANCED", "P1", "CONTENT", "SECTION_CANDIDATE",
                "paragraphs.primary_claim_id", "正文没有覆盖章节合同要求推进的全部命题。",
                "按must_advance_claim_ids补齐论证段落。", "WRITING_AGENT",
            ))
        if required_roles - paragraph_roles:
            findings.append(QualityFinding(
                "QG_CONTENT_REQUIRED_ROLES_MISSING", "P1", "CONTENT", "SECTION_CANDIDATE",
                "paragraphs.paragraph_role", "正文缺少章节合同要求的论证角色。",
                "依据section_profile补齐所需的证据、差距、方法、验证或贡献段落。", "WRITING_AGENT",
            ))
        foreign_keys = sorted(
            key for key in paragraph_keys if key and contract_keys and not any(key == root or key.startswith(root + "-") or key.startswith(root + ":") for root in contract_keys)
        )
        if foreign_keys:
            findings.append(QualityFinding(
                "QG_CONTENT_INFORMATION_KEY_OUTSIDE_CONTRACT", "P1", "CONTENT", "SECTION_CANDIDATE",
                "paragraphs.novel_content_key", f"正文出现{len(foreign_keys)}个不属于章节合同的信息键。",
                "仅展开本章节合同分配的信息单元。", "WRITING_AGENT",
            ))
        reused_prior = sorted(set(paragraph_keys) & prior_keys)
        if reused_prior:
            findings.append(QualityFinding(
                "QG_CONTENT_REUSES_PRIOR_INFORMATION", "P1", "CONTENT", "SECTION_CANDIDATE",
                "paragraphs.novel_content_key", f"正文复用了前文的{len(reused_prior)}个信息键。",
                "删除重复论述，改写为本章节独有的命题推进。", "WRITING_AGENT",
            ))
        if contract_id and str(advancement.get("section_contract_id") or "") != contract_id:
            findings.append(QualityFinding(
                "QG_CONTENT_ADVANCEMENT_CONTRACT_MISMATCH", "P1", "CONTENT", "SECTION_CANDIDATE",
                "claim_advancement.section_contract_id", "claim_advancement与当前章节合同不一致。",
                "由段落语义标识重新计算claim_advancement。", "WRITING_AGENT",
            ))
        if advancement_claims != paragraph_claims or advancement_keys != set(paragraph_keys):
            findings.append(QualityFinding(
                "QG_CONTENT_ADVANCEMENT_SUMMARY_INCONSISTENT", "P1", "CONTENT", "SECTION_CANDIDATE",
                "claim_advancement", "章节推进摘要与段落中的命题/信息键不一致。",
                "从段落primary_claim_id和novel_content_key确定性生成推进摘要。", "WRITING_AGENT",
            ))
        return findings

    def _audit_critic_coverage(self, candidate: dict[str, Any], output: dict[str, Any], payload: dict[str, Any] | None = None) -> list[QualityFinding]:
        findings: list[QualityFinding] = []
        result = output.get("result") or {}
        expected_ids = {str(p.get("paragraph_id")) for p in candidate.get("paragraphs", []) if isinstance(p, dict) and p.get("paragraph_id")}
        checked = set(str(x) for x in result.get("checked_paragraph_ids") or [])
        if expected_ids and checked != expected_ids:
            findings.append(QualityFinding(
                "QG_CRITIC_DID_NOT_READ_ALL_PARAGRAPHS", "P1", "CONTENT", "WRITE_CRITIC",
                "result.checked_paragraph_ids", f"Critic仅检查{len(checked)}/{len(expected_ids)}个段落。",
                "逐段返回检查结果；缺少任何段落ID时不得ACCEPT。",
                "ORIGINAL_PRODUCER",
            ))
        rules = {str(item.get("rule")) for item in result.get("profile_acceptance_results") or [] if isinstance(item, dict)}
        if len(rules) < 6:
            findings.append(QualityFinding(
                "QG_CRITIC_DIMENSIONS_TOO_SHALLOW", "P1", "CONTENT", "WRITE_CRITIC",
                "result.profile_acceptance_results", "正文Critic只检查结构和Trace，没有检查文种、中心命题、方法实质、创新、指标依据、基础和重复。",
                "按质量维度Schema逐项打分并提供段落级证据；任一核心维度不通过则REVISE/BLOCK。",
                "ORIGINAL_PRODUCER",
            ))
        if payload is not None:
            profile = payload.get("section_profile") or {}
            required_rules = {str(x) for x in profile.get("acceptance_rules") or [] if x}
            missing_rules = sorted(required_rules - rules)
            if missing_rules:
                findings.append(QualityFinding(
                    "QG_CRITIC_PROFILE_RULES_NOT_CHECKED", "P1", "CONTENT", "WRITE_CRITIC",
                    "result.profile_acceptance_results",
                    f"正文Critic未检查当前Section Profile中的{len(missing_rules)}条专用验收规则。",
                    "逐条复制并审查section_profile.acceptance_rules，提供段落级证据。",
                    "ORIGINAL_PRODUCER",
                ))
            scorecard = {str(item.get("dimension")): item for item in result.get("quality_dimensions") or [] if isinstance(item, dict)}
            profile_id = str(profile.get("profile_id") or "")
            required_dimensions = SECTION_COMMON_QUALITY_DIMENSIONS | SECTION_PROFILE_QUALITY_DIMENSIONS.get(profile_id, set())
            invalid_dimensions = sorted(
                dim for dim in required_dimensions
                if dim not in scorecard or not scorecard[dim].get("passed", False) or float(scorecard[dim].get("score", 0)) < 3
            )
            if invalid_dimensions:
                findings.append(QualityFinding(
                    "QG_CRITIC_REQUIRED_SCORECARD_MISSING", "P1", "CONTENT", "WRITE_CRITIC",
                    "result.quality_dimensions",
                    f"正文Critic缺少或未通过章节所需质量维度：{', '.join(invalid_dimensions)}。",
                    "按章节Profile补齐质量维度；分数低于3或未通过时不得ACCEPT。",
                    "ORIGINAL_PRODUCER",
                ))
        return findings

    def _audit_document(self, payload: dict[str, Any], output: dict[str, Any]) -> list[QualityFinding]:
        findings: list[QualityFinding] = []
        sections = payload.get("candidate_sections") or []
        section_map = payload.get("document_section_map") or []
        expected_candidate_ids = {str(item.get("section_id")) for item in section_map if isinstance(item, dict) and item.get("candidate_id")}
        actual_candidate_ids = {str(item.get("section_id")) for item in sections if isinstance(item, dict)}
        if expected_candidate_ids and actual_candidate_ids != expected_candidate_ids:
            findings.append(QualityFinding(
                "QG_INTEGRATION_CANDIDATE_SET_INCOMPLETE", "P1", "INTEGRATION", "CANDIDATE_DOCUMENT",
                "candidate_sections", f"全篇审查只收到{len(actual_candidate_ids)}/{len(expected_candidate_ids)}个已生成章节。",
                "终止审查并报告上下文装配错误，禁止使用Replay种子或单章候选替代全文。",
                "INTEGRATION_AGENT",
            ))
        texts = []
        all_paragraphs: list[tuple[str, str]] = []
        all_sentences: list[tuple[str, str]] = []
        information_locations: dict[str, set[str]] = collections.defaultdict(set)
        claim_locations: dict[str, set[str]] = collections.defaultdict(set)
        skeleton_locations: dict[str, set[str]] = collections.defaultdict(set)
        for item in sections:
            section_id = str(item.get("section_id") or "")
            candidate = item.get("candidate") or {}
            text = _content_text(candidate)
            texts.append(text)
            advancement = candidate.get("claim_advancement") or {}
            for key in advancement.get("new_information_keys") or []:
                if key:
                    information_locations[str(key)].add(section_id)
            for claim_id in advancement.get("advanced_claim_ids") or []:
                if claim_id:
                    claim_locations[str(claim_id)].add(section_id)
            for paragraph in candidate.get("paragraphs", []):
                if not isinstance(paragraph, dict):
                    continue
                paragraph_text = str(paragraph.get("text") or "").strip()
                if not paragraph_text:
                    continue
                all_paragraphs.append((section_id, paragraph_text))
                sentences = _normalized_sentences(paragraph_text)
                all_sentences.extend((section_id, sentence) for sentence in sentences)
                for sentence in sentences:
                    skeleton = _template_skeleton(sentence)
                    if len(skeleton) >= 18:
                        skeleton_locations[skeleton].add(section_id)
        joined = "\n".join(texts)
        paragraph_locations: dict[str, set[str]] = collections.defaultdict(set)
        sentence_locations: dict[str, set[str]] = collections.defaultdict(set)
        for section_id, text in all_paragraphs:
            paragraph_locations[text].add(section_id)
        for section_id, sentence in all_sentences:
            sentence_locations[sentence].add(section_id)
        exact_repeated = {text: ids for text, ids in paragraph_locations.items() if len(ids) >= 2 and len(text) >= 20}
        min_sentence_sections = max(3, math.ceil(len(sections) * 0.08))
        high_repeat = {sentence: ids for sentence, ids in sentence_locations.items() if len(ids) >= min_sentence_sections}
        template_skeletons = {sk: ids for sk, ids in skeleton_locations.items() if len(ids) >= min_sentence_sections}
        duplicate_information = {key: ids for key, ids in information_locations.items() if len(ids) >= 2}
        central_id = str(((payload.get("argument_graph") or {}).get("central_proposition") or {}).get("node_id") or "")
        claim_threshold = max(4, math.ceil(len(sections) * 0.25))
        claim_overconcentration = {
            claim_id: ids
            for claim_id, ids in claim_locations.items()
            if claim_id != central_id and len(ids) >= claim_threshold
        }
        affected_section_ids = sorted({
            sid
            for ids in [
                *exact_repeated.values(), *high_repeat.values(), *template_skeletons.values(),
                *duplicate_information.values(), *claim_overconcentration.values(),
            ]
            for sid in ids if sid
        })
        result = output.setdefault("result", {})
        result["redundancy_report"] = {
            "exact_duplicate_groups": len(exact_repeated),
            "semantic_template_groups": len(high_repeat),
            "affected_section_ids": affected_section_ids,
            "representative_signatures": [sha256_text(text)[:16] for text in list(exact_repeated)[:4]]
            + [sha256_text(sentence)[:16] for sentence in list(high_repeat)[:4]]
            + [sha256_text(skeleton)[:16] for skeleton in list(template_skeletons)[:4]],
            "duplicate_information_key_groups": len(duplicate_information),
            "claim_overconcentration_groups": len(claim_overconcentration),
            "template_skeleton_groups": len(template_skeletons),
        }
        if exact_repeated or high_repeat or template_skeletons:
            findings.append(QualityFinding(
                "QG_DOCUMENT_TEMPLATE_REPETITION", "P1", "INTEGRATION", "CANDIDATE_DOCUMENT",
                "candidate_sections", f"全篇存在{len(exact_repeated)}组重复段落、{len(high_repeat)}组高频句和{len(template_skeletons)}组同构表达，涉及{len(affected_section_ids)}个章节。",
                "仅重写受影响章节；每章必须保留独有命题、证据和新增信息键，并改变论证推进方式，而非仅替换标题或名词。",
                "WRITING_AGENT",
            ))
        if duplicate_information:
            findings.append(QualityFinding(
                "QG_DOCUMENT_DUPLICATE_INFORMATION_KEYS", "P1", "INTEGRATION", "CANDIDATE_DOCUMENT",
                "candidate_sections.claim_advancement", f"有{len(duplicate_information)}个新增信息键被多个章节重复声明。",
                "为每个信息键指定唯一主责章节；其他章节只能引用，不得再次声明为新增内容。",
                "PLANNING_AGENT",
            ))
        if claim_overconcentration:
            findings.append(QualityFinding(
                "QG_DOCUMENT_CLAIM_OVERCONCENTRATION", "P1", "INTEGRATION", "CANDIDATE_DOCUMENT",
                "candidate_sections.claim_advancement", f"有{len(claim_overconcentration)}个非中心命题被过多章节重复推进。",
                "重新分配章节合同：一个命题由有限章节分别承担提出、证明和验证，不得在多数章节重复展开。",
                "PLANNING_AGENT",
            ))
        meta_hits = sum(joined.count(term) for term in HIGH_RISK_META_TERMS)
        if meta_hits >= max(10, len(sections) // 2):
            findings.append(QualityFinding(
                "QG_DOCUMENT_DOMINATED_BY_AGENT_SYSTEM", "P1", "INTEGRATION", "CANDIDATE_DOCUMENT",
                "candidate_sections", f"全篇系统流程/审计/部署术语出现{meta_hits}次，主文研究论证被智能体系统说明取代。",
                "主文重构为研究问题—方法—验证链；系统实现与部署移入附件。",
                "INTEGRATION_AGENT",
            ))
        pd = payload.get("project_definition") or {}
        valid_ids = {str(i.get("item_id")) for i in pd.get("items", []) if isinstance(i, dict)}
        mapping_ids: set[str] = set()
        for m in (output.get("result") or {}).get("mapping_checks") or []:
            if isinstance(m, dict):
                mapping_ids.add(str(m.get("source_id") or ""))
                mapping_ids.update(str(x) for x in m.get("target_ids") or [])
        fabricated = sorted(x for x in mapping_ids if x and x not in valid_ids)
        if fabricated:
            findings.append(QualityFinding(
                "QG_INTEGRATION_FABRICATED_MAPPING", "P1", "INTEGRATION", "INTEGRATION_REPORT",
                "result.mapping_checks", f"Integration Critic使用了{len(fabricated)}个项目图谱中不存在的映射ID。",
                "映射检查必须引用真实项目对象；无法解析的ID直接BLOCK。",
                "INTEGRATION_AGENT",
            ))
        result = output.get("result") or {}
        quality_dimensions = result.get("quality_dimensions") or []
        scorecard = {str(item.get("dimension")): item for item in quality_dimensions if isinstance(item, dict)}
        invalid_dimensions = sorted(
            dim for dim in QUALITY_DIMENSIONS
            if dim not in scorecard or not scorecard[dim].get("passed", False) or float(scorecard[dim].get("score", 0)) < 3
        )
        if invalid_dimensions:
            findings.append(QualityFinding(
                "QG_INTEGRATION_SCOPE_TOO_NARROW", "P1", "INTEGRATION", "INTEGRATION_REPORT",
                "result.quality_dimensions",
                f"全篇Critic缺少或未通过质量维度：{', '.join(invalid_dimensions)}。",
                "完整审查文种、中心命题、论证链、证据、方法、创新、基础、指标、章节独特性、密度、篇幅和跨章一致性。",
                "INTEGRATION_AGENT",
            ))
        return findings

    @staticmethod
    def _merge_findings(output: dict[str, Any], findings: list[QualityFinding]) -> None:
        if not findings:
            return
        existing = output.setdefault("findings", [])
        existing_codes = {str(item.get("code")) for item in existing if isinstance(item, dict)}
        for finding in findings:
            if finding.code not in existing_codes:
                existing.append(finding.as_dict())
                existing_codes.add(finding.code)
        if any(f.severity == "P0" for f in findings):
            output["status"] = "BLOCK"
        elif any(f.severity == "P1" and f.blocking for f in findings):
            output["status"] = "REVISE"
        result = output.get("result")
        if isinstance(result, dict) and "verdict" in result and output.get("status") != "PASS":
            allowed = {"ACCEPT", "REVISE", "BLOCK"}
            result["verdict"] = "BLOCK" if output["status"] == "BLOCK" else "REVISE"
