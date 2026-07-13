from __future__ import annotations

import json
from typing import Any

from .executor import PromptExecutionError
from .research import PublicResearchError
from .util import new_id, sha256_json, utc_now

WORKFLOWS: dict[str, list[dict[str, Any]]] = {
    "WF-1_PROJECT_INTAKE": [
        {"prompt_id": "P-SECURITY-CLASSIFY"},
        {"prompt_id": "P-SECURITY-CLASSIFY-CRITIC"},
        {"prompt_id": "P-SCHEME-EXTRACT"},
        {"prompt_id": "P-SCHEME-CRITIC"},
        {"prompt_id": "P-PROJECT-DEFINITION-EXTRACT"},
        {"prompt_id": "P-PROJECT-DEFINITION-CRITIC"},
        {"prompt_id": "P-FACT-EXTRACT"},
        {"prompt_id": "P-FACT-CRITIC"},
        {"prompt_id": "P-PROJECT-READINESS-CRITIC"},
    ],
    "WF-2_TEMPLATE_EXTRACTION": [
        {"prompt_id": "P-TEMPLATE-EXTRACT"},
        {"prompt_id": "P-TEMPLATE-CRITIC"},
    ],
    "WF-3_HYBRID_ONLINE_ASSIST": [
        {"prompt_id": "P-SAFE-ONLINE-PACKAGE"},
        {"prompt_id": "P-SAFE-ONLINE-PACKAGE-CRITIC"},
        {"prompt_id": "P-PUBLIC-RESEARCH-PLAN"},
        {"type": "PUBLIC_SEARCH"},
        {"prompt_id": "P-PUBLIC-RESEARCH-SYNTHESIS"},
        {"prompt_id": "P-PUBLIC-RESEARCH-CRITIC"},
        {"prompt_id": "P-ONLINE-RESULT-IMPORT-CRITIC"},
    ],
    "WF-4_PROPOSAL_AUTHORING": [
        {"prompt_id": "P-REVISION-PLAN"},
        {"prompt_id": "P-REVISION-PLAN-CRITIC"},
        {"prompt_id": "P-WRITE-BLUEPRINT"},
        {"prompt_id": "P-WRITE-BLUEPRINT-CRITIC"},
        {"prompt_id": "P-WRITE-CONTENT"},
        {"prompt_id": "P-WRITE-CRITIC"},
        {"prompt_id": "P-INTEGRATION-CRITIC"},
    ],
    "WF-5_SECURITY_REVIEW_AND_EXPORT": [
        {"prompt_id": "P-FINAL-CONFIDENTIALITY-REVIEW"},
        {"type": "GATE", "gate_type": "FINAL_EXPORT_APPROVAL"},
    ],
}

GATE_ROLE = {
    "SCHEME_CONFIRMATION": "PROJECT_OWNER",
    "PROJECT_DEFINITION_CONFIRMATION": "PROJECT_OWNER",
    "PROJECT_GAP_RESOLUTION": "PROJECT_OWNER",
    "FACT_CONFIRMATION": "PROJECT_OWNER",
    "FACT_CONFLICT_RESOLUTION": "PROJECT_OWNER",
    "TEMPLATE_CONFIRMATION": "CONTENT_OPERATOR",
    "TECHNICAL_OR_METRIC_INFORMATION": "PROJECT_OWNER",
    "PLAN_CONFIRMATION": "PROJECT_OWNER",
    "CANDIDATE_REVIEW": "CONTENT_OPERATOR",
    "OUTBOUND_SECURITY_APPROVAL": "SECURITY_REVIEWER",
    "ONLINE_RESULT_IMPORT_APPROVAL": "SECURITY_REVIEWER",
    "FINAL_CONTENT_SECURITY_APPROVAL": "SECURITY_REVIEWER",
    "FINAL_EXPORT_APPROVAL": "EXPORT_APPROVER",
}

GATE_ACTIONS = {
    "OUTBOUND_SECURITY_APPROVAL": ["APPROVE", "RETURN", "REJECT", "CANCEL"],
    "ONLINE_RESULT_IMPORT_APPROVAL": ["APPROVE", "RETURN", "REJECT", "CANCEL"],
    "FINAL_CONTENT_SECURITY_APPROVAL": ["APPROVE", "RETURN", "REJECT", "CANCEL"],
    "FINAL_EXPORT_APPROVAL": ["APPROVE", "RETURN", "REJECT", "CANCEL"],
}

