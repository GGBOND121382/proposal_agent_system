from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from app.config import Settings
from app.db import Database
from app.util import utc_now


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Cancel the latest active WF-3 and rebuild it without executing it."
    )
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--service-url", default="http://127.0.0.1:8080")
    args = parser.parse_args()

    db = Database(Settings.load().db_path)
    source = db.fetchone(
        """SELECT * FROM workflows
             WHERE project_id=? AND workflow_type='WF-3_HYBRID_ONLINE_ASSIST'
               AND status NOT IN ('COMPLETED','CANCELLED')
             ORDER BY created_at DESC LIMIT 1""",
        (args.project_id,),
    )
    if source is None:
        parser.error(f"no active WF-3 workflow found for project {args.project_id}")

    state = json.loads(source["state_json"])
    options = dict(state.get("options") or {})
    options["idempotency_key"] = (
        "wf3-manual-rebuild-"
        + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    )

    with db.transaction() as tx:
        current = tx.fetchone("SELECT * FROM workflows WHERE id=?", (source["id"],))
        if current is None or current["updated_at"] != source["updated_at"]:
            raise RuntimeError("source workflow changed while rebuilding; run again")
        now = utc_now()
        cancelled_gates = tx.execute(
            """UPDATE gates
                  SET status='CANCELLED',decision_json=?,updated_at=?
                WHERE workflow_id=? AND status='OPEN'""",
            (
                json.dumps(
                    {"action": "CANCEL", "reason": "WF-3 manual rebuild"},
                    ensure_ascii=False,
                ),
                now,
                source["id"],
            ),
        ).rowcount
        state["cancelled_for_rebuild"] = {
            "cancelled_at": now,
            "reason": "Manual rebuild via scripts/rebuild_wf3.py",
        }
        tx.update_workflow(
            workflow_id=source["id"],
            status="CANCELLED",
            current_step=int(source["current_step"]),
            state=state,
            expected_updated_at=source["updated_at"],
        )
        tx.audit(
            "WORKFLOW_CANCELLED_FOR_REBUILD",
            project_id=args.project_id,
            object_id=source["id"],
            metadata={"cancelled_open_gates": cancelled_gates},
        )

    payload = json.dumps(
        {
            "project_id": args.project_id,
            "workflow_type": "WF-3_HYBRID_ONLINE_ASSIST",
            "options": options,
            "auto_advance": False,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        args.service_url.rstrip("/") + "/api/workflows",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        created = json.load(response)

    print(
        json.dumps(
            {
                "cancelled_workflow_id": source["id"],
                "cancelled_open_gates": cancelled_gates,
                "new_workflow_id": created["id"],
                "status": created["status"],
                "current_step": created["current_step"],
                "time_constraints": created.get("state", {})
                .get("options", {})
                .get("time_constraints"),
                "auto_advanced": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
