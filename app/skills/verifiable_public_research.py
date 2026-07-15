from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from .base import SkillContext, SkillResult
from .public_research import PublicResearchArchiveError, PublicResearchArchiveSkill
from .research_audit import upgrade_archive_result
from .research_plan import deduplicate_candidates, normalize_and_validate_plan

_DUPLICATE_ISSUES: ContextVar[tuple[dict[str, Any], ...]] = ContextVar("research_duplicate_issues", default=())


class VerifiablePublicResearchArchiveSkill(PublicResearchArchiveSkill):
    """Track-C production wrapper around the existing retrieval/archive implementation."""

    version = "2.0.0"
    description = "Plan-validated public search with canonical deduplication, hash verification, coverage evidence, and claim binding support."

    def run(self, payload: dict[str, Any], context: SkillContext) -> SkillResult:
        strict = bool(payload.get("require_structured_plan", False))
        try:
            normalized_plan, validation = normalize_and_validate_plan(payload.get("plan") or {}, strict=strict)
        except ValueError as exc:
            raise PublicResearchArchiveError(str(exc)) from exc
        if validation["status"] == "BLOCK":
            codes = [str(item.get("code")) for item in validation["findings"]]
            raise PublicResearchArchiveError("Research plan validation failed: " + ", ".join(codes))
        token = _DUPLICATE_ISSUES.set(())
        try:
            effective = dict(payload)
            effective["plan"] = {**(payload.get("plan") or {}), "queries": normalized_plan["queries"]}
            result = super().run(effective, context)
            return upgrade_archive_result(result, normalized_plan, validation, list(_DUPLICATE_ISSUES.get()))
        finally:
            _DUPLICATE_ISSUES.reset(token)

    @staticmethod
    def _deduplicate(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
        kept, issues = deduplicate_candidates(values)
        _DUPLICATE_ISSUES.set((*_DUPLICATE_ISSUES.get(), *issues))
        return kept

    def _load_recorded(self, record_file):
        return self._deduplicate(super()._load_recorded(record_file))

    def _load_connector(self, connector_file, planned_queries):
        candidates, manifest = super()._load_connector(connector_file, planned_queries)
        return self._deduplicate(candidates), manifest

    def _search_searxng(self, queries, max_results):
        return self._deduplicate(super()._search_searxng(queries, max_results))
