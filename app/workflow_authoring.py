from __future__ import annotations

from typing import Any

from .executor import PromptExecutionError


THREE_SECTION_PROFILE_ORDER = (
    "BACKGROUND_AND_SIGNIFICANCE",
    "RESEARCH_CONTENT",
    "TECHNICAL_ROUTE",
)


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
                self._observe_quality_result(wf, state, prompt_id, result)
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
        if writing_findings and not affected and self._three_section_mode(state):
            # The critic assigned the defect to writing but omitted a section locator.
            # Fail closed by regenerating the complete three-section set rather than
            # asking a human to edit prose or silently accepting an unowned finding.
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
        if self._three_section_mode(state or {"options": options}):
            sections = self._resolve_three_section_contract(sections, state or {"options": options})

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

