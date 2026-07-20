from __future__ import annotations

import collections
import copy
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import yaml

from .proposal_quality import (
    HIGH_RISK_META_TERMS,
    ProposalQualityGuard,
    _content_text,
    _meta_term_hits,
    _normalized_sentences,
    _template_skeleton,
)


CRITIC_PROMPTS = {
    "P-SCHEME-CRITIC",
    "P-PROJECT-DEFINITION-CRITIC",
    "P-FACT-CRITIC",
    "P-ARGUMENT-ARCHITECTURE-CRITIC",
    "P-REVISION-PLAN-CRITIC",
    "P-WRITE-BLUEPRINT-CRITIC",
    "P-WRITE-CRITIC",
    "P-EXPRESSION-CRITIC",
    "P-INTEGRATION-CRITIC",
}

CORE_SECTION_PROFILES = {
    "BACKGROUND_AND_SIGNIFICANCE",
    "LITERATURE_REVIEW",
    "KEY_ISSUE",
    "RESEARCH_OBJECTIVE",
    "RESEARCH_CONTENT",
    "METHOD_AND_ALGORITHM",
    "TECHNICAL_ROUTE",
    "EVALUATION",
    "INNOVATION",
    "OUTPUTS_AND_METRICS",
    "RESEARCH_FOUNDATION",
    "CONCLUSION",
    "APPENDIX",
}

UNAMBIGUOUS_RELATION_SIGNATURES: dict[str, tuple[set[str], set[str]]] = {
    "CAUSED_BY": ({"GAP", "PROBLEM"}, {"ROOT_CAUSE"}),
    "DECOMPOSES_TO": ({"OBJECTIVE"}, {"WORK_PACKAGE"}),
    "HAS_CURRENT_STATE": ({"PROJECT_BASIC", "DEMAND", "SCENARIO"}, {"CURRENT_STATE"}),
    "HAS_GAP": ({"CURRENT_STATE", "EXISTING_APPROACH"}, {"GAP"}),
    "MEASURED_BY": ({"OBJECTIVE", "DELIVERABLE", "EXPERIMENT"}, {"METRIC"}),
    "OCCURS_IN": ({"DEMAND", "PROBLEM", "RISK"}, {"SCENARIO"}),
    "SCHEDULED_IN": ({"WORK_PACKAGE"}, {"SCHEDULE_PHASE"}),
    "VALIDATED_BY": ({"OBJECTIVE", "WORK_PACKAGE", "METHOD", "INNOVATION"}, {"EXPERIMENT", "METRIC"}),
}

APPENDIX_ENGINEERING_TERMS = {
    "Docker",
    "Trace",
    "Prompt",
    "部署说明",
    "安装步骤",
    "运行日志",
    "审计日志",
    "Manifest校验",
}

STRUCTURAL_BLOCK_RE = re.compile(
    r"(?ms)^\s*(\[\[(?:TABLE|FORMULA|FIGURE)[^\n]*\]\]|```mermaid\s*.*?```)"
)
NUMBER_RE = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?%?")
CITATION_RE = re.compile(r"\[(?:\d+(?:\s*[-,，]\s*\d+)*)\]")


def _safe_evidence_ref(value: Any) -> str:
    ref = re.sub(r"[^A-Za-z0-9._:-]+", "_", str(value)).strip("._:-")
    if not ref:
        ref = "track-b-evidence"
    if not ref[0].isalnum():
        ref = "ref-" + ref
    return ref[:128]


def _source_ids(refs: Iterable[dict[str, Any]]) -> list[str]:
    return [str(ref.get("source_id")) for ref in refs if isinstance(ref, dict) and ref.get("source_id")]


@dataclass(frozen=True)
class TrackBFinding:
    code: str
    severity: str
    category: str
    target_type: str
    target_path: str
    description: str
    repair_instruction: str
    suggested_route: str
    blocking: bool = True
    evidence_refs: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "category": self.category,
            "target_type": self.target_type,
            "target_path_or_span": self.target_path,
            "description": self.description,
            "evidence_refs": list(self.evidence_refs),
            "repairable": bool(self.repair_instruction),
            "repair_instruction": self.repair_instruction,
            "suggested_route": self.suggested_route,
            "blocking": self.blocking,
        }


def _finding(
    code: str,
    category: str,
    target_type: str,
    target_path: str,
    description: str,
    repair_instruction: str,
    route: str,
    *,
    severity: str = "P1",
    evidence_refs: Iterable[str] = (),
) -> TrackBFinding:
    return TrackBFinding(
        code=code,
        severity=severity,
        category=category,
        target_type=target_type,
        target_path=target_path,
        description=description,
        repair_instruction=repair_instruction,
        suggested_route=route,
        evidence_refs=tuple(_safe_evidence_ref(item) for item in evidence_refs if item),
    )


def _candidate_for_prompt(prompt_id: str, payload: dict[str, Any], output: dict[str, Any]) -> dict[str, Any]:
    if prompt_id == "P-WRITE-BLUEPRINT":
        return (output.get("result") or {}).get("blueprint") or {}
    if prompt_id == "P-WRITE-BLUEPRINT-CRITIC":
        return payload.get("blueprint_candidate") or {}
    if prompt_id in {"P-WRITE-CONTENT", "P-EXPRESSION-POLISH"}:
        return output.get("result") or {}
    if prompt_id == "P-WRITE-CRITIC":
        candidate = payload.get("content_candidate") or {}
        return candidate.get("result") if isinstance(candidate.get("result"), dict) else candidate
    if prompt_id == "P-EXPRESSION-CRITIC":
        return payload.get("polished_candidate") or {}
    return {}


