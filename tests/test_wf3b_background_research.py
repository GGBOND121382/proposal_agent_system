from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.background_research import (
    BACKGROUND_DIMENSIONS,
    UNSCOPED_DIMENSION,
    WF3B_WORKFLOW_TYPE,
    background_execution_contract,
    build_background_cards,
    normalize_background_plan,
    normalize_required_dimensions,
    normalize_wf3b_options,
    resolve_wf3b_topic,
    wf3b_topic_id,
)
from app.config import Settings
from app.db import Database
from app.dependency_preflight import RuntimeDependencyPreflight
from app.executor import PromptExecutor
from app.pack import PromptPack
from app.runtime_context import LiveContextBuilder
from app.skills.research_quality import (
    build_background_coverage_dimensions,
    build_research_sufficiency,
)
from app.skills.research_plan import (
    MAX_BACKGROUND_RESEARCH_QUERIES,
    normalize_and_validate_plan,
)
from app.util import new_id, sha256_json, utc_now
from app.workflow_catalog import ALL_WORKFLOWS
from app.workflow_defs import CRITIC_PRODUCER, WORKFLOWS
from app.workflow_lifecycle import WorkflowLifecycleService
from app.workflow_status import should_pause_automatic_advancement
from app.workflows import WorkflowEngine
from tests.test_runtime_recovery import make_executor_db
from tests.test_v04_complex_runtime import _finish, _project, _runtime
from tests.test_workflow_lifecycle_rebuild import (
    CompletingWorkflows,
    add_project,
    add_workflow,
)

ROOT = Path(__file__).resolve().parents[1]


class StubContext:
    """Minimal context builder carrying canned per-workflow prompt results."""

    def __init__(self, results: dict | None = None):
        self.results = dict(results or {})

    def _result(
        self,
        project_id: str,
        prompt_id: str,
        key: str | None = None,
        *,
        workflow_id: str | None = None,
        exact_workflow: bool = False,
    ):
        del project_id, exact_workflow
        value = self.results.get((prompt_id, workflow_id))
        if key and isinstance(value, dict):
            return value.get(key)
        return value


def _engine(db: Database, results: dict | None = None) -> WorkflowEngine:
    return WorkflowEngine(
        db,
        SimpleNamespace(),
        StubContext(results),
        SimpleNamespace(),
        SimpleNamespace(),
    )


def _add_wf3b(db: Database, state: dict | None = None) -> str:
    workflow_id = new_id("wf")
    now = utc_now()
    db.execute(
        "INSERT INTO workflows(id,project_id,workflow_type,status,current_step,state_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (
            workflow_id,
            "project-1",
            WF3B_WORKFLOW_TYPE,
            "RUNNING",
            0,
            json.dumps(state or {"workflow_type": WF3B_WORKFLOW_TYPE, "options": {}, "step_results": {}}),
            now,
            now,
        ),
    )
    return workflow_id


def test_wf3b_workflow_definition_matches_frozen_eight_step_contract():
    steps = WORKFLOWS[WF3B_WORKFLOW_TYPE]
    assert steps == [
        {"prompt_id": "P-SAFE-ONLINE-PACKAGE"},
        {"prompt_id": "P-SAFE-ONLINE-PACKAGE-CRITIC"},
        {"prompt_id": "P-BACKGROUND-RESEARCH-PLAN"},
        {"prompt_id": "P-BACKGROUND-RESEARCH-PLAN-CRITIC"},
        {"type": "PUBLIC_SEARCH"},
        {"prompt_id": "P-BACKGROUND-RESEARCH-SYNTHESIS"},
        {"prompt_id": "P-BACKGROUND-RESEARCH-CRITIC"},
        {"prompt_id": "P-ONLINE-RESULT-IMPORT-CRITIC"},
    ]
    assert CRITIC_PRODUCER["P-BACKGROUND-RESEARCH-PLAN-CRITIC"] == "P-BACKGROUND-RESEARCH-PLAN"
    assert CRITIC_PRODUCER["P-BACKGROUND-RESEARCH-CRITIC"] == "P-BACKGROUND-RESEARCH-SYNTHESIS"
    assert WF3B_WORKFLOW_TYPE in ALL_WORKFLOWS
    assert ALL_WORKFLOWS[WF3B_WORKFLOW_TYPE] == steps


def test_wf3b_options_default_freezes_all_eight_dimensions():
    normalized = normalize_wf3b_options(None)
    assert normalized["required_dimensions"] == list(BACKGROUND_DIMENSIONS)
    assert len(normalized["required_dimensions"]) == 8
    assert normalized["required_dimensions_origin"] == "DEFAULT_ALL"


