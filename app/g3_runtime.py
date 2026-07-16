from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .config import Settings
from .context import ContextBuilder
from .db import Database
from .executor import PromptExecutor
from .util import sha256_json, utc_now, write_json
from .workflows import WorkflowEngine
from scripts.run_full_proposal_concurrent_acceptance import SECTION_TITLES, _add_materials, _create_project, _material_manifest


def _prepare_project(settings: Settings, db: Database) -> tuple[str, dict[str, Any]]:
    project_id = _create_project(db)
    _add_materials(settings, db, project_id)
    row = db.fetchone("SELECT config_json FROM projects WHERE id=?", (project_id,))
    config = json.loads(row["config_json"])
    config.update({
        "internet_access_allowed": True,
        "anonymized_external_processing_allowed": True,
        "allowed_public_topics": [
            "dynamic vehicle routing", "vehicle routing with time windows",
            "disruption management", "incremental optimization", "multi-agent planning",
        ],
        "prohibited_external_fields": [],
        "recipient_scope": ["G3 formal capability acceptance"],
        "allowed_model_endpoint_ids": ["offline-primary", "online-public-primary"],
        "require_public_research": True,
        "task_instruction": "Use the complete supplied material set, live public research, and explicit UNKNOWN markers for unsupported facts.",
    })
    db.execute(
        "UPDATE projects SET name=?,description=?,security_level=?,config_json=?,updated_at=? WHERE id=?",
        (
            "G3 formal capability acceptance project",
            "Complete-material LIVE capability validation; no simulated semantic response is permitted.",
            "INTERNAL", json.dumps(config, ensure_ascii=False), utc_now(), project_id,
        ),
    )
    manifest = _material_manifest(db, project_id)
    manifest["section_titles"] = SECTION_TITLES
    return project_id, manifest


def _decide_open_gate(engine: WorkflowEngine, workflow_id: str) -> dict[str, Any]:
    open_gates = [item for item in engine.list_gates(workflow_id=workflow_id) if item["status"] == "OPEN"]
    if not open_gates:
        raise RuntimeError(f"Workflow {workflow_id} is waiting without an open gate")
    gate = open_gates[0]
    action = "APPROVE" if "APPROVE" in gate["allowed_actions"] else "CONFIRM"
    return engine.decide_gate(
        gate["id"],
        action=action,
        decided_by=os.environ["G3_OPERATOR_ID"],
        decided_role=gate["required_role"],
        comment="G3 公开固定材料正式能力验收；只确认流程节点，不修改模型正文。",
        answers=[],
        context_hash=gate["context_hash"],
    )


async def _finish(engine: WorkflowEngine, project_id: str, workflow_type: str, options: dict[str, Any] | None = None) -> dict[str, Any]:
    workflow = engine.start(project_id, workflow_type, {**(options or {}), "idempotency_key": f"g3-{workflow_type}"})
    for _ in range(2400):
        workflow = await engine.advance(workflow["id"])
        if workflow["status"] == "WAITING_GATE":
            _decide_open_gate(engine, workflow["id"])
            continue
        if workflow["status"] in {"COMPLETED", "BLOCKED", "CANCELLED"}:
            return workflow
    raise RuntimeError(f"Workflow {workflow_type} exceeded advance limit")