class AgentPromptKernelValidator:
    """Independent G1 validator for Track B (Agent, Prompt and argument kernel).

    The validator composes the v0.6 proposal quality guard and adds the B-track
    acceptance conditions that were not previously deterministic: scheme-rule
    provenance, project relation direction, atomic facts, precise repair scope,
    conclusion closure, structure-preserving polish, and main-body/appendix
    separation.
    """

    def __init__(self, pack=None):
        self.pack = pack
        self.base_guard = ProposalQualityGuard()

    def apply(self, prompt_id: str, envelope: dict[str, Any], output: dict[str, Any]) -> dict[str, Any]:
        payload = envelope.get("payload") or {}
        original_status = str(output.get("status") or "PASS")
        original_verdict = (output.get("result") or {}).get("verdict")
        model_findings = copy.deepcopy(output.get("findings") or [])

        checked = self.base_guard.apply(prompt_id, envelope, output)
        if prompt_id == "P-INTEGRATION-CRITIC":
            self._replace_document_statistics_with_main_body_only(payload, checked)

        findings: list[TrackBFinding] = []
        if prompt_id in {"P-SCHEME-EXTRACT", "P-SCHEME-CRITIC"}:
            scheme = (
                (checked.get("result") or {}).get("scheme_profile")
                if prompt_id == "P-SCHEME-EXTRACT"
                else payload.get("scheme_candidate")
            ) or {}
            coverage = (
                (checked.get("result") or {}).get("extraction_coverage") or []
                if prompt_id == "P-SCHEME-EXTRACT"
                else None
            )
            ambiguous = (
                (checked.get("result") or {}).get("ambiguous_rule_ids") or []
                if prompt_id == "P-SCHEME-EXTRACT"
                else []
            )
            findings.extend(self._audit_scheme(scheme, coverage, ambiguous))

        if prompt_id in {"P-PROJECT-DEFINITION-EXTRACT", "P-PROJECT-DEFINITION-CRITIC"}:
            project_definition = (
                (checked.get("result") or {}).get("project_definition")
                if prompt_id == "P-PROJECT-DEFINITION-EXTRACT"
                else payload.get("project_definition_candidate")
            ) or {}
            findings.extend(self._audit_project_relations(project_definition))

        if prompt_id in {"P-FACT-EXTRACT", "P-FACT-CRITIC"}:
            facts = (
                (checked.get("result") or {}).get("fact_candidates")
                if prompt_id == "P-FACT-EXTRACT"
                else payload.get("fact_candidates")
            ) or []
            coverage = (
                (checked.get("result") or {}).get("coverage") or []
                if prompt_id == "P-FACT-EXTRACT"
                else None
            )
            findings.extend(self._audit_facts(facts, coverage))

        if prompt_id in CRITIC_PROMPTS:
            findings.extend(self._audit_finding_precision(model_findings, payload))

        if prompt_id == "P-TARGETED-REPAIR":
            findings.extend(self._audit_repair_scope(payload, checked.get("result") or {}))

        if prompt_id in {"P-EXPRESSION-POLISH", "P-EXPRESSION-CRITIC"}:
            source = payload.get("content_candidate") or {}
            polished = (
                checked.get("result") or {}
                if prompt_id == "P-EXPRESSION-POLISH"
                else payload.get("polished_candidate") or {}
            )
            findings.extend(self._audit_structure_preservation(source, polished))

        if prompt_id in {
            "P-WRITE-BLUEPRINT",
            "P-WRITE-BLUEPRINT-CRITIC",
            "P-WRITE-CONTENT",
            "P-WRITE-CRITIC",
            "P-EXPRESSION-POLISH",
            "P-EXPRESSION-CRITIC",
        }:
            profile_id = str((payload.get("section_profile") or {}).get("profile_id") or "")
            if profile_id == "CONCLUSION":
                findings.extend(
                    self._audit_conclusion(
                        _candidate_for_prompt(prompt_id, payload, checked),
                        payload,
                    )
                )

        if prompt_id == "P-INTEGRATION-CRITIC":
            findings.extend(self._audit_body_appendix_boundary(payload))

        ProposalQualityGuard._merge_findings(checked, findings)
        self._recalculate_status(checked, original_status, original_verdict)
        return checked

    @staticmethod
    def _audit_scheme(
        scheme: dict[str, Any],
        coverage: list[dict[str, Any]] | None,
        ambiguous_rule_ids: list[str],
    ) -> list[TrackBFinding]:
        findings: list[TrackBFinding] = []
        rules = [item for item in scheme.get("rules", []) if isinstance(item, dict)]
        rule_ids = [str(item.get("rule_id") or "") for item in rules]
        coverage_ids = {
            str(rule_id)
            for item in (coverage or [])
            if isinstance(item, dict)
            for rule_id in item.get("covered_rule_ids") or []
        }
        missing_coverage = (
            sorted(rule_id for rule_id in rule_ids if rule_id and rule_id not in coverage_ids)
            if coverage is not None
            else []
        )
        if missing_coverage:
            findings.append(_finding(
                "QG_SCHEME_RULE_NOT_COVERED",
                "SCHEME",
                "SCHEME_PROFILE",
                "result.extraction_coverage",
                f"{len(missing_coverage)}条申报规则没有进入来源覆盖表。",
                "逐条将规则绑定到实际来源；无法绑定的内容不得进入规则包。",
                "PROJECT_KNOWLEDGE_AGENT",
                evidence_refs=missing_coverage,
            ))

        ambiguous = {str(item) for item in ambiguous_rule_ids}
        for index, rule in enumerate(rules):
            rule_id = str(rule.get("rule_id") or f"rule-{index}")
            refs = [item for item in rule.get("source_refs") or [] if isinstance(item, dict)]
            usable = [
                ref for ref in refs
                if ref.get("source_id") and str(ref.get("quoted_text") or "").strip()
            ]
            if not usable:
                findings.append(_finding(
                    "QG_SCHEME_RULE_WITHOUT_SOURCE",
                    "SOURCE",
                    "SCHEME_RULE",
                    f"scheme_profile.rules[{index}].source_refs",
                    f"规则{rule_id}没有可定位的来源原文。",
                    "删除该规则，或补充正式指南、任务书、合同或用户确认中的具体来源Span。",
                    "PROJECT_KNOWLEDGE_AGENT",
                    evidence_refs=[rule_id],
                ))
            authoritative = [
                ref for ref in usable
                if ref.get("source_type") not in {"MODEL_INFERENCE", "REFERENCE_PROPOSAL", "PUBLIC_SOURCE"}
            ]
            if rule.get("mandatory") and not authoritative:
                findings.append(_finding(
                    "QG_SCHEME_EXTRAPOLATION_AS_MANDATORY",
                    "SCHEME",
                    "SCHEME_RULE",
                    f"scheme_profile.rules[{index}]",
                    f"规则{rule_id}仅由模型推断、参考申请书或公开资料支撑，却被标为强制条款。",
                    "将其降级为建议/待确认内容，或补充正式指南、任务书、合同或用户确认来源。",
                    "PROJECT_KNOWLEDGE_AGENT",
                    evidence_refs=[rule_id, *_source_ids(usable)],
                ))
            if rule.get("mandatory") and rule_id in ambiguous:
                findings.append(_finding(
                    "QG_SCHEME_AMBIGUOUS_RULE_MARKED_MANDATORY",
                    "SCHEME",
                    "SCHEME_RULE",
                    f"scheme_profile.rules[{index}].mandatory",
                    f"规则{rule_id}仍处于歧义集合，却被作为强制规则使用。",
                    "在歧义消解前保持待确认，禁止进入Proposal Contract硬约束。",
                    "PROJECT_KNOWLEDGE_AGENT",
                    evidence_refs=[rule_id],
                ))
        return findings

    @staticmethod
    def _audit_project_relations(project_definition: dict[str, Any]) -> list[TrackBFinding]:
        findings: list[TrackBFinding] = []
        items = {
            str(item.get("item_id")): item
            for item in project_definition.get("items", [])
            if isinstance(item, dict) and item.get("item_id")
        }
        relation_ids: set[str] = set()
        for index, relation in enumerate(project_definition.get("relations", [])):
            if not isinstance(relation, dict):
                continue
            relation_id = str(relation.get("relation_id") or f"relation-{index}")
            source_id = str(relation.get("source_item_id") or "")
            target_id = str(relation.get("target_item_id") or "")
            relation_type = str(relation.get("relation_type") or "")
            if relation_id in relation_ids:
                findings.append(_finding(
                    "QG_PROJECT_RELATION_ID_DUPLICATE",
                    "PROJECT_DEFINITION",
                    "PROJECT_RELATION",
                    f"relations[{index}].relation_id",
                    f"关系ID {relation_id}重复。",
                    "为每条关系分配唯一ID并重算关系Hash。",
                    "PROJECT_KNOWLEDGE_AGENT",
                    evidence_refs=[relation_id],
                ))
            relation_ids.add(relation_id)
            if source_id == target_id or source_id not in items or target_id not in items:
                findings.append(_finding(
                    "QG_PROJECT_RELATION_ENDPOINT_INVALID",
                    "PROJECT_DEFINITION",
                    "PROJECT_RELATION",
                    f"relations[{index}]",
                    f"关系{relation_id}存在自指或引用不存在的端点。",
                    "仅连接项目事实图中真实存在的两个不同对象。",
                    "PROJECT_KNOWLEDGE_AGENT",
                    evidence_refs=[relation_id, source_id, target_id],
                ))
                continue
            source_type = str(items[source_id].get("item_type") or "")
            target_type = str(items[target_id].get("item_type") or "")
            if relation.get("source_item_type") != source_type or relation.get("target_item_type") != target_type:
                findings.append(_finding(
                    "QG_PROJECT_RELATION_TYPE_MISMATCH",
                    "PROJECT_DEFINITION",
                    "PROJECT_RELATION",
                    f"relations[{index}]",
                    f"关系{relation_id}声明的端点类型与对象实际类型不一致。",
                    "使用端点对象的真实item_type重建关系，不得通过改标签掩盖方向错误。",
                    "PROJECT_KNOWLEDGE_AGENT",
                    evidence_refs=[relation_id, source_id, target_id],
                ))
            signature = UNAMBIGUOUS_RELATION_SIGNATURES.get(relation_type)
            if signature and (source_type not in signature[0] or target_type not in signature[1]):
                findings.append(_finding(
                    "QG_PROJECT_RELATION_DIRECTION_INVALID",
                    "PROJECT_DEFINITION",
                    "PROJECT_RELATION",
                    f"relations[{index}].relation_type",
                    f"关系{relation_id}的{relation_type}方向不合法：{source_type} → {target_type}。",
                    "按关系语义调整方向或选择合法关系类型；不得保留反向边。",
                    "PROJECT_KNOWLEDGE_AGENT",
                    evidence_refs=[relation_id, source_id, target_id],
                ))
        return findings

    @staticmethod
    def _audit_facts(facts: list[dict[str, Any]], coverage: list[dict[str, Any]] | None) -> list[TrackBFinding]:
        findings: list[TrackBFinding] = []
        claim_ids: set[str] = set()
        valid_claim_ids = {
            str(item.get("claim_id"))
            for item in facts
            if isinstance(item, dict) and item.get("claim_id")
        }
        covered_ids = {
            str(claim_id)
            for item in (coverage or [])
            if isinstance(item, dict)
            for claim_id in item.get("claim_ids") or []
        }
        for index, claim in enumerate(facts):
            if not isinstance(claim, dict):
                continue
            claim_id = str(claim.get("claim_id") or f"claim-{index}")
            text = str(claim.get("claim_text") or "").strip()
            if claim_id in claim_ids:
                findings.append(_finding(
                    "QG_FACT_ID_DUPLICATE",
                    "FACT",
                    "FACT_CLAIM",
                    f"fact_candidates[{index}].claim_id",
                    f"事实ID {claim_id}重复。",
                    "按原子命题重新分配唯一claim_id。",
                    "PROJECT_KNOWLEDGE_AGENT",
                    evidence_refs=[claim_id],
                ))
            claim_ids.add(claim_id)
            clauses = [
                part.strip()
                for part in re.split(r"[。；;]\s*", text)
                if part.strip()
            ]
            enumerated = bool(re.search(r"(?:^|[，,；;])(?:一是|二是|三是|首先|其次|最后)", text))
            if len(clauses) > 1 or enumerated:
                findings.append(_finding(
                    "QG_FACT_NOT_ATOMIC",
                    "FACT",
                    "FACT_CLAIM",
                    f"fact_candidates[{index}].claim_text",
                    f"事实{claim_id}包含多个可独立判真的分句。",
                    "拆分为一条记录一个命题，并分别保留主体、时间、限定词和来源。",
                    "PROJECT_KNOWLEDGE_AGENT",
                    evidence_refs=[claim_id, *_source_ids(claim.get("source_refs") or [])],
                ))
            if claim.get("claim_type") != "MODEL_INFERENCE" and not claim.get("subject_id"):
                findings.append(_finding(
                    "QG_FACT_SUBJECT_MISSING",
                    "FACT",
                    "FACT_CLAIM",
                    f"fact_candidates[{index}].subject_id",
                    f"事实{claim_id}缺少主体。",
                    "补充可解析的主体ID；无法确定时保持UNKNOWN并请求确认。",
                    "PROJECT_KNOWLEDGE_AGENT",
                    evidence_refs=[claim_id],
                ))
            if claim.get("temporal_status") == "UNKNOWN" and claim.get("claim_type") in {"FACT", "PLAN", "EXPECTED_RESULT"}:
                findings.append(_finding(
                    "QG_FACT_TEMPORAL_STATUS_UNKNOWN",
                    "FACT",
                    "FACT_CLAIM",
                    f"fact_candidates[{index}].temporal_status",
                    f"事实{claim_id}未区分已完成、当前、计划或预期状态。",
                    "根据来源明确时间状态，禁止把计划或预期结果改写为既有事实。",
                    "PROJECT_KNOWLEDGE_AGENT",
                    evidence_refs=[claim_id],
                ))
            numeric_tokens = NUMBER_RE.findall(text)
            numeric_values = claim.get("numeric_values") or []
            if numeric_tokens and not numeric_values:
                findings.append(_finding(
                    "QG_FACT_NUMERIC_BINDING_MISSING",
                    "FACT",
                    "FACT_CLAIM",
                    f"fact_candidates[{index}].numeric_values",
                    f"事实{claim_id}含数字{numeric_tokens[:4]}，但没有对象、单位和条件绑定。",
                    "为每个实质数字记录value、unit、object和condition。",
                    "PROJECT_KNOWLEDGE_AGENT",
                    evidence_refs=[claim_id],
                ))
            refs = [ref for ref in claim.get("source_refs") or [] if isinstance(ref, dict)]
            if claim.get("knowledge_status") in {"CONFIRMED", "DOCUMENT_EXTRACTED"} and not _source_ids(refs):
                findings.append(_finding(
                    "QG_FACT_CONFIRMED_WITHOUT_SOURCE",
                    "SOURCE",
                    "FACT_CLAIM",
                    f"fact_candidates[{index}].source_refs",
                    f"事实{claim_id}被标为已确认，但没有来源。",
                    "补充可定位来源，或降级为USER_ASSERTED/UNKNOWN。",
                    "PROJECT_KNOWLEDGE_AGENT",
                    evidence_refs=[claim_id],
                ))
        missing_coverage = sorted(valid_claim_ids - covered_ids) if coverage is not None else []
        unknown_coverage = sorted(covered_ids - valid_claim_ids) if coverage is not None else []
        if missing_coverage or unknown_coverage:
            findings.append(_finding(
                "QG_FACT_COVERAGE_INCONSISTENT",
                "FACT",
                "FACT_PACKAGE",
                "result.coverage",
                f"事实覆盖表遗漏{len(missing_coverage)}条事实并引用{len(unknown_coverage)}个未知ID。",
                "逐Span登记实际生成的事实ID，删除不存在的引用。",
                "PROJECT_KNOWLEDGE_AGENT",
                evidence_refs=[*missing_coverage, *unknown_coverage],
            ))
        return findings

    @staticmethod
    def _audit_finding_precision(
        findings: list[dict[str, Any]],
        payload: dict[str, Any],
    ) -> list[TrackBFinding]:
        audit: list[TrackBFinding] = []
        candidate_text = ""
        for field in ("content_candidate", "polished_candidate", "blueprint_candidate", "architecture_candidate"):
            value = payload.get(field)
            if isinstance(value, dict):
                candidate_text += "\n" + json.dumps(value, ensure_ascii=False)
        for index, finding in enumerate(findings):
            if not isinstance(finding, dict) or finding.get("severity") not in {"P0", "P1"}:
                continue
            code = str(finding.get("code") or f"finding-{index}")
            path = str(finding.get("target_path_or_span") or "").strip()
            refs = [str(item) for item in finding.get("evidence_refs") or [] if item]
            instruction = str(finding.get("repair_instruction") or "").strip()
            description = str(finding.get("description") or "").strip()
            vague = bool(re.fullmatch(r"(内容)?(不够|需要|建议)?(深入|完善|优化|补充)[。！!]?", description))
            evidence_in_text = any(ref in candidate_text for ref in refs) if candidate_text and refs else False
            if not path or not refs or len(instruction) < 8 or vague or (candidate_text and refs and not evidence_in_text):
                audit.append(_finding(
                    "QG_CRITIC_FINDING_NOT_PRECISE",
                    "CONTENT",
                    "FINDING",
                    f"findings[{index}]",
                    f"Finding {code}没有同时给出可定位路径、候选内证据和最小修复指令。",
                    "引用具体段落/节点/句子ID，说明问题机制，并限定可修改路径。",
                    "ORIGINAL_PRODUCER",
                    evidence_refs=[code, *refs],
                ))
        return audit

    @staticmethod
    def _audit_repair_scope(payload: dict[str, Any], result: dict[str, Any]) -> list[TrackBFinding]:
        findings: list[TrackBFinding] = []
        allowed = [str(item) for item in payload.get("allowed_paths") or []]
        protected = [str(item) for item in payload.get("protected_paths") or []]
        changed = [str(item) for item in result.get("changed_paths") or []]

        def under(path: str, roots: list[str]) -> bool:
            def normalize_selectors(value: str) -> str:
                return re.sub(r"\[[^\]]+\]", "", value)

            for root in roots:
                if "[" in root:
                    # Explicit selectors/indices remain strict: permission for [3]
                    # must never silently authorize [4].
                    if path == root or path.startswith(root + ".") or path.startswith(root + "["):
                        return True
                    continue
                normalized_path = normalize_selectors(path)
                normalized_root = normalize_selectors(root)
                if (
                    normalized_path == normalized_root
                    or normalized_path.startswith(normalized_root + ".")
                ):
                    return True
            return False

        outside = [path for path in changed if not under(path, allowed)]
        protected_hits = [path for path in changed if under(path, protected)]
        if outside or protected_hits:
            findings.append(_finding(
                "QG_REPAIR_PATH_OUTSIDE_ALLOWLIST",
                "CONTENT",
                "REPAIRED_OBJECT",
                "result.changed_paths",
                f"定向修复修改了{len(outside)}个未授权路径和{len(protected_hits)}个受保护路径。",
                "只修改allowed_paths的子路径；恢复protected_paths及其Hash。",
                "ORIGINAL_PRODUCER",
                evidence_refs=[*outside, *protected_hits],
            ))
        requested_codes = {
            str(item.get("code"))
            for item in payload.get("findings_to_repair") or []
            if isinstance(item, dict) and item.get("code")
        }
        resolved = {str(item) for item in result.get("resolved_finding_codes") or []}
        unknown = sorted(resolved - requested_codes)
        if unknown:
            findings.append(_finding(
                "QG_REPAIR_RESOLVED_UNKNOWN_FINDING",
                "CONTENT",
                "REPAIRED_OBJECT",
                "result.resolved_finding_codes",
                f"修复结果宣称关闭{len(unknown)}个本轮未请求的Finding。",
                "只报告findings_to_repair中的代码；其余问题必须由独立Critic重新发现和关闭。",
                "ORIGINAL_PRODUCER",
                evidence_refs=unknown,
            ))
        return findings

    @staticmethod
    def _audit_structure_preservation(
        source: dict[str, Any],
        polished: dict[str, Any],
    ) -> list[TrackBFinding]:
        findings: list[TrackBFinding] = []
        source_text = _content_text(source)
        polished_text = _content_text(polished)
        source_blocks = [re.sub(r"\s+", " ", item).strip() for item in STRUCTURAL_BLOCK_RE.findall(source_text)]
        polished_blocks = [re.sub(r"\s+", " ", item).strip() for item in STRUCTURAL_BLOCK_RE.findall(polished_text)]
        if source_blocks != polished_blocks:
            findings.append(_finding(
                "QG_EXPRESSION_STRUCTURE_BLOCK_CHANGED",
                "EXPRESSION",
                "SECTION_CANDIDATE",
                "result.candidate_text",
                "表达润色改变、删除或压平了表格、公式、图形或Mermaid结构块。",
                "逐字保留结构块及顺序，只润色普通叙述段落。",
                "EXPRESSION_EDITOR_AGENT",
            ))
        if collections.Counter(NUMBER_RE.findall(source_text)) != collections.Counter(NUMBER_RE.findall(polished_text)):
            findings.append(_finding(
                "QG_EXPRESSION_NUMERIC_TOKEN_CHANGED",
                "EXPRESSION",
                "SECTION_CANDIDATE",
                "result.candidate_text",
                "表达润色改变了数字、百分比或数值出现次数。",
                "恢复原始数字；需要改变指标时路由事实/规划Agent，不能由表达编辑器修改。",
                "EXPRESSION_EDITOR_AGENT",
            ))
        if collections.Counter(CITATION_RE.findall(source_text)) != collections.Counter(CITATION_RE.findall(polished_text)):
            findings.append(_finding(
                "QG_EXPRESSION_CITATION_CHANGED",
                "EXPRESSION",
                "SECTION_CANDIDATE",
                "result.candidate_text",
                "表达润色改变了正文引用标记。",
                "恢复引用标记及其顺序；引用调整必须回到证据写作阶段。",
                "EXPRESSION_EDITOR_AGENT",
            ))
        return findings

    @staticmethod
    def _audit_conclusion(candidate: dict[str, Any], payload: dict[str, Any]) -> list[TrackBFinding]:
        findings: list[TrackBFinding] = []
        graph = payload.get("argument_graph") or {}
        central_id = str((graph.get("central_proposition") or {}).get("node_id") or "")
        question_ids = {
            str(item.get("node_id"))
            for item in graph.get("research_questions") or []
            if isinstance(item, dict) and item.get("node_id")
        }
        known_ids = {central_id, *question_ids}
        known_ids.update(
            str(item.get("node_id"))
            for item in graph.get("nodes") or []
            if isinstance(item, dict) and item.get("node_id")
        )
        contract = payload.get("section_contract") or {}
        known_ids.update(str(item) for item in contract.get("must_advance_claim_ids") or [])
        paragraphs = [item for item in candidate.get("paragraphs") or [] if isinstance(item, dict)]
        advanced = {
            str(item.get("primary_claim_id"))
            for item in paragraphs
            if item.get("primary_claim_id")
        }
        advancement = candidate.get("claim_advancement") or {}
        advanced.update(str(item) for item in advancement.get("advanced_claim_ids") or [])
        missing_questions = sorted(question_ids - advanced)
        if missing_questions:
            findings.append(_finding(
                "QG_CONCLUSION_QUESTIONS_UNANSWERED",
                "CONTENT",
                "SECTION_CANDIDATE",
                "paragraphs.primary_claim_id",
                f"结论未回答{len(missing_questions)}个研究问题。",
                "逐一总结每个研究问题的答案、对应方法和验证结果，不新增前文之外的论证。",
                "WRITING_AGENT",
                evidence_refs=missing_questions,
            ))
        if central_id and central_id not in advanced:
            findings.append(_finding(
                "QG_CONCLUSION_CENTRAL_PROPOSITION_MISSING",
                "CONTENT",
                "SECTION_CANDIDATE",
                "claim_advancement.advanced_claim_ids",
                "结论没有回扣全文唯一中心命题。",
                "在综合各研究问题后明确中心命题在边界条件下如何得到支撑。",
                "WRITING_AGENT",
                evidence_refs=[central_id],
            ))
        unknown = sorted(item for item in advanced if item and item not in known_ids)
        if unknown:
            findings.append(_finding(
                "QG_CONCLUSION_INTRODUCES_NEW_CLAIM",
                "CONTENT",
                "SECTION_CANDIDATE",
                "claim_advancement.advanced_claim_ids",
                f"结论引入{len(unknown)}个论证图中不存在的新方法、指标或功能命题。",
                "删除新命题，或先回到论证架构和对应正文完成论证后再进入结论。",
                "WRITING_AGENT",
                evidence_refs=unknown,
            ))
        return findings

    @staticmethod
    def _placement_map(payload: dict[str, Any]) -> dict[str, str]:
        architecture = payload.get("narrative_architecture") or {}
        return {
            str(item.get("section_id")): str(item.get("placement") or "MAIN_BODY")
            for item in architecture.get("section_contracts") or []
            if isinstance(item, dict) and item.get("section_id")
        }

    def _replace_document_statistics_with_main_body_only(
        self,
        payload: dict[str, Any],
        output: dict[str, Any],
    ) -> None:
        placements = self._placement_map(payload)
        all_sections = [item for item in payload.get("candidate_sections") or [] if isinstance(item, dict)]
        appendix_ids = {
            str(item.get("section_id"))
            for item in all_sections
            if placements.get(str(item.get("section_id")), "MAIN_BODY") == "APPENDIX"
        }
        main_sections = [
            item for item in all_sections
            if str(item.get("section_id")) not in appendix_ids
        ]

        paragraph_locations: dict[str, set[str]] = collections.defaultdict(set)
        sentence_locations: dict[str, set[str]] = collections.defaultdict(set)
        skeleton_locations: dict[str, set[str]] = collections.defaultdict(set)
        information_locations: dict[str, set[str]] = collections.defaultdict(set)
        claim_locations: dict[str, set[str]] = collections.defaultdict(set)
        meta_texts: list[str] = []
        for item in main_sections:
            section_id = str(item.get("section_id") or "")
            candidate = item.get("candidate") or {}
            advancement = candidate.get("claim_advancement") or {}
            for key in advancement.get("new_information_keys") or []:
                if key:
                    information_locations[str(key)].add(section_id)
            for claim_id in advancement.get("advanced_claim_ids") or []:
                if claim_id:
                    claim_locations[str(claim_id)].add(section_id)
            for paragraph in candidate.get("paragraphs") or []:
                if not isinstance(paragraph, dict):
                    continue
                paragraph_text = str(paragraph.get("text") or "").strip()
                if not paragraph_text:
                    continue
                if not paragraph_text.startswith("[[REFERENCE]]"):
                    meta_texts.append(paragraph_text)
                if paragraph_text.startswith((
                    "[[TABLE]]", "[[MERMAID]]", "[[FIGURE]]",
                    "[[FORMULA]]", "[[REFERENCE]]",
                )):
                    continue
                paragraph_locations[paragraph_text].add(section_id)
                for sentence in _normalized_sentences(paragraph_text):
                    sentence_locations[sentence].add(section_id)
                    skeleton = _template_skeleton(sentence)
                    if len(skeleton) >= 18:
                        skeleton_locations[skeleton].add(section_id)

        exact = {text: ids for text, ids in paragraph_locations.items() if len(ids) >= 2 and len(text) >= 20}
        threshold = max(3, math.ceil(len(main_sections) * 0.08))
        repeated = {text: ids for text, ids in sentence_locations.items() if len(ids) >= threshold}
        skeletons = {text: ids for text, ids in skeleton_locations.items() if len(ids) >= threshold}
        duplicate_information = {key: ids for key, ids in information_locations.items() if len(ids) >= 2}
        central_id = str(((payload.get("argument_graph") or {}).get("central_proposition") or {}).get("node_id") or "")
        claim_threshold = max(4, math.ceil(len(main_sections) * 0.25))
        overconcentrated = {
            claim_id: ids
            for claim_id, ids in claim_locations.items()
            if claim_id != central_id and len(ids) >= claim_threshold
        }
        affected = sorted({
            section_id
            for group in [*exact.values(), *repeated.values(), *skeletons.values()]
            for section_id in group
        })
        main_text = "\n".join(meta_texts)
        meta_hits = _meta_term_hits(main_text)
        report = {
            "exact_duplicate_groups": len(exact),
            "semantic_template_groups": len(repeated),
            "affected_section_ids": affected,
            "representative_signatures": [],
            "duplicate_information_key_groups": len(duplicate_information),
            "claim_overconcentration_groups": len(overconcentrated),
            "template_skeleton_groups": len(skeletons),
            "main_body_section_count": len(main_sections),
            "excluded_appendix_section_ids": sorted(appendix_ids),
        }
        result = output.setdefault("result", {})
        result["redundancy_report"] = report
        result["main_body_redundancy_report"] = copy.deepcopy(report)

        removable = set()
        if not (exact or repeated or skeletons):
            removable.add("QG_DOCUMENT_TEMPLATE_REPETITION")
        if not duplicate_information:
            removable.add("QG_DOCUMENT_DUPLICATE_INFORMATION_KEYS")
        if not overconcentrated:
            removable.add("QG_DOCUMENT_CLAIM_OVERCONCENTRATION")
        if meta_hits < max(10, len(main_sections) // 2):
            removable.add("QG_DOCUMENT_DOMINATED_BY_AGENT_SYSTEM")
        output["findings"] = [
            item for item in output.get("findings") or []
            if not (isinstance(item, dict) and item.get("code") in removable)
        ]

    @staticmethod
    def _audit_body_appendix_boundary(payload: dict[str, Any]) -> list[TrackBFinding]:
        findings: list[TrackBFinding] = []
        placements = AgentPromptKernelValidator._placement_map(payload)
        contract = payload.get("proposal_contract") or {}
        forbidden_terms = {
            str(item) for item in contract.get("forbidden_main_body_topics") or [] if item
        }
        appendix_topics = {
            str(item) for item in contract.get("appendix_only_topics") or [] if item
        }
        forbidden_terms.update(APPENDIX_ENGINEERING_TERMS)
        forbidden_terms.update(appendix_topics)
        for item in payload.get("candidate_sections") or []:
            if not isinstance(item, dict):
                continue
            section_id = str(item.get("section_id") or "")
            if placements.get(section_id, "MAIN_BODY") != "MAIN_BODY":
                continue
            candidate = item.get("candidate") or {}
            # Bibliographic entries may legitimately contain words such as
            # ``Traceability`` or ``Prompting``. They are rendered as a
            # separate reference artifact and must not be classified as
            # main-body engineering instructions.  Filter both the canonical
            # paragraph list and candidate_text because legacy/model outputs
            # can update only one of the two representations.
            paragraph_text = "\n".join(
                str(paragraph.get("text") or "")
                for paragraph in candidate.get("paragraphs") or []
                if isinstance(paragraph, dict)
                and not str(paragraph.get("text") or "").lstrip().startswith("[[REFERENCE]]")
            )
            candidate_text = "\n".join(
                line for line in str(candidate.get("candidate_text") or "").splitlines()
                if not line.lstrip().startswith("[[REFERENCE]]")
            )
            text = "\n".join(part for part in (paragraph_text, candidate_text) if part)

            def contains_forbidden_term(term: str) -> bool:
                if re.fullmatch(r"[A-Za-z][A-Za-z0-9_ -]*", term):
                    return bool(re.search(
                        rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])",
                        text,
                        flags=re.IGNORECASE,
                    ))
                return term in text

            hits = sorted(term for term in forbidden_terms if term and contains_forbidden_term(term))
            if hits:
                findings.append(_finding(
                    "QG_MAIN_BODY_CONTAINS_APPENDIX_TOPIC",
                    "CONTENT",
                    "SECTION_CANDIDATE",
                    section_id,
                    f"主文章节包含仅允许在附录出现的内容：{', '.join(hits[:8])}。",
                    "删除主文中的部署、接口、Trace、审计或安装说明，并移动到APPENDIX合同章节。",
                    "WRITING_AGENT",
                    evidence_refs=[section_id],
                ))
        return findings

    @staticmethod
    def _recalculate_status(
        output: dict[str, Any],
        original_status: str,
        original_verdict: Any,
    ) -> None:
        findings = [item for item in output.get("findings") or [] if isinstance(item, dict)]
        if any(item.get("severity") == "P0" and item.get("blocking", True) for item in findings):
            status = "BLOCK"
        elif any(item.get("severity") == "P1" and item.get("blocking", True) for item in findings):
            status = "REVISE"
        else:
            status = original_status
        output["status"] = status
        result = output.get("result")
        if isinstance(result, dict) and "verdict" in result:
            if status == "BLOCK":
                result["verdict"] = "BLOCK"
            elif status == "REVISE":
                result["verdict"] = "REVISE"
            elif original_verdict is not None:
                result["verdict"] = original_verdict

    @staticmethod
    def validate_repository(root: Path) -> dict[str, Any]:
        root = root.resolve()
        prompt_root = root / "prompt_pack"
        registry = json.loads((prompt_root / "config" / "prompt_registry.json").read_text(encoding="utf-8"))
        prompt_ids = {item["prompt_id"] for item in registry.get("prompts", [])}
        profiles_doc = yaml.safe_load((prompt_root / "knowledge" / "section_profiles.yaml").read_text(encoding="utf-8"))
        profiles = {
            item["profile_id"]: item
            for item in profiles_doc.get("profiles", [])
            if isinstance(item, dict) and item.get("profile_id")
        }
        checks: dict[str, dict[str, Any]] = {}

        def record(track_id: str, passed: bool, evidence: list[str], detail: str) -> None:
            checks[track_id] = {
                "passed": bool(passed),
                "evidence": evidence,
                "detail": detail,
            }

        scheme_prompt = (prompt_root / "prompts" / "scheme" / "scheme_extract.md").read_text(encoding="utf-8")
        record("B1", {"P-SCHEME-EXTRACT", "P-SCHEME-CRITIC"} <= prompt_ids and "逐规则绑定来源" in scheme_prompt,
               ["P-SCHEME-EXTRACT", "P-SCHEME-CRITIC", "AgentPromptKernelValidator._audit_scheme"],
               "Scheme规则必须逐条绑定来源，外推不得升级为强制规则。")

        relation_schema = json.loads((prompt_root / "schemas" / "common" / "project_relation.schema.json").read_text(encoding="utf-8"))
        record("B2", {"P-PROJECT-DEFINITION-EXTRACT", "P-PROJECT-DEFINITION-CRITIC"} <= prompt_ids
               and "relation_type" in relation_schema.get("properties", {}),
               ["project_relation.schema.json", "AgentPromptKernelValidator._audit_project_relations"],
               "事实图、关系图和Proposal Contract具备确定性关系校验。")

        fact_prompt = (prompt_root / "prompts" / "fact" / "fact_extract.md").read_text(encoding="utf-8")
        record("B3", {"P-FACT-EXTRACT", "P-FACT-CRITIC"} <= prompt_ids and "单一可判真命题" in fact_prompt,
               ["P-FACT-EXTRACT", "P-FACT-CRITIC", "AgentPromptKernelValidator._audit_facts"],
               "事实记录检查原子性、主体、时间、数字绑定、来源和覆盖。")

        graph = yaml.safe_load((prompt_root / "knowledge" / "proposal_argument_graph.yaml").read_text(encoding="utf-8"))
        record("B4", {"P-ARGUMENT-ARCHITECTURE", "P-ARGUMENT-ARCHITECTURE-CRITIC"} <= prompt_ids
               and graph.get("constraints", {}).get("central_proposition", {}).get("maximum") == 1,
               ["proposal_argument_graph.yaml", "P-ARGUMENT-ARCHITECTURE"],
               "唯一中心命题和研究设计闭环已形成图谱契约。")

        blueprint_prompt = (prompt_root / "prompts" / "writing" / "write_blueprint.md").read_text(encoding="utf-8")
        record("B5", all(token in blueprint_prompt for token in ["argument_role", "primary_claim_id", "required_evidence_ids", "novel_content_key"]),
               ["P-WRITE-BLUEPRINT", "section_contract.schema.json"],
               "段落蓝图绑定论点、证据、推理角色和唯一新增信息。")

        core_present = CORE_SECTION_PROFILES <= set(profiles)
        rule_signatures = [
            tuple(item.get("acceptance_rules") or [])
            for profile_id, item in profiles.items()
            if profile_id in CORE_SECTION_PROFILES
        ]
        profiles_distinct = len(rule_signatures) == len(set(rule_signatures))
        record("B6", core_present and profiles_distinct,
               ["section_profiles.yaml", *sorted(CORE_SECTION_PROFILES)],
               "核心章节使用独立Profile与验收规则。")

        record("B7", "P-TARGETED-REPAIR" in prompt_ids and CRITIC_PROMPTS <= prompt_ids,
               ["P-TARGETED-REPAIR", "AgentPromptKernelValidator._audit_finding_precision", "AgentPromptKernelValidator._audit_repair_scope"],
               "Critic Finding可定位，Repair仅允许修改白名单路径。")

        record("B8", {"P-EXPRESSION-POLISH", "P-EXPRESSION-CRITIC"} <= prompt_ids,
               ["P-EXPRESSION-POLISH", "P-EXPRESSION-CRITIC", "AgentPromptKernelValidator._audit_structure_preservation"],
               "表达编辑保持语义身份、数字、引用和结构块。")

        record("B9", "CONCLUSION" in profiles,
               ["section_profiles.yaml:CONCLUSION", "AgentPromptKernelValidator._audit_conclusion"],
               "结论必须回答全部研究问题、回扣中心命题且不得引入新命题。")

        appendix_rules = profiles.get("APPENDIX", {}).get("acceptance_rules") or []
        record("B10", "APPENDIX" in profiles and any("主申请书分离" in str(rule) for rule in appendix_rules),
               ["section_profiles.yaml:APPENDIX", "AgentPromptKernelValidator._replace_document_statistics_with_main_body_only"],
               "主文阻断工程说明，附录从主文重复统计中排除。")

        return {
            "track": "B",
            "status": "PASS" if all(item["passed"] for item in checks.values()) else "FAIL",
            "checks": checks,
        }
