from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from .util import sha256_json, utc_now


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
    tmp.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


class FileHumanGateBridge:
    """Durable, auditable file bridge for human decisions.

    The bridge never auto-approves a gate.  It publishes the exact allowed actions,
    questions, role and context hash, then accepts only a response bound to the same
    gate and context.  This keeps an interactive GPT/human operator interchangeable
    with another approval UI without weakening the workflow's gate semantics.
    """

    def __init__(
        self,
        root: Path,
        *,
        poll_seconds: float = 0.5,
        timeout_seconds: float = 86400.0,
    ) -> None:
        self.root = Path(root).resolve()
        self.requests_dir = self.root / "gate_requests"
        self.responses_dir = self.root / "gate_responses"
        self.consumed_dir = self.root / "gate_consumed"
        self.poll_seconds = max(0.05, float(poll_seconds))
        self.timeout_seconds = max(1.0, float(timeout_seconds))

    @classmethod
    def from_settings(cls, settings: Any) -> "FileHumanGateBridge":
        root = getattr(settings, "human_gate_bridge_dir", None)
        if root is None:
            chat_root = getattr(settings, "chat_bridge_dir", None)
            if chat_root is not None:
                root = Path(chat_root) / "human_gates"
        if root is None:
            raw = os.getenv("HUMAN_GATE_BRIDGE_DIR", "").strip()
            if raw:
                root = Path(raw)
        if root is None:
            raise ValueError(
                "Human gate bridge requires HUMAN_GATE_BRIDGE_DIR or CHAT_BRIDGE_DIR"
            )
        return cls(
            Path(root),
            poll_seconds=float(os.getenv("HUMAN_GATE_POLL_SECONDS", "0.5")),
            timeout_seconds=float(os.getenv("HUMAN_GATE_TIMEOUT_SECONDS", "86400")),
        )

    def publish(self, gate: dict[str, Any]) -> Path:
        request = {
            "schema_version": "1.0",
            "gate_id": str(gate["id"]),
            "project_id": str(gate["project_id"]),
            "workflow_id": str(gate["workflow_id"]),
            "gate_type": str(gate["gate_type"]),
            "target_id": str(gate.get("target_id") or ""),
            "required_role": str(gate["required_role"]),
            "allowed_actions": list(gate.get("allowed_actions") or []),
            "questions": list(gate.get("questions") or []),
            "context_hash": str(gate["context_hash"]),
            "published_at": utc_now(),
        }
        request["request_hash"] = sha256_json(
            {key: value for key, value in request.items() if key != "request_hash"}
        )
        path = self.requests_dir / f"{gate['id']}.json"
        _atomic_json(path, request)
        return path

    async def wait_and_apply(self, engine: Any, gate: dict[str, Any]) -> dict[str, Any]:
        self.publish(gate)
        response_path = self.responses_dir / f"{gate['id']}.json"
        started = asyncio.get_running_loop().time()
        while not response_path.exists():
            if asyncio.get_running_loop().time() - started > self.timeout_seconds:
                raise TimeoutError(f"HUMAN_GATE_TIMEOUT:{gate['id']}")
            await asyncio.sleep(self.poll_seconds)
        response = json.loads(response_path.read_text(encoding="utf-8"))
        if not isinstance(response, dict):
            raise ValueError("Human gate response must be a JSON object")
        if str(response.get("gate_id") or "") != str(gate["id"]):
            raise ValueError("Human gate response gate_id mismatch")
        if str(response.get("context_hash") or "") != str(gate["context_hash"]):
            raise ValueError("Human gate response context_hash mismatch")
        action = str(response.get("action") or "")
        if action not in set(gate.get("allowed_actions") or []):
            raise ValueError(f"Human gate action is not allowed: {action}")
        role = str(response.get("decided_role") or "")
        if role not in {str(gate["required_role"]), "SYSTEM_ADMIN"}:
            raise PermissionError(
                f"Gate requires role {gate['required_role']}; received {role}"
            )
        result = engine.decide_gate(
            str(gate["id"]),
            action=action,
            decided_by=str(response.get("decided_by") or "file-bridge-user"),
            decided_role=role,
            comment=response.get("comment"),
            answers=list(response.get("answers") or []),
            context_hash=str(gate["context_hash"]),
        )
        _atomic_json(
            self.consumed_dir / f"{gate['id']}.json",
            {"request": json.loads((self.requests_dir / f"{gate['id']}.json").read_text(encoding="utf-8")), "response": response, "consumed_at": utc_now()},
        )
        return result
