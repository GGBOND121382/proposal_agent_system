from __future__ import annotations

import json
from typing import Any

from .util import new_id, sha256_json, utc_now
from .workflow_defs import GATE_ACTIONS, GATE_ROLE
from .wf3_input import WF3_INPUT_GATE_TYPE, options_from_gate_answers
from .workflow_input import (
    MATERIAL_INPUT_GATE_TYPES,
    build_human_resolutions,
    resolution_overrides,
)


class WorkflowGateMixin:
    def _create_gate(self, wf: dict[str, Any], gate_type: str, *, target_id: str, questions: list[dict[str, Any]]) -> str:
        existing = self.db.fetchone("SELECT id FROM gates WHERE workflow_id=? AND gate_type=? AND status='OPEN'", (wf["id"], gate_type))
        if existing:
            return existing["id"]
        gate_id = new_id("gate")
        context_hash = sha256_json({"workflow": wf["id"], "step": wf["current_step"], "state": wf["state"]})
        allowed = list(GATE_ACTIONS.get(gate_type, ["CONFIRM", "RETURN", "REJECT", "CANCEL"]))
        security_decision_gates = {
            "OUTBOUND_SECURITY_APPROVAL",
            "ONLINE_RESULT_IMPORT_APPROVAL",
            "FINAL_CONTENT_SECURITY_APPROVAL",
            "FINAL_EXPORT_APPROVAL",
        }
        if questions and gate_type not in security_decision_gates and "PROVIDE_INFORMATION" not in allowed:
            allowed.insert(0, "PROVIDE_INFORMATION")
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
        wf = self.get(gate["workflow_id"])
        next_step = wf["current_step"]
        state = wf["state"]
        questions = json.loads(gate["questions_json"])
        if approved and gate["gate_type"] == WF3_INPUT_GATE_TYPE:
            state["options"] = options_from_gate_answers(
                current_options=state.get("options") or {},
                questions=questions,
                answers=answers or [],
            )
            state.pop("workflow_input_required", None)
            state.pop("last_error", None)
            state.setdefault("technical_retry_attempts", {}).pop(str(wf["current_step"]), None)
            state["wf3_input_resolution"] = {
                "origin": "USER_INPUT_GATE",
                "target_task_type": state["options"].get("target_task_type"),
            }
        if approved and gate["gate_type"] in MATERIAL_INPUT_GATE_TYPES:
            state.pop("workflow_input_required", None)
            state.pop("last_error", None)
            state.setdefault("technical_retry_attempts", {}).pop(str(wf["current_step"]), None)

        current_result = state.get("step_results", {}).get(str(wf["current_step"])) or {}
        target_prompt_id = str(
            current_result.get("prompt_id")
            or (state.get("workflow_input_required") or {}).get("prompt_id")
            or ""
        )
        resolutions: list[dict[str, Any]] = []
        if approved and answers and target_prompt_id and gate["gate_type"] not in MATERIAL_INPUT_GATE_TYPES:
            resolutions = build_human_resolutions(
                gate_id=gate_id,
                prompt_id=target_prompt_id,
                questions=questions,
                answers=answers,
                decided_by=decided_by,
                decided_role=decided_role,
            )
            if resolutions:
                stored = state.setdefault("human_resolutions", {})
                prompt_resolutions = stored.setdefault(target_prompt_id, [])
                prompt_resolutions.extend(resolutions)
                del prompt_resolutions[:-50]
                state.setdefault("human_input_overrides", {}).setdefault(target_prompt_id, {}).update(
                    resolution_overrides(resolutions)
                )
        status = "APPROVED" if approved else ("CANCELLED" if action == "CANCEL" else "REJECTED")
        decision = {"action": action, "comment": comment, "answers": answers or [], "decided_by": decided_by, "decided_role": decided_role, "decided_at": utc_now(), "context_hash": gate["context_hash"]}
        self.db.execute("UPDATE gates SET status=?,decision_json=?,updated_at=? WHERE id=?", (status, json.dumps(decision, ensure_ascii=False), utc_now(), gate_id))
        if approved:
            if (
                gate.get("target_id") == current_result.get("run_id")
                and current_result.get("status") in {"REVISE", "NEED_USER_INPUT"}
            ):
                if resolutions and action in {"PROVIDE_INFORMATION", "RESOLVE"}:
                    step_key = str(wf["current_step"])
                    state.setdefault("superseded_step_results", {}).setdefault(step_key, []).append(current_result)
                    state.get("step_results", {}).pop(step_key, None)
                    state.setdefault("repair_attempts", {})[target_prompt_id] = int(
                        state.setdefault("repair_attempts", {}).get(target_prompt_id, 0)
                    ) + 1
                    state["rerun_from_human_input"] = {
                        "gate_id": gate_id,
                        "prompt_id": target_prompt_id,
                        "resolution_ids": [item["resolution_id"] for item in resolutions],
                    }
                    state.pop("last_error", None)
                else:
                    state.setdefault("accepted_step_results", {})[str(wf["current_step"])] = {
                        "run_id": current_result["run_id"],
                        "status": current_result["status"],
                        "gate_id": gate_id,
                        "action": action,
                        "answers": answers or [],
                    }
                    next_step += 1
        self._update(
            wf,
            status="RUNNING" if approved else "BLOCKED",
            current_step=next_step,
            state=state,
        )
        self.db.audit("GATE_DECIDED", project_id=gate["project_id"], object_id=gate_id, metadata={"gate_type": gate["gate_type"], "status": status, "decided_role": decided_role})
        if approved and gate["gate_type"] == WF3_INPUT_GATE_TYPE:
            self.db.audit(
                "WF3_RESEARCH_NEED_PROVIDED",
                project_id=gate["project_id"],
                object_id=gate["workflow_id"],
                metadata={
                    "gate_id": gate_id,
                    "target_task_type": state.get("options", {}).get("target_task_type"),
                    "answer_count": len(answers or []),
                },
            )
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
