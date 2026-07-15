from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.skills.public_research import PublicResearchArchiveError, PublicResearchArchiveSkill


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a public-research archive and its hashes.")
    parser.add_argument("archive_root", type=Path)
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()
    try:
        report = PublicResearchArchiveSkill.verify_archive(args.archive_root)
    except PublicResearchArchiveError as exc:
        report = {
            "schema_version": "1.0",
            "archive_root": str(args.archive_root),
            "status": "BLOCK",
            "findings": [{"code": "ARCHIVE_OPEN_FAILED", "message": str(exc)}],
        }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(text, encoding="utf-8")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
