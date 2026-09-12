from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

import pytest

from app.context_base import ContextBuilder
from app.db import Database
from app.model_semantic_contracts import (
    argument_authoritative_repair_paths,
    build_argument_architecture_critic_model_input,
    expand_argument_architecture_model_output,
)
from app.pack import PromptPack
from app.repair_ledger import RepairLedger
from app.util import sha256_json, utc_now
from app.workflow_repair import WorkflowRepairMixin, producer_consumer_value
from tests.test_semantic_contract_final_closure_v7 import _critic_context, _unit_key
from tests.test_semantic_model_contracts_v1 import (
    _argument_envelope_with_evidence,
    _semantic_argument_output,
)


ROOT = Path(__file__).resolve().parents[1]
CRITIC = "P-ARGUMENT-ARCHITECTURE-CRITIC"
PRODUCER = "P-ARGUMENT-ARCHITECTURE"


def _producer() -> tuple[dict[str, Any], dict[str, Any]]:
    envelope = _argument_envelope_with_evidence()
    canonical = expand_argument_architecture_model_output(
        envelope, _semantic_argument_output(envelope)
    )
    return envelope, canonical


def _scope_finding() -> dict[str, Any]:
    return {
        "finding_instance_id": "F-V9-SCOPE-001",
        "defect_namespace": "SEMANTIC_OBSERVATION",
        "defect_key": None,
        "code": "ARGUMENT_SCOPE_VIOLATION",
        "severity": "P1",
        "category": "ARGUMENT",
        "target_type": "ARGUMENT_SEMANTIC_COMPONENT",
        "target_path_or_span": "/result/scope_decision",
        "semantic_component": "SCOPE",
        "semantic_thread": None,
        "semantic_review_unit_key": None,
        "description": "Scope needs one local semantic revision.",
        "evidence_refs": [],
        "repairable": True,
        "repair_instruction": "Narrow the authored in-scope boundary.",
        "suggested_route": "ARGUMENT_ARCHITECTURE_AGENT",
        "blocking": True,
    }


class _Quality:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def record_targeted_repair(self, **kwargs: Any) -> None:
        self.calls.append(copy.deepcopy(kwargs))


