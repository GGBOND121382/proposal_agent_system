from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any

import httpx

from .security import Route


class LLMError(RuntimeError):
    pass


@dataclass
class LLMResult:
    output: dict[str, Any]
    raw_text: str
    model_id: str
    endpoint_id: str


def _extract_json(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.I)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end <= start:
            raise LLMError("Model response does not contain a JSON object")
        value = json.loads(stripped[start : end + 1])
    if not isinstance(value, dict):
        raise LLMError("Model response JSON must be an object")
    return value


class ModelGateway:
    def __init__(self, settings, pack):
        self.settings = settings
        self.pack = pack

    async def invoke(self, route: Route, prompt_id: str, system_prompt: str, envelope: dict[str, Any], output_schema: dict[str, Any]) -> LLMResult:
        mode = self.settings.runtime_mode
        if mode in {"REPLAY", "MOCK"}:
            output = self.pack.replay_output(prompt_id, "normal")
            if mode == "MOCK":
                output.setdefault("warnings", []).append("MOCK模式：输出来自静态样例，不代表真实模型质量。")
            return LLMResult(output=output, raw_text=json.dumps(output, ensure_ascii=False), model_id=f"{mode.lower()}-provider", endpoint_id="local-static")
        return await self._invoke_live(route, prompt_id, system_prompt, envelope, output_schema)

    async def _invoke_live(self, route: Route, prompt_id: str, system_prompt: str, envelope: dict[str, Any], output_schema: dict[str, Any]) -> LLMResult:
        endpoint = route.endpoint
        base_url = str(endpoint.get("base_url") or "").rstrip("/")
        if not base_url:
            raise LLMError(f"Endpoint {route.endpoint_id} has no base_url")
        secret_name = endpoint.get("api_key_secret")
        api_key = os.getenv(str(secret_name), "") if secret_name else ""
        if not route.provider_model_name:
            raise LLMError(f"Model {route.model_id} provider_model_name is empty")

        request = {
            "model": route.provider_model_name,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(envelope, ensure_ascii=False)},
            ],
            "temperature": route.profile.get("temperature", 0.0),
            "max_tokens": route.profile.get("max_output_tokens", 7000),
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": re.sub(r"[^A-Za-z0-9_]", "_", prompt_id),
                    "strict": True,
                    "schema": output_schema,
                },
            },
        }
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        timeout = httpx.Timeout(self.settings.request_timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(f"{base_url}/chat/completions", headers=headers, json=request)
            if response.status_code >= 400 and response.status_code in {400, 404, 422}:
                request["response_format"] = {"type": "json_object"}
                response = await client.post(f"{base_url}/chat/completions", headers=headers, json=request)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                body = response.text[:1000]
                raise LLMError(f"LLM endpoint returned {response.status_code}: {body}") from exc
            payload = response.json()
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("Invalid OpenAI-compatible response structure") from exc
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        output = _extract_json(str(content))
        return LLMResult(output=output, raw_text=str(content), model_id=route.model_id, endpoint_id=route.endpoint_id)
