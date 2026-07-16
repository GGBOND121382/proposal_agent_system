from __future__ import annotations

import copy
from typing import Any

from .util import sha256_json, utc_now

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


class FullProposalContractMixin:
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
