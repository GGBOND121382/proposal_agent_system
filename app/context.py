from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

from .context_base import ContextBuilder as BaseContextBuilder


_BUILD_PROMPT_ID: ContextVar[str | None] = ContextVar("proposal_build_prompt_id", default=None)
_BUILD_WORKFLOW_ID: ContextVar[str | None] = ContextVar("proposal_build_workflow_id", default=None)
_BUILD_SECTION_ID: ContextVar[str | None] = ContextVar("proposal_build_section_id", default=None)
_BUILD_AUTHORING_CHILD_IDS: ContextVar[tuple[str, ...]] = ContextVar(
    "proposal_build_authoring_child_ids", default=()
)


class ContextBuilder(BaseContextBuilder):
    """Context builder with workflow-and-section isolation for concurrent writing."""

    _SCOPED_SECTION_PRODUCERS = {
        "P-WRITE-BLUEPRINT",
        "P-WRITE-CONTENT",
        "P-EXPRESSION-POLISH",
    }

    @contextmanager
    def _workflow_build_scope(
        self,
        prompt_id: str,
        workflow_id: str | None,
        workflow_state: dict[str, Any] | None,
    ) -> Iterator[None]:
        """Bind per-build metadata to the current execution context.

        A single ContextBuilder instance is shared by concurrent section workers.
        Mutable instance attributes therefore leak workflow and section identity
        across tasks. ContextVars keep this metadata task-local while preserving
        synchronous helper access during one context build.
        """
        state = workflow_state or {}
        prompt_token = _BUILD_PROMPT_ID.set(prompt_id)
        workflow_token = _BUILD_WORKFLOW_ID.set(workflow_id)
        section_token = _BUILD_SECTION_ID.set(
            str(state.get("active_section_id") or "") or None
        )
        child_token = _BUILD_AUTHORING_CHILD_IDS.set(
            tuple(
                str(item)
                for item in state.get("authoring_child_workflow_ids", [])
                if item
            )
        )
        try:
            yield
        finally:
            _BUILD_AUTHORING_CHILD_IDS.reset(child_token)
            _BUILD_SECTION_ID.reset(section_token)
            _BUILD_WORKFLOW_ID.reset(workflow_token)
            _BUILD_PROMPT_ID.reset(prompt_token)

    def build(
        self,
        prompt_id: str,
        project_id: str,
        *,
        workflow_id: str | None = None,
        workflow_state: dict[str, Any] | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._workflow_build_scope(prompt_id, workflow_id, workflow_state):
            return super().build(
                prompt_id,
                project_id,
                workflow_id=workflow_id,
                workflow_state=workflow_state,
                overrides=overrides,
            )

    def _content_candidates(
        self,
        project_id: str,
        workflow_id: str | None = None,
        *,
        section_results: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        if section_results:
            return super()._content_candidates(
                project_id,
                workflow_id,
                section_results=section_results,
            )
        child_ids = list(_BUILD_AUTHORING_CHILD_IDS.get())
        if _BUILD_PROMPT_ID.get() != "P-INTEGRATION-CRITIC" or not child_ids:
            return super()._content_candidates(
                project_id,
                workflow_id,
                section_results=section_results,
            )
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
            return super()._result(
                project_id,
                prompt_id,
                key,
                workflow_id=workflow_id,
                exact_workflow=bool(workflow_id),
            )
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

    def _result(
        self,
        project_id: str,
        prompt_id: str,
        key: str | None = None,
        *,
        workflow_id: str | None = None,
        exact_workflow: bool = False,
    ) -> Any:
        workflow_id = workflow_id or _BUILD_WORKFLOW_ID.get()
        section_id = _BUILD_SECTION_ID.get()
        if (
            not exact_workflow
            and prompt_id in self._SCOPED_SECTION_PRODUCERS
            and workflow_id
            and section_id
        ):
            return self._section_prompt_result(
                project_id,
                prompt_id,
                workflow_id=workflow_id,
                section_id=section_id,
                key=key,
            )
        return super()._result(
            project_id,
            prompt_id,
            key,
            workflow_id=workflow_id,
            exact_workflow=exact_workflow,
        )
