from __future__ import annotations

import json
from typing import Any

from .executor import PromptExecutionError
from .quality import QualityGateBlocked, QualityLifecycleManager
from .research import PublicResearchError
from .util import new_id, utc_now
from .workflow_authoring import WorkflowAuthoringMixin
from .workflow_defs import WORKFLOWS
from .workflow_gates import WorkflowGateMixin
from .workflow_repair import WorkflowRepairMixin
from .wf3_input import WorkflowInputRequired


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

    @staticmethod
    def _unaccepted_completion_blockers(
        blockers: list[dict[str, Any]],
        state: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Keep accepted intake gaps open without blocking completion of the stage.

        A gate decision does not verify or close a quality finding. It can accept
        a NEED_USER_INPUT/REVISE result as the documented output of an intake
        stage. The finding stays open for downstream repair and final export.
        Deterministic QG findings are never eligible for this stage acceptance.
        """
        accepted_run_ids = {
            str(item.get("run_id"))
            for item in (state.get("accepted_step_results") or {}).values()
            if isinstance(item, dict) and item.get("run_id")
        }
        remaining: list[dict[str, Any]] = []
        accepted: list[dict[str, Any]] = []
        for record in blockers:
            finding = record.get("finding") or {}
            code = str(finding.get("code") or "")
            opened_run_id = str(
                (record.get("lifecycle") or {}).get("opened_by", {}).get("run_id") or ""
            )
            if opened_run_id in accepted_run_ids and not code.startswith("QG_"):
                accepted.append(record)
            else:
                remaining.append(record)
        return remaining, accepted

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
                # Concurrent authoring groups reuse the frozen WF-4 state machine
                # but are explicitly marked as child workflows.  A completed
                # group is not a completed proposal and must never unlock WF-5.
                rows = self.db.fetchall(
                    "SELECT id,state_json FROM workflows WHERE project_id=? AND workflow_type=? AND status='COMPLETED' ORDER BY updated_at DESC",
                    (project_id, required_type),
                )
                row = next(
                    (
                        item for item in rows
                        if not json.loads(item.get("state_json") or "{}").get("parent_workflow_id")
                    ),
                    None,
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

    @staticmethod
    def _is_legacy_wf3_input_block(wf: dict[str, Any], state: dict[str, Any]) -> bool:
        if wf.get("workflow_type") != "WF-3_HYBRID_ONLINE_ASSIST" or int(wf.get("current_step") or 0) != 0:
            return False
        if str(wf.get("status") or "") != "BLOCKED":
            return False
        if str(0) in (state.get("step_results") or {}):
            return False
        last_error = str(state.get("last_error") or "")
        return (
            "P-SAFE-ONLINE-PACKAGE" in last_error
            and (
                "unresolved schema scaffold" in last_error
                or "payload.research_need" in last_error
                or "WF-3 缺少可批准的公开研究问题" in last_error
            )
        )

    def get(self, workflow_id: str) -> dict[str, Any]:
        row = self.db.fetchone("SELECT * FROM workflows WHERE id=?", (workflow_id,))
        if not row:
            raise KeyError(f"Workflow not found: {workflow_id}")
        row["state"] = json.loads(row.pop("state_json"))
        row["steps"] = WORKFLOWS[row["workflow_type"]]
        return row

    async def advance(self, workflow_id: str) -> dict[str, Any]:
        wf = self.get(workflow_id)
        if wf["status"] in {"COMPLETED", "CANCELLED"}:
            return wf
        if self._is_legacy_wf3_input_block(wf, wf["state"]):
            state = wf["state"]
            state["recovered_from"] = state.get("last_error") or "LEGACY_WF3_INPUT_BLOCK"
            state.pop("last_error", None)
            state.pop("runtime_recoverable", None)
            state.setdefault("technical_retry_attempts", {}).pop(str(wf["current_step"]), None)
            self._update(wf, status="RUNNING", state=state)
            wf = self.get(workflow_id)
        if wf["status"] == "BLOCKED":
            state = wf["state"]
            step_key = str(wf["current_step"])
            steps = WORKFLOWS[wf["workflow_type"]]
            retries = state.setdefault("technical_retry_attempts", {})
            retry_limit = 6 if state.get("options", {}).get("acceptance_run") else 2
            current_prompt_id = (
                str(steps[wf["current_step"]].get("prompt_id") or "")
                if wf["current_step"] < len(steps)
                else ""
            )
            current_scope = f"stage:{current_prompt_id}" if current_prompt_id else ""
            has_current_deterministic_blocker = any(
                str((record.get("finding") or {}).get("code") or "").startswith("QG_")
                and str(record.get("scope_key") or "") == current_scope
                for record in self.quality_manager.open_blockers(
                    wf["project_id"],
                    workflow_id=workflow_id,
                )
            )
            deterministic_recheck = (
                wf["current_step"] < len(steps)
                and step_key in state.get("step_results", {})
                and (
                    "确定性质量校验" in str(state.get("last_error") or "")
                    or has_current_deterministic_blocker
                )
                and int(retries.get(step_key, 0)) < retry_limit
            )
            normalizer_version = str(
                getattr(self.executor, "output_normalizer_version", "") or ""
            )
            migration_versions = state.setdefault(
                "contract_migration_retry_versions",
                {},
            )
            failed_provider_output_exists = False
            if (
                normalizer_version
                and current_prompt_id
                and step_key not in state.get("step_results", {})
            ):
                failed_provider_output_exists = bool(
                    self.db.fetchone(
                        """SELECT id FROM prompt_runs
                           WHERE project_id=? AND workflow_id=? AND prompt_id=?
                             AND status='ERROR' AND output_json IS NOT NULL
                           ORDER BY created_at DESC LIMIT 1""",
                        (wf["project_id"], workflow_id, current_prompt_id),
                    )
                )
            contract_migration_retryable = (
                failed_provider_output_exists
                and str(migration_versions.get(step_key) or "") != normalizer_version
            )
            technical_retryable = (
                wf["current_step"] < len(steps)
                and (
                    step_key not in state.get("step_results", {})
                    or deterministic_recheck
                )
                and (
                    int(retries.get(step_key, 0)) < retry_limit
                    or contract_migration_retryable
                )
            )
            completion_recheck = (
                wf["current_step"] >= len(steps)
                and bool(state.get("quality_blocker_ids"))
            )
            if not technical_retryable and not completion_recheck:
                return wf
            if technical_retryable:
                if contract_migration_retryable:
                    migration_versions[step_key] = normalizer_version
                    state["contract_migration_recovery"] = {
                        "step": int(step_key),
                        "prompt_id": current_prompt_id,
                        "output_normalizer_version": normalizer_version,
                        "reason": "revalidate persisted provider output under the upgraded contract layer",
                    }
                else:
                    retries[step_key] = int(retries.get(step_key, 0)) + 1
                if deterministic_recheck:
                    previous = state.get("step_results", {}).pop(step_key)
                    state.setdefault("superseded_step_results", {}).setdefault(
                        step_key,
                        [],
                    ).append(previous)
            state["recovered_from"] = state.get("last_error") or "TECHNICAL_STEP_FAILURE"
            state.pop("last_error", None)
            self._update(wf, status="RUNNING", state=state)
            wf = self.get(workflow_id)
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
            except WorkflowInputRequired as exc:
                state["last_error"] = str(exc)
                state["workflow_input_required"] = {
                    "prompt_id": exc.prompt_id,
                    "gate_type": exc.gate_type,
                    "missing_paths": exc.missing_paths,
                }
                state.setdefault("technical_retry_attempts", {}).pop(str(wf["current_step"]), None)
                self._update(wf, state=state)
                refreshed = self.get(workflow_id)
                gate_id = self._create_gate(
                    refreshed,
                    exc.gate_type,
                    target_id=f"input:{prompt_id}:{workflow_id}",
                    questions=exc.questions,
                )
                self.db.audit(
                    "WORKFLOW_INPUT_REQUIRED",
                    project_id=wf["project_id"],
                    object_id=gate_id,
                    metadata={
                        "workflow_id": workflow_id,
                        "prompt_id": prompt_id,
                        "gate_type": exc.gate_type,
                        "missing_paths": exc.missing_paths,
                    },
                )
                self._update(refreshed, status="WAITING_GATE", state=state)
                return self.get(workflow_id)
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

        quality_scope = None if wf["workflow_type"] == "WF-5_SECURITY_REVIEW_AND_EXPORT" else workflow_id
        blockers = (
            self.quality_manager.open_delivery_blockers(wf["project_id"])
            if wf["workflow_type"] == "WF-5_SECURITY_REVIEW_AND_EXPORT"
            else self.quality_manager.open_blockers(
                wf["project_id"],
                workflow_id=quality_scope,
            )
        )
        accepted_blockers: list[dict[str, Any]] = []
        if wf["workflow_type"] != "WF-5_SECURITY_REVIEW_AND_EXPORT":
            blockers, accepted_blockers = self._unaccepted_completion_blockers(blockers, state)
        try:
            if blockers:
                raise QualityGateBlocked(blockers)
        except QualityGateBlocked as exc:
            state["last_error"] = str(exc) + "。必须记录修复运行并由独立Critic复审，人工确认或直接改库均不能放行。"
            state["quality_blocker_ids"] = [item.get("finding_id") for item in exc.findings]
            self._update(wf, status="BLOCKED", state=state)
            return self.get(workflow_id)
        if accepted_blockers:
            state["accepted_open_quality_finding_ids"] = [
                item.get("finding_id") for item in accepted_blockers
            ]
            if wf["workflow_type"] == "WF-1_PROJECT_INTAKE":
                state["completion_scope"] = "CONTENT_VALIDATION_ONLY"
        state.pop("last_error", None)
        state.pop("quality_blocker_ids", None)
        self._update(wf, status="COMPLETED", state=state)
        self.db.audit("WORKFLOW_COMPLETED", project_id=wf["project_id"], object_id=workflow_id, metadata={"workflow_type": wf["workflow_type"]})
        return self.get(workflow_id)
