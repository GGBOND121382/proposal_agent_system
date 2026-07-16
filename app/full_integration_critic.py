from __future__ import annotations

import json
from typing import Any

from .util import sha256_json, utc_now


REQUIRED_ARGUMENT_CHAINS = {
    "GAP_TO_QUESTION",
    "QUESTION_TO_OBJECTIVE",
    "OBJECTIVE_TO_WORK_PACKAGE",
    "WORK_PACKAGE_TO_METHOD",
    "METHOD_TO_EVALUATION",
    "RESULT_TO_CONTRIBUTION",
}

REQUIRED_QUALITY_DIMENSIONS = {
    "DOCUMENT_TYPE_FIT",
    "CENTRAL_THESIS",
    "ARGUMENT_CHAIN",
    "EVIDENCE_SUPPORT",
    "METHOD_SUBSTANCE",
    "INNOVATION_BASELINE",
    "FEASIBILITY_FOUNDATION",
    "METRIC_JUSTIFICATION",
    "SECTION_UNIQUENESS",
    "STYLE_AND_DENSITY",
    "PAGE_BUDGET",
    "CROSS_SECTION_CONSISTENCY",
}


class FullIntegrationCriticMixin:
    """Hard evidence and provenance checks for the whole-proposal critic.

    The model judges scientific merit, but it is not trusted to declare that it
    received the complete document or that a later review is independent.  Those
    properties are established from the frozen contract, child workflow state and
    persisted prompt runs before and after every P-INTEGRATION-CRITIC call.
    """

    def _validate_full_proposal_integration_envelope(
        self,
        state: dict[str, Any],
        envelope: dict[str, Any],
    ) -> None:
        super()._validate_full_proposal_integration_envelope(state, envelope)
        if not self._full_proposal_mode(state):
            return

        contract = state.get("full_proposal_contract") or {}
        contract_hash = str(contract.get("contract_hash") or "")
        children = state.get("full_proposal_children") or {}
        child_ids = [str(item) for item in state.get("authoring_child_workflow_ids") or [] if item]
        expected_groups = {
            str(group.get("group_id")): {str(s) for s in group.get("section_ids") or []}
            for group in contract.get("groups") or []
            if isinstance(group, dict) and group.get("section_ids")
        }
        if not contract_hash or not child_ids or len(child_ids) != len(set(child_ids)):
            raise ValueError("全文 Integration Critic 缺少冻结合同或唯一子工作流集合。")
        if set(children) != set(expected_groups):
            raise ValueError(
                "全文 Integration Critic 的并发组与冻结合同不一致；"
                f"expected={sorted(expected_groups)}, actual={sorted(children)}"
            )

        section_owner: dict[str, str] = {}
        for group_id, expected_sections in expected_groups.items():
            record = children.get(group_id) or {}
            workflow_id = str(record.get("workflow_id") or "")
            if workflow_id not in child_ids:
                raise ValueError(f"并发组 {group_id} 未登记到全文审查子工作流集合。")
            child = self.get(workflow_id)
            child_state = child["state"]
            actual_sections = {
                str(item.get("section_id"))
                for item in child_state.get("section_results") or []
                if isinstance(item, dict) and item.get("section_id")
            }
            if (
                child.get("status") != "COMPLETED"
                or str(child_state.get("parent_workflow_id") or "") == ""
                or str(child_state.get("full_proposal_contract_hash") or "") != contract_hash
                or actual_sections != expected_sections
            ):
                raise ValueError(
                    f"并发组 {group_id} 尚未形成与冻结合同一致的完成快照；"
                    f"status={child.get('status')}, expected={sorted(expected_sections)}, actual={sorted(actual_sections)}"
                )
            for section_id in expected_sections:
                if section_id in section_owner:
                    raise ValueError(f"章节 {section_id} 被多个并发组声明为最终责任方。")
                section_owner[section_id] = workflow_id

        payload = envelope.get("payload") or {}
        candidates = payload.get("candidate_sections") or []
        section_map = payload.get("document_section_map") or []
        map_by_section = {
            str(item.get("section_id")): item
            for item in section_map
            if isinstance(item, dict) and item.get("section_id")
        }
        candidate_ids: set[str] = set()
        manifest: list[dict[str, Any]] = []
        for item in candidates:
            section_id = str((item or {}).get("section_id") or "")
            candidate = (item or {}).get("candidate") or {}
            candidate_id = str(candidate.get("candidate_id") or "")
            if not section_id or not candidate_id:
                raise ValueError("全文候选章节缺少 section_id 或 candidate_id。")
            if candidate_id in candidate_ids:
                raise ValueError(f"全文候选集合重复使用 candidate_id：{candidate_id}")
            candidate_ids.add(candidate_id)
            mapped_id = str((map_by_section.get(section_id) or {}).get("candidate_id") or "")
            if mapped_id != candidate_id:
                raise ValueError(
                    f"章节 {section_id} 的 document_section_map candidate_id 与候选对象不一致："
                    f"map={mapped_id}, candidate={candidate_id}"
                )
            owner_id = section_owner.get(section_id)
            if not owner_id:
                raise ValueError(f"章节 {section_id} 没有冻结的责任子工作流。")
            provenance = self._final_candidate_provenance(
                project_id=str(envelope.get("scope", {}).get("project_id") or ""),
                workflow_id=owner_id,
                section_id=section_id,
                candidate_id=candidate_id,
            )
            manifest.append({
                "section_id": section_id,
                "title": str((map_by_section.get(section_id) or {}).get("title") or ""),
                "candidate_id": candidate_id,
                "candidate_hash": sha256_json(candidate),
                "producer_workflow_id": owner_id,
                **provenance,
            })

        ordered_ids = [str(item.get("section_id")) for item in section_map if isinstance(item, dict)]
        manifest_by_id = {item["section_id"]: item for item in manifest}
        ordered_manifest = [manifest_by_id[section_id] for section_id in ordered_ids]
        snapshot_core = {
            "contract_hash": contract_hash,
            "section_count": len(ordered_manifest),
            "child_workflow_ids": child_ids,
            "sections": ordered_manifest,
        }
        snapshot = {"captured_at": utc_now(), **snapshot_core}
        snapshot["candidate_set_hash"] = sha256_json(snapshot_core)
        state["full_integration_input_snapshot"] = snapshot

    def _final_candidate_provenance(
        self,
        *,
        project_id: str,
        workflow_id: str,
        section_id: str,
        candidate_id: str,
    ) -> dict[str, Any]:
        rows = self.db.fetchall(
            """SELECT id,prompt_id,input_json,output_json,input_hash,output_hash,created_at
               FROM prompt_runs
               WHERE project_id=? AND workflow_id=?
                 AND prompt_id IN ('P-EXPRESSION-POLISH','P-EXPRESSION-CRITIC')
                 AND status='PASS'
               ORDER BY created_at,id""",
            (project_id, workflow_id),
        )
        polish = None
        critic = None
        for row in rows:
            input_data = json.loads(row.get("input_json") or "{}")
            output_data = json.loads(row.get("output_json") or "{}")
            source = (input_data.get("payload") or {}).get("source_section") or {}
            if str(source.get("section_id") or "") != section_id:
                continue
            if row.get("prompt_id") == "P-EXPRESSION-POLISH":
                result = output_data.get("result") or {}
                if str(result.get("candidate_id") or "") == candidate_id:
                    polish = row
            elif row.get("prompt_id") == "P-EXPRESSION-CRITIC":
                polished = (input_data.get("payload") or {}).get("polished_candidate") or {}
                if str(polished.get("candidate_id") or "") == candidate_id:
                    critic = row
        if polish is None or critic is None:
            raise ValueError(
                f"章节 {section_id} 的候选 {candidate_id} 不是该责任子工作流中"
                "通过 Expression Polish 和独立 Expression Critic 的最终版本。"
            )
        if str(polish.get("id")) == str(critic.get("id")):
            raise ValueError(f"章节 {section_id} 的表达生产与复审运行不能相同。")
        return {
            "polish_run_id": str(polish.get("id")),
            "expression_critic_run_id": str(critic.get("id")),
            "polish_input_hash": str(polish.get("input_hash") or ""),
            "polish_output_hash": str(polish.get("output_hash") or ""),
            "expression_critic_output_hash": str(critic.get("output_hash") or ""),
        }

    def _record_full_integration_review(
        self,
        wf: dict[str, Any],
        state: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        if not self._full_proposal_mode(state):
            return
        snapshot = state.get("full_integration_input_snapshot") or {}
        if not snapshot.get("candidate_set_hash"):
            raise ValueError("全文 Integration Critic 缺少审查前候选快照。")
        output = result.get("output") or {}
        body = output.get("result") or {}
        run = self.db.fetchone(
            "SELECT input_hash,output_hash,model_id,endpoint_id,created_at FROM prompt_runs WHERE id=?",
            (result.get("run_id"),),
        ) or {}
        dimensions = {
            str(item.get("dimension")): item
            for item in body.get("quality_dimensions") or []
            if isinstance(item, dict)
        }
        chains = {
            str(item.get("chain_type")): item
            for item in body.get("argument_chain_checks") or []
            if isinstance(item, dict)
        }
        findings = [item for item in output.get("findings") or [] if isinstance(item, dict)]
        blockers = [
            item for item in findings
            if str(item.get("severity")) in {"P0", "P1"} and item.get("blocking", True)
        ]
        checks = {
            "all_required_dimensions_passed": all(
                dim in dimensions
                and dimensions[dim].get("passed", False)
                and float(dimensions[dim].get("score", 0)) >= 3
                for dim in REQUIRED_QUALITY_DIMENSIONS
            ),
            "all_six_argument_chains_complete": all(
                chain in chains and chains[chain].get("complete", False)
                for chain in REQUIRED_ARGUMENT_CHAINS
            ),
            "central_proposition_covered": bool((body.get("central_proposition_coverage") or {}).get("covered")),
            "document_type_clean": not bool((body.get("document_type_drift") or {}).get("detected")),
            "redundancy_clean": all(
                int((body.get("redundancy_report") or {}).get(key, 0)) == 0
                for key in (
                    "exact_duplicate_groups", "semantic_template_groups",
                    "duplicate_information_key_groups", "claim_overconcentration_groups",
                    "template_skeleton_groups",
                )
            ),
            "page_budget_passed": bool((body.get("page_budget_check") or {}).get("within_budget")),
            "no_blocking_findings": not blockers,
            "no_unresolved_items": not list(output.get("unresolved_items") or []),
        }
        history = state.setdefault("full_proposal_review_history", [])
        previous = history[-1] if history else None
        independent = previous is None or str(previous.get("run_id")) != str(result.get("run_id"))
        candidate_changed_after_revision = True
        if previous and previous.get("status") in {"REVISE", "BLOCK"}:
            candidate_changed_after_revision = (
                str(previous.get("candidate_set_hash") or "")
                != str(snapshot.get("candidate_set_hash") or "")
            )
        if result.get("status") == "PASS" and (not all(checks.values()) or not independent or not candidate_changed_after_revision):
            failed = [key for key, passed in checks.items() if not passed]
            if not independent:
                failed.append("review_run_not_independent")
            if not candidate_changed_after_revision:
                failed.append("candidate_set_unchanged_after_repair")
            raise ValueError(
                "全文 Integration Critic 不满足放行证据：" + "、".join(failed)
            )
        record = {
            "review_index": len(history) + 1,
            "run_id": str(result.get("run_id") or ""),
            "status": str(result.get("status") or ""),
            "recorded_at": utc_now(),
            "contract_hash": snapshot.get("contract_hash"),
            "candidate_set_hash": snapshot.get("candidate_set_hash"),
            "section_count": snapshot.get("section_count"),
            "section_manifest": snapshot.get("sections"),
            "child_workflow_ids": snapshot.get("child_workflow_ids"),
            "input_hash": run.get("input_hash"),
            "output_hash": run.get("output_hash"),
            "model_id": run.get("model_id"),
            "endpoint_id": run.get("endpoint_id"),
            "finding_codes": [str(item.get("code") or "") for item in findings],
            "routing_actions": list(body.get("routing_actions") or []),
            "checks": checks,
            "independent_from_previous_review": independent,
            "candidate_changed_after_revision": candidate_changed_after_revision,
        }
        history.append(record)
        state["full_integration_last_review"] = record
        self._update(wf, state=state)
