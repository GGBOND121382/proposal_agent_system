from __future__ import annotations

import asyncio
import copy
from typing import Any

from .research import (
    PublicResearchConfigurationError,
    PublicResearchIntegrityError,
    PublicResearchService,
    _facade_error,
)
from .skills.executor import SkillExecutionError, SkillExecutor
from .skills.research_audit import verify_research_archive
from .util import sha256_json


WF3B_WORKFLOW_TYPE = "WF-3B_TOPIC_BACKGROUND_RESEARCH"
WF3B_PLAN_PROMPT = "P-BACKGROUND-RESEARCH-PLAN"
WF3B_PLAN_CRITIC_PROMPT = "P-BACKGROUND-RESEARCH-PLAN-CRITIC"
WF3B_SYNTHESIS_PROMPT = "P-BACKGROUND-RESEARCH-SYNTHESIS"
WF3B_RESEARCH_CRITIC = "P-BACKGROUND-RESEARCH-CRITIC"

BACKGROUND_QUALITY_PROFILE = "application_background"

# The eight background dimensions of WF-3B (plan §5.3).  The runtime freezes the
# required subset per workflow; the model may only plan/cover inside this frozen
# set and may never invent new dimensions.
BACKGROUND_DIMENSIONS: tuple[str, ...] = (
    "APPLICATION_SCENARIO",
    "STAKEHOLDER_AND_PAIN",
    "INDUSTRY_SCALE_AND_TREND",
    "POLICY_STANDARD_AND_PROGRAM",
    "REPRESENTATIVE_CASE",
    "CURRENT_ADOPTION",
    "OPERATIONAL_CONSTRAINT",
    "RESEARCH_SIGNIFICANCE",
)
BACKGROUND_DIMENSION_SET = frozenset(BACKGROUND_DIMENSIONS)

# Synthesis claims that do not name a valid dimension still produce a card, but
# they never count toward the frozen dimension coverage.
UNSCOPED_DIMENSION = "UNSCOPED"


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _nested_options(options: dict[str, Any] | None) -> dict[str, Any]:
    source = copy.deepcopy(options or {})
    merged = copy.deepcopy(source)
    # ``wf3b`` is the original programmatic shape.  The web UI uses the more
    # descriptive ``background_research`` envelope; both are accepted at the
    # same boundary so UI/API callers reach the identical normalized contract.
    for key in ("wf3b", "background_research"):
        nested = source.get(key)
        if isinstance(nested, dict):
            merged.update(nested)
    return merged


def normalize_required_dimensions(
    options: dict[str, Any] | None,
) -> tuple[list[str], str]:
    """Freeze the required background dimension set for one WF-3B run.

    An explicit ``required_dimensions``/``background_dimensions`` option selects
    a subset of the eight canonical dimensions; otherwise all eight are
    required.  Unknown dimensions fail at workflow creation rather than later
    inside the search skill.
    """

    source = _nested_options(options)
    raw = source.get("required_dimensions")
    if raw is None:
        raw = source.get("background_dimensions")
    if raw is None:
        return list(BACKGROUND_DIMENSIONS), "DEFAULT_ALL"
    if not isinstance(raw, (list, tuple)):
        raise ValueError("WF-3B required_dimensions 必须是背景维度数组")
    selected: list[str] = []
    for item in raw:
        dimension = _clean_text(item).upper()
        if not dimension:
            continue
        if dimension not in BACKGROUND_DIMENSION_SET:
            raise ValueError(
                "WF-3B required_dimensions 含未知背景维度："
                + dimension
                + "；可选值为 "
                + "、".join(BACKGROUND_DIMENSIONS)
            )
        if dimension not in selected:
            selected.append(dimension)
    if not selected:
        raise ValueError("WF-3B required_dimensions 至少需要一个有效背景维度")
    ordered = [name for name in BACKGROUND_DIMENSIONS if name in selected]
    return ordered, "WORKFLOW_OPTIONS"


