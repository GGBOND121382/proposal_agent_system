from __future__ import annotations

import asyncio
import json
import os
import re
import time
from typing import Any

import httpx

from .llm import LLMError, LLMResult, _extract_json
from .runtime_gateway import AuditedModelGateway
from .security import Route


class G3AuditedModelGateway(AuditedModelGateway):
    """G3-only compatibility controls around the frozen shared gateway."""

    def __init__(self, settings, pack):
        super().__init__(settings, pack)
        self._request_start_lock = asyncio.Lock()
        self._last_request_started = 0.0
        self._endpoint_semaphores: dict[str, asyncio.Semaphore] = {}

    async def _invoke_live(
        self,
        route: Route,
        prompt_id: str,
        system_prompt: str,
        envelope: dict[str, Any],
        output_schema: dict[str, Any],
    ) -> LLMResult:
        endpoint = route.endpoint
        base_url = str(endpoint.get("base_url") or "").rstrip("/")
        if not base_url:
            raise LLMError(f"Endpoint {route.endpoint_id} has no base_url")
        secret_name = endpoint.get("api_key_secret")
        api_key = os.getenv(str(secret_name), "") if secret_name else ""
        if not route.provider_model_name:
            raise LLMError(f"Model {route.model_id} provider_model_name is empty")

        configured_max = int(getattr(self.settings, "model_max_output_tokens", 0) or 0)
        profile_max = int(route.profile.get("max_output_tokens", 7000) or 7000)
        max_tokens = min(profile_max, configured_max) if configured_max > 0 else profile_max
        response_mode = str(
            getattr(self.settings, "model_response_format", "json_schema") or "json_schema"
        ).lower()
        if response_mode not in {"json_schema", "json_object"}:
            raise LLMError("MODEL_RESPONSE_FORMAT must be json_schema or json_object")
        response_format: dict[str, Any]
        if response_mode == "json_object":
            response_format = {"type": "json_object"}
        else:
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": re.sub(r"[^A-Za-z0-9_]", "_", prompt_id),
                    "strict": True,
                    "schema": output_schema,
                },
            }
        request = {
            "model": route.provider_model_name,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(envelope, ensure_ascii=False)},
            ],
            "temperature": route.profile.get("temperature", 0.0),
            "max_tokens": max_tokens,
            "response_format": response_format,
        }
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        timeout = httpx.Timeout(self.settings.request_timeout_seconds)
        concurrency = max(
            1, int((endpoint.get("limits") or {}).get("max_concurrency") or 1)
        )
        semaphore = self._endpoint_semaphores.setdefault(
            route.endpoint_id, asyncio.Semaphore(concurrency)
        )

        async def post(client: httpx.AsyncClient):
            interval = max(
                0.0,
                float(
                    getattr(
                        self.settings, "model_min_request_interval_seconds", 0.0
                    )
                    or 0.0
                ),
            )
            async with self._request_start_lock:
                delay = interval - (time.monotonic() - self._last_request_started)
                if delay > 0:
                    await asyncio.sleep(delay)
                self._last_request_started = time.monotonic()
            return await client.post(
                f"{base_url}/chat/completions", headers=headers, json=request
            )

        async with semaphore:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await post(client)
                if (
                    response_mode == "json_schema"
                    and response.status_code in {400, 404, 422}
                ):
                    request["response_format"] = {"type": "json_object"}
                    response = await post(client)
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise LLMError(
                        f"LLM endpoint returned {response.status_code}: {response.text[:1000]}"
                    ) from exc
                payload = response.json()
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("Invalid OpenAI-compatible response structure") from exc
        if isinstance(content, list):
            content = "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        output = _extract_json(str(content))
        return LLMResult(
            output=output,
            raw_text=str(content),
            model_id=route.model_id,
            endpoint_id=route.endpoint_id,
        )
