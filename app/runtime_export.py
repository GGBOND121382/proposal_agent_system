from __future__ import annotations

import json
import os
from pathlib import Path

from .exporter import DocxExporter as BaseDocxExporter
from .runtime_evidence import FaultInjector
from .util import sha256_bytes, sha256_json, utc_now


class RecoverableDocxExporter(BaseDocxExporter):
    """Export wrapper that reuses a verified completed artifact after restart."""

    def __init__(self, db, settings):
        super().__init__(db, settings)
        data_dir = Path(getattr(settings, "data_dir", Path.cwd() / ".runtime-data"))
        self.runtime_faults = FaultInjector(data_dir / "model_calls")

    def _runtime_export_root(self) -> Path:
        data_dir = Path(getattr(self.settings, "data_dir", Path.cwd() / ".runtime-data"))
        root = Path(os.getenv("RUNTIME_EXPORT_EVIDENCE_DIR", str(data_dir / "runtime_exports")))
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _export_key(self, project_id: str, kind: str) -> str:
        candidates = self._candidate_runs(project_id)
        gates = self.db.fetchall(
            "SELECT id,gate_type,status,updated_at FROM gates WHERE project_id=? AND gate_type IN ('FINAL_CONTENT_SECURITY_APPROVAL','FINAL_EXPORT_APPROVAL') ORDER BY updated_at",
            (project_id,),
        )
        return "export-" + sha256_json(
            {
                "project_id": project_id,
                "kind": kind,
                "candidate_runs": [item.get("run_id") for item in candidates],
                "gates": gates,
            }
        )[:32]

    def _load(self, key: str) -> Path | None:
        manifest_path = self._runtime_export_root() / f"{key}.json"
        if not manifest_path.exists():
            return None
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        path = Path(manifest["path"])
        if not path.exists() or sha256_bytes(path.read_bytes()) != manifest.get("sha256"):
            return None
        return path

    def _save(self, key: str, path: Path) -> None:
        manifest_path = self._runtime_export_root() / f"{key}.json"
        temp = manifest_path.with_suffix(".tmp")
        temp.write_text(
            json.dumps(
                {"key": key, "path": str(path), "sha256": sha256_bytes(path.read_bytes()), "created_at": utc_now()},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        os.replace(temp, manifest_path)

    def export(self, project_id: str) -> Path:
        key = self._export_key(project_id, "docx")
        existing = self._load(key)
        if existing:
            return existing
        self.runtime_faults.hit("before_export", key)
        path = super().export(project_id)
        self._save(key, path)
        self.runtime_faults.hit("after_export", key)
        return path

    def export_package(self, project_id: str, document_path: Path | None = None) -> Path:
        key = self._export_key(project_id, "package")
        existing = self._load(key)
        if existing:
            return existing
        path = super().export_package(project_id, document_path)
        self._save(key, path)
        return path
