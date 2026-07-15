from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from audit_prompt_traces import audit
from validate_f import validate


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=ROOT, check=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


def snapshot_sqlite(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(source)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()


def material_manifest(destination: Path) -> dict[str, Any]:
    result = subprocess.run(
        ["git", "ls-files", "--", "prompt_pack/replay", "tests", "governance/f"],
        cwd=ROOT, check=True, text=True, stdout=subprocess.PIPE,
    )
    files = []
    for relative in sorted(line for line in result.stdout.splitlines() if line):
        path = ROOT / relative
        files.append({"path": relative, "size": path.stat().st_size, "sha256": sha256_file(path)})
    payload = {"version": "1.0", "file_count": len(files), "files": files}
    destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def build(database: Path, output: Path, test_logs: list[Path]) -> dict[str, Any]:
    validation = validate()
    if validation["status"] != "PASS":
        raise RuntimeError("F manifest validation failed: " + "; ".join(validation["errors"]))
    source_commit = git("rev-parse", "HEAD")

    with tempfile.TemporaryDirectory(prefix="f-recovery-") as temp:
        stage = Path(temp) / "bundle"
        for relative in (
            "source", "requests", "responses", "prompt_traces", "research_archive",
            "mermaid_artifacts", "exports", "test_logs",
        ):
            (stage / relative).mkdir(parents=True, exist_ok=True)

        (stage / "source_commit.txt").write_text(source_commit + "\n", encoding="utf-8")
        environment = {
            "python": sys.version,
            "platform": platform.platform(),
            "runtime_mode": os.getenv("MODEL_RUNTIME_MODE", "UNKNOWN"),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        (stage / "environment_manifest.json").write_text(
            json.dumps(environment, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        materials = material_manifest(stage / "input_material_manifest.json")

        source_archive = stage / "source" / "source.tar.gz"
        with source_archive.open("wb") as stream:
            subprocess.run(
                ["git", "archive", "--format=tar.gz", source_commit],
                cwd=ROOT, check=True, stdout=stream,
            )

        checkpoint = stage / "workflow_checkpoint.sqlite"
        snapshot_sqlite(database, checkpoint)
        trace_report = audit(checkpoint, stage / "prompt_traces", allow_empty=False)
        if trace_report["status"] != "PASS":
            raise RuntimeError("Trace audit failed: " + "; ".join(trace_report["errors"]))

        shutil.copy2(stage / "prompt_traces" / "calls.jsonl", stage / "requests" / "calls.jsonl")
        response_rows = []
        for line in (stage / "prompt_traces" / "calls.jsonl").read_text(encoding="utf-8").splitlines():
            item = json.loads(line)
            response_rows.append({
                "run_id": item["run_id"],
                "prompt_id": item["prompt_id"],
                "raw_response_text": item["raw_response_text"],
                "parsed_output": item["parsed_output"],
                "output_hash": item["output_hash"],
            })
        with (stage / "responses" / "calls.jsonl").open("w", encoding="utf-8") as stream:
            for item in response_rows:
                stream.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")

        markers = {
            "requests/README.txt": "Exact model inputs are retained in calls.jsonl.\n",
            "responses/README.txt": "Raw and parsed model outputs are retained in calls.jsonl.\n",
            "research_archive/README.txt": "Research artifacts are copied here when the stage includes public research.\n",
            "mermaid_artifacts/README.txt": "Mermaid source and rendered artifacts are copied here when present.\n",
            "exports/README.txt": "DOCX/PDF exports are copied here when present.\n",
            "test_logs/README.txt": "CI test logs copied into this directory are part of the evidence package.\n",
        }
        for relative, text in markers.items():
            (stage / relative).write_text(text, encoding="utf-8")
        for log in test_logs:
            if log.is_file():
                shutil.copy2(log, stage / "test_logs" / log.name)

        acceptance = f"""# F 轨道阶段验收报告

- Source commit: `{source_commit}`
- Manifest validation: `{validation['status']}`
- Prompt count: `{validation['counts']['prompts']}`
- Replay count: `{validation['counts']['replay_cases']}`
- Audited prompt calls: `{trace_report['evidence_count']}`
- Trace audit: `{trace_report['status']}`
- Material files: `{materials['file_count']}`

该包保存源码、环境、材料清单、SQLite 一致性快照、逐调用请求/响应、Trace 和测试日志。SIMULATED/REPLAY 只证明编排、Schema、审计和恢复链路，不作为真实模型语义能力证明。
"""
        (stage / "acceptance_report.md").write_text(acceptance, encoding="utf-8")

        files: dict[str, dict[str, Any]] = {}
        for path in sorted(stage.rglob("*")):
            if path.is_file() and path.name != "manifest.json":
                relative = path.relative_to(stage).as_posix()
                files[relative] = {"size": path.stat().st_size, "sha256": sha256_file(path)}
        manifest = {
            "gate": "F", "format_version": "1.0", "source_commit": source_commit,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "trace_count": trace_report["evidence_count"], "files": files,
        }
        (stage / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

        output.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(stage.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(stage).as_posix())

    return {
        "status": "PASS", "bundle": str(output), "source_commit": source_commit,
        "trace_count": trace_report["evidence_count"], "size": output.stat().st_size,
        "sha256": sha256_file(output),
    }


def verify(bundle: Path, extract_dir: Path | None = None) -> dict[str, Any]:
    spec = json.loads((ROOT / "governance" / "f" / "test_evidence_manifest.json").read_text(encoding="utf-8"))
    errors: list[str] = []
    temporary = tempfile.TemporaryDirectory(prefix="f-verify-") if extract_dir is None else None
    destination = Path(temporary.name) if temporary else extract_dir
    assert destination is not None
    if destination.exists() and not temporary:
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(bundle) as archive:
            root = destination.resolve()
            for info in archive.infolist():
                target = (destination / info.filename).resolve()
                if target != root and root not in target.parents:
                    errors.append(f"UNSAFE_PATH {info.filename}")
            if not errors:
                archive.extractall(destination)
        manifest_path = destination / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
        if not manifest:
            errors.append("MANIFEST_MISSING")
        for relative in spec["recovery_bundle"]["required_paths"]:
            if not (destination / relative).is_file():
                errors.append(f"REQUIRED_PATH_MISSING {relative}")
        for relative, metadata in (manifest.get("files") or {}).items():
            path = destination / relative
            if not path.is_file():
                errors.append(f"FILE_MISSING {relative}")
            elif path.stat().st_size != int(metadata["size"]):
                errors.append(f"SIZE_MISMATCH {relative}")
            elif sha256_file(path) != metadata["sha256"]:
                errors.append(f"HASH_MISMATCH {relative}")
        checkpoint = destination / "workflow_checkpoint.sqlite"
        if checkpoint.is_file():
            connection = sqlite3.connect(checkpoint)
            try:
                row = connection.execute("PRAGMA integrity_check").fetchone()
                if not row or row[0] != "ok":
                    errors.append("SQLITE_INTEGRITY_FAILED")
            finally:
                connection.close()
        return {
            "gate": "F5", "status": "PASS" if not errors else "FAIL",
            "bundle": str(bundle), "source_commit": manifest.get("source_commit"),
            "trace_count": manifest.get("trace_count"), "errors": errors,
        }
    finally:
        if temporary:
            temporary.cleanup()


def main() -> int:
    parser = argparse.ArgumentParser(description="Build or verify an F-stage recovery bundle.")
    sub = parser.add_subparsers(dest="command", required=True)
    build_parser = sub.add_parser("build")
    build_parser.add_argument("--database", type=Path, required=True)
    build_parser.add_argument("--output", type=Path, required=True)
    build_parser.add_argument("--test-log", type=Path, action="append", default=[])
    verify_parser = sub.add_parser("verify")
    verify_parser.add_argument("bundle", type=Path)
    verify_parser.add_argument("--extract-dir", type=Path)
    verify_parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.command == "build":
        report = build(args.database.resolve(), args.output.resolve(), [p.resolve() for p in args.test_log])
    else:
        report = verify(args.bundle.resolve(), args.extract_dir.resolve() if args.extract_dir else None)
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
