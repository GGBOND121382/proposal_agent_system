from __future__ import annotations

import asyncio
import copy
import json
from typing import Any

from app.db import Database
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
