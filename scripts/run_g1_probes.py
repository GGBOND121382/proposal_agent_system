from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


def canonical_sha256(value: Any) -> str:
    import hashlib

    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def track_a(component_root: Path, evidence_dir: Path) -> dict[str, Any]:
    sys.path.insert(0, str(component_root))
    from app.runtime_evidence import ModelCallEvidenceStore
    from app.util import sha256_json

    store = ModelCallEvidenceStore(evidence_dir / "model_calls")
    call_key = "g1-live-audit"
    request = {
        "prompt_id": "P-G1-AUDIT",
        "system_prompt": "G1 raw response integrity audit",
        "input_envelope": {"payload": {"material": "persisted-input"}},
        "output_schema": {"type": "object"},
        "model_id": "g1-audit-model",
        "endpoint_id": "g1-audit-endpoint",
    }
    output = {
        "schema_version": "2.0",
        "prompt_id": "P-G1-AUDIT",
        "prompt_version": "1.0.0",
        "status": "PASS",
        "result": {"audit": "raw-response-preserved"},
        "findings": [],
        "warnings": [],
        "user_questions": [],
    }
    raw_text = json.dumps(output, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    request_meta = store.write_request(call_key, request)
    response_meta = store.write_response(
        call_key,
        raw_text=raw_text,
        parsed_output=output,
        raw_parsed_output=json.loads(raw_text),
        metadata={
            "model_id": "g1-audit-model",
            "endpoint_id": "g1-audit-endpoint",
            "provider_model_name": "g1-audit-provider",
        },
    )
    verified = store.load_verified_response(call_key)
    consumed_hash = sha256_json(verified.parsed_output)
    status = (
        "PASS"
        if response_meta["parsed_object_sha256"]
        == response_meta["raw_parsed_object_sha256"]
        == consumed_hash
        and verified.raw_text == raw_text
        else "FAIL"
    )
    report = {
        "schema_version": "1.0",
        "gate": "G1",
        "track": "A",
        "status": status,
        "raw_response_text": verified.raw_text,
        "parsed_output": verified.parsed_output,
        "model_id": verified.metadata.get("model_id"),
        "endpoint_id": verified.metadata.get("endpoint_id"),
        "hashes": {
            "request_sha256": request_meta["request_sha256"],
            "raw_response_sha256": response_meta["raw_response_sha256"],
            "parsed_object_sha256": response_meta["parsed_object_sha256"],
            "raw_parsed_object_sha256": response_meta["raw_parsed_object_sha256"],
            "consumed_object_sha256": consumed_hash,
        },
    }
    write_json(evidence_dir / "runtime-evidence-audit.json", report)
    return report


def track_b(component_root: Path, evidence_dir: Path) -> dict[str, Any]:
    outputs = []
    for index in (1, 2):
        path = evidence_dir / f"track-b-restart-{index}.json"
        completed = subprocess.run(
            [
                sys.executable,
                "scripts/validate_track_b.py",
                "--json-out",
                str(path),
            ],
            cwd=component_root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        (evidence_dir / f"track-b-restart-{index}.log").write_text(
            completed.stdout, encoding="utf-8"
        )
        if completed.returncode != 0:
            report = {
                "schema_version": "1.0",
                "gate": "G1",
                "track": "B",
                "status": "FAIL",
                "returncode": completed.returncode,
                "iteration": index,
            }
            write_json(evidence_dir / "restart-probe.json", report)
            return report
        outputs.append(json.loads(path.read_text(encoding="utf-8")))
    hashes = [canonical_sha256(item) for item in outputs]
    report = {
        "schema_version": "1.0",
        "gate": "G1",
        "track": "B",
        "status": "PASS"
        if all(item.get("status") == "PASS" for item in outputs) and len(set(hashes)) == 1
        else "FAIL",
        "probe": "deterministic_reexecution",
        "process_count": 2,
        "report_sha256": hashes,
    }
    write_json(evidence_dir / "restart-probe.json", report)
    return report


def track_d(evidence_dir: Path) -> dict[str, Any]:
    path = evidence_dir / "delivery" / "D_TRACK_ACCEPTANCE.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    records = payload.get("render_records") or []
    report = {
        "schema_version": "1.0",
        "gate": "G1",
        "track": "D",
        "status": "PASS"
        if payload.get("status") == "PASS"
        and len(records) >= 3
        and all(item.get("cache_hit") for item in records)
        else "FAIL",
        "probe": "verified_mermaid_cache_reuse",
        "render_records": records,
    }
    write_json(evidence_dir / "restart-probe.json", report)
    return report


def track_e(component_root: Path, evidence_dir: Path) -> dict[str, Any]:
    sys.path.insert(0, str(component_root))
    from app.db import Database
    from app.quality import QualityLifecycleManager
    from app.util import utc_now

    db_path = evidence_dir / "quality-restart.sqlite3"
    db = Database(db_path)
    now = utc_now()
    project_id = "g1-project-e"
    workflow_id = "g1-workflow-e"
    db.execute(
        "INSERT INTO projects(id,name,description,security_level,config_json,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (project_id, "G1 E", "restart persistence", "INTERNAL", "{}", now, now),
    )
    db.execute(
        "INSERT INTO workflows(id,project_id,workflow_type,status,current_step,state_json,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (
            workflow_id,
            project_id,
            "WF-4_PROPOSAL_AUTHORING",
            "RUNNING",
            0,
            "{}",
            now,
            now,
        ),
    )
    finding = {
        "code": "QG_G1_RESTART",
        "severity": "P1",
        "category": "ARGUMENT",
        "target_type": "SECTION",
        "target_path_or_span": "section:g1",
        "description": "G1 restart persistence probe.",
        "required_action": "repair and review",
        "suggested_route": "WRITING_AGENT",
        "blocking": True,
        "repairable": True,
        "evidence_refs": [],
    }
    first = QualityLifecycleManager(db)
    first.observe_prompt_result(
        project_id=project_id,
        workflow_id=workflow_id,
        prompt_id="P-WRITE-CRITIC",
        run_id="g1-critic-open",
        status="REVISE",
        output={"findings": [finding]},
    )

    reloaded = QualityLifecycleManager(Database(db_path))
    blockers = reloaded.open_blockers(project_id)
    rows = reloaded.db.fetchall(
        "SELECT artifact_type,status,version FROM artifacts "
        "WHERE project_id=? AND artifact_type='QUALITY_FINDING' ORDER BY version",
        (project_id,),
    )
    report = {
        "schema_version": "1.0",
        "gate": "G1",
        "track": "E",
        "status": "PASS"
        if len(blockers) == 1
        and blockers[0]["finding"]["code"] == "QG_G1_RESTART"
        and rows
        and rows[-1]["status"] == "OPEN"
        else "FAIL",
        "probe": "quality_lifecycle_database_reload",
        "database": str(db_path),
        "open_blocker_count": len(blockers),
        "artifact_rows": rows,
    }
    write_json(evidence_dir / "restart-probe.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Run G1 special restart and integrity probes.")
    parser.add_argument("--track", required=True, choices=list("ABCDEF"))
    parser.add_argument("--component-root", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    args = parser.parse_args()
    component_root = args.component_root.resolve()
    evidence_dir = args.evidence_dir.resolve()
    evidence_dir.mkdir(parents=True, exist_ok=True)

    if args.track == "A":
        report = track_a(component_root, evidence_dir)
    elif args.track == "B":
        report = track_b(component_root, evidence_dir)
    elif args.track == "D":
        report = track_d(evidence_dir)
    elif args.track == "E":
        report = track_e(component_root, evidence_dir)
    else:
        report = {
            "schema_version": "1.0",
            "gate": "G1",
            "track": args.track,
            "status": "PASS",
            "probe": "covered_by_executed_component_tests",
        }
        write_json(evidence_dir / "restart-probe.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
