from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from .runtime_failures import ProviderFailureKind
from .security import Route
from .simulated_llm import SimulatedLLM


JSON_PARSER_VERSION = "2026-07-29.v1-audited-local-repairs"
MODEL_RESPONSE_PROTOCOL_VERSION = (
    "2026-08-11.v5-minimax-compact-tool-or-json"
)


class LLMError(RuntimeError):
    pass


class ProviderError(LLMError):
    """Typed provider failure preserved through PromptExecutionError causes."""

    def __init__(
        self,
        message: str,
        *,
        kind: ProviderFailureKind,
        http_status: int | None = None,
        retry_after_seconds: float | None = None,
        phase: str | None = None,
        response_excerpt: str | None = None,
        retryable_hint: bool | None = None,
        validation_errors: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.provider_failure_kind = kind
        self.http_status = http_status
        self.retry_after_seconds = retry_after_seconds
        self.provider_phase = phase
        self.response_excerpt = response_excerpt
        self.retryable_hint = retryable_hint
        self.validation_errors = list(validation_errors or [])


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = str(response.headers.get("Retry-After") or "").strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


@dataclass
class LLMResult:
    output: dict[str, Any]
    raw_text: str
    model_id: str
    endpoint_id: str
    parse_report: dict[str, Any] = field(default_factory=dict)
    response_contract_mode: str = "UNSPECIFIED"
    provider_attempts: int = 1
    fallback_reason: str | None = None


def _loads_with_local_json_repairs(
    candidate: str,
    *,
    repair_log: list[dict[str, Any]] | None = None,
) -> Any:
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
            repair_kind = ""
            if exc.msg == "Expecting ',' delimiter" and pos < len(repaired):
                if repaired[pos] == '"':
                    repaired = repaired[:pos] + "," + repaired[pos:]
                    changed = True
                    repair_kind = "INSERT_MISSING_COMMA"
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
                        repair_kind = "INSERT_MISSING_COMMA"
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
                        repair_kind = "INSERT_MISSING_COMMA"
                else:
                    quote = pos - 1
                    while quote >= 0 and repaired[quote].isspace():
                        quote -= 1
                    if quote >= 0 and repaired[quote] == '"':
                        repaired = repaired[:quote] + '\\"' + repaired[quote + 1 :]
                        changed = True
                        repair_kind = "ESCAPE_STRING_QUOTE"
            elif exc.msg.startswith("Invalid control character") and pos < len(repaired):
                replacement = {
                    "\n": "\\n",
                    "\r": "\\r",
                    "\t": "\\t",
                }.get(repaired[pos])
                if replacement:
                    repaired = repaired[:pos] + replacement + repaired[pos + 1 :]
                    changed = True
                    repair_kind = "ESCAPE_CONTROL_CHARACTER"
            elif exc.msg == "Expecting property name enclosed in double quotes" and pos < len(repaired):
                if repaired[pos] in "}]":
                    comma = pos - 1
                    while comma >= 0 and repaired[comma].isspace():
                        comma -= 1
                    if comma >= 0 and repaired[comma] == ",":
                        repaired = repaired[:comma] + repaired[comma + 1 :]
                        changed = True
                        repair_kind = "REMOVE_TRAILING_OR_DUPLICATE_COMMA"
                elif repaired[pos] == ",":
                    repaired = repaired[:pos] + repaired[pos + 1 :]
                    changed = True
                    repair_kind = "REMOVE_DUPLICATE_COMMA"
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
                        repair_kind = "QUOTE_PROPERTY_NAME"
                elif re.match(r"[A-Za-z_]", repaired[pos]):
                    colon = repaired.find(":", pos + 1)
                    if colon > pos:
                        key = repaired[pos:colon].strip()
                        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", key):
                            repaired = repaired[:pos] + json.dumps(key) + repaired[colon:]
                            changed = True
                            repair_kind = "QUOTE_PROPERTY_NAME"
            elif exc.msg == "Expecting value" and pos < len(repaired):
                if repaired[pos] in "]}":
                    comma = pos - 1
                    while comma >= 0 and repaired[comma].isspace():
                        comma -= 1
                    if comma >= 0 and repaired[comma] == ",":
                        repaired = repaired[:comma] + repaired[comma + 1 :]
                        changed = True
                        repair_kind = "REMOVE_TRAILING_OR_DUPLICATE_COMMA"
            if changed and repair_log is not None:
                repair_log.append(
                    {
                        "kind": repair_kind or "LOCAL_JSON_PUNCTUATION_REPAIR",
                        "position": pos,
                        "parser_error": exc.msg,
                    }
                )
            if not changed:
                raise
    raise json.JSONDecodeError("local JSON repair limit exceeded", repaired, 0)


def _extract_json_with_report(text: str) -> tuple[dict[str, Any], dict[str, Any]]:
    stripped = text.strip()
    code_fence_removed = False
    if stripped.startswith("```"):
        code_fence_removed = True
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.I)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        value = json.loads(stripped)
        report = {
            "mode": "STRICT_JSON",
            "repair_count": 0,
            "repairs": [],
            "code_fence_removed": code_fence_removed,
            "surrounding_text_removed": False,
        }
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end <= start:
            raise LLMError("Model response does not contain a JSON object")
        repairs: list[dict[str, Any]] = []
        try:
            value = _loads_with_local_json_repairs(
                stripped[start : end + 1],
                repair_log=repairs,
            )
        except json.JSONDecodeError as exc:
            raise LLMError(
                f"Model response contains malformed JSON at character {exc.pos}: {exc.msg}"
            ) from exc
        report = {
            "mode": "LOCALLY_REPAIRED_JSON" if repairs else "EXTRACTED_JSON",
            "repair_count": len(repairs),
            "repairs": repairs,
            "code_fence_removed": code_fence_removed,
            "surrounding_text_removed": start > 0 or end < len(stripped) - 1,
        }
    if not isinstance(value, dict):
        raise LLMError("Model response JSON must be an object")
    return value, report