async def _cross_chapter_reviews(
    *, engine: WorkflowEngine, builder: ContextBuilder, executor: PromptExecutor,
    parent: dict[str, Any], max_rounds: int = 2,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    history: list[dict[str, Any]] = []
    for round_index in range(1, max_rounds + 1):
        parent = engine.get(parent["id"])
        state = parent["state"]
        base = builder.build(
            "P-INTEGRATION-CRITIC", parent["project_id"], workflow_id=parent["id"], workflow_state=state
        )
        candidates = list((base.get("payload") or {}).get("candidate_sections") or [])
        section_map = list((base.get("payload") or {}).get("document_section_map") or [])
        by_id = {str(item.get("section_id")): item for item in candidates}
        ordered_ids = [str(item.get("section_id")) for item in section_map]
        final_round: list[dict[str, Any]] = []
        repair_scheduled = False
        for index in range(0, len(ordered_ids), 3):
            window_ids = ordered_ids[index:index + 3]
            window_candidates = [by_id[section_id] for section_id in window_ids]
            window_map = [item for item in section_map if str(item.get("section_id")) in set(window_ids)]
            envelope = builder.build(
                "P-INTEGRATION-CRITIC",
                parent["project_id"],
                workflow_id=parent["id"],
                workflow_state=state,
                overrides={
                    "payload.candidate_sections": window_candidates,
                    "payload.document_section_map": window_map,
                    "scope.target_object_ids": window_ids,
                },
            )
            result = await executor.execute(
                "P-INTEGRATION-CRITIC", envelope, project_id=parent["project_id"], workflow_id=parent["id"],
                original_environment=state.get("original_environment"),
            )
            engine._observe_quality_result(parent, state, "P-INTEGRATION-CRITIC", result)
            record = {
                "round": round_index,
                "window_index": index // 3 + 1,
                "section_ids": window_ids,
                "run_id": result["run_id"],
                "status": result["status"],
                "input_hash": sha256_json(envelope),
                "output_hash": sha256_json(result["output"]),
                "model_id": result["route"]["model_id"],
                "endpoint_id": result["route"]["endpoint_id"],
                "finding_codes": [str(item.get("code") or "") for item in result["output"].get("findings") or []],
            }
            history.append(record)
            final_round.append(record)
            if result["status"] != "PASS":
                action = engine._prepare_integration_repair(parent, state, result["output"])
                if action != "SCHEDULED":
                    raise RuntimeError(
                        f"Cross-chapter review window {record['window_index']} returned {result['status']} and could not schedule repair: {action}"
                    )
                repair_scheduled = True
                break
        if not repair_scheduled:
            return parent, final_round, history
        for _ in range(1800):
            parent = await engine.advance(parent["id"])
            if parent["status"] == "WAITING_GATE":
                gate = next(item for item in engine.list_gates(workflow_id=parent["id"]) if item["status"] == "OPEN")
                if gate["gate_type"] == "CANDIDATE_REVIEW":
                    break
                _decide_open_gate(engine, parent["id"])
            elif parent["status"] in {"BLOCKED", "CANCELLED"}:
                raise RuntimeError(parent["state"].get("last_error") or parent["status"])
        else:
            raise RuntimeError("Cross-chapter repair did not return to candidate review gate")
    raise RuntimeError("Cross-chapter reviews did not pass after bounded repair rounds")


async def _author_with_cross_reviews(engine: WorkflowEngine, builder: ContextBuilder, executor: PromptExecutor, project_id: str) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    parent = engine.start(
        project_id,
        "WF-4_PROPOSAL_AUTHORING",
        {
            "idempotency_key": "g3-WF-4",
            "full_proposal_concurrent": True,
            "integration_scope": "G3_FULL_PROPOSAL_LIVE",
            "require_public_research": True,
        },
    )
    for _ in range(2400):
        parent = await engine.advance(parent["id"])
        if parent["status"] == "WAITING_GATE":
            gate = next(item for item in engine.list_gates(workflow_id=parent["id"]) if item["status"] == "OPEN")
            if gate["gate_type"] == "CANDIDATE_REVIEW":
                parent, final_windows, history = await _cross_chapter_reviews(
                    engine=engine, builder=builder, executor=executor, parent=parent
                )
                _decide_open_gate(engine, parent["id"])
                break
            _decide_open_gate(engine, parent["id"])
            continue
        if parent["status"] in {"BLOCKED", "CANCELLED"}:
            raise RuntimeError(parent["state"].get("last_error") or parent["status"])
    else:
        raise RuntimeError("Authoring did not reach candidate review")

    for _ in range(2400):
        parent = await engine.advance(parent["id"])
        if parent["status"] == "WAITING_GATE":
            _decide_open_gate(engine, parent["id"])
            continue
        if parent["status"] == "COMPLETED":
            return parent, final_windows, history
        if parent["status"] in {"BLOCKED", "CANCELLED"}:
            raise RuntimeError(parent["state"].get("last_error") or parent["status"])
    raise RuntimeError("Authoring did not complete")


def _export_inputs_and_runs(db: Database, project_id: str, output_dir: Path) -> None:
    requests = output_dir / "requests"
    responses = output_dir / "responses"
    traces = output_dir / "prompt_traces"
    for path in (requests, responses, traces):
        path.mkdir(parents=True, exist_ok=True)
    rows = db.fetchall("SELECT * FROM prompt_runs WHERE project_id=? ORDER BY created_at,id", (project_id,))
    trace_rows = []
    for row in rows:
        run_id = str(row["id"])
        write_json(requests / f"{run_id}.json", json.loads(row["input_json"]))
        if row.get("output_json"):
            write_json(responses / f"{run_id}.json", json.loads(row["output_json"]))
        trace_rows.append({key: row.get(key) for key in (
            "id", "workflow_id", "prompt_id", "status", "model_id", "endpoint_id",
            "input_hash", "output_hash", "duration_ms", "created_at",
        )})
    with (traces / "prompt_runs.jsonl").open("w", encoding="utf-8") as handle:
        for row in trace_rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
