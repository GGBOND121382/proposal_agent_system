from __future__ import annotations

import copy
import inspect
from typing import Any, Mapping, Protocol, runtime_checkable

from .contracts import get_semantic_contract


GUARD_REPORT_SCHEMA_VERSION = "1.0"
GUARD_RESPONSIBILITY = "DETERMINISTIC_GUARD"

# Deterministic quality-gate codes whose fix is a model-owned semantic
# correction (e.g. an illegal relation direction).  They are routed back to the
# original producer as bounded revision feedback instead of hard-blocking the
# workflow on the first occurrence, and they do not disqualify a concrete
# blocking human gate: human answers add information, the repair loop runs on
# the post-gate regeneration if the defects persist.
MODEL_REPAIRABLE_QUALITY_CODES = {
    "QG_PROJECT_RELATION_DIRECTION_INVALID",
    "QG_PROJECT_RELATION_TYPE_MISMATCH",
    "QG_PROJECT_RELATION_ENDPOINT_INVALID",
    "QG_PROJECT_RELATION_ID_DUPLICATE",
    "QG_CONFIRMED_ITEM_WITHOUT_EVIDENCE",
    "QG_FACT_NOT_ATOMIC",
    "QG_FACT_NUMERIC_BINDING_MISSING",
}
_GUARD_STATUSES = {"PASS", "REVISE", "BLOCK"}


class QualityGuardContractError(RuntimeError):
    """Raised when a configured quality guard violates the observer contract."""


@runtime_checkable
class QualityGuardObserver(Protocol):
    """Non-mutating deterministic quality observation contract."""

    def observe(
        self,
        prompt_id: str,
        envelope: dict[str, Any],
        output: dict[str, Any],
    ) -> dict[str, Any]:
        ...


def ensure_quality_guard_observer(observer: Any) -> QualityGuardObserver:
    """Validate the structural observer contract before any provider call.

    A callable named ``observe`` is insufficient: legacy adapters with the wrong
    signature otherwise survive service startup and fail only after an expensive
    model response.  Bind a representative call without invoking the observer.
    """

    observe = getattr(observer, "observe", None)
    if not callable(observe):
        raise QualityGuardContractError(
            f"Quality guard {type(observer).__name__} does not expose the non-mutating observe contract"
        )
    try:
        inspect.signature(observe).bind("P-CONTRACT-PROBE", {}, {})
    except (TypeError, ValueError) as exc:
        raise QualityGuardContractError(
            f"Quality guard {type(observer).__name__}.observe does not accept "
            "(prompt_id, envelope, output)"
        ) from exc
    return observer


