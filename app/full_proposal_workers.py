from __future__ import annotations

import asyncio
import copy
import json
from typing import Any

from .dependency_preflight import DependencyIssue, DependencyReport
from .full_proposal_contract import FULL_PROPOSAL_GROUP_ORDER
from .util import new_id, utc_now
from .workflow_status import (
    WorkflowStatus,
    WorkflowStatusClass,
    aggregate_workflow_statuses,
    is_recoverable_block,
    is_terminal,
    is_waiting,
    status_class,
)


class FullProposalWorkersMixin:
    async def _execute_section_prompt(
        self,
        wf: dict[str, Any],
        state: dict[str, Any],
        section: dict[str, Any],
        progress: dict[str, Any],
        prompt_id: str,
        *,
        role: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        # LIVE model calls naturally yield while waiting on the endpoint.  The
        # deterministic simulator returns synchronously, so explicitly yield at
        # each stage to exercise the same group-level concurrency in acceptance.
        if (state.get("options") or {}).get("concurrent_group_child"):
            await asyncio.sleep(0)
        # All call identity, provider retry, arbitration, feedback lifecycle,
        # repair-subject and persistence semantics belong to the shared section
        # executor.  Keeping a second copy here allowed serial/full-proposal
        # execution to drift whenever the base contract changed.
        return await super()._execute_section_prompt(
            wf,
            state,
            section,
            progress,
            prompt_id,
            role=role,
        )

    @staticmethod
    def _decode_workflow_row(row: dict[str, Any]) -> dict[str, Any]:
        decoded = dict(row)
        decoded["state"] = json.loads(decoded.pop("state_json") or "{}")
        return decoded

    @staticmethod
    def _sync_workflow_snapshot(target: dict[str, Any], source: dict[str, Any]) -> None:
        target.update({
            "status": source["status"],
            "current_step": int(source["current_step"]),
            "state": source["state"],
            "updated_at": source["updated_at"],
        })

    @staticmethod
    def _full_proposal_parent_status(
        values: list[WorkflowStatus | str],
    ) -> WorkflowStatus:
        status = aggregate_workflow_statuses(values)
        if status is WorkflowStatus.COMPLETED:
            return status
        if status is WorkflowStatus.WAITING_CONFIGURATION:
            # The parent owns a consolidated DependencyReport and can therefore
            # legitimately persist WAITING_CONFIGURATION itself.
            return status
        if status is WorkflowStatus.CANCELLED:
            # A cancelled child cannot satisfy the frozen group contract, but a
            # child cancellation must not silently cancel the parent workflow.
            return WorkflowStatus.BLOCKED_TECHNICAL
        # Gate, provider, active, prerequisite and classified block states all
        # belong to the child checkpoint.  Persist their exact values in
        # full_proposal_child_statuses, while the coordinator itself waits on the
        # child as a prerequisite.  Copying a child-owned WAITING_GATE or
        # WAITING_PROVIDER onto the parent creates a checkpoint that the parent
        # cannot reconcile; copying a child block prevents the parent from ever
        # re-entering to observe that the child was repaired.
        return WorkflowStatus.WAITING_PREREQUISITE

    @staticmethod
    def _blocked_child_snapshot(child: dict[str, Any], message: str) -> dict[str, Any]:
        blocked = copy.deepcopy(child)
        blocked["status"] = WorkflowStatus.BLOCKED_TECHNICAL.value
        blocked.setdefault("state", {})["last_error"] = message
        blocked["state"]["checkpoint_integrity_error"] = True
        return blocked

    def _validate_full_proposal_child_checkpoint(
        self,
        parent_wf: dict[str, Any],
        parent_state: dict[str, Any],
        record: dict[str, Any],
        child: dict[str, Any],
    ) -> str | None:
        group_id = str(record.get("group_id") or "")
        contract = parent_state.get("full_proposal_contract") or {}
        contract_groups = {
            str(item.get("group_id") or ""): [str(section) for section in item.get("section_ids") or []]
            for item in contract.get("groups") or []
            if isinstance(item, dict)
        }
        expected_list = [str(item) for item in record.get("section_ids") or []]
        contract_expected = contract_groups.get(group_id)
        errors: list[str] = []
        if contract_expected is None or expected_list != contract_expected:
            errors.append("group sections differ from the frozen contract")
        child_state = child.get("state") or {}
        if str(child.get("project_id") or "") != str(parent_wf.get("project_id") or ""):
            errors.append("child project differs from parent project")
        if str(child_state.get("parent_workflow_id") or "") != str(parent_wf.get("id") or ""):
            errors.append("child parent_workflow_id differs from the current parent")
        if str(child_state.get("quality_parent_workflow_id") or "") != str(parent_wf.get("id") or ""):
            errors.append("child quality_parent_workflow_id differs from the current parent")
        if str(child_state.get("full_proposal_group_id") or "") != group_id:
            errors.append("child group id differs from the parent group record")
        if str(child_state.get("full_proposal_contract_hash") or "") != str(contract.get("contract_hash") or ""):
            errors.append("child contract hash differs from the frozen parent contract")
        target_ids = [str(item) for item in (child_state.get("options") or {}).get("target_section_ids") or []]
        if target_ids != expected_list:
            errors.append("child target sections differ from the parent group record")
        result_ids = [
            str(item.get("section_id") or "")
            for item in child_state.get("section_results") or []
            if isinstance(item, dict)
        ]
        if any(not item for item in result_ids):
            errors.append("child contains a section result without section_id")
        if len(result_ids) != len(set(result_ids)):
            errors.append("child contains duplicate section results")
        if not set(result_ids).issubset(set(expected_list)):
            errors.append("child contains sections outside its frozen group")
        if (
            child.get("status") == WorkflowStatus.COMPLETED.value
            and (
                len(result_ids) != len(expected_list)
                or set(result_ids) != set(expected_list)
            )
        ):
            errors.append("completed child does not contain the exact frozen section set")
        if errors:
            return f"并发组 {group_id} 子工作流检查点无效：" + "；".join(errors)
        return None

    def _create_full_proposal_child(
        self,
        wf: dict[str, Any],
        state: dict[str, Any],
        group: dict[str, Any],
    ) -> dict[str, Any]:
        """Create one group child and bind it to the parent atomically.

        Multiple coordinators may resume the same parent after a process restart.
        Child insertion and the parent group record therefore share one
        ``BEGIN IMMEDIATE`` transaction.  A second creator adopts the exact
        persisted record instead of inserting an orphan duplicate.
        """

        group_id = str(group["group_id"])
        section_ids = [str(item) for item in group.get("section_ids") or []]
        caller_contract = copy.deepcopy(state.get("full_proposal_contract") or {})
        contract_hash = str(caller_contract.get("contract_hash") or "")
        with self.db.transaction() as tx:
            parent_row = tx.fetchone("SELECT * FROM workflows WHERE id=?", (wf["id"],))
            if parent_row is None:
                raise KeyError(f"Workflow not found: {wf['id']}")
            if (
                parent_row.get("workflow_type") != "WF-4_PROPOSAL_AUTHORING"
                or parent_row.get("status") != WorkflowStatus.RUNNING.value
                or int(parent_row.get("current_step") or -1) != 5
            ):
                raise RuntimeError(
                    "只有处于 WRITE_SECTIONS 的 RUNNING 父工作流可以创建并发章节组。"
                )
            authoritative_state = json.loads(parent_row.get("state_json") or "{}")
            if (authoritative_state.get("options") or {}).get("concurrent_group_child"):
                raise RuntimeError("并发章节子工作流不能继续创建孙级工作流。")
            authoritative_contract = authoritative_state.get("full_proposal_contract") or {}
            authoritative_contract_hash = str(authoritative_contract.get("contract_hash") or "")
            if not contract_hash:
                raise RuntimeError("完整申请书并发组创建缺少冻结合同。")
            if authoritative_contract_hash:
                if authoritative_contract_hash != contract_hash:
                    raise RuntimeError(
                        "完整申请书并发组创建时父工作流合同已变化；必须重新读取父检查点。"
                    )
            else:
                # The contract is resolved deterministically at the WRITE_SECTIONS
                # boundary before the first child exists.  Persist that contract in
                # the same transaction as the first child so a crash cannot leave an
                # orphan child or a parent that cannot recognise it.  Only the exact
                # caller snapshot that still owns the authoritative updated_at token
                # may introduce the contract.
                if str(wf.get("updated_at") or "") != str(parent_row.get("updated_at") or ""):
                    raise RuntimeError(
                        "完整申请书并发组创建时父工作流检查点已变化；必须重新读取父检查点。"
                    )
                authoritative_contract = caller_contract
                authoritative_state["full_proposal_contract"] = copy.deepcopy(caller_contract)
                authoritative_state["current_workflow_id"] = wf["id"]

            contract_groups = {
                str(item.get("group_id") or ""): [str(section) for section in item.get("section_ids") or []]
                for item in authoritative_contract.get("groups") or []
                if isinstance(item, dict)
            }
            if contract_groups.get(group_id) != section_ids:
                raise ValueError(
                    f"并发组 {group_id} 的章节集合与冻结合同不一致。"
                )
            children = authoritative_state.setdefault("full_proposal_children", {})
            existing = children.get(group_id)
            if existing:
                if [str(item) for item in existing.get("section_ids") or []] != section_ids:
                    raise ValueError(
                        f"并发组 {group_id} 的持久化章节集合与冻结合同不一致。"
                    )
                authoritative = self._decode_workflow_row(parent_row)
                authoritative["state"] = authoritative_state
                record = existing
            else:
                child_id = new_id("wf-group")
                now = utc_now()
                parent_options = copy.deepcopy(authoritative_state.get("options") or {})
                parent_options.update({
                    "full_proposal_concurrent": False,
                    "integration_scope": "FULL_PROPOSAL_GROUP_CHILD",
                    "concurrent_group_child": True,
                    "suppress_candidate_gate": True,
                    "target_section_ids": section_ids,
                    "target_section_titles": [],
                })
                child_state = {
                    "workflow_type": "WF-4_PROPOSAL_AUTHORING",
                    "options": parent_options,
                    "step_results": {},
                    "repair_attempts": {},
                    "section_results": [],
                    "section_progress": {},
                    "public_search_results": authoritative_state.get("public_search_results"),
                    "original_environment": authoritative_state.get("original_environment"),
                    "parent_workflow_id": wf["id"],
                    "quality_parent_workflow_id": wf["id"],
                    "full_proposal_group_id": group_id,
                    "full_proposal_group_title": group.get("title"),
                    "full_proposal_contract_hash": contract_hash,
                }
                tx.execute(
                    "INSERT INTO workflows(id,project_id,workflow_type,status,current_step,state_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        child_id,
                        parent_row["project_id"],
                        "WF-4_PROPOSAL_AUTHORING",
                        WorkflowStatus.RUNNING.value,
                        5,
                        json.dumps(child_state, ensure_ascii=False),
                        now,
                        now,
                    ),
                )
                record = {
                    "group_id": group_id,
                    "title": group.get("title"),
                    "workflow_id": child_id,
                    "section_ids": section_ids,
                    "status": WorkflowStatus.RUNNING.value,
                    "created_at": now,
                }
                children[group_id] = record
                updated_at = tx.update_workflow(
                    workflow_id=wf["id"],
                    status=parent_row["status"],
                    current_step=int(parent_row["current_step"]),
                    state=authoritative_state,
                    expected_updated_at=parent_row.get("updated_at"),
                )
                tx.audit(
                    "FULL_PROPOSAL_GROUP_STARTED",
                    project_id=parent_row["project_id"],
                    object_id=child_id,
                    metadata={
                        "parent_workflow_id": wf["id"],
                        "group_id": group_id,
                        "section_ids": section_ids,
                    },
                )
                authoritative = {
                    **parent_row,
                    "updated_at": updated_at,
                    "state": authoritative_state,
                }

        state.clear()
        state.update(copy.deepcopy(authoritative_state))
        authoritative["state"] = state
        self._sync_workflow_snapshot(wf, authoritative)
        return state["full_proposal_children"][group_id]

    def _spawn_full_proposal_repair_child(
        self,
        child: dict[str, Any],
        child_state: dict[str, Any],
        parent_wf: dict[str, Any],
        parent_state: dict[str, Any],
        record: dict[str, Any],
    ) -> dict[str, Any]:
        """Create or adopt a replacement child in the parent transaction."""

        group_id = str(record.get("group_id") or "")
        with self.db.transaction() as tx:
            parent_row = tx.fetchone("SELECT * FROM workflows WHERE id=?", (parent_wf["id"],))
            if parent_row is None:
                raise KeyError(f"Workflow not found: {parent_wf['id']}")
            if (
                parent_row.get("workflow_type") != "WF-4_PROPOSAL_AUTHORING"
                or parent_row.get("status") != WorkflowStatus.RUNNING.value
                or int(parent_row.get("current_step") or -1) != 5
            ):
                raise RuntimeError(
                    "只有处于 WRITE_SECTIONS 的 RUNNING 父工作流可以创建替换子工作流。"
                )
            authoritative_state = json.loads(parent_row.get("state_json") or "{}")
            authoritative_contract = authoritative_state.get("full_proposal_contract") or {}
            contract_hash = str(authoritative_contract.get("contract_hash") or "")
            child_contract_hash = str(child_state.get("full_proposal_contract_hash") or "")
            if not contract_hash or child_contract_hash != contract_hash:
                raise RuntimeError(
                    "替换子工作流与父工作流冻结合同不一致。"
                )
            authoritative_record = (
                authoritative_state.get("full_proposal_children") or {}
            ).get(group_id)
            if not isinstance(authoritative_record, dict):
                raise RuntimeError(f"并发组 {group_id} 未绑定到父工作流。")
            expected_sections = [
                str(item) for item in authoritative_record.get("section_ids") or []
            ]
            target_sections = [
                str(item)
                for item in (child_state.get("options") or {}).get("target_section_ids") or []
            ]
            if expected_sections != target_sections:
                raise RuntimeError(
                    f"并发组 {group_id} 的替换子工作流章节集合与父记录不一致。"
                )
            current_id = str(authoritative_record.get("workflow_id") or "")
            if current_id != child["id"]:
                current_row = tx.fetchone("SELECT * FROM workflows WHERE id=?", (current_id,))
                if current_row is None:
                    raise RuntimeError(f"并发组 {group_id} 指向不存在的替换子工作流。")
                current = self._decode_workflow_row(current_row)
                if str(current["state"].get("supersedes_workflow_id") or "") != child["id"]:
                    raise RuntimeError(
                        f"并发组 {group_id} 已由不相关的子工作流占用：{current_id}"
                    )
                authoritative = self._decode_workflow_row(parent_row)
                authoritative["state"] = authoritative_state
                replacement = current
            else:
                old_child_row = tx.fetchone("SELECT * FROM workflows WHERE id=?", (current_id,))
                if old_child_row is None:
                    raise RuntimeError(f"并发组 {group_id} 指向不存在的原子工作流。")
                old_child = self._decode_workflow_row(old_child_row)
                old_state = old_child["state"]
                parent_contract = authoritative_state.get("full_proposal_contract") or {}
                expected_sections = [
                    str(item) for item in authoritative_record.get("section_ids") or []
                ]
                immutable_errors: list[str] = []
                if old_child.get("status") != WorkflowStatus.COMPLETED.value:
                    immutable_errors.append("原子工作流不是 COMPLETED")
                if str(old_child.get("project_id") or "") != str(parent_row.get("project_id") or ""):
                    immutable_errors.append("原子工作流项目与父工作流不一致")
                if old_child.get("workflow_type") != "WF-4_PROPOSAL_AUTHORING":
                    immutable_errors.append("原子工作流类型无效")
                if str(old_state.get("parent_workflow_id") or "") != parent_wf["id"]:
                    immutable_errors.append("原子工作流父身份无效")
                if str(old_state.get("quality_parent_workflow_id") or "") != parent_wf["id"]:
                    immutable_errors.append("原子工作流质量父身份无效")
                if str(old_state.get("full_proposal_group_id") or "") != group_id:
                    immutable_errors.append("原子工作流并发组身份无效")
                if str(old_state.get("full_proposal_contract_hash") or "") != str(
                    parent_contract.get("contract_hash") or ""
                ):
                    immutable_errors.append("原子工作流合同身份无效")
                old_targets = [
                    str(item)
                    for item in (old_state.get("options") or {}).get("target_section_ids") or []
                ]
                if old_targets != expected_sections:
                    immutable_errors.append("原子工作流目标章节无效")
                if str(child.get("updated_at") or "") != str(old_child.get("updated_at") or ""):
                    immutable_errors.append("调用方原子工作流快照已陈旧")
                if immutable_errors:
                    raise RuntimeError(
                        f"并发组 {group_id} 不能从无效检查点创建替换子工作流："
                        + "；".join(immutable_errors)
                    )
                replacement_id = new_id("wf")
                now = utc_now()
                child_state["supersedes_workflow_id"] = old_child["id"]
                child_state["replacement_reason"] = "FULL_PROPOSAL_INTEGRATION_REPAIR"
                tx.execute(
                    "INSERT INTO workflows(id,project_id,workflow_type,status,current_step,state_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        replacement_id,
                        parent_row["project_id"],
                        "WF-4_PROPOSAL_AUTHORING",
                        WorkflowStatus.RUNNING.value,
                        5,
                        json.dumps(child_state, ensure_ascii=False),
                        now,
                        now,
                    ),
                )
                authoritative_record.setdefault("superseded_workflow_ids", []).append(old_child["id"])
                authoritative_record["workflow_id"] = replacement_id
                authoritative_record["status"] = WorkflowStatus.RUNNING.value
                updated_at = tx.update_workflow(
                    workflow_id=parent_wf["id"],
                    status=parent_row["status"],
                    current_step=int(parent_row["current_step"]),
                    state=authoritative_state,
                    expected_updated_at=parent_row.get("updated_at"),
                )
                tx.audit(
                    "FULL_PROPOSAL_GROUP_REPLACEMENT_STARTED",
                    project_id=parent_row["project_id"],
                    object_id=replacement_id,
                    metadata={
                        "parent_workflow_id": parent_wf["id"],
                        "supersedes_workflow_id": old_child["id"],
                        "group_id": group_id,
                    },
                )
                authoritative = {
                    **parent_row,
                    "updated_at": updated_at,
                    "state": authoritative_state,
                }
                replacement = {
                    **old_child,
                    "id": replacement_id,
                    "status": WorkflowStatus.RUNNING.value,
                    "current_step": 5,
                    "state": child_state,
                    "created_at": now,
                    "updated_at": now,
                }

        parent_state.clear()
        parent_state.update(copy.deepcopy(authoritative_state))
        authoritative["state"] = parent_state
        self._sync_workflow_snapshot(parent_wf, authoritative)
        record.clear()
        record.update(parent_state["full_proposal_children"][group_id])
        return replacement

    def _reset_full_proposal_child_for_repair(
        self,
        child: dict[str, Any],
        parent_state: dict[str, Any],
        parent_wf: dict[str, Any] | str,
        affected: set[str],
        *,
        record: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        child_state = copy.deepcopy(child["state"]) if is_terminal(child["status"]) else child["state"]
        group_ids = {str(x) for x in (child_state.get("options") or {}).get("target_section_ids", [])}
        responsible = sorted(group_ids & affected)
        if not responsible:
            return child
        child_state["section_results"] = [
            item for item in child_state.get("section_results", [])
            if str(item.get("section_id")) not in set(responsible)
        ]
        progress = child_state.setdefault("section_progress", {})
        for section_id in responsible:
            progress.pop(section_id, None)
            self._reset_section_repair_state(
                child_state,
                section_id,
                reason="FULL_PROPOSAL_SECTION_REWRITE",
            )
        child_state["integration_repair_section_ids"] = responsible
        child_state["integration_repair_findings"] = copy.deepcopy(parent_state.get("integration_repair_findings") or [])
        parent_workflow_id = (
            str(parent_wf.get("id") or "")
            if isinstance(parent_wf, dict)
            else str(parent_wf or "")
        )
        if not parent_workflow_id:
            raise ValueError("full-proposal child repair requires a parent workflow id")
        child_state["quality_parent_workflow_id"] = parent_workflow_id
        if child["status"] == WorkflowStatus.COMPLETED.value:
            if record is None:
                raise ValueError("completed child rewrite requires its parent group record")
            if not isinstance(parent_wf, dict):
                raise ValueError(
                    "completed child rewrite requires the authoritative parent workflow snapshot"
                )
            return self._spawn_full_proposal_repair_child(
                child,
                child_state,
                parent_wf,
                parent_state,
                record,
            )
        if child["status"] == WorkflowStatus.CANCELLED.value:
            raise ValueError("cancelled full-proposal child cannot be rewritten")
        child["current_step"] = 5
        child["status"] = WorkflowStatus.RUNNING.value
        self._update(
            child,
            status=WorkflowStatus.RUNNING.value,
            current_step=5,
            state=child_state,
        )
        child["state"] = child_state
        return child

    async def _run_full_proposal_group(
        self,
        parent_wf: dict[str, Any],
        parent_state: dict[str, Any],
        record: dict[str, Any],
        repair_ids: set[str],
    ) -> dict[str, Any]:
        child_id = str(record.get("workflow_id") or "")
        try:
            child = self.get(child_id)
        except KeyError:
            message = (
                f"并发组 {record.get('group_id')} 指向不存在的子工作流：{child_id or 'EMPTY'}"
            )
            self.db.audit(
                "FULL_PROPOSAL_CHILD_CHECKPOINT_INVALID",
                project_id=parent_wf["project_id"],
                object_id=child_id or parent_wf["id"],
                metadata={
                    "parent_workflow_id": parent_wf["id"],
                    "group_id": record.get("group_id"),
                    "error": message,
                },
            )
            return {
                "id": child_id,
                "project_id": parent_wf["project_id"],
                "workflow_type": "WF-4_PROPOSAL_AUTHORING",
                "status": WorkflowStatus.BLOCKED_TECHNICAL.value,
                "current_step": 5,
                "state": {
                    "last_error": message,
                    "checkpoint_integrity_error": True,
                },
            }
        checkpoint_error = self._validate_full_proposal_child_checkpoint(
            parent_wf, parent_state, record, child
        )
        if checkpoint_error:
            self.db.audit(
                "FULL_PROPOSAL_CHILD_CHECKPOINT_INVALID",
                project_id=parent_wf["project_id"],
                object_id=child["id"],
                metadata={
                    "parent_workflow_id": parent_wf["id"],
                    "group_id": record.get("group_id"),
                    "error": checkpoint_error,
                },
            )
            return self._blocked_child_snapshot(child, checkpoint_error)
        if repair_ids:
            child = self._reset_full_proposal_child_for_repair(
                child,
                parent_state,
                parent_wf,
                repair_ids,
                record=record,
            )
            checkpoint_error = self._validate_full_proposal_child_checkpoint(
                parent_wf, parent_state, record, child
            )
            if checkpoint_error:
                return self._blocked_child_snapshot(child, checkpoint_error)
        expected = [str(x) for x in record.get("section_ids") or []]
        completed = [
            str(item.get("section_id") or "")
            for item in child["state"].get("section_results", [])
            if isinstance(item, dict)
        ]
        if (
            child["status"] == WorkflowStatus.COMPLETED.value
            and len(completed) == len(expected)
            and set(completed) == set(expected)
        ):
            return child
        if is_terminal(child["status"]):
            return self._blocked_child_snapshot(
                child,
                f"并发组 {record['group_id']} 的终态子工作流没有有效的完整章节快照。",
            )
        record["started_at"] = utc_now()
        record["status"] = child["status"]
        self._update(parent_wf, state=parent_state)
        if child["status"] == WorkflowStatus.WAITING_CONFIGURATION.value:
            report = self._configuration_recheck_report(child, child["state"])
            if report is not None and report.blocking_issues:
                self._pause_for_configuration(
                    child,
                    child["state"],
                    report,
                    source="FULL_PROPOSAL_CHILD_RECHECK",
                )
                return self.get(child["id"])
            self._clear_configuration_wait(child["state"])
            self._update(
                child,
                status=WorkflowStatus.RUNNING.value,
                state=child["state"],
            )
            child = self.get(child["id"])
        elif child["status"] == WorkflowStatus.WAITING_GATE.value:
            # Human approval belongs to the child checkpoint.  The parent may
            # propagate the wait but must never approve or step across it.
            return child
        elif is_waiting(child["status"]) or is_recoverable_block(child["status"]):
            # Let the canonical workflow recovery boundary decide whether a
            # provider wait, prerequisite wait, protocol upgrade, normalizer
            # migration, or explicitly recoverable runtime interruption may
            # resume.  Calling ``advance`` here does not force a blocked child
            # open: non-recoverable classified blocks remain unchanged.  It
            # merely prevents the parent coordinator from deadlocking forever
            # on a child checkpoint that the child engine itself can recover.
            child = await self.advance(child["id"])
            child = self.get(child["id"])
            if status_class(child["status"]) is not WorkflowStatusClass.ACTIVE:
                return child
        result = await self._write_sections_serial(child, child["state"])
        child = self.get(child["id"])
        if result is not None or status_class(child["status"]) is not WorkflowStatusClass.ACTIVE:
            return child
        completed = [
            str(item.get("section_id") or "")
            for item in child["state"].get("section_results", [])
            if isinstance(item, dict)
        ]
        if (
            len(completed) != len(expected)
            or len(completed) != len(set(completed))
            or set(completed) != set(expected)
        ):
            child["state"]["last_error"] = (
                f"并发组 {record['group_id']} 未形成精确章节快照：expected={expected}, completed={completed}"
            )
            self._update(child, status=WorkflowStatus.BLOCKED_TECHNICAL.value, state=child["state"])
            return self.get(child["id"])
        by_section = {
            str(item.get("section_id")): item
            for item in child["state"].get("section_results", [])
            if isinstance(item, dict) and item.get("section_id")
        }
        child["state"]["section_results"] = [by_section[section_id] for section_id in expected]
        child["state"]["group_status"] = "COMPLETED"
        child["state"]["completed_at"] = utc_now()
        self._update(child, status=WorkflowStatus.COMPLETED.value, state=child["state"])
        record["status"] = WorkflowStatus.COMPLETED.value
        record["finished_at"] = child["state"]["completed_at"]
        self._update(parent_wf, state=parent_state)
        self.db.audit(
            "FULL_PROPOSAL_GROUP_COMPLETED",
            project_id=parent_wf["project_id"],
            object_id=child["id"],
            metadata={"parent_workflow_id": parent_wf["id"], "group_id": record["group_id"]},
        )
        return self.get(child["id"])

    async def _write_full_proposal_concurrently(
        self,
        wf: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any] | None:
        state["current_workflow_id"] = wf["id"]
        sections = self._target_sections(wf["project_id"], state.get("options") or {}, state)
        contract = state.get("full_proposal_contract") or {}
        by_group = {item["group_id"]: item for item in contract.get("groups") or []}
        active_group_ids: list[str] = []
        for group_id in FULL_PROPOSAL_GROUP_ORDER:
            group = by_group[group_id]
            if group.get("section_ids"):
                self._create_full_proposal_child(wf, state, group)
                active_group_ids.append(group_id)
            else:
                state.setdefault("full_proposal_virtual_lanes", {})[group_id] = {
                    "group_id": group_id,
                    "title": group.get("title"),
                    "status": "COMPLETED",
                    "cross_cutting_roles": list(group.get("cross_cutting_roles") or []),
                    "reason": "本轮无独立参考文献或附录章节；图表、公式与交叉引用由各章节内容链和导出链生成并在全文阶段统一验证。",
                }
        records = [
            state["full_proposal_children"][group_id]
            for group_id in active_group_ids
        ]
        if not records:
            return self._block_section_chain(wf, state, {"section_id": "FULL_PROPOSAL", "title": "完整申请书"}, "没有可执行的并发章节组。")

        repair_ids = {str(x) for x in state.get("integration_repair_section_ids", []) if x}
        state["full_proposal_parallel_started_at"] = utc_now()
        # Operational interruptions must propagate so the parent remains RUNNING
        # and can resume from persisted child checkpoints.  Expected semantic
        # failures are represented by BLOCKED child workflows and handled below.
        results = await asyncio.gather(
            *(self._run_full_proposal_group(wf, state, record, repair_ids) for record in records),
        )
        interruptions: list[str] = []
        configuration_issues: list[DependencyIssue] = []
        children: list[dict[str, Any]] = []
        for record, result in zip(records, results):
            children.append(result)
            record["status"] = result["status"]
            if result["status"] == WorkflowStatus.WAITING_CONFIGURATION.value:
                before_count = len(configuration_issues)
                for raw in (result["state"].get("configuration_wait") or {}).get("issues") or []:
                    if not isinstance(raw, dict):
                        continue
                    configuration_issues.append(
                        DependencyIssue(
                            code=str(raw.get("code") or "CHILD_RUNTIME_DEPENDENCY_UNAVAILABLE"),
                            dependency=str(raw.get("dependency") or "FULL_PROPOSAL_CHILD"),
                            message=str(raw.get("message") or result["state"].get("last_error") or "并发章节组等待运行配置"),
                            required_settings=tuple(str(item) for item in raw.get("required_settings") or []),
                            severity=str(raw.get("severity") or "ERROR"),
                            retryable=bool(raw.get("retryable", True)),
                            details={**(raw.get("details") or {}), "child_workflow_id": result["id"], "group_id": record["group_id"]},
                        )
                    )
                if len(configuration_issues) == before_count:
                    configuration_issues.append(
                        DependencyIssue(
                            code="FULL_PROPOSAL_CHILD_WAITING_CONFIGURATION",
                            dependency="FULL_PROPOSAL_CHILD",
                            message=str(result["state"].get("last_error") or f"并发组 {record['group_id']} 等待运行配置"),
                            details={"child_workflow_id": result["id"], "group_id": record["group_id"]},
                        )
                    )
            if result["status"] != WorkflowStatus.COMPLETED.value:
                interruptions.append(
                    f"{record['group_id']}: {result['state'].get('last_error') or result['status']}"
                )
        state["full_proposal_parallel_finished_at"] = utc_now()
        state["full_proposal_child_statuses"] = {
            str(record["group_id"]): str(result["status"])
            for record, result in zip(records, results)
        }
        child_status_values = [result["status"] for result in results]
        if any(
            bool((result.get("state") or {}).get("checkpoint_integrity_error"))
            for result in results
        ):
            parent_status = WorkflowStatus.BLOCKED_TECHNICAL
        else:
            parent_status = self._full_proposal_parent_status(child_status_values)
        waiting_gate_ids = [
            result["id"]
            for result in results
            if result["status"] == WorkflowStatus.WAITING_GATE.value
        ]
        if waiting_gate_ids:
            state["waiting_on_child_gate_workflow_ids"] = waiting_gate_ids
        else:
            state.pop("waiting_on_child_gate_workflow_ids", None)
        if (
            parent_status is WorkflowStatus.WAITING_CONFIGURATION
            and configuration_issues
        ):
            return self._pause_for_configuration(
                wf,
                state,
                DependencyReport(
                    scope="FULL_PROPOSAL_CHILDREN",
                    issues=configuration_issues,
                ),
                source="FULL_PROPOSAL_CHILDREN",
            )
        if parent_status is not WorkflowStatus.COMPLETED:
            state["waiting_on_child_workflow_ids"] = [
                result["id"]
                for result in results
                if result["status"] != WorkflowStatus.COMPLETED.value
            ]
            state["last_error"] = "完整申请书并发组未完成：" + "；".join(interruptions)
            self._update(wf, status=parent_status.value, state=state)
            return self.get(wf["id"])

        state.pop("waiting_on_child_workflow_ids", None)
        section_records: dict[str, dict[str, Any]] = {}
        merged_progress: dict[str, Any] = {}
        child_ids: list[str] = []
        duplicate_sections: list[str] = []
        for child in children:
            child_ids.append(child["id"])
            for item in child["state"].get("section_results", []):
                section_id = str(item.get("section_id") or "")
                if not section_id or section_id in section_records:
                    duplicate_sections.append(section_id or "<EMPTY>")
                    continue
                section_records[section_id] = copy.deepcopy(item)
            for section_id, progress in child["state"].get("section_progress", {}).items():
                merged_progress[str(section_id)] = copy.deepcopy(progress)
        ordered_ids = [str(item["section_id"]) for item in contract.get("sections") or []]
        missing = [section_id for section_id in ordered_ids if section_id not in section_records]
        unexpected = sorted(set(section_records) - set(ordered_ids))
        if missing or unexpected or duplicate_sections or len(child_ids) != len(set(child_ids)):
            state["last_error"] = (
                "并发组完成后的章节所有权快照无效："
                f"missing={missing}, unexpected={unexpected}, duplicates={sorted(duplicate_sections)}, "
                f"child_ids_unique={len(child_ids) == len(set(child_ids))}"
            )
            self._update(wf, status=WorkflowStatus.BLOCKED_TECHNICAL.value, state=state)
            return self.get(wf["id"])
        state["section_results"] = [section_records[section_id] for section_id in ordered_ids]
        state["section_progress"] = merged_progress
        state["authoring_child_workflow_ids"] = child_ids
        state["full_proposal_concurrency"] = {
            "mode": "FIVE_GROUP_PARALLEL_SECTION_SERIAL",
            "contract_hash": contract.get("contract_hash"),
            "group_count": 5,
            "active_child_count": len(child_ids),
            "section_count": len(ordered_ids),
            "child_workflow_ids": child_ids,
            "started_at": state.get("full_proposal_parallel_started_at"),
            "finished_at": state.get("full_proposal_parallel_finished_at"),
            "no_shared_mutable_draft": True,
        }
        state.pop("integration_repair_section_ids", None)
        state.pop("integration_repair_findings", None)
        state.pop("last_error", None)
        next_step = int(wf["current_step"]) + 1
        skip_gate = bool(state.pop("skip_candidate_gate_once", False))
        if skip_gate:
            self._update(wf, current_step=next_step, state=state)
            return None
        self._create_gate(
            wf,
            "CANDIDATE_REVIEW",
            target_id=wf["id"],
            questions=[],
            checkpoint_status=WorkflowStatus.WAITING_GATE,
            checkpoint_step=next_step,
            checkpoint_state=state,
        )
        return self.get(wf["id"])
