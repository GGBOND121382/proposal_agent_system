from __future__ import annotations

import copy
import json
from typing import Any

from .util import new_id, sha256_json, utc_now
from .workflow_status import (
    WorkflowStatus,
    clear_terminal_runtime_transients,
    ensure_transition,
    is_terminal,
)
from .workflow_defs import GATE_ACTIONS, GATE_ROLE
from .wf3_input import WF3_INPUT_GATE_TYPE, options_from_gate_answers
from .workflow_input import (
    MATERIAL_INPUT_GATE_TYPES,
    build_human_resolutions,
    human_resolution_scope_key,
    section_id_for_run,
    validate_gate_questions,
)


class WorkflowGateMixin:
    @staticmethod
    def _gate_context_hash_v1(wf: dict[str, Any]) -> str:
        """Historical context identity retained for existing open gates."""

        return sha256_json(
            {
                "workflow": wf["id"],
                "step": wf["current_step"],
                "state": wf["state"],
            }
        )

    @staticmethod
    def _gate_context_hash_v2(
        wf: dict[str, Any],
        *,
        gate_type: str,
        target_id: str,
        questions: list[Any],
    ) -> str:
        """Bind an approval gate to one exact persisted checkpoint and target."""

        return sha256_json(
            {
                "protocol": "GATE_CONTEXT_V2",
                "workflow_id": wf["id"],
                "current_step": wf["current_step"],
                "state": wf["state"],
                "gate_type": gate_type,
                "target_id": target_id,
                "questions": questions,
            }
        )

    def _gate_context_is_current(
        self,
        gate: dict[str, Any],
        wf: dict[str, Any],
    ) -> bool:
        try:
            questions = json.loads(gate.get("questions_json") or "[]")
        except (TypeError, json.JSONDecodeError):
            return False
        context_hash = str(gate.get("context_hash") or "")
        v2_hash = self._gate_context_hash_v2(
            wf,
            gate_type=str(gate.get("gate_type") or ""),
            target_id=str(gate.get("target_id") or ""),
            questions=questions if isinstance(questions, list) else [],
        )
        try:
            question_version = int(gate.get("question_version") or 1)
        except (TypeError, ValueError):
            return False
        if question_version >= 2:
            # New Gate rows must use the target- and question-bound V2 identity.
            # Accepting a V1 hash here would silently downgrade the server-side
            # stale-check to the historical workflow-only contract.
            return context_hash == v2_hash
        return context_hash in {self._gate_context_hash_v1(wf), v2_hash}

    @staticmethod
    def _cancel_gate_in_transaction(
        tx: Any,
        *,
        gate: dict[str, Any],
        reason: str,
        now: str,
    ) -> None:
        decision = {
            "action": "SYSTEM_CANCEL_STALE",
            "reason": reason,
            "decided_by": "SYSTEM",
            "decided_role": "SYSTEM_ADMIN",
            "decided_at": now,
            "context_hash": gate.get("context_hash"),
        }
        cursor = tx.execute(
            """UPDATE gates
               SET status='CANCELLED',decision_json=?,updated_at=?
               WHERE id=? AND status='OPEN'""",
            (json.dumps(decision, ensure_ascii=False), now, gate["id"]),
        )
        if cursor.rowcount == 1:
            tx.audit(
                "GATE_CANCELLED_STALE",
                project_id=gate.get("project_id"),
                object_id=gate["id"],
                metadata={
                    "workflow_id": gate.get("workflow_id"),
                    "gate_type": gate.get("gate_type"),
                    "target_id": gate.get("target_id"),
                    "reason": reason,
                },
            )

    @staticmethod
    def _prepare_human_resolution_artifacts(
        *,
        gate: dict[str, Any],
        prompt_id: str,
        scope_key: str,
        section_id: str | None,
        workflow_step: int,
        resolutions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        prepared: list[dict[str, Any]] = []
        for resolution in resolutions:
            payload = {
                "schema_version": "1.0.0",
                "gate_id": gate["id"],
                "workflow_id": gate["workflow_id"],
                "prompt_id": prompt_id,
                "scope_key": scope_key,
                "section_id": section_id,
                "workflow_step": workflow_step,
                "target_run_id": gate.get("target_id"),
                "resolution": resolution,
                "authority": "HUMAN_GATE_DECISION",
                "supersedes_state_override": True,
            }
            prepared.append(
                {
                    "artifact_id": new_id("artifact"),
                    "payload": payload,
                    "created_at": utc_now(),
                }
            )
        return prepared

    def _gate_target_context(
        self,
        *,
        gate: dict[str, Any],
        wf: dict[str, Any],
        state: dict[str, Any],
    ) -> tuple[str, str | None, str]:
        """Resolve prompt and section from the Gate's exact persisted target.

        The current workflow step may still contain an older result when a
        section Gate is decided.  Target identity therefore wins over all
        compatibility fallbacks.
        """

        target_id = str(gate.get("target_id") or "").strip()
        prompt_id = ""
        section_id = section_id_for_run(state, target_id)
        run = None
        if target_id:
            run = self.db.fetchone(
                """SELECT prompt_id,input_json FROM prompt_runs
                   WHERE id=? AND workflow_id=?""",
                (target_id, wf["id"]),
            )
        if run:
            prompt_id = str(run.get("prompt_id") or "").strip()
            if not section_id:
                try:
                    input_data = json.loads(run.get("input_json") or "{}")
                except (TypeError, json.JSONDecodeError):
                    input_data = {}
                if not isinstance(input_data, dict):
                    input_data = {}
                payload = input_data.get("payload")
                payload = payload if isinstance(payload, dict) else {}
                task = input_data.get("task")
                task = task if isinstance(task, dict) else {}
                scope = input_data.get("scope")
                scope = scope if isinstance(scope, dict) else {}

                def nested_section(container: Any) -> Any:
                    return (
                        container.get("section_id")
                        if isinstance(container, dict)
                        else None
                    )

                candidates = [
                    nested_section(payload.get("source_section")),
                    nested_section(payload.get("current_section")),
                    nested_section(payload.get("section")),
                    task.get("section_id"),
                    scope.get("section_id"),
                ]
                section_id = next(
                    (str(item).strip() for item in candidates if str(item or "").strip()),
                    None,
                )

        pending_input = (
            state.get("workflow_input_required")
            if isinstance(state.get("workflow_input_required"), dict)
            else {}
        )
        if not prompt_id and target_id == f"input:{pending_input.get('prompt_id')}:{wf['id']}":
            prompt_id = str(pending_input.get("prompt_id") or "").strip()

        section_gate = (
            state.get("section_input_gate")
            if isinstance(state.get("section_input_gate"), dict)
            else {}
        )
        if not prompt_id and target_id == str(section_gate.get("run_id") or ""):
            prompt_id = str(section_gate.get("prompt_id") or "").strip()
            section_id = str(section_gate.get("section_id") or "").strip() or section_id

        current_result = (state.get("step_results") or {}).get(str(wf["current_step"])) or {}
        if not prompt_id and target_id == str(current_result.get("run_id") or ""):
            prompt_id = str(current_result.get("prompt_id") or "").strip()

        # Compatibility for workflow-level gates that target the workflow
        # rather than a model run.  Exact target-derived sources above always
        # take precedence.
        if not prompt_id and target_id == str(wf["id"]):
            prompt_id = str(pending_input.get("prompt_id") or "").strip()
        if not prompt_id:
            raise ValueError("Cannot resolve the prompt owned by this Gate target")

        scope_key = human_resolution_scope_key(
            prompt_id,
            section_id=section_id,
            workflow_step=wf["current_step"],
        )
        return prompt_id, section_id, scope_key

    @staticmethod
    def _insert_human_resolution_artifacts(
        tx: Any,
        *,
        gate: dict[str, Any],
        prompt_id: str,
        prepared: list[dict[str, Any]],
    ) -> None:
        for item in prepared:
            version = tx.next_artifact_version(
                project_id=gate["project_id"],
                workflow_id=gate["workflow_id"],
                artifact_type="HUMAN_RESOLUTION",
                prompt_id=prompt_id,
            )
            payload = item["payload"]
            tx.execute(
                """INSERT INTO artifacts(id,project_id,workflow_id,artifact_type,prompt_id,version,status,security_level,context_hash,content_json,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    item["artifact_id"],
                    gate["project_id"],
                    gate["workflow_id"],
                    "HUMAN_RESOLUTION",
                    prompt_id,
                    version,
                    "PASS",
                    gate["security_level"],
                    sha256_json(payload),
                    json.dumps(payload, ensure_ascii=False),
                    item["created_at"],
                ),
            )

    def _create_gate(
        self,
        wf: dict[str, Any],
        gate_type: str,
        *,
        target_id: str,
        questions: list[Any],
        checkpoint_status: WorkflowStatus | str | None = None,
        checkpoint_step: int | None = None,
        checkpoint_state: dict[str, Any] | None = None,
    ) -> str:
        """Create a Gate, optionally committing its workflow checkpoint atomically.

        Most legacy callers persist their workflow checkpoint before creating a
        Gate.  Crash-sensitive coordinators can instead supply the exact status,
        step and state that the Gate protects; the workflow update, Gate row and
        audit event then share one transaction.
        """

        expected_updated_at = wf.get("updated_at")
        checkpoint_requested = any(
            item is not None
            for item in (checkpoint_status, checkpoint_step, checkpoint_state)
        )
        gate_id = new_id("gate")
        questions = copy.deepcopy(list(questions))
        validate_gate_questions(questions)
        allowed = list(
            GATE_ACTIONS.get(
                gate_type,
                ["CONFIRM", "RETURN", "REJECT", "CANCEL"],
            )
        )
        security_decision_gates = {
            "OUTBOUND_SECURITY_APPROVAL",
            "ONLINE_RESULT_IMPORT_APPROVAL",
            "FINAL_CONTENT_SECURITY_APPROVAL",
            "FINAL_EXPORT_APPROVAL",
        }
        if (
            questions
            and gate_type not in security_decision_gates
            and "PROVIDE_INFORMATION" not in allowed
        ):
            allowed.insert(0, "PROVIDE_INFORMATION")
        now = utc_now()
        with self.db.transaction() as tx:
            workflow_row = tx.fetchone(
                "SELECT * FROM workflows WHERE id=?",
                (wf["id"],),
            )
            if workflow_row is None:
                raise KeyError(f"Workflow not found: {wf['id']}")
            if (
                expected_updated_at is not None
                and workflow_row.get("updated_at") != expected_updated_at
            ):
                raise RuntimeError(
                    f"workflow changed before gate creation: {wf['id']}"
                )
            current = dict(workflow_row)
            try:
                current["state"] = json.loads(current.pop("state_json"))
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError("workflow state is not valid JSON") from exc
            if is_terminal(current["status"]):
                raise ValueError("cannot create a gate for a terminal workflow")
            if checkpoint_requested:
                next_status = ensure_transition(
                    current["status"],
                    checkpoint_status or current["status"],
                ).value
                next_step = (
                    int(current["current_step"])
                    if checkpoint_step is None
                    else int(checkpoint_step)
                )
                next_state = copy.deepcopy(
                    current["state"]
                    if checkpoint_state is None
                    else checkpoint_state
                )
                updated_at = tx.update_workflow(
                    workflow_id=current["id"],
                    status=next_status,
                    current_step=next_step,
                    state=next_state,
                    expected_updated_at=current.get("updated_at"),
                )
                current.update({
                    "status": next_status,
                    "current_step": next_step,
                    "state": next_state,
                    "updated_at": updated_at,
                })
            context_hash = self._gate_context_hash_v2(
                current,
                gate_type=gate_type,
                target_id=target_id,
                questions=questions,
            )
            security_level_row = tx.fetchone(
                "SELECT security_level FROM projects WHERE id=?",
                (current["project_id"],),
            )
            security_level = (
                str((security_level_row or {}).get("security_level") or "INTERNAL")
            )
            open_gates = tx.fetchall(
                """SELECT * FROM gates
                   WHERE workflow_id=? AND status='OPEN'
                   ORDER BY created_at DESC,id DESC""",
                (current["id"],),
            )
            exact_gate: dict[str, Any] | None = None
            for existing in open_gates:
                try:
                    existing_questions = json.loads(
                        existing.get("questions_json") or "[]"
                    )
                except (TypeError, json.JSONDecodeError):
                    existing_questions = None
                exact_spec = (
                    str(existing.get("gate_type") or "") == gate_type
                    and str(existing.get("target_id") or "") == target_id
                    and existing_questions == questions
                    and self._gate_context_is_current(existing, current)
                )
                if exact_gate is None and exact_spec:
                    exact_gate = existing
                    continue
                self._cancel_gate_in_transaction(
                    tx,
                    gate=existing,
                    reason=(
                        "DUPLICATE_OPEN_GATE"
                        if exact_spec
                        else "SUPERSEDED_CHECKPOINT"
                    ),
                    now=now,
                )
            if exact_gate is not None:
                if checkpoint_requested:
                    wf.update({
                        "status": current["status"],
                        "current_step": int(current["current_step"]),
                        "state": current["state"],
                        "updated_at": current["updated_at"],
                    })
                return str(exact_gate["id"])
            required_role = GATE_ROLE.get(gate_type)
            if not required_role:
                raise KeyError(f"Unknown Gate type: {gate_type}")
            tx.execute(
                """INSERT INTO gates(id,project_id,workflow_id,gate_type,target_id,target_version,context_hash,question_version,required_role,allowed_actions_json,questions_json,security_level,status,decision_json,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    gate_id,
                    current["project_id"],
                    current["id"],
                    gate_type,
                    target_id,
                    1,
                    context_hash,
                    2,
                    required_role,
                    json.dumps(allowed),
                    json.dumps(questions, ensure_ascii=False),
                    security_level,
                    "OPEN",
                    None,
                    now,
                    now,
                ),
            )
            tx.audit(
                "GATE_CREATED",
                project_id=current["project_id"],
                object_id=gate_id,
                metadata={
                    "gate_type": gate_type,
                    "workflow_id": current["id"],
                    "target_id": target_id,
                    "context_hash": context_hash,
                    "context_protocol": "GATE_CONTEXT_V2",
                },
            )
        if checkpoint_requested:
            wf.update({
                "status": current["status"],
                "current_step": int(current["current_step"]),
                "state": current["state"],
                "updated_at": current["updated_at"],
            })
        return gate_id

    def _reconcile_open_gates(
        self,
        wf: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Atomically reconcile one workflow with its authoritative Open Gate.

        All Gate cancellation and the corresponding workflow status repair share
        one ``BEGIN IMMEDIATE`` transaction.  A process failure therefore cannot
        commit ``CANCELLED`` Gates while leaving a stale ``WAITING_GATE`` state,
        and a stale caller snapshot cannot decide which Gate remains current.
        """

        with self.db.transaction() as tx:
            workflow_row = tx.fetchone(
                "SELECT * FROM workflows WHERE id=?",
                (wf["id"],),
            )
            if workflow_row is None:
                raise KeyError(f"Workflow not found: {wf['id']}")
            current = dict(workflow_row)
            try:
                state = json.loads(current.pop("state_json"))
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError("workflow state is not valid JSON") from exc
            current["state"] = state
            open_gates = tx.fetchall(
                """SELECT * FROM gates
                   WHERE workflow_id=? AND status='OPEN'
                   ORDER BY created_at DESC,id DESC""",
                (current["id"],),
            )
            if not open_gates:
                if (
                    current["status"] == WorkflowStatus.WAITING_GATE.value
                    and not state.get("waiting_on_child_workflow_ids")
                ):
                    state["last_error"] = (
                        "Workflow is WAITING_GATE but no current OPEN Gate exists; "
                        "automatic advancement is refused to avoid crossing an "
                        "unproven human checkpoint."
                    )
                    failure = {
                        "code": "WAITING_GATE_WITHOUT_OPEN_GATE",
                        "detected_at": utc_now(),
                        "workflow_step": int(current["current_step"]),
                    }
                    state["gate_reconciliation_failure"] = failure
                    status = ensure_transition(
                        current["status"],
                        WorkflowStatus.BLOCKED_TECHNICAL.value,
                    ).value
                    tx.update_workflow(
                        workflow_id=current["id"],
                        status=status,
                        current_step=int(current["current_step"]),
                        state=state,
                        expected_updated_at=current.get("updated_at"),
                    )
                    tx.audit(
                        "WORKFLOW_GATE_RECONCILIATION_FAILED",
                        project_id=current["project_id"],
                        object_id=current["id"],
                        metadata=failure,
                    )
                return None

            terminal = is_terminal(current["status"])
            valid = [] if terminal else [
                gate
                for gate in open_gates
                if self._gate_context_is_current(gate, current)
            ]
            keep = valid[0] if valid else None
            now = utc_now()
            for gate in open_gates:
                if keep is not None and gate["id"] == keep["id"]:
                    continue
                self._cancel_gate_in_transaction(
                    tx,
                    gate=gate,
                    reason=(
                        "TERMINAL_WORKFLOW"
                        if terminal
                        else (
                            "SUPERSEDED_OPEN_GATE"
                            if gate in valid
                            else "STALE_CONTEXT"
                        )
                    ),
                    now=now,
                )

            if keep is not None:
                if current["status"] != WorkflowStatus.WAITING_GATE.value:
                    status = ensure_transition(
                        current["status"],
                        WorkflowStatus.WAITING_GATE.value,
                    ).value
                    tx.update_workflow(
                        workflow_id=current["id"],
                        status=status,
                        current_step=int(current["current_step"]),
                        state=state,
                        expected_updated_at=current.get("updated_at"),
                    )
                return keep

            if (
                current["status"] == WorkflowStatus.WAITING_GATE.value
                and not state.get("waiting_on_child_workflow_ids")
                and not terminal
            ):
                state["recovered_from"] = "STALE_OPEN_GATE_CANCELLED"
                state.pop("gate_reconciliation_failure", None)
                status = ensure_transition(
                    current["status"],
                    WorkflowStatus.RUNNING.value,
                ).value
                tx.update_workflow(
                    workflow_id=current["id"],
                    status=status,
                    current_step=int(current["current_step"]),
                    state=state,
                    expected_updated_at=current.get("updated_at"),
                )
                tx.audit(
                    "WORKFLOW_STALE_GATE_RECONCILED",
                    project_id=current["project_id"],
                    object_id=current["id"],
                    metadata={
                        "cancelled_gate_ids": [gate["id"] for gate in open_gates],
                        "workflow_step": int(current["current_step"]),
                    },
                )
            return None

    def decide_gate(self, gate_id: str, *, action: str, decided_by: str, decided_role: str, comment: str | None = None, answers: list[dict[str, Any]] | None = None, context_hash: str | None = None) -> dict[str, Any]:
        gate = self.db.fetchone("SELECT * FROM gates WHERE id=?", (gate_id,))
        if not gate:
            raise KeyError(f"Gate not found: {gate_id}")
        if gate["status"] != "OPEN":
            raise ValueError("Gate is not open")
        wf = self.get(gate["workflow_id"])
        current_gate = self._reconcile_open_gates(wf)
        gate = self.db.fetchone("SELECT * FROM gates WHERE id=?", (gate_id,))
        if (
            gate is None
            or gate["status"] != "OPEN"
            or current_gate is None
            or current_gate["id"] != gate_id
        ):
            raise ValueError("Gate is stale or has been superseded")
        wf = self.get(gate["workflow_id"])
        if not self._gate_context_is_current(gate, wf):
            raise ValueError("Gate context hash is stale")
        allowed = json.loads(gate["allowed_actions_json"])
        if action not in allowed:
            raise ValueError(f"Action {action} is not allowed")
        if decided_role != gate["required_role"] and decided_role != "SYSTEM_ADMIN":
            raise PermissionError(f"Gate requires role {gate['required_role']}")
        if context_hash is not None and context_hash != gate["context_hash"]:
            raise ValueError("Gate context hash is stale")
        approved = action in {"APPROVE", "CONFIRM", "RESOLVE", "PROVIDE_INFORMATION"}
        next_step = wf["current_step"]
        state = wf["state"]
        questions = json.loads(gate["questions_json"])
        pending_input = state.get("workflow_input_required") if isinstance(state.get("workflow_input_required"), dict) else {}
        section_input_gate = (
            state.get("section_input_gate")
            if isinstance(state.get("section_input_gate"), dict)
            else {}
        )
        current_result = state.get("step_results", {}).get(str(wf["current_step"])) or {}
        target_prompt_id = ""
        target_section_id: str | None = None
        resolution_scope_key = ""

        resolutions: list[dict[str, Any]] = []
        prepared_resolution_artifacts: list[dict[str, Any]] = []
        gate_target_id = str(gate.get("target_id") or "")
        section_gate_matches = (
            bool(section_input_gate)
            and gate_target_id == str(section_input_gate.get("run_id") or "")
        )
        current_result_matches = (
            gate_target_id == str(current_result.get("run_id") or "")
        )
        requires_human_answer = section_gate_matches or (
            current_result_matches
            and str(current_result.get("status") or "") == "NEED_USER_INPUT"
        )
        target_is_model_checkpoint = gate_target_id in {
            str(current_result.get("run_id") or ""),
            str(section_input_gate.get("run_id") or ""),
            f"input:{pending_input.get('prompt_id')}:{wf['id']}",
        }
        if approved and (questions or target_is_model_checkpoint):
            (
                target_prompt_id,
                target_section_id,
                resolution_scope_key,
            ) = self._gate_target_context(gate=gate, wf=wf, state=state)
            # Required questions are validated even when the caller sends no
            # answers.  This prevents CONFIRM with an empty answer list from
            # accepting a NEED_USER_INPUT checkpoint.
            if questions:
                resolutions = build_human_resolutions(
                    gate_id=gate_id,
                    prompt_id=target_prompt_id,
                    questions=questions,
                    answers=answers,
                    decided_by=decided_by,
                    decided_role=decided_role,
                    require_any_answer=requires_human_answer,
                )
            elif requires_human_answer:
                raise ValueError(
                    "NEED_USER_INPUT 检查点没有可回答的问题，不能通过空确认继续。"
                )
            if resolutions and gate["gate_type"] not in MATERIAL_INPUT_GATE_TYPES:
                prepared_resolution_artifacts = self._prepare_human_resolution_artifacts(
                    gate=gate,
                    prompt_id=target_prompt_id,
                    scope_key=resolution_scope_key,
                    section_id=target_section_id,
                    workflow_step=int(wf["current_step"]),
                    resolutions=resolutions,
                )
                artifact_ids = [item["artifact_id"] for item in prepared_resolution_artifacts]
                scoped_index = state.setdefault("human_resolution_artifact_ids", {}).setdefault(
                    resolution_scope_key, []
                )
                scoped_index.extend(artifact_ids)
                del scoped_index[:-50]
                # Deliberately do not create hidden business overrides in workflow state.
                state.pop("human_input_overrides", None)
                state.pop("human_resolutions", None)
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
        status = "APPROVED" if approved else ("CANCELLED" if action == "CANCEL" else "REJECTED")
        decided_at = utc_now()
        decision = {"action": action, "comment": comment, "answers": answers or [], "decided_by": decided_by, "decided_role": decided_role, "decided_at": decided_at, "context_hash": gate["context_hash"]}
        if approved:
            if section_gate_matches:
                section_id = str(section_input_gate.get("section_id") or "")
                progress = (
                    (state.get("section_progress") or {}).get(section_id)
                    if section_id
                    else None
                )
                if isinstance(progress, dict):
                    # section_input_gate is created only for NEED_USER_INPUT.
                    # It must always rerun the same prompt after validation;
                    # CONFIRM is not an override of the model decision.
                    progress["phase"] = section_input_gate.get("phase")
                    rerun_key = resolution_scope_key or human_resolution_scope_key(
                        str(section_input_gate.get("prompt_id") or target_prompt_id),
                        section_id=section_id,
                        workflow_step=wf["current_step"],
                    )
                    state.setdefault("human_input_reruns", {})[rerun_key] = int(
                        state.setdefault("human_input_reruns", {}).get(rerun_key, 0)
                    ) + 1
                    state["rerun_from_human_input"] = {
                        "gate_id": gate_id,
                        "prompt_id": target_prompt_id,
                        "scope_key": rerun_key,
                        "resolution_ids": [
                            item["resolution_id"] for item in resolutions
                        ],
                        "section_id": section_id,
                    }
                    progress["status"] = "RUNNING"
                    progress.pop("last_error", None)
                state.pop("section_input_gate", None)
                state.pop("last_error", None)
            if (
                not section_gate_matches
                and gate.get("target_id") == current_result.get("run_id")
                and current_result.get("status") in {"REVISE", "NEED_USER_INPUT"}
            ):
                rerun_required = current_result.get("status") == "NEED_USER_INPUT"
                if rerun_required or resolutions:
                    step_key = str(wf["current_step"])
                    state.setdefault("superseded_step_results", {}).setdefault(step_key, []).append(current_result)
                    state.get("step_results", {}).pop(step_key, None)
                    rerun_key = resolution_scope_key or human_resolution_scope_key(
                        target_prompt_id,
                        section_id=target_section_id,
                        workflow_step=wf["current_step"],
                    )
                    state.setdefault("human_input_reruns", {})[rerun_key] = int(
                        state.setdefault("human_input_reruns", {}).get(rerun_key, 0)
                    ) + 1
                    state["rerun_from_human_input"] = {
                        "gate_id": gate_id,
                        "prompt_id": target_prompt_id,
                        "scope_key": rerun_key,
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
        workflow_status = ensure_transition(
            wf["status"],
            "RUNNING" if approved else "BLOCKED_CONTENT",
        ).value
        with self.db.transaction() as tx:
            gate_cursor = tx.execute(
                """UPDATE gates
                   SET status=?,decision_json=?,updated_at=?
                   WHERE id=? AND status='OPEN' AND context_hash=?""",
                (
                    status,
                    json.dumps(decision, ensure_ascii=False),
                    decided_at,
                    gate_id,
                    gate["context_hash"],
                ),
            )
            if gate_cursor.rowcount != 1:
                raise ValueError("Gate was already decided or its context changed")
            if prepared_resolution_artifacts:
                self._insert_human_resolution_artifacts(
                    tx,
                    gate=gate,
                    prompt_id=target_prompt_id,
                    prepared=prepared_resolution_artifacts,
                )
            tx.update_workflow(
                workflow_id=wf["id"],
                status=workflow_status,
                current_step=next_step,
                state=state,
                expected_updated_at=wf.get("updated_at"),
            )
            tx.audit(
                "GATE_DECIDED",
                project_id=gate["project_id"],
                object_id=gate_id,
                metadata={
                    "gate_type": gate["gate_type"],
                    "status": status,
                    "decided_role": decided_role,
                },
            )
            if approved and gate["gate_type"] == WF3_INPUT_GATE_TYPE:
                tx.audit(
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
        return self.db.fetchone(
            """SELECT * FROM gates
               WHERE workflow_id=? AND status='OPEN'
               ORDER BY created_at DESC,id DESC LIMIT 1""",
            (workflow_id,),
        )

    def _project_level(self, project_id: str) -> str:
        row = self.db.fetchone("SELECT security_level FROM projects WHERE id=?", (project_id,))
        return row["security_level"] if row else "INTERNAL"

    def _update(
        self,
        wf: dict[str, Any],
        *,
        status: str | None = None,
        current_step: int | None = None,
        state: dict[str, Any] | None = None,
    ) -> None:
        """Update a workflow against its authoritative persisted version.

        The supplied workflow object is an optimistic concurrency token, not the
        source of truth for the current status.  This prevents a stale caller
        from overwriting a Gate decision or reopening a terminal workflow.
        """

        with self.db.transaction() as tx:
            current = tx.fetchone(
                "SELECT * FROM workflows WHERE id=?",
                (wf["id"],),
            )
            if current is None:
                raise KeyError(f"Workflow not found: {wf['id']}")
            expected_updated_at = wf.get("updated_at")
            if (
                expected_updated_at is not None
                and current.get("updated_at") != expected_updated_at
            ):
                raise RuntimeError(
                    f"workflow changed during atomic update: {wf['id']}"
                )
            next_status = ensure_transition(
                current["status"],
                status or current["status"],
            ).value
            next_step = (
                int(current["current_step"])
                if current_step is None
                else int(current_step)
            )
            if state is None:
                try:
                    next_state = json.loads(current.get("state_json") or "{}")
                except (TypeError, json.JSONDecodeError) as exc:
                    raise ValueError("workflow state is not valid JSON") from exc
            else:
                next_state = state
            clear_terminal_runtime_transients(next_state, next_status)
            updated_at = tx.update_workflow(
                workflow_id=wf["id"],
                status=next_status,
                current_step=next_step,
                state=next_state,
                expected_updated_at=current.get("updated_at"),
            )
        wf["status"] = next_status
        wf["current_step"] = next_step
        wf["state"] = next_state
        wf["updated_at"] = updated_at
