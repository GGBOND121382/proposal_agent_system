from __future__ import annotations

from typing import Any

from .util import sha256_text
from .workflow_input import CURRENT_PROPOSAL_INPUT, WorkflowInputRequired, material_input_questions
from .workflow_status import WorkflowStatus


class FullProposalRepairMixin:
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
        repairable_codes = {
            "QG_DOCUMENT_TEMPLATE_REPETITION", "DOCUMENT_TEMPLATE_REPETITION",
            "QG_DOCUMENT_DOMINATED_BY_AGENT_SYSTEM", "DOCUMENT_TYPE_DRIFT",
        }

        def effective_route(item: dict[str, Any]) -> str:
            """Resolve exactly one repair owner for a whole-document finding.

            The Critic's explicit ``suggested_route`` is part of the structured
            repair contract and must win over broad categories such as ARGUMENT.
            Category/code inference is only a compatibility fallback for older
            findings that did not declare a route.  Without this precedence, a
            local conclusion rewrite can incorrectly invalidate the complete
            argument architecture and every generated section.
            """
            explicit = str(item.get("suggested_route") or "").strip()
            if explicit:
                return explicit
            code = str(item.get("code") or "")
            if code in planning_codes:
                return "PLANNING_AGENT"
            if str(item.get("category") or "") == "ARGUMENT":
                return "ARGUMENT_ARCHITECTURE_AGENT"
            if code in repairable_codes:
                return "WRITING_AGENT"
            return ""

        routed_findings = [(item, effective_route(item)) for item in findings]
        argument_findings = [
            item for item, route in routed_findings if route in argument_routes
        ]
        planning_findings = [
            item for item, route in routed_findings if route == "PLANNING_AGENT"
        ]

        if argument_findings:
            rounds = int(state.get("integration_argument_rounds", 0))
            if rounds >= 1:
                state["last_error"] = "全篇审查在一次论证架构重构后仍发现上游论证缺陷，需要补充事实或由项目负责人调整中心命题。"
                self._update(wf, status=WorkflowStatus.BLOCKED_CONTENT.value, state=state)
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
                self._update(wf, status=WorkflowStatus.BLOCKED_CONTENT.value, state=state)
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
        writing_findings = [
            item for item, route in routed_findings if route == "WRITING_AGENT"
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
            self._update(wf, status=WorkflowStatus.BLOCKED_CONTENT.value, state=state)
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

    def _target_sections(
        self,
        project_id: str,
        options: dict[str, Any],
        state: dict[str, Any] | None = None,
        *,
        workflow_id: str | None = None,
    ) -> list[dict[str, Any]]:
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
        plan_workflow_id = str(
            (state or {}).get("parent_workflow_id")
            or workflow_id
            or (state or {}).get("current_workflow_id")
            or ""
        ) or None
        plan = self._context_result(
            project_id,
            "P-REVISION-PLAN",
            "revision_plan",
            workflow_id=plan_workflow_id,
            exact_workflow=bool(plan_workflow_id),
        ) or {}
        architecture = plan.get("narrative_architecture") or {}
        planned: list[dict[str, Any]] = []
        planned_ids: set[str] = set()
        project_row = self.db.fetchone("SELECT security_level FROM projects WHERE id=?", (project_id,)) or {}
        project_security_level = str(project_row.get("security_level") or "INTERNAL")
        for contract in architecture.get("section_contracts", []):
            if not isinstance(contract, dict) or contract.get("placement") == "OMIT":
                continue
            contract_title = str(contract.get("title") or "").strip()
            if contract_title in {"", "全文"}:
                continue
            section = by_id.get(str(contract.get("section_id"))) or by_title.get(contract_title)
            if section is None and contract.get("section_id") and contract.get("title"):
                # A confirmed revision plan may define a new target section even
                # when no CURRENT_PROPOSAL draft exists.  Represent that target
                # explicitly instead of falling back to a Replay sample.
                empty_text = ""
                section = {
                    "section_id": str(contract["section_id"]),
                    "section_key": str(contract.get("profile_id") or contract["section_id"]),
                    "title": str(contract["title"]),
                    "level": 1,
                    "text": empty_text,
                    "text_hash": sha256_text(empty_text),
                    "block_ids": [],
                    "contains_table": False,
                    "contains_formula": False,
                    "contains_image": False,
                    "contains_comment": False,
                    "contains_revision": False,
                    "security_level": project_security_level,
                }
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
        raise WorkflowInputRequired(
            "P-WRITE-BLUEPRINT",
            gate_type=CURRENT_PROPOSAL_INPUT,
            missing_paths=["payload.source_section", "payload.revision_plan.narrative_architecture.section_contracts"],
            questions=material_input_questions(CURRENT_PROPOSAL_INPUT),
            message=(
                "没有 CURRENT_PROPOSAL 章节，也没有已确认修订计划中的明确目标章节。"
                "系统不会使用 Replay 章节代替真实写作对象。"
            ),
        )
