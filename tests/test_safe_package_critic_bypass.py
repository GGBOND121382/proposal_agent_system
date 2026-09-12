from __future__ import annotations

from types import SimpleNamespace

from app.workflows import WorkflowEngine

CRITIC = "P-SAFE-ONLINE-PACKAGE-CRITIC"


def _engine(enabled: bool) -> WorkflowEngine:
    engine = object.__new__(WorkflowEngine)
    gateway = SimpleNamespace(settings=SimpleNamespace(safe_package_critic_enabled=enabled))
    engine.executor = SimpleNamespace(gateway=gateway)
    return engine


def _output() -> dict:
    return {
        "findings": [
            {"code": "SAFE_PACKAGE_SCOPE_EXCESS"},
            {"code": "SAFE_PACKAGE_REIDENTIFICATION"},
            "not-a-dict",
        ]
    }


def test_revise_is_bypassed_to_pass_with_audit_when_disabled():
    state: dict = {}
    status = _engine(False)._bypass_safe_package_critic_revise(
        state, CRITIC, "REVISE", _output(), run_id="run-1"
    )
    assert status == "PASS"
    record = state["safe_package_critic_bypassed"][-1]
    assert record["run_id"] == "run-1"
    assert record["original_status"] == "REVISE"
    assert record["finding_codes"] == [
        "SAFE_PACKAGE_SCOPE_EXCESS",
        "SAFE_PACKAGE_REIDENTIFICATION",
    ]
    assert record["recorded_at"]


def test_revise_is_kept_when_enabled():
    state: dict = {}
    status = _engine(True)._bypass_safe_package_critic_revise(
        state, CRITIC, "REVISE", _output(), run_id="run-1"
    )
    assert status == "REVISE"
    assert "safe_package_critic_bypassed" not in state


def test_other_critics_are_not_bypassed_when_disabled():
    state: dict = {}
    status = _engine(False)._bypass_safe_package_critic_revise(
        state, "P-PROJECT-READINESS-CRITIC", "REVISE", _output(), run_id="run-1"
    )
    assert status == "REVISE"
    assert "safe_package_critic_bypassed" not in state


def test_block_is_not_bypassed_when_disabled():
    state: dict = {}
    status = _engine(False)._bypass_safe_package_critic_revise(
        state, CRITIC, "BLOCK", _output(), run_id="run-1"
    )
    assert status == "BLOCK"
    assert "safe_package_critic_bypassed" not in state


def test_missing_settings_defaults_to_enabled():
    engine = object.__new__(WorkflowEngine)
    engine.executor = SimpleNamespace(gateway=None)
    state: dict = {}
    status = engine._bypass_safe_package_critic_revise(
        state, CRITIC, "REVISE", _output(), run_id="run-1"
    )
    assert status == "REVISE"
    assert "safe_package_critic_bypassed" not in state
