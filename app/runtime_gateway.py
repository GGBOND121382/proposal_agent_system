from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .llm import (
    JSON_PARSER_VERSION,
    LLMError,
    LLMResult,
    MODEL_RESPONSE_PROTOCOL_VERSION,
    ModelGateway as BaseModelGateway,
    ProviderError,
    _extract_json_with_report,
    _load_strict_json_object,
)
from .runtime_failures import ProviderFailureKind
from .runtime_evidence import ModelCallEvidenceStore
from .runtime_policy import CapabilityPolicy
from .util import new_id, sha256_json


@dataclass
class RuntimeLLMResult(LLMResult):
    call_key: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    reused_response: bool = False


class AuditedModelGateway(BaseModelGateway):
    """Model gateway with durable request/response evidence and integrity replay."""

    supports_runtime_evidence = True

    def __init__(self, settings, pack):
        super().__init__(settings, pack)
        self.policy = CapabilityPolicy.from_environment()
        self.policy.assert_environment(settings.runtime_mode)
        evidence_root = Path(
            os.getenv("MODEL_CALL_EVIDENCE_DIR", str(Path(settings.data_dir) / "model_calls"))
        ).resolve()
        self.evidence_store = ModelCallEvidenceStore(evidence_root)

    async def invoke(
        self,
        route,
        prompt_id: str,
        system_prompt: str,
        envelope: dict[str, Any],
        output_schema: dict[str, Any],
        *,
        call_key: str | None = None,
        direct_tool_arguments: bool = False,
    ) -> RuntimeLLMResult:
        call_key = call_key or new_id("call")
        request_payload = {
            "call_key": call_key,
            "prompt_id": prompt_id,
            "runtime_mode": self.settings.runtime_mode,
            "environment": route.environment,
            "model_id": route.model_id,
            "endpoint_id": route.endpoint_id,
            "provider_model_name": route.provider_model_name,
            "system_prompt": system_prompt,
            "input_envelope": envelope,
            "output_schema": output_schema,
            "input_sha256": sha256_json(envelope),
            "model_response_protocol_version": MODEL_RESPONSE_PROTOCOL_VERSION,
            "direct_tool_arguments": bool(direct_tool_arguments),
        }
        if str(self.settings.runtime_mode or "").upper() == "LIVE":
            request_payload["token_budget"] = self._resolve_output_token_budget(
                route,
                system_prompt,
                envelope,
                output_schema,
            )
            compact_envelope = json.dumps(
                envelope, ensure_ascii=False, separators=(",", ":")
            )
            compact_schema = json.dumps(
                output_schema, ensure_ascii=False, separators=(",", ":")
            )
            request_payload["request_size"] = {
                "system_prompt_utf8_bytes": len(system_prompt.encode("utf-8")),
                "input_envelope_utf8_bytes": len(compact_envelope.encode("utf-8")),
                "output_schema_utf8_bytes": len(compact_schema.encode("utf-8")),
                "core_request_utf8_bytes": (
                    len(system_prompt.encode("utf-8"))
                    + len(compact_envelope.encode("utf-8"))
                    + len(compact_schema.encode("utf-8"))
                ),
                "estimated_input_tokens": request_payload["token_budget"].get(
                    "estimated_input_tokens"
                ),
            }
        self.evidence_store.faults.hit("before_request_persist", call_key, prompt_id=prompt_id)
        request_meta = self.evidence_store.write_request(call_key, request_payload)
        self.evidence_store.faults.hit("after_request_persist", call_key, prompt_id=prompt_id)

        is_argument_two_stage_internal = bool(
            (route.profile or {}).get("argument_two_stage_internal_stage")
        )
        if (
            str(self.settings.runtime_mode or "").upper() == "LIVE"
            and prompt_id == "P-ARGUMENT-ARCHITECTURE"
            and not is_argument_two_stage_internal
        ):
            estimated_input_tokens = int(
                (request_payload.get("token_budget") or {}).get("estimated_input_tokens") or 0
            )
            if estimated_input_tokens > 15000:
                raise LLMError(
                    "P-ARGUMENT-ARCHITECTURE LIVE request exceeds the "
                    f"15000-token preflight limit: estimated_input_tokens={estimated_input_tokens}"
                )

        if self.evidence_store.has_response(call_key):
            verified = self.evidence_store.load_verified_response(call_key)
            return RuntimeLLMResult(
                output=verified.parsed_output,
                raw_text=verified.raw_text,
                model_id=str(verified.metadata.get("model_id") or route.model_id),
                endpoint_id=str(verified.metadata.get("endpoint_id") or route.endpoint_id),
                call_key=call_key,
                evidence={**request_meta, **verified.metadata},
                reused_response=True,
                parse_report=dict(verified.metadata.get("json_parse_report") or {}),
                response_contract_mode=str(
                    verified.metadata.get("response_contract_mode") or "UNSPECIFIED"
                ),
                provider_attempts=int(verified.metadata.get("provider_attempts") or 1),
                fallback_reason=verified.metadata.get("fallback_reason"),
            )

        if self.evidence_store.has_failed_response(call_key):
            failed = self.evidence_store.load_failed_response(call_key)
            metadata = failed["metadata"]
            kind_value = str(metadata.get("failure_kind") or ProviderFailureKind.RESPONSE_PARSE.value)
            try:
                kind = ProviderFailureKind(kind_value)
            except ValueError:
                kind = ProviderFailureKind.RESPONSE_PARSE
            raise ProviderError(
                str(metadata.get("error") or "Replayed persisted provider failure"),
                kind=kind,
                http_status=metadata.get("http_status"),
                retry_after_seconds=metadata.get("retry_after_seconds"),
                phase=metadata.get("provider_phase"),
                response_excerpt=metadata.get("response_excerpt"),
                response_text=failed.get("rejected_text"),
                retryable_hint=metadata.get("retryable_hint"),
                validation_errors=list(metadata.get("validation_errors") or []),
            )

        def persist_provider_raw(
            raw_text: str,
            provider_metadata: dict[str, Any],
        ) -> None:
            provider_attempt = int(provider_metadata.get("provider_attempt") or 1)
            self.evidence_store.write_provider_response(
                call_key,
                provider_attempt=provider_attempt,
                raw_text=raw_text,
                metadata={
                    "prompt_id": prompt_id,
                    "runtime_mode": self.settings.runtime_mode,
                    "environment": route.environment,
                    "model_id": route.model_id,
                    "endpoint_id": route.endpoint_id,
                    "provider_model_name": route.provider_model_name,
                    "request_sha256": request_meta["request_sha256"],
                    **provider_metadata,
                },
            )

        self.evidence_store.faults.hit("before_model_request", call_key, prompt_id=prompt_id)
        try:
            result = await super().invoke(
                route,
                prompt_id,
                system_prompt,
                envelope,
                output_schema,
                raw_response_sink=persist_provider_raw,
                direct_tool_arguments=direct_tool_arguments,
            )
        except ProviderError as exc:
            failure_meta = self.evidence_store.write_failed_response(
                call_key,
                rejected_text=exc.response_text,
                metadata={
                    "prompt_id": prompt_id,
                    "runtime_mode": self.settings.runtime_mode,
                    "environment": route.environment,
                    "model_id": route.model_id,
                    "endpoint_id": route.endpoint_id,
                    "provider_model_name": route.provider_model_name,
                    "request_sha256": request_meta["request_sha256"],
                    "error": str(exc),
                    "failure_kind": exc.provider_failure_kind.value,
                    "http_status": exc.http_status,
                    "retry_after_seconds": exc.retry_after_seconds,
                    "provider_phase": exc.provider_phase,
                    "response_excerpt": exc.response_excerpt,
                    "retryable_hint": exc.retryable_hint,
                    "validation_errors": list(exc.validation_errors or []),
                    "model_response_protocol_version": MODEL_RESPONSE_PROTOCOL_VERSION,
                },
            )
            self.evidence_store.faults.hit(
                "after_failed_response_persist",
                call_key,
                prompt_id=prompt_id,
            )
            raise
        if result.response_contract_mode in {
            "JSON_SCHEMA_STRICT",
            "FUNCTION_SERIALIZED_JSON_STREAM_MINIMAX",
        }:
            raw_parsed, raw_parse_report = _load_strict_json_object(result.raw_text)
        else:
            raw_parsed, raw_parse_report = _extract_json_with_report(result.raw_text)
        response_meta = self.evidence_store.write_response(
            call_key,
            raw_text=result.raw_text,
            parsed_output=result.output,
            raw_parsed_output=raw_parsed,
            metadata={
                "prompt_id": prompt_id,
                "runtime_mode": self.settings.runtime_mode,
                "environment": route.environment,
                "model_id": result.model_id,
                "endpoint_id": result.endpoint_id,
                "request_sha256": request_meta["request_sha256"],
                "json_parse_report": dict(result.parse_report or raw_parse_report),
                "json_parser_version": JSON_PARSER_VERSION,
                "response_contract_mode": result.response_contract_mode,
                "provider_attempts": result.provider_attempts,
                "fallback_reason": result.fallback_reason,
                "provider_responses": self.evidence_store.provider_response_records(call_key),
            },
        )
        self.evidence_store.faults.hit("after_response_persist", call_key, prompt_id=prompt_id)
        return RuntimeLLMResult(
            output=result.output,
            raw_text=result.raw_text,
            model_id=result.model_id,
            endpoint_id=result.endpoint_id,
            call_key=call_key,
            evidence={**request_meta, **response_meta},
            reused_response=False,
            parse_report=dict(result.parse_report or raw_parse_report),
            response_contract_mode=result.response_contract_mode,
            provider_attempts=result.provider_attempts,
            fallback_reason=result.fallback_reason,
        )
