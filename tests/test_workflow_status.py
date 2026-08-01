from __future__ import annotations

import pytest

from app.workflow_status import (
    WorkflowStatus,
    WorkflowStatusClass,
    aggregate_workflow_statuses,
    can_transition,
    ensure_transition,
    is_recoverable_block,
    is_terminal,
    is_waiting,
    occupies_workflow_slot,
    status_class,
    values_for_classes,
)


def test_every_status_has_exactly_one_class() -> None:
    assert {status_class(status) for status in WorkflowStatus} == set(WorkflowStatusClass)
    for status in WorkflowStatus:
        assert isinstance(status_class(status), WorkflowStatusClass)


def test_waiting_and_recoverable_states_occupy_the_workflow_slot() -> None:
    for status in (
        WorkflowStatus.WAITING_GATE,
        WorkflowStatus.WAITING_PREREQUISITE,
        WorkflowStatus.WAITING_CONFIGURATION,
        WorkflowStatus.WAITING_PROVIDER,
    ):
        assert is_waiting(status)
        assert occupies_workflow_slot(status)
    for status in (
        WorkflowStatus.BLOCKED,
        WorkflowStatus.BLOCKED_PROVIDER,
        WorkflowStatus.BLOCKED_CONTRACT,
        WorkflowStatus.BLOCKED_TECHNICAL,
        WorkflowStatus.BLOCKED_CONTENT,
    ):
        assert is_recoverable_block(status)
        assert occupies_workflow_slot(status)


def test_terminal_records_are_immutable_except_idempotent_writes() -> None:
    for status in (WorkflowStatus.COMPLETED, WorkflowStatus.CANCELLED):
        assert is_terminal(status)
        assert not occupies_workflow_slot(status)
        assert can_transition(status, status)
        assert not can_transition(status, WorkflowStatus.RUNNING)
        with pytest.raises(ValueError, match="illegal workflow status transition"):
            ensure_transition(status, WorkflowStatus.RUNNING)


def test_nonterminal_states_can_resume_wait_or_block() -> None:
    for current in (
        WorkflowStatus.RUNNING,
        WorkflowStatus.WAITING_PROVIDER,
        WorkflowStatus.BLOCKED_PROVIDER,
        WorkflowStatus.BLOCKED_CONTRACT,
    ):
        assert can_transition(current, WorkflowStatus.RUNNING)
        assert can_transition(current, WorkflowStatus.WAITING_GATE)
        assert can_transition(current, WorkflowStatus.BLOCKED_TECHNICAL)
        assert can_transition(current, WorkflowStatus.CANCELLED)


def test_class_value_query_is_centralized() -> None:
    waiting = values_for_classes(WorkflowStatusClass.WAITING)
    assert set(waiting) == {
        "WAITING_GATE",
        "WAITING_PREREQUISITE",
        "WAITING_CONFIGURATION",
        "WAITING_PROVIDER",
    }


def test_unknown_status_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown workflow status"):
        status_class("WAITING_SOMETHING_NEW")


def test_child_status_aggregation_preserves_wait_and_block_categories() -> None:
    assert aggregate_workflow_statuses([
        WorkflowStatus.COMPLETED,
        WorkflowStatus.WAITING_PROVIDER,
    ]) is WorkflowStatus.WAITING_PROVIDER
    assert aggregate_workflow_statuses([
        WorkflowStatus.WAITING_PROVIDER,
        WorkflowStatus.BLOCKED_CONTRACT,
    ]) is WorkflowStatus.BLOCKED_CONTRACT
    assert aggregate_workflow_statuses([
        WorkflowStatus.COMPLETED,
        WorkflowStatus.COMPLETED,
    ]) is WorkflowStatus.COMPLETED
    assert aggregate_workflow_statuses([]) is WorkflowStatus.RUNNING
