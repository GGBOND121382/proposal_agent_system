from __future__ import annotations

import asyncio
import copy
import json
from typing import Any

from .executor import PromptExecutionError
from .util import new_id, sha256_json, utc_now

THREE_SECTION_PROFILE_ORDER = (
    "BACKGROUND_AND_SIGNIFICANCE",
    "RESEARCH_CONTENT",
    "TECHNICAL_ROUTE",
)

FULL_PROPOSAL_GROUPS = (
    {
        "group_id": "GROUP_1_BACKGROUND_AND_PROBLEM",
        "title": "背景与问题",
        "profiles": (
            "ABSTRACT", "PROJECT_OVERVIEW", "BACKGROUND_AND_SIGNIFICANCE",
            "LITERATURE_REVIEW", "NEED_ANALYSIS",
        ),
    },
    {
        "group_id": "GROUP_2_OBJECTIVES_AND_TASKS",
        "title": "目标与任务",
        "profiles": ("KEY_ISSUE", "RESEARCH_OBJECTIVE", "RESEARCH_CONTENT"),
    },
    {
        "group_id": "GROUP_3_METHOD_AND_VALIDATION",
        "title": "方法与验证",
        "profiles": (
            "METHOD_AND_ALGORITHM", "TECHNICAL_ROUTE", "EVALUATION", "INNOVATION",
        ),
    },
    {
        "group_id": "GROUP_4_IMPLEMENTATION_AND_ASSURANCE",
        "title": "实施与保障",
        "profiles": (
            "OUTPUTS_AND_METRICS", "RESEARCH_FOUNDATION", "PROGRESS_BUDGET_RISK",
            "CONCLUSION", "SECTION_GENERAL",
        ),
    },
    {
        "group_id": "GROUP_5_FIGURES_AND_REFERENCES",
        "title": "图表与引用",
        "profiles": ("REFERENCES", "APPENDIX"),
        "cross_cutting_roles": (
            "MERMAID", "TABLE", "FORMULA", "REFERENCE", "CROSS_REFERENCE",
        ),
    },
)
FULL_PROPOSAL_GROUP_ORDER = tuple(item["group_id"] for item in FULL_PROPOSAL_GROUPS)
FULL_PROPOSAL_PROFILE_GROUP = {
    profile: item["group_id"]
    for item in FULL_PROPOSAL_GROUPS
    for profile in item["profiles"]
}
FULL_PROPOSAL_CORE_GROUPS = set(FULL_PROPOSAL_GROUP_ORDER[:4])


