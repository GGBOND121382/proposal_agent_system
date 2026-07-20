from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from .util import sha256_json, utc_now, write_json


class PublicSearchBridgeError(RuntimeError):
    pass


class FilePublicSearchBridge:
    """Durable external-search bridge used by portable LIVE runs.

    The agent publishes the approved structured research plan and an exact response
    contract. An external search executor (for example ChatGPT with web access)
    returns only public source records. The normal research skill then validates,
    deduplicates, hashes and archives those records; the bridge never creates
    PUBLIC_CLAIMs or bypasses import review.
    """

    def __init__(self, root: Path, *, poll_seconds: float = 0.25, timeout_seconds: int = 240):
        self.root = Path(root)
        self.requests_dir = self.root / "search_requests"
        self.responses_dir = self.root / "search_responses"
        self.consumed_dir = self.root / "search_consumed"
        for directory in (self.requests_dir, self.responses_dir, self.consumed_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self.poll_seconds = max(0.05, float(poll_seconds))
        self.timeout_seconds = max(1, int(timeout_seconds))

    @staticmethod
    def response_schema() -> dict[str, Any]:
        result_properties = {
            "source_id": {"type": ["string", "null"]},
            "title": {"type": "string", "minLength": 1},
            "url": {"type": "string", "pattern": "^https?://"},
            "doi": {"type": ["string", "null"]},
            "authors": {"type": "array", "items": {"type": "string"}},
            "publisher": {"type": ["string", "null"]},
            "published_at": {"type": ["string", "null"]},
            "source_type": {"type": ["string", "null"]},
            "content_text": {"type": "string", "minLength": 20},
            "authority_rank": {"type": ["integer", "null"], "minimum": 1, "maximum": 100},
            "verification": {"type": "object"},
        }
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["request_id", "request_hash", "run_id", "connector", "created_at", "responses"],
            "properties": {
                "request_id": {"type": "string", "minLength": 1},
                "request_hash": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
                "run_id": {"type": "string", "minLength": 1},
                "connector": {"type": "string", "minLength": 1},
                "created_at": {"type": "string", "minLength": 1},
                "responses": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["query", "retrieved_at", "results"],
                        "properties": {
                            "query": {"type": "string", "minLength": 1},
                            "retrieved_at": {"type": "string", "minLength": 1},
                            "results": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "required": ["title", "url", "content_text"],
                                    "properties": result_properties,
                                },
                            },
                        },
                    },
                },
            },
        }

    def _request_object(self, plan: dict[str, Any], max_results: int) -> dict[str, Any]:
        public_payload = {
            "bridge_type": "PUBLIC_SEARCH_FILE_BRIDGE",
            "schema_version": "1.0",
            "security_level": "PUBLIC",
            "plan": plan,
            "max_results": int(max_results),
            "execution_rules": [
                "Search only public HTTP(S) sources.",
                "Use primary papers, publisher/DOI metadata and official dataset pages where possible.",
                "Return exact planned query strings; every planned query must have one response entry.",
                "Do not invent authors, titles, years, DOI, URLs or quantitative results.",
                "content_text must contain only verifiable abstract/metadata/excerpt content, not model inference.",
                "Do not include personal, organizational, private-project or other-conversation information.",
            ],
            "expected_response_schema": self.response_schema(),
        }
        request_hash = sha256_json(public_payload)
        return {
            **public_payload,
            "request_id": f"public-search-{request_hash[:32]}",
            "request_hash": request_hash,
            "requested_at": utc_now(),
        }

    def publish(self, plan: dict[str, Any], max_results: int) -> dict[str, Any]:
        request = self._request_object(plan, max_results)
        path = self.requests_dir / f"{request['request_id']}.json"
        if path.exists():
            existing = json.loads(path.read_text(encoding="utf-8"))
            if existing.get("request_hash") != request["request_hash"]:
                raise PublicSearchBridgeError("Existing public-search request hash mismatch")
        else:
            write_json(path, request)
        return request

    def _validate_response(self, request: dict[str, Any], response: Any) -> dict[str, Any]:
        if not isinstance(response, dict):
            raise PublicSearchBridgeError("Public-search bridge response must be a JSON object")
        if response.get("request_id") != request["request_id"]:
            raise PublicSearchBridgeError("Public-search response request_id mismatch")
        if response.get("request_hash") != request["request_hash"]:
            raise PublicSearchBridgeError("Public-search response request_hash mismatch")
        responses = response.get("responses")
        if not isinstance(responses, list):
            raise PublicSearchBridgeError("Public-search response must contain a responses array")
        planned_queries = [str(item) for item in request["plan"].get("queries") or []]
        returned_queries = [str(item.get("query") or "") for item in responses if isinstance(item, dict)]
        missing = [query for query in planned_queries if query not in returned_queries]
        if missing:
            raise PublicSearchBridgeError(f"Public-search response misses planned queries: {missing}")
        for item in responses:
            if not isinstance(item, dict) or not isinstance(item.get("results"), list):
                raise PublicSearchBridgeError("Each public-search response entry must contain results")
            for result in item["results"]:
                if not isinstance(result, dict):
                    raise PublicSearchBridgeError("Public-search result must be an object")
                url = str(result.get("url") or "")
                text = str(result.get("content_text") or "")
                if not url.startswith(("http://", "https://")):
                    raise PublicSearchBridgeError("Public-search result URL must be HTTP(S)")
                if len(text.strip()) < 20:
                    raise PublicSearchBridgeError("Public-search result content_text is too short")
        return response

    def wait(self, request: dict[str, Any]) -> Path:
        response_path = self.responses_dir / f"{request['request_id']}.json"
        deadline = time.monotonic() + self.timeout_seconds
        while not response_path.exists():
            if time.monotonic() >= deadline:
                raise PublicSearchBridgeError(
                    f"Timed out waiting for public-search response: {request['request_id']}"
                )
            time.sleep(self.poll_seconds)
        response = self._validate_response(
            request, json.loads(response_path.read_text(encoding="utf-8"))
        )
        consumed_path = self.consumed_dir / response_path.name
        write_json(consumed_path, response)
        return consumed_path

    async def request(self, plan: dict[str, Any], max_results: int) -> Path:
        request = self.publish(plan, max_results)
        return await asyncio.to_thread(self.wait, request)
