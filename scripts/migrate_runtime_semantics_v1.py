from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


MIGRATION_NAME = "runtime_semantics_v1"
MIGRATION_SCHEMA_VERSION = "2.1.0"
_REQUIRED_COLUMNS = {
    "projects": {"id", "security_level"},
    "workflows": {"id", "project_id", "current_step", "state_json", "updated_at"},
    "artifacts": {
        "id",
        "project_id",
        "workflow_id",
        "artifact_type",
        "prompt_id",
        "version",
        "status",
        "security_level",
        "context_hash",
        "content_json",
        "created_at",
    },
    "audit_events": {
        "project_id",
        "event_type",
        "object_id",
        "metadata_json",
        "created_at",
    },
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def stable_id(prefix: str, *parts: Any) -> str:
    return f"{prefix}-{digest(list(parts))[:32]}"


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")}


def preflight(conn: sqlite3.Connection, database: Path) -> dict[str, Any]:
    if not database.exists() or not database.is_file():
        raise FileNotFoundError(database)
    missing: dict[str, list[str]] = {}
    for table, required in _REQUIRED_COLUMNS.items():
        columns = _table_columns(conn, table)
        absent = sorted(required - columns)
        if absent:
            missing[table] = absent
    if missing:
        raise ValueError(f"migration preflight failed; missing columns: {missing}")

    workflow_count = 0
    for row in conn.execute("SELECT id,state_json FROM workflows ORDER BY id"):
        try:
            state = json.loads(row["state_json"] or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError(f"workflow {row['id']} has invalid state_json") from exc
        if not isinstance(state, dict):
            raise ValueError(f"workflow {row['id']} state_json must be an object")
        workflow_count += 1
    return {
        "status": "PASS",
        "database": str(database),
        "workflow_count": workflow_count,
        "required_tables": sorted(_REQUIRED_COLUMNS),
    }


def _next_versions(conn: sqlite3.Connection) -> dict[tuple[str, str, str, str], int]:
    versions: dict[tuple[str, str, str, str], int] = {}
    rows = conn.execute(
        """SELECT project_id,COALESCE(workflow_id,'') AS workflow_id,artifact_type,
                  COALESCE(prompt_id,'') AS prompt_id,MAX(version) AS version
           FROM artifacts
           GROUP BY project_id,workflow_id,artifact_type,prompt_id"""
    ).fetchall()
    for row in rows:
        key = (
            str(row["project_id"]),
            str(row["workflow_id"]),
            str(row["artifact_type"]),
            str(row["prompt_id"]),
        )
        versions[key] = int(row["version"] or 0)
    return versions


def _plan_human_resolutions(
    *,
    workflow: sqlite3.Row,
    state: dict[str, Any],
    security_level: str,
    versions: dict[tuple[str, str, str, str], int],
) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    legacy = state.get("human_resolutions") or {}
    overrides = state.get("human_input_overrides") or {}
    if not isinstance(legacy, dict):
        raise ValueError(f"workflow {workflow['id']} human_resolutions must be an object")
    if not isinstance(overrides, dict):
        raise ValueError(f"workflow {workflow['id']} human_input_overrides must be an object")

    artifacts: list[dict[str, Any]] = []
    artifact_ids: dict[str, list[str]] = {}
    covered_paths: dict[str, set[str]] = {}
    source_state_hash = digest(state)

    def add_resolution(
        *,
        prompt_id: str,
        resolution: dict[str, Any],
        source_path: str,
        authority: str,
    ) -> None:
        section_id = str(
            resolution.get("section_id") or state.get("active_section_id") or ""
        ).strip() or None
        scope_key = str(resolution.get("scope_key") or "").strip()
        if not scope_key:
            scope_key = (
                f"section:{section_id}:{prompt_id}"
                if section_id
                else f"step:{workflow['current_step']}:{prompt_id}"
            )
        source_key = str(resolution.get("resolution_id") or digest(resolution))
        artifact_id = stable_id(
            "artifact",
            MIGRATION_NAME,
            workflow["id"],
            "HUMAN_RESOLUTION",
            scope_key,
            prompt_id,
            source_key,
        )
        payload = {
            "schema_version": "1.0.0",
            "workflow_id": workflow["id"],
            "prompt_id": prompt_id,
            "scope_key": scope_key,
            "section_id": section_id,
            "workflow_step": int(workflow["current_step"]),
            "resolution": resolution,
            "authority": authority,
            "supersedes_state_override": True,
            "migration": {
                "name": MIGRATION_NAME,
                "schema_version": MIGRATION_SCHEMA_VERSION,
                "source_path": source_path,
                "source_state_hash": source_state_hash,
            },
        }
        key = (
            str(workflow["project_id"]),
            str(workflow["id"]),
            "HUMAN_RESOLUTION",
            prompt_id,
        )
        versions[key] = versions.get(key, 0) + 1
        artifacts.append(
            {
                "id": artifact_id,
                "project_id": workflow["project_id"],
                "workflow_id": workflow["id"],
                "artifact_type": "HUMAN_RESOLUTION",
                "prompt_id": prompt_id,
                "version": versions[key],
                "status": "PASS",
                "security_level": security_level,
                "context_hash": digest(payload),
                "content_json": _canonical_json(payload),
                "source_key": source_key,
            }
        )
        artifact_ids.setdefault(scope_key, []).append(artifact_id)
        covered_paths.setdefault(scope_key, set()).update(
            str(item).strip()
            for item in resolution.get("target_paths") or []
            if str(item).strip()
        )

    for prompt_id in sorted(str(key) for key in legacy):
        resolutions = legacy.get(prompt_id)
        if not isinstance(resolutions, list):
            raise ValueError(
                f"workflow {workflow['id']} human_resolutions[{prompt_id}] must be an array"
            )
        for index, resolution in enumerate(resolutions):
            if not isinstance(resolution, dict):
                raise ValueError(
                    f"workflow {workflow['id']} resolution {prompt_id}[{index}] must be an object"
                )
            add_resolution(
                prompt_id=prompt_id,
                resolution=resolution,
                source_path=f"human_resolutions.{prompt_id}[{index}]",
                authority="MIGRATED_HUMAN_GATE_DECISION",
            )

    for prompt_id in sorted(str(key) for key in overrides):
        values = overrides.get(prompt_id)
        if not isinstance(values, dict):
            raise ValueError(
                f"workflow {workflow['id']} human_input_overrides[{prompt_id}] must be an object"
            )
        for target_path in sorted(str(key) for key in values):
            section_id = str(state.get("active_section_id") or "").strip() or None
            scope_key = (
                f"section:{section_id}:{prompt_id}"
                if section_id
                else f"step:{workflow['current_step']}:{prompt_id}"
            )
            if target_path in covered_paths.get(scope_key, set()):
                continue
            answer = values[target_path]
            resolution = {
                "resolution_id": stable_id(
                    "human", workflow["id"], prompt_id, target_path, answer
                ),
                "gate_id": "legacy-workflow-state",
                "prompt_id": prompt_id,
                "question_id": stable_id("legacy-question", prompt_id, target_path),
                "question": "Migrated legacy human input override",
                "target_paths": [target_path],
                "answer": answer,
                "decided_by": "runtime-semantics-migration",
                "decided_role": "MIGRATED_LEGACY_AUTHORITY",
            }
            add_resolution(
                prompt_id=prompt_id,
                resolution=resolution,
                source_path=f"human_input_overrides.{prompt_id}.{target_path}",
                authority="MIGRATED_LEGACY_HUMAN_OVERRIDE",
            )
    return artifacts, artifact_ids


def _repair_prompt_from_target_key(target_key: str) -> tuple[str | None, str]:
    if target_key.startswith("section:"):
        parts = target_key.split(":", 2)
        if len(parts) != 3 or not parts[1] or not parts[2]:
            raise ValueError(f"invalid section-scoped repair override key: {target_key}")
        return parts[1], parts[2]
    if not target_key.strip():
        raise ValueError("repair override key must not be empty")
    return None, target_key


def _plan_repair_overrides(
    *,
    workflow: sqlite3.Row,
    state: dict[str, Any],
    security_level: str,
    versions: dict[tuple[str, str, str, str], int],
) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    legacy = state.get("repair_overrides") or {}
    if not isinstance(legacy, dict):
        raise ValueError(f"workflow {workflow['id']} repair_overrides must be an object")
    artifacts: list[dict[str, Any]] = []
    artifact_ids: dict[str, list[str]] = {}
    source_state_hash = digest(state)
    for target_key in sorted(str(key) for key in legacy):
        section_id, producer_prompt = _repair_prompt_from_target_key(target_key)
        repaired_value = legacy[target_key]
        value_hash = digest(repaired_value)
        artifact_id = stable_id(
            "artifact",
            MIGRATION_NAME,
            workflow["id"],
            "REPAIR_APPLICATION",
            target_key,
            value_hash,
        )
        payload = {
            "schema_version": "1.0.0",
            "workflow_id": workflow["id"],
            "producer_prompt": producer_prompt,
            "target_key": target_key,
            "section_id": section_id,
            "application_status": "APPLIED",
            "repaired_value": repaired_value,
            "repaired_value_hash": value_hash,
            "authority": "MIGRATED_REPAIR_OVERRIDE",
            "migration": {
                "name": MIGRATION_NAME,
                "schema_version": MIGRATION_SCHEMA_VERSION,
                "source_path": f"repair_overrides.{target_key}",
                "source_state_hash": source_state_hash,
            },
        }
        key = (
            str(workflow["project_id"]),
            str(workflow["id"]),
            "REPAIR_APPLICATION",
            producer_prompt,
        )
        versions[key] = versions.get(key, 0) + 1
        artifacts.append(
            {
                "id": artifact_id,
                "project_id": workflow["project_id"],
                "workflow_id": workflow["id"],
                "artifact_type": "REPAIR_APPLICATION",
                "prompt_id": producer_prompt,
                "version": versions[key],
                "status": "PASS",
                "security_level": security_level,
                "context_hash": digest(payload),
                "content_json": _canonical_json(payload),
                "source_key": target_key,
            }
        )
        artifact_ids.setdefault(target_key, []).append(artifact_id)
    return artifacts, artifact_ids


def build_plan(conn: sqlite3.Connection, database: Path) -> dict[str, Any]:
    security = {
        str(row["id"]): str(row["security_level"])
        for row in conn.execute("SELECT id,security_level FROM projects ORDER BY id")
    }
    versions = _next_versions(conn)
    artifacts: list[dict[str, Any]] = []
    workflow_updates: list[dict[str, Any]] = []

    rows = conn.execute(
        "SELECT id,project_id,current_step,state_json,updated_at FROM workflows ORDER BY id"
    ).fetchall()
    for workflow in rows:
        state = json.loads(workflow["state_json"] or "{}")
        if not isinstance(state, dict):
            raise ValueError(f"workflow {workflow['id']} state_json must be an object")
        has_legacy_state = any(
            bool(state.get(key))
            for key in ("human_resolutions", "human_input_overrides", "repair_overrides")
        )
        if not has_legacy_state:
            continue
        project_id = str(workflow["project_id"])
        if project_id not in security:
            raise ValueError(f"workflow {workflow['id']} references missing project {project_id}")

        human_artifacts, human_ids = _plan_human_resolutions(
            workflow=workflow,
            state=state,
            security_level=security[project_id],
            versions=versions,
        )
        repair_artifacts, repair_ids = _plan_repair_overrides(
            workflow=workflow,
            state=state,
            security_level=security[project_id],
            versions=versions,
        )
        artifacts.extend(human_artifacts)
        artifacts.extend(repair_artifacts)
        next_state = json.loads(json.dumps(state, ensure_ascii=False))

        existing_human_index = next_state.get("human_resolution_artifact_ids")
        if not isinstance(existing_human_index, dict):
            existing_human_index = {}
        for prompt_id, ids in human_ids.items():
            merged = [str(item) for item in existing_human_index.get(prompt_id) or []]
            for artifact_id in ids:
                if artifact_id not in merged:
                    merged.append(artifact_id)
            existing_human_index[prompt_id] = merged[-50:]
        if existing_human_index:
            next_state["human_resolution_artifact_ids"] = existing_human_index

        existing_repair_index = next_state.get("repair_application_artifact_ids")
        if not isinstance(existing_repair_index, dict):
            existing_repair_index = {}
        for target_key, ids in repair_ids.items():
            merged = [str(item) for item in existing_repair_index.get(target_key) or []]
            for artifact_id in ids:
                if artifact_id not in merged:
                    merged.append(artifact_id)
            existing_repair_index[target_key] = merged[-50:]
        if existing_repair_index:
            next_state["repair_application_artifact_ids"] = existing_repair_index

        next_state.pop("human_resolutions", None)
        next_state.pop("human_input_overrides", None)
        next_state.pop("repair_overrides", None)
        next_state[MIGRATION_NAME] = {
            "schema_version": MIGRATION_SCHEMA_VERSION,
            "source_state_hash": digest(state),
            "human_resolution_artifact_count": sum(len(ids) for ids in human_ids.values()),
            "repair_application_artifact_count": sum(len(ids) for ids in repair_ids.values()),
        }
        workflow_updates.append(
            {
                "workflow_id": workflow["id"],
                "project_id": workflow["project_id"],
                "expected_updated_at": workflow["updated_at"],
                "expected_state_json": workflow["state_json"],
                "source_state_hash": digest(state),
                "state_json": _canonical_json(next_state),
                "artifact_ids": {
                    "human_resolutions": human_ids,
                    "repair_applications": repair_ids,
                },
            }
        )

    existing_by_id = {
        str(row["id"]): row
        for row in conn.execute(
            "SELECT id,context_hash,content_json,security_level,version FROM artifacts"
        )
    }
    create_artifacts: list[dict[str, Any]] = []
    reused_artifact_ids: list[str] = []
    for artifact in artifacts:
        existing = existing_by_id.get(artifact["id"])
        if existing is None:
            create_artifacts.append(artifact)
            continue
        if (
            str(existing["context_hash"]) != artifact["context_hash"]
            or str(existing["content_json"]) != artifact["content_json"]
            or str(existing["security_level"]) != artifact["security_level"]
        ):
            raise ValueError(
                f"deterministic artifact id collision with different content: {artifact['id']}"
            )
        reused_artifact_ids.append(artifact["id"])

    core = {
        "migration": MIGRATION_NAME,
        "schema_version": MIGRATION_SCHEMA_VERSION,
        "create_artifacts": create_artifacts,
        "reused_artifact_ids": sorted(reused_artifact_ids),
        "workflow_updates": workflow_updates,
    }
    plan_id = stable_id("migration", core)
    return {
        **core,
        "plan_id": plan_id,
        "backup_id": f"{MIGRATION_NAME}-{plan_id.split('-', 1)[1][:16]}",
        "database": str(database),
    }


def _backup_database(database: Path, backup_id: str) -> Path:
    backup_path = database.with_name(f"{database.name}.{backup_id}.bak")
    source = sqlite3.connect(database)
    target = sqlite3.connect(backup_path)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    return backup_path


def _verify_applied_plan(conn: sqlite3.Connection, plan: dict[str, Any]) -> None:
    for artifact in plan["create_artifacts"]:
        row = conn.execute(
            "SELECT context_hash,content_json,security_level FROM artifacts WHERE id=?",
            (artifact["id"],),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"artifact missing after migration: {artifact['id']}")
        if (
            str(row["context_hash"]) != artifact["context_hash"]
            or str(row["content_json"]) != artifact["content_json"]
            or str(row["security_level"]) != artifact["security_level"]
        ):
            raise RuntimeError(f"artifact integrity mismatch: {artifact['id']}")
    for update in plan["workflow_updates"]:
        row = conn.execute(
            "SELECT state_json FROM workflows WHERE id=?", (update["workflow_id"],)
        ).fetchone()
        if row is None or str(row["state_json"]) != update["state_json"]:
            raise RuntimeError(
                f"workflow state integrity mismatch: {update['workflow_id']}"
            )


def _apply_plan(conn: sqlite3.Connection, plan: dict[str, Any]) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        created_at = now()
        for artifact in plan["create_artifacts"]:
            conn.execute(
                """INSERT INTO artifacts(id,project_id,workflow_id,artifact_type,prompt_id,
                   version,status,security_level,context_hash,content_json,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    artifact["id"],
                    artifact["project_id"],
                    artifact["workflow_id"],
                    artifact["artifact_type"],
                    artifact["prompt_id"],
                    artifact["version"],
                    artifact["status"],
                    artifact["security_level"],
                    artifact["context_hash"],
                    artifact["content_json"],
                    created_at,
                ),
            )
        for update in plan["workflow_updates"]:
            cursor = conn.execute(
                """UPDATE workflows SET state_json=?,updated_at=?
                   WHERE id=? AND updated_at=? AND state_json=?""",
                (
                    update["state_json"],
                    created_at,
                    update["workflow_id"],
                    update["expected_updated_at"],
                    update["expected_state_json"],
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    f"workflow changed after migration plan was built: {update['workflow_id']}"
                )
            conn.execute(
                "INSERT INTO audit_events(project_id,event_type,object_id,metadata_json,created_at) VALUES(?,?,?,?,?)",
                (
                    update["project_id"],
                    "RUNTIME_SEMANTICS_MIGRATED",
                    update["workflow_id"],
                    _canonical_json(
                        {
                            "migration": MIGRATION_NAME,
                            "plan_id": plan["plan_id"],
                            "source_state_hash": update["source_state_hash"],
                            "artifact_ids": update["artifact_ids"],
                        }
                    ),
                    created_at,
                ),
            )
        _verify_applied_plan(conn, plan)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def migrate(database: Path, *, apply: bool) -> dict[str, Any]:
    database = database.resolve()
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    backup_path: Path | None = None
    try:
        preflight_report = preflight(conn, database)
        plan = build_plan(conn, database)
        has_changes = bool(plan["create_artifacts"] or plan["workflow_updates"])
        if apply and has_changes:
            conn.close()
            backup_path = _backup_database(database, plan["backup_id"])
            conn = sqlite3.connect(database)
            conn.row_factory = sqlite3.Row
            # Rebuild under the post-backup snapshot. The deterministic plan id
            # must remain unchanged or the database changed concurrently.
            refreshed = build_plan(conn, database)
            if refreshed["plan_id"] != plan["plan_id"]:
                raise RuntimeError("database changed while preparing migration backup")
            _apply_plan(conn, plan)
        return {
            "mode": "APPLY" if apply else "DRY_RUN",
            "status": "APPLIED" if apply and has_changes else ("NO_CHANGES" if apply else "PLANNED"),
            "database": str(database),
            "migration": MIGRATION_NAME,
            "schema_version": MIGRATION_SCHEMA_VERSION,
            "plan_id": plan["plan_id"],
            "backup_id": plan["backup_id"],
            "backup_path": str(backup_path) if backup_path else None,
            "preflight": preflight_report,
            "planned_workflows": len(plan["workflow_updates"]),
            "planned_new_artifacts": len(plan["create_artifacts"]),
            "reused_artifact_ids": plan["reused_artifact_ids"],
            "workflow_updates": plan["workflow_updates"],
            "artifact_ids": [item["id"] for item in plan["create_artifacts"]],
        }
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    result = migrate(args.database, apply=args.apply)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.report:
        args.report.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
