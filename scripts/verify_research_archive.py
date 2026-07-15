from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.skills.research_audit import verify_research_archive
from app.util import write_json


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Recompute every archived public-research hash and verify the session manifest."
    )
    parser.add_argument("manifest", type=Path, help="Path to research_archive/.../manifest.json")
    parser.add_argument("--json-out", type=Path, help="Optional path for the verification report")
    args = parser.parse_args()

    report = verify_research_archive(args.manifest)
    if args.json_out:
        write_json(args.json_out, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
