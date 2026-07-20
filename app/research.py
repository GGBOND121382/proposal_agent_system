from __future__ import annotations

from pathlib import Path
from typing import Any

from .skills.executor import SkillExecutionError, SkillExecutor
from .skills.research_audit import verify_research_archive
from .skills.research_claims import validate_public_claims
from .public_search_bridge import FilePublicSearchBridge, PublicSearchBridgeError
from .util import sha256_json, write_json


class PublicResearchError(RuntimeError):
    pass


class PublicResearchService:
    """Compatibility facade over the auditable public-research skill."""

    def __init__(self, settings, skill_executor: SkillExecutor | None = None):
        self.settings = settings
        self.skill_executor = skill_executor

    def simulated_search(self, plan: dict[str, Any]) -> dict[str, Any]:
        # Kept for old REPLAY/MOCK tests. New complex runs use recorded or live archives.
        return {"sources": [], "passages": [], "queries": self._queries(plan), "mode": "SIMULATED_EMPTY"}

    async def search(
        self,
        plan: dict[str, Any],
        *,
        project_id: str,
        workflow_id: str | None,
        security_level: str = "PUBLIC",
    ) -> dict[str, Any]:
        provider = str(self.settings.public_search_provider or "disabled").lower()
        if provider == "disabled":
            raise PublicResearchError("PUBLIC_SEARCH_PROVIDER is disabled")
        connector_file = self.settings.public_research_connector_file
        if provider == "bridge":
            bridge_dir = getattr(self.settings, "public_search_bridge_dir", None)
            if not bridge_dir:
                raise PublicResearchError("PUBLIC_SEARCH_PROVIDER=bridge requires PUBLIC_SEARCH_BRIDGE_DIR")
            try:
                bridge = FilePublicSearchBridge(
                    Path(bridge_dir),
                    timeout_seconds=int(getattr(self.settings, "request_timeout_seconds", 240)),
                )
                connector_file = str(
                    await bridge.request(plan, int(self.settings.public_search_max_results))
                )
            except PublicSearchBridgeError as exc:
                raise PublicResearchError(str(exc)) from exc
            provider = "connector"
        if self.skill_executor is None:
            raise PublicResearchError("Public research skill executor is not configured")
        try:
            result = self.skill_executor.execute(
                "public_research.archive",
                {
                    "provider": provider,
                    "base_url": self.settings.public_search_base_url,
                    "record_file": self.settings.public_research_record_file,
                    "connector_file": connector_file,
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
            raise PublicResearchError(str(exc)) from exc
        output = result.output
        verification = output.get("archive_verification") or verify_research_archive(output.get("archive_manifest", ""))
        if verification.get("status") != "PASS":
            raise PublicResearchError("Public research archive failed hash verification")
        return output

    def provider_ready(self) -> bool:
        provider = str(self.settings.public_search_provider or "disabled").lower()
        if provider == "bridge":
            return bool(getattr(self.settings, "public_search_bridge_dir", None))
        if provider == "connector":
            value = str(getattr(self.settings, "public_research_connector_file", "") or "")
            return bool(value and Path(value).exists())
        if provider == "recorded":
            value = str(getattr(self.settings, "public_research_record_file", "") or "")
            return bool(value and Path(value).exists())
        if provider == "searxng":
            return bool(getattr(self.settings, "public_search_base_url", ""))
        if provider == "crossref":
            return True
        return False

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
