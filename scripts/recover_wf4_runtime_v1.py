from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.retry_policy import RetryPolicy
from app.runtime_failures import FailureCategory, classify_runtime_failure
from app.workflow_status import WorkflowStatus, is_recoverable_block


LEGACY_RUNTIME_KEYS = (
    "human_resolutions",
    "human_input_overrides",
    "repair_overrides",
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _latest_runtime_failure(
    conn: sqlite3.Connection,
    workflow_id: str,
) -> tuple[dict[str, Any] | None, str]:
    row = conn.execute(
        """SELECT content_json
             FROM artifacts
            WHERE workflow_id=? AND artifact_type='RUNTIME_FAILURE'
            ORDER BY version DESC, created_at DESC
            LIMIT 1""",
        (workflow_id,),
    ).fetchone()
    if row is None:
        return None, "FORMAL_CLASSIFIER_FALLBACK"
    payload = _json_object(row["content_json"])
    if not payload:
        return None, "FORMAL_CLASSIFIER_FALLBACK"
    return payload, "RUNTIME_FAILURE_ARTIFACT"


def _classification_payload(
    conn: sqlite3.Connection,
    workflow_id: str,
    state: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    persisted, source = _latest_runtime_failure(conn, workflow_id)
    if persisted is not None:
        return persisted, source
    fallback = classify_runtime_failure(
        RuntimeError(str(state.get("last_error") or ""))
    ).to_dict()
    return fallback, source


def _legacy_state_keys(state: dict[str, Any]) -> list[str]:
    return [key for key in LEGACY_RUNTIME_KEYS if state.get(key)]


def build_recovery_plan(
    conn: sqlite3.Connection,
    workflow_id: str,
) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM workflows WHERE id=? AND workflow_type='WF-4_PROPOSAL_AUTHORING'",
        (workflow_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"WF-4 workflow not found: {workflow_id}")

    state = _json_object(row["state_json"])
    old_status = str(row["status"])
    refusals: list[str] = []
    changes: list[str] = []

    legacy_keys = _legacy_state_keys(state)
    if legacy_keys:
        refusals.append(
            "runtime semantic migration is incomplete; legacy state keys remain: "
            + ", ".join(sorted(legacy_keys))
        )

    try:
        recoverable_status = is_recoverable_block(old_status)
    except ValueError:
        recoverable_status = False
        refusals.append(f"unknown workflow status: {old_status}")
    if not recoverable_status:
        refusals.append(
            f"workflow status {old_status} is not a recoverable blocked state"
        )

    classification, classification_source = _classification_payload(
        conn, workflow_id, state
    )
    category = str(classification.get("category") or "")
    retryable = bool(classification.get("retryable"))
    if category != FailureCategory.PROVIDER_TRANSIENT.value or not retryable:
        refusals.append(
            "latest formal failure classification is not a retryable provider failure"
        )

    prompt_id = str(
        ((state.get("step_results") or {}).get(str(row["current_step"])) or {}).get(
            "prompt_id"
        )
        or classification.get("prompt_id")
        or "UNKNOWN"
    )
    retry_key = f"{row['current_step']}:{prompt_id}"
    policy = RetryPolicy.from_options(state.get("options") or {})
    ledger = state.get("repair_ledger_v1") or {}
    historical_retries = int(
        (ledger.get("provider_retries") or {}).get(retry_key, 0)
    )

    eligible = not refusals
    new_status = (
        WorkflowStatus.WAITING_PROVIDER.value if eligible else old_status
    )
    if eligible:
        changes.append(
            f"{old_status} retryable provider failure -> {new_status}"
        )
        changes.append(
            "next advance starts a new finite same-node retry cycle under RetryPolicy"
        )

    return {
        "workflow_id": workflow_id,
        "eligible": eligible,
        "from_status": old_status,
        "to_status": new_status,
        "current_step": int(row["current_step"]),
        "prompt_id": prompt_id,
        "retry_key": retry_key,
        "historical_provider_retries": historical_retries,
        "retry_policy": {
            "max_retries": policy.max_retries,
            "max_attempts": policy.max_retries + 1,
            "base_delay_seconds": policy.base_delay_seconds,
            "max_delay_seconds": policy.max_delay_seconds,
        },
        "classification_source": classification_source,
        "classification": classification,
        "changes": changes,
        "refusals": refusals,
        "planned_at": now(),
        "state": state,
    }


def recover(database: Path, workflow_id: str, *, apply: bool) -> dict[str, Any]:
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    try:
        plan = build_recovery_plan(conn, workflow_id)
        result = {
            key: value for key, value in plan.items() if key != "state"
        }
        result["mode"] = "APPLY" if apply else "DRY_RUN"
        result["applied"] = False

        if apply and not plan["eligible"]:
            raise ValueError(
                "WF-4 recovery refused: " + "; ".join(plan["refusals"])
            )
        if not apply:
            return result

        state = dict(plan["state"])
        state["provider_wait"] = {
            "retry_key": plan["retry_key"],
            "completed_attempts": 0,
            "max_retries": plan["retry_policy"]["max_retries"],
            "max_attempts": plan["retry_policy"]["max_attempts"],
            "prompt_id": plan["prompt_id"],
            "recovered_from_status": plan["from_status"],
            "classification_source": plan["classification_source"],
            "new_retry_cycle": True,
        }
        state["wf4_recovery_v1"] = {
            "recovered_at": now(),
            "from_status": plan["from_status"],
            "to_status": plan["to_status"],
            "classification_source": plan["classification_source"],
            "changes": plan["changes"],
        }
        state.pop("runtime_recoverable", None)

        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            """UPDATE workflows
                  SET status=?, state_json=?, updated_at=?
                WHERE id=? AND status=?""",
            (
                plan["to_status"],
                json.dumps(state, ensure_ascii=False),
                now(),
                workflow_id,
                plan["from_status"],
            ),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            raise RuntimeError(
                "workflow status changed concurrently; recovery was not applied"
            )
        conn.commit()
        result["applied"] = True
        return result
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--workflow-id", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        result = recover(args.database, args.workflow_id, apply=args.apply)
    except (ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
