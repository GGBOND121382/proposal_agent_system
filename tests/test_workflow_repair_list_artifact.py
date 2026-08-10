from __future__ import annotations

import asyncio
import copy
import json
from typing import Any

from app.db import Database
from app.executor import PromptExecutionError
from app.util import sha256_json, utc_now
from app.workflow_repair import WorkflowRepairMixin


FACTS = [
    {
        "claim_id": "METRIC-PROJ-001",
        "claim_text": "项目拟形成一套评价指标。",
        "claim_type": "FACT",
        "knowledge_status": "DOCUMENT_EXTRACTED",
        "temporal_status": "PLANNED",
    },
    {
        "claim_id": "FACT-PROJ-002",
        "claim_text": "材料来自项目设计输入。",
        "claim_type": "FACT",
        "knowledge_status": "DOCUMENT_EXTRACTED",
        "temporal_status": "CURRENT",
    },
]


class ListResultContext:
    def __init__(self) -> None:
        self.envelopes: list[dict[str, Any]] = []

    def _result(self, project_id: str, prompt_id: str, key: str | None = None) -> Any:
        assert project_id == "project-1"
        assert prompt_id == "P-FACT-EXTRACT"
        assert key == "fact_candidates"
        return copy.deepcopy(FACTS)

    def build(self, prompt_id: str, project_id: str, **kwargs: Any) -> dict[str, Any]:
        assert prompt_id == "P-TARGETED-REPAIR"
        envelope = {
            "prompt_id": prompt_id,
            "payload": {},
            "overrides": copy.deepcopy(kwargs.get("overrides") or {}),
        }
        self.envelopes.append(copy.deepcopy(envelope))
        return envelope




