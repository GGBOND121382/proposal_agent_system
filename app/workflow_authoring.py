from __future__ import annotations

from typing import Any

from .executor import PromptExecutionError


class WorkflowAuthoringMixin:
    async def _write_sections(self, wf: dict[str, Any], state: dict[str, Any]) -> dict[str, Any] | None:
        """Generate and review every requested proposal section before one human review gate.

        The V2 prompts are section-oriented.  Running them once for a full-document task
        silently produced only one candidate.  This orchestrator keeps the prompt contracts
        unchanged while iterating over the CURRENT_PROPOSAL section tree.
        """
        sections = self._target_sections(wf["project_id"], state.get("options") or {}, state)
        completed = {item.get("section_id") for item in state.get("section_results", [])}
        state.setdefault("section_results", [])
        for section in sections:
            section_id = section["section_id"]
            if section_id in completed:
                continue
            state["active_section_id"] = section_id
            state["active_section_title"] = section.get("title")
            section_record = {"section_id": section_id, "title": section.get("title"), "runs": []}
            for prompt_id in ["P-WRITE-BLUEPRINT", "P-WRITE-BLUEPRINT-CRITIC", "P-WRITE-CONTENT", "P-WRITE-CRITIC", "P-EXPRESSION-POLISH", "P-EXPRESSION-CRITIC"]:
                try:
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
                except (PromptExecutionError, ValueError, KeyError) as exc:
                    state["last_error"] = f"{section.get('title')}: {exc}"
                    self._update(wf, status="BLOCKED", state=state)
                    return self.get(wf["id"])
                if prompt_id == "P-WRITE-CONTENT" and self.diagram_enrichment is not None and result["status"] == "PASS":
                    result["output"] = await self.diagram_enrichment.enrich(
                        project_id=wf["project_id"],
                        workflow_id=wf["id"],
                        run_id=result["run_id"],
                        section=section,
                        output=result["output"],
                        security_level=result["output"].get("source_refs", [{}])[0].get("security_level", "INTERNAL") if result["output"].get("source_refs") else "INTERNAL",
                    )
                section_record["runs"].append({"prompt_id": prompt_id, "run_id": result["run_id"], "status": result["status"]})
                state["original_environment"] = result["route"]["environment"]
                if result["status"] != "PASS":
                    state["last_error"] = f"{section.get('title')} / {prompt_id} returned {result['status']}"
                    self._update(wf, status="BLOCKED", state=state)
                    return self.get(wf["id"])
            state["section_results"].append(section_record)
            self._update(wf, state=state)

        state.pop("active_section_id", None)
        state.pop("active_section_title", None)
        state.pop("integration_repair_section_ids", None)
        wf["current_step"] += 1
        skip_gate = bool(state.pop("skip_candidate_gate_once", False))
        self._update(wf, current_step=wf["current_step"], state=state)
        if skip_gate:
            # Integration-directed machine repair must be re-evaluated, not
            # converted into a human confirmation of an unchanged document.
            return None
        refreshed = self.get(wf["id"])
        self._create_gate(refreshed, "CANDIDATE_REVIEW", target_id=wf["id"], questions=[])
        self._update(refreshed, status="WAITING_GATE", state=state)
        return self.get(wf["id"])

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
            state.pop("integration_repair_section_ids", None)
            state.pop("integration_repair_findings", None)
            target_step = next(
                index for index, step in enumerate(self.get(wf["id"])["steps"])
                if step.get("prompt_id") == "P-REVISION-PLAN"
            )
            wf["current_step"] = target_step
            self._update(wf, status="RUNNING", current_step=target_step, state=state)
            return "SCHEDULED"

        result = output.get("result") or {}
        affected: set[str] = set()
        for key in ("redundancy_report", "document_type_drift", "page_budget_check"):
            report = result.get(key) or {}
            affected.update(str(x) for x in report.get("affected_section_ids", []) if x)
            affected.update(str(x) for x in report.get("overflow_section_ids", []) if x)
        repairable_codes = {
            "QG_DOCUMENT_TEMPLATE_REPETITION", "DOCUMENT_TEMPLATE_REPETITION",
            "QG_DOCUMENT_DOMINATED_BY_AGENT_SYSTEM", "DOCUMENT_TYPE_DRIFT",
        }
        writing_findings = [
            item for item in findings
            if str(item.get("code") or "") in repairable_codes
            or str(item.get("suggested_route") or "") == "WRITING_AGENT"
        ]
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
        state["section_results"] = [
            item for item in state.get("section_results", []) if str(item.get("section_id")) not in affected
        ]
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

