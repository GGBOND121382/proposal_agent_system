from __future__ import annotations

from typing import Any

from .skills.executor import SkillExecutionError, SkillExecutor


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
        return result.output

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
