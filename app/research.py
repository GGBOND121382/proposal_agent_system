from __future__ import annotations

from pathlib import Path
from typing import Any

from .skills.executor import SkillExecutionError, SkillExecutor
from .skills.public_research import PublicResearchArchiveSkill
from .util import write_json


class PublicResearchError(RuntimeError):
    pass


class PublicResearchService:
    """Compatibility facade over the auditable public-research skill."""

    def __init__(self, settings, skill_executor: SkillExecutor | None = None):
        self.settings = settings
        self.skill_executor = skill_executor

    def simulated_search(self, plan: dict[str, Any]) -> dict[str, Any]:
        # Kept for old REPLAY/MOCK tests. New capability tests must use archived sources.
        return {
            "sources": [],
            "passages": [],
            "queries": self._queries(plan),
            "mode": "SIMULATED_EMPTY",
            "blocking_issues": [],
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
            raise PublicResearchError("PUBLIC_SEARCH_PROVIDER is disabled")
        if self.skill_executor is None:
            raise PublicResearchError("Public research skill executor is not configured")
        try:
            result = self.skill_executor.execute(
                "public_research.archive",
                {
                    "provider": self.settings.public_search_provider,
                    "base_url": self.settings.public_search_base_url,
                    "record_file": self.settings.public_research_record_file,
                    "connector_file": self.settings.public_research_connector_file,
                    "max_results": self.settings.public_search_max_results,
                    "plan": plan,
                },
                project_id=project_id,
                workflow_id=workflow_id,
                security_level=security_level,
            )
        except SkillExecutionError as exc:
            raise PublicResearchError(str(exc)) from exc
        if result.status != "PASS":
            issues = result.output.get("blocking_issues") or result.output.get("issues") or []
            codes = [str(item.get("code")) for item in issues if isinstance(item, dict)]
            suffix = "、".join(codes[:8]) if codes else result.status
            raise PublicResearchError(
                "Public research archive did not pass deterministic evidence validation: " + suffix
            )
        return result.output

    def validate_synthesis(
        self,
        synthesis: dict[str, Any],
        research_output: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if not research_output:
            raise PublicResearchError("Public research synthesis has no archived research output")
        report = PublicResearchArchiveSkill.validate_claim_bindings(synthesis, research_output)
        archive_root = str(research_output.get("archive_root") or "").strip()
        if archive_root:
            report_path = Path(archive_root) / "claim_bindings.json"
            write_json(report_path, report)
            research_output["claim_binding_report"] = str(report_path)
        research_output["claim_binding_validation"] = report
        if report["status"] != "PASS":
            codes = [str(item.get("code")) for item in report.get("findings", [])]
            raise PublicResearchError(
                "Public research synthesis contains unbound or tampered claims: "
                + "、".join(codes[:8])
            )
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