class WorkflowAuthoringMixin:
    SECTION_PHASES = {
        "BLUEPRINT": ("P-WRITE-BLUEPRINT", "BLUEPRINT_CRITIC"),
        "BLUEPRINT_CRITIC": ("P-WRITE-BLUEPRINT-CRITIC", "CONTENT"),
        "CONTENT": ("P-WRITE-CONTENT", "CONTENT_CRITIC"),
        "CONTENT_CRITIC": ("P-WRITE-CRITIC", "POLISH"),
        "POLISH": ("P-EXPRESSION-POLISH", "EXPRESSION_CRITIC"),
        "EXPRESSION_CRITIC": ("P-EXPRESSION-CRITIC", "DONE"),
    }
    SECTION_REPAIR_CRITICS = {"P-WRITE-BLUEPRINT-CRITIC", "P-WRITE-CRITIC"}

    @staticmethod
    def _append_section_run(progress: dict[str, Any], result: dict[str, Any], *, prompt_id: str | None = None, role: str | None = None) -> None:
        run_id = str(result.get("run_id") or "")
        if run_id and any(str(item.get("run_id") or "") == run_id for item in progress["runs"]):
            return
        record = {
            "prompt_id": prompt_id or result.get("prompt_id"),
            "run_id": run_id,
            "status": result.get("status"),
        }
        if role:
            record["role"] = role
        progress["runs"].append(record)

    def _block_section_chain(
        self,
        wf: dict[str, Any],
        state: dict[str, Any],
        section: dict[str, Any],
        message: str,
    ) -> dict[str, Any]:
        section_id = str(section.get("section_id") or "")
        progress = state.setdefault("section_progress", {}).setdefault(section_id, {})
        progress["status"] = "BLOCKED"
        progress["last_error"] = message
        state["last_error"] = f"{section.get('title')}: {message}"
        self._update(wf, status="BLOCKED", state=state)
        return self.get(wf["id"])

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

    def _full_proposal_mode(self, state: dict[str, Any]) -> bool:
        options = state.get("options") or {}
        return bool(
            options.get("full_proposal_concurrent")
            or options.get("integration_scope") == "FULL_PROPOSAL_CONCURRENT"
        ) and not bool(options.get("concurrent_group_child"))

    def _resolve_full_proposal_contract(
        self,
        sections: list[dict[str, Any]],
        state: dict[str, Any],
    ) -> list[dict[str, Any]]:
        seen_ids: set[str] = set()
        records: list[dict[str, Any]] = []
        groups = {
            item["group_id"]: {
                "group_id": item["group_id"],
                "title": item["title"],
                "section_ids": [],
                "cross_cutting_roles": list(item.get("cross_cutting_roles") or []),
            }
            for item in FULL_PROPOSAL_GROUPS
        }
        for order, section in enumerate(sections, 1):
            section_id = str(section.get("section_id") or "").strip()
            if not section_id:
                raise ValueError("完整申请书并发编制发现缺少 section_id 的章节。")
            if section_id in seen_ids:
                raise ValueError(f"完整申请书 Section Contract 中章节重复：{section_id}")
            seen_ids.add(section_id)
            profile = self.pack.section_profile_for(str(section.get("title") or ""))
            profile_id = str(profile.get("profile_id") or "SECTION_GENERAL")
            group_id = FULL_PROPOSAL_PROFILE_GROUP.get(profile_id, "GROUP_4_IMPLEMENTATION_AND_ASSURANCE")
            record = {
                "section_id": section_id,
                "title": str(section.get("title") or ""),
                "profile_id": profile_id,
                "group_id": group_id,
                "document_order": order,
            }
            records.append(record)
            groups[group_id]["section_ids"].append(section_id)

        nonempty_core = {
            group_id for group_id in FULL_PROPOSAL_CORE_GROUPS
            if groups[group_id]["section_ids"]
        }
        if nonempty_core != FULL_PROPOSAL_CORE_GROUPS:
            missing = [
                groups[group_id]["title"]
                for group_id in FULL_PROPOSAL_GROUP_ORDER[:4]
                if group_id not in nonempty_core
            ]
            raise ValueError(
                "完整申请书必须覆盖背景与问题、目标与任务、方法与验证、实施与保障四个核心组；"
                "缺少：" + "、".join(missing)
            )
        if len(records) < 8:
            raise ValueError(
                f"完整申请书并发编制至少需要 8 个唯一章节；当前仅 {len(records)} 个。"
            )

        contract_core = {
            "contract_type": "FULL_PROPOSAL_CONCURRENT",
            "group_order": list(FULL_PROPOSAL_GROUP_ORDER),
            "groups": [groups[group_id] for group_id in FULL_PROPOSAL_GROUP_ORDER],
            "sections": records,
        }
        contract_core["contract_hash"] = sha256_json(contract_core)
        existing = state.get("full_proposal_contract") or {}
        if existing and existing.get("contract_hash") != contract_core["contract_hash"]:
            raise ValueError(
                "完整申请书 Section Contract 已冻结，当前章节集合或分组发生漂移；"
                "必须回到 P-REVISION-PLAN 重新生成并复审，不能在写作阶段静默改组。"
            )
        state["full_proposal_contract"] = contract_core
        return sections

    def _invalidate_full_proposal_generation(
        self,
        state: dict[str, Any],
        *,
        reason: str,
    ) -> None:
        """Archive and detach a full concurrent generation after upstream drift.

        Argument or planning defects invalidate every downstream section. Reusing
        completed children would preserve prose generated from the rejected
        architecture. Old children remain in SQLite for audit, while the parent
        starts a new generation after the upstream producer and critic pass.
        """
        if not self._full_proposal_mode(state):
            return
        children = copy.deepcopy(state.get("full_proposal_children") or {})
        if children:
            state.setdefault("full_proposal_child_generations", []).append({
                "invalidated_at": utc_now(),
                "reason": reason,
                "contract_hash": (state.get("full_proposal_contract") or {}).get("contract_hash"),
                "children": children,
            })
        state["full_proposal_children"] = {}
        state.pop("full_proposal_contract", None)
        state.pop("full_proposal_virtual_lanes", None)
        state.pop("full_proposal_concurrency", None)
        state.pop("authoring_child_workflow_ids", None)
        state.pop("section_progress", None)

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

    async def _write_sections(self, wf: dict[str, Any], state: dict[str, Any]) -> dict[str, Any] | None:
        if self._full_proposal_mode(state):
            return await self._write_full_proposal_concurrently(wf, state)
        return await self._write_sections_serial(wf, state)

    async def _write_sections_serial(self, wf: dict[str, Any], state: dict[str, Any]) -> dict[str, Any] | None:
        """Run an isolated, recoverable producer/critic/repair chain for each section.

        The chain is:
        Blueprint -> Blueprint Critic -> at most one Targeted Repair -> re-review
        -> Content -> Content Critic -> at most one Targeted Repair -> re-review
        -> Expression Polish -> Expression Critic.

        Progress is persisted after every model run.  A restart re-enters the same
        phase; the Track-A deterministic call key then reuses an already committed
        response instead of duplicating model calls or artifacts.
        """
        options = state.get("options") or {}
        sections = self._target_sections(wf["project_id"], options, state)
        if bool(options.get("single_section_complete_chain")) and len(sections) != 1:
            state["last_error"] = (
                "单章节完整链要求精确选择一个章节；当前匹配 " + str(len(sections)) + " 个。"
            )
            self._update(wf, status="BLOCKED", state=state)
            return self.get(wf["id"])

        completed = {str(item.get("section_id") or "") for item in state.get("section_results", [])}
        state.setdefault("section_results", [])
        progress_map = state.setdefault("section_progress", {})

        for section in sections:
            section_id = str(section["section_id"])
            if section_id in completed:
                continue
            state["active_section_id"] = section_id
            state["active_section_title"] = section.get("title")
            progress = progress_map.setdefault(
                section_id,
                {
                    "section_id": section_id,
                    "title": section.get("title"),
                    "phase": "BLUEPRINT",
                    "status": "RUNNING",
                    "runs": [],
                },
            )
            progress.setdefault("runs", [])
            progress.setdefault("phase", "BLUEPRINT")
            progress["status"] = "RUNNING"
            self._update(wf, state=state)

            while progress["phase"] != "DONE":
                phase = str(progress["phase"])
                if phase not in self.SECTION_PHASES:
                    return self._block_section_chain(wf, state, section, f"未知章节阶段：{phase}")
                prompt_id, next_phase = self.SECTION_PHASES[phase]
                try:
                    envelope, result = await self._execute_section_prompt(
                        wf, state, section, progress, prompt_id, role="INITIAL_REVIEW" if prompt_id.endswith("CRITIC") else "PRODUCER",
                    )
                except (PromptExecutionError, ValueError, KeyError) as exc:
                    return self._block_section_chain(wf, state, section, str(exc))

                if result["status"] == "PASS":
                    progress["phase"] = next_phase
                    self._update(wf, state=state)
                    continue

                if result["status"] == "REVISE" and prompt_id in self.SECTION_REPAIR_CRITICS:
                    if not self._can_auto_repair(prompt_id, state):
                        return self._block_section_chain(
                            wf, state, section, f"{prompt_id} 在一次定向修复后仍需修改；章节修复额度已耗尽。",
                        )
                    repaired = await self._auto_repair(wf, prompt_id, envelope, result["output"], state)
                    if not repaired:
                        return self._block_section_chain(
                            wf, state, section, f"{prompt_id} 返回 REVISE，但没有可执行的局部修复或定向修复失败。",
                        )
                    self._append_section_run(progress, repaired, prompt_id="P-TARGETED-REPAIR", role="TARGETED_REPAIR")
                    self._update(wf, state=state)
                    try:
                        _review_envelope, reviewed = await self._execute_section_prompt(
                            wf, state, section, progress, prompt_id, role="INDEPENDENT_REVIEW",
                        )
                    except (PromptExecutionError, ValueError, KeyError) as exc:
                        return self._block_section_chain(wf, state, section, f"定向修复后的独立复审失败：{exc}")
                    if reviewed["status"] != "PASS":
                        return self._block_section_chain(
                            wf, state, section,
                            f"{prompt_id} 定向修复后的独立复审返回 {reviewed['status']}；禁止二次自动修复或人工改正文放行。",
                        )
                    progress["phase"] = next_phase
                    self._update(wf, state=state)
                    continue

                return self._block_section_chain(
                    wf, state, section, f"{prompt_id} 返回 {result['status']}；该阶段不允许跳过或人工覆盖。",
                )

            progress["status"] = "COMPLETED"
            section_record = {
                "section_id": section_id,
                "title": section.get("title"),
                "status": "COMPLETED",
                "runs": list(progress["runs"]),
            }
            state["section_results"].append(section_record)
            completed.add(section_id)
            self._update(wf, state=state)

        state.pop("active_section_id", None)
        state.pop("active_section_title", None)
        state.pop("integration_repair_section_ids", None)
        state.pop("last_error", None)
        wf["current_step"] += 1
        skip_gate = bool(state.pop("skip_candidate_gate_once", False))
        suppress_gate = bool((state.get("options") or {}).get("suppress_candidate_gate"))
        self._update(wf, current_step=wf["current_step"], state=state)
        if skip_gate or suppress_gate:
            return None
        refreshed = self.get(wf["id"])
        self._create_gate(refreshed, "CANDIDATE_REVIEW", target_id=wf["id"], questions=[])
        self._update(refreshed, status="WAITING_GATE", state=state)
        return self.get(wf["id"])

    def _three_section_mode(self, state: dict[str, Any]) -> bool:
        options = state.get("options") or {}
        return bool(
            options.get("three_section_cross_chapter")
            or options.get("integration_scope") == "THREE_SECTION_CROSS_CHAPTER"
        )

    def _resolve_three_section_contract(
        self,
        sections: list[dict[str, Any]],
        state: dict[str, Any],
    ) -> list[dict[str, Any]]:
        by_profile: dict[str, list[dict[str, Any]]] = {profile: [] for profile in THREE_SECTION_PROFILE_ORDER}
        for section in sections:
            profile = self.pack.section_profile_for(str(section.get("title") or ""))
            profile_id = str(profile.get("profile_id") or "")
            if profile_id in by_profile:
                by_profile[profile_id].append(section)
        missing = [profile for profile, values in by_profile.items() if not values]
        duplicate = [profile for profile, values in by_profile.items() if len(values) > 1]
        if missing or duplicate:
            details = []
            if missing:
                details.append("缺少章节角色：" + "、".join(missing))
            if duplicate:
                details.append("章节角色重复：" + "、".join(duplicate))
            raise ValueError(
                "三章节跨章链必须且只能包含背景、研究内容、技术路线三个唯一章节；" + "；".join(details)
            )
        resolved = [by_profile[profile][0] for profile in THREE_SECTION_PROFILE_ORDER]
        state["three_section_contract"] = {
            "contract_type": "THREE_SECTION_CROSS_CHAPTER",
            "ordered_profiles": list(THREE_SECTION_PROFILE_ORDER),
            "sections": [
                {
                    "section_id": str(section.get("section_id")),
                    "title": str(section.get("title") or ""),
                    "profile_id": profile,
                    "order": index + 1,
                }
                for index, (profile, section) in enumerate(zip(THREE_SECTION_PROFILE_ORDER, resolved))
            ],
        }
        return resolved

    def _validate_three_section_integration_envelope(
        self,
        state: dict[str, Any],
        envelope: dict[str, Any],
    ) -> None:
        if not self._three_section_mode(state):
            return
        contract = state.get("three_section_contract") or {}
        expected = [
            str(item.get("section_id")) for item in contract.get("sections") or []
            if isinstance(item, dict) and item.get("section_id")
        ]
        candidates = (envelope.get("payload") or {}).get("candidate_sections") or []
        actual = [
            str(item.get("section_id")) for item in candidates
            if isinstance(item, dict) and item.get("section_id")
        ]
        if len(expected) != 3 or len(actual) != 3 or set(actual) != set(expected):
            raise ValueError(
                "三章节跨章审查输入必须与已冻结的背景—研究内容—技术路线合同完全一致；"
                f"expected={expected}, actual={actual}"
            )

    def _validate_full_proposal_integration_envelope(
        self,
        state: dict[str, Any],
        envelope: dict[str, Any],
    ) -> None:
        if not self._full_proposal_mode(state):
            return
        contract = state.get("full_proposal_contract") or {}
        expected = [
            str(item.get("section_id"))
            for item in contract.get("sections") or []
            if isinstance(item, dict) and item.get("section_id")
        ]
        payload = envelope.get("payload") or {}
        candidates = payload.get("candidate_sections") or []
        actual = [
            str(item.get("section_id"))
            for item in candidates
            if isinstance(item, dict) and item.get("section_id")
        ]
        section_map = [
            str(item.get("section_id"))
            for item in payload.get("document_section_map") or []
            if isinstance(item, dict) and item.get("section_id")
        ]
        if (
            len(expected) < 8
            or len(actual) != len(expected)
            or len(set(actual)) != len(actual)
            or set(actual) != set(expected)
            or section_map != expected
        ):
            raise ValueError(
                "完整申请书全文审查输入必须与冻结的 Section Contract 完全一致，"
                "且 document_section_map 保持原始章节顺序；"
                f"expected={expected}, candidates={actual}, section_map={section_map}"
            )

    @staticmethod
    def _section_ids_from_integration_output(
        output: dict[str, Any],
        known_section_ids: set[str],
    ) -> set[str]:
        result = output.get("result") or {}
        affected: set[str] = set()
        for key in ("redundancy_report", "document_type_drift", "page_budget_check"):
            report = result.get(key) or {}
            affected.update(str(x) for x in report.get("affected_section_ids", []) if x)
            affected.update(str(x) for x in report.get("overflow_section_ids", []) if x)
        for check in result.get("terminology_checks") or []:
            if isinstance(check, dict) and not check.get("consistent", True):
                affected.update(str(x) for x in check.get("sections", []) if x)
        evidence_strings: list[str] = []
        for check in result.get("numeric_checks") or []:
            if isinstance(check, dict) and not check.get("consistent", True):
                evidence_strings.extend(str(x) for x in check.get("occurrences", []) if x)
        for finding in output.get("findings") or []:
            if not isinstance(finding, dict):
                continue
            evidence_strings.append(str(finding.get("target_path_or_span") or ""))
            evidence_strings.extend(str(x) for x in finding.get("evidence_refs", []) if x)
        for value in evidence_strings:
            for section_id in known_section_ids:
                if section_id and section_id in value:
                    affected.add(section_id)
        return affected & known_section_ids

    def _prepare_integration_repair(self, wf: dict[str, Any], state: dict[str, Any], output: dict[str, Any]) -> str:
        """Route full-document findings to the earliest stage able to fix them.

        Argument defects require a new argument architecture; ownership and
        dependency defects require a new narrative plan; prose repetition can
        be repaired by rewriting only affected sections.  A later stage is
        never allowed to cosmetically mask an upstream structural defect.
        """
        findings = [
            item for item in output.get("findings", [])
            if isinstance(item, dict) and item.get("blocking", True)
        ]
        argument_routes = {"ARGUMENT_ARCHITECTURE_AGENT", "PROJECT_KNOWLEDGE_AGENT"}
        planning_codes = {
            "QG_DOCUMENT_DUPLICATE_INFORMATION_KEYS",
            "QG_DOCUMENT_CLAIM_OVERCONCENTRATION",
            "PAGE_BUDGET_EXCEEDED",
        }
        argument_findings = [
            item for item in findings
            if str(item.get("suggested_route") or "") in argument_routes
            or str(item.get("category") or "") == "ARGUMENT"
        ]
        planning_findings = [
            item for item in findings
            if str(item.get("suggested_route") or "") == "PLANNING_AGENT"
            or str(item.get("code") or "") in planning_codes
        ]

        if argument_findings:
            rounds = int(state.get("integration_argument_rounds", 0))
            if rounds >= 1:
                state["last_error"] = "全篇审查在一次论证架构重构后仍发现上游论证缺陷，需要补充事实或由项目负责人调整中心命题。"
                self._update(wf, status="BLOCKED", state=state)
                return "EXHAUSTED"
            state["integration_argument_rounds"] = rounds + 1
            state["argument_revision_findings"] = argument_findings
            state["planning_revision_findings"] = []
            state["section_results"] = []
            self._invalidate_full_proposal_generation(
                state, reason="INTEGRATION_ARGUMENT_ARCHITECTURE_REVISION",
            )
            state.pop("integration_repair_section_ids", None)
            state.pop("integration_repair_findings", None)
            target_step = next(
                index for index, step in enumerate(self.get(wf["id"])["steps"])
                if step.get("prompt_id") == "P-ARGUMENT-ARCHITECTURE"
            )
            wf["current_step"] = target_step
            self._update(wf, status="RUNNING", current_step=target_step, state=state)
            return "SCHEDULED"

        if planning_findings:
            rounds = int(state.get("integration_planning_rounds", 0))
            if rounds >= 1:
                state["last_error"] = "全篇审查在一次章节合同重构后仍发现命题或信息归属冲突，需要人工调整论证架构。"
                self._update(wf, status="BLOCKED", state=state)
                return "EXHAUSTED"
            state["integration_planning_rounds"] = rounds + 1
            state["planning_revision_findings"] = planning_findings
            state["section_results"] = []
            self._invalidate_full_proposal_generation(
                state, reason="INTEGRATION_SECTION_CONTRACT_REVISION",
            )
            state.pop("integration_repair_section_ids", None)
            state.pop("integration_repair_findings", None)
            target_step = next(
                index for index, step in enumerate(self.get(wf["id"])["steps"])
                if step.get("prompt_id") == "P-REVISION-PLAN"
            )
            wf["current_step"] = target_step
            self._update(wf, status="RUNNING", current_step=target_step, state=state)
            return "SCHEDULED"

        contract_sections = (state.get("three_section_contract") or {}).get("sections") or []
        known_section_ids = {
            str(item.get("section_id")) for item in contract_sections
            if isinstance(item, dict) and item.get("section_id")
        }
        if not known_section_ids:
            known_section_ids = {
                str(item.get("section_id")) for item in state.get("section_results", [])
                if isinstance(item, dict) and item.get("section_id")
            }
        affected = self._section_ids_from_integration_output(output, known_section_ids)
        repairable_codes = {
            "QG_DOCUMENT_TEMPLATE_REPETITION", "DOCUMENT_TEMPLATE_REPETITION",
            "QG_DOCUMENT_DOMINATED_BY_AGENT_SYSTEM", "DOCUMENT_TYPE_DRIFT",
        }
        writing_findings = [
            item for item in findings
            if str(item.get("code") or "") in repairable_codes
            or str(item.get("suggested_route") or "") == "WRITING_AGENT"
        ]
        if writing_findings and not affected and (
            self._three_section_mode(state) or self._full_proposal_mode(state)
        ):
            # A writing defect without a section locator cannot be silently accepted.
            # Regenerate the frozen three-section set rather than allowing manual edits.
            affected = set(known_section_ids)
        if not affected or not writing_findings:
            return "NOT_APPLICABLE"
        rounds = int(state.get("integration_repair_rounds", 0))
        if rounds >= 2:
            state["last_error"] = "全篇质量审查在两轮章节重写后仍未通过；需要修改论证架构或补充事实证据。"
            self._update(wf, status="BLOCKED", state=state)
            return "EXHAUSTED"
        state["integration_repair_rounds"] = rounds + 1
        state["integration_repair_section_ids"] = sorted(affected)
        state["integration_repair_findings"] = writing_findings
        state.setdefault("cross_section_repair_history", []).append({
            "round": rounds + 1,
            "finding_codes": [str(item.get("code") or "") for item in writing_findings],
            "responsible_section_ids": sorted(affected),
            "route": "WRITING_AGENT",
        })
        state["section_results"] = [
            item for item in state.get("section_results", []) if str(item.get("section_id")) not in affected
        ]
        # S1 persists per-section phase checkpoints.  A cross-section repair must
        # invalidate only the responsible sections; otherwise a recovered workflow
        # would see phase=DONE and reuse the unchanged body without creating repair
        # evidence for the open integration finding.
        progress_map = state.setdefault("section_progress", {})
        for section_id in affected:
            progress_map.pop(str(section_id), None)
        state["skip_candidate_gate_once"] = True
        write_step = next(
            index for index, step in enumerate(self.get(wf["id"])["steps"])
            if step.get("type") == "WRITE_SECTIONS"
        )
        wf["current_step"] = write_step
        self._update(wf, status="RUNNING", current_step=write_step, state=state)
        return "SCHEDULED"

    def _target_sections(self, project_id: str, options: dict[str, Any], state: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        source_sections = [
            section
            for section in self.context_builder.sections(project_id, "CURRENT_PROPOSAL")
            if section.get("level", 0) >= 1 and section.get("title", "").strip() not in {"", "全文"}
        ]
        by_id = {str(section.get("section_id")): section for section in source_sections if section.get("section_id")}
        by_title = {str(section.get("title")): section for section in source_sections if section.get("title")}

        # The approved narrative architecture, not the uploaded draft's raw
        # heading count, determines what belongs in the proposal.  This prevents
        # a long source outline from becoming dozens of same-type writing tasks.
        plan = self.context_builder._result(project_id, "P-REVISION-PLAN", "revision_plan") or {}
        architecture = plan.get("narrative_architecture") or {}
        planned: list[dict[str, Any]] = []
        planned_ids: set[str] = set()
        for contract in architecture.get("section_contracts", []):
            if not isinstance(contract, dict) or contract.get("placement") == "OMIT":
                continue
            section = by_id.get(str(contract.get("section_id"))) or by_title.get(str(contract.get("title")))
            section_id = str((section or {}).get("section_id") or "")
            if section and section_id not in planned_ids:
                planned.append(section)
                planned_ids.add(section_id)
        sections = planned or source_sections
        effective_state = state if state is not None else {"options": options}
        if self._three_section_mode(effective_state):
            sections = self._resolve_three_section_contract(sections, effective_state)
        if self._full_proposal_mode(effective_state):
            sections = self._resolve_full_proposal_contract(sections, effective_state)

        requested_ids = set(options.get("target_section_ids") or [])
        requested_titles = set(options.get("target_section_titles") or [])
        repair_ids = set((state or {}).get("integration_repair_section_ids") or [])
        if repair_ids:
            requested_ids = repair_ids
        if requested_ids or requested_titles:
            sections = [
                section
                for section in sections
                if section.get("section_id") in requested_ids or section.get("title") in requested_titles
            ]
        if sections:
            return sections
        # Backward-compatible smoke-test fallback for projects without an uploaded draft.
        return [self.pack.replay_input("P-WRITE-CONTENT")["payload"]["source_section"]]

