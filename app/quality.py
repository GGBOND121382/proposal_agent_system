from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable

from .util import new_id, sha256_json, utc_now
from .workflow_defs import CRITIC_PRODUCER


BLOCKING_SEVERITIES = {"P0", "P1"}
CLOSED_STATES = {"VERIFIED"}


class QualityGateBlocked(RuntimeError):
    def __init__(self, findings: list[dict[str, Any]]):
        self.findings = findings
        codes = ", ".join(str(item.get("finding", {}).get("code") or item.get("code") or "UNKNOWN") for item in findings[:8])
        super().__init__(f"存在未完成独立复审的P0/P1质量问题：{codes}")


@dataclass(frozen=True)
class ResponsibilityRoute:
    owner_kind: str
    owner: str
    workflow_type: str | None
    stage_prompt_ids: tuple[str, ...]
    reviewer_prompt_id: str | None
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "owner_kind": self.owner_kind,
            "owner": self.owner,
            "workflow_type": self.workflow_type,
            "stage_prompt_ids": list(self.stage_prompt_ids),
            "reviewer_prompt_id": self.reviewer_prompt_id,
            "reason": self.reason,
        }


ROUTE_STAGE_PROMPTS: dict[str, tuple[str, ...]] = {
    "PROJECT_KNOWLEDGE_AGENT": (
        "P-SCHEME-EXTRACT", "P-PROJECT-DEFINITION-EXTRACT", "P-FACT-EXTRACT",
        "P-PUBLIC-RESEARCH-SYNTHESIS",
    ),
    "ARGUMENT_ARCHITECTURE_AGENT": ("P-ARGUMENT-ARCHITECTURE",),
    "PLANNING_AGENT": ("P-REVISION-PLAN",),
    "WRITING_AGENT": ("P-WRITE-BLUEPRINT", "P-WRITE-CONTENT"),
    "EXPRESSION_EDITOR_AGENT": ("P-EXPRESSION-POLISH",),
    "SECURITY_REVIEW_AGENT": (
        "P-SECURITY-CLASSIFY", "P-SAFE-ONLINE-PACKAGE", "P-FINAL-CONFIDENTIALITY-REVIEW",
    ),
    "INTEGRATION_AGENT": ("P-INTEGRATION-CRITIC",),
}


