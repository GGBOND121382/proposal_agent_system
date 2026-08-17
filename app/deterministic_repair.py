from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any

from .json_pointer import (
    JsonPointerError,
    is_ancestor_or_same,
    parse_pointer,
    resolve_pointer,
)


@dataclass(frozen=True)
class DeterministicRepair:
    candidate: dict[str, Any]
    changed_paths: tuple[str, ...]
    rule_ids: tuple[str, ...]


def _authorized(path: str, allowed_paths: list[str]) -> bool:
    return any(is_ancestor_or_same(root, path) for root in allowed_paths)


def _set_existing(document: Any, path: str, value: Any) -> None:
    tokens = parse_pointer(path)
    if not tokens:
        raise JsonPointerError("root replacement is not allowed")
    current = document
    for token in tokens[:-1]:
        if isinstance(current, dict):
            current = current[token]
        elif isinstance(current, list):
            current = current[int(token)]
        else:
            raise JsonPointerError(f"cannot traverse {token!r}")
    leaf = tokens[-1]
    if isinstance(current, dict):
        if leaf not in current:
            raise JsonPointerError(f"missing key {leaf!r}")
        current[leaf] = copy.deepcopy(value)
        return
    if isinstance(current, list):
        index = int(leaf)
        current[index] = copy.deepcopy(value)
        return
    raise JsonPointerError("cannot replace below scalar")


def _error_paths(validation_errors: list[str]) -> set[str]:
    result: set[str] = set()
    for raw in validation_errors:
        match = re.match(r"^(/[^\s:]*)(?::|\s|$)", str(raw or ""))
        if not match or match.group(1) == "/":
            continue
        try:
            parse_pointer(match.group(1))
        except JsonPointerError:
            continue
        result.add(match.group(1))
    return result


def apply_deterministic_contract_repairs(
    candidate: dict[str, Any],
    validation_errors: list[str],
    allowed_paths: list[str],
) -> DeterministicRepair:
    """Apply only rules whose output is uniquely determined by authored data.

    This function is deliberately non-semantic. It never invents an entity,
    chooses among business alternatives, rewrites prose, or relaxes a validator.
    The returned object is a copy; callers must run the complete original
    validation chain before accepting it.
    """

    repaired = copy.deepcopy(candidate)
    changed: list[str] = []
    rules: list[str] = []
    error_paths = _error_paths(validation_errors)

    # CHOICE at answer-schema level is a representation alias for ENUM. This is
    # not a semantic guess about the user's question.
    for path in sorted(error_paths):
        tokens = parse_pointer(path)
        if (
            len(tokens) == 4
            and tokens[0] == "user_questions"
            and tokens[1].isdigit()
            and tokens[2:] == ("answer_schema", "type")
            and _authorized(path, allowed_paths)
        ):
            index = int(tokens[1])
            questions = repaired.get("user_questions") or []
            if not (0 <= index < len(questions)) or not isinstance(questions[index], dict):
                continue
            question_type = str(questions[index].get("question_type") or "")
            before = resolve_pointer(repaired, path)
            derived = (
                "ENUM" if str(before).upper() == "CHOICE"
                else "BOOLEAN" if question_type == "CONFIRMATION"
                else None
            )
            if derived is None:
                continue
            if before != derived:
                _set_existing(repaired, path, derived)
                changed.append(path)
                rules.append("QUESTION_TYPE_TO_ANSWER_SCHEMA")

    # A blocking USER-routed finding cannot be auto-repairable. The authored
    # business decision is blocking=true + route=USER; repairable is policy.
    for path in sorted(error_paths):
        tokens = parse_pointer(path)
        if (
            len(tokens) == 3
            and tokens[0] == "findings"
            and tokens[1].isdigit()
            and tokens[2] == "repairable"
            and _authorized(path, allowed_paths)
        ):
            index = int(tokens[1])
            findings = repaired.get("findings") or []
            if not (0 <= index < len(findings)) or not isinstance(findings[index], dict):
                continue
            finding = findings[index]
            if (
                bool(finding.get("blocking"))
                and str(finding.get("suggested_route") or "").upper() == "USER"
                and finding.get("repairable") is not False
            ):
                _set_existing(repaired, path, False)
                changed.append(path)
                rules.append("USER_BLOCKER_NOT_AUTO_REPAIRABLE")

    # Workflow status is deterministic when authored blockers already exist.
    status_path = "/status"
    status_relevant = (
        status_path in error_paths
        or any(
            path.startswith("/user_questions") or path.startswith("/findings")
            for path in error_paths
        )
    )
    if status_relevant and _authorized(status_path, allowed_paths):
        blocking_question = any(
            isinstance(item, dict) and bool(item.get("blocking"))
            for item in repaired.get("user_questions") or []
        )
        blocking_user_finding = any(
            isinstance(item, dict)
            and bool(item.get("blocking"))
            and str(item.get("suggested_route") or "").upper() == "USER"
            for item in repaired.get("findings") or []
        )
        if blocking_question or blocking_user_finding:
            before = repaired.get("status")
            if before in {"PASS", "REVISE"} and before != "NEED_USER_INPUT":
                repaired["status"] = "NEED_USER_INPUT"
                changed.append(status_path)
                rules.append("BLOCKING_USER_DEPENDENCY_TO_STATUS")

    return DeterministicRepair(
        candidate=repaired,
        changed_paths=tuple(dict.fromkeys(changed)),
        rule_ids=tuple(dict.fromkeys(rules)),
    )
