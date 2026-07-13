from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)(?::([^}]*))?\}")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def expand_env(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    if not isinstance(value, str):
        return value

    full = _ENV_PATTERN.fullmatch(value)
    if full:
        name, default = full.groups()
        raw = os.getenv(name, default if default is not None else "")
        if raw is None:
            return ""
        lowered = raw.lower()
        if lowered in {"true", "false"}:
            return lowered == "true"
        if raw.isdigit():
            return int(raw)
        return raw

    return _ENV_PATTERN.sub(lambda m: os.getenv(m.group(1), m.group(2) or ""), value)


def safe_filename(name: str) -> str:
    name = Path(name).name
    clean = re.sub(r"[^A-Za-z0-9._\-\u4e00-\u9fff]", "_", name)
    return clean[:180] or "upload.bin"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
