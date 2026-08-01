from __future__ import annotations

from enum import Enum
from typing import Iterable


class WorkflowStatus(str, Enum):
    """Canonical persisted workflow states.

    The enum includes the historical ``BLOCKED`` value so existing databases
    can be classified and migrated without inventing a second compatibility
    table in every caller.
    """

    RUNNING = "RUNNING"
    WAITING_GATE = "WAITING_GATE"
    WAITING_PREREQUISITE = "WAITING_PREREQUISITE"
    WAITING_CONFIGURATION = "WAITING_CONFIGURATION"
    WAITING_PROVIDER = "WAITING_PROVIDER"
    BLOCKED_PROVIDER = "BLOCKED_PROVIDER"
    BLOCKED_CONTRACT = "BLOCKED_CONTRACT"
    BLOCKED_TECHNICAL = "BLOCKED_TECHNICAL"
    BLOCKED_CONTENT = "BLOCKED_CONTENT"
    BLOCKED = "BLOCKED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class WorkflowStatusClass(str, Enum):
    ACTIVE = "ACTIVE"
    WAITING = "WAITING"
    RECOVERABLE_BLOCK = "RECOVERABLE_BLOCK"
    TERMINAL = "TERMINAL"


_STATUS_CLASS: dict[WorkflowStatus, WorkflowStatusClass] = {
    WorkflowStatus.RUNNING: WorkflowStatusClass.ACTIVE,
    WorkflowStatus.WAITING_GATE: WorkflowStatusClass.WAITING,
    WorkflowStatus.WAITING_PREREQUISITE: WorkflowStatusClass.WAITING,
    WorkflowStatus.WAITING_CONFIGURATION: WorkflowStatusClass.WAITING,
    WorkflowStatus.WAITING_PROVIDER: WorkflowStatusClass.WAITING,
    WorkflowStatus.BLOCKED_PROVIDER: WorkflowStatusClass.RECOVERABLE_BLOCK,
    WorkflowStatus.BLOCKED_CONTRACT: WorkflowStatusClass.RECOVERABLE_BLOCK,
    WorkflowStatus.BLOCKED_TECHNICAL: WorkflowStatusClass.RECOVERABLE_BLOCK,
    WorkflowStatus.BLOCKED_CONTENT: WorkflowStatusClass.RECOVERABLE_BLOCK,
    WorkflowStatus.BLOCKED: WorkflowStatusClass.RECOVERABLE_BLOCK,
    WorkflowStatus.COMPLETED: WorkflowStatusClass.TERMINAL,
    WorkflowStatus.CANCELLED: WorkflowStatusClass.TERMINAL,
}

# Transitions describe orchestration state, not business approval.  Any
# non-terminal workflow can be resumed after its external dependency or defect
# is resolved.  Terminal records are immutable except for idempotent writes.
_ALLOWED_TRANSITIONS: dict[WorkflowStatusClass, frozenset[WorkflowStatusClass]] = {
    WorkflowStatusClass.ACTIVE: frozenset(
        {
            WorkflowStatusClass.ACTIVE,
            WorkflowStatusClass.WAITING,
            WorkflowStatusClass.RECOVERABLE_BLOCK,
            WorkflowStatusClass.TERMINAL,
        }
    ),
    WorkflowStatusClass.WAITING: frozenset(
        {
            WorkflowStatusClass.ACTIVE,
            WorkflowStatusClass.WAITING,
            WorkflowStatusClass.RECOVERABLE_BLOCK,
            WorkflowStatusClass.TERMINAL,
        }
    ),
    WorkflowStatusClass.RECOVERABLE_BLOCK: frozenset(
        {
            WorkflowStatusClass.ACTIVE,
            WorkflowStatusClass.WAITING,
            WorkflowStatusClass.RECOVERABLE_BLOCK,
            WorkflowStatusClass.TERMINAL,
        }
    ),
    WorkflowStatusClass.TERMINAL: frozenset({WorkflowStatusClass.TERMINAL}),
}


def coerce_workflow_status(value: WorkflowStatus | str) -> WorkflowStatus:
    try:
        return value if isinstance(value, WorkflowStatus) else WorkflowStatus(str(value))
    except ValueError as exc:
        raise ValueError(f"unknown workflow status: {value!r}") from exc


def status_class(value: WorkflowStatus | str) -> WorkflowStatusClass:
    return _STATUS_CLASS[coerce_workflow_status(value)]


def is_active(value: WorkflowStatus | str) -> bool:
    return status_class(value) is WorkflowStatusClass.ACTIVE


def is_waiting(value: WorkflowStatus | str) -> bool:
    return status_class(value) is WorkflowStatusClass.WAITING


def is_recoverable_block(value: WorkflowStatus | str) -> bool:
    return status_class(value) is WorkflowStatusClass.RECOVERABLE_BLOCK


def is_terminal(value: WorkflowStatus | str) -> bool:
    return status_class(value) is WorkflowStatusClass.TERMINAL


def occupies_workflow_slot(value: WorkflowStatus | str) -> bool:
    """Whether an existing workflow prevents starting a duplicate instance."""

    return not is_terminal(value)


def can_transition(
    current: WorkflowStatus | str,
    target: WorkflowStatus | str,
) -> bool:
    current_status = coerce_workflow_status(current)
    target_status = coerce_workflow_status(target)
    if current_status is target_status:
        return True
    if is_terminal(current_status):
        return False
    return status_class(target_status) in _ALLOWED_TRANSITIONS[status_class(current_status)]


def ensure_transition(
    current: WorkflowStatus | str,
    target: WorkflowStatus | str,
) -> WorkflowStatus:
    current_status = coerce_workflow_status(current)
    target_status = coerce_workflow_status(target)
    if not can_transition(current_status, target_status):
        raise ValueError(
            f"illegal workflow status transition: {current_status.value}->{target_status.value}"
        )
    return target_status


def values_for_classes(*classes: WorkflowStatusClass) -> tuple[str, ...]:
    selected = set(classes)
    return tuple(
        status.value
        for status in WorkflowStatus
        if _STATUS_CLASS[status] in selected
    )


def normalize_status_values(values: Iterable[WorkflowStatus | str]) -> tuple[str, ...]:
    return tuple(coerce_workflow_status(value).value for value in values)

_PROPAGATION_PRIORITY: tuple[WorkflowStatus, ...] = (
    WorkflowStatus.CANCELLED,
    WorkflowStatus.BLOCKED_CONTRACT,
    WorkflowStatus.BLOCKED_TECHNICAL,
    WorkflowStatus.BLOCKED_PROVIDER,
    WorkflowStatus.BLOCKED_CONTENT,
    WorkflowStatus.BLOCKED,
    WorkflowStatus.WAITING_GATE,
    WorkflowStatus.WAITING_PREREQUISITE,
    WorkflowStatus.WAITING_CONFIGURATION,
    WorkflowStatus.WAITING_PROVIDER,
    WorkflowStatus.RUNNING,
    WorkflowStatus.COMPLETED,
)


def aggregate_workflow_statuses(
    values: Iterable[WorkflowStatus | str],
) -> WorkflowStatus:
    """Derive one parent status without flattening child state categories.

    Every exact child status remains persisted by the caller.  This function
    chooses the most actionable parent state: cancellation, recoverable block,
    waiting dependency, active work, then completion.
    """

    statuses = {coerce_workflow_status(value) for value in values}
    if not statuses:
        return WorkflowStatus.RUNNING
    for candidate in _PROPAGATION_PRIORITY:
        if candidate in statuses:
            return candidate
    raise AssertionError(f"unclassified workflow statuses: {statuses!r}")