def test_wf3b_explicit_dimension_subset_keeps_canonical_order_and_deduplicates():
    normalized = normalize_wf3b_options(
        {"background_dimensions": ["research_significance", "APPLICATION_SCENARIO", " application_scenario "]}
    )
    assert normalized["required_dimensions"] == ["APPLICATION_SCENARIO", "RESEARCH_SIGNIFICANCE"]
    assert normalized["required_dimensions_origin"] == "WORKFLOW_OPTIONS"

    nested = normalize_wf3b_options({"wf3b": {"required_dimensions": ["OPERATIONAL_CONSTRAINT"]}})
    assert nested["required_dimensions"] == ["OPERATIONAL_CONSTRAINT"]
    assert "wf3b" not in nested

    ui_options = normalize_wf3b_options(
        {
            "background_research": {
                "topic_override": "生成式人工智能科研应用",
                "background_dimensions": [
                    "POLICY_STANDARD_AND_PROGRAM",
                    "APPLICATION_SCENARIO",
                ],
                "focus": "政策和应用案例",
            }
        },
        project_id="project-ui",
    )
    assert ui_options["topic"] == "生成式人工智能科研应用"
    assert ui_options["topic_origin"] == "WORKFLOW_OPTIONS"
    assert ui_options["required_dimensions"] == [
        "APPLICATION_SCENARIO",
        "POLICY_STANDARD_AND_PROGRAM",
    ]
    assert ui_options["focus"] == "政策和应用案例"
    assert "background_research" not in ui_options


def test_wf3b_unknown_or_empty_dimensions_fail_fast():
    with pytest.raises(ValueError, match="未知背景维度"):
        normalize_required_dimensions({"background_dimensions": ["NOT_A_DIMENSION"]})
    with pytest.raises(ValueError, match="至少需要一个有效背景维度"):
        normalize_required_dimensions({"background_dimensions": []})
    with pytest.raises(ValueError, match="必须是背景维度数组"):
        normalize_required_dimensions({"required_dimensions": "APPLICATION_SCENARIO"})


def test_wf3b_topic_resolution_order_and_deterministic_topic_id():
    topic, origin = resolve_wf3b_topic(
        {"topic": " 智慧水务背景 "},
        wf1_project_definition={"project_title": "WF-1 标题"},
    )
    assert (topic, origin) == ("智慧水务背景", "WORKFLOW_OPTIONS")

    topic, origin = resolve_wf3b_topic(
        {},
        wf1_project_definition={"project_title": "农村污水治理", "research_object": "智慧监测"},
    )
    assert (topic, origin) == ("农村污水治理：智慧监测", "WF1_PROJECT_DEFINITION")

    topic, origin = resolve_wf3b_topic(
        {},
        wf1_project_definition={"project_title": "已含 智慧监测 的标题", "research_object": "智慧监测"},
    )
    assert (topic, origin) == ("已含 智慧监测 的标题", "WF1_PROJECT_DEFINITION")

    topic, origin = resolve_wf3b_topic(
        {},
        wf1_project_definition={"problem_definition": {"problem_statement": "问题" + "长" * 300}},
    )
    assert origin == "WF1_PROBLEM_STATEMENT"
    assert topic is not None and len(topic) == 200

    topic, origin = resolve_wf3b_topic(None)
    assert (topic, origin) == (None, "UNRESOLVED")

    definition = {"project_title": "农村污水治理"}
    first = normalize_wf3b_options({}, project_id="project-1", wf1_project_definition=definition)
    second = normalize_wf3b_options({}, project_id="project-1", wf1_project_definition=definition)
    assert first["topic"] == "农村污水治理"
    assert first["topic_id"] == wf3b_topic_id("project-1", "农村污水治理")
    assert first["topic_id"] == second["topic_id"]
    assert first["topic_id"] != wf3b_topic_id("project-2", "农村污水治理")

    unresolved = normalize_wf3b_options({})
    assert "topic" not in unresolved
    assert "topic_id" not in unresolved
    assert unresolved["topic_origin"] == "UNRESOLVED"


def test_background_plan_normalization_filters_dimensions_without_rewriting_semantics():
    plan = {
        "plan_id": "p1",
        "queries": [
            {
                "query_id": "q1",
                "query": "保留原始语义文本",
                "dimensions": ["APPLICATION_SCENARIO", "UNKNOWN_DIM", "STAKEHOLDER_AND_PAIN"],
            },
            {"query_id": "q2", "query": "另一条查询", "dimensions": ["not_a_dimension"]},
        ],
    }
    normalized, findings = normalize_background_plan(
        plan,
        required_dimensions=["APPLICATION_SCENARIO", "INDUSTRY_SCALE_AND_TREND"],
    )

    assert normalized["task_type"] == "PUBLIC_BACKGROUND_RESEARCH"
    assert normalized["required_dimensions"] == ["APPLICATION_SCENARIO", "INDUSTRY_SCALE_AND_TREND"]
    assert normalized["queries"][0]["query"] == "保留原始语义文本"
    assert normalized["queries"][0]["dimensions"] == ["APPLICATION_SCENARIO"]
    assert normalized["queries"][1]["dimensions"] == []
    assert normalized["dimension_coverage"] == {
        "APPLICATION_SCENARIO": {"status": "PLANNED"},
        "INDUSTRY_SCALE_AND_TREND": {"status": "UNPLANNED"},
    }

    assert [item["code"] for item in findings] == [
        "BACKGROUND_PLAN_UNKNOWN_DIMENSION",
        "BACKGROUND_PLAN_DIMENSION_OUT_OF_SCOPE",
        "BACKGROUND_PLAN_UNKNOWN_DIMENSION",
        "BACKGROUND_PLAN_DIMENSION_UNCOVERED",
    ]
    assert findings[0]["dimension"] == "UNKNOWN_DIM"
    assert findings[1]["dimension"] == "STAKEHOLDER_AND_PAIN"
    assert findings[3]["dimensions"] == ["INDUSTRY_SCALE_AND_TREND"]

    # The model-authored plan object itself is never mutated in place.
    assert plan["queries"][0]["dimensions"] == ["APPLICATION_SCENARIO", "UNKNOWN_DIM", "STAKEHOLDER_AND_PAIN"]


