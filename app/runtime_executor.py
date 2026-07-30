from __future__ import annotations

import copy
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .executor import (
    OUTPUT_NORMALIZER_VERSION,
    PromptExecutionError,
    PromptExecutor as BasePromptExecutor,
)
from .contract_registry import CONTRACT_REGISTRY_VERSION
from .llm import LLMError
from .privacy import OutboundPrivacyError, assert_online_payload_safe, load_project_config, sanitize_safe_online_package
from .output_integrity import TRUSTED_SOURCE_CATALOG_VERSION, attach_trusted_source_catalog
from .runtime_evidence import EvidenceIntegrityError, InjectedFailure, ModelCallEvidenceStore
from .runtime_policy import CapabilityModeError, CapabilityPolicy, LIVE_ENVELOPE_REGISTRY
from .security import RoutingDenied
from .util import new_id, sha256_json, utc_now


class RecoverablePromptExecutionError(PromptExecutionError):
    recoverable = True


class RuntimePromptExecutor(BasePromptExecutor):
    """Atomic, idempotent prompt executor backed by durable model-call evidence."""

    output_normalizer_version = OUTPUT_NORMALIZER_VERSION
    contract_registry_version = CONTRACT_REGISTRY_VERSION

    def __init__(self, db, pack, router, gateway, *, quality_guard=None, quality_guard_enabled: bool = True):
        if quality_guard is None:
            from .full_integration_quality import FullProposalQualityGuard
            quality_guard = FullProposalQualityGuard()
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

    def _model_request_spec(self, prompt_id: str) -> dict[str, Any]:
        """Return the provider-visible request contract for one prompt.

        This deliberately excludes local normalizer versions.  A failed
        provider object may be re-consumed after a normalizer upgrade only when
        the prompt text, model profile, registry entry, and output schema that
        produced it are unchanged.
        """
        try:
            entry = self.pack.entry(prompt_id)
            profile_name = entry.get("model_profile")
            profiles = self.pack.profiles.get("profiles") or self.pack.profiles
            return {
                "prompt_text": self.pack.prompt_text(prompt_id),
                "prompt_entry": entry,
                "model_profile": profiles.get(profile_name),
                "output_schema": self.pack.inlined_schema(prompt_id, "output"),
                "trusted_source_catalog_contract_version": TRUSTED_SOURCE_CATALOG_VERSION,
            }
        except (AttributeError, KeyError, TypeError):
            return {"prompt_id": prompt_id}

    def _model_request_spec_hash(self, prompt_id: str) -> str:
        return sha256_json(self._model_request_spec(prompt_id))

    def _execution_spec_hash(self, prompt_id: str) -> str:
        return sha256_json({
            "model_request_spec_hash": self._model_request_spec_hash(prompt_id),
            "output_normalizer_version": OUTPUT_NORMALIZER_VERSION,
            "contract_registry_version": CONTRACT_REGISTRY_VERSION,
        })

    def _call_key(
        self,
        *,
        prompt_id: str,
        project_id: str,
        workflow_id: str | None,
        input_hash: str,
        requested_call_key: str | None,
    ) -> str:
        del project_id  # retained for API compatibility and future tenancy salt
        if requested_call_key:
            return requested_call_key
        if workflow_id:
            return "call-" + sha256_json(
                {
                    "workflow_id": workflow_id,
                    "prompt_id": prompt_id,
                    "input_hash": input_hash,
                    "execution_spec_hash": self._execution_spec_hash(prompt_id),
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

    @staticmethod
    def _is_deterministic_contract_failure(error: Any) -> bool:
        message = str(error or "").strip()
        return message.startswith((
            "Output schema validation failed",
            "Output container structure validation failed",
            "Misplaced response-envelope field",
            "Output provenance is not backed by the trusted input envelope",
            "Untrusted source reference in Safe Online Package output",
            "Output reference integrity validation failed",
        ))

    @staticmethod
    def _contract_recovery_input_equivalent(
        prompt_id: str,
        prior_envelope: dict[str, Any],
        current_envelope: dict[str, Any],
    ) -> tuple[bool, str | None]:
        """Allow only explicitly registered, non-semantic input migrations.

        The WF-3 provenance upgrade adds persisted ``human_resolutions`` to an
        otherwise identical Safe Package request.  Older successful provider
        output may be reused because the research question, source objects,
        security policy, task and every model-visible semantic field are
        unchanged.  No other input difference is accepted.
        """
        if sha256_json(prior_envelope) == sha256_json(current_envelope):
            return True, "EXACT"

        prior = copy.deepcopy(prior_envelope)
        current = copy.deepcopy(current_envelope)
        prior_had_catalog = "trusted_source_catalog" in prior
        current_had_catalog = "trusted_source_catalog" in current
        prior.pop("trusted_source_catalog", None)
        current.pop("trusted_source_catalog", None)
        if sha256_json(prior) == sha256_json(current):
            if prior_had_catalog != current_had_catalog:
                return True, "ADDITIVE_TRUSTED_SOURCE_CATALOG"
            return True, "TRUSTED_SOURCE_CATALOG_REBUILD"

        if prompt_id != "P-SAFE-ONLINE-PACKAGE":
            return False, None
        prior_payload = prior.get("payload") if isinstance(prior.get("payload"), dict) else {}
        current_payload = current.get("payload") if isinstance(current.get("payload"), dict) else {}
        prior_payload.pop("human_resolutions", None)
        current_payload.pop("human_resolutions", None)
        if sha256_json(prior) == sha256_json(current):
            suffix = "_AND_TRUSTED_SOURCE_CATALOG" if prior_had_catalog != current_had_catalog else ""
            return True, "ADDITIVE_HUMAN_RESOLUTIONS" + suffix
        return False, None

    def _failed_run_audit_metadata(
        self,
        *,
        project_id: str,
        run_id: str,
    ) -> dict[str, Any]:
        """Load machine-readable metadata for a failed run when available."""
        rows = self.db.fetchall(
            """SELECT metadata_json FROM audit_events
               WHERE project_id=? AND event_type='MODEL_CALL_FAILED'
               ORDER BY id DESC LIMIT 200""",
            (project_id,),
        )
        for row in rows:
            try:
                metadata = json.loads(row.get("metadata_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            if str(metadata.get("run_id") or "") == run_id:
                return metadata
        return {}

    def _recoverable_contract_output(
        self,
        *,
        project_id: str,
        workflow_id: str | None,
        prompt_id: str,
        input_hash: str,
        model_envelope: dict[str, Any],
        quality_context_envelope: dict[str, Any],
        project_config: dict[str, Any],
        model_request_spec_hash: str,
    ) -> dict[str, Any] | None:
        """Return a prior provider object that passes the current contract.

        A successful model response must not be resent merely because an older
        normalizer, schema adapter, or deterministic quality rule rejected it.
        Recovery is limited to the identical project/workflow/prompt/input
        identity.  The immutable provider object is replayed through the entire
        current deterministic consumption pipeline; it is reusable only when
        normalization, privacy handling, quality guards, and strict output
        validation all pass now.
        """
        rows = self.db.fetchall(
            """SELECT id,model_id,endpoint_id,input_hash,input_json,output_json,error,created_at
               FROM prompt_runs
               WHERE project_id=? AND workflow_id IS ? AND prompt_id=?
                 AND status='ERROR' AND output_json IS NOT NULL
               ORDER BY created_at DESC
               LIMIT 50""",
            (project_id, workflow_id, prompt_id),
        )
        for row in rows:
            input_equivalence = "EXACT"
            if str(row.get("input_hash") or "") != input_hash:
                try:
                    prior_envelope = json.loads(row.get("input_json") or "{}")
                except (TypeError, json.JSONDecodeError):
                    continue
                if not isinstance(prior_envelope, dict):
                    continue
                equivalent, input_equivalence = self._contract_recovery_input_equivalent(
                    prompt_id,
                    prior_envelope,
                    model_envelope,
                )
                if not equivalent:
                    continue
            if not self._is_deterministic_contract_failure(row.get("error")):
                continue
            failed_metadata = self._failed_run_audit_metadata(
                project_id=project_id,
                run_id=str(row["id"]),
            )
            metadata_recoverable = failed_metadata.get("deterministic_recoverable")
            metadata_normalizer_version = str(
                failed_metadata.get("output_normalizer_version") or ""
            )
            # A prior runtime may have classified this exact deterministic
            # contract failure as non-recoverable before the corresponding
            # normalizer/binder existed.  Such a negative decision is stale
            # after a normalizer upgrade.  A negative decision emitted by the
            # current normalizer remains authoritative.  In every allowed
            # migration case the immutable provider object is still replayed
            # through the complete current contract, privacy, quality and
            # schema pipeline below; no failed output is accepted merely on
            # the basis of this metadata.
            if (
                metadata_recoverable is False
                and metadata_normalizer_version == OUTPUT_NORMALIZER_VERSION
            ):
                continue
            prior_request_hash = str(
                failed_metadata.get("model_request_spec_hash") or ""
            )
            request_contract_upgrade = None
            if prior_request_hash and prior_request_hash != model_request_spec_hash:
                provenance_failure = str(row.get("error") or "").startswith((
                    "Output provenance is not backed by the trusted input envelope",
                    "Untrusted source reference in Safe Online Package output",
                ))
                if provenance_failure and "TRUSTED_SOURCE_CATALOG" in str(input_equivalence or ""):
                    request_contract_upgrade = "TRUSTED_SOURCE_CATALOG_V1"
                else:
                    continue
            try:
                provider_output = json.loads(row["output_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(provider_output, dict):
                continue
            try:
                consumed_output = self._normalize_output(
                    prompt_id,
                    copy.deepcopy(provider_output),
                    model_envelope,
                )
                self.policy.assert_output_unchanged(
                    provider_output,
                    consumed_output,
                    stage="output_normalization",
                )
                if prompt_id == "P-SAFE-ONLINE-PACKAGE":
                    sanitized, redactions = sanitize_safe_online_package(
                        copy.deepcopy(consumed_output),
                        project_config,
                    )
                    if self.policy.enabled and redactions:
                        continue
                    consumed_output = sanitized
                    if redactions:
                        consumed_output.setdefault("warnings", []).append(
                            f"Deterministic outbound privacy guard redacted {len(redactions)} sensitive field occurrence(s)."
                        )
                if self.quality_guard_enabled:
                    guarded = self.quality_guard.apply(
                        prompt_id,
                        quality_context_envelope,
                        copy.deepcopy(consumed_output),
                    )
                    self.policy.assert_output_unchanged(
                        consumed_output,
                        guarded,
                        stage="proposal_quality_guard",
                    )
                    consumed_output = guarded
                if self.pack.validate(prompt_id, "output", consumed_output):
                    continue
            except (
                PromptExecutionError,
                CapabilityModeError,
                KeyError,
                TypeError,
                ValueError,
            ):
                continue
            return {
                "run_id": str(row["id"]),
                "model_id": row.get("model_id"),
                "endpoint_id": row.get("endpoint_id"),
                "provider_output": provider_output,
                "consumed_output": consumed_output,
                "failed_at": row.get("created_at"),
                "previous_error": row.get("error"),
                "prior_model_request_spec_hash": prior_request_hash or None,
                "input_equivalence": input_equivalence,
                "request_contract_upgrade": request_contract_upgrade,
            }
        return None

    # Backward-compatible private entry point retained for downstream tests and
    # integrations that imported the old enum-only helper.
    def _recoverable_enum_output(self, **kwargs: Any) -> dict[str, Any] | None:
        return self._recoverable_contract_output(**kwargs)

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
        model_envelope = attach_trusted_source_catalog(model_envelope)
        input_hash = sha256_json(model_envelope)
        model_request_spec_hash = self._model_request_spec_hash(prompt_id)
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
            system_prompt = self._system_prompt(prompt_id, output_schema, model_envelope)
            contract_recovery = self._recoverable_contract_output(
                project_id=project_id,
                workflow_id=workflow_id,
                prompt_id=prompt_id,
                input_hash=input_hash,
                model_envelope=model_envelope,
                quality_context_envelope=quality_context_envelope,
                project_config=project_config,
                model_request_spec_hash=model_request_spec_hash,
            )
            if contract_recovery is not None:
                result = SimpleNamespace(
                    output=copy.deepcopy(contract_recovery["provider_output"]),
                    raw_text=None,
                    model_id=contract_recovery.get("model_id") or route.model_id,
                    endpoint_id=contract_recovery.get("endpoint_id") or route.endpoint_id,
                    evidence={
                        "recovery_kind": "CONTRACT_RENORMALIZATION",
                        "recovered_from_run_id": contract_recovery["run_id"],
                        "failed_at": contract_recovery.get("failed_at"),
                        "previous_error": contract_recovery.get("previous_error"),
                        "input_equivalence": contract_recovery.get("input_equivalence"),
                        "output_normalizer_version": OUTPUT_NORMALIZER_VERSION,
                        "contract_registry_version": CONTRACT_REGISTRY_VERSION,
                        "model_request_spec_hash": model_request_spec_hash,
                    },
                    reused_response=True,
                )
            elif getattr(self.gateway, "supports_runtime_evidence", False):
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
            consumed_output = (
                copy.deepcopy(contract_recovery["consumed_output"])
                if contract_recovery is not None
                else self._normalize_output(prompt_id, provider_output, model_envelope)
            )
            self.policy.assert_output_unchanged(
                provider_output,
                consumed_output,
                stage="output_normalization",
            )
            parse_report = dict(getattr(result, "parse_report", {}) or {})
            repair_count = int(parse_report.get("repair_count") or 0)
            code_fence_removed = bool(parse_report.get("code_fence_removed"))
            surrounding_text_removed = bool(parse_report.get("surrounding_text_removed"))
            parse_adjusted = repair_count or code_fence_removed or surrounding_text_removed
            if parse_adjusted:
                if self.policy.enabled:
                    raise CapabilityModeError(
                        "Capability acceptance requires provider-native valid JSON without "
                        "code-fence stripping, surrounding-text extraction, or local syntax repair; "
                        f"repairs={repair_count}, code_fence_removed={code_fence_removed}, "
                        f"surrounding_text_removed={surrounding_text_removed}."
                    )
                repair_kinds = sorted(
                    {str(item.get("kind") or "UNKNOWN") for item in parse_report.get("repairs") or []}
                )
                consumed_output.setdefault("warnings", []).append(
                    "SYSTEM_JSON_PARSE_NORMALIZATION: provider response required "
                    f"repairs={repair_count}"
                    f" ({', '.join(repair_kinds) if repair_kinds else 'none'}), "
                    f"code_fence_removed={code_fence_removed}, "
                    f"surrounding_text_removed={surrounding_text_removed}; "
                    "the immutable raw response and detailed parse report are retained in model-call evidence"
                )

            if contract_recovery is None and prompt_id == "P-SAFE-ONLINE-PACKAGE":
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
            if contract_recovery is None and self.quality_guard_enabled:
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
                model_request_spec_hash=model_request_spec_hash,
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
                "contract_recovered_from_run_id": (
                    contract_recovery["run_id"] if contract_recovery is not None else None
                ),
            }
        except InjectedFailure as exc:
            raise RecoverablePromptExecutionError(str(exc)) from exc
        except (PromptExecutionError, RoutingDenied, OutboundPrivacyError, LLMError, CapabilityModeError, EvidenceIntegrityError, KeyError, ValueError) as exc:
            duration_ms = int((time.perf_counter() - started) * 1000)
            details = getattr(exc, "validation_errors", [])
            error = str(exc) + ((" | " + "; ".join(details[:20])) if details else "")
            persistence_error = self._commit_error(
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
            if persistence_error:
                error += " | ERROR_EVIDENCE_PERSISTENCE_FAILED: " + persistence_error
            raise PromptExecutionError(error, validation_errors=details) from exc

    def _next_version(self, conn, project_id: str, prompt_id: str, artifact_type: str) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(version),0) FROM artifacts WHERE project_id=? AND prompt_id=? AND artifact_type=?",
            (project_id, prompt_id, artifact_type),
        ).fetchone()
        return int(row[0]) + 1

    @staticmethod
    def _normalization_audit(
        provider_output: Any,
        consumed_output: Any,
        *,
        limit: int = 256,
    ) -> dict[str, Any]:
        """Return a bounded path-level diff between provider and consumed JSON."""
        changes: list[dict[str, Any]] = []
        truncated = False

        def pointer(path: tuple[Any, ...]) -> str:
            if not path:
                return "/"
            return "/" + "/".join(
                str(token).replace("~", "~0").replace("/", "~1")
                for token in path
            )

        def record(operation: str, path: tuple[Any, ...], before: Any, after: Any) -> None:
            nonlocal truncated
            if len(changes) >= limit:
                truncated = True
                return
            changes.append({
                "operation": operation,
                "path": pointer(path),
                "before_type": type(before).__name__ if before is not None else "null",
                "after_type": type(after).__name__ if after is not None else "null",
                "before_sha256": sha256_json(before) if before is not None else None,
                "after_sha256": sha256_json(after) if after is not None else None,
            })

        def walk(before: Any, after: Any, path: tuple[Any, ...]) -> None:
            nonlocal truncated
            if truncated:
                return
            if type(before) is not type(after):
                record("REPLACE", path, before, after)
                return
            if isinstance(before, dict):
                before_keys = set(before)
                after_keys = set(after)
                for key in sorted(before_keys - after_keys):
                    record("REMOVE", (*path, key), before[key], None)
                for key in sorted(after_keys - before_keys):
                    record("ADD", (*path, key), None, after[key])
                for key in sorted(before_keys & after_keys):
                    walk(before[key], after[key], (*path, key))
                return
            if isinstance(before, list):
                common = min(len(before), len(after))
                for index in range(common):
                    walk(before[index], after[index], (*path, index))
                for index in range(common, len(before)):
                    record("REMOVE", (*path, index), before[index], None)
                for index in range(common, len(after)):
                    record("ADD", (*path, index), None, after[index])
                return
            if before != after:
                record("REPLACE", path, before, after)

        if provider_output is not None and consumed_output is not None:
            walk(provider_output, consumed_output, ())
        return {
            "schema_version": "1.0",
            "changed": bool(changes),
            "change_count": len(changes),
            "truncated": truncated,
            "changes": changes,
        }

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
            "model_request_spec_hash": kwargs.get("model_request_spec_hash"),
            "output_normalizer_version": OUTPUT_NORMALIZER_VERSION,
            "contract_registry_version": CONTRACT_REGISTRY_VERSION,
            "provider_object_sha256": sha256_json(kwargs["provider_output"]) if kwargs.get("provider_output") is not None else None,
            "consumed_object_sha256": sha256_json(kwargs["consumed_output"]) if kwargs.get("consumed_output") is not None else None,
            "original_response_immutable": True,
            "capability_acceptance_mode": self.policy.enabled,
            "model_call_evidence": kwargs.get("evidence") or {},
            "normalization_audit": self._normalization_audit(
                kwargs.get("provider_output"),
                kwargs.get("consumed_output"),
            ),
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
                            "model_request_spec_hash": kwargs.get("model_request_spec_hash"),
                            "output_normalizer_version": OUTPUT_NORMALIZER_VERSION,
                            "contract_registry_version": CONTRACT_REGISTRY_VERSION,
                        },
                        ensure_ascii=False,
                    ),
                    utc_now(),
                ),
            )

    def _commit_error(self, **kwargs: Any) -> str | None:
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
                        json.dumps({
                            "run_id": kwargs["run_id"],
                            "prompt_id": kwargs["prompt_id"],
                            "error": kwargs["error"],
                            "deterministic_recoverable": self._is_deterministic_contract_failure(kwargs["error"]),
                            "model_request_spec_hash": kwargs.get("model_request_spec_hash"),
                            "output_normalizer_version": OUTPUT_NORMALIZER_VERSION,
                            "contract_registry_version": CONTRACT_REGISTRY_VERSION,
                        }, ensure_ascii=False),
                        utc_now(),
                    ),
                )
        except Exception as evidence_exc:
            # The original execution error remains authoritative, but silently
            # discarding a failed error record makes deterministic recovery
            # impossible. Surface a bounded secondary diagnostic without
            # manufacturing a successful run or replacing the primary error.
            detail = f"{type(evidence_exc).__name__}: {evidence_exc}"
            return detail[:1000]
        return None
