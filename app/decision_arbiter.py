from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any, Mapping

from .contracts import get_semantic_contract
from .util import new_id, sha256_json, utc_now


_BLOCKING = {"BLOCK", "REVISE", "NEED_USER_INPUT"}
_RESPONSIBILITY_PROTOCOL_VERSION = "1.0"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decode(value: str) -> Any:
    return json.loads(value)


@dataclass(frozen=True)
class DecisionRecord:
    """Immutable snapshot of the two decision sources and their ownership protocol."""

    record_id: str
    prompt_id: str
    critic_status: str
    guard_status: str
    decision: str
    contract_conflict: bool
    contract_version: str
    contract_rule_registry_version: str
    contract_hash: str
    responsibility_protocol_version: str
    critic_responsibility: str
    guard_responsibility: str
    raw_critic_output_json: str
    guard_report_json: str
    decision_basis_json: str
    created_at: str

    @property
    def raw_critic_output(self) -> dict[str, Any]:
        value = _decode(self.raw_critic_output_json)
        return value if isinstance(value, dict) else {}

    @property
    def guard_report(self) -> dict[str, Any]:
        value = _decode(self.guard_report_json)
        return value if isinstance(value, dict) else {}

    @property
    def decision_basis(self) -> dict[str, Any]:
        value = _decode(self.decision_basis_json)
        return value if isinstance(value, dict) else {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "2.0",
            "record_id": self.record_id,
            "prompt_id": self.prompt_id,
            "critic_status": self.critic_status,
            "guard_status": self.guard_status,
            "decision": self.decision,
            "contract_conflict": self.contract_conflict,
            "contract": {
                "version": self.contract_version,
                "rule_registry_version": self.contract_rule_registry_version,
                "sha256": self.contract_hash,
            },
            "responsibility_protocol": {
                "version": self.responsibility_protocol_version,
                "critic": self.critic_responsibility,
                "guard": self.guard_responsibility,
            },
            "raw_critic_output": self.raw_critic_output,
            "guard_report": self.guard_report,
            "decision_basis": self.decision_basis,
            "created_at": self.created_at,
        }