def test_background_plan_without_any_query_marks_every_frozen_dimension_uncovered():
    normalized, findings = normalize_background_plan(
        {"queries": []},
        required_dimensions=["APPLICATION_SCENARIO"],
    )
    assert normalized["dimension_coverage"] == {"APPLICATION_SCENARIO": {"status": "UNPLANNED"}}
    assert [item["code"] for item in findings] == ["BACKGROUND_PLAN_DIMENSION_UNCOVERED"]
    assert findings[0]["dimensions"] == ["APPLICATION_SCENARIO"]


def test_background_plan_adapts_dimension_queries_to_shared_execution_contract():
    dimensions = list(BACKGROUND_DIMENSIONS)
    plan = {
        "plan_id": "background-plan",
        "binding_contract_version": "1.0",
        "time_scope": "2021-01-01/2026-09-04",
        "evidence_requirements": ["优先使用一手公开来源"],
        "prohibited_inferences": ["不得推断内部信息"],
        "queries": [
            {
                "query_id": f"query-{index:02d}",
                "query": f"公开资料检索主题 {index} authoritative source",
                "dimension": dimensions[index % len(dimensions)],
                "purpose": "核验公开背景",
            }
            for index in range(22)
        ],
    }

    normalized, findings = normalize_background_plan(
        plan,
        required_dimensions=dimensions,
    )
    execution_plan = background_execution_contract(normalized)
    _, validation = normalize_and_validate_plan(
        execution_plan,
        strict=True,
        max_queries=MAX_BACKGROUND_RESEARCH_QUERIES,
    )

    assert len(normalized["queries"]) == 22
    assert {item["dimension"] for item in normalized["queries"]} == set(dimensions)
    assert all(item["linked_question_indexes"] for item in normalized["queries"])
    assert normalized["research_questions"]
    assert normalized["source_priorities"]
    assert validation["status"] == "PASS"
    assert findings == []


def test_background_execution_contract_forces_web_discovery_and_web_channel():
    contracted = background_execution_contract(
        {"require_web_discovery": False, "required_channels": ["academic"]}
    )
    assert contracted["require_web_discovery"] is True
    assert contracted["required_channels"] == ["ACADEMIC", "WEB_SEARCH"]

    again = background_execution_contract(contracted)
    assert again["require_web_discovery"] is True
    assert again["required_channels"] == ["ACADEMIC", "WEB_SEARCH"]

    assert background_execution_contract({})["required_channels"] == ["WEB_SEARCH"]


def test_wf3b_synthesis_representation_normalizes_dimension_profiles_and_span_id():
    output = {
        "result": {
            "claims": [
                {
                    "target_section_profiles": [
                        "RESEARCH_SIGNIFICANCE",
                        "STAKEHOLDER_AND_PAIN",
                        "BACKGROUND_AND_SIGNIFICANCE",
                    ],
                    "source_refs": [
                        {"source_id": "public-src-1", "span_id": "passage-1"}
                    ],
                }
            ]
        },
        "source_refs": [
            {"source_id": "public-src-1", "span_id": "passage-1"}
        ],
    }

    changes = PromptExecutor._normalize_wf3b_synthesis_representation(output)

    assert output["result"]["claims"][0]["target_section_profiles"] == [
        "BACKGROUND_AND_SIGNIFICANCE",
        "NEED_ANALYSIS",
    ]
    assert "span_id" not in output["result"]["claims"][0]["source_refs"][0]
    assert "span_id" not in output["source_refs"][0]
    assert changes


