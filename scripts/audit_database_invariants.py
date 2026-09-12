#!/usr/bin/env python3
"""Read-only consistency audit for Proposal Agent System SQLite databases.

The script never initializes the application ``Database`` wrapper because that
would execute DDL.  It opens the supplied file with SQLite ``mode=ro`` and
reports structural corruption, cross-object identity errors, historical state
migration risks, and backup portability warnings.
"""
from __future__ import annotations

import argparse
import collections
import json
import sqlite3
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.util import sha256_json  # noqa: E402
from app.workflow_defs import WORKFLOWS  # noqa: E402
from app.workflow_status import coerce_workflow_status, is_terminal  # noqa: E402


@dataclass(frozen=True)
class Finding:
    severity: str
    code: str
    object_type: str
    object_id: str
    message: str
    details: dict[str, Any]


class DatabaseInvariantAuditor:
    TERMINAL_TRANSIENT_KEYS = frozenset(
        {
            "provider_wait",
            "configuration_wait",
            "waiting_prerequisite",
            "runtime_recoverable",
            "runtime_failure_point",
            "runtime_blocked_at",
            "last_error",
            "active_section_id",
            "last_targeted_repair_failure",
            "pending_repair_rereviews",
        }
    )
    RUN_REFERENCE_KEYS = frozenset(
        {
            "run_id",
            "source_run_id",
            "target_run_id",
            "failed_run_id",
            "critic_run_id",
            "repair_run_id",
            "review_run_id",
        }
    )
    LEGACY_PROVIDER_MARKERS = (
        "transport failed",
        "connecterror",
        "connection reset",
        "connection refused",
        "timeout",
        "timed out",
        "stream completed without message content",
        "empty stream",
        "rate limit",
        "too many requests",
        "http 429",
        "http 500",
        "http 502",
        "http 503",
        "http 504",
    )
    LEGACY_CONTRACT_MARKERS = (
        "schema validation",
        "unresolved schema scaffold",
        "output schema",
        "output container",
        "contract",
        "not of type",
        "required property",
        "token limit",
        "output token",
        "json parse",
    )
    LEGACY_CONFIGURATION_MARKERS = (
        "configuration",
        "missing endpoint",
        "missing model",
        "api key",
        "base_url",
        "运行依赖未满足",
    )

    def __init__(self, database: Path):
        self.database = database.expanduser().resolve()
        self.findings: list[Finding] = []
        self.counts: dict[str, int] = {}

    def add(
        self,
        severity: str,
        code: str,
        object_type: str,
        object_id: str,
        message: str,
        **details: Any,
    ) -> None:
        self.findings.append(
            Finding(
                severity=severity,
                code=code,
                object_type=object_type,
                object_id=object_id,
                message=message,
                details=details,
            )
        )

    def _connect(self) -> sqlite3.Connection:
        if not self.database.is_file():
            raise FileNotFoundError(self.database)
        conn = sqlite3.connect(f"file:{self.database}?mode=ro&immutable=1", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @staticmethod
    def _json(
        row: sqlite3.Row | dict[str, Any],
        column: str,
        *,
        table: str,
        row_id: str,
        auditor: "DatabaseInvariantAuditor",
        nullable: bool = False,
    ) -> Any:
        raw = row[column]
        if raw is None and nullable:
            return None
        try:
            return json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            auditor.add(
                "ERROR",
                "INVALID_JSON",
                table,
                row_id,
                f"{table}.{column} is not valid JSON.",
                column=column,
                error=str(exc),
            )
            return None

    def _walk_run_refs(
        self,
        value: Any,
        *,
        workflow_id: str,
        project_id: str,
        runs: dict[str, dict[str, Any]],
        path: str = "state",
    ) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                current = f"{path}.{key}"
                if (
                    key in self.RUN_REFERENCE_KEYS
                    and isinstance(item, str)
                    and item.startswith("run-")
                ):
                    run = runs.get(item)
                    if run is None:
                        self.add(
                            "ERROR",
                            "STATE_DANGLING_RUN_REFERENCE",
                            "workflow",
                            workflow_id,
                            "Workflow state references a missing prompt run.",
                            path=current,
                            run_id=item,
                        )
                    elif run["project_id"] != project_id:
                        self.add(
                            "ERROR",
                            "STATE_CROSS_PROJECT_RUN_REFERENCE",
                            "workflow",
                            workflow_id,
                            "Workflow state references a run from another project.",
                            path=current,
                            run_id=item,
                            run_project_id=run["project_id"],
                            workflow_project_id=project_id,
                        )
                    elif run.get("workflow_id") != workflow_id:
                        self.add(
                            "ERROR",
                            "STATE_CROSS_WORKFLOW_RUN_REFERENCE",
                            "workflow",
                            workflow_id,
                            "Workflow state references a run from another workflow.",
                            path=current,
                            run_id=item,
                            run_workflow_id=run.get("workflow_id"),
                        )
                self._walk_run_refs(
                    item,
                    workflow_id=workflow_id,
                    project_id=project_id,
                    runs=runs,
                    path=current,
                )
        elif isinstance(value, list):
            for index, item in enumerate(value):
                self._walk_run_refs(
                    item,
                    workflow_id=workflow_id,
                    project_id=project_id,
                    runs=runs,
                    path=f"{path}[{index}]",
                )

    def audit(self) -> dict[str, Any]:
        with self._connect() as conn:
            integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
            if integrity != "ok":
                self.add(
                    "ERROR",
                    "SQLITE_INTEGRITY_FAILURE",
                    "database",
                    str(self.database),
                    "SQLite integrity_check did not return ok.",
                    result=integrity,
                )
            for row in conn.execute("PRAGMA foreign_key_check"):
                self.add(
                    "ERROR",
                    "SQLITE_FOREIGN_KEY_FAILURE",
                    "database",
                    str(self.database),
                    "SQLite foreign_key_check reported a violation.",
                    table=row[0],
                    rowid=row[1],
                    parent=row[2],
                    foreign_key_index=row[3],
                )

            tables = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            required_tables = {
                "projects",
                "documents",
                "artifacts",
                "prompt_runs",
                "skill_runs",
                "workflows",
                "gates",
                "audit_events",
            }
            for table in sorted(required_tables - tables):
                self.add(
                    "ERROR",
                    "MISSING_REQUIRED_TABLE",
                    "database",
                    str(self.database),
                    "Required application table is missing.",
                    table=table,
                )
            if required_tables - tables:
                return self.report()

            for table in sorted(required_tables):
                self.counts[table] = int(
                    conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                )

            self._audit_schema(conn)

            projects = {
                row["id"]: dict(row) for row in conn.execute("SELECT * FROM projects")
            }
            workflows = {
                row["id"]: dict(row) for row in conn.execute("SELECT * FROM workflows")
            }
            runs = {
                row["id"]: dict(row) for row in conn.execute("SELECT * FROM prompt_runs")
            }
            artifacts = {
                row["id"]: dict(row) for row in conn.execute("SELECT * FROM artifacts")
            }
            gates = {
                row["id"]: dict(row) for row in conn.execute("SELECT * FROM gates")
            }

            self._audit_references(projects, workflows, runs, artifacts, gates)
            self._audit_workflows(conn, workflows, runs)
            self._audit_runs(runs)
            self._audit_artifacts(artifacts)
            self._audit_gates(gates, workflows, runs)
            self._audit_documents(conn)
            self._audit_audit_events(conn, projects, workflows, runs)

        return self.report()

    def _audit_schema(self, conn: sqlite3.Connection) -> None:
        unconstrained: list[str] = []
        for table in ("artifacts", "prompt_runs", "skill_runs"):
            foreign_keys = [dict(row) for row in conn.execute(f"PRAGMA foreign_key_list({table})")]
            if not any(
                str(item.get("from") or "") == "workflow_id"
                and str(item.get("table") or "") == "workflows"
                and str(item.get("to") or "") == "id"
                for item in foreign_keys
            ):
                unconstrained.append(f"{table}.workflow_id")
        if unconstrained:
            self.add(
                "WARNING",
                "UNCONSTRAINED_WORKFLOW_REFERENCE_COLUMNS",
                "database",
                str(self.database),
                "Nullable workflow references are enforced only by application code, not by SQLite foreign keys.",
                columns=unconstrained,
                consequence=(
                    "A direct SQL write or defective migration can create an orphan or cross-project "
                    "Run/Artifact/Skill record without triggering PRAGMA foreign_key_check."
                ),
            )

        required_indexes = {
            "idx_artifacts_project_prompt_type_version",
            "idx_artifacts_workflow_prompt_type_status_version",
        }
        indexes = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        missing = sorted(required_indexes - indexes)
        if missing:
            self.add(
                "INFO",
                "MISSING_CURRENT_ARTIFACT_LOOKUP_INDEX",
                "database",
                str(self.database),
                "Database predates current artifact lookup indexes; current Database initialization will add them to a writable copy.",
                indexes=missing,
            )

    def _legacy_block_classification(
        self,
        *,
        workflow: dict[str, Any],
        state: dict[str, Any],
        runs: dict[str, dict[str, Any]],
    ) -> tuple[str, str, dict[str, Any]]:
        current_step = str(workflow["current_step"])
        current_result = (state.get("step_results") or {}).get(current_step)
        if isinstance(current_result, dict):
            run_id = str(current_result.get("run_id") or "")
            run = runs.get(run_id)
            output: dict[str, Any] = {}
            if run and run.get("output_json"):
                try:
                    decoded = json.loads(run["output_json"])
                except (TypeError, json.JSONDecodeError):
                    decoded = {}
                if isinstance(decoded, dict):
                    output = decoded
            blocking_questions = [
                item
                for item in output.get("user_questions") or []
                if isinstance(item, dict) and bool(item.get("blocking"))
            ]
            if current_result.get("status") in {"BLOCK", "NEED_USER_INPUT"} and blocking_questions:
                return (
                    "LEGACY_BLOCKED_HUMAN_INPUT_EVIDENCE",
                    "WAITING_GATE",
                    {
                        "run_id": run_id,
                        "prompt_id": current_result.get("prompt_id"),
                        "blocking_question_count": len(blocking_questions),
                    },
                )
            if current_result.get("status") == "REVISE":
                return (
                    "LEGACY_BLOCKED_CONTENT_EVIDENCE",
                    "BLOCKED_CONTENT",
                    {
                        "run_id": run_id,
                        "prompt_id": current_result.get("prompt_id"),
                        "result_status": "REVISE",
                    },
                )

        error = str(state.get("last_error") or state.get("recovered_from") or "")
        lowered = error.lower()
        if "确定性质量校验" in error or any(
            str(item).startswith("QG_") for item in state.get("quality_blocker_ids") or []
        ):
            return (
                "LEGACY_BLOCKED_CONTENT_EVIDENCE",
                "BLOCKED_CONTENT",
                {"last_error": error},
            )
        if any(marker in lowered for marker in self.LEGACY_CONFIGURATION_MARKERS):
            return (
                "LEGACY_BLOCKED_CONFIGURATION_EVIDENCE",
                "WAITING_CONFIGURATION",
                {"last_error": error},
            )
        if any(marker in lowered for marker in self.LEGACY_PROVIDER_MARKERS):
            return (
                "LEGACY_BLOCKED_PROVIDER_EVIDENCE",
                "WAITING_PROVIDER",
                {"last_error": error},
            )
        if any(marker in lowered for marker in self.LEGACY_CONTRACT_MARKERS):
            return (
                "LEGACY_BLOCKED_CONTRACT_EVIDENCE",
                "BLOCKED_CONTRACT",
                {"last_error": error},
            )
        return (
            "LEGACY_BLOCKED_TECHNICAL_EVIDENCE",
            "BLOCKED_TECHNICAL",
            {"last_error": error or None},
        )

    def _audit_references(
        self,
        projects: dict[str, dict[str, Any]],
        workflows: dict[str, dict[str, Any]],
        runs: dict[str, dict[str, Any]],
        artifacts: dict[str, dict[str, Any]],
        gates: dict[str, dict[str, Any]],
    ) -> None:
        for object_type, rows in (
            ("workflow", workflows),
            ("prompt_run", runs),
            ("artifact", artifacts),
            ("gate", gates),
        ):
            for object_id, row in rows.items():
                if row["project_id"] not in projects:
                    self.add(
                        "ERROR",
                        "DANGLING_PROJECT_REFERENCE",
                        object_type,
                        object_id,
                        "Object references a missing project.",
                        project_id=row["project_id"],
                    )

        for object_type, rows in (("prompt_run", runs), ("artifact", artifacts)):
            for object_id, row in rows.items():
                workflow_id = row.get("workflow_id")
                if not workflow_id:
                    continue
                workflow = workflows.get(workflow_id)
                if workflow is None:
                    self.add(
                        "ERROR",
                        "DANGLING_WORKFLOW_REFERENCE",
                        object_type,
                        object_id,
                        "Object references a missing workflow.",
                        workflow_id=workflow_id,
                    )
                elif workflow["project_id"] != row["project_id"]:
                    self.add(
                        "ERROR",
                        "CROSS_PROJECT_WORKFLOW_REFERENCE",
                        object_type,
                        object_id,
                        "Object and referenced workflow belong to different projects.",
                        workflow_id=workflow_id,
                        object_project_id=row["project_id"],
                        workflow_project_id=workflow["project_id"],
                    )

    def _audit_workflows(
        self,
        conn: sqlite3.Connection,
        workflows: dict[str, dict[str, Any]],
        runs: dict[str, dict[str, Any]],
    ) -> None:
        active_parents: dict[tuple[str, str], list[str]] = collections.defaultdict(list)
        for workflow_id, workflow in workflows.items():
            state = self._json(
                workflow,
                "state_json",
                table="workflows",
                row_id=workflow_id,
                auditor=self,
            )
            if not isinstance(state, dict):
                continue
            try:
                canonical_status = coerce_workflow_status(workflow["status"])
            except ValueError as exc:
                self.add(
                    "ERROR",
                    "UNKNOWN_WORKFLOW_STATUS",
                    "workflow",
                    workflow_id,
                    "Workflow status is not recognized by the current status ontology.",
                    status=workflow["status"],
                    error=str(exc),
                )
                continue

            workflow_type = workflow["workflow_type"]
            steps = WORKFLOWS.get(workflow_type)
            if steps is None:
                self.add(
                    "ERROR",
                    "UNKNOWN_WORKFLOW_TYPE",
                    "workflow",
                    workflow_id,
                    "Workflow type is not registered.",
                    workflow_type=workflow_type,
                )
                continue
            if state.get("workflow_type") != workflow_type:
                self.add(
                    "ERROR",
                    "WORKFLOW_TYPE_STATE_MISMATCH",
                    "workflow",
                    workflow_id,
                    "Workflow row and state_json disagree on workflow_type.",
                    row_workflow_type=workflow_type,
                    state_workflow_type=state.get("workflow_type"),
                )

            current_step = int(workflow["current_step"])
            if current_step < 0 or current_step > len(steps):
                self.add(
                    "ERROR",
                    "WORKFLOW_STEP_OUT_OF_RANGE",
                    "workflow",
                    workflow_id,
                    "Workflow current_step lies outside the registered workflow.",
                    current_step=current_step,
                    step_count=len(steps),
                )
            if canonical_status.value == "COMPLETED" and current_step != len(steps):
                self.add(
                    "ERROR",
                    "COMPLETED_WORKFLOW_WRONG_STEP",
                    "workflow",
                    workflow_id,
                    "Completed workflow is not positioned after its final step.",
                    current_step=current_step,
                    step_count=len(steps),
                )
            if not is_terminal(canonical_status) and current_step >= len(steps):
                self.add(
                    "ERROR",
                    "NONTERMINAL_WORKFLOW_AT_END",
                    "workflow",
                    workflow_id,
                    "Nonterminal workflow is positioned after its final step.",
                    current_step=current_step,
                    step_count=len(steps),
                    status=canonical_status.value,
                )

            if workflow["status"] == "BLOCKED":
                self.add(
                    "WARNING",
                    "LEGACY_GENERIC_BLOCKED",
                    "workflow",
                    workflow_id,
                    "Historical generic BLOCKED status requires evidence-based migration.",
                    current_step=current_step,
                    last_error=state.get("last_error"),
                )
                if not str(state.get("last_error") or "").strip():
                    self.add(
                        "WARNING",
                        "LEGACY_BLOCKED_WITHOUT_ERROR",
                        "workflow",
                        workflow_id,
                        "Historical BLOCKED workflow has no last_error; classification must use the persisted step result or Run output.",
                        current_step=current_step,
                    )
                evidence_code, suggested_status, evidence = self._legacy_block_classification(
                    workflow=workflow,
                    state=state,
                    runs=runs,
                )
                self.add(
                    "WARNING",
                    evidence_code,
                    "workflow",
                    workflow_id,
                    "Persisted evidence suggests a classified migration target; do not consume a generic technical retry before this classification is reconciled.",
                    current_step=current_step,
                    suggested_status=suggested_status,
                    evidence=evidence,
                )

            if canonical_status.value == "WAITING_CONFIGURATION" and not isinstance(
                state.get("configuration_wait"), dict
            ):
                self.add(
                    "ERROR",
                    "WAITING_CONFIGURATION_WITHOUT_CHECKPOINT",
                    "workflow",
                    workflow_id,
                    "WAITING_CONFIGURATION lacks configuration_wait evidence.",
                )

            transient = sorted(self.TERMINAL_TRANSIENT_KEYS.intersection(state))
            if is_terminal(canonical_status) and transient:
                self.add(
                    "WARNING",
                    "TERMINAL_WORKFLOW_HAS_TRANSIENT_STATE",
                    "workflow",
                    workflow_id,
                    "Terminal workflow retains active failure/wait checkpoint fields.",
                    fields=transient,
                )

            if not is_terminal(canonical_status) and not state.get("parent_workflow_id"):
                active_parents[(workflow["project_id"], workflow_type)].append(workflow_id)

            step_results = state.get("step_results")
            if not isinstance(step_results, dict):
                self.add(
                    "ERROR",
                    "STEP_RESULTS_NOT_OBJECT",
                    "workflow",
                    workflow_id,
                    "workflow_state.step_results must be an object keyed by step number.",
                    actual_type=type(step_results).__name__,
                )
            else:
                for key, result in step_results.items():
                    try:
                        step_index = int(key)
                    except (TypeError, ValueError):
                        self.add(
                            "ERROR",
                            "INVALID_STEP_RESULT_KEY",
                            "workflow",
                            workflow_id,
                            "step_results contains a non-integer key.",
                            key=key,
                        )
                        continue
                    if step_index < 0 or step_index >= len(steps):
                        self.add(
                            "ERROR",
                            "STEP_RESULT_OUT_OF_RANGE",
                            "workflow",
                            workflow_id,
                            "step_results key lies outside the workflow definition.",
                            key=key,
                            step_count=len(steps),
                        )
                    if not isinstance(result, dict):
                        self.add(
                            "ERROR",
                            "STEP_RESULT_NOT_OBJECT",
                            "workflow",
                            workflow_id,
                            "step_results value must be an object.",
                            key=key,
                            actual_type=type(result).__name__,
                        )
                        continue
                    run_id = str(result.get("run_id") or "")
                    step_definition = steps[step_index] if 0 <= step_index < len(steps) else {}
                    expects_prompt_run = bool(step_definition.get("prompt_id"))
                    if not run_id and not expects_prompt_run:
                        # Non-prompt workflow steps (for example WRITE_SECTIONS) may
                        # retain aggregate decision metadata in step_results without
                        # owning one parent-level prompt run.  Their child prompt runs
                        # are audited through the registered child workflows instead.
                        continue
                    run = runs.get(run_id)
                    if run is None:
                        self.add(
                            "ERROR",
                            "STEP_RESULT_DANGLING_RUN",
                            "workflow",
                            workflow_id,
                            "Step result references a missing prompt run.",
                            key=key,
                            run_id=run_id,
                        )
                        continue
                    if run["workflow_id"] != workflow_id or run["project_id"] != workflow["project_id"]:
                        self.add(
                            "ERROR",
                            "STEP_RESULT_WRONG_OWNER",
                            "workflow",
                            workflow_id,
                            "Step result run belongs to another workflow or project.",
                            key=key,
                            run_id=run_id,
                            run_workflow_id=run["workflow_id"],
                            run_project_id=run["project_id"],
                        )
                    if result.get("prompt_id") != run["prompt_id"]:
                        self.add(
                            "ERROR",
                            "STEP_RESULT_PROMPT_MISMATCH",
                            "workflow",
                            workflow_id,
                            "Step result prompt_id differs from its run.",
                            key=key,
                            run_id=run_id,
                            result_prompt_id=result.get("prompt_id"),
                            run_prompt_id=run["prompt_id"],
                        )
                    if result.get("status") != run["status"]:
                        self.add(
                            "ERROR",
                            "STEP_RESULT_STATUS_MISMATCH",
                            "workflow",
                            workflow_id,
                            "Step result status differs from its run.",
                            key=key,
                            run_id=run_id,
                            result_status=result.get("status"),
                            run_status=run["status"],
                        )

            self._walk_run_refs(
                state,
                workflow_id=workflow_id,
                project_id=workflow["project_id"],
                runs=runs,
            )

        for (project_id, workflow_type), workflow_ids in sorted(active_parents.items()):
            if len(workflow_ids) > 1:
                self.add(
                    "WARNING",
                    "MULTIPLE_ACTIVE_PARENT_WORKFLOWS",
                    "project",
                    project_id,
                    "Project has multiple nonterminal top-level workflows of the same type; current start() will remain blocked until legacy instances are resolved.",
                    workflow_type=workflow_type,
                    workflow_ids=workflow_ids,
                )

    def _audit_runs(self, runs: dict[str, dict[str, Any]]) -> None:
        for run_id, run in runs.items():
            envelope = self._json(
                run,
                "input_json",
                table="prompt_runs",
                row_id=run_id,
                auditor=self,
            )
            if isinstance(envelope, dict):
                computed = sha256_json(envelope)
                if computed != run["input_hash"]:
                    self.add(
                        "ERROR",
                        "PROMPT_RUN_INPUT_HASH_MISMATCH",
                        "prompt_run",
                        run_id,
                        "Prompt run input_hash does not match input_json.",
                        stored=run["input_hash"],
                        computed=computed,
                    )
                if envelope.get("prompt_id") != run["prompt_id"]:
                    self.add(
                        "ERROR",
                        "PROMPT_RUN_INPUT_PROMPT_MISMATCH",
                        "prompt_run",
                        run_id,
                        "Prompt run row and input envelope disagree on prompt_id.",
                        row_prompt_id=run["prompt_id"],
                        input_prompt_id=envelope.get("prompt_id"),
                    )

            output = self._json(
                run,
                "output_json",
                table="prompt_runs",
                row_id=run_id,
                auditor=self,
                nullable=True,
            )
            if output is not None and run["output_hash"]:
                computed = sha256_json(output)
                if computed != run["output_hash"]:
                    self.add(
                        "ERROR",
                        "PROMPT_RUN_OUTPUT_HASH_MISMATCH",
                        "prompt_run",
                        run_id,
                        "Prompt run output_hash does not match output_json.",
                        stored=run["output_hash"],
                        computed=computed,
                    )
            if isinstance(output, dict) and output.get("prompt_id") != run["prompt_id"]:
                self.add(
                    "ERROR",
                    "PROMPT_RUN_OUTPUT_PROMPT_MISMATCH",
                    "prompt_run",
                    run_id,
                    "Prompt run row and output object disagree on prompt_id.",
                    row_prompt_id=run["prompt_id"],
                    output_prompt_id=output.get("prompt_id"),
                )
            # ERROR rows may intentionally retain an immutable provider object
            # whose embedded business status is PASS/REVISE/NEED_USER_INPUT.
            if run["status"] != "ERROR" and isinstance(output, dict) and output.get("status") != run["status"]:
                self.add(
                    "ERROR",
                    "PROMPT_RUN_OUTPUT_STATUS_MISMATCH",
                    "prompt_run",
                    run_id,
                    "Successful prompt run row and output object disagree on status.",
                    row_status=run["status"],
                    output_status=output.get("status"),
                )
            if run["status"] == "ERROR" and not str(run.get("error") or "").strip():
                self.add(
                    "ERROR",
                    "ERROR_RUN_WITHOUT_ERROR",
                    "prompt_run",
                    run_id,
                    "ERROR prompt run does not contain an error message.",
                )
            if run["status"] != "ERROR" and output is None:
                self.add(
                    "ERROR",
                    "SUCCESSFUL_RUN_WITHOUT_OUTPUT",
                    "prompt_run",
                    run_id,
                    "Non-error prompt run does not contain output_json.",
                )

    def _audit_artifacts(self, artifacts: dict[str, dict[str, Any]]) -> None:
        prompt_versions: dict[tuple[str, str, str], list[int]] = collections.defaultdict(list)
        quality_versions: dict[str, list[int]] = collections.defaultdict(list)
        prompt_counts: dict[tuple[str, str, str], int] = collections.Counter()

        for artifact_id, artifact in artifacts.items():
            content = self._json(
                artifact,
                "content_json",
                table="artifacts",
                row_id=artifact_id,
                auditor=self,
            )
            if not isinstance(content, dict):
                continue
            artifact_type = artifact["artifact_type"]
            if artifact_type in {"PROMPT_OUTPUT", "PROMPT_TRACE"}:
                key = (artifact["project_id"], artifact["prompt_id"], artifact_type)
                prompt_versions[key].append(int(artifact["version"]))
                prompt_counts[key] += 1
                if content.get("prompt_id") != artifact["prompt_id"]:
                    self.add(
                        "ERROR",
                        "PROMPT_ARTIFACT_PROMPT_MISMATCH",
                        "artifact",
                        artifact_id,
                        "Prompt artifact row and content disagree on prompt_id.",
                        row_prompt_id=artifact["prompt_id"],
                        content_prompt_id=content.get("prompt_id"),
                    )
                if content.get("status") != artifact["status"]:
                    self.add(
                        "ERROR",
                        "PROMPT_ARTIFACT_STATUS_MISMATCH",
                        "artifact",
                        artifact_id,
                        "Prompt artifact row and content disagree on status.",
                        row_status=artifact["status"],
                        content_status=content.get("status"),
                    )
                if artifact_type == "PROMPT_TRACE" and content.get("version") != artifact["version"]:
                    self.add(
                        "ERROR",
                        "PROMPT_TRACE_VERSION_MISMATCH",
                        "artifact",
                        artifact_id,
                        "Prompt trace row and payload disagree on version.",
                        row_version=artifact["version"],
                        content_version=content.get("version"),
                    )

            if artifact_type == "QUALITY_FINDING":
                finding_id = str(content.get("finding_id") or "")
                quality_versions[finding_id].append(int(artifact["version"]))
                for field in ("project_id", "workflow_id", "version"):
                    if content.get(field) != artifact[field]:
                        self.add(
                            "ERROR",
                            "QUALITY_FINDING_IDENTITY_MISMATCH",
                            "artifact",
                            artifact_id,
                            "Quality finding row and lifecycle payload disagree.",
                            field=field,
                            row_value=artifact[field],
                            content_value=content.get(field),
                        )
                lifecycle = content.get("lifecycle") or {}
                if lifecycle.get("state") != artifact["status"]:
                    self.add(
                        "ERROR",
                        "QUALITY_FINDING_STATUS_MISMATCH",
                        "artifact",
                        artifact_id,
                        "Quality finding row and lifecycle state disagree.",
                        row_status=artifact["status"],
                        lifecycle_status=lifecycle.get("state"),
                    )

        for key, versions in sorted(prompt_versions.items()):
            unique = sorted(set(versions))
            if len(unique) != len(versions):
                self.add(
                    "ERROR",
                    "DUPLICATE_PROMPT_ARTIFACT_VERSION",
                    "artifact_group",
                    ":".join(str(item) for item in key),
                    "Prompt artifact versions are not unique within project/prompt/type.",
                    versions=versions,
                )
            expected = list(range(1, max(unique) + 1)) if unique else []
            if unique != expected:
                self.add(
                    "ERROR",
                    "GAPPED_PROMPT_ARTIFACT_VERSION",
                    "artifact_group",
                    ":".join(str(item) for item in key),
                    "Prompt artifact versions are not contiguous within project/prompt/type.",
                    actual=unique,
                    expected=expected,
                )

        for finding_id, versions in sorted(quality_versions.items()):
            unique = sorted(set(versions))
            expected = list(range(1, max(unique) + 1)) if unique else []
            if not finding_id or len(unique) != len(versions) or unique != expected:
                self.add(
                    "ERROR",
                    "INVALID_QUALITY_FINDING_VERSION_CHAIN",
                    "quality_finding",
                    finding_id or "<missing>",
                    "Quality finding identity has a duplicate, gap, or missing finding_id.",
                    versions=versions,
                    expected=expected,
                )

    def _audit_gates(
        self,
        gates: dict[str, dict[str, Any]],
        workflows: dict[str, dict[str, Any]],
        runs: dict[str, dict[str, Any]],
    ) -> None:
        open_by_workflow: dict[str, list[str]] = collections.defaultdict(list)
        for gate_id, gate in gates.items():
            workflow = workflows.get(gate["workflow_id"])
            if workflow is None:
                self.add(
                    "ERROR",
                    "GATE_DANGLING_WORKFLOW",
                    "gate",
                    gate_id,
                    "Gate references a missing workflow.",
                    workflow_id=gate["workflow_id"],
                )
                continue
            if workflow["project_id"] != gate["project_id"]:
                self.add(
                    "ERROR",
                    "GATE_CROSS_PROJECT_WORKFLOW",
                    "gate",
                    gate_id,
                    "Gate and workflow belong to different projects.",
                    gate_project_id=gate["project_id"],
                    workflow_project_id=workflow["project_id"],
                )

            allowed = self._json(
                gate,
                "allowed_actions_json",
                table="gates",
                row_id=gate_id,
                auditor=self,
            )
            questions = self._json(
                gate,
                "questions_json",
                table="gates",
                row_id=gate_id,
                auditor=self,
            )
            decision = self._json(
                gate,
                "decision_json",
                table="gates",
                row_id=gate_id,
                auditor=self,
                nullable=True,
            )
            if not isinstance(allowed, list):
                self.add(
                    "ERROR",
                    "GATE_ACTIONS_NOT_ARRAY",
                    "gate",
                    gate_id,
                    "allowed_actions_json must decode to an array.",
                )
                allowed = []
            if not isinstance(questions, list):
                self.add(
                    "ERROR",
                    "GATE_QUESTIONS_NOT_ARRAY",
                    "gate",
                    gate_id,
                    "questions_json must decode to an array.",
                )

            if gate["status"] == "OPEN":
                open_by_workflow[gate["workflow_id"]].append(gate_id)
                if decision is not None:
                    self.add(
                        "ERROR",
                        "OPEN_GATE_HAS_DECISION",
                        "gate",
                        gate_id,
                        "Open Gate already contains decision_json.",
                    )
            elif decision is None:
                self.add(
                    "ERROR",
                    "CLOSED_GATE_WITHOUT_DECISION",
                    "gate",
                    gate_id,
                    "Closed Gate lacks decision_json.",
                    status=gate["status"],
                )

            if gate["status"] == "APPROVED" and isinstance(decision, dict):
                if decision.get("action") not in allowed:
                    self.add(
                        "ERROR",
                        "GATE_DECISION_ACTION_NOT_ALLOWED",
                        "gate",
                        gate_id,
                        "Approved Gate action is not in allowed_actions.",
                        action=decision.get("action"),
                        allowed_actions=allowed,
                    )
                if decision.get("decided_role") != gate["required_role"]:
                    self.add(
                        "ERROR",
                        "GATE_DECISION_ROLE_MISMATCH",
                        "gate",
                        gate_id,
                        "Approved Gate was decided by the wrong role.",
                        required_role=gate["required_role"],
                        decided_role=decision.get("decided_role"),
                    )
                if decision.get("context_hash") != gate["context_hash"]:
                    self.add(
                        "ERROR",
                        "GATE_DECISION_CONTEXT_MISMATCH",
                        "gate",
                        gate_id,
                        "Approved Gate decision does not bind the Gate context hash.",
                        gate_context_hash=gate["context_hash"],
                        decision_context_hash=decision.get("context_hash"),
                    )

            target_id = gate["target_id"]
            if target_id.startswith("run-"):
                run = runs.get(target_id)
                if run is None:
                    self.add(
                        "ERROR",
                        "GATE_DANGLING_RUN_TARGET",
                        "gate",
                        gate_id,
                        "Gate target prompt run does not exist.",
                        target_id=target_id,
                    )
                elif run["workflow_id"] != gate["workflow_id"] or run["project_id"] != gate["project_id"]:
                    self.add(
                        "ERROR",
                        "GATE_RUN_TARGET_WRONG_OWNER",
                        "gate",
                        gate_id,
                        "Gate target run belongs to another workflow or project.",
                        target_id=target_id,
                        run_workflow_id=run["workflow_id"],
                        run_project_id=run["project_id"],
                    )
            elif target_id.startswith("wf-"):
                if target_id != gate["workflow_id"]:
                    self.add(
                        "ERROR",
                        "GATE_WORKFLOW_TARGET_MISMATCH",
                        "gate",
                        gate_id,
                        "Workflow-targeted Gate does not target its own workflow.",
                        target_id=target_id,
                        workflow_id=gate["workflow_id"],
                    )
            elif not target_id.startswith("input:"):
                self.add(
                    "WARNING",
                    "UNRECOGNIZED_GATE_TARGET_FORMAT",
                    "gate",
                    gate_id,
                    "Gate target uses an unrecognized identity format.",
                    target_id=target_id,
                )

        for workflow_id, gate_ids in sorted(open_by_workflow.items()):
            if len(gate_ids) > 1:
                self.add(
                    "ERROR",
                    "MULTIPLE_OPEN_GATES",
                    "workflow",
                    workflow_id,
                    "Workflow has more than one open Gate.",
                    gate_ids=gate_ids,
                )

    def _audit_documents(self, conn: sqlite3.Connection) -> None:
        for row in conn.execute("SELECT * FROM documents"):
            document_id = row["id"]
            parsed = self._json(
                row,
                "parsed_json",
                table="documents",
                row_id=document_id,
                auditor=self,
            )
            if isinstance(parsed, dict):
                if parsed.get("document_id") != document_id:
                    self.add(
                        "ERROR",
                        "DOCUMENT_PARSED_ID_MISMATCH",
                        "document",
                        document_id,
                        "Document row and parsed payload disagree on document_id.",
                        parsed_document_id=parsed.get("document_id"),
                    )
                if parsed.get("document_hash") != row["document_hash"]:
                    self.add(
                        "ERROR",
                        "DOCUMENT_PARSED_HASH_MISMATCH",
                        "document",
                        document_id,
                        "Document row and parsed payload disagree on document_hash.",
                        row_document_hash=row["document_hash"],
                        parsed_document_hash=parsed.get("document_hash"),
                    )
            path = Path(str(row["file_path"]))
            if not path.is_file():
                self.add(
                    "WARNING",
                    "DOCUMENT_SOURCE_FILE_UNAVAILABLE",
                    "document",
                    document_id,
                    "Stored upload path is unavailable in this backup environment; parsed_json remains present but raw reparse is not possible.",
                    filename=row["filename"],
                    file_path=row["file_path"],
                )

    def _audit_audit_events(
        self,
        conn: sqlite3.Connection,
        projects: dict[str, dict[str, Any]],
        workflows: dict[str, dict[str, Any]],
        runs: dict[str, dict[str, Any]],
    ) -> None:
        for row in conn.execute("SELECT * FROM audit_events"):
            event_id = str(row["id"])
            if row["project_id"] and row["project_id"] not in projects:
                self.add(
                    "ERROR",
                    "AUDIT_EVENT_DANGLING_PROJECT",
                    "audit_event",
                    event_id,
                    "Audit event references a missing project.",
                    project_id=row["project_id"],
                )
            metadata = self._json(
                row,
                "metadata_json",
                table="audit_events",
                row_id=event_id,
                auditor=self,
            )
            if not isinstance(metadata, dict):
                continue
            run_id = metadata.get("run_id")
            if isinstance(run_id, str) and run_id.startswith("run-") and run_id not in runs:
                self.add(
                    "ERROR",
                    "AUDIT_EVENT_DANGLING_RUN",
                    "audit_event",
                    event_id,
                    "Audit event metadata references a missing prompt run.",
                    run_id=run_id,
                    event_type=row["event_type"],
                )
            workflow_id = metadata.get("workflow_id")
            if (
                isinstance(workflow_id, str)
                and workflow_id.startswith("wf-")
                and workflow_id not in workflows
            ):
                self.add(
                    "ERROR",
                    "AUDIT_EVENT_DANGLING_WORKFLOW",
                    "audit_event",
                    event_id,
                    "Audit event metadata references a missing workflow.",
                    workflow_id=workflow_id,
                    event_type=row["event_type"],
                )

    def report(self) -> dict[str, Any]:
        severity_counts = collections.Counter(item.severity for item in self.findings)
        code_counts = collections.Counter(item.code for item in self.findings)
        ordered = sorted(
            self.findings,
            key=lambda item: (
                {"ERROR": 0, "WARNING": 1, "INFO": 2}.get(item.severity, 9),
                item.code,
                item.object_type,
                item.object_id,
            ),
        )
        return {
            "schema_version": "1.0",
            "database": str(self.database),
            "read_only": True,
            "table_counts": dict(sorted(self.counts.items())),
            "summary": {
                "finding_count": len(ordered),
                "severity_counts": dict(sorted(severity_counts.items())),
                "code_counts": dict(sorted(code_counts.items())),
            },
            "findings": [asdict(item) for item in ordered],
        }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Database invariant audit",
        "",
        f"- Database: `{report['database']}`",
        "- Open mode: read-only",
        f"- Findings: {report['summary']['finding_count']}",
        f"- Severity counts: `{json.dumps(report['summary']['severity_counts'], ensure_ascii=False, sort_keys=True)}`",
        "",
        "## Table counts",
        "",
    ]
    for table, count in report["table_counts"].items():
        lines.append(f"- `{table}`: {count}")
    lines.extend(["", "## Findings", ""])
    if not report["findings"]:
        lines.append("No invariant finding was detected.")
    for finding in report["findings"]:
        lines.extend(
            [
                f"### {finding['severity']} — `{finding['code']}`",
                "",
                f"- Object: `{finding['object_type']}:{finding['object_id']}`",
                f"- Message: {finding['message']}",
                f"- Details: `{json.dumps(finding['details'], ensure_ascii=False, sort_keys=True)}`",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    parser.add_argument(
        "--fail-on",
        choices=("NEVER", "ERROR", "WARNING"),
        default="ERROR",
        help="Exit nonzero when findings at or above this severity exist.",
    )
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    report = DatabaseInvariantAuditor(args.database).audit()
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(payload, encoding="utf-8")
    if args.markdown_output:
        args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
        args.markdown_output.write_text(render_markdown(report), encoding="utf-8")
    if not args.json_output and not args.markdown_output:
        sys.stdout.write(payload)

    counts = report["summary"]["severity_counts"]
    if args.fail_on == "WARNING" and (counts.get("ERROR", 0) or counts.get("WARNING", 0)):
        return 2
    if args.fail_on == "ERROR" and counts.get("ERROR", 0):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
