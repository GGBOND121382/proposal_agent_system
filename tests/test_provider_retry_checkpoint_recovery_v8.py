from __future__ import annotations

import copy

from app.workflow_status import WorkflowStatus
from app.workflows import WorkflowEngine


class _DB:
    def __init__(self) -> None:
        self.events = []

    def audit(self, event_type, **kwargs):
        self.events.append((event_type, copy.deepcopy(kwargs)))


class _Harness(WorkflowEngine):
    def __init__(self) -> None:
        self.db = _DB()

    def _update(self, wf, **kwargs):
        if "status" in kwargs:
            wf["status"] = kwargs["status"]
        if "state" in kwargs:
            wf["state"] = kwargs["state"]


def test_blocked_contract_with_pending_provider_retry_reopens_without_resetting_cycle() -> None:
    engine = _Harness()
    state = {
        "last_error": "repair failed",
        "provider_wait": {
            "retry_key": "0:P-ARGUMENT-ARCHITECTURE",
            "prompt_id": "P-ARGUMENT-ARCHITECTURE",
            "completed_attempts": 4,
            "max_attempts": 6,
            "attempt_call_key": "call-attempt-4",
            "retry_not_before": "2026-08-12T06:48:42+00:00",
            "decision": {
                "should_retry": True,
                "completed_attempts": 4,
                "max_attempts": 6,
            },
        },
        "provider_call_cycles": {
            "0:P-ARGUMENT-ARCHITECTURE": {
                "cycle_id": "cycle-1",
                "completed_attempts": 4,
            }
        },
    }
    wait_snapshot = copy.deepcopy(state["provider_wait"])
    cycle_snapshot = copy.deepcopy(state["provider_call_cycles"])

    wf = {
        "id": "wf-generic",
        "project_id": "project-1",
        "workflow_type": "WF-4_PROPOSAL_AUTHORING",
        "current_step": 0,
        "status": WorkflowStatus.BLOCKED_CONTRACT.value,
        "state": state,
    }

    assert engine._recover_retryable_provider_checkpoint(wf, state) is True
    assert wf["status"] == WorkflowStatus.RUNNING.value
    assert state["provider_wait"] == wait_snapshot
    assert state["provider_call_cycles"] == cycle_snapshot
    assert "last_error" not in state
    assert state["recovered_from"] == WorkflowStatus.BLOCKED_CONTRACT.value
    assert engine.db.events[0][0] == "PROVIDER_RETRY_CHECKPOINT_RECOVERED"


def test_blocked_contract_without_pending_retry_stays_blocked() -> None:
    engine = _Harness()
    state = {
        "provider_wait": {
            "retry_key": "0:P-ARGUMENT-ARCHITECTURE",
            "prompt_id": "P-ARGUMENT-ARCHITECTURE",
            "completed_attempts": 6,
            "max_attempts": 6,
            "decision": {"should_retry": False},
        }
    }
    wf = {
        "id": "wf-generic",
        "project_id": "project-1",
        "workflow_type": "WF-4_PROPOSAL_AUTHORING",
        "current_step": 0,
        "status": WorkflowStatus.BLOCKED_CONTRACT.value,
        "state": state,
    }

    assert engine._recover_retryable_provider_checkpoint(wf, state) is False
    assert wf["status"] == WorkflowStatus.BLOCKED_CONTRACT.value
    assert engine.db.events == []