def test_live_context_projects_approved_safe_package_into_wf3b_contract(tmp_path):
    db = make_executor_db(tmp_path)
    pack = PromptPack(ROOT / "prompt_pack")
    workflow_id = _add_wf3b(db)
    state = {
        "workflow_type": WF3B_WORKFLOW_TYPE,
        "options": {
            "topic": "智慧水务应用背景",
            "topic_id": "topic-smart-water",
            "topic_origin": "WORKFLOW_OPTIONS",
            "required_dimensions": [
                "APPLICATION_SCENARIO",
                "POLICY_STANDARD_AND_PROGRAM",
            ],
            "allowed_public_topics": ["智慧水务", "水质监测"],
        },
        "step_results": {},
        "repair_attempts": {},
    }
    safe_output = pack.replay_output("P-SAFE-ONLINE-PACKAGE", "normal")
    db.execute(
        """INSERT INTO artifacts(
             id,project_id,workflow_id,artifact_type,prompt_id,version,status,
             security_level,context_hash,content_json,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            new_id("artifact"),
            "project-1",
            workflow_id,
            "PROMPT_OUTPUT",
            "P-SAFE-ONLINE-PACKAGE",
            1,
            "PASS",
            "INTERNAL",
            sha256_json(safe_output),
            json.dumps(safe_output, ensure_ascii=False),
            utc_now(),
        ),
    )

    envelope = LiveContextBuilder(db, pack).build(
        "P-BACKGROUND-RESEARCH-PLAN",
        "project-1",
        workflow_id=workflow_id,
        workflow_state=state,
    )

    assert pack.validate("P-BACKGROUND-RESEARCH-PLAN", "input", envelope) == []
    payload = envelope["payload"]
    assert payload["task_type"] == "PUBLIC_BACKGROUND_RESEARCH"
    assert payload["safe_online_package_content"]["task_type"] == (
        "PUBLIC_BACKGROUND_RESEARCH"
    )
    assert payload["topic"] == {
        "topic_id": "topic-smart-water",
        "topic_description": "智慧水务应用背景",
    }
    assert payload["required_dimensions"] == [
        "APPLICATION_SCENARIO",
        "POLICY_STANDARD_AND_PROGRAM",
    ]
    assert payload["optional_dimensions"] == []
    assert payload["retrieval_contract"]["require_web_discovery"] is True
    assert "WEB_SEARCH" in payload["retrieval_contract"]["required_channels"]


def _record(source_id: str, provider: str) -> dict:
    return {"source_id": source_id, "verification": {"discovery_provider": provider}}


def test_application_background_pure_academic_set_is_never_sufficient():
    by_query = {
        "q1": {
            "source_count": 3,
            "source_ids": ["s1", "s2", "s3"],
            "authoritative_source_count": 1,
            "authoritative_source_ids": ["s1"],
        }
    }
    academic_only = [_record("s1", "openalex"), _record("s2", "crossref"), _record("s3", "semantic_scholar")]
    dimensions = build_background_coverage_dimensions(academic_only, by_query)
    assert dimensions["web_evidence"]["status"] == "INSUFFICIENT"
    assert dimensions["web_evidence"]["require_web_discovery"] is True
    assert dimensions["web_evidence"]["source_ids"] == []

    sufficiency = build_research_sufficiency(
        {"status": "INSUFFICIENT", "by_query": by_query, "dimensions": dimensions},
        {"queries": ["q1"], "query_items": []},
        {"status": "PASS", "blocking_reason_codes": []},
    )
    assert sufficiency["status"] == "DEGRADED"
    assert sufficiency["status"] != "SUFFICIENT"
    assert sufficiency["may_continue"] is True
    assert any("WEB_EVIDENCE" in gap["gap_types"] for gap in sufficiency["research_gaps"])

    with_web = build_background_coverage_dimensions(
        academic_only + [_record("s4", "searxng")], by_query
    )
    assert with_web["web_evidence"]["status"] == "PASS"
    assert with_web["web_evidence"]["source_ids"] == ["s4"]
    sufficiency = build_research_sufficiency(
        {"status": "PASS", "by_query": by_query, "dimensions": with_web},
        {"queries": ["q1"], "query_items": []},
        {"status": "PASS", "blocking_reason_codes": []},
    )
    assert sufficiency["status"] == "SUFFICIENT"


def _synthesis_with_claims() -> dict:
    return {
        "claims": [
            {
                "claim_id": "c1",
                "dimension": "industry_scale_and_trend",
                "claim_text": "行业规模增长",
                "scope_qualifiers": ["中国"],
                "target_section_profiles": ["BACKGROUND_AND_SIGNIFICANCE"],
            },
            {"claim_id": "c2", "dimension": "POLICY_STANDARD_AND_PROGRAM", "claim_text": "被拒绝的 claim"},
            {"claim_id": "c3", "dimension": "NOT_A_DIMENSION", "claim_text": "维度外 claim"},
        ]
    }


def _claim_validation() -> dict:
    return {
        "bindings": [
            {"claim_id": "c1", "source_ids": ["s1", "s2"], "evidence_mode": "FULLTEXT"},
            {"claim_id": "c2", "source_ids": ["s3"], "evidence_mode": "SNIPPET"},
            {"claim_id": "c3", "source_ids": [], "evidence_mode": ""},
        ],
        "findings": [{"claim_id": "c2", "severity": "P0", "code": "PUBLIC_CLAIM_INVALID"}],
    }


def test_background_cards_only_from_bound_claims_with_deterministic_ids():
    required = ["INDUSTRY_SCALE_AND_TREND", "POLICY_STANDARD_AND_PROGRAM"]
    bundle = build_background_cards(
        _synthesis_with_claims(),
        {},
        _claim_validation(),
        required_dimensions=required,
        topic_id="topic-x",
    )
    cards = bundle["background_cards"]
    assert [card["claim_id"] for card in cards] == ["c1", "c3"]

    bound = cards[0]
    assert bound["card_id"].startswith("bgcard-")
    assert bound["dimension"] == "INDUSTRY_SCALE_AND_TREND"
    assert bound["source_ids"] == ["s1", "s2"]
    assert bound["evidence_mode"] == "FULLTEXT"
    assert bound["scope_qualifiers"] == ["中国"]
    assert cards[1]["dimension"] == UNSCOPED_DIMENSION

    repeat = build_background_cards(
        _synthesis_with_claims(), {}, _claim_validation(),
        required_dimensions=required, topic_id="topic-x",
    )
    assert [card["card_id"] for card in repeat["background_cards"]] == [
        card["card_id"] for card in cards
    ]
    other_topic = build_background_cards(
        _synthesis_with_claims(), {}, _claim_validation(),
        required_dimensions=required, topic_id="topic-y",
    )
    assert other_topic["background_cards"][0]["card_id"] != bound["card_id"]

    coverage = bundle["background_dimensions"]
    assert coverage["INDUSTRY_SCALE_AND_TREND"]["status"] == "COVERED"
    assert coverage["INDUSTRY_SCALE_AND_TREND"]["card_ids"] == [bound["card_id"]]
    assert coverage["POLICY_STANDARD_AND_PROGRAM"]["status"] == "GAP"
    gaps = bundle["background_gaps"]
    assert [gap["dimension"] for gap in gaps] == ["POLICY_STANDARD_AND_PROGRAM"]
    assert gaps[0]["gap_id"] == "background-gap-001"
    assert gaps[0]["scope"] == "DIMENSION"


def _persist_state(**overrides) -> dict:
    state = {
        "options": {
            "required_dimensions": ["APPLICATION_SCENARIO", "POLICY_STANDARD_AND_PROGRAM"],
            "topic": "智慧水务",
            "topic_id": "topic-x",
            "topic_origin": "WORKFLOW_OPTIONS",
        },
        "background_search_results": {
            "coverage": {"status": "PASS"},
            "retrieval_health": {"status": "PASS"},
            "research_sufficiency": {
                "schema_version": "1.0",
                "status": "SUFFICIENT",
                "coverage_status": "PASS",
                "research_gaps": [],
                "blocking_reasons": [],
                "retrieval_health_status": "PASS",
                "may_continue": True,
            },
            "source_catalog": [{"source_id": "s1"}],
            "archive_manifest": "manifest.json",
            "archive_root": "archive",
        },
        "background_claim_validation": {
            "bindings": [{"claim_id": "c1", "source_ids": ["s1"], "evidence_mode": "FULLTEXT"}],
            "findings": [],
        },
    }
    state.update(overrides)
    return state


def _persist_results(workflow_id: str) -> dict:
    return {
        ("P-BACKGROUND-RESEARCH-SYNTHESIS", workflow_id): {
            "claims": [
                {"claim_id": "c1", "dimension": "APPLICATION_SCENARIO", "claim_text": "场景 claim"}
            ]
        },
        ("P-ONLINE-RESULT-IMPORT-CRITIC", workflow_id): {
            "accepted_claim_ids": ["c1"],
            "reference_only_claim_ids": [],
            "rejected_claim_ids": [],
        },
    }


def test_persist_wf3b_background_result_writes_artifact_with_explicit_gaps(tmp_path):
    db = make_executor_db(tmp_path)
    workflow_id = _add_wf3b(db)
    state = _persist_state()
    engine = _engine(db, _persist_results(workflow_id))
    wf = {"id": workflow_id, "project_id": "project-1", "workflow_type": WF3B_WORKFLOW_TYPE}

    artifact_id = engine._persist_wf3b_background_result(wf, state)

    row = db.fetchone("SELECT * FROM artifacts WHERE id=?", (artifact_id,))
    assert row["artifact_type"] == "TOPIC_BACKGROUND_RESULT"
    assert row["prompt_id"] == "P-ONLINE-RESULT-IMPORT-CRITIC"
    assert row["version"] == 1
    assert row["status"] == "SUFFICIENT"
    assert row["security_level"] == "INTERNAL"

    payload = json.loads(row["content_json"])
    assert payload["schema_version"] == "1.0"
    assert payload["project_id"] == "project-1"
    assert payload["workflow_id"] == workflow_id
    assert payload["topic"] == "智慧水务"
    assert payload["topic_id"] == "topic-x"
    assert payload["topic_origin"] == "WORKFLOW_OPTIONS"
    assert payload["required_dimensions"] == ["APPLICATION_SCENARIO", "POLICY_STANDARD_AND_PROGRAM"]
    assert payload["background_dimensions"]["APPLICATION_SCENARIO"]["status"] == "COVERED"
    assert payload["background_dimensions"]["POLICY_STANDARD_AND_PROGRAM"]["status"] == "GAP"
    assert [card["claim_id"] for card in payload["background_cards"]] == ["c1"]
    assert payload["completion_semantics"] == "COMPLETED_WITH_BACKGROUND_GAPS"
    assert payload["accepted_claim_ids"] == ["c1"]
    assert payload["source_catalog"] == [{"source_id": "s1"}]
    assert payload["archive_manifest"] == "manifest.json"

    assert state["wf3b_background_result_artifact_id"] == artifact_id
    assert state["completion_semantics"] == "COMPLETED_WITH_BACKGROUND_GAPS"
    assert state["background_research_sufficiency"]["status"] == "SUFFICIENT"

    audits = db.fetchall(
        "SELECT event_type,object_id,metadata_json FROM audit_events WHERE event_type='WF3B_BACKGROUND_RESULT_PERSISTED'"
    )
    assert [row["object_id"] for row in audits] == [artifact_id]
    metadata = json.loads(audits[0]["metadata_json"])
    assert metadata["workflow_id"] == workflow_id
    assert metadata["completion_semantics"] == "COMPLETED_WITH_BACKGROUND_GAPS"
    assert metadata["card_count"] == 1
    assert metadata["gap_count"] == 1


def test_persist_wf3b_completion_semantics_completed_only_without_gaps(tmp_path):
    db = make_executor_db(tmp_path)
    workflow_id = _add_wf3b(db)
    state = _persist_state()
    state["options"]["required_dimensions"] = ["APPLICATION_SCENARIO"]
    engine = _engine(db, _persist_results(workflow_id))
    wf = {"id": workflow_id, "project_id": "project-1", "workflow_type": WF3B_WORKFLOW_TYPE}

    artifact_id = engine._persist_wf3b_background_result(wf, state)
    payload = json.loads(db.fetchone("SELECT content_json FROM artifacts WHERE id=?", (artifact_id,))["content_json"])
    assert payload["completion_semantics"] == "COMPLETED"
    assert payload["background_gaps"] == []

    # A degraded sufficiency alone also yields explicit gap semantics.
    workflow_id2 = _add_wf3b(db)
    degraded = _persist_state()
    degraded["options"]["required_dimensions"] = ["APPLICATION_SCENARIO"]
    degraded["background_search_results"]["research_sufficiency"]["status"] = "DEGRADED"
    engine2 = _engine(db, _persist_results(workflow_id2))
    artifact_id2 = engine2._persist_wf3b_background_result(
        {"id": workflow_id2, "project_id": "project-1", "workflow_type": WF3B_WORKFLOW_TYPE},
        degraded,
    )
    payload2 = json.loads(db.fetchone("SELECT content_json FROM artifacts WHERE id=?", (artifact_id2,))["content_json"])
    assert payload2["completion_semantics"] == "COMPLETED_WITH_BACKGROUND_GAPS"


def test_persist_wf3b_versions_increment_and_repeat_call_is_idempotent(tmp_path):
    db = make_executor_db(tmp_path)
    workflow_id = _add_wf3b(db)
    now = utc_now()
    db.execute(
        """INSERT INTO artifacts(id,project_id,workflow_id,artifact_type,prompt_id,version,status,security_level,context_hash,content_json,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            new_id("artifact"),
            "project-1",
            workflow_id,
            "TOPIC_BACKGROUND_RESULT",
            "P-ONLINE-RESULT-IMPORT-CRITIC",
            3,
            "SUFFICIENT",
            "INTERNAL",
            "hash",
            "{}",
            now,
        ),
    )
    state = _persist_state()
    engine = _engine(db, _persist_results(workflow_id))
    wf = {"id": workflow_id, "project_id": "project-1", "workflow_type": WF3B_WORKFLOW_TYPE}

    artifact_id = engine._persist_wf3b_background_result(wf, state)
    row = db.fetchone("SELECT version FROM artifacts WHERE id=?", (artifact_id,))
    assert row["version"] == 4

    repeated = engine._persist_wf3b_background_result(wf, state)
    assert repeated == artifact_id
    count = db.fetchone(
        "SELECT COUNT(*) AS n FROM artifacts WHERE workflow_id=? AND artifact_type='TOPIC_BACKGROUND_RESULT'",
        (workflow_id,),
    )["n"]
    assert count == 2
    audits = db.fetchall(
        "SELECT id FROM audit_events WHERE event_type='WF3B_BACKGROUND_RESULT_PERSISTED'"
    )
    assert len(audits) == 1


