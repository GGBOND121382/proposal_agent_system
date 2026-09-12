from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Resume a workflow's targeted repair from a previously committed critic run."
    )
    parser.add_argument("--workflow-id", required=True)
    parser.add_argument("--critic-run-id", required=True)
    parser.add_argument("--section-id", required=True)
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    load_dotenv(".env")

    from app.main import db, workflows

    run = db.fetchone(
        "SELECT * FROM prompt_runs WHERE id=? AND workflow_id=?",
        (args.critic_run_id, args.workflow_id),
    )
    if not run or not run.get("input_json") or not run.get("output_json"):
        raise RuntimeError("Committed critic run with input and output was not found")

    wf = workflows.get(args.workflow_id)
    state = wf["state"]
    if str(state.get("active_section_id") or "") != args.section_id:
        raise RuntimeError("Active section does not match the requested recovery section")

    critic_prompt = str(run["prompt_id"])
    critic_input = json.loads(run["input_json"])
    critic_output = json.loads(run["output_json"])
    progress = state["section_progress"][args.section_id]
    progress["status"] = "RUNNING"
    progress.pop("last_error", None)
    state.pop("last_error", None)
    workflows._update(wf, status="RUNNING", state=state)

    repaired = await workflows._auto_repair(
        wf,
        critic_prompt,
        critic_input,
        critic_output,
        state,
    )
    if not repaired:
        raise RuntimeError("Targeted repair did not produce a PASS result")

    workflows._append_section_run(
        progress,
        repaired,
        prompt_id="P-TARGETED-REPAIR",
        role="TARGETED_REPAIR_RECOVERY",
    )
    progress["status"] = "RUNNING"
    workflows._update(wf, status="RUNNING", state=state)
    db.audit(
        "TARGETED_REPAIR_RECOVERED_FROM_COMMITTED_CRITIC",
        project_id=wf["project_id"],
        object_id=repaired["run_id"],
        metadata={
            "workflow_id": args.workflow_id,
            "section_id": args.section_id,
            "critic_run_id": args.critic_run_id,
            "critic_prompt": critic_prompt,
        },
    )
    print(
        json.dumps(
            {
                "repair_run_id": repaired["run_id"],
                "status": repaired["status"],
                "critic_prompt": critic_prompt,
                "section_id": args.section_id,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
