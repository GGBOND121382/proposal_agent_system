from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from app.config import Settings
from app.context import ContextBuilder
from app.db import Database
from app.diagram_enrichment import DiagramEnrichmentService
from app.executor import PromptExecutor
from app.human_gate_bridge import FileHumanGateBridge
from app.llm import ModelGateway
from app.pack import PromptPack
from app.research import PublicResearchService
from app.security import SecurityRouter
from app.skill_setup import build_skill_executor
from app.track_b import TrackBAgentPromptValidator
from app.workflows import WorkflowEngine


def build_runtime() -> tuple[Settings, Database, WorkflowEngine]:
    settings = Settings.load()
    pack = PromptPack(settings.prompt_pack_dir)
    db = Database(settings.db_path)
    router = SecurityRouter(pack)
    gateway = ModelGateway(settings, pack)
    builder = ContextBuilder(db, pack)
    executor = PromptExecutor(
        db,
        pack,
        router,
        gateway,
        quality_guard=TrackBAgentPromptValidator(pack),
        quality_guard_enabled=settings.proposal_quality_guard_enabled,
    )
    skills = build_skill_executor(db, settings)
    research = PublicResearchService(settings, skills)
    diagrams = DiagramEnrichmentService(db, pack, skills)
    engine = WorkflowEngine(db, pack, builder, executor, research, diagrams)
    return settings, db, engine


def _open_gates(engine: WorkflowEngine, project_id: str) -> list[dict[str, Any]]:
    return [
        gate
        for gate in engine.list_gates(project_id=project_id)
        if gate.get("status") == "OPEN"
    ]


async def drive_workflow(
    engine: WorkflowEngine,
    gate_bridge: FileHumanGateBridge,
    workflow_id: str,
) -> dict[str, Any]:
    while True:
        workflow = engine.get(workflow_id)
        if workflow["status"] in {"COMPLETED", "CANCELLED"}:
            return workflow
        if workflow["status"] == "BLOCKED":
            # RecoverableWorkflowEngine decides whether a BLOCKED state can resume.
            advanced = await engine.advance(workflow_id)
            if advanced["status"] == "BLOCKED":
                raise RuntimeError(
                    f"Workflow blocked: {workflow_id}: "
                    f"{advanced['state'].get('last_error') or 'unknown error'}"
                )
            continue

        gates = _open_gates(engine, workflow["project_id"])
        if gates:
            # Full-proposal authoring may expose child-workflow gates.  Publish and
            # consume every open gate one at a time; never auto-approve.
            gate = sorted(gates, key=lambda item: (item["created_at"], item["id"]))[0]
            await gate_bridge.wait_and_apply(engine, gate)
            continue

        await engine.advance(workflow_id)


async def main_async(args: argparse.Namespace) -> int:
    settings, _db, engine = build_runtime()
    bridge = FileHumanGateBridge.from_settings(settings)
    options = json.loads(args.options_json)
    options.setdefault("idempotency_key", args.idempotency_key)
    workflow = engine.start(args.project_id, args.workflow_type, options)
    completed = await drive_workflow(engine, bridge, workflow["id"])
    print(json.dumps(completed, ensure_ascii=False, indent=2))
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one proposal workflow with an auditable model and human file bridge."
    )
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--workflow-type", required=True)
    parser.add_argument("--idempotency-key", required=True)
    parser.add_argument("--options-json", default="{}")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async(parse_args())))
