from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import zipfile
from pathlib import Path
from typing import Any

from .human_gate_bridge import workflow_gate_scope_ids
from .util import utc_now


def _safe_name(value: str) -> str:
    normalized = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in str(value))
    normalized = normalized.strip("-._")
    return normalized[:120] or "portable-run"


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
    tmp.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class PortableRunTrace:
    """Crash-safe evidence bundle for one portable workflow invocation.

    Model-call evidence and file-bridge traffic are configured to live under the
    run directory by the CLI.  This class adds database backups, decoded workflow
    snapshots, gate history, event checkpoints, a hash manifest and a zip bundle.
    It deliberately records failed and blocked runs as first-class evidence.
    """

    def __init__(
        self,
        run_dir: Path,
        *,
        project_id: str,
        workflow_type: str,
        idempotency_key: str,
        options: dict[str, Any],
    ) -> None:
        self.run_dir = Path(run_dir).resolve()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.project_id = str(project_id)
        self.workflow_type = str(workflow_type)
        self.idempotency_key = str(idempotency_key)
        self.options = options
        self.workflow_id: str | None = None
        self._event_index = self._existing_event_count()
        metadata_path = self.run_dir / "RUN_METADATA.json"
        if not metadata_path.exists():
            _atomic_json(
                metadata_path,
                {
                    "schema_version": "1.0",
                    "project_id": self.project_id,
                    "workflow_type": self.workflow_type,
                    "idempotency_key": self.idempotency_key,
                    "options": self.options,
                    "run_dir": str(self.run_dir),
                    "created_at": utc_now(),
                },
            )

    @staticmethod
    def default_run_dir(data_dir: Path, idempotency_key: str) -> Path:
        explicit = os.getenv("PORTABLE_RUN_DIR", "").strip()
        if explicit:
            return Path(explicit).resolve()
        return (Path(data_dir).resolve() / "portable_runs" / _safe_name(idempotency_key)).resolve()

    def _existing_event_count(self) -> int:
        path = self.run_dir / "events.jsonl"
        if not path.exists():
            return 0
        return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())

    def _append_event(self, event: dict[str, Any]) -> None:
        self._event_index += 1
        record = {"index": self._event_index, "recorded_at": utc_now(), **event}
        path = self.run_dir / "events.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _backup_database(source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        tmp = destination.with_name(destination.name + f".tmp-{os.getpid()}")
        if tmp.exists():
            tmp.unlink()
        with sqlite3.connect(source) as src, sqlite3.connect(tmp) as dst:
            src.backup(dst)
        os.replace(tmp, destination)

    @staticmethod
    def _decode_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        decoded: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            for key in list(item):
                if not key.endswith("_json") or not isinstance(item[key], str):
                    continue
                try:
                    item[key.removesuffix("_json")] = json.loads(item[key])
                except json.JSONDecodeError:
                    item[key.removesuffix("_json")] = None
            decoded.append(item)
        return decoded

    def snapshot(
        self,
        engine: Any,
        workflow_id: str,
        *,
        phase: str,
        note: str | None = None,
        error: str | None = None,
    ) -> Path:
        self.workflow_id = str(workflow_id)
        workflow = engine.get(self.workflow_id)
        scope_ids = sorted(workflow_gate_scope_ids(engine, self.workflow_id))
        checkpoint_name = f"{self._event_index + 1:04d}_{_safe_name(phase)}"
        checkpoint_dir = self.run_dir / "checkpoints" / checkpoint_name
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        self._backup_database(Path(engine.db.path), checkpoint_dir / "proposal_agents.sqlite3")

        placeholders = ",".join("?" for _ in scope_ids) or "''"
        project = engine.db.fetchone("SELECT * FROM projects WHERE id=?", (self.project_id,))
        documents = engine.db.fetchall(
            "SELECT * FROM documents WHERE project_id=? ORDER BY created_at,id",
            (self.project_id,),
        )
        workflows = engine.db.fetchall(
            f"SELECT * FROM workflows WHERE id IN ({placeholders}) ORDER BY created_at,id",
            tuple(scope_ids),
        )
        gates = engine.db.fetchall(
            f"SELECT * FROM gates WHERE workflow_id IN ({placeholders}) ORDER BY created_at,id",
            tuple(scope_ids),
        )
        prompt_runs = engine.db.fetchall(
            f"SELECT * FROM prompt_runs WHERE workflow_id IN ({placeholders}) ORDER BY created_at,id",
            tuple(scope_ids),
        )
        skill_runs = engine.db.fetchall(
            f"SELECT * FROM skill_runs WHERE workflow_id IN ({placeholders}) ORDER BY created_at,id",
            tuple(scope_ids),
        )
        artifacts = engine.db.fetchall(
            f"SELECT * FROM artifacts WHERE workflow_id IN ({placeholders}) ORDER BY created_at,id",
            tuple(scope_ids),
        )
        audit_events = engine.db.fetchall(
            "SELECT * FROM audit_events WHERE project_id=? ORDER BY id",
            (self.project_id,),
        )
        snapshot = {
            "schema_version": "1.0",
            "phase": phase,
            "note": note,
            "error": error,
            "project": self._decode_rows([project] if project else []),
            "documents": self._decode_rows(documents),
            "workflow_scope_ids": scope_ids,
            "workflows": self._decode_rows(workflows),
            "gates": self._decode_rows(gates),
            "prompt_runs": self._decode_rows(prompt_runs),
            "skill_runs": self._decode_rows(skill_runs),
            "artifacts": self._decode_rows(artifacts),
            "audit_events": self._decode_rows(audit_events),
            "captured_at": utc_now(),
        }
        _atomic_json(checkpoint_dir / "STATE_SNAPSHOT.json", snapshot)
        _atomic_json(self.run_dir / "LATEST_STATE.json", snapshot)
        self._append_event(
            {
                "event_type": "CHECKPOINT",
                "phase": phase,
                "workflow_id": self.workflow_id,
                "workflow_status": workflow.get("status"),
                "current_step": workflow.get("current_step"),
                "checkpoint_dir": str(checkpoint_dir),
                "note": note,
                "error": error,
            }
        )
        return checkpoint_dir

    def record_preflight_failure(self, error: BaseException) -> None:
        self._append_event(
            {
                "event_type": "PREFLIGHT_FAILURE",
                "phase": "PREFLIGHT_FAILED",
                "error_type": type(error).__name__,
                "error": str(error),
            }
        )
        _atomic_json(
            self.run_dir / "FAILURE.json",
            {"phase": "PREFLIGHT_FAILED", "error_type": type(error).__name__, "error": str(error), "recorded_at": utc_now()},
        )

    def _collect_external_tree(self, source: Path | None, label: str) -> None:
        if source is None or not source.exists():
            return
        try:
            source.relative_to(self.run_dir)
            return
        except ValueError:
            pass
        destination = self.run_dir / "external_evidence" / label
        if source.is_dir():
            shutil.copytree(source, destination, dirs_exist_ok=True)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)

    def finalize(
        self,
        *,
        status: str,
        engine: Any | None = None,
        workflow_id: str | None = None,
        error: BaseException | None = None,
        external_paths: dict[str, Path | None] | None = None,
    ) -> Path:
        if engine is not None and workflow_id:
            try:
                self.snapshot(
                    engine,
                    workflow_id,
                    phase=f"FINAL_{status}",
                    error=str(error) if error else None,
                )
            except Exception as snapshot_error:  # preserve the original failure
                self._append_event(
                    {
                        "event_type": "FINAL_SNAPSHOT_FAILURE",
                        "phase": f"FINAL_{status}",
                        "error_type": type(snapshot_error).__name__,
                        "error": str(snapshot_error),
                    }
                )
        for label, path in (external_paths or {}).items():
            self._collect_external_tree(path, _safe_name(label))
        final_record = {
            "status": status,
            "project_id": self.project_id,
            "workflow_id": workflow_id or self.workflow_id,
            "error_type": type(error).__name__ if error else None,
            "error": str(error) if error else None,
            "finished_at": utc_now(),
        }
        _atomic_json(self.run_dir / "RUN_RESULT.json", final_record)
        self._append_event({"event_type": "RUN_FINALIZED", **final_record})

        records = []
        for path in sorted(self.run_dir.rglob("*")):
            if not path.is_file() or path.name == "TRACE_MANIFEST.json":
                continue
            records.append(
                {
                    "path": path.relative_to(self.run_dir).as_posix(),
                    "size_bytes": path.stat().st_size,
                    "sha256": _sha256_file(path),
                }
            )
        manifest = {
            "schema_version": "1.0",
            "status": status,
            "project_id": self.project_id,
            "workflow_id": workflow_id or self.workflow_id,
            "file_count": len(records),
            "files": records,
            "created_at": utc_now(),
        }
        _atomic_json(self.run_dir / "TRACE_MANIFEST.json", manifest)

        bundle_path = self.run_dir.parent / f"{self.run_dir.name}.zip"
        tmp_bundle = bundle_path.with_name(bundle_path.name + f".tmp-{os.getpid()}")
        if tmp_bundle.exists():
            tmp_bundle.unlink()
        with zipfile.ZipFile(tmp_bundle, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(self.run_dir.rglob("*")):
                if path.is_file():
                    archive.write(path, arcname=path.relative_to(self.run_dir.parent).as_posix())
        os.replace(tmp_bundle, bundle_path)
        return bundle_path
