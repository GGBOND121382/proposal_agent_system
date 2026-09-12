from __future__ import annotations

import json
import urllib.error
import urllib.request


def safe_rebuild_latest(db, *, project_id: str, workflow_type: str, service_url: str) -> dict:
    source = db.fetchone(
        """SELECT * FROM workflows
             WHERE project_id=? AND workflow_type=? AND status!='CANCELLED'
             ORDER BY created_at DESC LIMIT 1""",
        (project_id, workflow_type),
    )
    if source is None:
        raise ValueError(f"no {workflow_type} workflow found for project {project_id}")
    payload = json.dumps({"scope": "ALL_DOWNSTREAM", "auto_advance": False}).encode("utf-8")
    request = urllib.request.Request(
        service_url.rstrip("/") + f"/api/workflows/{source['id']}/rebuild",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            operation = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"rebuild request failed: HTTP {exc.code}: {detail}") from exc
    nodes = (operation.get("plan") or {}).get("nodes") or []
    root = nodes[0] if nodes else {}
    new_id = root.get("new_workflow_id")
    created = db.fetchone("SELECT status,current_step FROM workflows WHERE id=?", (new_id,)) if new_id else None
    return {
        "rebuild_operation_id": operation.get("id"),
        "branch_id": operation.get("branch_id"),
        "source_workflow_id": source["id"],
        "cancelled_workflow_id": source["id"] if source["status"] != "COMPLETED" else None,
        "cancelled_open_gates": 0,
        "new_workflow_id": new_id,
        "status": (created or {}).get("status") or root.get("new_status"),
        "current_step": int((created or {}).get("current_step") or 0),
        "auto_advanced": False,
        "cascade_planned": len(nodes) > 1,
    }