def test_persist_wf3b_ignores_other_workflow_types(tmp_path):
    db = make_executor_db(tmp_path)
    engine = _engine(db)
    wf = {"id": "wf-other", "project_id": "project-1", "workflow_type": "WF-3_HYBRID_ONLINE_ASSIST"}
    assert engine._persist_wf3b_background_result(wf, {}) is None
    assert db.fetchone("SELECT COUNT(*) AS n FROM artifacts")["n"] == 0


def test_wf3b_requires_completed_wf1(tmp_path):
    db = Database(tmp_path / "state.db")
    project_id = add_project(db)
    engine = _engine(db)

    assert engine._required_workflow_types(project_id, WF3B_WORKFLOW_TYPE, {}) == [
        "WF-1_PROJECT_INTAKE"
    ]
    created = engine.start(project_id, WF3B_WORKFLOW_TYPE, {"topic": "智慧水务"})
    assert created["status"] == "WAITING_PREREQUISITE"
    assert "WF-1" in created["state"]["last_error"]


def test_wf3b_start_without_resolvable_topic_waits_for_prerequisite(tmp_path):
    db = Database(tmp_path / "state.db")
    project_id = add_project(db)
    add_workflow(db, project_id, "WF-1_PROJECT_INTAKE")
    engine = _engine(db)

    created = engine.start(project_id, WF3B_WORKFLOW_TYPE, {})
    assert created["status"] == "WAITING_PREREQUISITE"
    assert "topic" in created["state"]["last_error"]


