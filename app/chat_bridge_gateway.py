from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from .generation_mode import CHECKPOINT_RESPONSE_REUSE, MODEL_GENERATED
from .g3_runtime_gateway import G3AuditedModelGateway
from .llm import _extract_json
from .runtime_evidence import EvidenceIntegrityError
from .runtime_gateway import RuntimeLLMResult
from .util import new_id, sha256_json, utc_now


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


class ChatBridgeModelGateway(G3AuditedModelGateway):
    """Durable file bridge from the workflow to the current ChatGPT conversation.

    The workflow persists its normal model-call evidence first, then writes an additional
    bridge request. A response file is consumed unchanged, validated, hashed and committed
    by the existing runtime. No Replay, Mock, SimulatedLLM or HTTP model endpoint is used.
    """

    async def invoke(
        self,
        route,
        prompt_id: str,
        system_prompt: str,
        envelope: dict[str, Any],
        output_schema: dict[str, Any],
        *,
        call_key: str | None = None,
    ) -> RuntimeLLMResult:
        call_key = call_key or new_id("call")
        # A workflow retry must reproduce exactly the request object that was first
        # persisted for this call key. In particular, a fresh wall-clock timestamp
        # would make an otherwise identical request fail the evidence hash check.
        existing_request_path, _existing_meta_path = self.evidence_store.request_paths(call_key)
        requested_at = utc_now()
        if existing_request_path.exists():
            try:
                existing_request = json.loads(existing_request_path.read_text(encoding="utf-8"))
                requested_at = str(existing_request.get("requested_at") or requested_at)
            except (OSError, json.JSONDecodeError, TypeError):
                pass
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
            "bridge_type": "CHATGPT_CONVERSATION_FILE_BRIDGE",
            "requested_at": requested_at,
        }

        self.evidence_store.faults.hit("before_request_persist", call_key, prompt_id=prompt_id)
        request_meta = self.evidence_store.write_request(call_key, request_payload)
        self.evidence_store.faults.hit("after_request_persist", call_key, prompt_id=prompt_id)

        bridge_root = Path(os.environ["CHAT_BRIDGE_DIR"]).resolve()
        requests_dir = bridge_root / "model_requests"
        responses_dir = bridge_root / "model_responses"
        requests_dir.mkdir(parents=True, exist_ok=True)
        responses_dir.mkdir(parents=True, exist_ok=True)
        request_path = requests_dir / f"{call_key}.json"
        response_path = responses_dir / f"{call_key}.json"
        _atomic_json(request_path, request_payload)

        # G3's evaluator expects one auditable directory per call in addition to the
        # frozen Track-A evidence layout.
        call_dir = self.evidence_store.root / call_key
        _atomic_json(call_dir / "request.json", request_payload)

        if self.evidence_store.has_response(call_key):
            if not self.generation_mode.allow_reuse:
                raise EvidenceIntegrityError(
                    f"Fresh generation refused pre-existing bridge response: {call_key}"
                )
            verified = self.evidence_store.load_verified_response(call_key)
            return RuntimeLLMResult(
                output=verified.parsed_output,
                raw_text=verified.raw_text,
                model_id=str(verified.metadata.get("model_id") or route.model_id),
                endpoint_id=str(verified.metadata.get("endpoint_id") or route.endpoint_id),
                call_key=call_key,
                evidence={**request_meta, **verified.metadata},
                reused_response=True,
                generation_origin=CHECKPOINT_RESPONSE_REUSE,
                source_call_key=call_key,
            )

        timeout_seconds = float(os.getenv("CHAT_BRIDGE_TIMEOUT_SECONDS", "86400"))
        poll_seconds = max(0.05, float(os.getenv("CHAT_BRIDGE_POLL_SECONDS", "0.2")))
        started = asyncio.get_running_loop().time()
        while not response_path.exists():
            if asyncio.get_running_loop().time() - started > timeout_seconds:
                raise TimeoutError(f"CHAT_BRIDGE_TIMEOUT:{call_key}:{prompt_id}")
            await asyncio.sleep(poll_seconds)

        payload = json.loads(response_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise EvidenceIntegrityError(f"Bridge response must be an object: {call_key}")
        output = payload.get("output", payload)
        if not isinstance(output, dict):
            raise EvidenceIntegrityError(f"Bridge output must be an object: {call_key}")
        raw_text = str(payload.get("raw_text") or json.dumps(output, ensure_ascii=False))
        raw_parsed = _extract_json(raw_text)
        if sha256_json(raw_parsed) != sha256_json(output):
            raise EvidenceIntegrityError(f"Bridge raw text and parsed output differ: {call_key}")
        model_id = str(payload.get("model_id") or "").strip()
        endpoint_id = str(payload.get("endpoint_id") or "").strip()
        if not model_id or not endpoint_id:
            raise EvidenceIntegrityError(
                "Bridge response must declare the actual model_id and endpoint_id"
            )
        response_meta = self.evidence_store.write_response(
            call_key,
            raw_text=raw_text,
            parsed_output=output,
            raw_parsed_output=raw_parsed,
            metadata={
                "prompt_id": prompt_id,
                "runtime_mode": self.settings.runtime_mode,
                "environment": route.environment,
                "model_id": model_id,
                "endpoint_id": endpoint_id,
                "request_sha256": request_meta["request_sha256"],
                "bridge_response_path": str(response_path),
                "bridge_type": "CHATGPT_CONVERSATION_FILE_BRIDGE",
            },
        )
        _atomic_json(
            call_dir / "response.json",
            {
                "call_key": call_key,
                "prompt_id": prompt_id,
                "model_id": model_id,
                "endpoint_id": endpoint_id,
                "raw_text": raw_text,
                "output": output,
                "metadata": response_meta,
            },
        )
        return RuntimeLLMResult(
            output=output,
            raw_text=raw_text,
            model_id=model_id,
            endpoint_id=endpoint_id,
            call_key=call_key,
            evidence={**request_meta, **response_meta},
            reused_response=False,
            generation_origin=MODEL_GENERATED,
            source_call_key=None,
        )
