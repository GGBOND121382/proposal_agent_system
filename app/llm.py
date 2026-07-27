from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from typing import Any

import httpx

from .security import Route
from .simulated_llm import SimulatedLLM


class LLMError(RuntimeError):
    pass


@dataclass
class LLMResult:
    output: dict[str, Any]
    raw_text: str
    model_id: str
    endpoint_id: str


def _loads_with_local_json_repairs(candidate: str) -> Any:
    """Repair a bounded set of local JSON punctuation errors.

    MiniMax occasionally emits an otherwise complete JSON object with a missing
    comma, an unescaped quote inside a string, a raw control character, or a
    trailing comma. Repairs are localized at the parser-reported position and
    never synthesize missing fields or close a truncated object.
    """
    repaired = candidate
    # Long MiniMax schema responses can contain dozens of independent,
    # mechanically recoverable punctuation slips. Keep the repair set narrow,
    # but allow enough iterations to process a complete long-form critic
    # response instead of failing after the 32nd local comma.
    for _ in range(256):
        try:
            return json.loads(repaired)
        except json.JSONDecodeError as exc:
            pos = exc.pos
            changed = False
            if exc.msg == "Expecting ',' delimiter" and pos < len(repaired):
                if repaired[pos] == '"':
                    repaired = repaired[:pos] + "," + repaired[pos:]
                    changed = True
                elif repaired[pos] in "[{":
                    # A common long-response failure is two complete array/object
                    # members emitted back-to-back: `}{` or `][`.  The parser
                    # points at the opening delimiter of the second value.
                    previous = pos - 1
                    while previous >= 0 and repaired[previous].isspace():
                        previous -= 1
                    if previous >= 0 and (
                        repaired[previous] in '}"\']'
                        or repaired[previous].isdigit()
                    ):
                        repaired = repaired[:pos] + "," + repaired[pos:]
                        changed = True
                elif re.match(r"[-0-9tfn]", repaired[pos]):
                    # Likewise recover a missing comma before a scalar value,
                    # without attempting to synthesize or close any value.
                    previous = pos - 1
                    while previous >= 0 and repaired[previous].isspace():
                        previous -= 1
                    if previous >= 0 and (
                        repaired[previous] in '}"\']'
                        or repaired[previous].isdigit()
                    ):
                        repaired = repaired[:pos] + "," + repaired[pos:]
                        changed = True
                else:
                    quote = pos - 1
                    while quote >= 0 and repaired[quote].isspace():
                        quote -= 1
                    if quote >= 0 and repaired[quote] == '"':
                        repaired = repaired[:quote] + '\\"' + repaired[quote + 1 :]
                        changed = True
            elif exc.msg.startswith("Invalid control character") and pos < len(repaired):
                replacement = {
                    "\n": "\\n",
                    "\r": "\\r",
                    "\t": "\\t",
                }.get(repaired[pos])
                if replacement:
                    repaired = repaired[:pos] + replacement + repaired[pos + 1 :]
                    changed = True
            elif exc.msg == "Expecting property name enclosed in double quotes" and pos < len(repaired):
                if repaired[pos] in "}]":
                    comma = pos - 1
                    while comma >= 0 and repaired[comma].isspace():
                        comma -= 1
                    if comma >= 0 and repaired[comma] == ",":
                        repaired = repaired[:comma] + repaired[comma + 1 :]
                        changed = True
                elif repaired[pos] == ",":
                    repaired = repaired[:pos] + repaired[pos + 1 :]
                    changed = True
                elif repaired[pos] == "'":
                    closing = repaired.find("'", pos + 1)
                    colon = repaired.find(":", pos + 1)
                    if closing >= 0 and colon > closing:
                        repaired = (
                            repaired[:pos]
                            + '"'
                            + repaired[pos + 1 : closing]
                            + '"'
                            + repaired[closing + 1 :]
                        )
                        changed = True
                elif re.match(r"[A-Za-z_]", repaired[pos]):
                    colon = repaired.find(":", pos + 1)
                    if colon > pos:
                        key = repaired[pos:colon].strip()
                        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", key):
                            repaired = repaired[:pos] + json.dumps(key) + repaired[colon:]
                            changed = True
            elif exc.msg == "Expecting value" and pos < len(repaired):
                if repaired[pos] in "]}":
                    comma = pos - 1
                    while comma >= 0 and repaired[comma].isspace():
                        comma -= 1
                    if comma >= 0 and repaired[comma] == ",":
                        repaired = repaired[:comma] + repaired[comma + 1 :]
                        changed = True
            if not changed:
                raise
    raise json.JSONDecodeError("local JSON repair limit exceeded", repaired, 0)


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
        try:
            value = _loads_with_local_json_repairs(stripped[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMError(
                f"Model response contains malformed JSON at character {exc.pos}: {exc.msg}"
            ) from exc
    if not isinstance(value, dict):
        raise LLMError("Model response JSON must be an object")
    return value


class ModelGateway:
    def __init__(self, settings, pack):
        self.settings = settings
        self.pack = pack
        self.simulator = SimulatedLLM(pack)

    async def invoke(self, route: Route, prompt_id: str, system_prompt: str, envelope: dict[str, Any], output_schema: dict[str, Any]) -> LLMResult:
        mode = self.settings.runtime_mode
        if mode in {"REPLAY", "MOCK"}:
            output = self.pack.replay_output(prompt_id, "normal")
            if mode == "MOCK":
                output.setdefault("warnings", []).append("MOCK模式：输出来自静态样例，不代表真实模型质量。")
            return LLMResult(output=output, raw_text=json.dumps(output, ensure_ascii=False), model_id=f"{mode.lower()}-provider", endpoint_id="local-static")
        if mode == "SIMULATED":
            output = self.simulator.invoke(prompt_id, envelope)
            output.setdefault("warnings", []).append("SIMULATED模式：输出由本地确定性智能体模拟器生成，用于端到端测试与审计。")
            return LLMResult(output=output, raw_text=json.dumps(output, ensure_ascii=False), model_id="simulated-provider", endpoint_id="local-simulated")
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
        is_minimax = self._is_minimax(base_url, route.provider_model_name)
        if is_minimax:
            request["response_format"] = {"type": "json_object"}
            request["reasoning_split"] = True
            request["stream"] = True
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    if is_minimax:
                        try:
                            async with asyncio.timeout(self.settings.request_timeout_seconds):
                                content = await self._stream_chat_completion(
                                    client,
                                    f"{base_url}/chat/completions",
                                    headers,
                                    request,
                                )
                        except TimeoutError as exc:
                            raise LLMError(
                                f"LLM stream exceeded the total timeout of {self.settings.request_timeout_seconds} seconds"
                            ) from exc
                        try:
                            output = _extract_json(content)
                        except LLMError as exc:
                            if attempt < 2:
                                await asyncio.sleep(2 ** attempt)
                                continue
                            raise LLMError(
                                f"MiniMax returned malformed JSON after 3 attempts: {exc}"
                            ) from exc
                        return LLMResult(
                            output=output,
                            raw_text=content,
                            model_id=route.model_id,
                            endpoint_id=route.endpoint_id,
                        )

                    response = await client.post(f"{base_url}/chat/completions", headers=headers, json=request)
                    if response.status_code >= 400 and response.status_code in {400, 404, 422}:
                        request["response_format"] = {"type": "json_object"}
                        response = await client.post(f"{base_url}/chat/completions", headers=headers, json=request)
                    try:
                        response.raise_for_status()
                    except httpx.HTTPStatusError as exc:
                        body = response.text[:1000]
                        raise LLMError(f"LLM endpoint returned {response.status_code}: {body}") from exc
                    try:
                        payload = response.json()
                    except (json.JSONDecodeError, ValueError) as exc:
                        raise LLMError("LLM endpoint returned a non-JSON response") from exc
                break
            except httpx.RequestError as exc:
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
                    continue
                detail = str(exc).strip() or repr(exc)
                raise LLMError(
                    f"LLM transport failed after 3 attempts ({type(exc).__name__}): {detail}"
                ) from exc
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("Invalid OpenAI-compatible response structure") from exc
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        output = _extract_json(str(content))
        return LLMResult(output=output, raw_text=str(content), model_id=route.model_id, endpoint_id=route.endpoint_id)

    @staticmethod
    def _is_minimax(base_url: str, provider_model_name: str) -> bool:
        return "minimax" in base_url.lower() or provider_model_name.lower().startswith("minimax-")

    async def _stream_chat_completion(
        self,
        client: httpx.AsyncClient,
        url: str,
        headers: dict[str, str],
        request: dict[str, Any],
    ) -> str:
        content = ""
        finish_reason: str | None = None
        async with client.stream("POST", url, headers=headers, json=request) as response:
            if response.status_code >= 400:
                body = (await response.aread()).decode("utf-8", errors="replace")[:1000]
                raise LLMError(f"LLM endpoint returned {response.status_code}: {body}")
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    event = json.loads(data)
                    choice = (event.get("choices") or [{}])[0]
                    delta = choice.get("delta") or {}
                    piece = delta.get("content") or ""
                    message = choice.get("message") or {}
                    if not piece and message.get("content"):
                        piece = message["content"]
                    finish_reason = choice.get("finish_reason") or finish_reason
                except (json.JSONDecodeError, AttributeError, IndexError, TypeError) as exc:
                    raise LLMError("LLM stream returned an invalid event") from exc
                if isinstance(piece, list):
                    piece = "".join(
                        part.get("text", "")
                        for part in piece
                        if isinstance(part, dict)
                    )
                piece = str(piece)
                if not piece:
                    continue
                # MiniMax may emit cumulative content while other compatible
                # providers emit token deltas. Support both without duplication.
                if piece.startswith(content):
                    content = piece
                else:
                    content += piece
        if not content:
            raise LLMError("LLM stream completed without message content")
        if finish_reason == "length":
            raise LLMError("LLM stream reached the output token limit before completing")
        return content