def test_wf3b_waiting_workflow_accepts_topic_and_keeps_same_identity(tmp_path):
    db = Database(tmp_path / "state.db")
    project_id = add_project(db)
    add_workflow(db, project_id, "WF-1_PROJECT_INTAKE")
    engine = _engine(db)
    created = engine.start(project_id, WF3B_WORKFLOW_TYPE, {})

    updated = engine.provide_wf3b_topic(created["id"], "智慧水务应用背景")

    assert updated["id"] == created["id"]
    assert updated["status"] == "WAITING_PREREQUISITE"
    assert updated["state"]["options"]["topic"] == "智慧水务应用背景"
    assert updated["state"]["options"]["topic_origin"] == "WORKFLOW_OPTIONS"
    assert updated["state"]["options"]["topic_id"] == wf3b_topic_id(
        project_id, "智慧水务应用背景"
    )
    event = db.fetchone(
        "SELECT event_type FROM audit_events WHERE object_id=? ORDER BY created_at DESC LIMIT 1",
        (created["id"],),
    )
    assert event["event_type"] == "WF3B_TOPIC_SUPPLIED"


def test_wf3b_topic_input_rejects_non_wf3b_workflow(tmp_path):
    db = Database(tmp_path / "state.db")
    project_id = add_project(db)
    workflow_id = add_workflow(db, project_id, "WF-1_PROJECT_INTAKE")
    engine = _engine(db)

    with pytest.raises(ValueError, match="WF-3B_TOPIC_BACKGROUND_RESEARCH"):
        engine.provide_wf3b_topic(workflow_id, "智慧水务")


