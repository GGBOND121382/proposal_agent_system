from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

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
CREATE INDEX IF NOT EXISTS idx_gates_workflow ON gates(workflow_id, status);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
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
            (project_id, event_type, object_id, json.dumps(metadata or {}, ensure_ascii=False), utc_now()),
        )
