from __future__ import annotations

"""Shared contract gateway for the file-bridged staged workflow."""

import copy
import json
import os
from pathlib import Path
from typing import Any, Mapping

from .contract_registry import (
    CONTRACT_REGISTRY_VERSION,
    augment_prompt_with_enum_contract,
    augment_prompt_with_field_ownership_contract,
    normalize_against_schema,
    repair_field_ownership_against_schema,
    required_null_container_errors,
)
from .output_integrity import (
    normalize_reference_id_aliases,
    normalize_staged_source_ref_aliases,
    validate_staged_reference_integrity,
)

_TRACE_DIR: Path | None = None
_TRACE_LABEL = "model-output"
_TRACE_INDEX = 0


def _latest_request_input() -> Any | None:
    """Return the latest persisted staged request input envelope.

    Every staged ingest command sets ``_TRACE_DIR`` to its run directory before
    validating a model response.  The response being ingested always belongs to
    the most recently created request in that directory.  Reading only that
    request avoids trusting stale or rejected model responses from earlier
    attempts while still giving the shared contract gateway the exact upstream
    IDs visible to the model.
    """
    if _TRACE_DIR is None:
        return None
    request_dir = _TRACE_DIR / "requests"
    if not request_dir.exists():
        return None
    candidates = [path for path in request_dir.glob("*.json") if path.is_file()]
    if not candidates:
        return None
    latest = max(candidates, key=lambda path: (path.stat().st_mtime_ns, path.name))
    try:
        payload = json.loads(latest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    input_envelope = payload.get("input_envelope")
    return copy.deepcopy(input_envelope) if isinstance(input_envelope, (dict, list)) else None


def set_contract_trace_context(run_dir: str | Path | None, label: str) -> None:
    """Enable durable raw/normalized contract traces for the current command."""
    global _TRACE_DIR, _TRACE_LABEL, _TRACE_INDEX
    _TRACE_DIR = Path(run_dir).resolve() if run_dir is not None else None
    _TRACE_LABEL = label
    _TRACE_INDEX = 0


def clear_contract_trace_context() -> None:
    global _TRACE_DIR, _TRACE_LABEL, _TRACE_INDEX
    _TRACE_DIR = None
    _TRACE_LABEL = "model-output"
    _TRACE_INDEX = 0


def require_model_response_envelope(value: Any, *, label: str = "model response") -> dict[str, Any]:
    """Return a file-bridge response envelope or fail with a controlled error.

    Stage 1--5 use an outer bridge envelope whose ``output`` member contains
    the schema-governed model object.  Historically those commands called
    ``.get`` on the envelope before checking its root type, allowing a JSON
    array/scalar to escape as ``AttributeError``/``TypeError``.  Keep this
    boundary independent from stage-specific semantic validation.
    """
    if not isinstance(value, dict):
        raise SystemExit(
            f"{label} envelope must be a JSON object; received {type(value).__name__}"
        )
    if "output" in value and not isinstance(value.get("output"), dict):
        raise SystemExit(
            f"{label} envelope.output must be a JSON object; "
            f"received {type(value.get('output')).__name__}"
        )
    return value


def _safe_label(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in value)[:120]


def _write_trace(raw: Any, normalized: Any, report: Mapping[str, Any]) -> None:
    global _TRACE_INDEX
    if _TRACE_DIR is None:
        return
    if not report.get("normalized_count") and not report.get("unresolved_count"):
        return
    _TRACE_INDEX += 1
    target_dir = _TRACE_DIR / "quality" / "contract_normalization"
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{_TRACE_INDEX:03d}_{_safe_label(_TRACE_LABEL)}.json"
    payload = {
        "schema_version": "1.0",
        "raw_model_object": raw,
        "normalized_model_object": normalized,
        "normalization_report": dict(report),
        "raw_response_immutable": True,
    }
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)



