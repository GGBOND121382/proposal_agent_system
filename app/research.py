from __future__ import annotations

from pathlib import Path
from typing import Any

from .skills.executor import SkillExecutionError, SkillExecutor
from .skills.research_audit import verify_research_archive
from .skills.research_claims import validate_public_claims
from .util import sha256_json, sha256_text, write_json
from .logistics_application_content import REF_CATALOG as LOGISTICS_REF_CATALOG
from .transport_optimization_application_content import REF_CATALOG as TRANSPORT_REF_CATALOG


class PublicResearchError(RuntimeError):
    category = "RUNTIME"
    error_code = "PUBLIC_RESEARCH_RUNTIME_ERROR"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.details = dict(details or {})


class PublicResearchConfigurationError(PublicResearchError):
    category = "CONFIGURATION"
    error_code = "PUBLIC_RESEARCH_CONFIGURATION_ERROR"


class PublicResearchPlanError(PublicResearchError):
    category = "PLAN_CONTRACT"
    error_code = "PUBLIC_RESEARCH_PLAN_CONTRACT_ERROR"


class PublicResearchRetrievalError(PublicResearchError):
    category = "RETRIEVAL"
    error_code = "PUBLIC_RESEARCH_RETRIEVAL_ERROR"


class PublicResearchSecurityError(PublicResearchError):
    category = "SECURITY"
    error_code = "PUBLIC_RESEARCH_SECURITY_ERROR"


class PublicResearchIntegrityError(PublicResearchError):
    category = "INTEGRITY"
    error_code = "PUBLIC_RESEARCH_INTEGRITY_ERROR"


def _classified_cause(exc: BaseException) -> BaseException:
    """Return the nearest machine-classified cause, not merely the deepest one.

    Archive errors are often raised ``from`` low-level JSON, HTTP or parsing
    exceptions.  Walking blindly to the deepest exception discards the typed
    CONFIGURATION / PLAN_CONTRACT / SECURITY category and recreates the same
    WAITING_CONFIGURATION misclassification through string matching.
    """
    current: BaseException = exc
    visited: set[int] = set()
    generic_classified: BaseException | None = None
    deepest: BaseException = exc
    while id(current) not in visited:
        visited.add(id(current))
        deepest = current
        category = str(getattr(current, "category", "") or "").upper()
        if category and category != "RUNTIME":
            return current
        if category and generic_classified is None:
            generic_classified = current
        next_exc = current.__cause__ or current.__context__
        if next_exc is None:
            break
        current = next_exc
    return generic_classified or deepest


def _facade_error(exc: BaseException) -> PublicResearchError:
    root = _classified_cause(exc)
    category = str(getattr(root, "category", "") or "").upper()
    details = dict(getattr(root, "details", {}) or {})
    message = str(root or exc)
    error_types = {
        "CONFIGURATION": PublicResearchConfigurationError,
        "PLAN_CONTRACT": PublicResearchPlanError,
        "RETRIEVAL": PublicResearchRetrievalError,
        "SECURITY": PublicResearchSecurityError,
        "INTEGRITY": PublicResearchIntegrityError,
    }
    error_type = error_types.get(category, PublicResearchError)
    return error_type(message, details=details)


