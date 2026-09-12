from __future__ import annotations

from fastapi.testclient import TestClient

import app.main as main


class _FakeWorkflows:
    def __init__(self) -> None:
        self.advanced: list[str] = []

    def decide_gate(self, gate_id: str, **_kwargs):
        return {
            "id": gate_id,
            "workflow_id": "wf-test",
            "status": "APPROVED",
        }

    async def advance(self, workflow_id: str):
        self.advanced.append(workflow_id)
        return {"id": workflow_id, "status": "WAITING_GATE"}


def test_approved_gate_auto_advances_workflow(monkeypatch):
    workflows = _FakeWorkflows()
    monkeypatch.setattr(main, "workflows", workflows)

    with TestClient(main.app) as client:
        response = client.post(
            "/api/gates/gate-test/decide",
            json={
                "action": "CONFIRM",
                "decided_by": "pytest",
                "decided_role": "PROJECT_OWNER",
            },
        )

    assert response.status_code == 200
    assert response.json()["status"] == "APPROVED"
    assert workflows.advanced == ["wf-test"]


def test_gate_can_skip_auto_advance(monkeypatch):
    workflows = _FakeWorkflows()
    monkeypatch.setattr(main, "workflows", workflows)

    with TestClient(main.app) as client:
        response = client.post(
            "/api/gates/gate-test/decide",
            json={
                "action": "CONFIRM",
                "decided_by": "pytest",
                "decided_role": "PROJECT_OWNER",
                "auto_advance": False,
            },
        )

    assert response.status_code == 200
    assert workflows.advanced == []
