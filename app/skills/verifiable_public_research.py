from __future__ import annotations

import json
import os
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from .academic_search import AcademicDiscoveryError, AcademicSearchClient
from .base import SkillContext, SkillResult
from .public_research import (
    PublicResearchArchiveSkill,
    PublicResearchPlanContractError,
    PublicResearchRetrievalError,
)
from .research_audit import upgrade_archive_result
from .research_execution import (
    ResearchExecutionContractError,
    build_plan_lock,
    validate_connector_execution,
)
from .research_plan import deduplicate_candidates, normalize_and_validate_plan
from .research_screening import screen_and_select_candidates
from .research_quality import build_retrieval_health
from .research_validation import write_validation_bundle
from ..util import safe_filename, utc_now, write_json

_DUPLICATE_ISSUES: ContextVar[tuple[dict[str, Any], ...]] = ContextVar("research_duplicate_issues", default=())
_NORMALIZED_PLAN: ContextVar[dict[str, Any] | None] = ContextVar("research_normalized_plan", default=None)
_STRICT_RESEARCH: ContextVar[bool] = ContextVar("research_strict_execution", default=False)
_SELECTION_REPORT: ContextVar[dict[str, Any] | None] = ContextVar("research_selection_report", default=None)
_EXECUTION_REPORT: ContextVar[dict[str, Any] | None] = ContextVar("research_execution_report", default=None)
_EFFECTIVE_MAX_RESULTS: ContextVar[int] = ContextVar("research_effective_max_results", default=40)
_QUALITY_PROFILE: ContextVar[str] = ContextVar("research_quality_profile", default="legacy")