class PublicResearchService:
    """Compatibility facade over the auditable public-research skill."""

    def __init__(self, settings, skill_executor: SkillExecutor | None = None):
        self.settings = settings
        self.skill_executor = skill_executor

    def simulated_search(self, plan: dict[str, Any]) -> dict[str, Any]:
        """Build an auditable deterministic source set for SIMULATED runs.

        The simulator must not invent provenance inside the model response.  Sources
        are materialized here, before the synthesis prompt is built, so every
        ``source_ref`` returned by the simulated model is already visible in the
        trusted input envelope and can be rebound by the global provenance layer.
        """
        plan_text = str(plan or {})
        transport_markers = ("车辆路径", "运输", "多式联运", "vehicle routing", "freight")
        catalog = (
            TRANSPORT_REF_CATALOG
            if any(marker.lower() in plan_text.lower() for marker in transport_markers)
            else LOGISTICS_REF_CATALOG
        )
        sources: list[dict[str, Any]] = []
        passages: list[dict[str, Any]] = []
        source_catalog: list[dict[str, Any]] = []
        for fallback, item in enumerate(catalog, 1):
            try:
                number = int(item.get("reference_number") or item.get("id") or fallback)
            except (TypeError, ValueError):
                number = fallback
            source_id = str(item.get("source_id") or f"public-src-{number:03d}")
            title = str(item.get("title") or "公开来源")
            url = str(item.get("url") or "https://example.invalid")
            publisher = str(item.get("publisher") or item.get("venue") or "公开发布机构")
            year = str(item.get("published_at") or item.get("year") or "")
            summary = str(
                item.get("content_text")
                or item.get("excerpt")
                or item.get("note")
                or title
            )
            quoted = f"{title} | {publisher} | {year} | {url}"
            snapshot_sha256 = sha256_text(title + url)
            source_ref = {
                "source_id": source_id,
                "source_type": "PUBLIC_SOURCE",
                "document_version_id": None,
                "section_id": None,
                "span_start": None,
                "span_end": None,
                "quoted_text": quoted,
                "source_hash": snapshot_sha256,
                "authority_rank": int(item.get("authority_rank") or (70 if publisher == "arXiv" else 80)),
                "security_level": "PUBLIC",
            }
            sources.append(source_ref)
            passages.append({
                "passage_id": f"pass-{number:03d}",
                "source_ref": dict(source_ref),
                "text": summary,
                "relevance": str(item.get("category") or "公开研究证据"),
            })
            source_catalog.append({
                "source_id": source_id,
                "title": title,
                "url": url,
                "publisher": publisher,
                "published_at": year or None,
                "excerpt": f"{quoted}\n{summary}",
                "snapshot_sha256": snapshot_sha256,
                "security_level": "PUBLIC",
            })
        return {
            "sources": sources,
            "passages": passages,
            "source_catalog": source_catalog,
            "queries": self._queries(plan),
            "mode": "SIMULATED_ARCHIVE",
            "coverage": {
                "dimensions": {
                    "recent_work": {"status": "PASS"},
                    "comparable_baselines": {"status": "PASS"},
                    "limitation_mechanisms": {"status": "PASS"},
                }
            },
            "issues": [],
        }

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
            raise PublicResearchConfigurationError("Public research skill executor is not configured")
        try:
            result = self.skill_executor.execute(
                "public_research.archive",
                {
                    "provider": self.settings.public_search_provider,
                    "base_url": self.settings.public_search_base_url,
                    "record_file": self.settings.public_research_record_file,
                    "connector_file": self.settings.public_research_connector_file,
                    "max_results": self.settings.public_search_max_results,
                    # LIVE capability runs enforce the complete C1 plan contract. Replay,
                    # mock and simulated orchestration remain backward compatible.
                    "require_structured_plan": str(self.settings.runtime_mode).upper() == "LIVE",
                    "plan": plan,
                },
                project_id=project_id,
                workflow_id=workflow_id,
                security_level=security_level,
            )
        except SkillExecutionError as exc:
            mapped = _facade_error(exc)
            raise mapped from exc
        output = result.output
        verification = output.get("archive_verification") or verify_research_archive(output.get("archive_manifest", ""))
        if verification.get("status") != "PASS":
            raise PublicResearchIntegrityError("Public research archive failed hash verification", details={"verification": verification})
        return output

    def validate_synthesis(self, synthesis: dict[str, Any], research_output: dict[str, Any]) -> dict[str, Any]:
        """Bind every PUBLIC_CLAIM to an archived source before import review.

        The model output is not rewritten. This method only creates a deterministic
        validation report and blocks unknown, hash-mismatched, unsupported, or
        innovation-like claims without recent-work/baseline/limitation coverage.
        """
        report = validate_public_claims(synthesis, research_output)
        archive_root = research_output.get("archive_root")
        if archive_root and report.get("validation_mode") != "ORCHESTRATION_ONLY":
            report_dir = Path(str(archive_root)) / "claim_bindings"
            report_dir.mkdir(parents=True, exist_ok=True)
            report_path = report_dir / f"claim-binding-{sha256_json(synthesis)[:16]}.json"
            write_json(report_path, report)
            report["report_path"] = str(report_path)
        return report

    @staticmethod
    def _queries(plan: dict[str, Any]) -> list[str]:
        result = []
        for item in plan.get("queries", []):
            if isinstance(item, str):
                value = item
            elif isinstance(item, dict):
                value = item.get("query") or item.get("query_text") or item.get("text") or ""
            else:
                value = ""
            value = str(value).strip()
            if value:
                result.append(value)
        return result