def test_wf3b_start_normalizes_options_and_freezes_topic(tmp_path):
    db = Database(tmp_path / "state.db")
    project_id = add_project(db)
    wf1 = add_workflow(db, project_id, "WF-1_PROJECT_INTAKE")
    engine = _engine(db)

    created = engine.start(
        project_id,
        WF3B_WORKFLOW_TYPE,
        {"topic": "智慧水务", "background_dimensions": ["research_significance", "APPLICATION_SCENARIO"]},
    )
    assert created["status"] == "RUNNING"
    options = created["state"]["options"]
    assert options["topic"] == "智慧水务"
    assert options["topic_origin"] == "WORKFLOW_OPTIONS"
    assert options["topic_id"] == wf3b_topic_id(project_id, "智慧水务")
    assert options["required_dimensions"] == ["APPLICATION_SCENARIO", "RESEARCH_SIGNIFICANCE"]
    assert created["state"]["prerequisite_workflow_ids"] == {"WF-1_PROJECT_INTAKE": wf1}


def test_wf3b_start_resolves_topic_from_bound_wf1_project_definition(tmp_path):
    db = Database(tmp_path / "state.db")
    project_id = add_project(db)
    wf1 = add_workflow(db, project_id, "WF-1_PROJECT_INTAKE")
    results = {
        ("P-PROJECT-DEFINITION-EXTRACT", wf1): {
            "project_definition": {"project_title": "农村污水智慧监测"}
        }
    }
    engine = _engine(db, results)

    created = engine.start(project_id, WF3B_WORKFLOW_TYPE, {})
    assert created["status"] == "RUNNING"
    options = created["state"]["options"]
    assert options["topic"] == "农村污水智慧监测"
    assert options["topic_origin"] == "WF1_PROJECT_DEFINITION"
    assert options["topic_id"] == wf3b_topic_id(project_id, "农村污水智慧监测")
    assert created["state"]["prerequisite_workflow_ids"] == {"WF-1_PROJECT_INTAKE": wf1}


class WF3BRuntime:
    def _required_workflow_types(self, project_id, workflow_type, options):
        del project_id, options
        return {
            "WF-1_PROJECT_INTAKE": [],
            WF3B_WORKFLOW_TYPE: ["WF-1_PROJECT_INTAKE"],
        }[workflow_type]


def test_rebuild_completed_wf1_cascades_to_wf3b_with_frozen_branch_remap(tmp_path):
    db = Database(tmp_path / "state.db")
    project_id = add_project(db)
    wf1 = add_workflow(db, project_id, "WF-1_PROJECT_INTAKE")
    wf3b = add_workflow(
        db,
        project_id,
        WF3B_WORKFLOW_TYPE,
        prerequisites={"WF-1_PROJECT_INTAKE": wf1},
    )
    workflows = CompletingWorkflows(db)
    workflows.runtime = WF3BRuntime()
    service = WorkflowLifecycleService(db, workflows)

    operation = asyncio.run(service.rebuild(wf1))
    assert operation["status"] == "COMPLETED"
    nodes = operation["plan"]["nodes"]
    assert [node["source_workflow_id"] for node in nodes] == [wf1, wf3b]
    new1, new3b = [node["new_workflow_id"] for node in nodes]

    # Completed history is immutable.
    assert db.fetchone("SELECT status FROM workflows WHERE id=?", (wf1,))["status"] == "COMPLETED"
    assert db.fetchone("SELECT status FROM workflows WHERE id=?", (wf3b,))["status"] == "COMPLETED"
    assert workflows.get(new3b)["state"]["prerequisite_workflow_ids"] == {
        "WF-1_PROJECT_INTAKE": new1
    }

    lineage = db.fetchall(
        "SELECT parent_workflow_id,child_workflow_id,relation_type FROM workflow_lineage WHERE operation_id=? ORDER BY created_at,id",
        (operation["id"],),
    )
    assert [
        (row["parent_workflow_id"], row["child_workflow_id"], row["relation_type"])
        for row in lineage
    ] == [
        (wf1, new1, "RERUN_OF"),
        (wf3b, new3b, "REBUILD_OF"),
    ]


