from __future__ import annotations

import json
from typing import Any

from .context_base import ContextBuilder as BaseContextBuilder


class ContextBuilder(BaseContextBuilder):
    """Context builder with workflow-and-section isolation for concurrent writing."""

    _SCOPED_SECTION_PRODUCERS = {
        "P-WRITE-BLUEPRINT",
        "P-WRITE-CONTENT",
        "P-EXPRESSION-POLISH",
    }

    def build(
        self,
        prompt_id: str,
        project_id: str,
        *,
        workflow_id: str | None = None,
        workflow_state: dict[str, Any] | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        previous = (
            getattr(self, "_active_prompt_id", None),
            getattr(self, "_active_workflow_id", None),
            getattr(self, "_active_section_id", None),
            getattr(self, "_active_authoring_child_ids", None),
        )
        state = workflow_state or {}
        self._active_prompt_id = prompt_id
        self._active_workflow_id = workflow_id
        self._active_section_id = str(state.get("active_section_id") or "") or None
        self._active_authoring_child_ids = [
            str(item) for item in state.get("authoring_child_workflow_ids", []) if item
        ]
        try:
            return super().build(
                prompt_id,
                project_id,
                workflow_id=workflow_id,
                workflow_state=workflow_state,
                overrides=overrides,
            )
        finally:
            (
                self._active_prompt_id,
                self._active_workflow_id,
                self._active_section_id,
                self._active_authoring_child_ids,
            ) = previous

    def _content_candidates(
        self,
        project_id: str,
        workflow_id: str | None = None,
    ) -> list[dict[str, Any]]:
        child_ids = list(getattr(self, "_active_authoring_child_ids", None) or [])
        if getattr(self, "_active_prompt_id", None) != "P-INTEGRATION-CRITIC" or not child_ids:
            return super()._content_candidates(project_id, workflow_id)
        sql = "SELECT id,prompt_id,input_json,output_json,created_at FROM prompt_runs WHERE project_id=? AND prompt_id IN ('P-WRITE-CONTENT','P-EXPRESSION-POLISH') AND status='PASS'"
        sql += " AND workflow_id IN (" + ",".join("?" for _ in child_ids) + ")"
        sql += " ORDER BY created_at,id"
        latest_by_section: dict[str, dict[str, Any]] = {}
        for row in self.db.fetchall(sql, (project_id, *child_ids)):
            if not row.get("output_json"):
                continue
            input_data = json.loads(row["input_json"])
            output_data = json.loads(row["output_json"])
            section = (input_data.get("payload") or {}).get("source_section") or {}
            candidate = output_data.get("result") or {}
            section_id = section.get("section_id")
            if not section_id or not candidate.get("candidate_id"):
                continue
            latest_by_section[section_id] = {
                "run_id": row["id"],
                "prompt_id": row.get("prompt_id"),
                "section": section,
                "candidate": candidate,
            }
        return list(latest_by_section.values())

    def _section_prompt_result(
        self,
        project_id: str,
        prompt_id: str,
        *,
        workflow_id: str | None,
        section_id: str | None,
        key: str | None = None,
    ) -> Any:
        if not workflow_id or not section_id:
            return super()._result(project_id, prompt_id, key)
        rows = self.db.fetchall(
            """SELECT input_json,output_json FROM prompt_runs
               WHERE project_id=? AND workflow_id=? AND prompt_id=? AND status='PASS'
               ORDER BY created_at DESC,id DESC""",
            (project_id, workflow_id, prompt_id),
        )
        for row in rows:
            if not row.get("output_json"):
                continue
            input_data = json.loads(row["input_json"])
            source = (input_data.get("payload") or {}).get("source_section") or {}
            if str(source.get("section_id") or "") != str(section_id):
                continue
            output = json.loads(row["output_json"])
            result = output.get("result")
            return result.get(key) if key and isinstance(result, dict) else result
        return None

    def _result(self, project_id: str, prompt_id: str, key: str | None = None) -> Any:
        workflow_id = getattr(self, "_active_workflow_id", None)
        section_id = getattr(self, "_active_section_id", None)
        if prompt_id in self._SCOPED_SECTION_PRODUCERS and workflow_id and section_id:
            return self._section_prompt_result(
                project_id,
                prompt_id,
                workflow_id=workflow_id,
                section_id=section_id,
                key=key,
            )
        return super()._result(project_id, prompt_id, key)
