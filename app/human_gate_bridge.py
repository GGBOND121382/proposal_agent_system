from __future__ import annotations

from typing import Any


_MAX_SCOPE_WORKFLOWS = 256


def _workflow_links(state: dict[str, Any]) -> set[str]:
    """Return persisted parent/child workflow references from one workflow state.

    Portable traces need the same Gate scope as the full-proposal coordinator:
    the selected workflow, its parent, active/superseded group children, and any
    explicitly recorded child waits.  Only persisted workflow ids are followed;
    no project-wide heuristic search is performed.
    """

    links: set[str] = set()
    for key in (
        "parent_workflow_id",
        "quality_parent_workflow_id",
        "supersedes_workflow_id",
    ):
        value = str(state.get(key) or "").strip()
        if value:
            links.add(value)

    for key in ("authoring_child_workflow_ids", "waiting_on_child_workflow_ids"):
        for item in state.get(key) or []:
            value = str(item or "").strip()
            if value:
                links.add(value)

    children = state.get("full_proposal_children") or {}
    if isinstance(children, dict):
        for record in children.values():
            if not isinstance(record, dict):
                continue
            value = str(record.get("workflow_id") or "").strip()
            if value:
                links.add(value)
            for item in record.get("superseded_workflow_ids") or []:
                old_id = str(item or "").strip()
                if old_id:
                    links.add(old_id)
    return links


def workflow_gate_scope_ids(engine: Any, workflow_id: str) -> set[str]:
    """Return the exact persisted workflow family whose Gates belong in a trace.

    Missing historical references are retained in the returned id set so the
    snapshot query remains deterministic, but they are not expanded.  A bounded
    traversal prevents corrupt cyclic state from causing an unbounded walk.
    """

    root = str(workflow_id or "").strip()
    if not root:
        return set()
    scope: set[str] = set()
    pending = [root]
    while pending and len(scope) < _MAX_SCOPE_WORKFLOWS:
        current = pending.pop()
        if current in scope:
            continue
        scope.add(current)
        try:
            workflow = engine.get(current)
        except (KeyError, ValueError):
            continue
        state = workflow.get("state") or {}
        if not isinstance(state, dict):
            continue
        for linked in sorted(_workflow_links(state)):
            if linked not in scope:
                pending.append(linked)
    return scope
