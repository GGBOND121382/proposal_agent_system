from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any

SECURITY_ORDER = {"PUBLIC": 0, "INTERNAL": 1, "SENSITIVE": 2, "CLASSIFIED": 3}


class RoutingDenied(RuntimeError):
    pass


@dataclass(frozen=True)
class Route:
    prompt_id: str
    environment: str
    model_id: str
    endpoint_id: str
    provider_model_name: str
    endpoint: dict[str, Any]
    profile: dict[str, Any]


class SecurityRouter:
    def __init__(self, pack):
        self.pack = pack
        self.endpoint_by_id = {e["endpoint_id"]: e for e in pack.endpoints["endpoints"]}
        self.model_by_id = {m["model_id"]: m for m in pack.models["models"]}

    def route(self, prompt_id: str, envelope: dict[str, Any], *, original_environment: str | None = None) -> Route:
        entry = self.pack.entry(prompt_id)
        required = entry["required_environment"]
        if required == "SAME_AS_ORIGINAL":
            if not original_environment:
                raise RoutingDenied("P-TARGETED-REPAIR requires the original execution environment")
            required = original_environment

        ctx = envelope.get("security_context", {})
        level = ctx.get("input_max_security_level") or ctx.get("project_security_level") or "INTERNAL"
        approval = ctx.get("online_transfer_approval_status", "NOT_REQUIRED")
        allowed_endpoints = set(ctx.get("allowed_model_endpoint_ids") or [])

        if required == "ONLINE_PUBLIC":
            if level != "PUBLIC":
                raise RoutingDenied(f"Online execution only accepts PUBLIC input, got {level}")
            if approval not in {"APPROVED", "NOT_REQUIRED"}:
                raise RoutingDenied("Online transfer approval is missing")
        elif required == "OFFLINE_LOCAL":
            pass
        else:
            raise RoutingDenied(f"Unsupported required environment: {required}")

        profile = self.pack.model_profile(prompt_id)
        candidates = list(profile.get("preferred_models", [])) + list(profile.get("fallback_models", []))
        reasons: list[str] = []
        simulation = os.getenv("MODEL_RUNTIME_MODE", "REPLAY").upper() in {"REPLAY", "MOCK", "SIMULATED"}
        # CHAT_BRIDGE is itself the configured model transport. It must not require
        # the underlying HTTP endpoint/model to be enabled, because no HTTP request
        # is made; the exact request is persisted for an external model to answer.
        # Environment, security-level, approval and allowed-endpoint checks below
        # remain fully enforced.
        bridge_transport = os.getenv("MODEL_GATEWAY_MODE", "OPENAI_COMPATIBLE").strip().upper() == "CHAT_BRIDGE"
        transport_available = simulation or bridge_transport
        for model_id in candidates:
            model = self.model_by_id.get(model_id)
            if not model or (not model.get("enabled", False) and not transport_available):
                reasons.append(f"{model_id}: disabled")
                continue
            endpoint = self.endpoint_by_id.get(model["endpoint_id"])
            if not endpoint or (not endpoint.get("enabled", False) and not transport_available):
                reasons.append(f"{model_id}: endpoint disabled")
                continue
            if endpoint["environment"] != required:
                reasons.append(f"{model_id}: environment mismatch")
                continue
            if level not in endpoint.get("allowed_security_levels", []):
                reasons.append(f"{model_id}: security level denied")
                continue
            if allowed_endpoints and endpoint["endpoint_id"] not in allowed_endpoints:
                reasons.append(f"{model_id}: not in allowed endpoint list")
                continue
            provider_name = str(model.get("provider_model_name") or "").strip()
            return Route(prompt_id, required, model_id, endpoint["endpoint_id"], provider_name, endpoint, profile)
        raise RoutingDenied("No eligible model route: " + "; ".join(reasons))
