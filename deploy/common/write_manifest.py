#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--output", default="SHA256SUMS.txt")
    parser.add_argument("--json-output", default="manifest.json")
    args = parser.parse_args()
    root = args.root.resolve()
    text_output = root / args.output
    json_output = root / args.json_output
    excluded = {text_output.resolve(), json_output.resolve()}
    records = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.resolve() in excluded:
            continue
        records.append({
            "path": path.relative_to(root).as_posix(),
            "sha256": sha256(path),
            "size_bytes": path.stat().st_size,
        })
    text_output.write_text("\n".join(f"{item['sha256']}  {item['path']}" for item in records) + "\n", encoding="utf-8")
    json_output.write_text(json.dumps({
        "schema_version": "1.0",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "file_count": len(records),
        "files": records,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(text_output)
    print(json_output)


if __name__ == "__main__":
    main()
