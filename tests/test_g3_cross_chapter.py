from __future__ import annotations

import asyncio

from tests.test_full_proposal_concurrent import (
    FULL_PROPOSAL_OPTIONS,
    FULL_PROPOSAL_TITLES,
    _prepare,
    _run_parent,
)
from tests.test_runtime import add_standard_materials, create_project


def test_g3_every_three_chapters_are_reviewed_before_full_integration(runtime):
    settings, _, db, _, _, _, engine, _ = runtime
    project_id = create_project(db, internet=False)
    add_standard_materials(
        settings,
        db,
        project_id,
        current_sections=FULL_PROPOSAL_TITLES,
    )
    options = {
        **FULL_PROPOSAL_OPTIONS,
        "g3_formal_acceptance": True,
        "cross_chapter_batch_size": 3,
    }

    async def scenario():
        await _prepare(engine, project_id)
        workflow = engine.start(project_id, "WF-4_PROPOSAL_AUTHORING", options)
        return await _run_parent(engine, workflow, max_steps=800)

    completed = asyncio.run(asyncio.wait_for(scenario(), timeout=150))
    assert completed["status"] == "COMPLETED", completed["state"].get("last_error")
    state = completed["state"]
    summary = state["g3_cross_chapter_reviews"]
    assert summary["status"] == "PASS"
    assert summary["batch_size"] == 3
    assert summary["batch_count"] == 5
    assert len(summary["latest_pass_run_ids"]) == 5
    assert len(set(summary["latest_pass_run_ids"])) == 5
    history = state["g3_cross_chapter_review_history"]
    latest = {item["batch_index"]: item for item in history if item["status"] == "PASS"}
    assert sorted(len(item["section_ids"]) for item in latest.values()) == [2, 3, 3, 3, 3]
    final_run = state["full_proposal_review_history"][-1]["run_id"]
    assert final_run not in summary["latest_pass_run_ids"]
