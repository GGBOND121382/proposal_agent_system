from __future__ import annotations

import asyncio
import copy
import json
from typing import Any

from .dependency_preflight import DependencyIssue, DependencyReport
from .full_proposal_contract import FULL_PROPOSAL_GROUP_ORDER
from .util import new_id, sha256_json, utc_now
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
        envelope = self.context_builder.build(
            prompt_id,
            wf["project_id"],
            workflow_id=wf["id"],
            workflow_state=state,
        )
        candidate_round_key = (
            f"section:{state.get('active_section_id') or ''}:{prompt_id}"
        )
        candidate_round = int(
            (state.get("acceptance_candidate_rounds") or {}).get(
                candidate_round_key,
                0,
            )
        )
        requested_call_key = None
        if candidate_round:
            requested_call_key = "call-acceptance-" + sha256_json(
                {
                    "workflow_id": wf["id"],
                    "section_id": state.get("active_section_id"),
                    "prompt_id": prompt_id,
                    "candidate_round": candidate_round,
                }
            )[:24]
        result = await self.executor.execute(
            prompt_id,
            envelope,
            project_id=wf["project_id"],
            workflow_id=wf["id"],
            original_environment=state.get("original_environment"),
            call_key=requested_call_key,
        )
        if prompt_id == "P-WRITE-CONTENT" and self.diagram_enrichment is not None and result["status"] == "PASS":
            result["output"] = await self.diagram_enrichment.enrich(
                project_id=wf["project_id"],
                workflow_id=wf["id"],
                run_id=result["run_id"],
                section=section,
                output=result["output"],
                security_level=(
                    result["output"].get("source_refs", [{}])[0].get("security_level", "INTERNAL")
                    if result["output"].get("source_refs") else "INTERNAL"
                ),
            )
        self._append_section_run(progress, result, prompt_id=prompt_id, role=role)
        state["original_environment"] = result["route"]["environment"]
        if result["status"] == "PASS":
            critic_prompt = next(
                (
                    critic
                    for critic, (producer, _phase) in self.SECTION_CRITIC_PRODUCERS.items()
                    if producer == prompt_id
                ),
                None,
            )
            if critic_prompt:
                self._supersede_repair_subject(
                    state,
                    critic_prompt=critic_prompt,
                    producer_prompt=prompt_id,
                    reason="FRESH_PRODUCER_PASS",
                )
        self._observe_quality_result(wf, state, prompt_id, result)
        self._update(wf, state=state)
        return envelope, result

    def _create_full_proposal_child(
        self,
        wf: dict[str, Any],
        state: dict[str, Any],
        group: dict[str, Any],
    ) -> dict[str, Any]:
        children = state.setdefault("full_proposal_children", {})
        group_id = str(group["group_id"])
        existing = children.get(group_id)
        if existing:
            return existing

        child_id = new_id("wf-group")
        now = utc_now()
        parent_options = copy.deepcopy(state.get("options") or {})
        parent_options.update({
            "full_proposal_concurrent": False,
            "integration_scope": "FULL_PROPOSAL_GROUP_CHILD",
            "concurrent_group_child": True,
            "suppress_candidate_gate": True,
            "target_section_ids": list(group.get("section_ids") or []),
            "target_section_titles": [],
        })
        child_state = {
            "workflow_type": "WF-4_PROPOSAL_AUTHORING",
            "options": parent_options,
            "step_results": {},
            "repair_attempts": {},
            "section_results": [],
            "section_progress": {},
            "public_search_results": state.get("public_search_results"),
            "original_environment": state.get("original_environment"),
            "parent_workflow_id": wf["id"],
            "quality_parent_workflow_id": wf["id"],
            "full_proposal_group_id": group_id,
            "full_proposal_group_title": group.get("title"),
            "full_proposal_contract_hash": (state.get("full_proposal_contract") or {}).get("contract_hash"),
        }
        self.db.execute(
            "INSERT INTO workflows(id,project_id,workflow_type,status,current_step,state_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                child_id,
                wf["project_id"],
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
            "section_ids": list(group.get("section_ids") or []),
            "status": WorkflowStatus.RUNNING.value,
            "created_at": now,
        }
        children[group_id] = record
        self.db.audit(
            "FULL_PROPOSAL_GROUP_STARTED",
            project_id=wf["project_id"],
            object_id=child_id,
            metadata={"parent_workflow_id": wf["id"], "group_id": group_id, "section_ids": record["section_ids"]},
        )
        self._update(wf, state=state)
        return record

    def _spawn_full_proposal_repair_child(
        self,
        child: dict[str, Any],
        child_state: dict[str, Any],
        parent_workflow_id: str,
        record: dict[str, Any],
    ) -> dict[str, Any]:
        """Create a replacement execution record instead of reopening a terminal child."""

        replacement_id = new_id("wf")
        now = utc_now()
        child_state["supersedes_workflow_id"] = child["id"]
        child_state["replacement_reason"] = "FULL_PROPOSAL_INTEGRATION_REPAIR"
        self.db.execute(
            "INSERT INTO workflows(id,project_id,workflow_type,status,current_step,state_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                replacement_id,
                child["project_id"],
                child["workflow_type"],
                WorkflowStatus.RUNNING.value,
                5,
                json.dumps(child_state, ensure_ascii=False),
                now,
                now,
            ),
        )
        record.setdefault("superseded_workflow_ids", []).append(child["id"])
        record["workflow_id"] = replacement_id
        record["status"] = WorkflowStatus.RUNNING.value
        self.db.audit(
            "FULL_PROPOSAL_GROUP_REPLACEMENT_STARTED",
            project_id=child["project_id"],
            object_id=replacement_id,
            metadata={
                "parent_workflow_id": parent_workflow_id,
                "supersedes_workflow_id": child["id"],
                "group_id": record.get("group_id"),
            },
        )
        return {
            **child,
            "id": replacement_id,
            "status": WorkflowStatus.RUNNING.value,
            "current_step": 5,
            "state": child_state,
            "created_at": now,
            "updated_at": now,
        }

    def _reset_full_proposal_child_for_repair(
        self,
        child: dict[str, Any],
        parent_state: dict[str, Any],
        parent_workflow_id: str,
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
        child_state["quality_parent_workflow_id"] = parent_workflow_id
        if child["status"] == WorkflowStatus.COMPLETED.value:
            if record is None:
                raise ValueError("completed child rewrite requires its parent group record")
            return self._spawn_full_proposal_repair_child(
                child,
                child_state,
                parent_workflow_id,
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
        child = self.get(str(record["workflow_id"]))
        if repair_ids:
            child = self._reset_full_proposal_child_for_repair(
                child,
                parent_state,
                parent_wf["id"],
                repair_ids,
                record=record,
            )
        expected = {str(x) for x in record.get("section_ids") or []}
        completed = {str(item.get("section_id")) for item in child["state"].get("section_results", [])}
        if child["status"] == WorkflowStatus.COMPLETED.value and expected <= completed:
            return child
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
        elif is_waiting(child["status"]) or is_recoverable_block(child["status"]):
            return child
        result = await self._write_sections_serial(child, child["state"])
        child = self.get(child["id"])
        if result is not None or status_class(child["status"]) is not WorkflowStatusClass.ACTIVE:
            return child
        completed = {str(item.get("section_id")) for item in child["state"].get("section_results", [])}
        if not expected <= completed:
            child["state"]["last_error"] = (
                f"并发组 {record['group_id']} 未完成全部章节：expected={sorted(expected)}, completed={sorted(completed)}"
            )
            self._update(child, status=WorkflowStatus.BLOCKED_TECHNICAL.value, state=child["state"])
            return self.get(child["id"])
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
        records: list[dict[str, Any]] = []
        for group_id in FULL_PROPOSAL_GROUP_ORDER:
            group = by_group[group_id]
            if group.get("section_ids"):
                records.append(self._create_full_proposal_child(wf, state, group))
            else:
                state.setdefault("full_proposal_virtual_lanes", {})[group_id] = {
                    "group_id": group_id,
                    "title": group.get("title"),
                    "status": "COMPLETED",
                    "cross_cutting_roles": list(group.get("cross_cutting_roles") or []),
                    "reason": "本轮无独立参考文献或附录章节；图表、公式与交叉引用由各章节内容链和导出链生成并在全文阶段统一验证。",
                }
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
        parent_status = aggregate_workflow_statuses(
            result["status"] for result in results
        )
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
        for child in children:
            child_ids.append(child["id"])
            for item in child["state"].get("section_results", []):
                section_records[str(item.get("section_id"))] = copy.deepcopy(item)
            for section_id, progress in child["state"].get("section_progress", {}).items():
                merged_progress[str(section_id)] = copy.deepcopy(progress)
        ordered_ids = [str(item["section_id"]) for item in contract.get("sections") or []]
        missing = [section_id for section_id in ordered_ids if section_id not in section_records]
        if missing:
            state["last_error"] = "并发组完成后缺少章节结果：" + "、".join(missing)
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
        wf["current_step"] += 1
        skip_gate = bool(state.pop("skip_candidate_gate_once", False))
        self._update(wf, current_step=wf["current_step"], state=state)
        if skip_gate:
            return None
        refreshed = self.get(wf["id"])
        self._create_gate(refreshed, "CANDIDATE_REVIEW", target_id=wf["id"], questions=[])
        self._update(refreshed, status=WorkflowStatus.WAITING_GATE.value, state=state)
        return self.get(wf["id"])
