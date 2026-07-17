from __future__ import annotations

from typing import Any

_TEXT_KEYS = (
    "objective",
    "goal",
    "task_description",
    "description",
    "desired_outcome",
    "topic",
)
_LIST_KEYS = (
    "specific_requirements",
    "requirements",
    "constraints",
    "must_preserve",
    "forbidden_changes",
    "acceptance_preferences",
    "priority_order",
    "deliverables",
)


def _strings(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        result: list[str] = []
        for item in value:
            result.extend(_strings(item))
        return result
    if isinstance(value, dict):
        result: list[str] = []
        for key in _TEXT_KEYS + _LIST_KEYS:
            if key in value:
                result.extend(_strings(value[key]))
        return result
    text = str(value).strip()
    return [text] if text else []


def instruction_text(value: Any, fallback: str = "") -> str:
    """Return a stable human-readable objective for scalar prompt fields."""
    if isinstance(value, dict):
        for key in _TEXT_KEYS:
            values = _strings(value.get(key))
            if values:
                return values[0]
        values = _strings(value)
        if values:
            return "；".join(dict.fromkeys(values))
        return fallback
    values = _strings(value)
    return values[0] if values else fallback


def intended_uses(value: Any, fallback: str = "") -> list[str]:
    """Normalize task instructions to the string-array contract used by security prompts."""
    values = _strings(value)
    if not values and fallback.strip():
        values = [fallback.strip()]
    return list(dict.fromkeys(item for item in values if item))
