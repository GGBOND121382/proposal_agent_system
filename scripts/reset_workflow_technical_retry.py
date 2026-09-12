from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from app.db import Database
from app.util import utc_now
from app.workflow_defs import WORKFLOWS
from app.workflows import technical_retry_key


def _state(row: dict[str, Any]) -> dict[str, Any]:
    value = json.loads(row.get("state_json") or "{}")
    return dict(value) if isinstance(value, dict) else {}


def build_plan(
    db: Database,
    workflow_id: str,
    *,
    expected_step: int | None = None,
    expected_section_id: str | None = None,
    expected_phase: str | None = None,
) -> dict[str, Any]:
    row = db.fetchone("SELECT * FROM workflows WHERE id=?", (workflow_id,))
    if row is None:
        raise ValueError(f"workflow not found: {workflow_id}")
    state = _state(row)
    steps = WORKFLOWS.get(str(row.get("workflow_type") or ""))
    if steps is None:
        raise ValueError(f"unknown workflow type: {row.get('workflow_type')}")
    current_step = int(row.get("current_step") or 0)
    if current_step >= len(steps):
        raise ValueError("workflow has no current executable step")
    step = steps[current_step]
    is_section_step = str(step.get("type") or "") == "WRITE_SECTIONS"
    retry_key = technical_retry_key(
        str(current_step), state, is_section_step=is_section_step
    )
    active_section_id = str(state.get("active_section_id") or "")
    progress = (state.get("section_progress") or {}).get(active_section_id) or {}
    active_phase = str(progress.get("phase") or "")
    retries = state.get("technical_retry_attempts") or {}
    current_count = int(retries.get(retry_key, 0))
    refusals: list[str] = []
    if expected_step is not None and current_step != expected_step:
        refusals.append(f"current_step changed: expected {expected_step}, got {current_step}")
    if expected_section_id is not None and active_section_id != expected_section_id:
        refusals.append(
            f"active_section_id changed: expected {expected_section_id}, got {active_section_id}"
        )
    if expected_phase is not None and active_phase != expected_phase:
        refusals.append(f"phase changed: expected {expected_phase}, got {active_phase}")
    if current_count <= 0:
        refusals.append(f"retry checkpoint {retry_key} is already empty")
    gate = db.fetchone(
        "SELECT id FROM gates WHERE workflow_id=? AND status='OPEN' LIMIT 1",
        (workflow_id,),
    )
    if gate is not None:
        refusals.append("workflow has an open gate")
    return {
        "workflow_id": workflow_id,
        "project_id": row["project_id"],
        "status": row["status"],
        "current_step": current_step,
        "active_section_id": active_section_id,
        "phase": active_phase,
        "retry_key": retry_key,
        "retry_count_before": current_count,
        "eligible": not refusals,
        "refusals": refusals,
        "expected_updated_at": row["updated_at"],
    }


def reset_checkpoint(
    database: Path,
    workflow_id: str,
    *,
    expected_step: int | None = None,
    expected_section_id: str | None = None,
    expected_phase: str | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    db = Database(database)
    plan = build_plan(
        db,
        workflow_id,
        expected_step=expected_step,
        expected_section_id=expected_section_id,
        expected_phase=expected_phase,
    )
    result = {**plan, "mode": "APPLY" if apply else "DRY_RUN", "applied": False}
    if not apply:
        return result
    if not plan["eligible"]:
        raise ValueError("checkpoint reset refused: " + "; ".join(plan["refusals"]))

    with db.transaction() as tx:
        row = tx.fetchone("SELECT * FROM workflows WHERE id=?", (workflow_id,))
        if row is None:
            raise ValueError(f"workflow not found: {workflow_id}")
        if row["updated_at"] != plan["expected_updated_at"]:
            raise RuntimeError("workflow changed after dry-run planning")
        state = _state(row)
        retries = state.setdefault("technical_retry_attempts", {})
        removed = int(retries.pop(plan["retry_key"], 0))
        if removed != plan["retry_count_before"]:
            raise RuntimeError("technical retry checkpoint changed concurrently")
        state.setdefault("technical_retry_reset_history", []).append(
            {
                "retry_key": plan["retry_key"],
                "removed_count": removed,
                "reason": "CODE_DEFECT_FIXED",
                "reset_at": utc_now(),
            }
        )
        tx.update_workflow(
            workflow_id=workflow_id,
            status=str(row["status"]),
            current_step=int(row["current_step"]),
            state=state,
            expected_updated_at=str(row["updated_at"]),
        )
        tx.audit(
            "TECHNICAL_RETRY_CHECKPOINT_RESET",
            project_id=str(row["project_id"]),
            object_id=workflow_id,
            metadata={
                "retry_key": plan["retry_key"],
                "removed_count": removed,
                "current_step": plan["current_step"],
                "active_section_id": plan["active_section_id"],
                "phase": plan["phase"],
                "reason": "CODE_DEFECT_FIXED",
            },
        )
    result["applied"] = True
    result["retry_count_after"] = 0
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--workflow-id", required=True)
    parser.add_argument("--expected-step", type=int)
    parser.add_argument("--expected-section-id")
    parser.add_argument("--expected-phase")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        result = reset_checkpoint(
            args.database,
            args.workflow_id,
            expected_step=args.expected_step,
            expected_section_id=args.expected_section_id,
            expected_phase=args.expected_phase,
            apply=args.apply,
        )
    except (ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