class VerifiablePublicResearchArchiveSkill(PublicResearchArchiveSkill):
    """WF-3 public research with plan locking, academic discovery and audited coverage."""

    version = "2.3.0"
    description = (
        "Plan-validated public/academic search with exact execution binding, deterministic "
        "screening, canonical deduplication, coverage evidence and claim binding support."
    )

    @staticmethod
    def _candidate_budget_per_query() -> int:
        raw = os.getenv("PUBLIC_SEARCH_CANDIDATES_PER_QUERY", "8").strip()
        try:
            value = int(raw)
        except ValueError:
            value = 8
        return max(3, min(value, 25))

    @staticmethod
    def _minimum_results_per_query() -> int:
        raw = os.getenv("PUBLIC_RESEARCH_MIN_SOURCES_PER_QUERY", "3").strip()
        try:
            value = int(raw)
        except ValueError:
            value = 3
        return max(1, min(value, 8))

    def _academic_connector_file(
        self,
        provider: str,
        normalized_plan: dict[str, Any],
        context: SkillContext,
    ) -> tuple[Path, dict[str, Any]]:
        queries = list(normalized_plan.get("queries") or [])
        per_query = self._candidate_budget_per_query()
        discovery: dict[str, Any] | None = None
        academic_error: Exception | None = None
        try:
            discovery = AcademicSearchClient(self.settings).discover(
                queries,
                time_scope=normalized_plan.get("time_scope"),
                per_query_limit=per_query,
            )
        except Exception as exc:
            academic_error = exc
            if provider == "academic":
                raise PublicResearchRetrievalError(
                    f"Academic discovery failed: {exc}",
                    details={"exception_type": type(exc).__name__},
                ) from exc

        if discovery is None:
            discovery = {
                "schema_version": "1.0",
                "run_id": f"academic-discovery-fallback-{safe_filename(context.workflow_id or 'workflow')}",
                "connector": "wf3-hybrid-discovery",
                "created_at": utc_now(),
                "agent_generated_queries": list(queries),
                "providers": [],
                "responses": [{"query": query, "retrieved_at": utc_now(), "results": []} for query in queries],
                "provider_runs": [],
                "failures": [],
                "per_query_limit": per_query,
                "time_scope": normalized_plan.get("time_scope"),
            }
        if academic_error is not None:
            discovery.setdefault("failures", []).append(
                {
                    "provider": "academic-multi-source",
                    "category": "RETRIEVAL",
                    "error_code": "ACADEMIC_DISCOVERY_ALL_PROVIDERS_FAILED",
                    "message": f"{type(academic_error).__name__}: {academic_error}",
                }
            )

        if provider == "hybrid":
            try:
                # Bypass this wrapper's screening override so the web channel contributes
                # its full per-query candidate pool before unified screening/deduplication.
                web_candidates, web_failures = PublicResearchArchiveSkill._search_searxng(
                    self,
                    queries,
                    per_query,
                )
            except Exception as exc:
                web_candidates, web_failures = [], [
                    {
                        "provider": "searxng",
                        "category": "RETRIEVAL",
                        "error_code": "HYBRID_SEARXNG_DISCOVERY_ERROR",
                        "message": f"{type(exc).__name__}: {exc}",
                    }
                ]
            by_query = {
                str(row.get("query") or ""): row
                for row in discovery.get("responses") or []
                if isinstance(row, dict)
            }
            for candidate in web_candidates:
                query = str(candidate.get("matched_query") or "").strip()
                if query not in by_query:
                    continue
                candidate = dict(candidate)
                candidate["matched_queries"] = [query]
                verification = dict(candidate.get("verification") or {})
                verification.update(
                    {
                        "status": "SEARXNG_DISCOVERY_RETURNED",
                        "discovery_provider": "searxng",
                        "query": query,
                        "matched_queries": [query],
                    }
                )
                candidate["verification"] = verification
                candidate["academic_provider"] = "searxng"
                by_query[query].setdefault("results", []).append(candidate)
            discovery.setdefault("providers", []).append("searxng")
            discovery.setdefault("failures", []).extend(web_failures)
            discovery.setdefault("provider_runs", []).append(
                {
                    "provider": "searxng",
                    "result_count": len(web_candidates),
                    "query_failures": web_failures,
                    "raw_response": {
                        "note": "SearXNG candidate projection; source snapshots are archived separately.",
                        "candidates": web_candidates,
                    },
                }
            )

        query_id_by_text = {
            str(item.get("query") or ""): str(item.get("query_id") or "")
            for item in normalized_plan.get("query_items") or []
            if isinstance(item, dict)
        }
        for row in discovery.get("responses") or []:
            if not isinstance(row, dict):
                continue
            query = str(row.get("query") or "")
            query_id = query_id_by_text.get(query)
            if query_id:
                row["query_id"] = query_id
        discovery["plan_hash"] = build_plan_lock(normalized_plan)["plan_hash"]
        discovery["retrieval_provider"] = provider

        root = (
            Path(context.data_dir)
            / "research_discovery_inputs"
            / safe_filename(context.project_id)
            / safe_filename(context.workflow_id or "workflow")
        )
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"{safe_filename(str(discovery.get('run_id') or 'academic-discovery'))}.json"
        write_json(path, discovery)
        return path, discovery

    def run(self, payload: dict[str, Any], context: SkillContext) -> SkillResult:
        strict = bool(payload.get("require_structured_plan", False))
        quality_profile = str(payload.get("research_quality_profile") or "legacy").strip().lower()
        original_provider = str(payload.get("provider") or self.settings.public_search_provider).lower()
        try:
            normalized_plan, validation = normalize_and_validate_plan(payload.get("plan") or {}, strict=strict)
        except ValueError as exc:
            raise PublicResearchPlanContractError(str(exc)) from exc
        if validation["status"] == "BLOCK":
            codes = [str(item.get("code")) for item in validation["findings"]]
            raise PublicResearchPlanContractError(
                "Research plan validation failed: " + ", ".join(codes),
                details={"validation": validation, "normalized_plan": normalized_plan},
            )

        configured_max = max(1, min(int(payload.get("max_results") or self.settings.public_search_max_results), 100))
        effective_max = configured_max
        if strict and quality_profile == "proposal_related_work":
            minimum_required = (
                len(normalized_plan.get("queries") or [])
                * self._minimum_results_per_query()
            )
            # The archive is validated after fetch failures and identity/content
            # deduplication. Reserving exactly the minimum makes one ordinary
            # failure deterministically fatal, so keep a bounded 20%/five-source
            # screening margin without weakening the actual coverage threshold.
            capacity_margin = max(5, (minimum_required + 4) // 5)
            effective_max = max(
                configured_max,
                min(100, minimum_required + capacity_margin),
            )

        token_duplicate = _DUPLICATE_ISSUES.set(())
        token_plan = _NORMALIZED_PLAN.set(normalized_plan)
        token_strict = _STRICT_RESEARCH.set(strict)
        token_selection = _SELECTION_REPORT.set(None)
        token_execution = _EXECUTION_REPORT.set(None)
        token_max = _EFFECTIVE_MAX_RESULTS.set(effective_max)
        token_quality = _QUALITY_PROFILE.set(quality_profile)
        discovery_file: Path | None = None
        discovery_manifest: dict[str, Any] | None = None
        try:
            effective = dict(payload)
            effective["plan"] = {**(payload.get("plan") or {}), "queries": normalized_plan["queries"]}
            effective["max_results"] = effective_max
            if original_provider in {"academic", "hybrid"}:
                discovery_file, discovery_manifest = self._academic_connector_file(
                    original_provider,
                    normalized_plan,
                    context,
                )
                effective["provider"] = "connector"
                effective["connector_file"] = str(discovery_file)
            try:
                result = super().run(effective, context)
            except ResearchExecutionContractError as exc:
                raise PublicResearchPlanContractError(
                    str(exc),
                    details={"code": exc.code, **exc.details},
                ) from exc

            retrieval_health = build_retrieval_health(
                discovery_manifest,
                retrieval_provider=original_provider,
                queries=list(normalized_plan.get("queries") or []),
            )
            result = upgrade_archive_result(
                result,
                normalized_plan,
                validation,
                list(_DUPLICATE_ISSUES.get()),
                quality_profile=quality_profile,
                selection_report=_SELECTION_REPORT.get(),
                execution_report=_EXECUTION_REPORT.get(),
                min_sources_per_query=self._minimum_results_per_query(),
                retrieval_health=retrieval_health,
            )
            if original_provider in {"academic", "hybrid"}:
                manifest_path = Path(result.output["archive_manifest"])
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest["provider"] = original_provider
                manifest["retrieval_mode"] = (
                    "LIVE_ACADEMIC_MULTI_SOURCE"
                    if original_provider == "academic"
                    else "LIVE_HYBRID_ACADEMIC_WEB"
                )
                manifest["discovery_input"] = str(discovery_file) if discovery_file else None
                manifest["discovery_providers"] = list((discovery_manifest or {}).get("providers") or [])
                manifest["retrieval_health"] = retrieval_health
                write_json(manifest_path, manifest)
                result.output["mode"] = manifest["retrieval_mode"]
                result.output["discovery_input"] = manifest["discovery_input"]
                result.output["discovery_providers"] = manifest["discovery_providers"]
                result.output["retrieval_health"] = retrieval_health

            # Persist the quality-observation bundle before the sufficiency gate can
            # raise. An INSUFFICIENT run is exactly the run that needs the best audit
            # trail for diagnosis, so validation artifacts must not depend on PASS.
            validation_root = write_validation_bundle(
                context=context,
                original_plan=dict(payload.get("plan") or {}),
                normalized_plan=normalized_plan,
                plan_validation=validation,
                provider=original_provider,
                quality_profile=quality_profile,
                configured_max_results=configured_max,
                effective_max_results=effective_max,
                discovery_manifest=discovery_manifest,
                discovery_input=str(discovery_file) if discovery_file else None,
                result_output=result.output,
            )
            result.output["validation_bundle_dir"] = str(validation_root)
            result.artifacts = list(result.artifacts or []) + [str(validation_root / "00_run_manifest.json"), str(validation_root / "08_quality_summary.json")]

            if strict and quality_profile == "proposal_related_work" and result.output.get("coverage", {}).get("status") != "PASS":
                raise PublicResearchRetrievalError(
                    "Proposal related-work research coverage is insufficient; synthesis is blocked until retrieval depth/diversity gaps are repaired.",
                    details={
                        "coverage": result.output.get("coverage"),
                        "selection_report": result.output.get("selection_report"),
                        "archive_manifest": result.output.get("archive_manifest"),
                        "validation_bundle_dir": result.output.get("validation_bundle_dir"),
                        "source_count": len(result.output.get("source_catalog") or []),
                    },
                )
            return result
        finally:
            _DUPLICATE_ISSUES.reset(token_duplicate)
            _NORMALIZED_PLAN.reset(token_plan)
            _STRICT_RESEARCH.reset(token_strict)
            _SELECTION_REPORT.reset(token_selection)
            _EXECUTION_REPORT.reset(token_execution)
            _EFFECTIVE_MAX_RESULTS.reset(token_max)
            _QUALITY_PROFILE.reset(token_quality)

    @staticmethod
    def _deduplicate(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
        kept, issues = deduplicate_candidates(values)
        _DUPLICATE_ISSUES.set((*_DUPLICATE_ISSUES.get(), *issues))
        return kept

    def _screen(self, values: list[dict[str, Any]]) -> list[dict[str, Any]]:
        plan = _NORMALIZED_PLAN.get() or {}
        strict = _STRICT_RESEARCH.get()
        if not values:
            return values
        selected, report = screen_and_select_candidates(
            values,
            plan,
            max_results=_EFFECTIVE_MAX_RESULTS.get(),
            strict=strict,
            min_per_query=self._minimum_results_per_query(),
            enforce_semantic_relevance=(strict and _QUALITY_PROFILE.get() == "proposal_related_work"),
        )
        _SELECTION_REPORT.set(report)
        # Screening performs identity dedup before archive selection.  Re-emit those
        # duplicate facts through the established audit channel so Track-C keeps its
        # DUPLICATE_SOURCE / SOURCE_CONFLICT semantics instead of hiding them inside a
        # new shortlist-only report.
        duplicate_events = []
        for issue in report.get("issues") or []:
            if issue.get("code") != "CANDIDATE_DEDUPLICATED":
                continue
            duplicate_events.append({
                key: value
                for key, value in issue.items()
                if key not in {"type", "code"}
            })
        if duplicate_events:
            _DUPLICATE_ISSUES.set((*_DUPLICATE_ISSUES.get(), *duplicate_events))
        return selected

    def _load_recorded(self, record_file):
        return self._deduplicate(self._screen(super()._load_recorded(record_file)))

    def _load_connector(self, connector_file, planned_queries):
        plan = _NORMALIZED_PLAN.get() or {}
        # Validate the raw execution envelope before the legacy loader classifies
        # missing query responses as a configuration problem.  In strict WF-3 this is
        # a plan-contract violation, not something that editing .env can repair.
        if _STRICT_RESEARCH.get():
            path = Path(connector_file)
            try:
                raw_manifest = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                raw_manifest = None
            if isinstance(raw_manifest, dict):
                _EXECUTION_REPORT.set(
                    validate_connector_execution(raw_manifest, plan, strict=True)
                )
        candidates, manifest = super()._load_connector(connector_file, planned_queries)
        report = validate_connector_execution(manifest, plan, strict=_STRICT_RESEARCH.get())
        _EXECUTION_REPORT.set(report)
        return self._deduplicate(self._screen(candidates)), manifest

    def _search_searxng(self, queries, max_results):
        # Search each query to the candidate budget.  The archive limit is applied only
        # after all query pools have been screened, so a small global max_results cannot
        # silently starve later research questions.
        candidate_budget = self._candidate_budget_per_query()
        candidates, query_failures = super()._search_searxng(queries, candidate_budget)
        _EXECUTION_REPORT.set(
            {
                "status": "PASS" if not query_failures else "WARN",
                "plan_hash": build_plan_lock(_NORMALIZED_PLAN.get() or {})["plan_hash"],
                "planned_query_count": len(queries),
                "executed_query_count": len(queries) - len({str(item.get('query') or '') for item in query_failures}),
                "findings": query_failures,
            }
        )
        return self._deduplicate(self._screen(candidates)), query_failures