CRITIC_PRODUCER = {
    "P-SECURITY-CLASSIFY-CRITIC": "P-SECURITY-CLASSIFY",
    "P-SAFE-ONLINE-PACKAGE-CRITIC": "P-SAFE-ONLINE-PACKAGE",
    "P-PUBLIC-RESEARCH-CRITIC": "P-PUBLIC-RESEARCH-SYNTHESIS",
    "P-SCHEME-CRITIC": "P-SCHEME-EXTRACT",
    "P-PROJECT-DEFINITION-CRITIC": "P-PROJECT-DEFINITION-EXTRACT",
    "P-FACT-CRITIC": "P-FACT-EXTRACT",
    "P-TEMPLATE-CRITIC": "P-TEMPLATE-EXTRACT",
    "P-REVISION-PLAN-CRITIC": "P-REVISION-PLAN",
    "P-WRITE-BLUEPRINT-CRITIC": "P-WRITE-BLUEPRINT",
    "P-WRITE-CRITIC": "P-WRITE-CONTENT",
}


class WorkflowEngine:
    def __init__(self, db, pack, context_builder, executor, research_service):
        self.db = db
        self.pack = pack
        self.context_builder = context_builder
        self.executor = executor
        self.research_service = research_service

    def start(self, project_id: str, workflow_type: str, options: dict[str, Any] | None = None) -> dict[str, Any]:
        if workflow_type not in WORKFLOWS:
            raise KeyError(f"Unknown workflow: {workflow_type}")
        if not self.db.fetchone("SELECT id FROM projects WHERE id=?", (project_id,)):
            raise KeyError(f"Project not found: {project_id}")
        workflow_id = new_id("wf")
        now = utc_now()
        state = {
            "workflow_type": workflow_type,
            "options": options or {},
            "step_results": {},
            "repair_attempts": {},
            "repair_overrides": {},
            "public_search_results": None,
        }
        self.db.execute(
            "INSERT INTO workflows(id,project_id,workflow_type,status,current_step,state_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (workflow_id, project_id, workflow_type, "RUNNING", 0, json.dumps(state, ensure_ascii=False), now, now),
        )
        self.db.audit("WORKFLOW_STARTED", project_id=project_id, object_id=workflow_id, metadata={"workflow_type": workflow_type})
        return self.get(workflow_id)

    def get(self, workflow_id: str) -> dict[str, Any]:
        row = self.db.fetchone("SELECT * FROM workflows WHERE id=?", (workflow_id,))
        if not row:
            raise KeyError(f"Workflow not found: {workflow_id}")
        row["state"] = json.loads(row.pop("state_json"))
        row["steps"] = WORKFLOWS[row["workflow_type"]]
        return row

    async def advance(self, workflow_id: str) -> dict[str, Any]:
        wf = self.get(workflow_id)
        if wf["status"] in {"COMPLETED", "BLOCKED", "CANCELLED"}:
            return wf
        if self._open_gate(workflow_id):
            self._update(wf, status="WAITING_GATE")
            return self.get(workflow_id)

        wf["status"] = "RUNNING"
        steps = WORKFLOWS[wf["workflow_type"]]
        state = wf["state"]
        while wf["current_step"] < len(steps):
            step = steps[wf["current_step"]]
            if step.get("type") == "PUBLIC_SEARCH":
                try:
                    await self._run_public_search(wf, state)
                except PublicResearchError as exc:
                    state["last_error"] = str(exc)
                    self._update(wf, status="BLOCKED", state=state)
                    return self.get(workflow_id)
                wf["current_step"] += 1
                self._update(wf, current_step=wf["current_step"], state=state)
                continue
            if step.get("type") == "GATE":
                wf["current_step"] += 1
                self._update(wf, current_step=wf["current_step"], state=state)
                refreshed = self.get(workflow_id)
                self._create_gate(refreshed, step["gate_type"], target_id=workflow_id, questions=[])
                self._update(refreshed, status="WAITING_GATE", state=state)
                return self.get(workflow_id)

            prompt_id = step["prompt_id"]
            try:
                envelope = self.context_builder.build(prompt_id, wf["project_id"], workflow_id=workflow_id, workflow_state=state)
                result = await self.executor.execute(prompt_id, envelope, project_id=wf["project_id"], workflow_id=workflow_id, original_environment=state.get("original_environment"))
            except (PromptExecutionError, ValueError, KeyError) as exc:
                state["last_error"] = str(exc)
                self._update(wf, status="BLOCKED", state=state)
                return self.get(workflow_id)

            state["step_results"][str(wf["current_step"])] = {"prompt_id": prompt_id, "run_id": result["run_id"], "status": result["status"]}
            state["original_environment"] = result["route"]["environment"]
            output = result["output"]
            if result["status"] == "BLOCK":
                self._update(wf, status="BLOCKED", state=state)
                return self.get(workflow_id)
            if result["status"] == "REVISE" and self._can_auto_repair(prompt_id, state):
                repaired = await self._auto_repair(wf, prompt_id, envelope, output, state)
                if repaired:
                    continue
            if result["status"] in {"REVISE", "NEED_USER_INPUT"}:
                gate_type = self.pack.entry(prompt_id).get("next_human_gate") or "PROJECT_GAP_RESOLUTION"
                self._create_gate(wf, gate_type, target_id=result["run_id"], questions=output.get("user_questions", []))
                self._update(wf, status="WAITING_GATE", state=state)
                return self.get(workflow_id)

            next_gate = self.pack.entry(prompt_id).get("next_human_gate")
            wf["current_step"] += 1
            self._update(wf, current_step=wf["current_step"], state=state)
            if next_gate:
                self._create_gate(wf, next_gate, target_id=result["run_id"], questions=output.get("user_questions", []))
                self._update(wf, status="WAITING_GATE", state=state)
                return self.get(workflow_id)

        self._update(wf, status="COMPLETED", state=state)
        self.db.audit("WORKFLOW_COMPLETED", project_id=wf["project_id"], object_id=workflow_id, metadata={"workflow_type": wf["workflow_type"]})
        return self.get(workflow_id)

    async def _run_public_search(self, wf: dict[str, Any], state: dict[str, Any]) -> None:
        if self.executor.gateway.settings.runtime_mode in {"REPLAY", "MOCK"}:
            state["public_search_results"] = {"sources": [], "passages": [], "queries": [], "mode": self.executor.gateway.settings.runtime_mode}
            return
        plan = self.context_builder._result(wf["project_id"], "P-PUBLIC-RESEARCH-PLAN") or {}
        state["public_search_results"] = await self.research_service.search(plan)

    def _can_auto_repair(self, prompt_id: str, state: dict[str, Any]) -> bool:
        if prompt_id not in CRITIC_PRODUCER:
            return False
        return int(state["repair_attempts"].get(prompt_id, 0)) < 1

    async def _auto_repair(self, wf: dict[str, Any], critic_prompt: str, critic_input: dict[str, Any], critic_output: dict[str, Any], state: dict[str, Any]) -> bool:
        producer = CRITIC_PRODUCER[critic_prompt]
        findings = critic_output.get("findings", [])
        if not findings:
            return False
        state["repair_attempts"][critic_prompt] = int(state["repair_attempts"].get(critic_prompt, 0)) + 1
        original = self.context_builder._result(wf["project_id"], producer)
        overrides = {
            "payload.original_object": original or {},
            "payload.original_producer": producer,
            "payload.findings_to_repair": findings,
            "payload.allowed_paths": [f.get("target_path", "result") for f in findings],
        }
        try:
            envelope = self.context_builder.build("P-TARGETED-REPAIR", wf["project_id"], workflow_id=wf["id"], workflow_state=state, overrides=overrides)
            repaired = await self.executor.execute("P-TARGETED-REPAIR", envelope, project_id=wf["project_id"], workflow_id=wf["id"], original_environment=state.get("original_environment", "OFFLINE_LOCAL"))
        except PromptExecutionError:
            return False
        if repaired["status"] != "PASS":
            return False
        state["repair_overrides"][producer] = repaired["output"]["result"]["repaired_object"]
        self._update(wf, state=state)
        return True

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