class DecisionArbiter:
    """Record and arbitrate independent model and deterministic observations.

    Source objects are serialized at record creation, so later caller mutation
    cannot alter audit history.  Arbitration may annotate why a source finding
    was or was not actionable, but never removes or rewrites that finding in the
    preserved source snapshots.
    """

    @staticmethod
    def _guard_status(guard_report: Mapping[str, Any]) -> str:
        explicit = str(guard_report.get("status") or "").upper()
        if explicit in {"PASS", "REVISE", "BLOCK"}:
            return explicit
        findings = [item for item in guard_report.get("findings") or [] if isinstance(item, Mapping)]
        return "REVISE" if any(bool(item.get("blocking", True)) for item in findings) else "PASS"

    @staticmethod
    def _coerce_guard_report(value: Mapping[str, Any] | list[dict[str, Any]] | None) -> dict[str, Any]:
        contract = get_semantic_contract()
        if isinstance(value, Mapping):
            return dict(value)
        findings = [dict(item) for item in (value or []) if isinstance(item, Mapping)]
        return {
            "schema_version": "0.legacy",
            "status": "REVISE" if any(bool(item.get("blocking", True)) for item in findings) else "PASS",
            "responsibility": "DETERMINISTIC_GUARD",
            "contract_version": contract.version,
            "contract_rule_registry_version": contract.rule_registry_version,
            "contract_hash": contract.contract_hash,
            "findings": findings,
        }

    @staticmethod
    def _is_deterministic_critic_finding(finding: Mapping[str, Any]) -> bool:
        contract = get_semantic_contract()
        responsibility = str(finding.get("responsibility") or finding.get("source") or "").upper()
        if responsibility in {"DETERMINISTIC_GUARD", "GUARD", "OUTPUT_INTEGRITY"}:
            return True
        rule_id = str(finding.get("rule_id") or "")
        if rule_id in contract.rule_ids:
            return True
        return str(finding.get("code") or "").upper().startswith("QG_")

    @staticmethod
    def _blocking_findings(value: Mapping[str, Any]) -> list[dict[str, Any]]:
        return [
            dict(item)
            for item in value.get("findings") or []
            if isinstance(item, Mapping) and bool(item.get("blocking", True))
        ]

    @staticmethod
    def _protocol_errors(guard_report: Mapping[str, Any]) -> list[str]:
        contract = get_semantic_contract()
        errors: list[str] = []
        expected = {
            "contract_version": contract.version,
            "contract_rule_registry_version": contract.rule_registry_version,
            "contract_hash": contract.contract_hash,
        }
        for field, value in expected.items():
            observed = str(guard_report.get(field) or "")
            if observed != str(value):
                errors.append(f"{field}: expected {value}, observed {observed or '<missing>'}")
        responsibility = str(guard_report.get("responsibility") or "").upper()
        if responsibility != "DETERMINISTIC_GUARD":
            errors.append(
                "guard responsibility: expected DETERMINISTIC_GUARD, "
                f"observed {responsibility or '<missing>'}"
            )
        for finding in guard_report.get("findings") or []:
            if not isinstance(finding, Mapping):
                errors.append("guard findings must be objects")
                continue
            rule_id = str(finding.get("rule_id") or "")
            if rule_id in contract.rule_ids:
                expected_owner = contract.rule(rule_id).responsibility.value
                observed_owner = str(finding.get("responsibility") or responsibility).upper()
                if observed_owner != expected_owner:
                    errors.append(
                        f"{rule_id} responsibility: expected {expected_owner}, observed {observed_owner}"
                    )
        return errors

    @staticmethod
    def _action_entry(source: str, finding: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "source": source,
            "finding": json.loads(_canonical_json(dict(finding))),
        }

    def arbitrate(
        self,
        critic_output: dict[str, Any],
        guard_report: Mapping[str, Any] | list[dict[str, Any]] | None = None,
        *,
        prompt_id: str = "UNKNOWN",
    ) -> DecisionRecord:
        critic_snapshot = json.loads(_canonical_json(critic_output))
        guard_snapshot = json.loads(_canonical_json(self._coerce_guard_report(guard_report)))
        critic_status = str(critic_snapshot.get("status") or "PASS").upper()
        guard_status = self._guard_status(guard_snapshot)

        protocol_errors = self._protocol_errors(guard_snapshot)
        critic_blocking = self._blocking_findings(critic_snapshot)
        ignored_critic = [
            item for item in critic_blocking if self._is_deterministic_critic_finding(item)
        ]
        owned_critic = [
            item for item in critic_blocking if not self._is_deterministic_critic_finding(item)
        ]
        guard_blocking = self._blocking_findings(guard_snapshot)
        blocking_questions = [
            dict(item)
            for item in critic_snapshot.get("user_questions") or []
            if isinstance(item, Mapping) and bool(item.get("blocking", True))
        ]

        conflict = bool(protocol_errors)
        if conflict:
            decision = "CONTRACT_CONFLICT"
        elif guard_blocking:
            decision = "BLOCK" if guard_status == "BLOCK" else "REVISE"
        elif critic_status == "NEED_USER_INPUT" or blocking_questions:
            decision = "WAITING_HUMAN_INPUT"
        elif owned_critic:
            decision = "BLOCK" if critic_status == "BLOCK" else "REVISE"
        elif critic_status == "BLOCK":
            # A hard block without an owned Finding cannot be silently promoted.
            # Keep the block while recording that its rationale is unsupported.
            decision = "BLOCK"
        else:
            decision = "PASS"

        actionable = [
            *[self._action_entry("QUALITATIVE_LLM_CRITIC", item) for item in owned_critic],
            *[self._action_entry("DETERMINISTIC_GUARD", item) for item in guard_blocking],
        ]
        basis = {
            "schema_version": "2.0",
            "protocol_errors": protocol_errors,
            "critic_owned_blocking_findings": [dict(item) for item in owned_critic],
            "guard_owned_blocking_findings": [dict(item) for item in guard_blocking],
            "ignored_critic_findings": [
                {
                    "reason": "OUTSIDE_QUALITATIVE_CRITIC_RESPONSIBILITY",
                    "finding": dict(item),
                }
                for item in ignored_critic
            ],
            "blocking_user_questions": blocking_questions,
            "critic_status_without_owned_blocker": (
                critic_status in {"REVISE", "BLOCK"} and not owned_critic
            ),
            "actionable_findings": actionable,
        }
        contract = get_semantic_contract()
        return DecisionRecord(
            record_id=new_id("decision"),
            prompt_id=prompt_id,
            critic_status=critic_status,
            guard_status=guard_status,
            decision=decision,
            contract_conflict=conflict,
            contract_version=contract.version,
            contract_rule_registry_version=contract.rule_registry_version,
            contract_hash=contract.contract_hash,
            responsibility_protocol_version=_RESPONSIBILITY_PROTOCOL_VERSION,
            critic_responsibility="QUALITATIVE_LLM_CRITIC",
            guard_responsibility=str(guard_snapshot.get("responsibility") or "DETERMINISTIC_GUARD"),
            raw_critic_output_json=_canonical_json(critic_snapshot),
            guard_report_json=_canonical_json(guard_snapshot),
            decision_basis_json=_canonical_json(basis),
            created_at=utc_now(),
        )

    @staticmethod
    def persist(
        db: Any,
        *,
        project_id: str,
        workflow_id: str,
        prompt_id: str,
        record: DecisionRecord,
        security_level: str,
        workflow_state: dict[str, Any],
        workflow_status: str,
        current_step: int,
    ) -> str:
        """Atomically persist the record, workflow index/state, and audit event."""

        payload = record.to_dict()
        artifact_id = new_id("artifact")
        next_state = copy.deepcopy(workflow_state)
        next_state.setdefault("decision_record_ids", []).append(artifact_id)
        del next_state["decision_record_ids"][:-100]

        with db.transaction() as tx:
            version = tx.next_artifact_version(
                project_id=project_id,
                workflow_id=workflow_id,
                artifact_type="DECISION_RECORD",
                prompt_id=prompt_id,
            )
            tx.execute(
                """INSERT INTO artifacts(id,project_id,workflow_id,artifact_type,prompt_id,version,status,security_level,context_hash,content_json,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    artifact_id,
                    project_id,
                    workflow_id,
                    "DECISION_RECORD",
                    prompt_id,
                    version,
                    record.decision,
                    security_level,
                    sha256_json(payload),
                    json.dumps(payload, ensure_ascii=False),
                    record.created_at,
                ),
            )
            tx.update_workflow(
                workflow_id=workflow_id,
                status=workflow_status,
                current_step=current_step,
                state=next_state,
            )
            tx.audit(
                "DECISION_RECORDED",
                project_id=project_id,
                object_id=artifact_id,
                metadata={
                    "workflow_id": workflow_id,
                    "prompt_id": prompt_id,
                    "version": version,
                    "decision": record.decision,
                    "record_id": record.record_id,
                    "contract_hash": record.contract_hash,
                },
            )

        workflow_state.clear()
        workflow_state.update(next_state)
        return artifact_id