def _deduplicate_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in findings:
        if not isinstance(item, Mapping):
            raise QualityGuardContractError("Guard findings must be objects")
        finding = copy.deepcopy(dict(item))
        key = (
            str(finding.get("code") or ""),
            str(finding.get("target_path_or_span") or finding.get("target_path") or ""),
            str(finding.get("description") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(finding)
    return result


def guard_status_from_findings(findings: list[dict[str, Any]]) -> str:
    blocking = [
        item for item in findings
        if isinstance(item, Mapping) and bool(item.get("blocking", True))
    ]
    if any(str(item.get("severity") or "").upper() == "P0" for item in blocking):
        return "BLOCK"
    if blocking:
        return "REVISE"
    return "PASS"


def build_guard_report(
    prompt_id: str,
    model_output: Mapping[str, Any],
    findings: list[dict[str, Any]],
    *,
    components: list[dict[str, Any]] | None = None,
    observations: Mapping[str, Any] | None = None,
    observation_status: str = "OBSERVED",
) -> dict[str, Any]:
    normalized_findings = _deduplicate_findings(findings)
    status = guard_status_from_findings(normalized_findings)
    contract = get_semantic_contract()
    report = {
        "schema_version": GUARD_REPORT_SCHEMA_VERSION,
        "prompt_id": prompt_id,
        "status": status,
        "blocking": status != "PASS",
        "observation_status": observation_status,
        "model_status_observed": str(model_output.get("status") or "PASS"),
        "contract_version": contract.version,
        "contract_rule_registry_version": contract.rule_registry_version,
        "contract_hash": contract.contract_hash,
        "responsibility": GUARD_RESPONSIBILITY,
        "findings": normalized_findings,
    }
    if components is not None:
        report["components"] = copy.deepcopy(components)
    if observations is not None:
        report["observations"] = copy.deepcopy(dict(observations))
    return report


def disabled_guard_report(
    prompt_id: str,
    model_output: Mapping[str, Any],
) -> dict[str, Any]:
    return build_guard_report(
        prompt_id,
        model_output,
        [],
        components=[],
        observation_status="DISABLED",
    )


def validate_guard_report(
    report: Mapping[str, Any],
    *,
    prompt_id: str,
    model_output: Mapping[str, Any],
    expected_observation_status: str = "OBSERVED",
) -> list[str]:
    contract = get_semantic_contract()
    errors: list[str] = []
    if str(report.get("schema_version") or "") != GUARD_REPORT_SCHEMA_VERSION:
        errors.append(
            f"schema_version: expected {GUARD_REPORT_SCHEMA_VERSION}, "
            f"observed {report.get('schema_version') or '<missing>'}"
        )
    if str(report.get("prompt_id") or "") != prompt_id:
        errors.append(
            f"prompt_id: expected {prompt_id}, observed {report.get('prompt_id') or '<missing>'}"
        )
    status = str(report.get("status") or "").upper()
    if status not in _GUARD_STATUSES:
        errors.append(f"status: expected one of {sorted(_GUARD_STATUSES)}, observed {status or '<missing>'}")
    blocking = report.get("blocking")
    if type(blocking) is not bool:
        errors.append("blocking must be a boolean")
    elif blocking != (status != "PASS"):
        errors.append("blocking must equal status != PASS")
    observed_state = str(report.get("observation_status") or "").upper()
    if observed_state != expected_observation_status:
        errors.append(
            f"observation_status: expected {expected_observation_status}, "
            f"observed {observed_state or '<missing>'}"
        )
    model_status = str(model_output.get("status") or "PASS")
    if str(report.get("model_status_observed") or "") != model_status:
        errors.append(
            f"model_status_observed: expected {model_status}, "
            f"observed {report.get('model_status_observed') or '<missing>'}"
        )
    expected_contract = {
        "contract_version": contract.version,
        "contract_rule_registry_version": contract.rule_registry_version,
        "contract_hash": contract.contract_hash,
    }
    for field, expected in expected_contract.items():
        observed = str(report.get(field) or "")
        if observed != str(expected):
            errors.append(f"{field}: expected {expected}, observed {observed or '<missing>'}")
    if str(report.get("responsibility") or "").upper() != GUARD_RESPONSIBILITY:
        errors.append(
            f"responsibility: expected {GUARD_RESPONSIBILITY}, "
            f"observed {report.get('responsibility') or '<missing>'}"
        )
    findings = report.get("findings")
    if not isinstance(findings, list):
        errors.append("findings must be an array")
    elif any(not isinstance(item, Mapping) for item in findings):
        errors.append("findings entries must be objects")
    components = report.get("components")
    if components is not None and (
        not isinstance(components, list)
        or any(not isinstance(item, Mapping) for item in components)
    ):
        errors.append("components must be an array of objects")
    observations = report.get("observations")
    if observations is not None and not isinstance(observations, Mapping):
        errors.append("observations must be an object")
    return errors


def observe_guard(
    observer: Any,
    prompt_id: str,
    envelope: dict[str, Any],
    model_output: dict[str, Any],
) -> dict[str, Any]:
    ensure_quality_guard_observer(observer)
    observer_input = copy.deepcopy(model_output)
    observer_baseline = copy.deepcopy(observer_input)
    report = observer.observe(prompt_id, envelope, observer_input)
    if observer_input != observer_baseline:
        raise QualityGuardContractError(
            f"Quality guard {type(observer).__name__} mutated the model output while observing"
        )
    if not isinstance(report, Mapping):
        raise QualityGuardContractError(
            f"Quality guard {type(observer).__name__} did not return an object guard_report"
        )
    errors = validate_guard_report(
        report,
        prompt_id=prompt_id,
        model_output=model_output,
        expected_observation_status="OBSERVED",
    )
    if errors:
        raise QualityGuardContractError(
            f"Quality guard {type(observer).__name__} returned an invalid guard_report: "
            + "; ".join(errors)
        )
    return copy.deepcopy(dict(report))


def require_guard_report(
    result: Mapping[str, Any],
    *,
    prompt_id: str,
    model_output: Mapping[str, Any],
    default_guard_enabled: bool | None,
) -> dict[str, Any]:
    marker = result.get("quality_guard_enabled")
    if marker is not None and type(marker) is not bool:
        raise QualityGuardContractError(
            "Prompt result quality_guard_enabled marker must be a boolean"
        )
    if default_guard_enabled is not None and type(default_guard_enabled) is not bool:
        raise QualityGuardContractError(
            "Configured quality_guard_enabled value must be a boolean"
        )
    if (
        marker is not None
        and default_guard_enabled is not None
        and marker is not default_guard_enabled
    ):
        raise QualityGuardContractError(
            "Prompt result quality_guard_enabled marker does not match "
            "the configured executor state"
        )
    guard_enabled = default_guard_enabled if marker is None else marker
    if guard_enabled is None:
        raise QualityGuardContractError(
            "Prompt result does not declare whether the quality guard was enabled"
        )
    report = result.get("guard_report")
    if not isinstance(report, Mapping):
        state = "enabled" if guard_enabled else "disabled"
        raise QualityGuardContractError(
            f"Quality guard is {state}, but the prompt result has no guard_report"
        )
    expected_status = "OBSERVED" if guard_enabled else "DISABLED"
    top_level_status = result.get("guard_observation_status")
    if top_level_status is not None:
        if not isinstance(top_level_status, str):
            raise QualityGuardContractError(
                "Prompt result guard_observation_status must be a string"
            )
        if top_level_status.upper() != expected_status:
            raise QualityGuardContractError(
                "Prompt result guard_observation_status does not match "
                f"quality_guard_enabled: expected {expected_status}, "
                f"observed {top_level_status or '<missing>'}"
            )
    errors = validate_guard_report(
        report,
        prompt_id=prompt_id,
        model_output=model_output,
        expected_observation_status=expected_status,
    )
    if errors:
        raise QualityGuardContractError(
            "Prompt result contains an invalid guard_report: " + "; ".join(errors)
        )
    return copy.deepcopy(dict(report))
