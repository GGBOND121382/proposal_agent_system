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
    normalize_against_schema,
)

_TRACE_DIR: Path | None = None
_TRACE_LABEL = "model-output"
_TRACE_INDEX = 0


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
    prepared["system_prompt"] = augment_prompt_with_enum_contract(
        system_prompt, output_schema, contract_id=contract_id
    )
    prepared.setdefault("model_contract", {})["enum_contract_registry_version"] = CONTRACT_REGISTRY_VERSION
    return prepared

def normalize_in_place(value: Any, schema: Mapping[str, Any], *, contract_id: str) -> dict[str, Any]:
    """Normalize registered enum drift and mutate ``value`` in place.

    Mutation keeps existing staged scripts backward compatible because their
    validators and downstream deterministic checks continue to operate on the
    same object reference.  Unknown values are retained for strict validation.
    """
    raw = copy.deepcopy(value)
    normalized, report = normalize_against_schema(value, schema, contract_id=contract_id)
    if isinstance(value, dict) and isinstance(normalized, dict):
        value.clear()
        value.update(normalized)
    elif isinstance(value, list) and isinstance(normalized, list):
        value[:] = normalized
    _write_trace(raw, normalized, report)
    return report
