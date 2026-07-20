from __future__ import annotations

import asyncio
import copy
import json
from typing import Any

from .full_proposal_contract import FULL_PROPOSAL_GROUP_ORDER
from .util import new_id, utc_now


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
        result = await self.executor.execute(
            prompt_id,
            envelope,
            project_id=wf["project_id"],
            workflow_id=wf["id"],
            original_environment=state.get("original_environment"),
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
        generation_attempt_id = str(
            state.get("full_proposal_generation_attempt_id") or ""
        ).strip()
        if not generation_attempt_id:
            generation_attempt_id = new_id("generation-attempt")
            state["full_proposal_generation_attempt_id"] = generation_attempt_id
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
            "repair_overrides": {},
            "section_results": [],
            "section_progress": {},
            "public_search_results": state.get("public_search_results"),
            "original_environment": state.get("original_environment"),
            "parent_workflow_id": wf["id"],
            "quality_parent_workflow_id": wf["id"],
            "full_proposal_group_id": group_id,
            "full_proposal_group_title": group.get("title"),
            "full_proposal_contract_hash": (state.get("full_proposal_contract") or {}).get("contract_hash"),
            "generation_attempt_id": generation_attempt_id,
        }
        self.db.execute(
            "INSERT INTO workflows(id,project_id,workflow_type,status,current_step,state_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                child_id,
                wf["project_id"],
                "WF-4_PROPOSAL_AUTHORING",
                "RUNNING",
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
            "status": "RUNNING",
            "created_at": now,
            "generation_attempt_id": generation_attempt_id,
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

    def _reset_full_proposal_child_for_repair(
        self,
        child: dict[str, Any],
        parent_state: dict[str, Any],
        parent_workflow_id: str,
        affected: set[str],
    ) -> None:
        child_state = child["state"]
        group_ids = {str(x) for x in (child_state.get("options") or {}).get("target_section_ids", [])}
        responsible = sorted(group_ids & affected)
        if not responsible:
            return
        repair_attempt_id = str(
            parent_state.get("full_proposal_repair_attempt_id") or ""
        ).strip()
        if not repair_attempt_id:
            # Backward-compatible recovery for a checkpoint created before repair
            # attempts were explicitly scoped.  The ID is persisted on the parent
            # immediately by the caller's normal state update.
            repair_attempt_id = new_id("generation-repair")
            parent_state["full_proposal_repair_attempt_id"] = repair_attempt_id
        if (
            child_state.get("generation_attempt_id") == repair_attempt_id
            and sorted(child_state.get("integration_repair_section_ids") or []) == responsible
        ):
            # The same repair round may be resumed many times while waiting for a
            # model or human bridge.  Do not rotate identity or erase progress.
            return
        child_state["generation_attempt_id"] = repair_attempt_id
        child_state["section_results"] = [
            item for item in child_state.get("section_results", [])
            if str(item.get("section_id")) not in set(responsible)
        ]
        progress = child_state.setdefault("section_progress", {})
        overrides = child_state.setdefault("repair_overrides", {})
        for section_id in responsible:
            progress.pop(section_id, None)
            for key in list(overrides):
                if key.startswith(f"section:{section_id}:"):
                    overrides.pop(key, None)
        child_state["integration_repair_section_ids"] = responsible
        child_state["integration_repair_findings"] = copy.deepcopy(parent_state.get("integration_repair_findings") or [])
        child_state["quality_parent_workflow_id"] = parent_workflow_id
        child["current_step"] = 5
        child["status"] = "RUNNING"
        self._update(child, status="RUNNING", current_step=5, state=child_state)

    async def _run_full_proposal_group(
        self,
        parent_wf: dict[str, Any],
        parent_state: dict[str, Any],
        record: dict[str, Any],
        repair_ids: set[str],
    ) -> dict[str, Any]:
        child = self.get(str(record["workflow_id"]))
        if repair_ids:
            self._reset_full_proposal_child_for_repair(
                child, parent_state, parent_wf["id"], repair_ids,
            )
            child = self.get(str(record["workflow_id"]))
        expected = {str(x) for x in record.get("section_ids") or []}
        completed = {str(item.get("section_id")) for item in child["state"].get("section_results", [])}
        if child["status"] == "COMPLETED" and expected <= completed:
            return child
        record["started_at"] = utc_now()
        record["status"] = "RUNNING"
        self._update(parent_wf, state=parent_state)
        child["status"] = "RUNNING"
        self._update(child, status="RUNNING", state=child["state"])
        result = await self._write_sections_serial(child, child["state"])
        child = self.get(child["id"])
        if result is not None or child["status"] == "BLOCKED":
            return child
        completed = {str(item.get("section_id")) for item in child["state"].get("section_results", [])}
        if not expected <= completed:
            child["state"]["last_error"] = (
                f"并发组 {record['group_id']} 未完成全部章节：expected={sorted(expected)}, completed={sorted(completed)}"
            )
            self._update(child, status="BLOCKED", state=child["state"])
            return self.get(child["id"])
        child["state"]["group_status"] = "COMPLETED"
        child["state"]["completed_at"] = utc_now()
        self._update(child, status="COMPLETED", state=child["state"])
        record["status"] = "COMPLETED"
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
        failures: list[str] = []
        children: list[dict[str, Any]] = []
        for record, result in zip(records, results):
            children.append(result)
            record["status"] = result["status"]
            if result["status"] != "COMPLETED":
                failures.append(f"{record['group_id']}: {result['state'].get('last_error') or result['status']}")
        state["full_proposal_parallel_finished_at"] = utc_now()
        if failures:
            state["last_error"] = "完整申请书并发组失败：" + "；".join(failures)
            self._update(wf, status="BLOCKED", state=state)
            return self.get(wf["id"])

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
            self._update(wf, status="BLOCKED", state=state)
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
        state.pop("full_proposal_repair_attempt_id", None)
        state.pop("last_error", None)
        wf["current_step"] += 1
        skip_gate = bool(state.pop("skip_candidate_gate_once", False))
        self._update(wf, current_step=wf["current_step"], state=state)
        if skip_gate:
            return None
        refreshed = self.get(wf["id"])
        self._create_gate(refreshed, "CANDIDATE_REVIEW", target_id=wf["id"], questions=[])
        self._update(refreshed, status="WAITING_GATE", state=state)
        return self.get(wf["id"])