class QualityLifecycleManager:
    """Append-only quality finding ledger built on the frozen artifacts table.

    The G0 contract freezes the SQLite schema.  Quality lifecycle events are therefore
    persisted as versioned ``QUALITY_FINDING`` artifacts instead of introducing a new
    mutable table.  A P0/P1 finding is complete only after a repair run and a later,
    independent review run are both recorded.  Merely changing workflow state or a
    human gate decision cannot close it.
    """

    artifact_type = "QUALITY_FINDING"

    def __init__(self, db):
        self.db = db

    # ---------- public query/gate API ----------

    def list_findings(
        self,
        project_id: str,
        *,
        workflow_id: str | None = None,
        states: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        latest = self._latest(project_id, workflow_id=workflow_id)
        values = list(latest.values())
        if states is not None:
            values = [item for item in values if str(item.get("lifecycle", {}).get("state")) in states]
        return sorted(values, key=lambda item: (item.get("finding", {}).get("severity", "P9"), item.get("finding_id", "")))

    def open_blockers(self, project_id: str, *, workflow_id: str | None = None) -> list[dict[str, Any]]:
        return [
            item for item in self.list_findings(project_id, workflow_id=workflow_id)
            if item.get("finding", {}).get("severity") in BLOCKING_SEVERITIES
            and item.get("finding", {}).get("blocking", True)
            and item.get("lifecycle", {}).get("state") not in CLOSED_STATES
        ]

    def assert_no_open_blockers(self, project_id: str, *, workflow_id: str | None = None) -> None:
        blockers = self.open_blockers(project_id, workflow_id=workflow_id)
        if blockers:
            raise QualityGateBlocked(blockers)

    def active_lineage_workflow_ids(self, project_id: str, *, review_workflow_id: str) -> list[str]:
        """Return the exact workflow lineage that contributes to a final candidate.

        Rejected fresh-generation attempts remain in the append-only quality ledger for
        audit, but they are not part of a later frozen candidate.  Final acceptance must
        therefore inspect the bound WF-4 parent, its exact producer children, the WF-5
        approval workflow when supplied, and the latest completed prerequisite workflows
        that existed before that WF-4 run.  It must not silently treat every abandoned
        project attempt as part of the current candidate.
        """
        review = self.db.fetchone(
            "SELECT * FROM workflows WHERE id=? AND project_id=?",
            (review_workflow_id, project_id),
        )
        if not review:
            raise KeyError(review_workflow_id)
        try:
            review_state = json.loads(review.get("state_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            review_state = {}

        workflow_ids: list[str] = [review_workflow_id]
        review_type = str(review.get("workflow_type") or "")
        source_workflow_id = ""
        if review_type == "WF-5_SECURITY_REVIEW_AND_EXPORT":
            source_workflow_id = str(
                review_state.get("source_workflow_id")
                or (review_state.get("options") or {}).get("source_workflow_id")
                or ""
            )
        elif review_type == "WF-4_PROPOSAL_AUTHORING":
            source_workflow_id = review_workflow_id

        source = None
        source_state: dict[str, Any] = {}
        if source_workflow_id:
            source = self.db.fetchone(
                "SELECT * FROM workflows WHERE id=? AND project_id=?",
                (source_workflow_id, project_id),
            )
            if not source or str(source.get("workflow_type")) != "WF-4_PROPOSAL_AUTHORING":
                raise ValueError(f"Final review is not bound to a valid WF-4 source: {source_workflow_id}")
            workflow_ids.append(source_workflow_id)
            try:
                source_state = json.loads(source.get("state_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                source_state = {}

            # The WF-5 manifest is the strongest binding because it lists the exact
            # candidate producer for every frozen section.
            for item in review_state.get("source_section_manifest") or []:
                if isinstance(item, dict) and item.get("producer_workflow_id"):
                    workflow_ids.append(str(item["producer_workflow_id"]))

            # WF-4 acceptance/export can be invoked directly, before a WF-5 object is
            # available, so retain the persisted child lineage as a deterministic fallback.
            workflow_ids.extend(
                str(item) for item in source_state.get("authoring_child_workflow_ids") or [] if item
            )
            concurrency = source_state.get("full_proposal_concurrency") or {}
            workflow_ids.extend(str(item) for item in concurrency.get("child_workflow_ids") or [] if item)
            for item in (source_state.get("full_proposal_children") or {}).values():
                if isinstance(item, dict) and item.get("workflow_id"):
                    workflow_ids.append(str(item["workflow_id"]))

            cutoff = str(source.get("created_at") or review.get("created_at") or "")
            for prerequisite_type in (
                "WF-1_PROJECT_INTAKE",
                "WF-2_TEMPLATE_EXTRACTION",
                "WF-3_HYBRID_ONLINE_ASSIST",
            ):
                prerequisite = self.db.fetchone(
                    """SELECT id FROM workflows
                       WHERE project_id=? AND workflow_type=? AND status='COMPLETED' AND created_at<=?
                       ORDER BY updated_at DESC, created_at DESC LIMIT 1""",
                    (project_id, prerequisite_type, cutoff),
                )
                if prerequisite:
                    workflow_ids.append(str(prerequisite["id"]))

        return list(dict.fromkeys(item for item in workflow_ids if item))

    def open_active_lineage_blockers(
        self,
        project_id: str,
        *,
        review_workflow_id: str,
    ) -> list[dict[str, Any]]:
        lineage = set(self.active_lineage_workflow_ids(
            project_id, review_workflow_id=review_workflow_id
        ))
        return [
            item for item in self.open_blockers(project_id)
            if str(item.get("workflow_id") or "") in lineage
        ]

    def assert_no_active_lineage_blockers(
        self,
        project_id: str,
        *,
        review_workflow_id: str,
    ) -> None:
        blockers = self.open_active_lineage_blockers(
            project_id, review_workflow_id=review_workflow_id
        )
        if blockers:
            raise QualityGateBlocked(blockers)

    def quality_matrix(self, project_id: str, *, workflow_id: str | None = None) -> dict[str, Any]:
        findings = self.list_findings(project_id, workflow_id=workflow_id)
        by_state: dict[str, int] = {}
        by_severity: dict[str, int] = {}
        by_owner: dict[str, int] = {}
        for item in findings:
            state = str(item.get("lifecycle", {}).get("state") or "UNKNOWN")
            severity = str(item.get("finding", {}).get("severity") or "UNKNOWN")
            owner = str(item.get("responsibility", {}).get("owner") or "UNROUTED")
            by_state[state] = by_state.get(state, 0) + 1
            by_severity[severity] = by_severity.get(severity, 0) + 1
            by_owner[owner] = by_owner.get(owner, 0) + 1
        return {
            "project_id": project_id,
            "workflow_id": workflow_id,
            "total": len(findings),
            "open_blockers": len(self.open_blockers(project_id, workflow_id=workflow_id)),
            "by_state": by_state,
            "by_severity": by_severity,
            "by_owner": by_owner,
            "acceptance": "PASS" if not self.open_blockers(project_id, workflow_id=workflow_id) else "BLOCK",
        }

    # ---------- prompt/runtime integration ----------

    def observe_prompt_result(
        self,
        *,
        project_id: str,
        workflow_id: str | None,
        prompt_id: str,
        run_id: str,
        status: str,
        output: dict[str, Any],
        workflow_state: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        state = workflow_state or {}
        scope_key = self._scope_key(prompt_id, state)
        findings = [item for item in output.get("findings", []) if isinstance(item, dict)]
        current_codes = {str(item.get("code")) for item in findings if item.get("code")}

        # A successful producer/repair run may supply repair evidence, but never closes
        # the finding by itself.
        if status == "PASS":
            self._record_matching_repair_runs(
                project_id=project_id,
                workflow_id=workflow_id,
                prompt_id=prompt_id,
                run_id=run_id,
                scope_key=scope_key,
            )

        # A critic can verify only a prior repair run.  The review run must be later and
        # have a different run id; disappearance of a code without repair evidence is
        # deliberately insufficient.
        self._verify_absent_findings(
            project_id=project_id,
            workflow_id=workflow_id,
            reviewer_prompt_id=prompt_id,
            review_run_id=run_id,
            scope_key=scope_key,
            current_codes=current_codes,
        )

        records: list[dict[str, Any]] = []
        for finding in findings:
            severity = str(finding.get("severity") or "P3")
            if severity not in BLOCKING_SEVERITIES and not finding.get("blocking", False):
                continue
            records.append(self._open_or_refresh(
                project_id=project_id,
                workflow_id=workflow_id,
                prompt_id=prompt_id,
                run_id=run_id,
                finding=finding,
                scope_key=scope_key,
            ))
        return records

    def record_targeted_repair(
        self,
        *,
        project_id: str,
        workflow_id: str | None,
        repair_run_id: str,
        finding_codes: Iterable[str],
        workflow_state: dict[str, Any] | None = None,
    ) -> None:
        codes = {str(code) for code in finding_codes if code}
        if not codes:
            return
        for record in self.open_blockers(project_id, workflow_id=workflow_id):
            if record.get("finding", {}).get("code") not in codes:
                continue
            self._append_repair(record, prompt_id="P-TARGETED-REPAIR", run_id=repair_run_id)

    # ---------- post-export responsibility routing ----------

    def ingest_delivery_findings(
        self,
        *,
        project_id: str,
        workflow_id: str | None,
        validation_run_id: str,
        findings: Iterable[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        records = []
        for raw in findings:
            finding = dict(raw)
            finding.setdefault("severity", "P1")
            finding.setdefault("blocking", True)
            finding.setdefault("repairable", True)
            finding.setdefault("evidence_refs", [])
            finding.setdefault("target_path_or_span", None)
            route = self.route_delivery_finding(finding)
            finding.setdefault("suggested_route", "WRITING_AGENT" if route.owner_kind == "AGENT" else "BLOCK")
            records.append(self._open_or_refresh(
                project_id=project_id,
                workflow_id=workflow_id,
                prompt_id="DELIVERY_VALIDATOR",
                run_id=validation_run_id,
                finding=finding,
                scope_key=self._delivery_scope(finding),
                responsibility=route,
            ))
        return records

    def route_delivery_finding(self, finding: dict[str, Any]) -> ResponsibilityRoute:
        category = str(finding.get("category") or "SYSTEM")
        target_type = str(finding.get("target_type") or "").upper()
        code = str(finding.get("code") or "").upper()
        engineering = (
            category in {"FORMAT", "SYSTEM"}
            or any(token in target_type for token in {"DOCX", "PDF", "EXPORT", "RENDER", "LAYOUT"})
            or any(token in code for token in {"EXPORT", "RENDER", "DOCX", "PDF", "LAYOUT", "CLIP", "OVERLAP"})
        )
        if engineering:
            return ResponsibilityRoute(
                owner_kind="ENGINEERING",
                owner="EXPORT_ENGINEERING",
                workflow_type=None,
                stage_prompt_ids=("DELIVERY_VALIDATOR",),
                reviewer_prompt_id="DELIVERY_VALIDATOR",
                reason="导出、渲染、版式或文件结构缺陷属于工程责任，不允许通过改写正文掩盖。",
            )
        return ResponsibilityRoute(
            owner_kind="AGENT",
            owner="WRITING_AGENT",
            workflow_type="WF-4_PROPOSAL_AUTHORING",
            stage_prompt_ids=("P-WRITE-BLUEPRINT", "P-WRITE-CONTENT", "P-EXPRESSION-POLISH"),
            reviewer_prompt_id="P-INTEGRATION-CRITIC",
            reason="正文语义、章节内容或跨章一致性问题返回WF-4对应章节修复。",
        )

    # ---------- explicit evidence API ----------

    def add_repair_evidence(self, finding_id: str, *, project_id: str, prompt_id: str, run_id: str) -> dict[str, Any]:
        record = self._latest(project_id).get(finding_id)
        if not record:
            raise KeyError(finding_id)
        return self._append_repair(record, prompt_id=prompt_id, run_id=run_id)

    def verify_finding(
        self,
        finding_id: str,
        *,
        project_id: str,
        reviewer: str,
        review_run_id: str,
        review_hash: str,
    ) -> dict[str, Any]:
        record = self._latest(project_id).get(finding_id)
        if not record:
            raise KeyError(finding_id)
        repairs = list(record.get("lifecycle", {}).get("repair_evidence") or [])
        if not repairs:
            raise ValueError("P0/P1 finding cannot close without repair evidence")
        if not review_run_id or not review_hash:
            raise ValueError("independent review run id and hash are required")
        expected_reviewer = str(record.get("responsibility", {}).get("reviewer_prompt_id") or "")
        if expected_reviewer and reviewer != expected_reviewer:
            raise ValueError(f"finding must be reviewed by {expected_reviewer}, not {reviewer}")
        if any(str(item.get("run_id")) == review_run_id for item in repairs):
            raise ValueError("repair and independent review must use different runs")
        lifecycle = dict(record.get("lifecycle") or {})
        lifecycle["state"] = "VERIFIED"
        lifecycle["verified_at"] = utc_now()
        lifecycle.setdefault("review_evidence", []).append({
            "reviewer": reviewer,
            "run_id": review_run_id,
            "review_hash": review_hash,
            "recorded_at": utc_now(),
        })
        updated = dict(record)
        updated["lifecycle"] = lifecycle
        return self._persist(updated)

    # ---------- internal lifecycle helpers ----------

    def _open_or_refresh(
        self,
        *,
        project_id: str,
        workflow_id: str | None,
        prompt_id: str,
        run_id: str,
        finding: dict[str, Any],
        scope_key: str,
        responsibility: ResponsibilityRoute | None = None,
    ) -> dict[str, Any]:
        identity = self._identity(finding, scope_key)
        finding_id = "qf-" + identity[:20]
        latest = self._latest(project_id).get(finding_id)
        route = responsibility or self._route_prompt_finding(prompt_id, finding)
        if latest and latest.get("lifecycle", {}).get("state") not in CLOSED_STATES:
            lifecycle = dict(latest.get("lifecycle") or {})
            seen = list(lifecycle.get("observations") or [])
            if not any(item.get("run_id") == run_id for item in seen):
                seen.append({"prompt_id": prompt_id, "run_id": run_id, "recorded_at": utc_now()})
            lifecycle["observations"] = seen
            lifecycle["state"] = "OPEN"
            updated = dict(latest)
            # A finding that reappears in a replacement workflow belongs to that
            # candidate's active lineage.  Keeping the original workflow id would let
            # a current blocker hide behind an abandoned attempt during final export.
            updated["workflow_id"] = workflow_id
            updated["finding"] = finding
            updated["responsibility"] = route.as_dict()
            updated["lifecycle"] = lifecycle
            return self._persist(updated)

        lifecycle = {
            "state": "OPEN",
            "opened_at": utc_now(),
            "opened_by": {"prompt_id": prompt_id, "run_id": run_id},
            "observations": [{"prompt_id": prompt_id, "run_id": run_id, "recorded_at": utc_now()}],
            "repair_evidence": [],
            "review_evidence": [],
        }
        record = {
            "finding_id": finding_id,
            "identity_hash": identity,
            "project_id": project_id,
            "workflow_id": workflow_id,
            "scope_key": scope_key,
            "finding": finding,
            "responsibility": route.as_dict(),
            "lifecycle": lifecycle,
        }
        return self._persist(record)

    def _record_matching_repair_runs(
        self,
        *,
        project_id: str,
        workflow_id: str | None,
        prompt_id: str,
        run_id: str,
        scope_key: str,
    ) -> None:
        for record in self.open_blockers(project_id, workflow_id=workflow_id):
            route = record.get("responsibility") or {}
            if prompt_id not in set(route.get("stage_prompt_ids") or []):
                continue
            opened_run = str(record.get("lifecycle", {}).get("opened_by", {}).get("run_id") or "")
            if opened_run == run_id:
                continue
            if not self._scope_matches(str(record.get("scope_key") or "document"), scope_key):
                continue
            self._append_repair(record, prompt_id=prompt_id, run_id=run_id)

    def _verify_absent_findings(
        self,
        *,
        project_id: str,
        workflow_id: str | None,
        reviewer_prompt_id: str,
        review_run_id: str,
        scope_key: str,
        current_codes: set[str],
    ) -> None:
        for record in self.open_blockers(project_id, workflow_id=workflow_id):
            route = record.get("responsibility") or {}
            if route.get("reviewer_prompt_id") != reviewer_prompt_id:
                continue
            if record.get("finding", {}).get("code") in current_codes:
                continue
            if not self._scope_matches(str(record.get("scope_key") or "document"), scope_key):
                continue
            repairs = list(record.get("lifecycle", {}).get("repair_evidence") or [])
            if not repairs or any(str(item.get("run_id")) == review_run_id for item in repairs):
                continue
            self.verify_finding(
                str(record["finding_id"]),
                project_id=project_id,
                reviewer=reviewer_prompt_id,
                review_run_id=review_run_id,
                review_hash=sha256_json({"prompt_id": reviewer_prompt_id, "run_id": review_run_id, "absent_code": record.get("finding", {}).get("code")}),
            )

    def _append_repair(self, record: dict[str, Any], *, prompt_id: str, run_id: str) -> dict[str, Any]:
        lifecycle = dict(record.get("lifecycle") or {})
        repairs = list(lifecycle.get("repair_evidence") or [])
        if any(str(item.get("run_id")) == run_id for item in repairs):
            return record
        repairs.append({
            "prompt_id": prompt_id,
            "run_id": run_id,
            "repair_hash": sha256_json({"prompt_id": prompt_id, "run_id": run_id, "finding_id": record.get("finding_id")}),
            "recorded_at": utc_now(),
        })
        lifecycle["repair_evidence"] = repairs
        lifecycle["state"] = "REPAIR_RECORDED"
        updated = dict(record)
        updated["lifecycle"] = lifecycle
        return self._persist(updated)

    def _persist(self, record: dict[str, Any]) -> dict[str, Any]:
        project_id = str(record["project_id"])
        workflow_id = record.get("workflow_id")
        finding_id = str(record["finding_id"])
        latest = self._latest(project_id).get(finding_id)
        version = int((latest or {}).get("version", 0)) + 1
        payload = dict(record)
        payload["version"] = version
        payload["updated_at"] = utc_now()
        context_hash = sha256_json(payload)
        status = str(payload.get("lifecycle", {}).get("state") or "OPEN")
        self.db.execute(
            """INSERT INTO artifacts(id,project_id,workflow_id,artifact_type,prompt_id,version,status,security_level,context_hash,content_json,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                new_id("artifact"), project_id, workflow_id, self.artifact_type,
                str(payload.get("lifecycle", {}).get("opened_by", {}).get("prompt_id") or "QUALITY_LIFECYCLE"),
                version, status, self._project_level(project_id), context_hash,
                json.dumps(payload, ensure_ascii=False), utc_now(),
            ),
        )
        self.db.audit(
            "QUALITY_FINDING_LIFECYCLE",
            project_id=project_id,
            object_id=finding_id,
            metadata={"state": status, "version": version, "workflow_id": workflow_id},
        )
        return payload

    def _latest(self, project_id: str, *, workflow_id: str | None = None) -> dict[str, dict[str, Any]]:
        sql = "SELECT workflow_id,version,content_json FROM artifacts WHERE project_id=? AND artifact_type=?"
        params: list[Any] = [project_id, self.artifact_type]
        if workflow_id is not None:
            sql += " AND workflow_id=?"
            params.append(workflow_id)
        sql += " ORDER BY created_at ASC, version ASC"
        latest: dict[str, dict[str, Any]] = {}
        for row in self.db.fetchall(sql, tuple(params)):
            try:
                payload = json.loads(row["content_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            payload["version"] = int(row.get("version") or payload.get("version") or 0)
            finding_id = str(payload.get("finding_id") or "")
            if not finding_id:
                continue
            previous = latest.get(finding_id)
            if previous is None or int(payload["version"]) >= int(previous.get("version", 0)):
                latest[finding_id] = payload
        return latest

    def _route_prompt_finding(self, prompt_id: str, finding: dict[str, Any]) -> ResponsibilityRoute:
        suggested = str(finding.get("suggested_route") or "")
        producer = CRITIC_PRODUCER.get(prompt_id)
        reviewer = (
            prompt_id
            if prompt_id.endswith("-CRITIC") or prompt_id == "P-INTEGRATION-CRITIC"
            else self._critic_for_producer(prompt_id)
        )
        if suggested == "ORIGINAL_PRODUCER" and producer:
            stage_prompts = (producer,)
            owner = self._owner_for_prompt(producer)
        else:
            stage_prompts = ROUTE_STAGE_PROMPTS.get(suggested, (producer or prompt_id,))
            owner = suggested or self._owner_for_prompt(producer or prompt_id)
        workflow_type = self._workflow_for_prompt(stage_prompts[0] if stage_prompts else prompt_id)
        return ResponsibilityRoute(
            owner_kind="AGENT" if owner not in {"USER", "BLOCK"} else owner,
            owner=owner,
            workflow_type=workflow_type,
            stage_prompt_ids=tuple(stage_prompts),
            reviewer_prompt_id=reviewer,
            reason="问题返回最早能够修复其语义或结构责任的生产阶段，并由独立Critic复审。",
        )

    @staticmethod
    def _critic_for_producer(prompt_id: str) -> str | None:
        for critic, producer in CRITIC_PRODUCER.items():
            if producer == prompt_id:
                return critic
        return "P-INTEGRATION-CRITIC" if prompt_id in {"P-WRITE-CONTENT", "P-EXPRESSION-POLISH"} else None

    @staticmethod
    def _owner_for_prompt(prompt_id: str) -> str:
        if prompt_id.startswith("P-SECURITY") or prompt_id.startswith("P-SAFE") or prompt_id.startswith("P-FINAL-CONFIDENTIALITY"):
            return "SECURITY_REVIEW_AGENT"
        if "ARGUMENT-ARCHITECTURE" in prompt_id:
            return "ARGUMENT_ARCHITECTURE_AGENT"
        if "REVISION-PLAN" in prompt_id:
            return "PLANNING_AGENT"
        if "EXPRESSION" in prompt_id:
            return "EXPRESSION_EDITOR_AGENT"
        if "WRITE" in prompt_id:
            return "WRITING_AGENT"
        return "PROJECT_KNOWLEDGE_AGENT"

    @staticmethod
    def _workflow_for_prompt(prompt_id: str) -> str | None:
        if prompt_id in {"P-TEMPLATE-EXTRACT", "P-TEMPLATE-CRITIC"}:
            return "WF-2_TEMPLATE_EXTRACTION"
        if prompt_id.startswith("P-SAFE") or prompt_id.startswith("P-PUBLIC") or prompt_id.startswith("P-ONLINE"):
            return "WF-3_HYBRID_ONLINE_ASSIST"
        if any(token in prompt_id for token in {"ARGUMENT", "REVISION", "WRITE", "EXPRESSION", "INTEGRATION"}):
            return "WF-4_PROPOSAL_AUTHORING"
        if prompt_id.startswith("P-FINAL"):
            return "WF-5_SECURITY_REVIEW_AND_EXPORT"
        return "WF-1_PROJECT_INTAKE"

    @staticmethod
    def _scope_key(prompt_id: str, state: dict[str, Any]) -> str:
        section_id = str(state.get("active_section_id") or "").strip()
        if section_id:
            return f"section:{section_id}"
        if prompt_id == "P-INTEGRATION-CRITIC":
            return "document"
        return f"stage:{prompt_id.replace('-CRITIC', '')}"

    @staticmethod
    def _delivery_scope(finding: dict[str, Any]) -> str:
        target = str(finding.get("target_path_or_span") or "document")
        return f"delivery:{target}"

    @staticmethod
    def _scope_matches(left: str, right: str) -> bool:
        return left == right or left == "document" or right == "document"

    @staticmethod
    def _identity(finding: dict[str, Any], scope_key: str) -> str:
        return sha256_json({
            "code": finding.get("code"),
            "scope_key": scope_key,
            "target_type": finding.get("target_type"),
            "target_path_or_span": finding.get("target_path_or_span"),
            "suggested_route": finding.get("suggested_route"),
        })

    def _project_level(self, project_id: str) -> str:
        row = self.db.fetchone("SELECT security_level FROM projects WHERE id=?", (project_id,))
        return str(row.get("security_level") if row else "INTERNAL")