def test_wf3b_workflow_preflight_covers_online_public_model_and_search(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_RUNTIME_MODE", "SIMULATED")
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PROMPT_PACK_DIR", str(ROOT / "prompt_pack"))
    settings = Settings.load()
    pack = PromptPack(settings.prompt_pack_dir)
    db = Database(settings.db_path)
    project_id = new_id("project")
    now = utc_now()
    config = {
        "internet_access_allowed": True,
        "anonymized_external_processing_allowed": True,
        "allowed_model_endpoint_ids": ["offline-primary", "online-public-primary"],
    }
    db.execute(
        "INSERT INTO projects(id,name,description,security_level,config_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        (project_id, "预检项目", "", "INTERNAL", json.dumps(config), now, now),
    )
    preflight = RuntimeDependencyPreflight(settings, pack, db)

    report = preflight.workflow_report(project_id, WF3B_WORKFLOW_TYPE)
    check_names = {check["name"] for check in report.checks}
    assert "MODEL_OFFLINE_LOCAL" in check_names
    assert "MODEL_ONLINE_PUBLIC" in check_names
    assert "PUBLIC_SEARCH" in check_names

    wf1_report = preflight.workflow_report(project_id, "WF-1_PROJECT_INTAKE")
    wf1_check_names = {check["name"] for check in wf1_report.checks}
    assert "MODEL_OFFLINE_LOCAL" in wf1_check_names
    assert "MODEL_ONLINE_PUBLIC" not in wf1_check_names
    assert "PUBLIC_SEARCH" not in wf1_check_names


def test_wf3b_simulated_end_to_end_persists_topic_background_result(tmp_path, monkeypatch):
    settings, pack, db, builder, executor, engine = _runtime(tmp_path, monkeypatch)
    project_id = _project(db)

    async def finish():
        intake = await _finish(engine, project_id, "WF-1_PROJECT_INTAKE")
        assert intake["status"] == "COMPLETED", intake["state"].get("last_error")
        # The current WF-1 project definition artifact (v2 items/relations) carries
        # no project_title/problem_statement, so the topic is pinned explicitly.
        wf = engine.start(project_id, WF3B_WORKFLOW_TYPE, {"topic": "后勤保障智能体应用背景"})
        assert wf["status"] == "RUNNING", wf["state"].get("last_error")
        for _ in range(500):
            wf = await engine.advance(wf["id"])
            if wf["status"] == "WAITING_GATE":
                gate = next(
                    gate
                    for gate in engine.list_gates(workflow_id=wf["id"])
                    if gate["status"] == "OPEN"
                )
                action = "APPROVE" if "APPROVE" in gate["allowed_actions"] else "CONFIRM"
                engine.decide_gate(
                    gate["id"],
                    action=action,
                    decided_by="pytest",
                    decided_role=gate["required_role"],
                )
                continue
            if should_pause_automatic_advancement(wf["status"]):
                break
        assert wf["status"] == "COMPLETED", wf["state"].get("last_error")
        return wf

    wf = asyncio.run(finish())
    options = wf["state"]["options"]
    assert options["required_dimensions"] == list(BACKGROUND_DIMENSIONS)
    assert options["topic"] == "后勤保障智能体应用背景"
    assert options["topic_origin"] == "WORKFLOW_OPTIONS"

    artifact = db.fetchone(
        "SELECT * FROM artifacts WHERE project_id=? AND artifact_type='TOPIC_BACKGROUND_RESULT'",
        (project_id,),
    )
    assert artifact is not None
    payload = json.loads(artifact["content_json"])
    assert payload["schema_version"] == "1.0"
    assert payload["workflow_id"] == wf["id"]
    assert payload["topic_id"].startswith("topic-")
    assert payload["required_dimensions"] == list(BACKGROUND_DIMENSIONS)
    assert set(payload["background_dimensions"]) == set(BACKGROUND_DIMENSIONS)
    assert payload["completion_semantics"] in {"COMPLETED", "COMPLETED_WITH_BACKGROUND_GAPS"}
    assert payload["completion_semantics"] == wf["state"]["completion_semantics"]

    audit = db.fetchone(
        "SELECT id FROM audit_events WHERE project_id=? AND event_type='WF3B_BACKGROUND_RESULT_PERSISTED'",
        (project_id,),
    )
    assert audit is not None