def resolve_wf3b_topic(
    options: dict[str, Any] | None,
    *,
    wf1_project_definition: dict[str, Any] | None = None,
) -> tuple[str | None, str]:
    """Resolve the background research topic.

    An explicit option always wins; otherwise the topic is derived from the
    completed WF-1 project definition.  A plain unresolved topic is reported as
    ``UNRESOLVED`` so the workflow layer can apply its prerequisite/input
    blocking semantics instead of inventing a topic.
    """

    source = _nested_options(options)
    explicit = _clean_text(
        source.get("topic")
        or source.get("topic_override")
        or source.get("topic_description")
        or source.get("background_topic")
    )
    if explicit:
        return explicit, "WORKFLOW_OPTIONS"
    definition = wf1_project_definition if isinstance(wf1_project_definition, dict) else {}
    title = _clean_text(definition.get("project_title"))
    if title:
        research_object = _clean_text(definition.get("research_object"))
        if research_object and research_object not in title:
            return f"{title}：{research_object}", "WF1_PROJECT_DEFINITION"
        return title, "WF1_PROJECT_DEFINITION"
    problem = definition.get("problem_definition")
    statement = _clean_text((problem or {}).get("problem_statement")) if isinstance(problem, dict) else ""
    if statement:
        return statement[:200], "WF1_PROBLEM_STATEMENT"
    return None, "UNRESOLVED"


def wf3b_topic_id(project_id: str, topic: str) -> str:
    return "topic-" + sha256_json({"project_id": str(project_id), "topic": topic})[:20]


