from __future__ import annotations

import re
from typing import Any, Mapping

_STATUS_VERDICTS: dict[str, set[str]] = {
    "PASS": {"ACCEPT", "ACCEPT_FOR_HUMAN_APPROVAL", "ACCEPT_FOR_IMPORT_REVIEW"},
    "REVISE": {"REVISE"},
    "NEED_USER_INPUT": {"REVISE"},
    "BLOCK": {"BLOCK"},
}


def documented_finding_codes(prompt_text: str) -> set[str]:
    """Return Finding codes declared in the prompt's dedicated section."""

    marker = "## Finding代码"
    if marker not in prompt_text:
        return set()
    section = prompt_text.split(marker, 1)[1]
    if "\n## " in section:
        section = section.split("\n## ", 1)[0]
    return set(re.findall(r"`([A-Z][A-Z0-9_]+)`", section))


def protocol_semantic_errors(
    *,
    prompt_id: str,
    kind: str,
    value: Mapping[str, Any],
    expected_output_schema: str,
) -> list[str]:
    """Validate cross-field Prompt protocol rules not compactly expressible in Schema."""

    errors: list[str] = []
    if kind == "input":
        observed_schema = value.get("expected_output_schema")
        if observed_schema != expected_output_schema:
            errors.append(
                "/expected_output_schema: expected "
                f"{expected_output_schema!r}, observed {observed_schema!r}"
            )
        return errors

    if kind != "output":
        return errors

    status = str(value.get("status") or "").upper()
    findings = [item for item in value.get("findings") or [] if isinstance(item, dict)]
    questions = [item for item in value.get("user_questions") or [] if isinstance(item, dict)]
    unresolved = [item for item in value.get("unresolved_items") or [] if isinstance(item, dict)]
    blocking_findings = [item for item in findings if bool(item.get("blocking", True))]
    blocking_questions = [item for item in questions if bool(item.get("blocking", True))]
    blocking_unresolved = [item for item in unresolved if bool(item.get("blocking", True))]

    if status == "NEED_USER_INPUT" and not blocking_questions:
        errors.append(
            "/user_questions: NEED_USER_INPUT requires at least one blocking, "
            "directly answerable question"
        )
    if status != "NEED_USER_INPUT" and blocking_questions:
        errors.append(
            f"/status: {status or '<missing>'} cannot carry blocking user_questions; "
            "use NEED_USER_INPUT"
        )
    if status == "PASS" and blocking_findings:
        errors.append("/status: PASS cannot carry blocking findings")
    if status == "PASS" and blocking_unresolved:
        errors.append("/status: PASS cannot carry blocking unresolved_items")

    result_object = value.get("result")
    if isinstance(result_object, dict) and "verdict" in result_object:
        verdict = str(result_object.get("verdict") or "").upper()
        allowed = _STATUS_VERDICTS.get(status)
        if allowed is not None and verdict not in allowed:
            errors.append(
                "/result/verdict: "
                f"{verdict or '<missing>'} conflicts with status {status}; "
                f"expected one of {sorted(allowed)}"
            )

    return errors


def finding_code_errors(
    *, prompt_id: str, output: Mapping[str, Any], prompt_text: str
) -> list[str]:
    """Reject findings that are not part of the prompt's declared vocabulary."""

    documented = documented_finding_codes(prompt_text)
    errors: list[str] = []
    for index, finding in enumerate(output.get("findings") or []):
        if not isinstance(finding, dict):
            continue
        code = str(finding.get("code") or "")
        if code and code not in documented:
            errors.append(
                f"/findings/{index}/code: {code!r} is not documented by {prompt_id}"
            )
    return errors


# Compatibility name retained for the standalone Replay validator.
replay_finding_code_errors = finding_code_errors
