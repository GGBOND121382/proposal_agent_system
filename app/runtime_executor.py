from __future__ import annotations

import copy
import json
import os
import time
from pathlib import Path
from typing import Any

from .executor import PromptExecutionError, PromptExecutor as BasePromptExecutor
from .generation_mode import (
    COMMITTED_RESULT_REUSE,
    MODEL_GENERATED,
    GenerationMode,
)
from .llm import LLMError
from .privacy import OutboundPrivacyError, assert_online_payload_safe, load_project_config, sanitize_safe_online_package
from .runtime_evidence import EvidenceIntegrityError, InjectedFailure, ModelCallEvidenceStore
from .runtime_policy import CapabilityModeError, CapabilityPolicy, LIVE_ENVELOPE_REGISTRY
from .security import RoutingDenied
from .util import new_id, sha256_json, utc_now


class RecoverablePromptExecutionError(PromptExecutionError):
    recoverable = True


class RuntimePromptExecutor(BasePromptExecutor):
    """Atomic, idempotent prompt executor backed by durable model-call evidence."""

    def __init__(self, db, pack, router, gateway, *, quality_guard=None, quality_guard_enabled: bool = True):
        super().__init__(
            db,
            pack,
            router,
            gateway,
            quality_guard=quality_guard,
            quality_guard_enabled=quality_guard_enabled,
        )
        self.policy = CapabilityPolicy.from_environment()
        runtime_mode = str(getattr(getattr(gateway, "settings", None), "runtime_mode", os.getenv("MODEL_RUNTIME_MODE", "REPLAY"))).upper()
        self.policy.assert_environment(runtime_mode)
        self.runtime_mode = runtime_mode
        self.generation_mode = GenerationMode.from_environment()
        store = getattr(gateway, "evidence_store", None)
        if store is None:
            root = Path(os.getenv("MODEL_CALL_EVIDENCE_DIR", "data/model_calls")).resolve()
            store = ModelCallEvidenceStore(root)
        self.evidence_store = store

    def _call_key(
        self,
        *,
        prompt_id: str,
        prompt_version: str,
        project_id: str,
        workflow_id: str | None,
        input_hash: str,
        model_id: str,
        endpoint_id: str,
        provider_model_name: str,
        requested_call_key: str | None,
    ) -> str:
        if requested_call_key:
            return requested_call_key
        return "call-" + sha256_json(
            {
                "project_id": project_id,
                "workflow_id": workflow_id,
                "prompt_id": prompt_id,
                "prompt_version": prompt_version,
                "input_hash": input_hash,
                "model_id": model_id,
                "endpoint_id": endpoint_id,
                "provider_model_name": provider_model_name,
            }
        )[:32]

    def _committed_result(
        self,
        call_key: str,
        *,
        project_id: str,
        workflow_id: str | None,
        prompt_id: str,
        prompt_version: str,
        input_hash: str,
        model_id: str,
        endpoint_id: str,
        provider_model_name: str,
    ) -> dict[str, Any] | None:
        event = self.db.fetchone(
            "SELECT metadata_json FROM audit_events WHERE event_type='MODEL_CALL_COMMITTED' AND object_id=? ORDER BY id DESC LIMIT 1",
            (call_key,),
        )
        if not event:
            return None
        if not self.generation_mode.allow_reuse:
            raise EvidenceIntegrityError(
                f"Fresh generation refused committed result reuse: {call_key}"
            )
        metadata = json.loads(event["metadata_json"])
        run = self.db.fetchone("SELECT * FROM prompt_runs WHERE id=?", (metadata.get("run_id"),))
        if not run or not run.get("output_json"):
            raise EvidenceIntegrityError(f"Committed call {call_key} has no matching prompt run")
        try:
            committed_input = json.loads(run.get("input_json") or "{}")
        except json.JSONDecodeError as exc:
            raise EvidenceIntegrityError(f"Committed call {call_key} has invalid input JSON") from exc
        expected = {
            "project_id": project_id,
            "workflow_id": workflow_id,
            "prompt_id": prompt_id,
            "prompt_version": prompt_version,
            "input_hash": input_hash,
            "model_id": model_id,
            "endpoint_id": endpoint_id,
            "provider_model_name": provider_model_name,
        }
        actual = {
            "project_id": run.get("project_id"),
            "workflow_id": run.get("workflow_id"),
            "prompt_id": run.get("prompt_id"),
            # Older prompt envelopes may omit prompt_version. Treat an omitted
            # version and the canonical empty version as the same identity; a
            # non-empty version still participates in the exact reuse key.
            "prompt_version": str(
                committed_input.get("prompt_version")
                or metadata.get("prompt_version")
                or ""
            ),
            "input_hash": run.get("input_hash"),
            # Compare the configured request route, not a provider-reported
            # response model alias.  Both are retained in the trace.
            "model_id": metadata.get("route_model_id") or metadata.get("model_id") or run.get("model_id"),
            "endpoint_id": metadata.get("route_endpoint_id") or metadata.get("endpoint_id") or run.get("endpoint_id"),
            # Legacy commits predate this field; their deterministic call key
            # already included the provider model name. New commits persist it.
            "provider_model_name": metadata.get("provider_model_name") or provider_model_name,
        }
        if actual != expected:
            raise EvidenceIntegrityError(
                f"Committed result identity mismatch for {call_key}: expected={expected}, actual={actual}"
            )
        output = json.loads(run["output_json"])
        self.db.audit(
            "MODEL_CALL_REUSED_FROM_CHECKPOINT",
            project_id=project_id,
            object_id=call_key,
            metadata={
                "source_run_id": run["id"],
                "workflow_id": workflow_id,
                "prompt_id": prompt_id,
                "input_hash": input_hash,
                "model_id": model_id,
                "endpoint_id": endpoint_id,
                "provider_model_name": provider_model_name,
            },
        )
        return {
            "run_id": run["id"],
            "prompt_id": run["prompt_id"],
            "status": run["status"],
            "route": {
                "environment": metadata.get("environment"),
                "model_id": run.get("model_id"),
                "endpoint_id": run.get("endpoint_id"),
            },
            "output": output,
            "call_key": call_key,
            "reused_committed_result": True,
            "generation_origin": COMMITTED_RESULT_REUSE,
            "source_run_id": run["id"],
        }

    async def execute(
        self,
        prompt_id: str,
        envelope: dict[str, Any],
        *,
        project_id: str,
        workflow_id: str | None = None,
        original_environment: str | None = None,
        call_key: str | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        quality_context_envelope = envelope
        model_envelope, input_compaction = self._prepare_model_envelope(prompt_id, envelope)
        input_hash = sha256_json(model_envelope)
        if self.policy.enabled and not LIVE_ENVELOPE_REGISTRY.contains_hash(sha256_json(envelope)):
            raise PromptExecutionError(
                "Capability acceptance rejected an unattested input envelope. "
                "Replay/sample/direct payloads are not consumable; rebuild context from persisted project material."
            )

        run_id = new_id("run")
        route = None
        resolved_call_key = call_key or new_id("call-pending")
        system_prompt = None
        output_schema: dict[str, Any] | None = None
        raw_response_text: str | None = None
        provider_output: dict[str, Any] | None = None
        consumed_output: dict[str, Any] | None = None
        result = None
        try:
            input_errors = self.pack.validate(prompt_id, "input", envelope)
            if input_errors:
                raise PromptExecutionError("Input schema validation failed", validation_errors=input_errors)
            if model_envelope is not envelope:
                compact_errors = self.pack.validate(prompt_id, "input", model_envelope)
                if compact_errors:
                    raise PromptExecutionError("Compacted model input schema validation failed", validation_errors=compact_errors)
            route = self.router.route(prompt_id, model_envelope, original_environment=original_environment)
            output_schema = self.pack.inlined_schema(prompt_id, "output")
            system_prompt = self._system_prompt(prompt_id, output_schema)
            prompt_version = str(model_envelope.get("prompt_version") or self.pack.entry(prompt_id).get("version") or "")
            resolved_call_key = self._call_key(
                prompt_id=prompt_id,
                prompt_version=prompt_version,
                project_id=project_id,
                workflow_id=workflow_id,
                input_hash=input_hash,
                model_id=route.model_id,
                endpoint_id=route.endpoint_id,
                provider_model_name=str(route.provider_model_name or ""),
                requested_call_key=call_key,
            )
            committed = self._committed_result(
                resolved_call_key,
                project_id=project_id,
                workflow_id=workflow_id,
                prompt_id=prompt_id,
                prompt_version=prompt_version,
                input_hash=input_hash,
                model_id=route.model_id,
                endpoint_id=route.endpoint_id,
                provider_model_name=str(route.provider_model_name or ""),
            )
            if committed:
                return committed
            project_config = load_project_config(self.db, project_id)
            if route.environment == "ONLINE_PUBLIC":
                assert_online_payload_safe(model_envelope, project_config)
            if getattr(self.gateway, "supports_runtime_evidence", False):
                result = await self.gateway.invoke(
                    route,
                    prompt_id,
                    system_prompt,
                    model_envelope,
                    output_schema,
                    call_key=resolved_call_key,
                )
            else:
                result = await self.gateway.invoke(route, prompt_id, system_prompt, model_envelope, output_schema)
            raw_response_text = result.raw_text
            provider_output = copy.deepcopy(result.output)
            consumed_output = copy.deepcopy(provider_output)

            if prompt_id == "P-SAFE-ONLINE-PACKAGE":
                sanitized, redactions = sanitize_safe_online_package(copy.deepcopy(consumed_output), project_config)
                if self.policy.enabled and redactions:
                    raise CapabilityModeError(
                        "Capability acceptance refuses post-generation semantic redaction; the model output must already satisfy the outbound policy."
                    )
                consumed_output = sanitized
                if redactions:
                    consumed_output.setdefault("warnings", []).append(
                        f"Deterministic outbound privacy guard redacted {len(redactions)} sensitive field occurrence(s)."
                    )
            if self.quality_guard_enabled:
                guarded = self.quality_guard.apply(prompt_id, quality_context_envelope, copy.deepcopy(consumed_output))
                self.policy.assert_output_unchanged(consumed_output, guarded, stage="proposal_quality_guard")
                consumed_output = guarded
            output_errors = self.pack.validate(prompt_id, "output", consumed_output)
            if output_errors:
                raise PromptExecutionError("Output schema validation failed", validation_errors=output_errors)
            status = consumed_output.get("status", "ERROR")
            duration_ms = int((time.perf_counter() - started) * 1000)
            self.evidence_store.faults.hit("before_db_transaction", resolved_call_key, prompt_id=prompt_id)
            self._commit_success(
                run_id=run_id,
                call_key=resolved_call_key,
                project_id=project_id,
                workflow_id=workflow_id,
                prompt_id=prompt_id,
                status=status,
                model_id=result.model_id,
                endpoint_id=result.endpoint_id,
                route_model_id=route.model_id,
                route_endpoint_id=route.endpoint_id,
                provider_model_name=str(route.provider_model_name or ""),
                input_hash=input_hash,
                model_envelope=model_envelope,
                consumed_output=consumed_output,
                provider_output=provider_output,
                raw_response_text=raw_response_text,
                system_prompt=system_prompt,
                output_schema=output_schema,
                environment=route.environment,
                duration_ms=duration_ms,
                quality_context_envelope=quality_context_envelope if input_compaction else None,
                input_compaction=input_compaction,
                evidence=getattr(result, "evidence", {}),
                generation_origin=getattr(result, "generation_origin", MODEL_GENERATED),
                source_call_key=getattr(result, "source_call_key", None),
            )
            self.evidence_store.faults.hit("after_db_transaction", resolved_call_key, prompt_id=prompt_id)
            if prompt_id.endswith("CRITIC"):
                self.evidence_store.faults.hit("after_critic_commit", resolved_call_key, prompt_id=prompt_id)
            if (model_envelope.get("payload") or {}).get("revision_findings"):
                self.evidence_store.faults.hit("after_repair_commit", resolved_call_key, prompt_id=prompt_id)
            self.evidence_store.mark_committed(
                resolved_call_key,
                {
                    "run_id": run_id,
                    "prompt_id": prompt_id,
                    "project_id": project_id,
                    "workflow_id": workflow_id,
                    "input_sha256": input_hash,
                    "output_sha256": sha256_json(consumed_output),
                    "prompt_version": prompt_version,
                    "model_id": result.model_id,
                    "endpoint_id": result.endpoint_id,
                    "route_model_id": route.model_id,
                    "route_endpoint_id": route.endpoint_id,
                    "provider_model_name": str(route.provider_model_name or ""),
                    "generation_origin": getattr(result, "generation_origin", MODEL_GENERATED),
                },
            )
            return {
                "run_id": run_id,
                "prompt_id": prompt_id,
                "status": status,
                "route": {
                    "environment": route.environment,
                    "model_id": result.model_id,
                    "endpoint_id": result.endpoint_id,
                    "configured_model_id": route.model_id,
                    "configured_endpoint_id": route.endpoint_id,
                    "provider_model_name": str(route.provider_model_name or ""),
                },
                "output": consumed_output,
                "call_key": resolved_call_key,
                "reused_committed_result": False,
                "generation_origin": getattr(result, "generation_origin", MODEL_GENERATED),
                "source_call_key": getattr(result, "source_call_key", None),
            }
        except InjectedFailure as exc:
            raise RecoverablePromptExecutionError(str(exc)) from exc
        except (PromptExecutionError, RoutingDenied, OutboundPrivacyError, LLMError, CapabilityModeError, EvidenceIntegrityError, KeyError, ValueError) as exc:
            duration_ms = int((time.perf_counter() - started) * 1000)
            details = getattr(exc, "validation_errors", [])
            error = str(exc) + ((" | " + "; ".join(details[:20])) if details else "")
            self._commit_error(
                run_id=run_id,
                call_key=resolved_call_key,
                project_id=project_id,
                workflow_id=workflow_id,
                prompt_id=prompt_id,
                model_id=getattr(result, "model_id", None) or (route.model_id if route else None),
                endpoint_id=getattr(result, "endpoint_id", None) or (route.endpoint_id if route else None),
                route_model_id=route.model_id if route else None,
                route_endpoint_id=route.endpoint_id if route else None,
                provider_model_name=str(route.provider_model_name or "") if route else None,
                input_hash=input_hash,
                model_envelope=model_envelope,
                provider_output=provider_output,
                raw_response_text=raw_response_text,
                system_prompt=system_prompt,
                output_schema=output_schema,
                environment=route.environment if route else None,
                duration_ms=duration_ms,
                error=error,
                quality_context_envelope=quality_context_envelope if input_compaction else None,
                input_compaction=input_compaction,
                evidence=getattr(result, "evidence", {}) if result else {},
                generation_origin=getattr(result, "generation_origin", None) if result else None,
                source_call_key=getattr(result, "source_call_key", None) if result else None,
            )
            raise PromptExecutionError(error, validation_errors=details) from exc

    def _next_version(self, conn, project_id: str, prompt_id: str, artifact_type: str) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(version),0) FROM artifacts WHERE project_id=? AND prompt_id=? AND artifact_type=?",
            (project_id, prompt_id, artifact_type),
        ).fetchone()
        return int(row[0]) + 1

    def _trace_payload(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "prompt_id": kwargs["prompt_id"],
            "version": kwargs["version"],
            "status": kwargs["status"],
            "duration_ms": kwargs["duration_ms"],
            "environment": kwargs.get("environment"),
            "model_id": kwargs.get("model_id"),
            "endpoint_id": kwargs.get("endpoint_id"),
            "configured_model_id": kwargs.get("route_model_id"),
            "configured_endpoint_id": kwargs.get("route_endpoint_id"),
            "provider_model_name": kwargs.get("provider_model_name"),
            "system_prompt": kwargs.get("system_prompt"),
            "input_envelope": kwargs["model_envelope"],
            "quality_context_envelope": kwargs.get("quality_context_envelope"),
            "quality_context_hash": sha256_json(kwargs["quality_context_envelope"]) if kwargs.get("quality_context_envelope") is not None else None,
            "input_compaction": kwargs.get("input_compaction"),
            "output_schema": kwargs.get("output_schema"),
            "output": kwargs.get("consumed_output"),
            "provider_parsed_output": kwargs.get("provider_output"),
            "raw_response_text": kwargs.get("raw_response_text"),
            "error": kwargs.get("error"),
            "call_key": kwargs["call_key"],
            "input_sha256": kwargs["input_hash"],
            "provider_object_sha256": sha256_json(kwargs["provider_output"]) if kwargs.get("provider_output") is not None else None,
            "consumed_object_sha256": sha256_json(kwargs["consumed_output"]) if kwargs.get("consumed_output") is not None else None,
            "original_response_immutable": True,
            "capability_acceptance_mode": self.policy.enabled,
            "model_call_evidence": kwargs.get("evidence") or {},
            "generation_mode": self.generation_mode.value,
            "generation_origin": kwargs.get("generation_origin") or MODEL_GENERATED,
            "source_call_key": kwargs.get("source_call_key"),
        }

    def _commit_success(self, **kwargs: Any) -> None:
        security_level = kwargs["model_envelope"].get("security_context", {}).get("input_max_security_level", "INTERNAL")
        context_hash = sha256_json(kwargs["model_envelope"])
        with self.db.connection() as conn:
            output_version = self._next_version(conn, kwargs["project_id"], kwargs["prompt_id"], "PROMPT_OUTPUT")
            trace_version = self._next_version(conn, kwargs["project_id"], kwargs["prompt_id"], "PROMPT_TRACE")
            conn.execute(
                """INSERT INTO prompt_runs(id,project_id,workflow_id,prompt_id,status,model_id,endpoint_id,input_hash,output_hash,input_json,output_json,error,duration_ms,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    kwargs["run_id"], kwargs["project_id"], kwargs["workflow_id"], kwargs["prompt_id"], kwargs["status"],
                    kwargs["model_id"], kwargs["endpoint_id"], kwargs["input_hash"], sha256_json(kwargs["consumed_output"]),
                    json.dumps(kwargs["model_envelope"], ensure_ascii=False), json.dumps(kwargs["consumed_output"], ensure_ascii=False),
                    None, kwargs["duration_ms"], utc_now(),
                ),
            )
            conn.execute(
                """INSERT INTO artifacts(id,project_id,workflow_id,artifact_type,prompt_id,version,status,security_level,context_hash,content_json,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    new_id("artifact"), kwargs["project_id"], kwargs["workflow_id"], "PROMPT_OUTPUT", kwargs["prompt_id"],
                    output_version, kwargs["status"], security_level, context_hash,
                    json.dumps(kwargs["consumed_output"], ensure_ascii=False), utc_now(),
                ),
            )
            trace = self._trace_payload(version=trace_version, error=None, **kwargs)
            conn.execute(
                """INSERT INTO artifacts(id,project_id,workflow_id,artifact_type,prompt_id,version,status,security_level,context_hash,content_json,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    new_id("artifact"), kwargs["project_id"], kwargs["workflow_id"], "PROMPT_TRACE", kwargs["prompt_id"],
                    trace_version, kwargs["status"], security_level, context_hash, json.dumps(trace, ensure_ascii=False), utc_now(),
                ),
            )
            conn.execute(
                "INSERT INTO audit_events(project_id,event_type,object_id,metadata_json,created_at) VALUES(?,?,?,?,?)",
                (
                    kwargs["project_id"], "MODEL_CALL_COMMITTED", kwargs["call_key"],
                    json.dumps(
                        {
                            "run_id": kwargs["run_id"], "prompt_id": kwargs["prompt_id"], "workflow_id": kwargs["workflow_id"],
                            "environment": kwargs.get("environment"), "input_hash": kwargs["input_hash"],
                            "output_hash": sha256_json(kwargs["consumed_output"]),
                            "model_id": kwargs.get("model_id"),
                            "endpoint_id": kwargs.get("endpoint_id"),
                            "route_model_id": kwargs.get("route_model_id"),
                            "route_endpoint_id": kwargs.get("route_endpoint_id"),
                            "provider_model_name": kwargs.get("provider_model_name"),
                            "prompt_version": (kwargs.get("model_envelope") or {}).get("prompt_version"),
                            "generation_mode": self.generation_mode.value,
                            "generation_origin": kwargs.get("generation_origin") or MODEL_GENERATED,
                            "source_call_key": kwargs.get("source_call_key"),
                        },
                        ensure_ascii=False,
                    ),
                    utc_now(),
                ),
            )

    def _commit_error(self, **kwargs: Any) -> None:
        security_level = kwargs["model_envelope"].get("security_context", {}).get("input_max_security_level", "INTERNAL")
        context_hash = sha256_json(kwargs["model_envelope"])
        try:
            with self.db.connection() as conn:
                trace_version = self._next_version(conn, kwargs["project_id"], kwargs["prompt_id"], "PROMPT_TRACE")
                conn.execute(
                    """INSERT INTO prompt_runs(id,project_id,workflow_id,prompt_id,status,model_id,endpoint_id,input_hash,output_hash,input_json,output_json,error,duration_ms,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        kwargs["run_id"], kwargs["project_id"], kwargs["workflow_id"], kwargs["prompt_id"], "ERROR",
                        kwargs.get("model_id"), kwargs.get("endpoint_id"), kwargs["input_hash"],
                        sha256_json(kwargs["provider_output"]) if kwargs.get("provider_output") is not None else None,
                        json.dumps(kwargs["model_envelope"], ensure_ascii=False),
                        json.dumps(kwargs["provider_output"], ensure_ascii=False) if kwargs.get("provider_output") is not None else None,
                        kwargs["error"], kwargs["duration_ms"], utc_now(),
                    ),
                )
                trace = self._trace_payload(
                    version=trace_version,
                    status="ERROR",
                    consumed_output=None,
                    **kwargs,
                )
                conn.execute(
                    """INSERT INTO artifacts(id,project_id,workflow_id,artifact_type,prompt_id,version,status,security_level,context_hash,content_json,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        new_id("artifact"), kwargs["project_id"], kwargs["workflow_id"], "PROMPT_TRACE", kwargs["prompt_id"],
                        trace_version, "ERROR", security_level, context_hash, json.dumps(trace, ensure_ascii=False), utc_now(),
                    ),
                )
                conn.execute(
                    "INSERT INTO audit_events(project_id,event_type,object_id,metadata_json,created_at) VALUES(?,?,?,?,?)",
                    (
                        kwargs["project_id"], "MODEL_CALL_FAILED", kwargs["call_key"],
                        json.dumps({"run_id": kwargs["run_id"], "prompt_id": kwargs["prompt_id"], "error": kwargs["error"]}, ensure_ascii=False),
                        utc_now(),
                    ),
                )
        except Exception:
            # The original execution error remains authoritative. A secondary evidence
            # write failure must not hide it or manufacture a successful run.
            pass
