from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def json_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def audit(database: Path, output_dir: Path | None = None, *, allow_empty: bool = False) -> dict[str, Any]:
    registry = load_json(ROOT / "prompt_pack" / "config" / "prompt_registry.json")
    roles = {entry["prompt_id"]: entry["executor_role"] for entry in registry["prompts"]}
    contract = load_json(ROOT / "governance" / "g0" / "interface_contract.json")
    required = contract["artifact_interface"]["trace_payload_required_fields"]
    errors: list[str] = []
    evidence: list[dict[str, Any]] = []

    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        for table in ("prompt_runs", "artifacts"):
            if table not in tables:
                errors.append(f"TABLE_MISSING {table}")
        if errors:
            runs = []
            traces = []
        else:
            runs = list(connection.execute("SELECT rowid AS _rowid,* FROM prompt_runs ORDER BY created_at,_rowid"))
            traces = list(
                connection.execute(
                    """SELECT rowid AS _rowid,* FROM artifacts
                       WHERE artifact_type='PROMPT_TRACE' ORDER BY created_at,_rowid"""
                )
            )
    finally:
        connection.close()

    if not runs and not allow_empty:
        errors.append("NO_PROMPT_RUNS")

    unused = set(range(len(traces)))
    for run in runs:
        try:
            input_value = json.loads(run["input_json"])
            output_value = json.loads(run["output_json"]) if run["output_json"] else None
        except json.JSONDecodeError as exc:
            errors.append(f"RUN_JSON_INVALID {run['id']}: {exc}")
            continue

        match_index = None
        match_payload = None
        for index in sorted(unused):
            trace = traces[index]
            if (
                trace["project_id"] != run["project_id"]
                or trace["workflow_id"] != run["workflow_id"]
                or trace["prompt_id"] != run["prompt_id"]
            ):
                continue
            try:
                payload = json.loads(trace["content_json"])
            except json.JSONDecodeError:
                continue
            if json_hash(payload.get("input_envelope")) == run["input_hash"]:
                match_index = index
                match_payload = payload
                break
        if match_index is None or match_payload is None:
            errors.append(f"TRACE_MISSING {run['id']} {run['prompt_id']}")
            continue
        unused.remove(match_index)
        trace = traces[match_index]

        missing = [field for field in required if field not in match_payload]
        if missing:
            errors.append(f"TRACE_FIELD_MISSING {run['id']}: {','.join(missing)}")
        input_hash = json_hash(input_value)
        output_hash = json_hash(output_value) if output_value is not None else None
        checks = {
            "run_input_hash": run["input_hash"] == input_hash,
            "run_output_hash": run["output_hash"] == output_hash,
            "trace_context_hash": trace["context_hash"] == input_hash,
            "trace_input": match_payload.get("input_envelope") == input_value,
            "trace_output": match_payload.get("output") == output_value,
            "status": match_payload.get("status") == run["status"],
            "duration": match_payload.get("duration_ms") == run["duration_ms"],
            "model": match_payload.get("model_id") == run["model_id"],
            "endpoint": match_payload.get("endpoint_id") == run["endpoint_id"],
        }
        for name, passed in checks.items():
            if not passed:
                errors.append(f"TRACE_{name.upper()}_MISMATCH {run['id']}")
        if run["status"] != "ERROR" and not str(match_payload.get("raw_response_text") or "").strip():
            errors.append(f"RAW_RESPONSE_MISSING {run['id']}")
        if run["prompt_id"] not in roles:
            errors.append(f"RESPONSIBILITY_UNKNOWN {run['prompt_id']}")

        evidence.append(
            {
                "run_id": run["id"],
                "trace_artifact_id": trace["id"],
                "project_id": run["project_id"],
                "workflow_id": run["workflow_id"],
                "prompt_id": run["prompt_id"],
                "responsibility_agent": roles.get(run["prompt_id"]),
                "status": run["status"],
                "duration_ms": run["duration_ms"],
                "environment": match_payload.get("environment"),
                "model_id": run["model_id"],
                "endpoint_id": run["endpoint_id"],
                "input_hash": run["input_hash"],
                "output_hash": run["output_hash"],
                "input_envelope": input_value,
                "raw_response_text": match_payload.get("raw_response_text"),
                "parsed_output": output_value,
            }
        )

    for index in sorted(unused):
        trace = traces[index]
        errors.append(f"ORPHAN_TRACE {trace['id']} {trace['prompt_id']}")

    report = {
        "gate": "F4",
        "status": "PASS" if not errors else "FAIL",
        "database": str(database),
        "audited_at": datetime.now(timezone.utc).isoformat(),
        "run_count": len(runs),
        "trace_count": len(traces),
        "evidence_count": len(evidence),
        "errors": errors,
    }
    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)
        with (output_dir / "calls.jsonl").open("w", encoding="utf-8") as stream:
            for item in evidence:
                stream.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
        (output_dir / "trace_audit.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit PROMPT_RUN/PROMPT_TRACE evidence.")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--allow-empty", action="store_true")
    args = parser.parse_args()
    report = audit(
        args.database.resolve(),
        args.output_dir.resolve() if args.output_dir else None,
        allow_empty=args.allow_empty,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