class _LifecycleHarness(WorkflowRepairMixin):
    def __init__(self, tmp_path: Path, canonical_output: dict[str, Any], producer_input: dict[str, Any]) -> None:
        self.db = Database(tmp_path / "runtime.sqlite3")
        self.pack = PromptPack(ROOT / "prompt_pack")
        self.context_builder = ContextBuilder(self.db, self.pack)
        self.quality_manager = _Quality()
        now = utc_now()
        self.db.execute(
            "INSERT INTO projects(id,name,description,security_level,config_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            ("project-v9", "test", "test", "INTERNAL", "{}", now, now),
        )
        self.db.execute(
            "INSERT INTO workflows(id,project_id,workflow_type,status,current_step,state_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            ("wf-v9", "project-v9", "WF-4_PROPOSAL_AUTHORING", "RUNNING", 1, "{}", now, now),
        )
        self.db.execute(
            """INSERT INTO prompt_runs(
                   id,project_id,workflow_id,prompt_id,status,model_id,endpoint_id,
                   input_hash,output_hash,input_json,output_json,error,duration_ms,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "run-v9-producer",
                "project-v9",
                "wf-v9",
                PRODUCER,
                "PASS",
                "model-test",
                "endpoint-test",
                sha256_json(producer_input),
                sha256_json(canonical_output),
                json.dumps(producer_input, ensure_ascii=False),
                json.dumps(canonical_output, ensure_ascii=False),
                None,
                1,
                now,
            ),
        )
        self.db.execute(
            """INSERT INTO artifacts(id,project_id,workflow_id,artifact_type,prompt_id,version,status,security_level,context_hash,content_json,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "artifact-v9-producer", "project-v9", "wf-v9", "PROMPT_OUTPUT", PRODUCER,
                1, "PASS", "INTERNAL", sha256_json(canonical_output),
                json.dumps(canonical_output, ensure_ascii=False), now,
            ),
        )
        fact_output = {
            "result": {
                "fact_candidates": copy.deepcopy(
                    (producer_input.get("payload") or {}).get("confirmed_facts") or []
                )
            }
        }
        self.db.execute(
            """INSERT INTO artifacts(id,project_id,workflow_id,artifact_type,prompt_id,version,status,security_level,context_hash,content_json,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "artifact-v9-facts", "project-v9", "wf-v9", "PROMPT_OUTPUT", "P-FACT-EXTRACT",
                1, "PASS", "INTERNAL", sha256_json(fact_output),
                json.dumps(fact_output, ensure_ascii=False), now,
            ),
        )
        project_payload = producer_input.get("payload") or {}
        definition_output = {
            "result": {
                "project_definition": {
                    "items": copy.deepcopy((project_payload.get("project_subgraph") or {}).get("items") or []),
                    "relations": copy.deepcopy((project_payload.get("project_subgraph") or {}).get("relations") or []),
                },
                "proposal_contract": copy.deepcopy(project_payload.get("proposal_contract") or {}),
                "argument_graph_seed": copy.deepcopy(project_payload.get("argument_graph_seed") or {}),
            }
        }
        self.db.execute(
            """INSERT INTO artifacts(id,project_id,workflow_id,artifact_type,prompt_id,version,status,security_level,context_hash,content_json,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "artifact-v9-project-definition", "project-v9", "wf-v9", "PROMPT_OUTPUT", "P-PROJECT-DEFINITION-EXTRACT",
                1, "PASS", "INTERNAL", sha256_json(definition_output),
                json.dumps(definition_output, ensure_ascii=False), now,
            ),
        )


    def workflow(self) -> dict[str, Any]:
        row = self.db.fetchone("SELECT * FROM workflows WHERE id='wf-v9'")
        assert row is not None
        row["state"] = json.loads(row.pop("state_json"))
        return row

    @staticmethod
    def _project_level(project_id: str) -> str:
        assert project_id == "project-v9"
        return "INTERNAL"

    def _inherited_producer_source_catalog(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return []

    async def _execute_prompt_with_provider_retry(
        self,
        wf: dict[str, Any],
        state: dict[str, Any],
        *,
        prompt_id: str,
        envelope: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        assert prompt_id == "P-TARGETED-REPAIR"
        original = copy.deepcopy(envelope["payload"]["original_object"]["content"])
        assert "authored_state" in original
        assert "result" not in original
        original["authored_state"]["scope"]["in_scope"] = ["仅保留动态重规划核心问题"]
        finding_ids = [
            str(item["finding_instance_id"])
            for item in envelope["payload"]["findings_to_repair"]
        ]
        return {
            "run_id": "run-v9-repair",
            "status": "PASS",
            "route": {"environment": "OFFLINE_LOCAL"},
            "call_key": kwargs.get("call_key") or "call-v9-repair",
            "output": {
                "status": "PASS",
                "result": {
                    "repaired_object": original,
                    "resolved_finding_ids": finding_ids,
                    "unresolved_finding_ids": [],
                },
            },
        }


def test_v9_lifecycle_contract_uses_explicit_protocol_result_state_shapes() -> None:
    contract = __import__("app.contracts.semantic_contract", fromlist=["get_semantic_contract"]).get_semantic_contract()
    cfg = contract.rule("SC-ARGUMENT-LIFECYCLE-COMPOSITION").config
    assert cfg["shapes"] == {
        "producer_persisted_output": "PRODUCER_PROTOCOL_OUTPUT",
        "producer_consumer_value": "PRODUCER_RESULT",
        "critic_architecture_candidate": "PRODUCER_RESULT",
        "targeted_repair_original_content": "PRODUCER_RESULT",
        "repair_application_value": "PRODUCER_RESULT",
        "repair_canonical_output": "PRODUCER_PROTOCOL_OUTPUT",
        "authoritative_state": "AUTHORED_STATE",
        "argument_graph_consumer": "ARGUMENT_GRAPH",
    }
    assert cfg["lifecycle_rules"]["repair_commit_and_rereview_checkpoint"] == "ATOMIC"
    assert cfg["lifecycle_rules"]["original_producer_regeneration"] == "SUPERSEDES_ACTIVE_REPAIR"
    assert cfg["lifecycle_rules"]["integration_argument_regeneration"] == "SUPERSEDES_ACTIVE_REPAIR"
    assert cfg["lifecycle_rules"]["prerequisite_source_scope"] == "TRANSITIVE_FROZEN_CLOSURE"
    assert cfg["transitions"]["CONTEXT_TO_CRITIC"]["adapter"] == "AUTHORITATIVE_REPROJECT"
    assert cfg["transitions"]["CONTEXT_TO_DOWNSTREAM"]["adapter"] == "AUTHORITATIVE_REPROJECT"
    assert cfg["transitions"]["CONTEXT_TO_WF3_RESEARCH"]["reader_shape"] == "ARGUMENT_GRAPH"
    assert cfg["lifecycle_rules"]["post_projection_contract_failure"] == "REGENERATE_OR_RUNTIME_FAIL_NO_LLM_ENVELOPE_REPAIR"


def test_v9_producer_protocol_to_consumer_to_critic_shape_is_schema_valid() -> None:
    envelope, canonical = _producer()
    value = producer_consumer_value(PRODUCER, canonical)
    assert value == canonical["result"]
    assert "authored_state" in value
    assert "result" not in value
    critic_input, _, _ = _critic_context(canonical, envelope)
    critic_input["payload"]["architecture_candidate"] = copy.deepcopy(value)
    pack = PromptPack(ROOT / "prompt_pack")
    assert pack.validate(CRITIC, "input", critic_input) == []
    assert build_argument_architecture_critic_model_input(critic_input)


def test_v9_argument_repair_paths_are_relative_to_producer_result() -> None:
    envelope, canonical = _producer()
    critic_input, model_input, _ = _critic_context(canonical, envelope)
    method_key = _unit_key(model_input, "FORMAL_MODEL")
    paths = argument_authoritative_repair_paths(
        producer_consumer_value(PRODUCER, canonical),
        [{
            "code": "ARGUMENT_METHOD_SUBSTANCE_WEAK",
            "semantic_component": "METHOD",
            "semantic_thread": 0,
            "semantic_review_unit_key": method_key,
            "target_path_or_span": "/result/argument_architecture/nodes/0",
        }],
    )
    assert paths
    assert all(path.startswith("/authored_state/") for path in paths)
    assert not any(path.startswith("/result/") for path in paths)


def test_v9_real_repair_persistence_context_override_and_critic_rereview_compose(tmp_path: Path) -> None:
    producer_input, canonical = _producer()
    harness = _LifecycleHarness(tmp_path, canonical, producer_input)
    wf = harness.workflow()
    state: dict[str, Any] = {"options": {"targeted_repair_contract_retry_limit": 0}}
    critic_input, _, _ = _critic_context(canonical, producer_input)

    repaired = asyncio.run(
        harness._auto_repair(
            wf,
            CRITIC,
            critic_input,
            {"status": "REVISE", "findings": [_scope_finding()]},
            state,
        )
    )
    assert repaired is not None

    # The same DB transaction must persist the active repair pointer, APPLIED
    # ledger event, and independent rereview checkpoint.
    persisted_wf = harness.workflow()
    persisted_state = persisted_wf["state"]
    checkpoint = persisted_state["pending_repair_rereviews"][CRITIC]
    artifact_id = checkpoint["repair_application_artifact_id"]
    target_key = PRODUCER
    assert persisted_state["repair_application_artifact_ids"][target_key] == [artifact_id]
    events = RepairLedger.events(
        persisted_state,
        key=checkpoint["repair_attempt_key"],
        repair_id=checkpoint["repair_id"],
    )
    assert any(
        event["event"] == "APPLIED"
        and event["application_artifact_id"] == artifact_id
        for event in events
    )

    row = harness.db.fetchone("SELECT content_json FROM artifacts WHERE id=?", (artifact_id,))
    assert row is not None
    payload = json.loads(row["content_json"])
    assert payload["repaired_value_shape"] == "PRODUCER_RESULT"
    assert payload["repaired_canonical_output_shape"] == "PRODUCER_PROTOCOL_OUTPUT"
    assert "authored_state" in payload["repaired_value"]
    assert "result" not in payload["repaired_value"]
    assert payload["repaired_canonical_output"]["result"] == payload["repaired_value"]

    # Read through the actual ContextBuilder boundary, not directly from the
    # artifact payload, and feed that exact value to the Critic schema/runtime.
    value = harness.context_builder._repair_override(
        persisted_state, PRODUCER, workflow_id="wf-v9"
    )
    assert value == payload["repaired_value"]
    rereview_input = copy.deepcopy(critic_input)
    rereview_input["payload"]["architecture_candidate"] = value
    assert harness.pack.validate(CRITIC, "input", rereview_input) == []
    assert build_argument_architecture_critic_model_input(rereview_input)


def test_v9_argument_repair_override_fails_closed_on_hash_or_shape_corruption(tmp_path: Path) -> None:
    producer_input, canonical = _producer()
    harness = _LifecycleHarness(tmp_path, canonical, producer_input)
    wf = harness.workflow()
    state: dict[str, Any] = {"options": {"targeted_repair_contract_retry_limit": 0}}
    critic_input, _, _ = _critic_context(canonical, producer_input)
    repaired = asyncio.run(
        harness._auto_repair(
            wf, CRITIC, critic_input,
            {"status": "REVISE", "findings": [_scope_finding()]}, state,
        )
    )
    assert repaired is not None
    persisted = harness.workflow()["state"]
    artifact_id = persisted["pending_repair_rereviews"][CRITIC]["repair_application_artifact_id"]
    row = harness.db.fetchone("SELECT content_json FROM artifacts WHERE id=?", (artifact_id,))
    payload = json.loads(row["content_json"])
    payload["repaired_value"]["scope_decision"] = {"tampered": True}
    harness.db.execute(
        "UPDATE artifacts SET content_json=? WHERE id=?",
        (json.dumps(payload, ensure_ascii=False), artifact_id),
    )
    with pytest.raises(ValueError, match="hash mismatch"):
        harness.context_builder._repair_override(persisted, PRODUCER, workflow_id="wf-v9")


def test_v9_v8_legacy_full_envelope_repair_is_read_only_migrated_to_result(tmp_path: Path) -> None:
    producer_input, canonical = _producer()
    harness = _LifecycleHarness(tmp_path, canonical, producer_input)
    wf = harness.workflow()
    artifact_id = "artifact-v8-legacy"
    payload = {
        "schema_version": "1.0.0",
        "workflow_id": "wf-v9",
        "producer_prompt": PRODUCER,
        "critic_prompt": CRITIC,
        "target_key": PRODUCER,
        "application_status": "APPLIED",
        "original_object_hash": "0" * 64,
        "repaired_value_hash": sha256_json(canonical),
        "repaired_value": canonical,
        # v8 artifact intentionally has no repaired_value_shape.
    }
    harness.db.execute(
        """INSERT INTO artifacts(id,project_id,workflow_id,artifact_type,prompt_id,version,status,security_level,context_hash,content_json,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            artifact_id, "project-v9", "wf-v9", "REPAIR_APPLICATION", PRODUCER,
            1, "PASS", "INTERNAL", sha256_json(payload),
            json.dumps(payload, ensure_ascii=False), utc_now(),
        ),
    )
    state = {"repair_application_artifact_ids": {PRODUCER: [artifact_id]}}
    value = harness.context_builder._repair_override(state, PRODUCER, workflow_id="wf-v9")
    assert value == canonical["result"]
    assert "result" not in value


def test_v9_superseding_argument_subject_removes_stale_repair_pointer() -> None:
    class Harness(WorkflowRepairMixin):
        pass

    state = {
        "repair_application_artifact_ids": {PRODUCER: ["artifact-old"]},
        "pending_repair_rereviews": {
            CRITIC: {
                "repair_attempt_key": CRITIC,
                "repair_id": "repair-old",
                "repair_application_artifact_id": "artifact-old",
            }
        },
        "repair_attempts": {CRITIC: 1},
    }
    Harness()._supersede_repair_subject(
        state,
        critic_prompt=CRITIC,
        producer_prompt=PRODUCER,
        reason="ORIGINAL_PRODUCER_REGENERATION_SCHEDULED",
    )
    assert PRODUCER not in (state.get("repair_application_artifact_ids") or {})
    assert CRITIC not in (state.get("pending_repair_rereviews") or {})
    assert CRITIC not in (state.get("repair_attempts") or {})


def test_v9_context_builder_reprojects_authoritative_result_from_current_evidence(tmp_path: Path) -> None:
    producer_input, canonical = _producer()
    harness = _LifecycleHarness(tmp_path, canonical, producer_input)
    cached_gap = next(
        node for node in canonical["result"]["argument_architecture"]["nodes"]
        if node["node_type"] == "RESEARCH_GAP"
    )
    old_text = cached_gap["source_refs"][0]["quoted_text"]
    old_projection_hash = canonical["result"]["projection_meta"]["projection_input_sha256"]

    row = harness.db.fetchone("SELECT content_json FROM artifacts WHERE id='artifact-v9-facts'")
    facts_output = json.loads(row["content_json"])
    source_ref = facts_output["result"]["fact_candidates"][0]["source_refs"][0]
    source_ref["quoted_text"] = "UPDATED EVIDENCE TEXT"
    source_ref["source_hash"] = "f" * 64
    harness.db.execute(
        "UPDATE artifacts SET content_json=? WHERE id='artifact-v9-facts'",
        (json.dumps(facts_output, ensure_ascii=False),),
    )

    critic_envelope = harness.context_builder.build(
        CRITIC, "project-v9", workflow_id="wf-v9", workflow_state={}
    )
    candidate = critic_envelope["payload"]["architecture_candidate"]
    refreshed_gap = next(
        node for node in candidate["argument_architecture"]["nodes"]
        if node["node_type"] == "RESEARCH_GAP"
    )
    assert old_text != "UPDATED EVIDENCE TEXT"
    assert refreshed_gap["source_refs"][0]["quoted_text"] == "UPDATED EVIDENCE TEXT"
    assert refreshed_gap["source_refs"][0]["source_hash"] == "f" * 64
    assert candidate["authored_state"] == canonical["result"]["authored_state"]
    assert candidate["projection_meta"]["source_state_sha256"] == canonical["result"]["projection_meta"]["source_state_sha256"]
    assert candidate["projection_meta"]["projection_input_sha256"] != old_projection_hash
    assert harness.pack.validate(CRITIC, "input", critic_envelope) == []

@pytest.mark.asyncio
async def test_argument_post_projection_contract_failure_never_uses_llm_full_envelope_repair() -> None:
    from app.executor import PromptExecutionError
    from tests.test_step0_targeted_contract_repair import (
        _RepairHarness,
        _classification,
        _producer_row,
    )

    producer_input, canonical = _producer()
    harness = _RepairHarness(_producer_row(canonical), canonical)
    # Preserve the real semantic producer input shape for complete revalidation
    # should a deterministic representation rule ever repair the candidate.
    harness.db.row["input_json"] = json.dumps(producer_input, ensure_ascii=False)
    exc = PromptExecutionError(
        "post-projection contract failure",
        validation_errors=[
            "/result/authored_state/central_proposition/statement: simulated contract failure"
        ],
        run_id="run-producer",
    )
    state: dict[str, Any] = {}

    outcome = await harness._repair_producer_contract_failure(
        {"id": "wf-v9", "project_id": "project-1"},
        state,
        prompt_id=PRODUCER,
        envelope=producer_input,
        exc=exc,
        classification=_classification(),
    )

    assert outcome == {"attempted": True, "result": None}
    assert harness.repair_calls == 0
    assert harness.persisted is None
    escalation = state["contract_repair_escalations"][-1]
    assert escalation["reason"] == "AUTHORITATIVE_RUNTIME_CONTRACT_REGENERATION_REQUIRED"

def test_v9_persisted_rereview_checkpoint_resumes_idempotently_after_restart(tmp_path: Path) -> None:
    producer_input, canonical = _producer()
    harness = _LifecycleHarness(tmp_path, canonical, producer_input)
    wf = harness.workflow()
    state: dict[str, Any] = {"options": {"targeted_repair_contract_retry_limit": 0}}
    critic_input, _, _ = _critic_context(canonical, producer_input)
    repaired = asyncio.run(
        harness._auto_repair(
            wf,
            CRITIC,
            critic_input,
            {"status": "REVISE", "findings": [_scope_finding()]},
            state,
        )
    )
    assert repaired is not None

    # Simulate a process restart by discarding the in-memory state and loading
    # the transactionally persisted workflow state from SQLite.
    resumed = harness.workflow()["state"]
    checkpoint = harness._workflow_repair_rereview_checkpoint(resumed, CRITIC)
    assert checkpoint is not None
    first_count = harness._start_repair_rereview(
        resumed, checkpoint, critic_prompt=CRITIC
    )
    assert first_count == 1

    # Persist/reload again at the crash boundary immediately after start.  The
    # lifecycle identity makes REREVIEW_STARTED idempotent for the same repair.
    round_tripped = json.loads(json.dumps(resumed, ensure_ascii=False))
    checkpoint2 = harness._workflow_repair_rereview_checkpoint(round_tripped, CRITIC)
    assert checkpoint2 is not None
    second_count = harness._start_repair_rereview(
        round_tripped, checkpoint2, critic_prompt=CRITIC
    )
    assert second_count == 1
    events = RepairLedger.events(
        round_tripped,
        key=checkpoint2["repair_attempt_key"],
        repair_id=checkpoint2["repair_id"],
    )
    assert sum(event["event"] == "REREVIEW_STARTED" for event in events) == 1


def test_v9_user_input_transition_clears_only_rereview_checkpoint_not_active_repair() -> None:
    class Harness(WorkflowRepairMixin):
        pass

    state = {
        "repair_application_artifact_ids": {PRODUCER: ["artifact-active"]},
        "pending_repair_rereviews": {
            CRITIC: {
                "repair_id": "repair-active",
                "repair_attempt_key": CRITIC,
                "repair_application_artifact_id": "artifact-active",
            }
        },
    }
    Harness()._clear_workflow_repair_rereview(state, CRITIC)
    assert "pending_repair_rereviews" not in state
    assert state["repair_application_artifact_ids"][PRODUCER] == ["artifact-active"]

def test_v9_original_producer_transition_supersedes_active_repair_before_regeneration() -> None:
    from app.workflows import WorkflowEngine

    class _PackStub:
        @staticmethod
        def entry(prompt_id: str) -> dict[str, Any]:
            assert prompt_id == CRITIC
            return {"model_contract_mode": "SEMANTIC"}

    class _DBStub:
        def __init__(self) -> None:
            self.events: list[tuple[str, dict[str, Any]]] = []

        def audit(self, event: str, **kwargs: Any) -> None:
            self.events.append((event, copy.deepcopy(kwargs)))

    class Harness(WorkflowEngine):
        def __init__(self) -> None:
            self.pack = _PackStub()
            self.db = _DBStub()
            self.snapshot: dict[str, Any] | None = None

        def get(self, workflow_id: str) -> dict[str, Any]:
            assert workflow_id == "wf-v9"
            return {
                "id": "wf-v9",
                "steps": [
                    {"prompt_id": PRODUCER},
                    {"prompt_id": CRITIC},
                ],
            }

        def _update(self, wf: dict[str, Any], **kwargs: Any) -> None:
            if "current_step" in kwargs:
                wf["current_step"] = kwargs["current_step"]
            if "status" in kwargs:
                wf["status"] = kwargs["status"]
            if "state" in kwargs:
                wf["state"] = kwargs["state"]
            self.snapshot = copy.deepcopy(wf)

    state: dict[str, Any] = {
        "options": {"original_producer_regeneration_limit": 2},
        "step_results": {"0": {"old": True}, "1": {"old": True}},
        "repair_application_artifact_ids": {PRODUCER: ["artifact-old"]},
        "pending_repair_rereviews": {
            CRITIC: {
                "repair_id": "repair-old",
                "repair_attempt_key": CRITIC,
                "repair_application_artifact_id": "artifact-old",
            }
        },
        "repair_attempts": {CRITIC: 1},
    }
    wf = {
        "id": "wf-v9",
        "project_id": "project-v9",
        "current_step": 1,
        "status": "RUNNING",
        "state": state,
    }
    output = {
        "findings": [
            {
                "finding_instance_id": "F-STRUCTURAL",
                "code": "RESEARCH_DESIGN_INCOMPLETE",
                "blocking": True,
                "suggested_route": "ORIGINAL_PRODUCER",
            }
        ]
    }

    result = Harness()._prepare_original_producer_regeneration(
        wf, state, critic_prompt=CRITIC, output=output
    )
    assert result == "SCHEDULED"
    assert wf["current_step"] == 0
    assert PRODUCER not in (state.get("repair_application_artifact_ids") or {})
    assert CRITIC not in (state.get("pending_repair_rereviews") or {})
    assert CRITIC not in (state.get("repair_attempts") or {})
    assert state["producer_regeneration_rounds"][CRITIC] == 1
    assert state["step_results"] == {}

def test_v9_integration_argument_regeneration_supersedes_active_argument_repair() -> None:
    from app.workflows import WorkflowEngine

    class Harness(WorkflowEngine):
        def __init__(self) -> None:
            self.invalidations: list[str] = []

        def get(self, workflow_id: str) -> dict[str, Any]:
            assert workflow_id == "wf-v9"
            return {
                "id": "wf-v9",
                "steps": [
                    {"prompt_id": PRODUCER},
                    {"prompt_id": CRITIC},
                    {"prompt_id": "P-INTEGRATION-CRITIC"},
                ],
            }

        def _update(self, wf: dict[str, Any], **kwargs: Any) -> None:
            if "current_step" in kwargs:
                wf["current_step"] = kwargs["current_step"]
            if "status" in kwargs:
                wf["status"] = kwargs["status"]
            if "state" in kwargs:
                wf["state"] = kwargs["state"]

        def _invalidate_full_proposal_generation(self, state: dict[str, Any], *, reason: str) -> None:
            self.invalidations.append(reason)

    state: dict[str, Any] = {
        "repair_application_artifact_ids": {PRODUCER: ["artifact-old"]},
        "pending_repair_rereviews": {
            CRITIC: {
                "repair_id": "repair-old",
                "repair_attempt_key": CRITIC,
                "repair_application_artifact_id": "artifact-old",
            }
        },
        "repair_attempts": {CRITIC: 1},
        "section_results": [{"old": True}],
    }
    wf = {
        "id": "wf-v9",
        "project_id": "project-v9",
        "current_step": 2,
        "status": "RUNNING",
        "state": state,
    }
    output = {
        "findings": [
            {
                "code": "INTEGRATION_ARGUMENT_DEFECT",
                "category": "ARGUMENT",
                "blocking": True,
                "suggested_route": "ARGUMENT_ARCHITECTURE_AGENT",
            }
        ]
    }

    result = Harness()._prepare_integration_repair(wf, state, output)
    assert result == "SCHEDULED"
    assert wf["current_step"] == 0
    assert PRODUCER not in (state.get("repair_application_artifact_ids") or {})
    assert CRITIC not in (state.get("pending_repair_rereviews") or {})
    assert CRITIC not in (state.get("repair_attempts") or {})
    assert state["argument_revision_findings"] == output["findings"]

def test_v9_wf3_research_consumer_uses_fresh_authoritative_projection(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import app.context_base as context_module

    producer_input, canonical = _producer()
    harness = _LifecycleHarness(tmp_path, canonical, producer_input)
    builder = harness.context_builder
    sentinel_graph = {"graph_id": "fresh-graph", "research_questions": [], "nodes": [], "edges": []}
    seen: dict[str, Any] = {}

    monkeypatch.setattr(
        builder,
        "_reproject_authoritative_argument_result",
        lambda project_id, state, *, workflow_id, **kwargs: {
            "argument_architecture": copy.deepcopy(sentinel_graph)
        },
    )

    def fake_build_research_need(**kwargs: Any) -> tuple[dict[str, Any], str]:
        seen["argument_graph"] = copy.deepcopy(kwargs.get("argument_graph"))
        return (
            {
                "need_id": "need-v9",
                "question": "What current evidence is needed?",
                "scope": "PUBLIC",
            },
            "ARGUMENT_GRAPH",
        )

    monkeypatch.setattr(context_module, "build_research_need", fake_build_research_need)
    result = builder._wf3_online_assist_payload(
        project={
            "id": "project-v9",
            "name": "test",
            "description": "test",
            "security_level": "INTERNAL",
        },
        config={},
        docs=[],
        state={"options": {"source_items": []}},
        workflow_id="wf-v9",
    )
    assert seen["argument_graph"] == sentinel_graph
    assert result["research_need"]["need_id"] == "need-v9"


def test_v9_real_user_gate_decision_preserves_active_argument_repair(tmp_path: Path) -> None:
    from app.workflow_gates import WorkflowGateMixin

    producer_input, canonical = _producer()
    harness = _LifecycleHarness(tmp_path, canonical, producer_input)
    db = harness.db
    now = utc_now()
    state = {
        "repair_application_artifact_ids": {PRODUCER: ["artifact-active"]},
        "step_results": {
            "1": {
                "prompt_id": CRITIC,
                "run_id": "run-v9-critic-need-user",
                "status": "NEED_USER_INPUT",
            }
        },
    }
    db.execute(
        "UPDATE workflows SET status='WAITING_GATE',current_step=1,state_json=?,updated_at=? WHERE id='wf-v9'",
        (json.dumps(state, ensure_ascii=False), now),
    )
    critic_input, _, _ = _critic_context(canonical, producer_input)
    db.execute(
        """INSERT INTO prompt_runs(
               id,project_id,workflow_id,prompt_id,status,model_id,endpoint_id,
               input_hash,output_hash,input_json,output_json,error,duration_ms,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "run-v9-critic-need-user", "project-v9", "wf-v9", CRITIC,
            "NEED_USER_INPUT", "model-test", "endpoint-test",
            sha256_json(critic_input), sha256_json({"status": "NEED_USER_INPUT"}),
            json.dumps(critic_input, ensure_ascii=False),
            json.dumps({"status": "NEED_USER_INPUT"}, ensure_ascii=False),
            None, 1, now,
        ),
    )

    class GateHarness(WorkflowGateMixin):
        def __init__(self, database: Database) -> None:
            self.db = database

        def get(self, workflow_id: str) -> dict[str, Any]:
            row = self.db.fetchone("SELECT * FROM workflows WHERE id=?", (workflow_id,))
            assert row is not None
            row["state"] = json.loads(row.pop("state_json"))
            return row

    gate_engine = GateHarness(db)
    workflow = gate_engine.get("wf-v9")
    questions = [{
        "question_id": "Q-V9-USER",
        "question": "Please choose the intended semantic interpretation.",
        "target_paths": ["payload.architecture_candidate.authored_state.central_proposition.statement"],
        "answer_schema": {"type": "STRING"},
        "blocking": True,
    }]
    context_hash = gate_engine._gate_context_hash_v2(
        workflow,
        gate_type="PROJECT_GAP_RESOLUTION",
        target_id="run-v9-critic-need-user",
        questions=questions,
    )
    db.execute(
        """INSERT INTO gates(
               id,project_id,workflow_id,gate_type,target_id,target_version,context_hash,
               question_version,required_role,allowed_actions_json,questions_json,security_level,
               status,decision_json,created_at,updated_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "gate-v9-user", "project-v9", "wf-v9", "PROJECT_GAP_RESOLUTION",
            "run-v9-critic-need-user", 1, context_hash, 2, "PROJECT_OWNER",
            json.dumps(["PROVIDE_INFORMATION"]), json.dumps(questions, ensure_ascii=False),
            "INTERNAL", "OPEN", None, now, now,
        ),
    )

    gate_engine.decide_gate(
        "gate-v9-user",
        action="PROVIDE_INFORMATION",
        decided_by="pytest",
        decided_role="PROJECT_OWNER",
        answers=[{"question_id": "Q-V9-USER", "value": "Use interpretation A"}],
        context_hash=context_hash,
    )
    persisted = gate_engine.get("wf-v9")["state"]
    assert persisted["repair_application_artifact_ids"][PRODUCER] == ["artifact-active"]
    assert "1" not in (persisted.get("step_results") or {})
    assert persisted["rerun_from_human_input"]["prompt_id"] == CRITIC


def test_v9_workflow_artifact_scope_closes_transitive_frozen_prerequisites(tmp_path: Path) -> None:
    producer_input, canonical = _producer()
    harness = _LifecycleHarness(tmp_path, canonical, producer_input)
    db = harness.db
    now = utc_now()
    for workflow_id, workflow_type, prereqs in [
        ("wf-intake", "WF-1_PROJECT_INTAKE", {}),
        ("wf-template", "WF-2_TEMPLATE_EXTRACTION", {}),
        ("wf-research", "WF-3_HYBRID_ONLINE_ASSIST", {"WF-1_PROJECT_INTAKE": "wf-intake"}),
        ("wf-authoring", "WF-4_PROPOSAL_AUTHORING", {
            "WF-1_PROJECT_INTAKE": "wf-intake",
            "WF-2_TEMPLATE_EXTRACTION": "wf-template",
            "WF-3_HYBRID_ONLINE_ASSIST": "wf-research",
        }),
        ("wf-export", "WF-5_SECURITY_REVIEW_AND_EXPORT", {"WF-4_PROPOSAL_AUTHORING": "wf-authoring"}),
    ]:
        db.execute(
            "INSERT INTO workflows(id,project_id,workflow_type,status,current_step,state_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                workflow_id, "project-v9", workflow_type,
                "RUNNING" if workflow_id == "wf-export" else "COMPLETED",
                0, json.dumps({"prerequisite_workflow_ids": prereqs}, ensure_ascii=False),
                now, now,
            ),
        )
    source_ids = harness.context_builder._workflow_artifact_source_ids("wf-export")
    assert source_ids == [
        "wf-export", "wf-authoring", "wf-intake", "wf-template", "wf-research"
    ]
