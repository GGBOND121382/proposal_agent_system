from __future__ import annotations

import copy
from typing import Any

from .runtime_policy import LIVE_ENVELOPE_REGISTRY
from .util import sha256_json, utc_now


class G3CrossChapterReviewMixin:
    """Persisted three-chapter reviews used only by formal G3 runs."""

    def _g3_cross_chapter_enabled(self, state: dict[str, Any]) -> bool:
        options = state.get("options") or {}
        return bool(
            self._full_proposal_mode(state)
            and options.get("g3_formal_acceptance")
            and int(options.get("cross_chapter_batch_size") or 3) > 0
        )

    def _g3_cross_chapter_batches(self, state: dict[str, Any]) -> list[list[str]]:
        contract = state.get("full_proposal_contract") or {}
        ordered = [
            str(item.get("section_id"))
            for item in contract.get("sections") or []
            if isinstance(item, dict) and item.get("section_id")
        ]
        size = max(2, int((state.get("options") or {}).get("cross_chapter_batch_size") or 3))
        return [ordered[index : index + size] for index in range(0, len(ordered), size)]

    @staticmethod
    def _g3_batch_snapshot(envelope: dict[str, Any], section_ids: list[str]) -> dict[str, Any]:
        payload = envelope.get("payload") or {}
        candidates = {
            str(item.get("section_id")): item
            for item in payload.get("candidate_sections") or []
            if isinstance(item, dict) and item.get("section_id")
        }
        sections = []
        for section_id in section_ids:
            item = candidates.get(section_id) or {}
            candidate = item.get("candidate") or {}
            sections.append(
                {
                    "section_id": section_id,
                    "candidate_id": str(candidate.get("candidate_id") or ""),
                    "candidate_hash": sha256_json(candidate),
                }
            )
        core = {"section_ids": section_ids, "sections": sections}
        return {**core, "candidate_set_hash": sha256_json(core)}

    def _g3_batch_envelope(
        self,
        envelope: dict[str, Any],
        section_ids: list[str],
        batch_index: int,
    ) -> dict[str, Any]:
        subset = copy.deepcopy(envelope)
        payload = subset.get("payload") or {}
        wanted = set(section_ids)
        payload["candidate_sections"] = [
            item
            for item in payload.get("candidate_sections") or []
            if isinstance(item, dict) and str(item.get("section_id") or "") in wanted
        ]
        payload["document_section_map"] = [
            item
            for item in payload.get("document_section_map") or []
            if isinstance(item, dict) and str(item.get("section_id") or "") in wanted
        ]
        subset["task"]["current_step"] = f"G3_CROSS_CHAPTER_REVIEW_{batch_index}"
        subset["scope"]["target_object_ids"] = list(section_ids)
        errors = self.pack.validate("P-INTEGRATION-CRITIC", "input", subset)
        if errors:
            raise ValueError("G3 cross-chapter input validation failed: " + "; ".join(errors[:12]))
        LIVE_ENVELOPE_REGISTRY.register(subset)
        return subset

    async def _run_g3_cross_chapter_reviews(
        self,
        wf: dict[str, Any],
        state: dict[str, Any],
        full_envelope: dict[str, Any],
    ) -> str:
        if not self._g3_cross_chapter_enabled(state):
            return "NOT_REQUIRED"
        history = state.setdefault("g3_cross_chapter_review_history", [])
        batches = self._g3_cross_chapter_batches(state)
        if not batches:
            raise ValueError("G3 formal acceptance has no frozen sections for cross-chapter review")

        for batch_index, section_ids in enumerate(batches, 1):
            snapshot = self._g3_batch_snapshot(full_envelope, section_ids)
            passed = next(
                (
                    item
                    for item in reversed(history)
                    if int(item.get("batch_index") or 0) == batch_index
                    and item.get("status") == "PASS"
                    and item.get("candidate_set_hash") == snapshot["candidate_set_hash"]
                ),
                None,
            )
            if passed:
                continue
            envelope = self._g3_batch_envelope(full_envelope, section_ids, batch_index)
            result = await self.executor.execute(
                "P-INTEGRATION-CRITIC",
                envelope,
                project_id=wf["project_id"],
                workflow_id=wf["id"],
                original_environment=state.get("original_environment"),
            )
            state["original_environment"] = result["route"]["environment"]
            self._observe_quality_result(wf, state, "P-INTEGRATION-CRITIC", result)
            output = result.get("output") or {}
            record = {
                "batch_index": batch_index,
                "section_ids": list(section_ids),
                "candidate_set_hash": snapshot["candidate_set_hash"],
                "run_id": result["run_id"],
                "status": result["status"],
                "finding_codes": [
                    str(item.get("code") or "")
                    for item in output.get("findings") or []
                    if isinstance(item, dict)
                ],
                "reviewed_at": utc_now(),
            }
            history.append(record)
            state["g3_cross_chapter_review_count"] = len(batches)
            self._update(wf, state=state)
            if result["status"] == "PASS":
                continue
            if result["status"] == "REVISE":
                repair = self._prepare_integration_repair(wf, state, output)
                if repair == "SCHEDULED":
                    return "SCHEDULED"
            state["last_error"] = (
                f"G3 cross-chapter batch {batch_index} did not pass: {result['status']}"
            )
            self._update(wf, status="BLOCKED", state=state)
            return "BLOCKED"

        state["g3_cross_chapter_reviews"] = {
            "status": "PASS",
            "batch_size": max(2, int((state.get("options") or {}).get("cross_chapter_batch_size") or 3)),
            "batch_count": len(batches),
            "latest_pass_run_ids": [
                next(
                    item["run_id"]
                    for item in reversed(history)
                    if int(item.get("batch_index") or 0) == index
                    and item.get("status") == "PASS"
                    and item.get("candidate_set_hash")
                    == self._g3_batch_snapshot(full_envelope, section_ids)["candidate_set_hash"]
                )
                for index, section_ids in enumerate(batches, 1)
            ],
            "completed_at": utc_now(),
        }
        self._update(wf, state=state)
        return "PASS"
