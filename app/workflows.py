from __future__ import annotations

import json
from typing import Any

from .executor import PromptExecutionError
from .quality import QualityGateBlocked, QualityLifecycleManager
from .research import PublicResearchError
from .util import new_id, sha256_json, utc_now
from .workflow_authoring import WorkflowAuthoringMixin
from .workflow_defs import WORKFLOWS
from .workflow_gates import WorkflowGateMixin
from .workflow_repair import WorkflowRepairMixin


class WorkflowEngine(WorkflowAuthoringMixin, WorkflowRepairMixin, WorkflowGateMixin):
    def __init__(self, db, pack, context_builder, executor, research_service, diagram_enrichment=None, quality_manager=None):
        self.db = db
        self.pack = pack
        self.context_builder = context_builder
        self.executor = executor
        self.research_service = research_service
        self.diagram_enrichment = diagram_enrichment
        self.quality_manager = quality_manager or QualityLifecycleManager(db)


    def _observe_quality_result(self, wf: dict[str, Any], state: dict[str, Any], prompt_id: str, result: dict[str, Any]) -> None:
        quality_workflow_id = str(state.get("quality_parent_workflow_id") or wf["id"])
        self.quality_manager.observe_prompt_result(
            project_id=wf["project_id"],
            workflow_id=quality_workflow_id,
            prompt_id=prompt_id,
            run_id=result["run_id"],
            status=result["status"],
            output=result["output"],
            workflow_state=state,
        )

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
        if workflow_type == "WF-5_SECURITY_REVIEW_AND_EXPORT":
            source = self._resolve_source_wf4(
                project_id,
                str((options or {}).get("source_workflow_id") or "") or None,
            )
            if source:
                state["source_workflow_id"] = source["id"]
                state["source_candidate_set_hash"] = source["candidate_set_hash"]
                state["source_binding_mode"] = source.get("binding_mode", "FULL_INTEGRATION_MANIFEST")
                state["source_section_manifest"] = source.get("section_manifest") or []
        prerequisite_error = self._workflow_prerequisite_error(project_id, workflow_type, options or {})
        status = "BLOCKED" if prerequisite_error else "RUNNING"
        if prerequisite_error:
            state["last_error"] = prerequisite_error
        self.db.execute(
            "INSERT INTO workflows(id,project_id,workflow_type,status,current_step,state_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (workflow_id, project_id, workflow_type, status, 0, json.dumps(state, ensure_ascii=False), now, now),
        )
        self.db.audit("WORKFLOW_STARTED", project_id=project_id, object_id=workflow_id, metadata={"workflow_type": workflow_type})
        return self.get(workflow_id)

    def _resolve_source_wf4(
        self,
        project_id: str,
        requested_workflow_id: str | None = None,
    ) -> dict[str, Any] | None:
        params: tuple[Any, ...]
        if requested_workflow_id:
            rows = self.db.fetchall(
                "SELECT id,state_json,status,updated_at FROM workflows "
                "WHERE project_id=? AND workflow_type='WF-4_PROPOSAL_AUTHORING' AND id=?",
                (project_id, requested_workflow_id),
            )
        else:
            rows = self.db.fetchall(
                "SELECT id,state_json,status,updated_at FROM workflows "
                "WHERE project_id=? AND workflow_type='WF-4_PROPOSAL_AUTHORING' "
                "AND status='COMPLETED' ORDER BY updated_at DESC,id DESC",
                (project_id,),
            )
        for row in rows:
            state = json.loads(row.get("state_json") or "{}")
            if state.get("parent_workflow_id") or row.get("status") != "COMPLETED":
                continue
            reviews = [
                item for item in state.get("full_proposal_review_history") or []
                if isinstance(item, dict) and item.get("status") == "PASS"
            ]
            if reviews:
                review = reviews[-1]
                manifest = review.get("section_manifest") or []
                if not manifest:
                    continue
                return {
                    "id": row["id"],
                    "candidate_set_hash": str(review.get("candidate_set_hash") or ""),
                    "section_manifest": manifest,
                    "binding_mode": "FULL_INTEGRATION_MANIFEST",
                }

            # Backward-compatible recovery for a completed pre-concurrent WF-4.
            # It is accepted only when the persisted workflow contains a PASS
            # Integration Critic and every completed section has exact PASS polish
            # and expression-critic run IDs.  This is checkpoint migration, not a
            # project-wide latest-candidate fallback.
            integration_pass = any(
                isinstance(item, dict)
                and item.get("prompt_id") == "P-INTEGRATION-CRITIC"
                and item.get("status") == "PASS"
                for item in (state.get("step_results") or {}).values()
            )
            if not integration_pass:
                continue
            manifest: list[dict[str, str]] = []
            for section in state.get("section_results") or []:
                if not isinstance(section, dict) or section.get("status") != "COMPLETED":
                    continue
                runs = [item for item in section.get("runs") or [] if isinstance(item, dict)]
                polish = next(
                    (item for item in reversed(runs) if item.get("prompt_id") == "P-EXPRESSION-POLISH" and item.get("status") == "PASS"),
                    None,
                )
                critic = next(
                    (item for item in reversed(runs) if item.get("prompt_id") == "P-EXPRESSION-CRITIC" and item.get("status") == "PASS"),
                    None,
                )
                if not polish or not critic:
                    manifest = []
                    break
                polish_row = self.db.fetchone(
                    "SELECT output_json,status FROM prompt_runs WHERE project_id=? AND workflow_id=? AND id=? ",
                    (project_id, row["id"], str(polish.get("run_id") or "")),
                )
                if not polish_row or polish_row.get("status") != "PASS" or not polish_row.get("output_json"):
                    manifest = []
                    break
                output = json.loads(polish_row.get("output_json") or "{}")
                candidate_id = str((output.get("result") or {}).get("candidate_id") or "")
                if not candidate_id:
                    manifest = []
                    break
                manifest.append({
                    "section_id": str(section.get("section_id") or ""),
                    "candidate_id": candidate_id,
                    "polish_run_id": str(polish.get("run_id") or ""),
                    "expression_critic_run_id": str(critic.get("run_id") or ""),
                })
            if not manifest:
                continue
            return {
                "id": row["id"],
                "candidate_set_hash": sha256_json({"sections": manifest}),
                "section_manifest": manifest,
                "binding_mode": "MIGRATED_LEGACY_CHECKPOINT",
            }
        return None

    def _workflow_prerequisite_error(self, project_id: str, workflow_type: str, options: dict[str, Any]) -> str | None:
        required: list[str] = []
        if workflow_type == "WF-3_HYBRID_ONLINE_ASSIST":
            required = ["WF-1_PROJECT_INTAKE"]
        elif workflow_type == "WF-4_PROPOSAL_AUTHORING":
            required = ["WF-1_PROJECT_INTAKE", "WF-2_TEMPLATE_EXTRACTION"]
            project = self.db.fetchone("SELECT config_json FROM projects WHERE id=?", (project_id,)) or {}
            config = json.loads(project.get("config_json") or "{}")
            if bool(options.get("require_public_research", config.get("require_public_research", False))):
                required.append("WF-3_HYBRID_ONLINE_ASSIST")
        elif workflow_type == "WF-5_SECURITY_REVIEW_AND_EXPORT":
            required = ["WF-4_PROPOSAL_AUTHORING"]
        missing = []
        for required_type in required:
            if required_type == "WF-4_PROPOSAL_AUTHORING":
                row = self._resolve_source_wf4(
                    project_id,
                    str(options.get("source_workflow_id") or "") or None,
                )
            else:
                row = self.db.fetchone(
                    "SELECT id FROM workflows WHERE project_id=? AND workflow_type=? AND status='COMPLETED' ORDER BY updated_at DESC LIMIT 1",
                    (project_id, required_type),
                )
            if not row:
                missing.append(required_type)
        if missing:
            return "工作流前置条件未满足：" + "、".join(missing) + "。不得使用Replay样例或空上下文代替已完成的前序结果。"
        return None

    @staticmethod
    def _has_nonconfirmable_quality_failure(output: dict[str, Any]) -> bool:
        """Return true when confirmation cannot repair the generated object.

        QG findings are produced by deterministic proposal-quality validation.  A
        human may supply missing source material or make an explicit project
        decision, but merely confirming the unchanged model output cannot repair
        cloned plans, incomplete critic coverage, document-type drift or invalid
        source mappings.
        """
        for item in output.get("findings", []):
            if not isinstance(item, dict) or not str(item.get("code", "")).startswith("QG_"):
                continue
            if not item.get("blocking", True):
                continue
            suggested = str(item.get("suggested_route") or "")
            if suggested not in {"USER", "PROJECT_OWNER"}:
                return True
        return False

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
            if step.get("type") == "WRITE_SECTIONS":
                try:
                    result = await self._write_sections(wf, state)
                except (ValueError, KeyError) as exc:
                    state["last_error"] = str(exc)
                    self._update(wf, status="BLOCKED", state=state)
                    return self.get(workflow_id)
                if result is not None:
                    return result
                wf = self.get(workflow_id)
                state = wf["state"]
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
                if prompt_id == "P-INTEGRATION-CRITIC":
                    self._validate_three_section_integration_envelope(state, envelope)
                    self._validate_full_proposal_integration_envelope(state, envelope)
                result = await self.executor.execute(prompt_id, envelope, project_id=wf["project_id"], workflow_id=workflow_id, original_environment=state.get("original_environment"))
            except (PromptExecutionError, ValueError, KeyError) as exc:
                state["last_error"] = str(exc)
                self._update(wf, status="BLOCKED", state=state)
                return self.get(workflow_id)

            state["step_results"][str(wf["current_step"])] = {"prompt_id": prompt_id, "run_id": result["run_id"], "status": result["status"]}
            state["original_environment"] = result["route"]["environment"]
            output = result["output"]
            if prompt_id == "P-PUBLIC-RESEARCH-SYNTHESIS" and result["status"] == "PASS":
                claim_validation = self.research_service.validate_synthesis(
                    output.get("result") or {},
                    state.get("public_search_results") or {},
                )
                state["public_claim_validation"] = claim_validation
                if claim_validation.get("status") != "PASS":
                    codes = [str(item.get("code") or "PUBLIC_CLAIM_INVALID") for item in claim_validation.get("findings", [])]
                    state["last_error"] = (
                        "公开研究综合未通过确定性 Claim—来源绑定校验："
                        + "、".join(codes[:12])
                        + "。不得进入公开结果导入 Gate。"
                    )
                    self._update(wf, status="BLOCKED", state=state)
                    return self.get(workflow_id)
                self._update(wf, state=state)
            self._observe_quality_result(wf, state, prompt_id, result)
            if prompt_id == "P-INTEGRATION-CRITIC" and self._three_section_mode(state):
                state.setdefault("cross_section_review_history", []).append({
                    "run_id": result["run_id"],
                    "status": result["status"],
                    "finding_codes": [
                        str(item.get("code") or "")
                        for item in output.get("findings") or []
                        if isinstance(item, dict)
                    ],
                    "contract_section_ids": [
                        str(item.get("section_id"))
                        for item in (state.get("three_section_contract") or {}).get("sections") or []
                        if isinstance(item, dict) and item.get("section_id")
                    ],
                })
                self._update(wf, state=state)
            if prompt_id == "P-INTEGRATION-CRITIC" and self._full_proposal_mode(state):
                try:
                    self._record_full_integration_review(wf, state, result)
                except ValueError as exc:
                    state["last_error"] = str(exc)
                    self._update(wf, status="BLOCKED", state=state)
                    return self.get(workflow_id)
            if result["status"] == "BLOCK":
                self._update(wf, status="BLOCKED", state=state)
                return self.get(workflow_id)
            if prompt_id == "P-INTEGRATION-CRITIC" and result["status"] == "REVISE":
                repair_state = self._prepare_integration_repair(wf, state, output)
                if repair_state == "SCHEDULED":
                    wf = self.get(workflow_id)
                    state = wf["state"]
                    continue
                if repair_state == "EXHAUSTED":
                    return self.get(workflow_id)
            if result["status"] == "REVISE" and self._can_auto_repair(prompt_id, state):
                repaired = await self._auto_repair(wf, prompt_id, envelope, output, state)
                if repaired:
                    continue
            if result["status"] == "REVISE" and self._has_nonconfirmable_quality_failure(output):
                codes = [str(item.get("code")) for item in output.get("findings", []) if str(item.get("code", "")).startswith("QG_")]
                state["last_error"] = (
                    f"{prompt_id} 未通过确定性质量校验：" + "、".join(codes[:8])
                    + "。该问题必须由对应生产/审查阶段重新生成或补充证据，不能通过人工空确认覆盖。"
                )
                self._update(wf, status="BLOCKED", state=state)
                return self.get(workflow_id)
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

        try:
            if wf["workflow_type"] == "WF-5_SECURITY_REVIEW_AND_EXPORT":
                self.quality_manager.assert_no_active_lineage_blockers(
                    wf["project_id"], review_workflow_id=workflow_id
                )
            else:
                self.quality_manager.assert_no_open_blockers(
                    wf["project_id"], workflow_id=workflow_id
                )
        except QualityGateBlocked as exc:
            state["last_error"] = str(exc) + "。必须记录修复运行并由独立Critic复审，人工确认或直接改库均不能放行。"
            state["quality_blocker_ids"] = [item.get("finding_id") for item in exc.findings]
            self._update(wf, status="BLOCKED", state=state)
            return self.get(workflow_id)
        self._update(wf, status="COMPLETED", state=state)
        self.db.audit("WORKFLOW_COMPLETED", project_id=wf["project_id"], object_id=workflow_id, metadata={"workflow_type": wf["workflow_type"]})
        return self.get(workflow_id)
