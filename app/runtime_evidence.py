from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .util import canonical_json, sha256_json, sha256_text, utc_now


class EvidenceIntegrityError(RuntimeError):
    pass


class InjectedFailure(RuntimeError):
    def __init__(self, point: str, call_key: str):
        super().__init__(f"INJECTED_FAILURE:{point}:{call_key}")
        self.point = point
        self.call_key = call_key


def _safe_key(value: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9._-]", "_", str(value))
    return clean[:180] or "call"


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".tmp-{os.getpid()}")
    with temp.open("wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def _atomic_write_text(path: Path, content: str) -> None:
    _atomic_write_bytes(path, content.encode("utf-8"))


def _atomic_write_json(path: Path, value: Any) -> None:
    _atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _extract_json_object(text: str) -> dict[str, Any]:
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
            raise EvidenceIntegrityError("Raw response does not contain a JSON object")
        try:
            value = json.loads(stripped[start : end + 1])
        except json.JSONDecodeError as exc:
            raise EvidenceIntegrityError("Raw response JSON is invalid") from exc
    if not isinstance(value, dict):
        raise EvidenceIntegrityError("Raw response JSON must be an object")
    return value


@dataclass(frozen=True)
class VerifiedResponse:
    raw_text: str
    parsed_output: dict[str, Any]
    metadata: dict[str, Any]


class FaultInjector:
    """One-shot durable fault injection for restart tests.

    Set RUNTIME_FAULT_POINT to one or more comma-separated points. A marker is fsynced
    before raising/exiting, so the same call resumes past that point after restart.
    Optional RUNTIME_FAULT_CALL_KEY and RUNTIME_FAULT_PROMPT_ID filters keep tests narrow.
    """

    def __init__(self, root: Path):
        self.root = root / "fault_markers"

    def hit(self, point: str, call_key: str, *, prompt_id: str | None = None) -> None:
        configured = {
            item.strip() for item in os.getenv("RUNTIME_FAULT_POINT", "").split(",") if item.strip()
        }
        if point not in configured:
            return
        call_filter = os.getenv("RUNTIME_FAULT_CALL_KEY", "").strip()
        if call_filter and call_filter != call_key:
            return
        prompt_filter = os.getenv("RUNTIME_FAULT_PROMPT_ID", "").strip()
        if prompt_filter and prompt_filter != str(prompt_id or ""):
            return
        marker = self.root / f"{_safe_key(call_key)}.{_safe_key(point)}.fired"
        if marker.exists():
            return
        _atomic_write_json(marker, {"point": point, "call_key": call_key, "prompt_id": prompt_id, "fired_at": utc_now()})
        if os.getenv("RUNTIME_FAULT_ACTION", "raise").strip().lower() == "exit":
            os._exit(int(os.getenv("RUNTIME_FAULT_EXIT_CODE", "97")))
        raise InjectedFailure(point, call_key)


class ModelCallEvidenceStore:
    """Durable, hash-verified request/response evidence for every model call."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.requests_dir = self.root / "requests"
        self.responses_dir = self.root / "responses"
        self.commits_dir = self.root / "commits"
        self.requests_dir.mkdir(parents=True, exist_ok=True)
        self.responses_dir.mkdir(parents=True, exist_ok=True)
        self.commits_dir.mkdir(parents=True, exist_ok=True)
        self.faults = FaultInjector(self.root)

    def request_paths(self, call_key: str) -> tuple[Path, Path]:
        key = _safe_key(call_key)
        return self.requests_dir / f"{key}.json", self.requests_dir / f"{key}.meta.json"

    def response_paths(self, call_key: str) -> tuple[Path, Path, Path]:
        key = _safe_key(call_key)
        return (
            self.responses_dir / f"{key}.raw.txt",
            self.responses_dir / f"{key}.parsed.json",
            self.responses_dir / f"{key}.meta.json",
        )

    def write_request(self, call_key: str, request_payload: dict[str, Any]) -> dict[str, Any]:
        request_path, meta_path = self.request_paths(call_key)
        request_hash = sha256_json(request_payload)
        if request_path.exists() or meta_path.exists():
            if not request_path.exists() or not meta_path.exists():
                raise EvidenceIntegrityError(f"Partial request evidence for {call_key}")
            existing = json.loads(request_path.read_text(encoding="utf-8"))
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if sha256_json(existing) != request_hash or meta.get("request_sha256") != request_hash:
                raise EvidenceIntegrityError(f"Request evidence mismatch for {call_key}")
            return meta
        _atomic_write_json(request_path, request_payload)
        meta = {
            "call_key": call_key,
            "request_sha256": request_hash,
            "request_path": str(request_path),
            "created_at": utc_now(),
        }
        _atomic_write_json(meta_path, meta)
        return meta

    def write_response(
        self,
        call_key: str,
        *,
        raw_text: str,
        parsed_output: dict[str, Any],
        raw_parsed_output: dict[str, Any],
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        if sha256_json(parsed_output) != sha256_json(raw_parsed_output):
            raise EvidenceIntegrityError(
                f"Raw response JSON and gateway parsed object differ for {call_key}; refusing consumption."
            )
        raw_path, parsed_path, meta_path = self.response_paths(call_key)
        raw_hash = sha256_text(raw_text)
        parsed_hash = sha256_json(parsed_output)
        response_meta = {
            **metadata,
            "call_key": call_key,
            "raw_response_sha256": raw_hash,
            "parsed_object_sha256": parsed_hash,
            "raw_parsed_object_sha256": sha256_json(raw_parsed_output),
            "raw_path": str(raw_path),
            "parsed_path": str(parsed_path),
            "created_at": utc_now(),
        }
        if raw_path.exists() or parsed_path.exists() or meta_path.exists():
            verified = self.load_verified_response(call_key)
            if (
                verified.metadata.get("raw_response_sha256") != raw_hash
                or verified.metadata.get("parsed_object_sha256") != parsed_hash
            ):
                raise EvidenceIntegrityError(f"Response evidence mismatch for {call_key}")
            return verified.metadata
        _atomic_write_text(raw_path, raw_text)
        _atomic_write_json(parsed_path, parsed_output)
        _atomic_write_json(meta_path, response_meta)
        return response_meta

    def has_response(self, call_key: str) -> bool:
        return all(path.exists() for path in self.response_paths(call_key))

    def load_verified_response(self, call_key: str) -> VerifiedResponse:
        raw_path, parsed_path, meta_path = self.response_paths(call_key)
        if not (raw_path.exists() and parsed_path.exists() and meta_path.exists()):
            raise FileNotFoundError(call_key)
        raw_text = raw_path.read_text(encoding="utf-8")
        parsed = json.loads(parsed_path.read_text(encoding="utf-8"))
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        if not isinstance(parsed, dict):
            raise EvidenceIntegrityError(f"Parsed response must be an object for {call_key}")
        if sha256_text(raw_text) != metadata.get("raw_response_sha256"):
            raise EvidenceIntegrityError(f"Raw response hash mismatch for {call_key}")
        if sha256_json(parsed) != metadata.get("parsed_object_sha256"):
            raise EvidenceIntegrityError(f"Parsed response hash mismatch for {call_key}")
        raw_object = _extract_json_object(raw_text)
        if sha256_json(raw_object) != sha256_json(parsed):
            raise EvidenceIntegrityError(f"Raw response object mismatch for {call_key}")
        return VerifiedResponse(raw_text=raw_text, parsed_output=parsed, metadata=metadata)

    def mark_committed(self, call_key: str, payload: dict[str, Any]) -> Path:
        path = self.commits_dir / f"{_safe_key(call_key)}.json"
        _atomic_write_json(path, {**payload, "call_key": call_key, "committed_at": utc_now()})
        return path

    def request_digest(self, call_key: str) -> str:
        request_path, _ = self.request_paths(call_key)
        return sha256_text(canonical_json(json.loads(request_path.read_text(encoding="utf-8"))))
