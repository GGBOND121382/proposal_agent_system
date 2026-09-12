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




_LEGACY_PROVIDER_ERROR_MARKERS = (
    "transport failed",
    "connecterror",
    "connection reset",
    "connection refused",
    "timeout",
    "timed out",
    "stream completed without message content",
    "empty stream",
    "rate limit",
    "too many requests",
    "http 429",
    "http 500",
    "http 502",
    "http 503",
    "http 504",
)

_LEGACY_CONTRACT_ERROR_MARKERS = (
    "schema validation",
    "unresolved schema scaffold",
    "output schema",
    "output container",
    "contract",
    "not of type",
    "required property",
    "token limit",
    "output token",
    "json parse",
)

_LEGACY_CONFIGURATION_ERROR_MARKERS = (
    "configuration",
    "missing endpoint",
    "missing model",
    "api key",
    "base_url",
    "运行依赖未满足",
)

_TERMINAL_RUNTIME_TRANSIENT_KEYS = (
    "runtime_recoverable",
    "runtime_failure_point",
    "runtime_blocked_at",
)


_LEGACY_STATUS_ALIASES: dict[str, WorkflowStatus] = {
    # File-bridged Stage tools historically persisted WAITING_MODEL in the
    # shared workflows table.  The canonical runtime state is
    # WAITING_PROVIDER; keep the alias readable so old checkpoints can be
    # migrated without teaching every caller about a second waiting state.
    "WAITING_MODEL": WorkflowStatus.WAITING_PROVIDER,
}


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
    if isinstance(value, WorkflowStatus):
        return value
    raw = str(value)
    aliased = _LEGACY_STATUS_ALIASES.get(raw)
    if aliased is not None:
        return aliased
    try:
        return WorkflowStatus(raw)
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


def should_pause_automatic_advancement(value: WorkflowStatus | str) -> bool:
    """Whether a driver loop must stop calling ``advance`` automatically.

    Gate-aware drivers may handle ``WAITING_GATE`` before calling this helper.
    Every other waiting state requires an external dependency, recoverable
    blocks require an explicit recovery decision, and terminal states cannot
    advance.  Centralising the boundary prevents callers from recognising only
    the historical generic ``BLOCKED`` value and then spinning until their
    arbitrary maximum-step limit hides the real workflow error.
    """

    return is_waiting(value) or is_recoverable_block(value) or is_terminal(value)


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


def classify_legacy_blocked_error(error: str | None) -> WorkflowStatus:
    """Classify a pre-ontology generic BLOCKED error without retrying it.

    Historical databases stored unrelated provider, contract, configuration,
    and technical failures under one ``BLOCKED`` value.  Migration must first
    recover the failure category from persisted evidence; only the resulting
    canonical state may decide whether automatic recovery is allowed.
    """

    lowered = str(error or "").lower()
    if any(marker in lowered for marker in _LEGACY_PROVIDER_ERROR_MARKERS):
        return WorkflowStatus.WAITING_PROVIDER
    if any(marker in lowered for marker in _LEGACY_CONTRACT_ERROR_MARKERS):
        return WorkflowStatus.BLOCKED_CONTRACT
    if any(marker in lowered for marker in _LEGACY_CONFIGURATION_ERROR_MARKERS):
        return WorkflowStatus.WAITING_CONFIGURATION
    return WorkflowStatus.BLOCKED_TECHNICAL


def clear_terminal_runtime_transients(
    state: dict,
    status: WorkflowStatus | str,
) -> dict:
    """Remove crash-resume flags when a workflow becomes terminal.

    The caller-owned state object is updated in place so nested workflow
    checkpoints keep their established identity.  Diagnostic history remains
    available in audit events; only flags that could mislead a future recovery
    path are removed.
    """

    if is_terminal(status):
        for key in _TERMINAL_RUNTIME_TRANSIENT_KEYS:
            state.pop(key, None)
    return state
