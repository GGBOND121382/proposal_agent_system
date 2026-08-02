from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .util import utc_now


@dataclass(frozen=True)
class RepairBudget:
    provider_retry_limit: int = 2
    technical_retry_limit: int = 2
    semantic_repair_limit: int = 1


class RepairLedger:
    """Event ledger for independent retry and semantic-repair lifecycles.

    A model response is not a semantic repair attempt.  The semantic budget is
    consumed exactly once when an applied repair enters an independent review.
    Provider and technical retry counters remain completely independent.
    """

    ROOT_KEY = "repair_ledger_v1"
    SCHEMA_VERSION = "2.0.0"
    MAX_EVENTS = 400

    @classmethod
    def _root(cls, state: dict[str, Any]) -> dict[str, Any]:
        root = state.setdefault(
            cls.ROOT_KEY,
            {
                "schema_version": cls.SCHEMA_VERSION,
                "provider_retries": {},
                "technical_retries": {},
                "semantic_repairs": {},
                "events": [],
            },
        )
        root.setdefault("schema_version", cls.SCHEMA_VERSION)
        root.setdefault("provider_retries", {})
        root.setdefault("technical_retries", {})
        root.setdefault("semantic_repairs", {})
        root.setdefault("events", [])
        return root

    @classmethod
    def count(cls, state: dict[str, Any], bucket: str, key: str) -> int:
        return int(cls._root(state).setdefault(bucket, {}).get(key, 0))

    @classmethod
    def events(
        cls,
        state: dict[str, Any],
        *,
        key: str | None = None,
        repair_id: str | None = None,
    ) -> list[dict[str, Any]]:
        values = list(cls._root(state).get("events") or [])
        if key is not None:
            values = [item for item in values if item.get("key") == key]
        if repair_id is not None:
            values = [item for item in values if item.get("repair_id") == repair_id]
        return values

    @classmethod
    def _has_event(
        cls,
        state: dict[str, Any],
        *,
        event: str,
        key: str,
        repair_id: str | None,
    ) -> bool:
        return any(
            item.get("event") == event
            and item.get("key") == key
            and item.get("repair_id") == repair_id
            for item in cls._root(state).get("events") or []
        )

    @classmethod
    def record(
        cls,
        state: dict[str, Any],
        *,
        bucket: str,
        key: str,
        event: str,
        run_id: str | None = None,
        repair_id: str | None = None,
        application_artifact_id: str | None = None,
        details: dict[str, Any] | None = None,
        increment: bool = False,
        idempotent: bool = False,
    ) -> int:
        root = cls._root(state)
        counts = root.setdefault(bucket, {})
        if idempotent and cls._has_event(
            state,
            event=event,
            key=key,
            repair_id=repair_id,
        ):
            return int(counts.get(key, 0))
        if increment:
            counts[key] = int(counts.get(key, 0)) + 1
        value = int(counts.get(key, 0))
        root.setdefault("events", []).append(
            {
                "event": event,
                "bucket": bucket,
                "key": key,
                "count": value,
                "run_id": run_id,
                "repair_id": repair_id,
                "application_artifact_id": application_artifact_id,
                "details": details or {},
                "recorded_at": utc_now(),
            }
        )
        del root["events"][:-cls.MAX_EVENTS]
        return value

    @classmethod
    def reset_semantic_budget(
        cls,
        state: dict[str, Any],
        key: str,
        *,
        reason: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        root = cls._root(state)
        counts = root.setdefault("semantic_repairs", {})
        previous = int(counts.get(key, 0))
        if previous == 0 and key not in counts:
            return
        counts[key] = 0
        cls.record(
            state,
            bucket="semantic_repairs",
            key=key,
            event="SUBJECT_SUPERSEDED",
            details={
                **(details or {}),
                "reason": reason,
                "previous_count": previous,
            },
        )

    @classmethod
    def provider_retry(cls, state: dict[str, Any], key: str, **kwargs: Any) -> int:
        return cls.record(
            state,
            bucket="provider_retries",
            key=key,
            event="PROVIDER_RETRY",
            increment=True,
            **kwargs,
        )

    @classmethod
    def provider_recovered(cls, state: dict[str, Any], key: str, **kwargs: Any) -> int:
        return cls.record(
            state,
            bucket="provider_retries",
            key=key,
            event="PROVIDER_RECOVERED",
            increment=False,
            **kwargs,
        )

    @classmethod
    def technical_retry(cls, state: dict[str, Any], key: str, **kwargs: Any) -> int:
        return cls.record(
            state,
            bucket="technical_retries",
            key=key,
            event="TECHNICAL_RETRY",
            increment=True,
            **kwargs,
        )

    @classmethod
    def contract_rejected(cls, state: dict[str, Any], key: str, **kwargs: Any) -> int:
        return cls.record(
            state,
            bucket="technical_retries",
            key=key,
            event="CONTRACT_REJECTED",
            increment=False,
            **kwargs,
        )

    @classmethod
    def contract_recovered(cls, state: dict[str, Any], key: str, **kwargs: Any) -> int:
        return cls.record(
            state,
            bucket="technical_retries",
            key=key,
            event="CONTRACT_RECOVERED",
            increment=False,
            **kwargs,
        )

    @classmethod
    def repair_rejected(cls, state: dict[str, Any], key: str, **kwargs: Any) -> int:
        return cls.record(
            state,
            bucket="semantic_repairs",
            key=key,
            event="REPAIR_REJECTED",
            increment=False,
            **kwargs,
        )

    @classmethod
    def repair_created(cls, state: dict[str, Any], key: str, **kwargs: Any) -> int:
        return cls.record(
            state,
            bucket="semantic_repairs",
            key=key,
            event="CREATED",
            increment=False,
            **kwargs,
        )

    @classmethod
    def model_returned(cls, state: dict[str, Any], key: str, **kwargs: Any) -> int:
        return cls.record(
            state,
            bucket="semantic_repairs",
            key=key,
            event="MODEL_RETURNED",
            increment=False,
            **kwargs,
        )

    @classmethod
    def schema_validated(cls, state: dict[str, Any], key: str, **kwargs: Any) -> int:
        return cls.record(
            state,
            bucket="semantic_repairs",
            key=key,
            event="SCHEMA_VALIDATED",
            increment=False,
            **kwargs,
        )

    @classmethod
    def diff_validated(cls, state: dict[str, Any], key: str, **kwargs: Any) -> int:
        return cls.record(
            state,
            bucket="semantic_repairs",
            key=key,
            event="DIFF_VALIDATED",
            increment=False,
            **kwargs,
        )

    @classmethod
    def applied(cls, state: dict[str, Any], key: str, **kwargs: Any) -> int:
        return cls.record(
            state,
            bucket="semantic_repairs",
            key=key,
            event="APPLIED",
            increment=False,
            **kwargs,
        )

    @classmethod
    def rereview_started(cls, state: dict[str, Any], key: str, **kwargs: Any) -> int:
        repair_id = kwargs.get("repair_id")
        if not cls._has_event(
            state,
            event="APPLIED",
            key=key,
            repair_id=repair_id,
        ):
            raise ValueError(
                "Independent re-review may start only for an APPLIED repair application"
            )
        return cls.record(
            state,
            bucket="semantic_repairs",
            key=key,
            event="REREVIEW_STARTED",
            increment=True,
            idempotent=True,
            **kwargs,
        )

    @classmethod
    def rereview_completed(
        cls,
        state: dict[str, Any],
        key: str,
        *,
        status: str,
        **kwargs: Any,
    ) -> int:
        normalized = str(status or "").upper()
        repair_id = kwargs.get("repair_id")
        if not cls._has_event(
            state,
            event="REREVIEW_STARTED",
            key=key,
            repair_id=repair_id,
        ):
            raise ValueError(
                "Independent re-review completion requires REREVIEW_STARTED"
            )
        event = "REREVIEW_PASS" if normalized == "PASS" else "REREVIEW_REVISE"
        return cls.record(
            state,
            bucket="semantic_repairs",
            key=key,
            event=event,
            increment=False,
            idempotent=True,
            details={**(kwargs.pop("details", {}) or {}), "status": normalized},
            **kwargs,
        )
