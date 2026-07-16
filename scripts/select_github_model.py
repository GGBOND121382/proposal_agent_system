from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path
from typing import Any

CATALOG_URL = "https://models.github.ai/catalog/models"
PREFERRED_MODELS = (
    "openai/gpt-4.1-mini",
    "openai/gpt-4o-mini",
    "microsoft/Phi-4-mini-instruct",
    "mistral-ai/Ministral-3B",
)


def _text_chat_model(item: dict[str, Any]) -> bool:
    model_id = str(item.get("id") or item.get("model") or "")
    task = str(item.get("task") or item.get("task_type") or "").lower()
    capabilities = {str(value).lower() for value in item.get("capabilities") or []}
    tags = {str(value).lower() for value in item.get("tags") or []}
    haystack = " ".join((model_id, task, *capabilities, *tags)).lower()
    if any(marker in haystack for marker in ("embedding", "image", "audio", "speech")):
        return False
    return not task or "chat" in task or "text" in haystack


def _tier(item: dict[str, Any]) -> str:
    value = item.get("rate_limit_tier")
    if isinstance(value, dict):
        value = value.get("name") or value.get("tier")
    return str(value or "").lower()


def _input_limit(item: dict[str, Any]) -> int:
    limits = item.get("limits") or {}
    return int(
        item.get("max_input_tokens")
        or limits.get("max_input_tokens")
        or limits.get("input_tokens")
        or 0
    )


def choose_model(catalog: list[dict[str, Any]]) -> dict[str, Any]:
    eligible = [item for item in catalog if _text_chat_model(item) and _tier(item) == "low"]
    if not eligible:
        eligible = [item for item in catalog if _text_chat_model(item)]
    if not eligible:
        raise RuntimeError("GitHub Models catalog contains no usable text chat model")
    by_id = {str(item.get("id") or item.get("model") or ""): item for item in eligible}
    for model_id in PREFERRED_MODELS:
        if model_id in by_id:
            return by_id[model_id]
    return max(eligible, key=lambda item: (_input_limit(item), str(item.get("id") or "")))


def fetch_catalog(token: str) -> list[dict[str, Any]]:
    request = urllib.request.Request(
        CATALOG_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "proposal-agent-system-g3",
        },
    )
    with urllib.request.urlopen(request, timeout=45) as response:  # nosec B310
        payload = json.load(response)
    if isinstance(payload, dict):
        payload = payload.get("models") or payload.get("data") or []
    if not isinstance(payload, list):
        raise RuntimeError("Unexpected GitHub Models catalog response")
    return [item for item in payload if isinstance(item, dict)]


def main() -> int:
    parser = argparse.ArgumentParser(description="Select a text model from the GitHub Models catalog.")
    parser.add_argument("--catalog-file", type=Path)
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()
    if args.catalog_file:
        payload = json.loads(args.catalog_file.read_text(encoding="utf-8"))
        catalog = payload if isinstance(payload, list) else payload.get("models") or payload.get("data") or []
    else:
        token = os.getenv("GITHUB_TOKEN", "")
        if not token:
            raise RuntimeError("GITHUB_TOKEN is required to read the model catalog")
        catalog = fetch_catalog(token)
    selected = choose_model([item for item in catalog if isinstance(item, dict)])
    model_id = str(selected.get("id") or selected.get("model") or "")
    result = {
        "model_id": model_id,
        "rate_limit_tier": _tier(selected) or "unknown",
        "max_input_tokens": _input_limit(selected),
        "catalog_entry": {
            "id": model_id,
            "name": selected.get("name"),
            "publisher": selected.get("publisher"),
            "task": selected.get("task") or selected.get("task_type"),
            "rate_limit_tier": _tier(selected) or "unknown",
            "max_input_tokens": _input_limit(selected),
        },
    }
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    github_output = os.getenv("GITHUB_OUTPUT")
    if github_output:
        with Path(github_output).open("a", encoding="utf-8") as handle:
            handle.write(f"model_id={model_id}\n")
            handle.write(f"rate_limit_tier={result['rate_limit_tier']}\n")
            handle.write(f"max_input_tokens={result['max_input_tokens']}\n")
    github_env = os.getenv("GITHUB_ENV")
    if github_env:
        with Path(github_env).open("a", encoding="utf-8") as handle:
            handle.write(f"OFFLINE_GENERAL_MODEL={model_id}\n")
            handle.write(f"OFFLINE_CRITIC_MODEL={model_id}\n")
            handle.write(f"ONLINE_PUBLIC_MODEL={model_id}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
