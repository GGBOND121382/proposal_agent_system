from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .private_storage import secure_private_directory, secure_private_file
from .secret_redaction import redact_secrets
from .util import utc_now


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  security_level TEXT NOT NULL,
  config_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  filename TEXT NOT NULL,
  role TEXT NOT NULL,
  security_level TEXT NOT NULL,
  document_hash TEXT NOT NULL,
  file_path TEXT NOT NULL,
  parsed_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS artifacts (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  workflow_id TEXT,
  artifact_type TEXT NOT NULL,
  prompt_id TEXT,
  version INTEGER NOT NULL,
  status TEXT NOT NULL,
  security_level TEXT NOT NULL,
  context_hash TEXT NOT NULL,
  content_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifacts_project_prompt_type_version
  ON artifacts(project_id,prompt_id,artifact_type,version DESC);
CREATE INDEX IF NOT EXISTS idx_artifacts_workflow_prompt_type_status_version
  ON artifacts(workflow_id,prompt_id,artifact_type,status,version DESC);

CREATE TABLE IF NOT EXISTS prompt_runs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  workflow_id TEXT,
  prompt_id TEXT NOT NULL,
  status TEXT NOT NULL,
  model_id TEXT,
  endpoint_id TEXT,
  input_hash TEXT NOT NULL,
  output_hash TEXT,
  input_json TEXT NOT NULL,
  output_json TEXT,
  error TEXT,
  duration_ms INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS skill_runs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  workflow_id TEXT,
  skill_id TEXT NOT NULL,
  skill_version TEXT NOT NULL,
  status TEXT NOT NULL,
  input_hash TEXT NOT NULL,
  output_hash TEXT,
  input_json TEXT NOT NULL,
  output_json TEXT,
  error TEXT,
  duration_ms INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workflows (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  workflow_type TEXT NOT NULL,
  status TEXT NOT NULL,
  current_step INTEGER NOT NULL,
  state_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workflow_rebuild_operations (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  branch_id TEXT NOT NULL,
  root_source_workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE RESTRICT,
  scope TEXT NOT NULL,
  status TEXT NOT NULL,
  current_index INTEGER NOT NULL,
  plan_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workflow_lineage (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  branch_id TEXT NOT NULL,
  operation_id TEXT NOT NULL REFERENCES workflow_rebuild_operations(id) ON DELETE CASCADE,
  parent_workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE RESTRICT,
  child_workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE RESTRICT,
  relation_type TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(child_workflow_id)
);
CREATE TABLE IF NOT EXISTS workflow_branch_members (
  branch_id TEXT NOT NULL,
  operation_id TEXT NOT NULL REFERENCES workflow_rebuild_operations(id) ON DELETE CASCADE,
  workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
  source_workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE RESTRICT,
  position INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(branch_id, workflow_id)
);
CREATE TABLE IF NOT EXISTS gates (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
  gate_type TEXT NOT NULL,
  target_id TEXT NOT NULL,
  target_version INTEGER NOT NULL,
  context_hash TEXT NOT NULL,
  question_version INTEGER NOT NULL,
  required_role TEXT NOT NULL,
  allowed_actions_json TEXT NOT NULL,
  questions_json TEXT NOT NULL,
  security_level TEXT NOT NULL,
  status TEXT NOT NULL,
  decision_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT,
  event_type TEXT NOT NULL,
  object_id TEXT,
  metadata_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_documents_project ON documents(project_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_project ON artifacts(project_id, prompt_id, created_at);
CREATE INDEX IF NOT EXISTS idx_runs_project ON prompt_runs(project_id, created_at);
CREATE INDEX IF NOT EXISTS idx_skill_runs_project ON skill_runs(project_id, skill_id, created_at);
CREATE INDEX IF NOT EXISTS idx_gates_workflow ON gates(workflow_id, status);
CREATE INDEX IF NOT EXISTS idx_workflow_rebuild_project ON workflow_rebuild_operations(project_id, created_at);
CREATE INDEX IF NOT EXISTS idx_workflow_lineage_parent ON workflow_lineage(parent_workflow_id, created_at);
CREATE INDEX IF NOT EXISTS idx_workflow_lineage_child ON workflow_lineage(child_workflow_id);
CREATE INDEX IF NOT EXISTS idx_workflow_branch_members ON workflow_branch_members(branch_id, position);
"""


class DatabaseTransaction:
    """One explicit SQLite transaction shared by a logical state change."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def audit(
        self,
        event_type: str,
        *,
        project_id: str | None = None,
        object_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.execute(
            "INSERT INTO audit_events(project_id,event_type,object_id,metadata_json,created_at) VALUES(?,?,?,?,?)",
            (
                project_id,
                event_type,
                object_id,
                json.dumps(redact_secrets(metadata or {}), ensure_ascii=False),
                utc_now(),
            ),
        )

    def next_artifact_version(
        self,
        *,
        project_id: str,
        workflow_id: str | None,
        artifact_type: str,
        prompt_id: str | None,
    ) -> int:
        row = self.fetchone(
            """SELECT COALESCE(MAX(version),0) AS version
               FROM artifacts
               WHERE project_id=?
                 AND workflow_id IS ?
                 AND artifact_type=?
                 AND prompt_id IS ?""",
            (project_id, workflow_id, artifact_type, prompt_id),
        )
        return int((row or {}).get("version") or 0) + 1


    def update_workflow(
        self,
        *,
        workflow_id: str,
        status: str,
        current_step: int,
        state: dict[str, Any],
        expected_updated_at: str | None = None,
    ) -> str:
        updated_at = utc_now()
        sql = "UPDATE workflows SET status=?,current_step=?,state_json=?,updated_at=? WHERE id=?"
        params: tuple[Any, ...] = (
            status,
            current_step,
            json.dumps(state, ensure_ascii=False),
            updated_at,
            workflow_id,
        )
        if expected_updated_at is not None:
            sql += " AND updated_at=?"
            params += (expected_updated_at,)
        cursor = self.execute(sql, params)
        if cursor.rowcount != 1:
            if expected_updated_at is not None:
                raise RuntimeError(
                    f"workflow changed during atomic update: {workflow_id}"
                )
            raise KeyError(f"workflow not found during atomic update: {workflow_id}")
        return updated_at


class Database:
    def __init__(self, path: Path):
        self.path = path
        secure_private_directory(self.path.parent)
        with self.connection() as conn:
            conn.executescript(SCHEMA)
        for database_file in (
            self.path,
            Path(f"{self.path}-wal"),
            Path(f"{self.path}-shm"),
            Path(f"{self.path}-journal"),
        ):
            if database_file.is_file():
                secure_private_file(database_file)

    def _open_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = self._open_connection()
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[DatabaseTransaction]:
        """Open one explicit transaction for a complete logical write.

        ``BEGIN IMMEDIATE`` is the default so version allocation and the insert
        that consumes it are serialized against other writers.  Callers must not
        invoke top-level ``Database.execute/fetch/audit`` methods inside this
        block because those methods intentionally use separate connections.
        """

        conn = self._open_connection()
        try:
            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield DatabaseTransaction(conn)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        with self.connection() as conn:
            conn.execute(sql, params)

    def fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def audit(self, event_type: str, *, project_id: str | None = None, object_id: str | None = None, metadata: dict[str, Any] | None = None) -> None:
        self.execute(
            "INSERT INTO audit_events(project_id,event_type,object_id,metadata_json,created_at) VALUES(?,?,?,?,?)",
            (project_id, event_type, object_id, json.dumps(redact_secrets(metadata or {}), ensure_ascii=False), utc_now()),
        )
