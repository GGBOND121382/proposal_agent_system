from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .private_storage import secure_private_directory, secure_private_file, secure_private_tree
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


def _secure_directory(path: Path) -> None:
    secure_private_directory(path)


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    _secure_directory(path.parent)
    temp = path.with_name(path.name + f".tmp-{os.getpid()}")
    with temp.open("wb") as handle:
        secure_private_file(temp)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)
    secure_private_file(path)


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
            from .llm import _extract_json

            value = _extract_json(stripped[start : end + 1])
        except (ValueError, RuntimeError) as exc:
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
        _secure_directory(self.root)
        _secure_directory(self.requests_dir)
        _secure_directory(self.responses_dir)
        _secure_directory(self.commits_dir)
        secure_private_tree(self.root)
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

    def provider_response_paths(
        self,
        call_key: str,
        provider_attempt: int,
    ) -> tuple[Path, Path]:
        key = _safe_key(call_key)
        attempt = max(1, int(provider_attempt))
        stem = f"{key}.provider-attempt-{attempt}"
        return (
            self.responses_dir / f"{stem}.raw.txt",
            self.responses_dir / f"{stem}.meta.json",
        )

    def failed_response_paths(self, call_key: str) -> tuple[Path, Path]:
        key = _safe_key(call_key)
        return (
            self.responses_dir / f"{key}.rejected.txt",
            self.responses_dir / f"{key}.failed.meta.json",
        )

    def write_provider_response(
        self,
        call_key: str,
        *,
        provider_attempt: int,
        raw_text: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist the provider wire response before any parsing or validation.

        This evidence is intentionally separate from ``response_paths``.  The
        latter preserves the historical contract of storing the extracted
        business JSON on successful calls, while these files preserve exactly
        what the provider returned (including MiniMax SSE lines) even when the
        response cannot be parsed.
        """

        raw_path, meta_path = self.provider_response_paths(call_key, provider_attempt)
        raw_hash = sha256_text(raw_text)
        record = {
            **metadata,
            "call_key": call_key,
            "provider_attempt": max(1, int(provider_attempt)),
            "raw_response_sha256": raw_hash,
            "raw_path": str(raw_path),
            "created_at": utc_now(),
        }
        if raw_path.exists() or meta_path.exists():
            if not raw_path.exists() or not meta_path.exists():
                raise EvidenceIntegrityError(
                    f"Partial provider response evidence for {call_key} attempt {provider_attempt}"
                )
            existing_raw = raw_path.read_text(encoding="utf-8")
            existing_meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if (
                sha256_text(existing_raw) != raw_hash
                or existing_meta.get("raw_response_sha256") != raw_hash
            ):
                raise EvidenceIntegrityError(
                    f"Provider response evidence mismatch for {call_key} attempt {provider_attempt}"
                )
            return existing_meta

        _atomic_write_text(raw_path, raw_text)
        _atomic_write_json(meta_path, record)
        return record

    def provider_response_records(self, call_key: str) -> list[dict[str, Any]]:
        key = _safe_key(call_key)
        records: list[dict[str, Any]] = []
        pattern = f"{key}.provider-attempt-*.meta.json"
        for meta_path in sorted(self.responses_dir.glob(pattern)):
            metadata = json.loads(meta_path.read_text(encoding="utf-8"))
            attempt = int(metadata.get("provider_attempt") or 1)
            raw_path, expected_meta_path = self.provider_response_paths(call_key, attempt)
            if expected_meta_path != meta_path or not raw_path.exists():
                raise EvidenceIntegrityError(
                    f"Partial provider response evidence for {call_key} attempt {attempt}"
                )
            raw_text = raw_path.read_text(encoding="utf-8")
            if sha256_text(raw_text) != metadata.get("raw_response_sha256"):
                raise EvidenceIntegrityError(
                    f"Provider raw response hash mismatch for {call_key} attempt {attempt}"
                )
            records.append(metadata)
        return records

    def write_failed_response(
        self,
        call_key: str,
        *,
        rejected_text: str | None,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        """Commit failure metadata without discarding the unparseable response.

        ``rejected_text`` is the full logical candidate that failed parsing or
        shape checks when one exists.  The exact provider wire response is kept
        separately by ``write_provider_response`` and linked from this record.
        """

        rejected_path, meta_path = self.failed_response_paths(call_key)
        rejected_hash = (
            sha256_text(rejected_text) if rejected_text is not None else None
        )
        provider_records = self.provider_response_records(call_key)
        record = {
            **metadata,
            "call_key": call_key,
            "rejected_response_sha256": rejected_hash,
            "rejected_path": str(rejected_path) if rejected_text is not None else None,
            "provider_responses": provider_records,
            "provider_response_count": len(provider_records),
            "created_at": utc_now(),
        }

        if meta_path.exists() or rejected_path.exists():
            if not meta_path.exists():
                raise EvidenceIntegrityError(f"Partial failed response evidence for {call_key}")
            existing = json.loads(meta_path.read_text(encoding="utf-8"))
            if rejected_text is not None:
                if not rejected_path.exists():
                    raise EvidenceIntegrityError(
                        f"Missing rejected response evidence for {call_key}"
                    )
                existing_text = rejected_path.read_text(encoding="utf-8")
                if (
                    sha256_text(existing_text) != rejected_hash
                    or existing.get("rejected_response_sha256") != rejected_hash
                ):
                    raise EvidenceIntegrityError(
                        f"Rejected response evidence mismatch for {call_key}"
                    )
            elif rejected_path.exists():
                raise EvidenceIntegrityError(
                    f"Unexpected rejected response evidence for {call_key}"
                )
            return existing

        if rejected_text is not None:
            _atomic_write_text(rejected_path, rejected_text)
        _atomic_write_json(meta_path, record)
        return record

    def has_failed_response(self, call_key: str) -> bool:
        _, meta_path = self.failed_response_paths(call_key)
        return meta_path.exists()

    def load_failed_response(self, call_key: str) -> dict[str, Any]:
        rejected_path, meta_path = self.failed_response_paths(call_key)
        if not meta_path.exists():
            raise FileNotFoundError(call_key)
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        rejected_hash = metadata.get("rejected_response_sha256")
        rejected_text = None
        if rejected_hash is not None:
            if not rejected_path.exists():
                raise EvidenceIntegrityError(
                    f"Missing rejected response evidence for {call_key}"
                )
            rejected_text = rejected_path.read_text(encoding="utf-8")
            if sha256_text(rejected_text) != rejected_hash:
                raise EvidenceIntegrityError(
                    f"Rejected response hash mismatch for {call_key}"
                )
        provider_records = self.provider_response_records(call_key)
        expected_records = metadata.get("provider_responses") or []
        if [r.get("raw_response_sha256") for r in provider_records] != [
            r.get("raw_response_sha256") for r in expected_records
        ]:
            raise EvidenceIntegrityError(
                f"Provider response list mismatch for failed call {call_key}"
            )
        return {
            "metadata": metadata,
            "rejected_text": rejected_text,
            "provider_responses": provider_records,
        }

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
        parsed_hash = sha256_json(parsed)
        persisted_raw_parsed_hash = metadata.get("raw_parsed_object_sha256")
        if persisted_raw_parsed_hash is not None:
            # The raw-to-parsed relationship was verified before the response
            # evidence was committed.  Re-parse only legacy records that lack
            # this proof: parser upgrades must not invalidate immutable evidence.
            if persisted_raw_parsed_hash != parsed_hash:
                raise EvidenceIntegrityError(f"Raw response object mismatch for {call_key}")
        else:
            raw_object = _extract_json_object(raw_text)
            if sha256_json(raw_object) != parsed_hash:
                raise EvidenceIntegrityError(f"Raw response object mismatch for {call_key}")
        return VerifiedResponse(raw_text=raw_text, parsed_output=parsed, metadata=metadata)

    def mark_committed(self, call_key: str, payload: dict[str, Any]) -> Path:
        path = self.commits_dir / f"{_safe_key(call_key)}.json"
        _atomic_write_json(path, {**payload, "call_key": call_key, "committed_at": utc_now()})
        return path

    def request_digest(self, call_key: str) -> str:
        request_path, _ = self.request_paths(call_key)
        return sha256_text(canonical_json(json.loads(request_path.read_text(encoding="utf-8"))))
