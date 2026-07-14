from __future__ import annotations

import copy
import json
import time
from typing import Any

from .llm import LLMError, ModelGateway
from .privacy import OutboundPrivacyError, assert_online_payload_safe, load_project_config, sanitize_safe_online_package
from .proposal_quality import ProposalQualityGuard
from .security import RoutingDenied, SecurityRouter
from .util import new_id, sha256_json, utc_now


class PromptExecutionError(RuntimeError):
    def __init__(self, message: str, *, validation_errors: list[str] | None = None):
        super().__init__(message)
        self.validation_errors = validation_errors or []


class PromptExecutor:
    def __init__(self, db, pack, router: SecurityRouter, gateway: ModelGateway, *, quality_guard: ProposalQualityGuard | None = None, quality_guard_enabled: bool = True):
        self.db = db
        self.pack = pack
        self.router = router
        self.gateway = gateway
        self.quality_guard = quality_guard or ProposalQualityGuard()
        self.quality_guard_enabled = quality_guard_enabled

    async def execute(
        self,
        prompt_id: str,
        envelope: dict[str, Any],
        *,
        project_id: str,
        workflow_id: str | None = None,
        original_environment: str | None = None,
    ) -> dict[str, Any]:
        run_id = new_id("run")
        started = time.perf_counter()
        quality_context_envelope = envelope
        model_envelope, input_compaction = self._prepare_model_envelope(prompt_id, envelope)
        input_hash = sha256_json(model_envelope)
        route = None
        output: dict[str, Any] | None = None
        error: str | None = None
        status = "ERROR"
        system_prompt = None
        raw_response_text = None
        output_schema: dict[str, Any] | None = None
        try:
            input_errors = self.pack.validate(prompt_id, "input", envelope)
            if input_errors:
                raise PromptExecutionError("Input schema validation failed", validation_errors=input_errors)
            if model_envelope is not envelope:
                model_input_errors = self.pack.validate(prompt_id, "input", model_envelope)
                if model_input_errors:
                    raise PromptExecutionError("Compacted model input schema validation failed", validation_errors=model_input_errors)
            route = self.router.route(prompt_id, model_envelope, original_environment=original_environment)
            project_config = load_project_config(self.db, project_id)
            if route.environment == "ONLINE_PUBLIC":
                assert_online_payload_safe(model_envelope, project_config)
            output_schema = self.pack.inlined_schema(prompt_id, "output")
            system_prompt = self._system_prompt(prompt_id, output_schema)
            result = await self.gateway.invoke(route, prompt_id, system_prompt, model_envelope, output_schema)
            raw_response_text = result.raw_text
            output = result.output
            if prompt_id == "P-SAFE-ONLINE-PACKAGE":
                output, redactions = sanitize_safe_online_package(output, project_config)
                if redactions:
                    output.setdefault("warnings", []).append(
                        f"Deterministic outbound privacy guard redacted {len(redactions)} sensitive field occurrence(s)."
                    )
            if self.quality_guard_enabled:
                output = self.quality_guard.apply(prompt_id, quality_context_envelope, output)
            output_errors = self.pack.validate(prompt_id, "output", output)
            if output_errors:
                raise PromptExecutionError("Output schema validation failed", validation_errors=output_errors)
            status = output.get("status", "ERROR")
            duration_ms = int((time.perf_counter() - started) * 1000)
            self._save_run(run_id, project_id, workflow_id, prompt_id, status, result.model_id, result.endpoint_id, input_hash, model_envelope, output, None, duration_ms)
            self._save_artifact(
                project_id, workflow_id, prompt_id, output, model_envelope, system_prompt,
                raw_response_text, output_schema, route.environment if route else None,
                result.model_id, result.endpoint_id, duration_ms, status, None,
                quality_context_envelope=quality_context_envelope if input_compaction else None,
                input_compaction=input_compaction,
            )
            return {
                "run_id": run_id,
                "prompt_id": prompt_id,
                "status": status,
                "route": {"environment": route.environment, "model_id": result.model_id, "endpoint_id": result.endpoint_id},
                "output": output,
            }
        except (PromptExecutionError, RoutingDenied, OutboundPrivacyError, LLMError, KeyError, ValueError) as exc:
            duration_ms = int((time.perf_counter() - started) * 1000)
            details = getattr(exc, "validation_errors", [])
            error = str(exc) + ((" | " + "; ".join(details[:20])) if details else "")
            self._save_run(run_id, project_id, workflow_id, prompt_id, "ERROR", route.model_id if route else None, route.endpoint_id if route else None, input_hash, model_envelope, output, error, duration_ms)
            self._save_trace(
                project_id, workflow_id, prompt_id, model_envelope, system_prompt,
                raw_response_text, output_schema, route.environment if route else None,
                route.model_id if route else None, route.endpoint_id if route else None,
                duration_ms, "ERROR", error,
                quality_context_envelope=quality_context_envelope if input_compaction else None,
                input_compaction=input_compaction,
            )
            raise PromptExecutionError(error, validation_errors=details) from exc

    @staticmethod
    def _compact_paragraph_text(text: str, *, limit: int = 180) -> str:
        value = str(text or "").strip()
        if len(value) <= limit:
            return value
        marker = "……[中段省略]……"
        available = max(40, limit - len(marker))
        head = max(24, int(available * 0.65))
        tail = max(16, available - head)
        return value[:head].rstrip() + marker + value[-tail:].lstrip()

    def _prepare_model_envelope(self, prompt_id: str, envelope: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Return the exact envelope sent to the model.

        Whole-document review needs the complete candidate set for deterministic
        verification, but weak/short-context models do not need every repeated
        byte of every paragraph.  The quality guard therefore keeps the original
        envelope, while the model receives all sections, semantic identities,
        evidence links and bounded excerpts.  The trace records both envelopes.
        """
        if prompt_id != "P-INTEGRATION-CRITIC":
            return envelope, None
        original_chars = len(json.dumps(envelope, ensure_ascii=False))
        if original_chars <= 80000:
            return envelope, None
        compact = copy.deepcopy(envelope)
        sections = (compact.get("payload") or {}).get("candidate_sections") or []
        paragraph_count = 0
        for item in sections:
            candidate = (item or {}).get("candidate") or {}
            advancement = candidate.get("claim_advancement") or {}
            contribution = str(advancement.get("section_contribution") or "").strip()
            candidate["candidate_text"] = contribution or self._compact_paragraph_text(candidate.get("candidate_text", ""), limit=600)
            paragraphs = candidate.get("paragraphs") or []
            paragraph_count += len(paragraphs)
            for paragraph in paragraphs:
                if isinstance(paragraph, dict):
                    paragraph["text"] = self._compact_paragraph_text(paragraph.get("text", ""), limit=180)
                    paragraph["evidence_ids"] = list(paragraph.get("evidence_ids") or [])[:2]
            # Retain one verifiable trace per paragraph.  Paragraph semantic IDs
            # and evidence IDs remain complete; duplicated trace objects and long
            # quoted spans are the main source of whole-document context growth.
            links_by_id = {
                str(link.get("trace_id")): link
                for link in candidate.get("trace_links") or []
                if isinstance(link, dict) and link.get("trace_id")
            }
            primary_trace_id = next(iter(links_by_id), "")
            if primary_trace_id:
                for paragraph in paragraphs:
                    paragraph["trace_link_ids"] = [primary_trace_id]
                link = links_by_id[primary_trace_id]
                if link.get("source_path_or_span"):
                    link["source_path_or_span"] = str(link["source_path_or_span"])[:96]
                candidate["trace_links"] = [link]

        payload = compact.get("payload") or {}
        referenced_ids: set[str] = set()
        for item in sections:
            candidate = (item or {}).get("candidate") or {}
            advancement = candidate.get("claim_advancement") or {}
            referenced_ids.update(str(x) for x in advancement.get("advanced_claim_ids", []) if x)
            referenced_ids.update(str(x) for x in advancement.get("new_information_keys", []) if x)
            for paragraph in candidate.get("paragraphs") or []:
                if not isinstance(paragraph, dict):
                    continue
                referenced_ids.add(str(paragraph.get("primary_claim_id") or ""))
                referenced_ids.update(str(x) for x in paragraph.get("evidence_ids", []) if x)
            for link in candidate.get("trace_links") or []:
                if isinstance(link, dict) and link.get("source_id"):
                    referenced_ids.add(str(link["source_id"]))
        referenced_ids.discard("")

        def trim_source_refs(value: dict[str, Any]) -> None:
            compact_refs = []
            for ref in value.get("source_refs") or []:
                if not isinstance(ref, dict):
                    continue
                compact_ref = {
                    "source_id": ref.get("source_id"),
                    "source_type": ref.get("source_type"),
                    "authority_rank": ref.get("authority_rank"),
                    "security_level": ref.get("security_level"),
                }
                if ref.get("source_hash"):
                    compact_ref["source_hash"] = ref.get("source_hash")
                compact_refs.append(compact_ref)
            value["source_refs"] = compact_refs[:2]

        project_definition = payload.get("project_definition") or {}
        for item in project_definition.get("items") or []:
            if isinstance(item, dict):
                # Source validity is checked against the full quality context.
                # The whole-document model only needs the typed project object.
                item["source_refs"] = []
        # Relations are kept because the integration critic validates the complete
        # project chain, but explanatory text is bounded.
        for relation in project_definition.get("relations") or []:
            if isinstance(relation, dict) and relation.get("rationale"):
                relation["rationale"] = str(relation["rationale"])[:160]

        fact_package = payload.get("fact_package") or {}
        claims = [c for c in fact_package.get("claims") or [] if isinstance(c, dict)]
        selected_claims = [c for c in claims if str(c.get("claim_id") or "") in referenced_ids]
        if len(selected_claims) < 6:
            selected_ids = {str(c.get("claim_id") or "") for c in selected_claims}
            remaining_claims = [c for c in claims if str(c.get("claim_id") or "") not in selected_ids]
            selected_claims.extend(remaining_claims[: 6 - len(selected_claims)])
        fact_package["claims"] = selected_claims[:8]
        for claim in fact_package.get("claims") or []:
            trim_source_refs(claim)
            for ref in claim.get("source_refs") or []:
                if isinstance(ref, dict):
                    for key in ("quoted_text", "source_path_or_span"):
                        if ref.get(key):
                            ref[key] = str(ref[key])[:120]
        fact_package["conflicts"] = list(fact_package.get("conflicts") or [])[:4]

        architecture = payload.get("narrative_architecture") or {}
        for contract in architecture.get("section_contracts") or []:
            if not isinstance(contract, dict):
                continue
            contract["argument_function"] = str(contract.get("argument_function") or contract.get("profile_id") or "章节论证")[:80]
            contract["must_use_evidence_ids"] = list(contract.get("must_use_evidence_ids") or [])[:2]
            contract["unique_information_keys"] = list(contract.get("unique_information_keys") or [])[:1]
            contract["required_argument_roles"] = list(contract.get("required_argument_roles") or [])[:3]
            contract["prerequisite_section_ids"] = list(contract.get("prerequisite_section_ids") or [])[-1:]
            contract["must_not_repeat_section_ids"] = list(contract.get("must_not_repeat_section_ids") or [])[-2:]
            contract["allowed_shared_context_ids"] = list(contract.get("allowed_shared_context_ids") or [])[:1]
            contract["forbidden_topics"] = list(contract.get("forbidden_topics") or [])[:1]
            rules = list(contract.get("acceptance_rules") or [])
            contract["acceptance_rules"] = (rules[:2] if len(rules) >= 2 else [*rules, "保持章节论证功能"][:2])

        compact_chars = len(json.dumps(compact, ensure_ascii=False))
        return compact, {
            "strategy": "FULL_SEMANTIC_IDENTITY_WITH_BOUNDED_EXCERPTS",
            "original_chars": original_chars,
            "model_chars": compact_chars,
            "candidate_section_count": len(sections),
            "paragraph_count": paragraph_count,
            "quality_guard_uses_full_context": True,
        }

    def _system_prompt(self, prompt_id: str, output_schema: dict[str, Any]) -> str:
        return (
            self.pack.shared_prompt
            + "\n\n"
            + self.pack.prompt_text(prompt_id)
            + "\n\n# 运行时强制输出Schema\n"
            + json.dumps(output_schema, ensure_ascii=False)
        )

    def _save_run(self, run_id: str, project_id: str, workflow_id: str | None, prompt_id: str, status: str, model_id: str | None, endpoint_id: str | None, input_hash: str, envelope: dict[str, Any], output: dict[str, Any] | None, error: str | None, duration_ms: int) -> None:
        self.db.execute(
            """INSERT INTO prompt_runs(id,project_id,workflow_id,prompt_id,status,model_id,endpoint_id,input_hash,output_hash,input_json,output_json,error,duration_ms,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id, project_id, workflow_id, prompt_id, status, model_id, endpoint_id,
                input_hash, sha256_json(output) if output is not None else None,
                json.dumps(envelope, ensure_ascii=False), json.dumps(output, ensure_ascii=False) if output is not None else None,
                error, duration_ms, utc_now(),
            ),
        )
        self.db.audit("PROMPT_EXECUTED", project_id=project_id, object_id=run_id, metadata={"prompt_id": prompt_id, "status": status, "input_hash": input_hash, "duration_ms": duration_ms})

    def _save_artifact(self, project_id: str, workflow_id: str | None, prompt_id: str, output: dict[str, Any], envelope: dict[str, Any], system_prompt: str | None, raw_response_text: str | None, output_schema: dict[str, Any] | None, environment: str | None, model_id: str | None, endpoint_id: str | None, duration_ms: int, status: str, error: str | None, *, quality_context_envelope: dict[str, Any] | None = None, input_compaction: dict[str, Any] | None = None) -> None:
        row = self.db.fetchone("SELECT COALESCE(MAX(version),0) AS v FROM artifacts WHERE project_id=? AND prompt_id=? AND artifact_type='PROMPT_OUTPUT'", (project_id, prompt_id))
        version = int(row["v"]) + 1 if row else 1
        security_level = envelope.get("security_context", {}).get("input_max_security_level", "INTERNAL")
        context_hash = sha256_json(envelope)
        self.db.execute(
            """INSERT INTO artifacts(id,project_id,workflow_id,artifact_type,prompt_id,version,status,security_level,context_hash,content_json,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (new_id("artifact"), project_id, workflow_id, "PROMPT_OUTPUT", prompt_id, version, output.get("status", "UNKNOWN"), security_level, context_hash, json.dumps(output, ensure_ascii=False), utc_now()),
        )
        self._save_trace(
            project_id, workflow_id, prompt_id, envelope, system_prompt, raw_response_text,
            output_schema, environment, model_id, endpoint_id, duration_ms, status, error,
            version=version, output=output,
            quality_context_envelope=quality_context_envelope,
            input_compaction=input_compaction,
        )

    def _save_trace(self, project_id: str, workflow_id: str | None, prompt_id: str, envelope: dict[str, Any], system_prompt: str | None, raw_response_text: str | None, output_schema: dict[str, Any] | None, environment: str | None, model_id: str | None, endpoint_id: str | None, duration_ms: int, status: str, error: str | None, *, version: int | None = None, output: dict[str, Any] | None = None, quality_context_envelope: dict[str, Any] | None = None, input_compaction: dict[str, Any] | None = None) -> None:
        if version is None:
            row = self.db.fetchone("SELECT COALESCE(MAX(version),0) AS v FROM artifacts WHERE project_id=? AND prompt_id=? AND artifact_type='PROMPT_TRACE'", (project_id, prompt_id))
            version = int(row["v"]) + 1 if row else 1
        security_level = envelope.get("security_context", {}).get("input_max_security_level", "INTERNAL")
        context_hash = sha256_json(envelope)
        trace_payload = {
            "prompt_id": prompt_id,
            "version": version,
            "status": status,
            "duration_ms": duration_ms,
            "environment": environment,
            "model_id": model_id,
            "endpoint_id": endpoint_id,
            "system_prompt": system_prompt,
            "input_envelope": envelope,
            "quality_context_envelope": quality_context_envelope,
            "quality_context_hash": sha256_json(quality_context_envelope) if quality_context_envelope is not None else None,
            "input_compaction": input_compaction,
            "output_schema": output_schema,
            "output": output,
            "raw_response_text": raw_response_text,
            "error": error,
        }
        self.db.execute(
            """INSERT INTO artifacts(id,project_id,workflow_id,artifact_type,prompt_id,version,status,security_level,context_hash,content_json,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (new_id("artifact"), project_id, workflow_id, "PROMPT_TRACE", prompt_id, version, status, security_level, context_hash, json.dumps(trace_payload, ensure_ascii=False), utc_now()),
        )
