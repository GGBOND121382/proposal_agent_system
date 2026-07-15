from __future__ import annotations

import copy
import json
import os
import time
from pathlib import Path
from typing import Any

from .executor import PromptExecutionError, PromptExecutor as BasePromptExecutor
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
        store = getattr(gateway, "evidence_store", None)
        if store is None:
            root = Path(os.getenv("MODEL_CALL_EVIDENCE_DIR", "data/model_calls")).resolve()
            store = ModelCallEvidenceStore(root)
        self.evidence_store = store

    def _call_key(
        self,
        *,
        prompt_id: str,
        project_id: str,
        workflow_id: str | None,
        input_hash: str,
        requested_call_key: str | None,
    ) -> str:
        if requested_call_key:
            return requested_call_key
        if workflow_id:
            return "call-" + sha256_json(
                {
                    "workflow_id": workflow_id,
                    "prompt_id": prompt_id,
                    "input_hash": input_hash,
                }
            )[:32]
        return new_id("call")

    def _committed_result(self, call_key: str) -> dict[str, Any] | None:
        event = self.db.fetchone(
            "SELECT metadata_json FROM audit_events WHERE event_type='MODEL_CALL_COMMITTED' AND object_id=? ORDER BY id DESC LIMIT 1",
            (call_key,),
        )
        if not event:
            return None
        metadata = json.loads(event["metadata_json"])
        run = self.db.fetchone("SELECT * FROM prompt_runs WHERE id=?", (metadata.get("run_id"),))
        if not run or not run.get("output_json"):
            raise EvidenceIntegrityError(f"Committed call {call_key} has no matching prompt run")
        output = json.loads(run["output_json"])
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
        call_key = self._call_key(
            prompt_id=prompt_id,
            project_id=project_id,
            workflow_id=workflow_id,
            input_hash=input_hash,
            requested_call_key=call_key,
        )
        committed = self._committed_result(call_key)
        if committed:
            return committed
        if self.policy.enabled and not LIVE_ENVELOPE_REGISTRY.contains_hash(sha256_json(envelope)):
            raise PromptExecutionError(
                "Capability acceptance rejected an unattested input envelope. "
                "Replay/sample/direct payloads are not consumable; rebuild context from persisted project material."
            )

        run_id = new_id("run")
        route = None
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
            project_config = load_project_config(self.db, project_id)
            if route.environment == "ONLINE_PUBLIC":
                assert_online_payload_safe(model_envelope, project_config)
            output_schema = self.pack.inlined_schema(prompt_id, "output")
            system_prompt = self._system_prompt(prompt_id, output_schema)
            if getattr(self.gateway, "supports_runtime_evidence", False):
                result = await self.gateway.invoke(
                    route,
                    prompt_id,
                    system_prompt,
                    model_envelope,
                    output_schema,
                    call_key=call_key,
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
            self.evidence_store.faults.hit("before_db_transaction", call_key, prompt_id=prompt_id)
            self._commit_success(
                run_id=run_id,
                call_key=call_key,
                project_id=project_id,
                workflow_id=workflow_id,
                prompt_id=prompt_id,
                status=status,
                model_id=result.model_id,
                endpoint_id=result.endpoint_id,
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
            )
            self.evidence_store.faults.hit("after_db_transaction", call_key, prompt_id=prompt_id)
            if prompt_id.endswith("CRITIC"):
                self.evidence_store.faults.hit("after_critic_commit", call_key, prompt_id=prompt_id)
            if (model_envelope.get("payload") or {}).get("revision_findings"):
                self.evidence_store.faults.hit("after_repair_commit", call_key, prompt_id=prompt_id)
            self.evidence_store.mark_committed(
                call_key,
                {
                    "run_id": run_id,
                    "prompt_id": prompt_id,
                    "project_id": project_id,
                    "workflow_id": workflow_id,
                    "input_sha256": input_hash,
                    "output_sha256": sha256_json(consumed_output),
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
                },
                "output": consumed_output,
                "call_key": call_key,
                "reused_committed_result": False,
            }
        except InjectedFailure as exc:
            raise RecoverablePromptExecutionError(str(exc)) from exc
        except (PromptExecutionError, RoutingDenied, OutboundPrivacyError, LLMError, CapabilityModeError, EvidenceIntegrityError, KeyError, ValueError) as exc:
            duration_ms = int((time.perf_counter() - started) * 1000)
            details = getattr(exc, "validation_errors", [])
            error = str(exc) + ((" | " + "; ".join(details[:20])) if details else "")
            self._commit_error(
                run_id=run_id,
                call_key=call_key,
                project_id=project_id,
                workflow_id=workflow_id,
                prompt_id=prompt_id,
                model_id=getattr(result, "model_id", None) or (route.model_id if route else None),
                endpoint_id=getattr(result, "endpoint_id", None) or (route.endpoint_id if route else None),
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
