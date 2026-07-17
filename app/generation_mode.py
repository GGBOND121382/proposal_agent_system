from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum


class GenerationMode(str, Enum):
    """How model outputs may enter a workflow run.

    ``FRESH_GENERATION`` is for portability/cold-start acceptance: every prompt must
    reach the configured model provider and any pre-existing committed response is
    treated as contamination.

    ``RESUME_FROM_CHECKPOINT`` is for durable production execution: an already
    committed result may be reused only when the complete request identity,
    workflow, prompt version, and model route are unchanged.
    """

    FRESH_GENERATION = "FRESH_GENERATION"
    RESUME_FROM_CHECKPOINT = "RESUME_FROM_CHECKPOINT"

    @property
    def allow_reuse(self) -> bool:
        return self is GenerationMode.RESUME_FROM_CHECKPOINT

    @classmethod
    def from_environment(cls) -> "GenerationMode":
        raw = os.getenv("PROPOSAL_GENERATION_MODE", cls.RESUME_FROM_CHECKPOINT.value)
        try:
            return cls(str(raw).strip().upper())
        except ValueError as exc:
            allowed = ", ".join(item.value for item in cls)
            raise ValueError(
                f"PROPOSAL_GENERATION_MODE must be one of: {allowed}"
            ) from exc


@dataclass(frozen=True)
class GenerationLineage:
    origin: str
    source_call_key: str | None = None
    source_run_id: str | None = None


MODEL_GENERATED = "MODEL_GENERATED"
CHECKPOINT_RESPONSE_REUSE = "CHECKPOINT_RESPONSE_REUSE"
COMMITTED_RESULT_REUSE = "COMMITTED_RESULT_REUSE"