class MigratedRepairContext(ListResultContext):
    def __init__(self) -> None:
        super().__init__()
        self.workflow_ids: list[str | None] = []

    def _repair_override(
        self,
        state: dict[str, Any],
        producer_prompt: str,
        *,
        workflow_id: str | None,
    ) -> Any:
        assert producer_prompt == "P-FACT-EXTRACT"
        self.workflow_ids.append(workflow_id)
        return copy.deepcopy(FACTS)

    def _result(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("explicit REPAIR_APPLICATION must be used before producer fallback")

class ListRepairExecutor:
    async def execute(self, prompt_id: str, envelope: dict[str, Any], **_: Any) -> dict[str, Any]:
        assert prompt_id == "P-TARGETED-REPAIR"
        content = copy.deepcopy(
            envelope["overrides"]["payload.original_object"]["content"]
        )
        content["fact_candidates"][0]["claim_type"] = "EXPECTED_RESULT"
        return {
            "run_id": "run-repair-facts-1",
            "status": "PASS",
            "route": {"environment": "OFFLINE_LOCAL"},
            "output": {
                "result": {"repaired_object": content},
            },
        }


class ContractRetryExecutor(ListRepairExecutor):
    def __init__(self) -> None:
        self.attempts = 0

    async def execute(
        self, prompt_id: str, envelope: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        self.attempts += 1
        if self.attempts == 1:
            raise PromptExecutionError(
                "Output schema validation failed",
                validation_errors=[
                    "/result: 'unresolved_finding_ids' is a required property"
                ],
                run_id="run-contract-error-1",
            )
        result = await super().execute(prompt_id, envelope, **kwargs)
        finding_ids = [
            str(item["finding_instance_id"])
            for item in envelope["overrides"]["payload.findings_to_repair"]
        ]
        result["output"]["result"].update(
            {
                "resolved_finding_ids": finding_ids,
                "unresolved_finding_ids": [],
            }
        )
        return result


class ExhaustedContractRetryExecutor:
    def __init__(self) -> None:
        self.attempts = 0

    async def execute(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        self.attempts += 1
        raise PromptExecutionError(
            "Output schema validation failed",
            validation_errors=[
                "/result: 'unresolved_finding_ids' is a required property"
            ],
            run_id=f"run-contract-error-{self.attempts}",
        )


class TwoContractFailuresThenPassExecutor(ContractRetryExecutor):
    async def execute(
        self, prompt_id: str, envelope: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        self.attempts += 1
        if self.attempts <= 2:
            raise PromptExecutionError(
                "Output schema validation failed after malformed JSON regeneration",
                run_id=f"run-malformed-{self.attempts}",
            )
        result = await ListRepairExecutor.execute(
            self, prompt_id, envelope, **kwargs
        )
        finding_ids = [
            str(item["finding_instance_id"])
            for item in envelope["overrides"]["payload.findings_to_repair"]
        ]
        result["output"]["result"].update({
            "resolved_finding_ids": finding_ids,
            "unresolved_finding_ids": [],
        })
        return result


class RecordingQualityManager:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def record_targeted_repair(self, **kwargs: Any) -> None:
        self.calls.append(copy.deepcopy(kwargs))


class RepairHarness(WorkflowRepairMixin):
    def __init__(self, tmp_path) -> None:
        self.context_builder = ListResultContext()
        self.executor = ListRepairExecutor()
        self.quality_manager = RecordingQualityManager()
        self.provider_retry_calls: list[dict[str, Any]] = []
        self.db = Database(tmp_path / "runtime.sqlite3")
        now = utc_now()
        self.db.execute(
            "INSERT INTO projects(id,name,description,security_level,config_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            ("project-1", "test", "test", "INTERNAL", "{}", now, now),
        )
        self.db.execute(
            "INSERT INTO workflows(id,project_id,workflow_type,status,current_step,state_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            ("wf-1", "project-1", "WF-TEST", "RUNNING", 0, "{}", now, now),
        )

    def workflow(self) -> dict[str, Any]:
        row = self.db.fetchone("SELECT * FROM workflows WHERE id='wf-1'")
        row["state"] = json.loads(row.pop("state_json"))
        return row

    @staticmethod
    def _project_level(project_id: str) -> str:
        assert project_id == "project-1"
        return "INTERNAL"

    @staticmethod
    def _update(wf: dict[str, Any], **updates: Any) -> None:
        wf.update(updates)

    async def _execute_prompt_with_provider_retry(
        self,
        wf: dict[str, Any],
        state: dict[str, Any],
        *,
        prompt_id: str,
        envelope: dict[str, Any],
        call_key: str | None = None,
        retry_categories=None,
    ) -> dict[str, Any]:
        self.provider_retry_calls.append({
            "prompt_id": prompt_id,
            "call_key": call_key,
            "retry_categories": retry_categories,
        })
        return await self.executor.execute(
            prompt_id,
            envelope,
            project_id=wf["project_id"],
            workflow_id=wf["id"],
            original_environment=state.get("original_environment"),
            call_key=call_key,
        )


def test_targeted_repair_inherits_original_producer_semantic_namespace(tmp_path) -> None:
    harness = RepairHarness(tmp_path)
    now = utc_now()
    producer_input = {
        "security_context": {"input_max_security_level": "INTERNAL"},
        "payload": {
            "argument_nodes": [
                {
                    "node_id": "RC-002",
                    "node_type": "WORK_PACKAGE",
                    "security_level": "INTERNAL",
                }
            ]
        },
    }
    harness.db.execute(
        """INSERT INTO prompt_runs(
               id,project_id,workflow_id,prompt_id,status,model_id,endpoint_id,
               input_hash,output_hash,input_json,output_json,error,duration_ms,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "run-content-source-1",
            "project-1",
            "wf-1",
            "P-WRITE-CONTENT",
            "PASS",
            "model-1",
            "endpoint-1",
            "a" * 64,
            "b" * 64,
            json.dumps(producer_input),
            json.dumps({"result": {"candidate_id": "candidate-1"}}),
            None,
            1,
            now,
        ),
    )
    state = {
        "active_section_id": "section-1",
        "section_progress": {
            "section-1": {
                "runs": [
                    {
                        "prompt_id": "P-WRITE-CONTENT",
                        "run_id": "run-content-source-1",
                        "status": "PASS",
                    }
                ]
            }
        },
    }

    catalog = harness._inherited_producer_source_catalog(
        harness.workflow(), state, "P-WRITE-CONTENT"
    )

    assert "RC-002" in {entry["source_id"] for entry in catalog}
    assert all(not entry["source_id"].startswith("input-") for entry in catalog)


def test_repair_application_artifact_replaces_list_shaped_state_override(tmp_path) -> None:
    harness = RepairHarness(tmp_path)
    wf = harness.workflow()
    state = {
        "repair_attempts": {},
        "original_environment": "OFFLINE_LOCAL",
    }
    critic_output = {
        "status": "REVISE",
        "findings": [
            {
                "code": "FACT_CRITIC_STATUS_UPGRADE",
                "repairable": True,
                "target_path_or_span": "METRIC-PROJ-001.claim_type",
                "repair_instruction": "将该候选的claim_type改为EXPECTED_RESULT。",
            }
        ],
    }

    repaired = asyncio.run(
        harness._auto_repair(
            wf,
            "P-FACT-CRITIC",
            {},
            critic_output,
            state,
        )
    )

    assert repaired is not None
    from app.runtime_failures import FailureCategory

    assert harness.provider_retry_calls[0]["retry_categories"] == frozenset({
        FailureCategory.PROVIDER_TRANSIENT
    })
    assert harness.provider_retry_calls[0]["call_key"].startswith("call-repair-")
    envelope = harness.context_builder.envelopes[0]
    original_object = envelope["overrides"]["payload.original_object"]
    assert original_object["content"] == {"fact_candidates": FACTS}
    assert original_object["object_hash"] == sha256_json({"fact_candidates": FACTS})
    assert envelope["overrides"]["payload.allowed_paths"] == [
        "/content/fact_candidates/0/claim_type"
    ]
    artifact_id = repaired["repair_application_artifact_id"]
    assert state["repair_application_artifact_ids"]["P-FACT-EXTRACT"] == [artifact_id]
    assert "repair_overrides" not in state
    artifact = harness.db.fetchone(
        "SELECT version,status,content_json FROM artifacts WHERE id=?", (artifact_id,)
    )
    assert artifact["version"] == 1
    assert artifact["status"] == "PASS"
    payload = json.loads(artifact["content_json"])
    override = payload["repaired_value"]
    assert isinstance(override, list)
    assert override[0]["claim_type"] == "EXPECTED_RESULT"
    assert override[1] == FACTS[1]
    assert payload["application_status"] == "APPLIED"
    assert payload["target_key"] == "P-FACT-EXTRACT"
    assert state.get("repair_attempts", {}).get("P-FACT-CRITIC", 0) == 0
    assert [
        item["event"]
        for item in state["repair_ledger_v1"]["events"]
    ][-5:] == [
        "CREATED",
        "MODEL_RETURNED",
        "SCHEMA_VALIDATED",
        "DIFF_VALIDATED",
        "APPLIED",
    ]
    assert state["repair_shape_adaptations"][0]["collection_key"] == "fact_candidates"
    assert len(harness.quality_manager.calls) == 1
    assert harness.db.fetchone(
        "SELECT id FROM audit_events WHERE event_type='REPAIR_APPLICATION_APPLIED' AND object_id=?",
        (artifact_id,),
    ) is not None


def test_list_repair_rejects_missing_collection_wrapper() -> None:
    restored, value = RepairHarness._restore_repaired_shape(
        {"wrong_key": copy.deepcopy(FACTS)},
        "fact_candidates",
    )
    assert restored is False
    assert value is None


def test_contract_failure_regenerates_complete_repair_without_semantic_budget(tmp_path) -> None:
    harness = RepairHarness(tmp_path)
    harness.executor = ContractRetryExecutor()
    wf = harness.workflow()
    state = {
        "repair_attempts": {},
        "original_environment": "OFFLINE_LOCAL",
        "options": {"targeted_repair_contract_retry_limit": 1},
    }
    critic_output = {
        "status": "REVISE",
        "findings": [
            {
                "code": "FACT_CRITIC_STATUS_UPGRADE",
                "repairable": True,
                "target_path_or_span": "METRIC-PROJ-001.claim_type",
                "repair_instruction": "change the first claim type",
            },
            {
                "code": "FACT_CRITIC_STATUS_UPGRADE",
                "repairable": True,
                "target_path_or_span": "FACT-PROJ-002.claim_type",
                "repair_instruction": "change the second claim type",
            },
        ],
    }

    repaired = asyncio.run(
        harness._auto_repair(
            wf, "P-FACT-CRITIC", {}, critic_output, state
        )
    )

    assert repaired is not None
    assert harness.executor.attempts == 2
    assert len(harness.context_builder.envelopes) == 2
    first_findings = harness.context_builder.envelopes[0]["overrides"][
        "payload.findings_to_repair"
    ]
    assert first_findings[0]["code"] == first_findings[1]["code"]
    assert first_findings[0]["finding_instance_id"] != first_findings[1][
        "finding_instance_id"
    ]
    feedback = harness.context_builder.envelopes[1]["overrides"][
        "payload.contract_feedback"
    ]
    assert feedback["attempt"] == 2
    assert "unresolved_finding_ids" in feedback["validation_errors"][0]
    ledger = state["repair_ledger_v1"]
    assert sum(ledger["technical_retries"].values()) == 1
    assert sum(ledger["semantic_repairs"].values()) == 0
    events = [item["event"] for item in ledger["events"]]
    assert "CONTRACT_REJECTED" in events
    assert "TECHNICAL_RETRY" in events
    assert "CONTRACT_RECOVERED" in events
    assert "REREVIEW_STARTED" not in events
    assert "last_targeted_repair_failure" not in state


def test_contract_retry_exhaustion_preserves_exact_failure_and_semantic_budget(tmp_path) -> None:
    harness = RepairHarness(tmp_path)
    harness.executor = ExhaustedContractRetryExecutor()
    wf = harness.workflow()
    state = {
        "repair_attempts": {},
        "original_environment": "OFFLINE_LOCAL",
        "options": {"targeted_repair_contract_retry_limit": 1},
    }
    critic_output = {
        "status": "REVISE",
        "findings": [
            {
                "code": "FACT_CRITIC_STATUS_UPGRADE",
                "repairable": True,
                "target_path_or_span": "METRIC-PROJ-001.claim_type",
                "repair_instruction": "change the claim type",
            }
        ],
    }

    repaired = asyncio.run(
        harness._auto_repair(
            wf, "P-FACT-CRITIC", {}, critic_output, state
        )
    )

    assert repaired is None
    assert harness.executor.attempts == 2
    failure = state["last_targeted_repair_failure"]
    assert failure["category"] == "OUTPUT_CONTRACT_ERROR"
    assert failure["run_id"] == "run-contract-error-2"
    assert failure["technical_retries_used"] == 1
    assert failure["consumes_semantic_repair_budget"] is False
    ledger = state["repair_ledger_v1"]
    assert sum(ledger["technical_retries"].values()) == 1
    assert sum(ledger["semantic_repairs"].values()) == 0
    assert [item["event"] for item in ledger["events"]].count(
        "CONTRACT_REJECTED"
    ) == 2
    message = harness._targeted_repair_failure_message(
        state,
        prompt_id="P-FACT-CRITIC",
        fallback="fallback",
    )
    assert "unresolved_finding_ids" in message
    assert "run-contract-error-2" in message


def test_default_contract_budget_matches_shared_two_retry_policy(tmp_path) -> None:
    harness = RepairHarness(tmp_path)
    harness.executor = TwoContractFailuresThenPassExecutor()
    wf = harness.workflow()
    state = {
        "repair_attempts": {},
        "original_environment": "OFFLINE_LOCAL",
        "options": {},
    }
    critic_output = {
        "status": "REVISE",
        "findings": [{
            "code": "FACT_CRITIC_STATUS_UPGRADE",
            "repairable": True,
            "target_path_or_span": "METRIC-PROJ-001.claim_type",
            "repair_instruction": "change the claim type",
        }],
    }

    repaired = asyncio.run(
        harness._auto_repair(
            wf, "P-FACT-CRITIC", {}, critic_output, state
        )
    )

    assert repaired is not None
    assert harness.executor.attempts == 3
    assert sum(
        state["repair_ledger_v1"]["technical_retries"].values()
    ) == 2
    assert sum(
        state["repair_ledger_v1"]["semantic_repairs"].values()
    ) == 0


def test_list_repair_accepts_plain_content_wrapper() -> None:
    restored, value = RepairHarness._restore_repaired_shape(
        {"content": {"fact_candidates": copy.deepcopy(FACTS)}},
        "fact_candidates",
    )
    assert restored is True
    assert value == FACTS


def test_generic_result_target_allows_whole_wrapped_collection() -> None:
    content = {"fact_candidates": copy.deepcopy(FACTS)}
    assert RepairHarness._canonical_repair_path(
        "result",
        content=content,
        collection_key="fact_candidates",
    ) == "/content/fact_candidates"


def test_candidate_wrapper_is_removed_from_producer_repair_path() -> None:
    content = {
        "argument_architecture": {"nodes": []},
        "research_design_matrix": [],
    }
    assert RepairHarness._canonical_repair_path(
        "architecture_candidate.research_design_matrix[0].method_ids",
        content=content,
        collection_key=None,
    ) == "/content/research_design_matrix/0/method_ids"


def test_semantic_locator_cannot_authorize_a_nonexistent_content_field() -> None:
    import pytest

    content = {
        "section_contract_id": "SC-001",
        "paragraphs": [{"paragraph_id": "P-ABS-001", "function": "定位"}],
    }
    with pytest.raises(ValueError, match="existing repair-object field"):
        RepairHarness._canonical_repair_path(
            "SC-001",
            content=content,
            collection_key=None,
        )


def test_guard_paragraph_locator_resolves_to_exact_existing_field() -> None:
    content = {
        "paragraphs": [
            {
                "paragraph_id": "P-ABS-001",
                "required_evidence_ids": ["FACT-001"],
            },
            {
                "paragraph_id": "P-ABS-002",
                "required_evidence_ids": ["FACT-002"],
            },
        ]
    }

    assert RepairHarness._canonical_repair_path(
        "paragraphs[P-ABS-002].required_evidence_ids",
        content=content,
        collection_key=None,
    ) == "/content/paragraphs/1/required_evidence_ids"
    assert RepairHarness._canonical_repair_path(
        "paragraphs",
        content=content,
        collection_key=None,
    ) == "/content/paragraphs"


def test_collection_locators_resolve_to_concrete_json_pointer_indexes() -> None:
    content = {"fact_candidates": copy.deepcopy(FACTS)}
    assert RepairHarness._canonical_repair_path(
        "/fact_candidates/0/claim_type",
        content=content,
        collection_key="fact_candidates",
    ) == "/content/fact_candidates/0/claim_type"
    assert RepairHarness._canonical_repair_path(
        "result.fact_candidates.METRIC-PROJ-001.claim_type",
        content=content,
        collection_key="fact_candidates",
    ) == "/content/fact_candidates/0/claim_type"


def test_targeted_repair_schema_accepts_every_registered_producer_role() -> None:
    import json
    from pathlib import Path

    from app.workflow_repair import PRODUCER_ROLE

    root = Path(__file__).resolve().parents[1]
    schema = json.loads(
        (root / "prompt_pack/schemas/prompts/targeted_repair_input.schema.json").read_text(
            encoding="utf-8"
        )
    )
    allowed = set(
        schema["properties"]["payload"]["properties"]["original_producer"]["enum"]
    )
    assert set(PRODUCER_ROLE.values()) <= allowed


def test_auto_repair_reads_migrated_application_with_explicit_workflow_id(tmp_path) -> None:
    harness = RepairHarness(tmp_path)
    context = MigratedRepairContext()
    harness.context_builder = context
    wf = harness.workflow()
    state = {
        "repair_attempts": {},
        "original_environment": "OFFLINE_LOCAL",
        "repair_application_artifact_ids": {"P-FACT-EXTRACT": ["migrated-repair"]},
    }
    critic_output = {
        "status": "REVISE",
        "findings": [
            {
                "code": "FACT_CRITIC_STATUS_UPGRADE",
                "repairable": True,
                "target_path_or_span": "METRIC-PROJ-001.claim_type",
                "repair_instruction": "将该候选的claim_type改为EXPECTED_RESULT。",
            }
        ],
    }

    repaired = asyncio.run(
        harness._auto_repair(wf, "P-FACT-CRITIC", {}, critic_output, state)
    )

    assert repaired is not None
    assert context.workflow_ids == ["wf-1"]
    assert len(context.envelopes) == 1


class ContractMigrationPassExecutor(ListRepairExecutor):
    async def execute(
        self, prompt_id: str, envelope: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        result = await super().execute(prompt_id, envelope, **kwargs)
        finding_ids = [
            str(item["finding_instance_id"])
            for item in envelope["overrides"]["payload.findings_to_repair"]
        ]
        result["run_id"] = "run-contract-migration-pass"
        result["output"]["result"].update({
            "resolved_finding_ids": finding_ids,
            "unresolved_finding_ids": [],
        })
        return result


def test_targeted_repair_contract_migration_reuses_exact_repair_checkpoint(
    tmp_path,
) -> None:
    harness = RepairHarness(tmp_path)
    harness.executor = ExhaustedContractRetryExecutor()
    wf = harness.workflow()
    state = {
        "repair_attempts": {},
        "original_environment": "OFFLINE_LOCAL",
        "options": {"targeted_repair_contract_retry_limit": 1},
        "active_section_id": "section-objective",
    }
    critic_output = {
        "status": "REVISE",
        "findings": [{
            "code": "FACT_CRITIC_STATUS_UPGRADE",
            "repairable": True,
            "target_path_or_span": "METRIC-PROJ-001.claim_type",
            "repair_instruction": "change the claim type",
        }],
    }

    first = asyncio.run(
        harness._auto_repair(wf, "P-FACT-CRITIC", {}, critic_output, state)
    )
    assert first is None
    failed = copy.deepcopy(state["last_targeted_repair_failure"])
    failed_call_key = failed["call_key"]
    created_before = [
        item for item in state["repair_ledger_v1"]["events"]
        if item["event"] == "CREATED"
    ]
    state["contract_migration_recovery"] = {
        "checkpoint_identity_version": 1,
        "step": 0,
        "section_id": "section-objective",
        "section_phase": None,
        "prompt_id": "P-TARGETED-REPAIR",
        "failed_run_id": failed["run_id"],
        "output_normalizer_version": "new-normalizer",
    }
    harness.executor = ContractMigrationPassExecutor()

    repaired = asyncio.run(
        harness._auto_repair(wf, "P-FACT-CRITIC", {}, critic_output, state)
    )

    assert repaired is not None
    assert repaired["repair_id"] == failed["repair_id"]
    assert harness.provider_retry_calls[-1]["call_key"] == failed_call_key
    assert harness.context_builder.envelopes[-1]["overrides"][
        "payload.contract_feedback"
    ]["attempt"] == failed["execution_attempt"]
    created_after = [
        item for item in state["repair_ledger_v1"]["events"]
        if item["event"] == "CREATED"
    ]
    assert len(created_after) == len(created_before) == 1
    assert "last_targeted_repair_failure" not in state
    assert "contract_migration_recovery" not in state


def test_targeted_repair_contract_migration_does_not_cross_section_identity(
    tmp_path,
) -> None:
    harness = RepairHarness(tmp_path)
    harness.executor = ExhaustedContractRetryExecutor()
    wf = harness.workflow()
    state = {
        "repair_attempts": {},
        "original_environment": "OFFLINE_LOCAL",
        "options": {"targeted_repair_contract_retry_limit": 0},
        "active_section_id": "section-a",
    }
    critic_output = {
        "status": "REVISE",
        "findings": [{
            "code": "FACT_CRITIC_STATUS_UPGRADE",
            "repairable": True,
            "target_path_or_span": "METRIC-PROJ-001.claim_type",
            "repair_instruction": "change the claim type",
        }],
    }
    assert asyncio.run(
        harness._auto_repair(wf, "P-FACT-CRITIC", {}, critic_output, state)
    ) is None
    failed = copy.deepcopy(state["last_targeted_repair_failure"])
    state["contract_migration_recovery"] = {
        "checkpoint_identity_version": 1,
        "step": 0,
        "section_id": "section-a",
        "section_phase": None,
        "prompt_id": "P-TARGETED-REPAIR",
        "failed_run_id": failed["run_id"],
    }
    state["active_section_id"] = "section-b"
    harness.executor = ContractMigrationPassExecutor()

    repaired = asyncio.run(
        harness._auto_repair(wf, "P-FACT-CRITIC", {}, critic_output, state)
    )

    assert repaired is not None
    assert repaired["repair_id"] != failed["repair_id"]
    assert harness.provider_retry_calls[-1]["call_key"] != failed["call_key"]

class SemanticRejectRepairExecutor(ListRepairExecutor):
    async def execute(
        self, prompt_id: str, envelope: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        result = await super().execute(prompt_id, envelope, **kwargs)
        finding_id = str(
            envelope["overrides"]["payload.findings_to_repair"][0][
                "finding_instance_id"
            ]
        )
        result["status"] = "REVISE"
        result["output"]["result"].update({
            "resolved_finding_ids": [],
            "unresolved_finding_ids": [finding_id],
        })
        return result


def test_semantic_repair_rejection_persists_exact_section_checkpoint(
    tmp_path,
) -> None:
    harness = RepairHarness(tmp_path)
    harness.executor = SemanticRejectRepairExecutor()
    wf = harness.workflow()
    state = {
        "repair_attempts": {},
        "original_environment": "OFFLINE_LOCAL",
        "options": {},
        "active_section_id": "section-objective",
        "section_progress": {
            "section-objective": {"phase": "FACT_CRITIC"},
        },
    }
    critic_output = {
        "status": "REVISE",
        "findings": [{
            "code": "FACT_CRITIC_STATUS_UPGRADE",
            "repairable": True,
            "target_path_or_span": "METRIC-PROJ-001.claim_type",
            "repair_instruction": "change the claim type",
        }],
    }

    repaired = asyncio.run(
        harness._auto_repair(wf, "P-FACT-CRITIC", {}, critic_output, state)
    )

    assert repaired is None
    failure = state["last_targeted_repair_failure"]
    assert failure["category"] == "SEMANTIC_REPAIR_REJECTED"
    assert failure["repair_checkpoint_version"] == 1
    assert failure["workflow_step"] == 0
    assert failure["section_id"] == "section-objective"
    assert failure["section_phase"] == "FACT_CRITIC"
    assert failure["critic_prompt"] == "P-FACT-CRITIC"
    assert failure["repair_attempt_key"] == (
        "section:section-objective:P-FACT-CRITIC"
    )


class MissingOriginalContext(ListResultContext):
    def _result(self, project_id: str, prompt_id: str, key: str | None = None) -> Any:
        assert project_id == "project-1"
        assert prompt_id == "P-FACT-EXTRACT"
        assert key == "fact_candidates"
        return None


def test_repairable_critic_cannot_silently_skip_missing_original_object(tmp_path) -> None:
    harness = RepairHarness(tmp_path)
    harness.context_builder = MissingOriginalContext()
    wf = harness.workflow()
    state = {"repair_attempts": {}, "original_environment": "OFFLINE_LOCAL"}
    critic_output = {
        "status": "REVISE",
        "findings": [{
            "code": "FACT_CRITIC_STATUS_UPGRADE",
            "repairable": True,
            "target_type": "FACT_CANDIDATE",
            "target_path_or_span": "METRIC-PROJ-001.claim_type",
            "description": "The candidate type is wrong.",
            "repair_instruction": "Change the claim type.",
        }],
    }

    repaired = asyncio.run(
        harness._auto_repair(wf, "P-FACT-CRITIC", {}, critic_output, state)
    )

    assert repaired is None
    failure = state["last_targeted_repair_failure"]
    assert failure["category"] == "SEMANTIC_REPAIR_REJECTED"
    assert failure["reason_code"] == "ORIGINAL_OBJECT_UNAVAILABLE"
    assert failure["repair_attempt_key"] == "P-FACT-CRITIC"
    assert failure["consumes_semantic_repair_budget"] is False
    assert harness.provider_retry_calls == []
    assert state.get("repair_attempts", {}).get("P-FACT-CRITIC", 0) == 0
    events = state["repair_ledger_v1"]["events"]
    assert [item["event"] for item in events] == ["REPAIR_NOT_EXECUTABLE"]


class UnsupportedOriginalContext(ListResultContext):
    def _result(self, project_id: str, prompt_id: str, key: str | None = None) -> Any:
        assert project_id == "project-1"
        assert prompt_id == "P-FACT-EXTRACT"
        assert key == "fact_candidates"
        return "legacy scalar payload"


def test_repairable_critic_rejects_unsupported_original_shape_without_budget_use(tmp_path) -> None:
    harness = RepairHarness(tmp_path)
    harness.context_builder = UnsupportedOriginalContext()
    wf = harness.workflow()
    state = {"repair_attempts": {}, "original_environment": "OFFLINE_LOCAL"}
    critic_output = {
        "status": "REVISE",
        "findings": [{
            "code": "FACT_CRITIC_STATUS_UPGRADE",
            "repairable": True,
            "target_type": "FACT_CANDIDATE",
            "target_path_or_span": "METRIC-PROJ-001.claim_type",
            "description": "The candidate type is wrong.",
            "repair_instruction": "Change the claim type.",
        }],
    }

    repaired = asyncio.run(
        harness._auto_repair(wf, "P-FACT-CRITIC", {}, critic_output, state)
    )

    assert repaired is None
    failure = state["last_targeted_repair_failure"]
    assert failure["category"] == "SEMANTIC_REPAIR_REJECTED"
    assert failure["reason_code"] == "ORIGINAL_OBJECT_SHAPE_UNSUPPORTED"
    assert failure["consumes_semantic_repair_budget"] is False
    assert harness.provider_retry_calls == []
    assert state.get("repair_attempts", {}).get("P-FACT-CRITIC", 0) == 0
    assert [item["event"] for item in state["repair_ledger_v1"]["events"]] == [
        "REPAIR_NOT_EXECUTABLE"
    ]
