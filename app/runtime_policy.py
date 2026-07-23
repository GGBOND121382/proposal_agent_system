from __future__ import annotations

import copy
import os
import threading
from dataclasses import dataclass
from typing import Any

from .util import sha256_json


class CapabilityModeError(RuntimeError):
    """Raised when a capability-acceptance run would use non-LIVE shortcuts."""


@dataclass(frozen=True)
class CapabilityPolicy:
    enabled: bool

    @classmethod
    def from_environment(cls) -> "CapabilityPolicy":
        raw = os.getenv("CAPABILITY_ACCEPTANCE_MODE", "false").strip().lower()
        return cls(enabled=raw in {"1", "true", "yes", "on"})

    def assert_environment(self, runtime_mode: str) -> None:
        if not self.enabled:
            return
        mode = str(runtime_mode or "").upper()
        if mode != "LIVE":
            raise CapabilityModeError(
                "CAPABILITY_ACCEPTANCE_MODE requires MODEL_RUNTIME_MODE=LIVE; "
                f"received {mode or '<empty>'}."
            )
        forbidden = {
            "PUBLIC_SEARCH_PROVIDER": {"recorded", "replay", "mock", "simulated"},
            "MODEL_RESPONSE_AUTOMATION": {"1", "true", "yes", "on"},
            "SAMPLE_SECTION_FALLBACK": {"1", "true", "yes", "on"},
            "AUTO_RESPONSE_ENABLED": {"1", "true", "yes", "on"},
        }
        violations = []
        for name, values in forbidden.items():
            value = os.getenv(name, "").strip().lower()
            if value in values:
                violations.append(f"{name}={value}")
        if violations:
            raise CapabilityModeError(
                "Capability acceptance forbids replay/recorded providers, automatic responders, "
                "and sample-section fallbacks: " + ", ".join(violations)
            )

    @staticmethod
    def _semantic_projection(value: dict[str, Any]) -> dict[str, Any]:
        """Return the model-authored semantic payload without validator annotations.

        Deterministic validators may append findings and derive an execution status,
        but they must never rewrite the model-authored proposal content.  Provider
        output is persisted separately, so this projection gives capability runs a
        precise immutability check instead of rejecting every legitimate quality
        finding.
        """
        projected = copy.deepcopy(value)
        projected.pop("status", None)
        projected.pop("findings", None)
        result = projected.get("result")
        if isinstance(result, dict):
            result.pop("verdict", None)
        return projected

    def assert_output_unchanged(self, original: dict[str, Any], candidate: dict[str, Any], *, stage: str) -> None:
        if not self.enabled:
            return
        if sha256_json(self._semantic_projection(original)) != sha256_json(self._semantic_projection(candidate)):
            raise CapabilityModeError(
                "Capability acceptance allows deterministic status/finding annotations only; "
                f"stage {stage} attempted to rewrite model-authored semantic content."
            )


class LiveEnvelopeRegistry:
    """Process-local attestation for envelopes produced without Replay scaffolds.

    Capability acceptance rejects direct Replay/sample payload submission. A LIVE context
    builder registers the final, schema-valid envelope hash; the executor only consumes an
    attested envelope. The set is intentionally process-local: after restart, the workflow
    rebuilds the context from persisted project state and attests it again.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hashes: set[str] = set()

    def register(self, envelope: dict[str, Any]) -> str:
        digest = sha256_json(envelope)
        with self._lock:
            self._hashes.add(digest)
        return digest

    def contains_hash(self, digest: str) -> bool:
        with self._lock:
            return digest in self._hashes

    def clear(self) -> None:
        with self._lock:
            self._hashes.clear()


LIVE_ENVELOPE_REGISTRY = LiveEnvelopeRegistry()
