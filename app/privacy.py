from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable


_EMAIL_RE = re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![A-Za-z0-9.-])")
_PHONE_RE = re.compile(r"(?<!\d)(?:(?:\+?86)[ -]?)?1[3-9]\d(?:[ -]?\d){8}(?!\d)")


@dataclass(frozen=True)
class RedactionRule:
    value: str
    entity_type: str
    placeholder: str
    field_label: str


@dataclass(frozen=True)
class PrivacyMatch:
    path: str
    entity_type: str
    field_label: str
    placeholder: str


class OutboundPrivacyError(RuntimeError):
    def __init__(self, matches: list[PrivacyMatch]):
        self.matches = matches
        summary = ", ".join(f"{item.path}:{item.entity_type}" for item in matches[:10])
        super().__init__(f"Online payload contains prohibited personal or project-specific data: {summary}")


def load_project_config(db, project_id: str) -> dict[str, Any]:
    row = db.fetchone("SELECT config_json FROM projects WHERE id=?", (project_id,))
    if not row:
        return {}
    try:
        return json.loads(row["config_json"])
    except (TypeError, json.JSONDecodeError):
        return {}


def redaction_rules(config: dict[str, Any]) -> list[RedactionRule]:
    rules: list[RedactionRule] = []
    seen: set[str] = set()
    raw_entities = config.get("external_redaction_entities") or []
    for index, raw in enumerate(raw_entities, 1):
        if not isinstance(raw, dict):
            continue
        value = str(raw.get("value") or "").strip()
        if not value or value in seen:
            continue
        entity_type = str(raw.get("entity_type") or "CUSTOM").strip().upper()
        placeholder = str(raw.get("placeholder") or f"[{entity_type}_{index}]").strip()
        field_label = str(raw.get("field_label") or entity_type).strip()
        rules.append(RedactionRule(value, entity_type, placeholder, field_label))
        seen.add(value)
    for index, raw in enumerate(config.get("prohibited_external_values") or [], len(rules) + 1):
        value = str(raw or "").strip()
        if not value or value in seen:
            continue
        rules.append(RedactionRule(value, "CUSTOM", f"[REDACTED_{index}]", "配置禁止值"))
        seen.add(value)
    rules.sort(key=lambda item: len(item.value), reverse=True)
    return rules


def _walk_strings(value: Any, path: str = "$" ) -> Iterable[tuple[str, str]]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _walk_strings(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk_strings(item, f"{path}[{index}]")
    elif isinstance(value, str):
        yield path, value


def _is_opaque_machine_field(path: str) -> bool:
    """Return True for hashes and machine identifiers.

    Exact configured prohibited values are still checked in these fields. Only the
    generic email/phone regexes are skipped because random IDs and digests can
    naturally contain 11-digit substrings.
    """
    leaf = path.rsplit(".", 1)[-1].lower()
    leaf = re.sub(r"\[\d+\]$", "", leaf)
    if leaf.endswith("hash") or leaf.endswith("sha256") or leaf in {"digest", "checksum"}:
        return True
    if leaf == "id" or leaf.endswith("_id") or leaf.endswith("_ids"):
        return True
    return leaf in {"uuid", "nonce", "etag", "cursor", "token_fingerprint"}


def find_sensitive_values(value: Any, config: dict[str, Any], *, include_generic_patterns: bool = True) -> list[PrivacyMatch]:
    matches: list[PrivacyMatch] = []
    rules = redaction_rules(config)
    for path, text in _walk_strings(value):
        for rule in rules:
            if rule.value in text:
                matches.append(PrivacyMatch(path, rule.entity_type, rule.field_label, rule.placeholder))
        if include_generic_patterns:
            # Cryptographic hashes and opaque identifiers can accidentally contain
            # an 11-digit substring that resembles a phone number. Generic PII
            # regexes apply only to content-bearing fields, never integrity fields.
            opaque_field = _is_opaque_machine_field(path)
            if not opaque_field and _EMAIL_RE.search(text):
                matches.append(PrivacyMatch(path, "EMAIL", "电子邮箱", "[EMAIL]"))
            if not opaque_field and _PHONE_RE.search(text):
                matches.append(PrivacyMatch(path, "PHONE", "联系电话", "[PHONE]"))
    unique: dict[tuple[str, str, str], PrivacyMatch] = {}
    for match in matches:
        unique[(match.path, match.entity_type, match.placeholder)] = match
    return list(unique.values())


def assert_online_payload_safe(value: Any, config: dict[str, Any]) -> None:
    matches = find_sensitive_values(value, config, include_generic_patterns=True)
    if matches:
        raise OutboundPrivacyError(matches)


def sanitize_safe_online_package(output: dict[str, Any], config: dict[str, Any]) -> tuple[dict[str, Any], list[PrivacyMatch]]:
    """Redact project-specific entities and generic contact details from a PUBLIC package.

    The model produces the candidate package in the offline environment. This deterministic
    pass is applied before schema validation, persistence, approval, or any online model call.
    Raw sensitive values are never copied into ``removed_fields`` or public audit metadata.
    """
    sanitized = copy.deepcopy(output)
    result = sanitized.get("result")
    if not isinstance(result, dict):
        return sanitized, []

    rules = redaction_rules(config)
    matches: list[PrivacyMatch] = []

    def replace(value: Any, path: str = "$.result") -> Any:
        if isinstance(value, dict):
            return {key: replace(item, f"{path}.{key}") for key, item in value.items()}
        if isinstance(value, list):
            return [replace(item, f"{path}[{index}]") for index, item in enumerate(value)]
        if not isinstance(value, str):
            return value
        text = value
        for rule in rules:
            if rule.value in text:
                text = text.replace(rule.value, rule.placeholder)
                matches.append(PrivacyMatch(path, rule.entity_type, rule.field_label, rule.placeholder))
        if not _is_opaque_machine_field(path) and _EMAIL_RE.search(text):
            text = _EMAIL_RE.sub("[EMAIL]", text)
            matches.append(PrivacyMatch(path, "EMAIL", "电子邮箱", "[EMAIL]"))
        if not _is_opaque_machine_field(path) and _PHONE_RE.search(text):
            text = _PHONE_RE.sub("[PHONE]", text)
            matches.append(PrivacyMatch(path, "PHONE", "联系电话", "[PHONE]"))
        return text

    sanitized["result"] = replace(result)
    result = sanitized["result"]

    placeholders = list(result.get("entity_placeholders") or [])
    existing_placeholders = {item.get("placeholder") for item in placeholders if isinstance(item, dict)}
    removed_fields = list(result.get("removed_fields") or [])
    for match in matches:
        if match.placeholder not in existing_placeholders:
            placeholders.append({"placeholder": match.placeholder, "entity_type": match.entity_type})
            existing_placeholders.add(match.placeholder)
        if match.field_label not in removed_fields:
            removed_fields.append(match.field_label)
    result["entity_placeholders"] = placeholders
    result["removed_fields"] = removed_fields

    unique: dict[tuple[str, str, str], PrivacyMatch] = {}
    for match in matches:
        unique[(match.path, match.entity_type, match.placeholder)] = match
    return sanitized, list(unique.values())