def _extract_json(text: str) -> dict[str, Any]:
    value, _ = _extract_json_with_report(text)
    return value


def _load_strict_json_object(text: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load an exact JSON object without extracting or repairing model text.

    A provider response governed by ``json_schema`` is an executable contract,
    not prose that may be recovered heuristically.  Keeping this path strict
    ensures malformed structured output is regenerated by the workflow instead
    of being silently changed after generation.
    """

    stripped = text.strip()
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise LLMError(
            f"Model response contains malformed JSON at character {exc.pos}: {exc.msg}"
        ) from exc
    if not isinstance(value, dict):
        raise LLMError("Model response JSON must be an object")
    return value, {
        "mode": "STRICT_JSON",
        "repair_count": 0,
        "repairs": [],
        "code_fence_removed": False,
        "surrounding_text_removed": False,
    }


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
            return LLMResult(
                output=output,
                raw_text=json.dumps(output, ensure_ascii=False),
                model_id=f"{mode.lower()}-provider",
                endpoint_id="local-static",
                response_contract_mode=f"{mode}_SCHEMA_REPLAY",
            )
        if mode == "SIMULATED":
            output = self.simulator.invoke(prompt_id, envelope)
            output.setdefault("warnings", []).append("SIMULATED模式：输出由本地确定性智能体模拟器生成，用于端到端测试与审计。")
            return LLMResult(
                output=output,
                raw_text=json.dumps(output, ensure_ascii=False),
                model_id="simulated-provider",
                endpoint_id="local-simulated",
                response_contract_mode="SIMULATED_SCHEMA_OUTPUT",
            )
        return await self._invoke_live(route, prompt_id, system_prompt, envelope, output_schema)

    @staticmethod
    def _is_structured_output_rejection(status_code: int, body: str) -> bool:
        """Return whether an endpoint rejected the structured-output feature.

        Do not silently downgrade on unrelated 4xx errors.  In particular, a
        404 usually means the endpoint path/model is wrong and retrying with a
        weaker response format only hides the real configuration problem.
        """
        if status_code not in {400, 422}:
            return False
        text = str(body or "").lower()
        feature_tokens = (
            "response_format",
            "json_schema",
            "json schema",
            "structured output",
            "structured_outputs",
        )
        rejection_tokens = (
            "unsupported",
            "not support",
            "unknown",
            "unrecognized",
            "invalid parameter",
            "invalid request",
            "not allowed",
        )
        return any(token in text for token in feature_tokens) and any(
            token in text for token in rejection_tokens
        )

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
        response_contract_mode = "JSON_SCHEMA_STRICT"
        fallback_reason: str | None = None
        provider_attempts = 0
        if is_minimax:
            response_contract_mode = "FUNCTION_SERIALIZED_JSON_STREAM_MINIMAX"
            function_name = (
                "submit_" + re.sub(r"[^A-Za-z0-9_-]", "_", prompt_id)
            )[:64]
            request.pop("response_format")
            request["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": function_name,
                        "description": (
                            "Submit one exact serialized JSON business output for "
                            + prompt_id
                        ),
                        "strict": True,
                        "parameters": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "output_json": {
                                    "type": "string",
                                    "description": (
                                        "The complete final business output serialized "
                                        "as one strict JSON object string matching the "
                                        "runtime output schema."
                                    ),
                                }
                            },
                            "required": ["output_json"],
                        },
                    },
                }
            ]
            # MiniMax currently supports auto/none tool selection, so a tool call
            # cannot be forced at the wire level.  Prefer the single submit tool,
            # while allowing one strict assistant JSON object as a transport-only
            # fallback.  Both representations still flow through the same business
            # schema validation owned by the prompt executor.
            request["tool_choice"] = "auto"
            request["messages"][0]["content"] += (
                "\n\n# MiniMax structured submission boundary\n"
                f"Prefer calling `{function_name}` exactly once. The sole `output_json` "
                "argument must be a JSON string containing the complete final business "
                "output object governed by the runtime output schema above. If no tool "
                "call is emitted, return that same complete business output directly as "
                "one strict JSON object and nothing else. Do not return prose, markdown, "
                "code fences, or multiple objects. Do not omit, rename, move, repair, or "
                "default any business field. Serialize the final business object as compact "
                "JSON: no pretty-print indentation, blank lines, or optional whitespace. "
                "Keep descriptive strings concise and do not repeat the same evidence prose "
                "across fields when exact reference IDs already carry that linkage."
            )
            request["reasoning_split"] = True
            # Streaming keeps long generations active across intermediaries that
            # otherwise close an idle non-streaming request.  The stream is only
            # a wire transport: both the function wrapper and the embedded
            # business object are parsed strictly after the final event.
            request["stream"] = True

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                provider_attempts += 1
                if is_minimax:
                    content, wire_report = await self._stream_serialized_function_call(
                        client,
                        f"{base_url}/chat/completions",
                        headers,
                        request,
                        expected_name=function_name,
                    )
                    payload = None
                    response = None
                else:
                    wire_report = None
                    response = await client.post(
                        f"{base_url}/chat/completions",
                        headers=headers,
                        json=request,
                    )
                if response is not None and response.status_code >= 400:
                    first_error_body = response.text[:1000]
                    if self._is_structured_output_rejection(
                        response.status_code,
                        first_error_body,
                    ):
                        request["response_format"] = {"type": "json_object"}
                        response_contract_mode = "JSON_OBJECT_FALLBACK"
                        fallback_reason = (
                            f"provider rejected json_schema with HTTP {response.status_code}: "
                            + first_error_body
                        )[:1200]
                        provider_attempts += 1
                        response = await client.post(
                            f"{base_url}/chat/completions",
                            headers=headers,
                            json=request,
                        )
                if response is not None and response.status_code >= 400:
                    body = response.text[:1000]
                    raise ProviderError(
                        f"LLM endpoint returned {response.status_code}: {body}",
                        kind=ProviderFailureKind.HTTP_STATUS,
                        http_status=response.status_code,
                        retry_after_seconds=_retry_after_seconds(response),
                        phase="request",
                        response_excerpt=body,
                    )
                if response is not None:
                    try:
                        payload = response.json()
                    except (json.JSONDecodeError, ValueError) as exc:
                        raise ProviderError(
                            "LLM endpoint returned a non-JSON response",
                            kind=ProviderFailureKind.RESPONSE_PARSE,
                            phase="response_parse",
                            response_excerpt=response.text[:1000],
                            retryable_hint=False,
                        ) from exc
        except ProviderError:
            raise
        except httpx.TimeoutException as exc:
            detail = str(exc).strip() or repr(exc)
            raise ProviderError(
                f"LLM transport timed out ({type(exc).__name__}): {detail}",
                kind=ProviderFailureKind.TIMEOUT,
                phase="request",
                retryable_hint=True,
            ) from exc
        except httpx.RequestError as exc:
            detail = str(exc).strip() or repr(exc)
            raise ProviderError(
                f"LLM transport failed ({type(exc).__name__}): {detail}",
                kind=ProviderFailureKind.TRANSPORT,
                phase="request",
                retryable_hint=True,
            ) from exc

        if not is_minimax:
            try:
                message = payload["choices"][0]["message"]
            except (KeyError, IndexError, TypeError) as exc:
                raise ProviderError(
                    "Invalid OpenAI-compatible response structure",
                    kind=ProviderFailureKind.RESPONSE_SHAPE,
                    phase="response_shape",
                    retryable_hint=False,
                ) from exc
            content = message.get("content")
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        try:
            if response_contract_mode in {
                "JSON_SCHEMA_STRICT",
                "FUNCTION_SERIALIZED_JSON_STREAM_MINIMAX",
            }:
                output, parse_report = _load_strict_json_object(str(content))
            else:
                output, parse_report = _extract_json_with_report(str(content))
        except LLMError as exc:
            raise ProviderError(
                f"{'MiniMax' if is_minimax else 'Provider'} returned malformed JSON: {exc}",
                kind=ProviderFailureKind.RESPONSE_PARSE,
                phase="response_parse",
                response_excerpt=str(content)[:1000],
                retryable_hint=False,
            ) from exc
        if wire_report is not None:
            parse_report = {
                **parse_report,
                "wire_protocol": "STRICT_MINIMAX_TOOL_OR_JSON",
                "wire_wrapper_parse_report": wire_report,
            }
        return LLMResult(
            output=output,
            raw_text=str(content),
            model_id=route.model_id,
            endpoint_id=route.endpoint_id,
            parse_report=parse_report,
            response_contract_mode=response_contract_mode,
            provider_attempts=provider_attempts,
            fallback_reason=fallback_reason,
        )

    @staticmethod
    def _is_minimax(base_url: str, provider_model_name: str) -> bool:
        return "minimax" in base_url.lower() or provider_model_name.lower().startswith("minimax-")

    async def _stream_serialized_function_call(
        self,
        client: httpx.AsyncClient,
        url: str,
        headers: dict[str, str],
        request: dict[str, Any],
        *,
        expected_name: str,
    ) -> tuple[str, dict[str, Any]]:
        """Receive one strict MiniMax structured business object.

        The preferred wire representation is one ``submit_*`` function call
        carrying ``output_json``.  MiniMax exposes ``tool_choice=auto`` rather
        than a force-this-function mode, so a response with no tool call may
        instead carry the complete business object directly in assistant
        content.  That fallback is intentionally narrow: it must be exactly one
        strict JSON object with no prose, markdown, code fences, or local JSON
        repair.  Business-schema validation remains owned by the prompt executor.
        """

        calls: dict[int, dict[str, str]] = {}
        assistant_content = ""
        finish_reason: str | None = None
        event_count = 0
        async with client.stream("POST", url, headers=headers, json=request) as response:
            if response.status_code >= 400:
                body = (await response.aread()).decode("utf-8", errors="replace")[:1000]
                raise ProviderError(
                    f"LLM endpoint returned {response.status_code}: {body}",
                    kind=ProviderFailureKind.HTTP_STATUS,
                    http_status=response.status_code,
                    retry_after_seconds=_retry_after_seconds(response),
                    phase="stream_open",
                    response_excerpt=body,
                )
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
                    event_count += 1
                    finish_reason = choice.get("finish_reason") or finish_reason
                    piece = delta.get("content") or ""
                    if isinstance(piece, list):
                        piece = "".join(
                            part.get("text", "")
                            for part in piece
                            if isinstance(part, dict)
                        )
                    assistant_content += str(piece)
                    for tool_call in delta.get("tool_calls") or []:
                        index = int(tool_call.get("index", 0))
                        function = tool_call.get("function") or {}
                        collected = calls.setdefault(
                            index, {"name": "", "arguments": ""}
                        )
                        if function.get("name"):
                            collected["name"] = str(function["name"])
                        argument_piece = function.get("arguments") or ""
                        if argument_piece:
                            argument_piece = str(argument_piece)
                            current = collected["arguments"]
                            collected["arguments"] = (
                                argument_piece
                                if argument_piece.startswith(current)
                                else current + argument_piece
                            )
                except (json.JSONDecodeError, AttributeError, IndexError, TypeError, ValueError) as exc:
                    raise ProviderError(
                        "LLM stream returned an invalid function-call event",
                        kind=ProviderFailureKind.STREAM_EVENT,
                        phase="stream_event",
                        response_excerpt=data[:1000],
                        retryable_hint=True,
                    ) from exc

        if finish_reason == "length":
            raise ProviderError(
                "LLM function stream reached the output token limit before completing",
                kind=ProviderFailureKind.OUTPUT_TRUNCATED,
                phase="stream_complete",
                retryable_hint=False,
            )

        if not calls:
            direct_json = assistant_content.strip()
            if not direct_json:
                raise ProviderError(
                    "MiniMax stream completed without a function call or assistant JSON object",
                    kind=ProviderFailureKind.EMPTY_STREAM,
                    phase="stream_complete",
                    retryable_hint=True,
                )
            try:
                _, direct_report = _load_strict_json_object(direct_json)
            except LLMError as exc:
                raise ProviderError(
                    f"MiniMax returned assistant content instead of a strict JSON object: {exc}",
                    kind=ProviderFailureKind.RESPONSE_PARSE,
                    phase="assistant_json_parse",
                    response_excerpt=assistant_content[:1000],
                    retryable_hint=False,
                ) from exc
            return direct_json, {
                **direct_report,
                "mode": "STRICT_ASSISTANT_JSON_FALLBACK",
                "event_count": event_count,
                "finish_reason": finish_reason,
                "transport": "ASSISTANT_JSON",
            }

        matching = [
            item for item in calls.values() if item.get("name") == expected_name
        ]
        if len(calls) != 1 or len(matching) != 1 or assistant_content.strip():
            raise ProviderError(
                (
                    f"MiniMax must either call {expected_name} exactly once without "
                    "assistant content, or return one strict assistant JSON object "
                    "without any tool call"
                ),
                kind=ProviderFailureKind.RESPONSE_SHAPE,
                phase="function_call",
                response_excerpt=assistant_content[:1000],
                retryable_hint=False,
            )
        arguments_text = matching[0].get("arguments") or ""
        if not arguments_text:
            raise ProviderError(
                "MiniMax function stream completed without arguments",
                kind=ProviderFailureKind.EMPTY_STREAM,
                phase="stream_complete",
                retryable_hint=True,
            )
        try:
            wrapper, wrapper_report = _load_strict_json_object(arguments_text)
        except LLMError as exc:
            raise ProviderError(
                f"MiniMax returned malformed function arguments: {exc}",
                kind=ProviderFailureKind.RESPONSE_PARSE,
                phase="function_arguments_parse",
                response_excerpt=arguments_text[:1000],
                retryable_hint=False,
            ) from exc
        if set(wrapper) != {"output_json"} or not isinstance(
            wrapper.get("output_json"), str
        ):
            raise ProviderError(
                "MiniMax function arguments must contain exactly one output_json string",
                kind=ProviderFailureKind.RESPONSE_SHAPE,
                phase="function_arguments_shape",
                response_excerpt=arguments_text[:1000],
                retryable_hint=False,
            )
        return wrapper["output_json"], {
            **wrapper_report,
            "mode": "STRICT_FUNCTION_WRAPPER_JSON",
            "event_count": event_count,
            "finish_reason": finish_reason,
            "transport": "FUNCTION_OUTPUT_JSON",
        }

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
                raise ProviderError(
                    f"LLM endpoint returned {response.status_code}: {body}",
                    kind=ProviderFailureKind.HTTP_STATUS,
                    http_status=response.status_code,
                    retry_after_seconds=_retry_after_seconds(response),
                    phase="stream_open",
                    response_excerpt=body,
                )
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
                    raise ProviderError(
                        "LLM stream returned an invalid event",
                        kind=ProviderFailureKind.STREAM_EVENT,
                        phase="stream_event",
                        response_excerpt=data[:1000],
                        retryable_hint=True,
                    ) from exc
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
            raise ProviderError(
                "LLM stream completed without message content",
                kind=ProviderFailureKind.EMPTY_STREAM,
                phase="stream_complete",
                retryable_hint=True,
            )
        if finish_reason == "length":
            raise ProviderError(
                "LLM stream reached the output token limit before completing",
                kind=ProviderFailureKind.OUTPUT_TRUNCATED,
                phase="stream_complete",
                retryable_hint=False,
            )
        return content