def prepare_staged_artifact(path: str | Path, value: Any) -> Any:
    """Inject the registry-derived enum contract into every model request.

    Staged tools all persist requests as JSON files.  Centralizing the injection
    here prevents individual stages from maintaining hand-written enum lists.
    Non-request artifacts are returned unchanged.
    """
    target = Path(path)
    if target.parent.name != "requests" or not isinstance(value, dict):
        return value
    output_schema = value.get("output_schema")
    system_prompt = value.get("system_prompt")
    if not isinstance(output_schema, Mapping) or not isinstance(system_prompt, str):
        return value
    prepared = copy.deepcopy(value)
    contract_id = f"staged-request:{prepared.get('prompt_id') or target.stem}:output"
    prepared_prompt = augment_prompt_with_enum_contract(
        system_prompt, output_schema, contract_id=contract_id
    )
    prepared["system_prompt"] = augment_prompt_with_field_ownership_contract(
        prepared_prompt,
        output_schema,
        contract_id=f"{contract_id}:field-ownership",
    )
    model_contract = prepared.setdefault("model_contract", {})
    model_contract["enum_contract_registry_version"] = CONTRACT_REGISTRY_VERSION
    model_contract["field_ownership_contract_registry_version"] = CONTRACT_REGISTRY_VERSION
    return prepared

def normalize_in_place(value: Any, schema: Mapping[str, Any], *, contract_id: str) -> dict[str, Any]:
    """Apply the shared output-contract gateway to a staged model object.

    Required null containers remain untouched so the stage's strict validator
    rejects the omission.  Registered enum aliases and unique ancestor-chain
    field ownership drift are repaired deterministically and traced.
    """
    raw = copy.deepcopy(value)
    null_errors = required_null_container_errors(value, schema)
    if null_errors:
        normalized = copy.deepcopy(value)
        report: dict[str, Any] = {
            "schema_version": "1.0",
            "normalizer_version": CONTRACT_REGISTRY_VERSION,
            "contract_id": contract_id,
            "normalized_count": 0,
            "changes": [],
            "unresolved_count": len(null_errors),
            "unresolved": [
                {
                    "path": error.split(":", 1)[0],
                    "field": "",
                    "value": None,
                    "allowed_values": ["object", "array"],
                    "reason": error,
                }
                for error in null_errors
            ],
            "required_null_errors": null_errors,
        }
    else:
        normalized, enum_report = normalize_against_schema(
            value,
            schema,
            contract_id=contract_id,
        )
        normalized, ownership_report = repair_field_ownership_against_schema(
            normalized,
            schema,
            contract_id=f"{contract_id}:field-ownership",
        )
        enum_changes = list(enum_report.get("changes") or [])
        ownership_changes = list(ownership_report.get("changes") or [])
        enum_unresolved = list(enum_report.get("unresolved") or [])
        ownership_unresolved = list(ownership_report.get("unresolved") or [])
        trusted_context = _latest_request_input()
        normalized, source_alias_report = normalize_staged_source_ref_aliases(
            normalized,
            trusted_context,
        )
        normalized, reference_alias_report = normalize_reference_id_aliases(
            normalized,
            trusted_context if isinstance(trusted_context, Mapping) else {},
        )
        source_alias_changes = list(source_alias_report.get("changes") or [])
        reference_alias_changes = list(reference_alias_report.get("changes") or [])
        reference_errors = validate_staged_reference_integrity(
            normalized,
            trusted_context,
        )
        reference_unresolved = [
            {
                "path": error.split(":", 1)[0],
                "field": "",
                "value": None,
                "allowed_values": [],
                "reason": error,
                "kind": "REFERENCE_INTEGRITY",
            }
            for error in reference_errors
        ]
        report = {
            "schema_version": "1.0",
            "normalizer_version": CONTRACT_REGISTRY_VERSION,
            "contract_id": contract_id,
            "normalized_count": (
                len(enum_changes)
                + len(ownership_changes)
                + len(source_alias_changes)
                + len(reference_alias_changes)
            ),
            "changes": enum_changes + ownership_changes + source_alias_changes + reference_alias_changes,
            "unresolved_count": len(enum_unresolved) + len(ownership_unresolved) + len(reference_unresolved),
            "unresolved": enum_unresolved + ownership_unresolved + reference_unresolved,
            "enum_report": enum_report,
            "field_ownership_report": ownership_report,
            "source_alias_report": source_alias_report,
            "reference_alias_report": reference_alias_report,
            "reference_integrity_errors": reference_errors,
        }
    if isinstance(value, dict) and isinstance(normalized, dict):
        value.clear()
        value.update(normalized)
    elif isinstance(value, list) and isinstance(normalized, list):
        value[:] = normalized
    _write_trace(raw, normalized, report)
    return report


def contract_validation_errors(report: Mapping[str, Any]) -> list[str]:
    """Render unresolved shared-contract findings for stage validators."""
    errors: list[str] = []
    for item in report.get("unresolved") or []:
        if not isinstance(item, Mapping):
            continue
        reason = str(item.get("reason") or "").strip()
        if reason:
            errors.append(reason)
    return errors
