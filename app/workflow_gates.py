from __future__ import annotations

import json
from typing import Any

from .util import new_id, sha256_json, utc_now
from .workflow_defs import GATE_ACTIONS, GATE_ROLE


class WorkflowGateMixin:
    def _create_gate(self, wf: dict[str, Any], gate_type: str, *, target_id: str, questions: list[dict[str, Any]]) -> str:
        existing = self.db.fetchone("SELECT id FROM gates WHERE workflow_id=? AND gate_type=? AND status='OPEN'", (wf["id"], gate_type))
        if existing:
            return existing["id"]
        gate_id = new_id("gate")
        context_hash = sha256_json({"workflow": wf["id"], "step": wf["current_step"], "state": wf["state"]})
        allowed = GATE_ACTIONS.get(gate_type, ["CONFIRM", "RETURN", "REJECT", "CANCEL"])
        now = utc_now()
        self.db.execute(
            """INSERT INTO gates(id,project_id,workflow_id,gate_type,target_id,target_version,context_hash,question_version,required_role,allowed_actions_json,questions_json,security_level,status,decision_json,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (gate_id, wf["project_id"], wf["id"], gate_type, target_id, 1, context_hash, 1, GATE_ROLE[gate_type], json.dumps(allowed), json.dumps(questions, ensure_ascii=False), self._project_level(wf["project_id"]), "OPEN", None, now, now),
        )
        self.db.audit("GATE_CREATED", project_id=wf["project_id"], object_id=gate_id, metadata={"gate_type": gate_type, "workflow_id": wf["id"], "context_hash": context_hash})
        return gate_id

    def decide_gate(self, gate_id: str, *, action: str, decided_by: str, decided_role: str, comment: str | None = None, answers: list[dict[str, Any]] | None = None, context_hash: str | None = None) -> dict[str, Any]:
        gate = self.db.fetchone("SELECT * FROM gates WHERE id=?", (gate_id,))
        if not gate:
            raise KeyError(f"Gate not found: {gate_id}")
        if gate["status"] != "OPEN":
            raise ValueError("Gate is not open")
        allowed = json.loads(gate["allowed_actions_json"])
        if action not in allowed:
            raise ValueError(f"Action {action} is not allowed")
        if decided_role != gate["required_role"] and decided_role != "SYSTEM_ADMIN":
            raise PermissionError(f"Gate requires role {gate['required_role']}")
        if context_hash and context_hash != gate["context_hash"]:
            raise ValueError("Gate context hash is stale")
        approved = action in {"APPROVE", "CONFIRM", "RESOLVE", "PROVIDE_INFORMATION"}
        status = "APPROVED" if approved else ("CANCELLED" if action == "CANCEL" else "REJECTED")
        decision = {"action": action, "comment": comment, "answers": answers or [], "decided_by": decided_by, "decided_role": decided_role, "decided_at": utc_now(), "context_hash": gate["context_hash"]}
        self.db.execute("UPDATE gates SET status=?,decision_json=?,updated_at=? WHERE id=?", (status, json.dumps(decision, ensure_ascii=False), utc_now(), gate_id))
        wf = self.get(gate["workflow_id"])
        self._update(wf, status="RUNNING" if approved else "BLOCKED")
        self.db.audit("GATE_DECIDED", project_id=gate["project_id"], object_id=gate_id, metadata={"gate_type": gate["gate_type"], "status": status, "decided_role": decided_role})
        return self._gate(gate_id)

    def list_gates(self, project_id: str | None = None, workflow_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM gates WHERE 1=1"
        params: list[Any] = []
        if project_id:
            sql += " AND project_id=?"; params.append(project_id)
        if workflow_id:
            sql += " AND workflow_id=?"; params.append(workflow_id)
        sql += " ORDER BY created_at DESC"
        return [self._decode_gate(r) for r in self.db.fetchall(sql, tuple(params))]

    def _gate(self, gate_id: str) -> dict[str, Any]:
        row = self.db.fetchone("SELECT * FROM gates WHERE id=?", (gate_id,))
        if not row:
            raise KeyError(gate_id)
        return self._decode_gate(row)

    @staticmethod
    def _decode_gate(row: dict[str, Any]) -> dict[str, Any]:
        row["allowed_actions"] = json.loads(row.pop("allowed_actions_json"))
        row["questions"] = json.loads(row.pop("questions_json"))
        row["decision"] = json.loads(row.pop("decision_json")) if row.get("decision_json") else None
        return row

    def _open_gate(self, workflow_id: str) -> dict[str, Any] | None:
        return self.db.fetchone("SELECT * FROM gates WHERE workflow_id=? AND status='OPEN' ORDER BY created_at DESC LIMIT 1", (workflow_id,))

    def _project_level(self, project_id: str) -> str:
        row = self.db.fetchone("SELECT security_level FROM projects WHERE id=?", (project_id,))
        return row["security_level"] if row else "INTERNAL"

    def _update(self, wf: dict[str, Any], *, status: str | None = None, current_step: int | None = None, state: dict[str, Any] | None = None) -> None:
        self.db.execute(
            "UPDATE workflows SET status=?,current_step=?,state_json=?,updated_at=? WHERE id=?",
            (status or wf["status"], wf["current_step"] if current_step is None else current_step, json.dumps(state if state is not None else wf["state"], ensure_ascii=False), utc_now(), wf["id"]),
        )