def normalize_wf3b_options(
    options: dict[str, Any] | None,
    *,
    project_id: str = "",
    wf1_project_definition: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the normalized WF-3B options consumed by workflow state.

    The returned mapping always carries the frozen ``required_dimensions`` and
    the topic resolution metadata.  When no topic can be resolved the ``topic``
    key is absent and ``topic_origin`` is ``UNRESOLVED``; the caller decides the
    blocking semantics.
    """

    source = _nested_options(options)
    normalized = copy.deepcopy(source)
    normalized.pop("wf3b", None)
    normalized.pop("background_research", None)
    dimensions, dimensions_origin = normalize_required_dimensions(source)
    normalized["required_dimensions"] = dimensions
    normalized["required_dimensions_origin"] = dimensions_origin
    topic, topic_origin = resolve_wf3b_topic(
        source,
        wf1_project_definition=wf1_project_definition,
    )
    normalized["topic_origin"] = topic_origin
    if topic:
        normalized["topic"] = topic
        normalized["topic_id"] = _clean_text(source.get("topic_id")) or wf3b_topic_id(
            project_id,
            topic,
        )
    else:
        normalized.pop("topic", None)
        normalized.pop("topic_id", None)
    return normalized


def normalize_background_plan(
    plan: dict[str, Any] | None,
    *,
    required_dimensions: list[str] | tuple[str, ...],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Freeze the background dimension boundary onto the model-authored plan.

    The model only supplies semantic queries.  This function injects the frozen
    ``required_dimensions``, drops query dimension tags outside the frozen set,
    and reports (without rewriting) which frozen dimensions the plan does not
    cover.  Findings are deterministic facts for the Critic and for audit; they
    never fabricate coverage.
    """

    normalized = copy.deepcopy(plan) if isinstance(plan, dict) else {}
    required = [
        _clean_text(item).upper()
        for item in required_dimensions
        if _clean_text(item).upper() in BACKGROUND_DIMENSION_SET
    ] or list(BACKGROUND_DIMENSIONS)
    required_set = set(required)
    findings: list[dict[str, Any]] = []
    normalized["task_type"] = "PUBLIC_BACKGROUND_RESEARCH"
    normalized["required_dimensions"] = list(required)

    query_items = [
        item
        for item in normalized.get("queries") or []
        if isinstance(item, dict) and _clean_text(item.get("query") or item.get("query_text"))
    ]
    for item in query_items:
        if not isinstance(item, dict):
            continue
        raw_dimensions = item.get("dimensions") or item.get("background_dimensions")
        if raw_dimensions is None and item.get("dimension") is not None:
            raw_dimensions = [item.get("dimension")]
        raw_dimensions = raw_dimensions or []
        if isinstance(raw_dimensions, str):
            raw_dimensions = [raw_dimensions]
        kept: list[str] = []
        for raw in raw_dimensions:
            dimension = _clean_text(raw).upper()
            if not dimension:
                continue
            if dimension not in BACKGROUND_DIMENSION_SET:
                findings.append({
                    "code": "BACKGROUND_PLAN_UNKNOWN_DIMENSION",
                    "severity": "P1",
                    "dimension": dimension,
                    "query": _clean_text(item.get("query") or item.get("query_text")),
                })
                continue
            if dimension not in required_set:
                findings.append({
                    "code": "BACKGROUND_PLAN_DIMENSION_OUT_OF_SCOPE",
                    "severity": "P1",
                    "dimension": dimension,
                    "query": _clean_text(item.get("query") or item.get("query_text")),
                })
                continue
            if dimension not in kept:
                kept.append(dimension)
        if raw_dimensions:
            item["dimensions"] = kept
            if kept:
                item["dimension"] = kept[0]

    # Every model query that passed the plan Critic is approved.  Keep that set
    # intact here: execute_all_approved_queries is part of the audited retrieval
    # contract, so silently applying the generic WF-3 twelve-query cap would be
    # both a destructive plan delta and an incomplete execution.
    normalized["queries"] = query_items

    normalized["research_questions"] = [
        f"核验背景维度 {dimension} 的公开事实、代表性来源与适用边界"
        for dimension in required
    ]
    for item in query_items:
        dimensions = item.get("dimensions") or []
        item["linked_question_indexes"] = [
            required.index(dimension)
            for dimension in dimensions
            if dimension in required
        ]
    normalized["source_priorities"] = list(
        normalized.get("source_priorities")
        or [
            "官方机构、政府或军方公开发布",
            "标准组织正式文本",
            "原始论文、研究机构技术报告",
        ]
    )

    covered = {
        dimension
        for item in query_items
        for dimension in item.get("dimensions") or []
    }

    missing = [name for name in required if name not in covered]
    if missing:
        findings.append({
            "code": "BACKGROUND_PLAN_DIMENSION_UNCOVERED",
            "severity": "P1",
            "dimensions": missing,
        })
    normalized["dimension_coverage"] = {
        name: {"status": "PLANNED" if name in covered else "UNPLANNED"}
        for name in required
    }
    return normalized, findings


def merge_background_followup_plan(plan: dict[str, Any], feedback: dict[str, Any]) -> dict[str, Any]:
    """Append model-authored follow-ups while retaining the reviewed plan verbatim.

    Called before output validation and the plan Critic.  The Critic therefore
    reviews exactly the queries that the search step will execute.
    """
    previous = feedback.get("previous_plan")
    if not isinstance(previous, dict) or not previous.get("queries"):
        return plan
    merged = copy.deepcopy(previous)
    queries = merged["queries"]
    seen = {str(item.get("query") or "").strip() for item in queries}
    ids = {str(item.get("query_id") or "") for item in queries}
    limit = min(24 - len(queries), max(0, int(feedback.get("max_additional_queries") or 0)))
    added = 0
    for item in plan.get("queries") or []:
        if not isinstance(item, dict):
            continue
        query = str(item.get("query") or "").strip()
        if not query or query in seen or added >= limit:
            continue
        new_item = copy.deepcopy(item)
        next_id = len(queries) + 1
        while f"query-followup-{next_id:03d}" in ids:
            next_id += 1
        new_item["query_id"] = f"query-followup-{next_id:03d}"
        queries.append(new_item)
        seen.add(query)
        ids.add(new_item["query_id"])
        added += 1
    return merged


def background_search_feedback(state: dict[str, Any]) -> dict[str, Any] | None:
    """Offer one bounded, source-informed replan when query coverage is missing."""
    rounds = int(state.get("background_search_refinement_rounds") or 0)
    if rounds >= 1:
        return None
    search = state.get("background_search_results") or {}
    previous = state.get("background_last_executed_plan") or {}
    if not previous.get("queries") or len(previous["queries"]) >= 24:
        return None
    gaps = search.get("research_gaps") or (search.get("research_sufficiency") or {}).get("research_gaps") or []
    actionable = [gap for gap in gaps if set(gap.get("gap_types") or []) & {"TARGET_ENTITY", "DEPTH", "AUTHORITY"}]
    if not actionable:
        return None
    summaries = [
        f"{item.get('title', '')} | {item.get('url', '')}\n{str(item.get('excerpt') or '')[:800]}"
        for item in search.get("source_catalog") or [] if isinstance(item, dict)
    ][:20]
    return {
        "round": rounds + 1,
        "previous_plan": copy.deepcopy(previous),
        "source_summaries": summaries,
        "gaps": [f"{gap.get('query') or ''}: {gap.get('description') or ''} ({', '.join(gap.get('gap_types') or [])})" for gap in actionable][:24],
        "max_additional_queries": min(6, 24 - len(previous["queries"])),
    }


def background_execution_contract(plan: dict[str, Any]) -> dict[str, Any]:
    """Force the WF-3B retrieval execution contract onto the approved plan.

    ``require_web_discovery`` is runtime-owned for background research: a pure
    Academic-only success must never let the background workflow complete, so
    the WEB_SEARCH channel is always required regardless of what the model
    plan declared.  ``browser_search`` is a required provider as well: the
    background workflow needs at least one independent real web channel beyond
    the local SearXNG aggregate, whose upstream engines can silently degrade.
    """

    contracted = copy.deepcopy(plan) if isinstance(plan, dict) else {}
    contracted["require_web_discovery"] = True
    channels = [
        _clean_text(item).upper()
        for item in contracted.get("required_channels") or []
        if _clean_text(item)
    ]
    if "WEB_SEARCH" not in channels:
        channels.append("WEB_SEARCH")
    contracted["required_channels"] = channels
    requirements = contracted.get("provider_execution_requirements")
    requirements = dict(requirements) if isinstance(requirements, dict) else {}
    required_providers = [
        _clean_text(item).lower()
        for item in requirements.get("required_providers") or []
        if _clean_text(item)
    ]
    if "browser_search" not in required_providers:
        required_providers.append("browser_search")
    requirements["required_providers"] = required_providers
    requirements["execute_all_approved_queries"] = bool(
        requirements.get("execute_all_approved_queries", True)
    )
    contracted["provider_execution_requirements"] = requirements
    return contracted


def build_background_cards(
    synthesis: dict[str, Any],
    research_output: dict[str, Any],
    claim_validation: dict[str, Any] | None,
    *,
    required_dimensions: list[str] | tuple[str, ...],
    topic_id: str = "",
) -> dict[str, Any]:
    """Build deterministic background evidence cards from validated claims.

    Only claims that survived the deterministic claim-source binding
    validation become cards.  ``card_id`` is runtime-generated and stable for
    the same topic/claim/dimension triple.  Frozen dimensions without any
    validated claim become explicit ``background_gaps``; they are never filled
    from model memory.
    """

    required = [
        _clean_text(item).upper()
        for item in required_dimensions
        if _clean_text(item).upper() in BACKGROUND_DIMENSION_SET
    ] or list(BACKGROUND_DIMENSIONS)
    validation = claim_validation if isinstance(claim_validation, dict) else {}
    bindings = {
        str(item.get("claim_id") or ""): item
        for item in validation.get("bindings") or []
        if isinstance(item, dict) and str(item.get("claim_id") or "").strip()
    }
    rejected_claim_ids = {
        str(item.get("claim_id") or "")
        for item in validation.get("findings") or []
        if isinstance(item, dict)
        and str(item.get("severity") or "") == "P0"
        and str(item.get("claim_id") or "").strip()
    }

    cards: list[dict[str, Any]] = []
    for claim in (synthesis or {}).get("claims") or []:
        if not isinstance(claim, dict):
            continue
        claim_id = _clean_text(claim.get("claim_id"))
        if not claim_id or claim_id in rejected_claim_ids:
            continue
        binding = bindings.get(claim_id) or {}
        dimension = _clean_text(
            claim.get("dimension") or claim.get("background_dimension")
        ).upper()
        if dimension not in BACKGROUND_DIMENSION_SET:
            dimension = UNSCOPED_DIMENSION
        cards.append({
            "card_id": "bgcard-"
            + sha256_json({
                "topic_id": str(topic_id),
                "claim_id": claim_id,
                "dimension": dimension,
            })[:16],
            "claim_id": claim_id,
            "dimension": dimension,
            "claim_text": str(claim.get("claim_text") or ""),
            "source_ids": [str(value) for value in binding.get("source_ids") or []],
            "evidence_mode": str(binding.get("evidence_mode") or ""),
            "scope_qualifiers": [
                str(value)
                for value in claim.get("scope_qualifiers") or claim.get("qualifiers") or []
            ],
            "target_section_profiles": [
                str(value) for value in claim.get("target_section_profiles") or []
            ],
            "conflicts": copy.deepcopy(claim.get("conflicts") or []),
            "limitations": copy.deepcopy(claim.get("limitations") or []),
        })

    coverage: dict[str, dict[str, Any]] = {}
    gaps: list[dict[str, Any]] = []
    for name in required:
        card_ids = [card["card_id"] for card in cards if card["dimension"] == name]
        coverage[name] = {
            "status": "COVERED" if card_ids else "GAP",
            "card_ids": card_ids,
        }
        if not card_ids:
            gaps.append({
                "gap_id": f"background-gap-{len(gaps) + 1:03d}",
                "scope": "DIMENSION",
                "dimension": name,
                "description": (
                    f"背景维度 {name} 没有任何通过来源绑定校验的证据卡；"
                    "不得用模型记忆补齐。"
                ),
            })
    return {
        "background_cards": cards,
        "background_dimensions": coverage,
        "background_gaps": gaps,
    }


class BackgroundResearchService(PublicResearchService):
    """WF-3B facade over the auditable public-research archive skill.

    Same execution substrate as :class:`PublicResearchService`, but the payload
    always enforces the ``application_background`` quality profile and the
    runtime-owned ``require_web_discovery=true`` execution contract.
    """

    def __init__(self, settings, skill_executor: SkillExecutor | None = None):
        super().__init__(settings, skill_executor)

    def simulated_search(self, plan: dict[str, Any]) -> dict[str, Any]:
        result = copy.deepcopy(super().simulated_search(plan))
        dimensions = [
            _clean_text(item).upper()
            for item in (plan or {}).get("required_dimensions") or []
            if _clean_text(item).upper() in BACKGROUND_DIMENSION_SET
        ] or list(BACKGROUND_DIMENSIONS)
        result["coverage"] = {
            "status": "PASS",
            "quality_profile": BACKGROUND_QUALITY_PROFILE,
            "dimensions": {name: {"status": "PASS"} for name in dimensions},
        }
        result["research_quality_profile"] = BACKGROUND_QUALITY_PROFILE
        return result

    async def search(
        self,
        plan: dict[str, Any],
        *,
        project_id: str,
        workflow_id: str | None,
        security_level: str = "PUBLIC",
    ) -> dict[str, Any]:
        if self.settings.public_search_provider == "disabled":
            raise PublicResearchConfigurationError("PUBLIC_SEARCH_PROVIDER is disabled")
        if self.skill_executor is None:
            raise PublicResearchConfigurationError("Background research skill executor is not configured")
        try:
            result = await asyncio.to_thread(
                self.skill_executor.execute,
                "public_research.archive",
                {
                    "provider": self.settings.public_search_provider,
                    "base_url": self.settings.public_search_base_url,
                    "record_file": self.settings.public_research_record_file,
                    "connector_file": self.settings.public_research_connector_file,
                    "max_results": self.settings.public_search_max_results,
                    # WF-3B carries no legacy replay burden: the structured plan
                    # contract and the application-background profile are always
                    # enforced, so a BLOCKING retrieval result cannot silently
                    # degrade into a completed background workflow.
                    "require_structured_plan": True,
                    "research_quality_profile": BACKGROUND_QUALITY_PROFILE,
                    "plan": background_execution_contract(plan),
                },
                project_id=project_id,
                workflow_id=workflow_id,
                security_level=security_level,
            )
        except SkillExecutionError as exc:
            raise _facade_error(exc) from exc
        output = result.output
        verification = output.get("archive_verification") or verify_research_archive(output.get("archive_manifest", ""))
        if verification.get("status") != "PASS":
            raise PublicResearchIntegrityError(
                "Background research archive failed hash verification",
                details={"verification": verification},
            )
        return output
