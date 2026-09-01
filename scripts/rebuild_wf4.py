from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from app.config import Settings
from app.db import Database
from scripts._rebuild_compat import safe_rebuild_latest


def main() -> int:
    parser = argparse.ArgumentParser(description="Compatibility wrapper for standard workflow rebuild.")
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--service-url", default="http://127.0.0.1:8080")
    args = parser.parse_args()
    db = Database(Settings.load().db_path)
    try:
        result = safe_rebuild_latest(
            db,
            project_id=args.project_id,
            workflow_type="WF-4_PROPOSAL_AUTHORING",
            service_url=args.service_url,
        )
    except (ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
