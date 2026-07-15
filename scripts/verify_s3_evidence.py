from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.s3_evidence import verify_s3_evidence
from app.util import write_json


def main() -> None:
    parser = argparse.ArgumentParser(description="Re-verify a persisted S3 Research + Mermaid + Export evidence set.")
    parser.add_argument("acceptance", type=Path, help="Path to S3_ACCEPTANCE.json")
    parser.add_argument("--data-dir", type=Path, required=True, help="APP_DATA_DIR that owns artifact:// references")
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()
    report = verify_s3_evidence(args.acceptance.resolve(), args.data_dir.resolve())
    if args.json_out:
        write_json(args.json_out.resolve(), report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
