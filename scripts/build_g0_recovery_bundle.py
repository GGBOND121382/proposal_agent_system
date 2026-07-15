from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
G0_DIR = ROOT / "governance" / "g0"


def run(*args: str, cwd: Path = ROOT) -> str:
    result = subprocess.run(
        list(args),
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_sqlite(destination: Path) -> None:
    source = Path(os.getenv("APP_DATA_DIR", str(ROOT / "data"))).resolve() / "proposal_agents.sqlite3"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_file():
        source_connection = sqlite3.connect(source)
        destination_connection = sqlite3.connect(destination)
        try:
            source_connection.backup(destination_connection)
        finally:
            destination_connection.close()
            source_connection.close()
        return

    from app.db import Database

    Database(destination)


def export_prompt_traces(database_path: Path, destination: Path) -> int:
    destination.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    count = 0
    try:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='artifacts'"
        ).fetchone()
        with destination.open("w", encoding="utf-8") as stream:
            if table:
                rows = connection.execute(
                    """
                    SELECT id, project_id, workflow_id, prompt_id, version, status,
                           security_level, context_hash, content_json, created_at
                    FROM artifacts
                    WHERE artifact_type='PROMPT_TRACE'
                    ORDER BY created_at, id
                    """
                )
                for row in rows:
                    payload = dict(row)
                    try:
                        payload["content"] = json.loads(payload.pop("content_json"))
                    except json.JSONDecodeError:
                        payload["content"] = payload.pop("content_json")
                    stream.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
                    count += 1
    finally:
        connection.close()
    return count


def tracked_files(*pathspecs: str) -> list[str]:
    output = run("git", "ls-files", "--", *pathspecs)
    return [line for line in output.splitlines() if line]


def write_material_manifest(destination: Path) -> dict[str, Any]:
    files = tracked_files("prompt_pack/replay", "tests")
    entries = []
    for relative in files:
        path = ROOT / relative
        entries.append(
            {
                "path": relative,
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    payload = {
        "version": "1.0",
        "roots": ["prompt_pack/replay", "tests"],
        "file_count": len(entries),
        "files": entries,
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def write_restore_guide(destination: Path, source_commit: str, product_version: str) -> None:
    destination.write_text(
        f"""# G0 恢复说明

本恢复包由提交 `{source_commit}` 生成，对应产品版本 `{product_version}`。

## Linux / macOS / WSL

```bash
mkdir restored && cd restored
tar -xzf ../source/source.tar.gz
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
export APP_DATA_DIR=\"$PWD/runtime-data\"
mkdir -p \"$APP_DATA_DIR\"
cp ../sqlite/proposal_agents.sqlite3 \"$APP_DATA_DIR/proposal_agents.sqlite3\"
python scripts/validate_g0.py --skip-git-history
python -m compileall app
python prompt_pack/tools/validate_pack.py
python -m pytest -q
```

## Windows PowerShell

```powershell
New-Item -ItemType Directory restored | Out-Null
Set-Location restored
tar -xzf ..\source\source.tar.gz
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
$env:APP_DATA_DIR = (Join-Path (Get-Location) 'runtime-data')
New-Item -ItemType Directory -Force $env:APP_DATA_DIR | Out-Null
Copy-Item ..\sqlite\proposal_agents.sqlite3 (Join-Path $env:APP_DATA_DIR 'proposal_agents.sqlite3')
python scripts/validate_g0.py --skip-git-history
python -m compileall app
python prompt_pack/tools/validate_pack.py
python -m pytest -q
```

`--skip-git-history` 只用于从 `git archive` 提取、没有 `.git` 元数据的恢复目录；仓库分支和 CI 中必须执行完整 `python scripts/validate_g0.py`。
""",
        encoding="utf-8",
    )


def add_file_metadata(root: Path, relative: str) -> dict[str, Any]:
    path = root / relative
    return {"sha256": sha256_file(path), "size": path.stat().st_size}


def build_bundle(output_path: Path, *, root: Path = ROOT) -> dict[str, Any]:
    source_commit = run("git", "rev-parse", "HEAD", cwd=root)
    baseline = json.loads((root / "governance" / "g0" / "baseline.json").read_text(encoding="utf-8"))
    product_version = str(baseline["product_version"])

    subprocess.run(
        [sys.executable, str(root / "scripts" / "validate_g0.py"), "--root", str(root)],
        cwd=root,
        check=True,
    )

    with tempfile.TemporaryDirectory(prefix="g0-recovery-") as temp_dir:
        stage = Path(temp_dir) / "bundle"
        for relative in ("source", "dependencies", "sqlite", "trace", "manifests"):
            (stage / relative).mkdir(parents=True, exist_ok=True)

        source_archive = stage / "source" / "source.tar.gz"
        with source_archive.open("wb") as stream:
            subprocess.run(
                ["git", "-C", str(root), "archive", "--format=tar.gz", source_commit],
                check=True,
                stdout=stream,
            )

        shutil.copy2(root / "requirements.txt", stage / "dependencies" / "requirements.txt")
        shutil.copy2(root / "requirements-dev.txt", stage / "dependencies" / "requirements-dev.txt")

        database_snapshot = stage / "sqlite" / "proposal_agents.sqlite3"
        snapshot_sqlite(database_snapshot)
        trace_count = export_prompt_traces(database_snapshot, stage / "trace" / "prompt_traces.jsonl")

        contract_names = (
            "baseline.json",
            "interface_contract.json",
            "security_freeze.json",
            "layout.json",
        )
        for name in contract_names:
            shutil.copy2(root / "governance" / "g0" / name, stage / "manifests" / name)
        materials = write_material_manifest(stage / "manifests" / "materials_manifest.json")
        write_restore_guide(stage / "RESTORE.md", source_commit, product_version)

        files: dict[str, dict[str, Any]] = {}
        for path in sorted(stage.rglob("*")):
            if path.is_file():
                relative = path.relative_to(stage).as_posix()
                files[relative] = add_file_metadata(stage, relative)

        manifest = {
            "gate": "G0",
            "format_version": "1.0",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_commit": source_commit,
            "code_baseline_commit": baseline["code_baseline_commit"],
            "product_version": product_version,
            "python_runtime": baseline["python_runtime"],
            "material_file_count": materials["file_count"],
            "prompt_trace_count": trace_count,
            "files": files,
            "restore_validation": [
                "python scripts/validate_g0.py --skip-git-history",
                "python -m compileall app",
                "python prompt_pack/tools/validate_pack.py",
                "python -m pytest -q",
            ],
        }
        (stage / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(stage.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(stage).as_posix())

    return {
        "status": "PASS",
        "bundle": str(output_path),
        "source_commit": source_commit,
        "product_version": product_version,
        "prompt_trace_count": trace_count,
        "material_file_count": materials["file_count"],
        "sha256": sha256_file(output_path),
        "size": output_path.stat().st_size,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a self-verifying G0 recovery bundle.")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    root = args.root.resolve()
    commit = run("git", "rev-parse", "--short=12", "HEAD", cwd=root)
    output = args.output or (root / "recovery_evidence" / "g0" / commit / f"g0-recovery-{commit}.zip")
    if not output.is_absolute():
        output = root / output
    result = build_bundle(output.resolve(), root=root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
