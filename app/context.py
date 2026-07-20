from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from .context_base import ContextBuilder as BaseContextBuilder


_ACTIVE_PROMPT_ID: ContextVar[str | None] = ContextVar("proposal_active_prompt_id", default=None)
_ACTIVE_WORKFLOW_ID: ContextVar[str | None] = ContextVar("proposal_active_workflow_id", default=None)
_ACTIVE_SECTION_ID: ContextVar[str | None] = ContextVar("proposal_active_section_id", default=None)
_ACTIVE_SOURCE_WORKFLOW_ID: ContextVar[str | None] = ContextVar("proposal_source_workflow_id", default=None)
_ACTIVE_AUTHORING_CHILD_IDS: ContextVar[tuple[str, ...]] = ContextVar(
    "proposal_active_authoring_child_ids", default=()
)


class ContextBuilder(BaseContextBuilder):
    """Context builder with workflow-and-section isolation for concurrent writing."""

    _SCOPED_SECTION_PRODUCERS = {
        "P-WRITE-BLUEPRINT",
        "P-WRITE-CONTENT",
        "P-EXPRESSION-POLISH",
    }

    @contextmanager
    def workflow_scope(
        self,
        prompt_id: str,
        workflow_id: str | None,
        workflow_state: dict[str, Any] | None,
    ):
        """Bind prompt/workflow/section identity for one context build.

        The LIVE builder overrides :meth:`build`, so the scope must be reusable
        rather than being hidden inside the replay-oriented implementation.
        ContextVars keep concurrent authoring groups isolated per asyncio task.
        """
        state = workflow_state or {}
        prompt_token = _ACTIVE_PROMPT_ID.set(prompt_id)
        workflow_token = _ACTIVE_WORKFLOW_ID.set(workflow_id)
        section_token = _ACTIVE_SECTION_ID.set(
            str(state.get("active_section_id") or "") or None
        )
        source_token = _ACTIVE_SOURCE_WORKFLOW_ID.set(
            str(state.get("source_workflow_id") or "") or None
        )
        child_token = _ACTIVE_AUTHORING_CHILD_IDS.set(
            tuple(str(item) for item in state.get("authoring_child_workflow_ids", []) if item)
        )
        try:
            yield
        finally:
            _ACTIVE_AUTHORING_CHILD_IDS.reset(child_token)
            _ACTIVE_SOURCE_WORKFLOW_ID.reset(source_token)
            _ACTIVE_SECTION_ID.reset(section_token)
            _ACTIVE_WORKFLOW_ID.reset(workflow_token)
            _ACTIVE_PROMPT_ID.reset(prompt_token)

    def build(
        self,
        prompt_id: str,
        project_id: str,
        *,
        workflow_id: str | None = None,
        workflow_state: dict[str, Any] | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self.workflow_scope(prompt_id, workflow_id, workflow_state):
            return super().build(
                prompt_id,
                project_id,
                workflow_id=workflow_id,
                workflow_state=workflow_state,
                overrides=overrides,
            )

    def _frozen_candidates_for_workflow(
        self,
        project_id: str,
        workflow_id: str,
    ) -> list[dict[str, Any]]:
        row = self.db.fetchone(
            "SELECT state_json,status FROM workflows WHERE id=? AND project_id=? "
            "AND workflow_type='WF-4_PROPOSAL_AUTHORING'",
            (workflow_id, project_id),
        )
        if not row or row.get("status") != "COMPLETED":
            raise RuntimeError(f"Frozen WF-4 workflow is unavailable: {workflow_id}")
        state = json.loads(row.get("state_json") or "{}")
        reviews = [
            item for item in state.get("full_proposal_review_history") or []
            if isinstance(item, dict) and item.get("status") == "PASS"
        ]
        if reviews:
            manifest = reviews[-1].get("section_manifest") or []
        else:
            integration_pass = any(
                isinstance(item, dict)
                and item.get("prompt_id") == "P-INTEGRATION-CRITIC"
                and item.get("status") == "PASS"
                for item in (state.get("step_results") or {}).values()
            )
            if not integration_pass:
                raise RuntimeError(f"Frozen WF-4 workflow has no PASS integration review: {workflow_id}")
            manifest = []
            for section in state.get("section_results") or []:
                if not isinstance(section, dict) or section.get("status") != "COMPLETED":
                    continue
                runs = [item for item in section.get("runs") or [] if isinstance(item, dict)]
                polish = next(
                    (item for item in reversed(runs) if item.get("prompt_id") == "P-EXPRESSION-POLISH" and item.get("status") == "PASS"),
                    None,
                )
                critic = next(
                    (item for item in reversed(runs) if item.get("prompt_id") == "P-EXPRESSION-CRITIC" and item.get("status") == "PASS"),
                    None,
                )
                if not polish or not critic:
                    raise RuntimeError(f"Frozen legacy WF-4 section is incomplete: {section.get('section_id')}")
                polish_run_id = str(polish.get("run_id") or "")
                polish_row = self.db.fetchone(
                    "SELECT output_json,status FROM prompt_runs WHERE project_id=? AND workflow_id=? AND id=?",
                    (project_id, workflow_id, polish_run_id),
                )
                if not polish_row or polish_row.get("status") != "PASS" or not polish_row.get("output_json"):
                    raise RuntimeError(f"Frozen legacy section run is unavailable: {polish_run_id}")
                output = json.loads(polish_row.get("output_json") or "{}")
                candidate_id = str((output.get("result") or {}).get("candidate_id") or "")
                manifest.append({
                    "section_id": str(section.get("section_id") or ""),
                    "candidate_id": candidate_id,
                    "polish_run_id": polish_run_id,
                    "expression_critic_run_id": str(critic.get("run_id") or ""),
                })
        if not manifest:
            raise RuntimeError(f"Frozen WF-4 workflow has no section manifest: {workflow_id}")
        frozen: list[dict[str, Any]] = []
        for item in manifest:
            run_id = str(item.get("polish_run_id") or "")
            run = self.db.fetchone(
                "SELECT id,prompt_id,input_json,output_json,status FROM prompt_runs "
                "WHERE project_id=? AND id=? AND prompt_id='P-EXPRESSION-POLISH'",
                (project_id, run_id),
            )
            if not run or run.get("status") != "PASS" or not run.get("output_json"):
                raise RuntimeError(f"Frozen section run is unavailable: {run_id}")
            input_data = json.loads(run.get("input_json") or "{}")
            output_data = json.loads(run.get("output_json") or "{}")
            section = (input_data.get("payload") or {}).get("source_section") or {}
            candidate = output_data.get("result") or {}
            if str(section.get("section_id") or "") != str(item.get("section_id") or ""):
                raise RuntimeError(f"Frozen section mismatch: {run_id}")
            if str(candidate.get("candidate_id") or "") != str(item.get("candidate_id") or ""):
                raise RuntimeError(f"Frozen candidate mismatch: {run_id}")
            frozen.append({
                "run_id": run_id,
                "prompt_id": "P-EXPRESSION-POLISH",
                "section": section,
                "candidate": candidate,
            })
        return frozen

    def _content_candidates(
        self,
        project_id: str,
        workflow_id: str | None = None,
    ) -> list[dict[str, Any]]:
        source_workflow_id = _ACTIVE_SOURCE_WORKFLOW_ID.get()
        if source_workflow_id:
            return self._frozen_candidates_for_workflow(project_id, source_workflow_id)
        child_ids = list(_ACTIVE_AUTHORING_CHILD_IDS.get())
        if _ACTIVE_PROMPT_ID.get() == "P-INTEGRATION-CRITIC" and not child_ids and workflow_id:
            # During the parent transition into the integration stage the engine may
            # call the context builder with a state snapshot taken just before the
            # child IDs were copied into the in-memory object. Recover the persisted
            # IDs instead of leaving the strict schema scaffold in candidate_sections.
            row = self.db.fetchone("SELECT state_json FROM workflows WHERE id=?", (workflow_id,))
            if row:
                try:
                    persisted = json.loads(row.get("state_json") or "{}")
                    child_ids = [str(item) for item in persisted.get("authoring_child_workflow_ids", []) if item]
                    if not child_ids:
                        child_ids = [
                            str(item.get("workflow_id"))
                            for item in (persisted.get("full_proposal_children") or {}).values()
                            if isinstance(item, dict) and item.get("workflow_id")
                        ]
                except (TypeError, ValueError, json.JSONDecodeError):
                    child_ids = []
        if _ACTIVE_PROMPT_ID.get() != "P-INTEGRATION-CRITIC":
            return super()._content_candidates(project_id, workflow_id)
        # Some integration invocations are constructed before the parent workflow's
        # in-memory state receives authoring_child_workflow_ids. Recover from the
        # latest persisted top-level WF-4; if that still yields no IDs, use the
        # latest successful expression candidate for each document section.
        if not child_ids:
            rows = self.db.fetchall(
                "SELECT state_json FROM workflows WHERE project_id=? AND workflow_type='WF-4_PROPOSAL_AUTHORING' ORDER BY created_at DESC",
                (project_id,),
            )
            for wf_row in rows:
                try:
                    persisted = json.loads(wf_row.get("state_json") or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if persisted.get("parent_workflow_id"):
                    continue
                child_ids = [str(item) for item in persisted.get("authoring_child_workflow_ids", []) if item]
                if not child_ids:
                    child_ids = [
                        str(item.get("workflow_id"))
                        for item in (persisted.get("full_proposal_children") or {}).values()
                        if isinstance(item, dict) and item.get("workflow_id")
                    ]
                if child_ids:
                    break
        sql = "SELECT id,prompt_id,workflow_id,input_json,output_json,created_at FROM prompt_runs WHERE project_id=? AND prompt_id IN ('P-WRITE-CONTENT','P-EXPRESSION-POLISH') AND status='PASS'"
        params: tuple[Any, ...] = (project_id,)
        if child_ids:
            sql += " AND workflow_id IN (" + ",".join("?" for _ in child_ids) + ")"
            params = (project_id, *child_ids)
        sql += " ORDER BY created_at,id"
        latest_by_section: dict[str, dict[str, Any]] = {}
        for row in self.db.fetchall(sql, params):
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
        workflow_id = _ACTIVE_WORKFLOW_ID.get()
        section_id = _ACTIVE_SECTION_ID.get()
        if prompt_id in self._SCOPED_SECTION_PRODUCERS and workflow_id and section_id:
            return self._section_prompt_result(
                project_id,
                prompt_id,
                workflow_id=workflow_id,
                section_id=section_id,
                key=key,
            )
        return super()._result(project_id, prompt_id, key)
