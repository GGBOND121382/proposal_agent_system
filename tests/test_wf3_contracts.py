from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.executor import PromptExecutionError, PromptExecutor
from app.contract_registry import normalize_exact_null_literals
from app.context_base import ContextBuilder
from app.llm import ProviderError
from app.output_integrity import build_trusted_source_catalog
from app.pack import PromptPack
from app.runtime_failures import ProviderFailureKind
from app.runtime_executor import RuntimePromptExecutor
from app.util import utc_now
from app.wf3_contracts import (
    WF3_FIELD_OWNERSHIP,
    WF3_PROVENANCE_PAYLOAD_FIELDS,
    WF3_PROVIDER_REQUEST_CHAR_BUDGETS,
    canonicalize_wf3_critic_control,
    canonicalize_wf3_machine_fields,
    canonicalize_wf3_producer_status,
    compact_wf3_research_envelope,
    compare_public_search_candidates,
    compare_wf3_plan_candidates,
    compare_wf3_synthesis_candidates,
    wf3_critic_routing_report,
    wf3_output_semantic_errors,
    wf3_provider_request_budget_report,
)
from app.workflows import WorkflowEngine
from tests.test_runtime_recovery import SequencePromptExecutor, make_executor_db


FIXTURE = Path(__file__).parent / "fixtures" / "wf3_historical_regressions_20260826.json"
PACK = PromptPack(Path(__file__).resolve().parents[1] / "prompt_pack")
WF3_PROMPT_IDS = (
    "P-SAFE-ONLINE-PACKAGE",
    "P-SAFE-ONLINE-PACKAGE-CRITIC",
    "P-PUBLIC-RESEARCH-PLAN",
    "P-PUBLIC-RESEARCH-SYNTHESIS",
    "P-PUBLIC-RESEARCH-CRITIC",
    "P-ONLINE-RESULT-IMPORT-CRITIC",
)


def _cases():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]


def test_wf3_historical_inventory_names_and_hashes_are_immutable():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    inventory = fixture["historical_run_inventory"]
    assert [item["run_id"] for item in inventory] == [
        "run-07fc40992d0946f9",
        "run-add7d9fedb2a4483",
        "run-65a65c0e963a425e",
        "run-ef65772880274b45",
        "run-de8115e860304095",
        "run-c6168c0320354a95",
        "run-9adaf66163eb4db6",
        "run-ca78064053684e54",
    ]
    assert all(len(item["input_sha256"]) == 64 for item in inventory)
    assert all(len(item["output_sha256"]) == 64 for item in inventory)
    assert set(item["prompt_id"] for item in inventory) == set(WF3_PROMPT_IDS)


def test_recorded_wf3_requests_fit_node_budgets_with_regression_headroom():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    measurements = fixture["request_size_baseline"]
    measurements.pop("measurement", None)
    assert set(measurements) == set(WF3_PROMPT_IDS)
    for prompt_id, metrics in measurements.items():
        assert metrics["provider_visible_chars"] == (
            metrics["system_prompt_chars"] + metrics["provider_envelope_chars"]
        )
        assert metrics["provider_visible_chars"] < WF3_PROVIDER_REQUEST_CHAR_BUDGETS[prompt_id]


def test_historical_advisory_only_synthesis_does_not_block():
    case = _cases()["advisory_only_synthesis"]
    output, report = canonicalize_wf3_producer_status(
        "P-PUBLIC-RESEARCH-SYNTHESIS", case["output"]
    )

    assert case["run_id"] == "run-ef65772880274b45"
    assert case["input_sha256"] == "88416b1cb85e491d3cb29f79a896559371049714349d09ffd79813ef5251513d"
    assert case["output_sha256"] == "40a3e12cfd7a34bd55678332c7d4db7039428aa73feda4699fc07474e8062934"
    assert len(case["output"]["findings"]) == case["historical_counts"]["findings"]
    assert len(case["output"]["unresolved_items"]) == case["historical_counts"]["unresolved_items"]
    assert len(case["output"]["user_questions"]) == case["historical_counts"]["user_questions"]
    assert output["status"] == "PASS"
    assert report["reason"] == "ADVISORY_ONLY"
    assert all(item["blocking"] is False for item in output["findings"])


def test_wf3_producer_pass_with_blocker_is_deterministically_revise():
    output, report = canonicalize_wf3_producer_status(
        "P-PUBLIC-RESEARCH-PLAN",
        {
            "status": "PASS",
            "findings": [
                {
                    "code": "MISSING_SCOPE",
                    "blocking": True,
                    "repairable": True,
                    "suggested_route": "ORIGINAL_PRODUCER",
                }
            ],
            "unresolved_items": [],
            "user_questions": [],
            "warnings": [],
        },
    )

    assert output["status"] == "REVISE"
    assert report["reason"] == "EXECUTABLE_BLOCKING_CONTENT_ITEM"


def test_wf3_unroutable_blocker_is_block_not_fake_revise():
    output, report = canonicalize_wf3_producer_status(
        "P-PUBLIC-RESEARCH-PLAN",
        {
            "status": "REVISE",
            "findings": [],
            "unresolved_items": [{"blocking": True, "required_action": "unknown"}],
            "user_questions": [],
            "warnings": [],
        },
    )
    assert output["status"] == "BLOCK"
    assert report["reason"] == "UNROUTABLE_BLOCKING_CONTENT_ITEM"


def test_historical_critic_retrieval_findings_never_route_to_synthesis():
    case = _cases()["critic_cross_capability"]
    report = wf3_critic_routing_report(case["output"])

    assert case["run_id"] == "run-de8115e860304095"
    assert case["input_sha256"] == "5399c8efb6a7d5b95148c081d19f369ad1a8db36ece8b2b9d38c300e10ddfe09"
    assert case["output_sha256"] == "1b21481dd81b3e6796dd1ff5187dd1315c693b03da0c6431b8209ab42ac222ca"
    assert report["blocking_finding_count"] == 2
    assert report["route_counts"]["RETRIEVAL"] == 2
    assert report["route_counts"]["SYNTHESIS"] == 0
    assert report["has_non_synthesis_route"] is True


def _search_candidate(*, queries, source_ids, uncovered=(), dimensions=(), issues=()):
    return {
        "queries": list(queries),
        "sources": [{"source_id": source_id} for source_id in source_ids],
        "source_catalog": [
            {"source_id": source_id, "authority_rank": 90}
            for source_id in source_ids
        ],
        "coverage": {
            "by_query": {
                query: {"source_count": 0 if query in uncovered else 1}
                for query in queries
            },
            "uncovered_queries": list(uncovered),
            "dimensions": {
                name: {"status": "PASS"} for name in dimensions
            },
        },
        "issues": list(issues),
        "archive_verification": {"status": "PASS"},
    }


def test_search_candidate_cannot_silently_drop_queries_or_coverage():
    accepted = _search_candidate(
        queries=["q1", "q2", "q3"],
        source_ids=["s1", "s2", "s3"],
        dimensions=["recent_work", "comparable_baselines"],
    )
    candidate = _search_candidate(
        queries=["q1", "q2"],
        source_ids=["n1", "n2"],
        dimensions=["recent_work"],
    )

    comparison = compare_public_search_candidates(accepted, candidate)

    assert comparison["accepted"] is False
    assert "QUERY_SET_SHRANK" in comparison["regressions"]
    assert "COVERAGE_DIMENSION_REGRESSED" in comparison["regressions"]


def test_search_candidate_may_remove_bad_source_when_coverage_is_preserved():
    bad_issue = {"type": "SOURCE_CONFLICT", "code": "WITHDRAWN_SOURCE"}
    accepted = _search_candidate(
        queries=["q1", "q2"],
        source_ids=["s1", "s2", "bad"],
        dimensions=["recent_work", "comparable_baselines"],
        issues=[bad_issue],
    )
    candidate = _search_candidate(
        queries=["q1", "q2"],
        source_ids=["s1", "s2"],
        dimensions=["recent_work", "comparable_baselines"],
    )

    comparison = compare_public_search_candidates(accepted, candidate)

    assert comparison["accepted"] is True
    assert "critical_issue_count" in comparison["improvements"]


def test_model_projection_deduplicates_passage_source_text_only():
    envelope = {
        "payload": {
            "retrieved_sources": [
                {
                    "source_id": "s1",
                    "source_type": "PUBLIC_SOURCE",
                    "quoted_text": "duplicate excerpt",
                    "authority_rank": 90,
                }
            ],
            "extracted_passages": [
                {
                    "passage_id": "p1",
                    "source_ref": {
                        "source_id": "s1",
                        "source_type": "PUBLIC_SOURCE",
                        "quoted_text": "duplicate excerpt",
                        "source_hash": "abc",
                    },
                    "text": "complete evidence text",
                }
            ],
        }
    }

    compact, report = compact_wf3_research_envelope(
        "P-PUBLIC-RESEARCH-SYNTHESIS", envelope
    )

    assert compact["payload"]["extracted_passages"][0]["text"] == "complete evidence text"
    assert compact["payload"]["extracted_passages"][0]["source_ref"] == {
        "source_id": "s1",
        "source_type": "PUBLIC_SOURCE",
        "source_hash": "abc",
    }
    assert "quoted_text" not in compact["payload"]["retrieved_sources"][0]
    assert envelope["payload"]["retrieved_sources"][0]["quoted_text"] == "duplicate excerpt"
    assert report["quality_guard_uses_full_context"] is True

    provider, provider_report = PromptExecutor._prepare_provider_envelope(compact)
    provider_ref = provider["payload"]["extracted_passages"][0]["source_ref"]
    assert "source_hash" not in provider_ref
    assert "quoted_text" not in provider["payload"]["retrieved_sources"][0]
    assert provider_report["removed_hash_fields"]["source_hash"] == 1


def test_live_safe_package_historical_gate_schema_and_source_path_aliases_normalize():
    """Replay the two exact contract shapes seen in the 2026-08-26 LIVE run."""

    output = copy.deepcopy(PACK.replay_output("P-SAFE-ONLINE-PACKAGE", "normal"))
    output["status"] = "NEED_USER_INPUT"
    output["user_questions"] = [
        {
            "question_id": "provider-authored-id",
            "question_type": "MISSING_INFORMATION",
            "question": "请提供允许公开检索的主题及补充说明。",
            "reason": "允许主题尚未确认。",
            "target_paths": ["payload.allowed_topics"],
            "answer_schema": {
                "type": "OBJECT",
                "properties": {
                    "topics": {"type": "ARRAY", "items": {"type": "STRING"}}
                },
                "required": ["topics"],
            },
            "blocking": True,
            "priority": "P0",
        }
    ]
    output["source_refs"] = [
        {
            "source_id": "payload.source_items[0]",
            "source_type": "CURRENT_PROPOSAL",
            "authority_rank": 1,
            "security_level": "INTERNAL",
        }
    ]
    envelope = {
        "security_context": {"input_max_security_level": "INTERNAL"},
        "payload": {
            "source_items": [
                {
                    "object_id": "doc-46545e6862d5497b",
                    "object_type": "SOURCE_DOCUMENT:CURRENT_PROPOSAL",
                    "object_hash": "5" * 64,
                    "security_level": "INTERNAL",
                }
            ]
        },
    }
    executor = object.__new__(PromptExecutor)
    executor.pack = PACK
    executor.db = None

    normalized = executor._normalize_output(
        "P-SAFE-ONLINE-PACKAGE", output, envelope
    )

    source_entry = next(
        item
        for item in build_trusted_source_catalog(envelope)
        if item["object_path"] == "payload.source_items"
    )
    assert normalized["source_refs"][0]["source_id"] == source_entry["source_id"]
    assert normalized["source_refs"][0]["authority_rank"] == source_entry["authority_rank"]
    assert normalized["user_questions"][0]["answer_schema"] == {"type": "STRING"}
    assert PACK.validate("P-SAFE-ONLINE-PACKAGE", "output", normalized) == []
    assert any("SYSTEM_SAFE_PACKAGE_SOURCE_NORMALIZATION" in item for item in normalized["warnings"])


def test_wf3_contract_retry_feedback_is_exact_bounded_and_phase_scoped():
    provider = ProviderError(
        "safe package contract failed",
        kind=ProviderFailureKind.RESPONSE_SHAPE,
        phase="output_schema_validation",
        validation_errors=[
            "/user_questions/0/answer_schema: unexpected required",
            "/source_refs/1/source_id: unknown source",
        ],
    )
    wrapped = PromptExecutionError(
        "prompt failed", validation_errors=list(provider.validation_errors)
    )
    wrapped.__cause__ = provider

    feedback = WorkflowEngine._contract_retry_feedback(
        "P-SAFE-ONLINE-PACKAGE", wrapped
    )

    assert feedback == provider.validation_errors
    rendered = PromptExecutor._contract_retry_feedback_prompt(feedback)
    assert "上一轮候选已被拒绝" in rendered
    assert "/source_refs/1/source_id" in rendered
    assert WorkflowEngine._contract_retry_feedback(
        "P-PUBLIC-RESEARCH-PLAN", TimeoutError("timeout")
    ) == []


@pytest.mark.parametrize("prompt_id", WF3_PROMPT_IDS)
def test_each_wf3_model_node_carries_exact_contract_error_into_bounded_retry(prompt_id):
    provider = ProviderError(
        "candidate failed",
        kind=ProviderFailureKind.RESPONSE_SHAPE,
        phase="output_schema_validation",
        validation_errors=["/result/items/0/id: unknown trusted identity"],
    )
    wrapped = PromptExecutionError(
        "prompt failed", validation_errors=list(provider.validation_errors)
    )
    wrapped.__cause__ = provider
    assert WorkflowEngine._contract_retry_feedback(prompt_id, wrapped) == [
        "/result/items/0/id: unknown trusted identity"
    ]


def test_wf3_provider_retry_passes_previous_validation_errors_to_next_attempt(tmp_path):
    db = make_executor_db(tmp_path)
    state = {
        "workflow_type": "WF-3_HYBRID_ONLINE_ASSIST",
        "options": {
            "provider_retry_limit": 1,
            "provider_retry_base_delay_seconds": 0,
        },
        "step_results": {},
    }
    db.execute(
        "INSERT INTO workflows(id,project_id,workflow_type,status,current_step,state_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (
            "wf-wf3-feedback",
            "project-1",
            "WF-3_HYBRID_ONLINE_ASSIST",
            "RUNNING",
            0,
            json.dumps(state),
            utc_now(),
            utc_now(),
        ),
    )
    wf = {
        "id": "wf-wf3-feedback",
        "project_id": "project-1",
        "workflow_type": "WF-3_HYBRID_ONLINE_ASSIST",
        "status": "RUNNING",
        "current_step": 0,
        "state": state,
    }
    provider = ProviderError(
        "safe package output contract failed",
        kind=ProviderFailureKind.RESPONSE_SHAPE,
        phase="output_structure_validation",
        validation_errors=["/source_refs/1/source_id: unknown source"],
    )
    first = PromptExecutionError(
        "prompt execution failed", validation_errors=list(provider.validation_errors)
    )
    first.__cause__ = provider
    success = {"run_id": "run-wf3-ok", "status": "PASS", "output": {"status": "PASS"}}
    executor = SequencePromptExecutor([first, success])
    engine = WorkflowEngine(db, SimpleNamespace(), SimpleNamespace(), executor, SimpleNamespace())

    result = asyncio.run(
        engine._execute_prompt_with_provider_retry(
            wf,
            state,
            prompt_id="P-SAFE-ONLINE-PACKAGE",
            envelope={"payload": {}},
        )
    )

    assert result is success
    assert "contract_retry_feedback" not in executor.call_kwargs[0]
    assert executor.call_kwargs[1]["contract_retry_feedback"] == provider.validation_errors
    assert "contract_retry_feedback" not in state["provider_call_cycles"]["0:P-SAFE-ONLINE-PACKAGE"]


def test_wf3_nullable_fields_normalize_only_exact_null_literal():
    schema = PACK.inlined_schema("P-SAFE-ONLINE-PACKAGE", "output")
    base = copy.deepcopy(PACK.replay_output("P-SAFE-ONLINE-PACKAGE", "normal"))
    for raw, expected, changed in (
        ("null", None, 1),
        ("NULL", "NULL", 0),
        (" null ", " null ", 0),
    ):
        candidate = copy.deepcopy(base)
        candidate["result"]["valid_until"] = raw
        normalized, report = normalize_exact_null_literals(candidate, schema)
        assert normalized["result"]["valid_until"] == expected
        assert report["normalized_count"] == changed

    candidate = copy.deepcopy(base)
    candidate["result"]["task_description"] = "the null hypothesis remains"
    normalized, report = normalize_exact_null_literals(candidate, schema)
    assert normalized["result"]["task_description"] == "the null hypothesis remains"
    assert report["normalized_count"] == 0


@pytest.mark.parametrize(
    ("prompt_id", "mutate", "read"),
    [
        (
            "P-SAFE-ONLINE-PACKAGE",
            lambda value: value["result"].__setitem__("valid_until", "null"),
            lambda value: value["result"]["valid_until"],
        ),
        (
            "P-PUBLIC-RESEARCH-PLAN",
            lambda value: value["result"].__setitem__("time_scope", "null"),
            lambda value: value["result"]["time_scope"],
        ),
        (
            "P-PUBLIC-RESEARCH-SYNTHESIS",
            lambda value: value["result"].__setitem__(
                "claims",
                [
                    {
                        "claim_id": "c1",
                        "claim_text": "semantic claim",
                        "claim_type": "PUBLIC_CLAIM",
                        "subject_id": "null",
                        "temporal_status": "CURRENT",
                        "qualifiers": [],
                        "numeric_values": [],
                        "source_refs": [],
                        "knowledge_status": "CONFIRMED",
                        "security_level": "PUBLIC",
                    }
                ],
            ),
            lambda value: value["result"]["claims"][0]["subject_id"],
        ),
    ],
)
def test_wf3_business_nullable_fields_normalize_exact_null_only(prompt_id, mutate, read):
    candidate = copy.deepcopy(PACK.replay_output(prompt_id, "normal"))
    mutate(candidate)
    normalized, report = normalize_exact_null_literals(
        candidate, PACK.inlined_schema(prompt_id, "output")
    )
    assert read(normalized) is None
    assert report["normalized_count"] == 1


def test_wf3_field_ownership_covers_six_model_nodes_and_search_without_schema_changes():
    assert set(WF3_FIELD_OWNERSHIP) == {
        "P-SAFE-ONLINE-PACKAGE",
        "P-SAFE-ONLINE-PACKAGE-CRITIC",
        "P-PUBLIC-RESEARCH-PLAN",
        "PUBLIC-RESEARCH-SEARCH",
        "P-PUBLIC-RESEARCH-SYNTHESIS",
        "P-PUBLIC-RESEARCH-CRITIC",
        "P-ONLINE-RESULT-IMPORT-CRITIC",
    }
    assert all(
        set(classes) == {
            "MODEL_SEMANTIC",
            "RUNTIME_DERIVED",
            "INPUT_COPIED",
            "GUARD_CONTROLLED",
        }
        for classes in WF3_FIELD_OWNERSHIP.values()
    )


@pytest.mark.parametrize("prompt_id", WF3_PROMPT_IDS)
def test_wf3_prompts_delegate_machine_fields_to_semantic_runtime_contract(
    prompt_id,
):
    prompt = PACK.prompt_text(prompt_id)
    entry = PACK.entry(prompt_id)
    assert entry.get("model_contract_mode") == "SEMANTIC"
    assert "用户问题" in prompt or "用户问题" in PACK.shared_prompt_for(prompt_id)
    assert "Hash" in prompt or "Hash" in PACK.shared_prompt_for(prompt_id)
    assert "先验证每个对象的ID、版本、Hash与安全标签" not in prompt
    assert "输出中新增的候选ID必须唯一" not in prompt
    assert "Schema错误、引用错误、Hash过期和安全环境不匹配" not in prompt


def test_wf3_synthesis_prompt_preserves_only_visible_claim_source_selection():
    prompt = PACK.prompt_text("P-PUBLIC-RESEARCH-SYNTHESIS")
    assert "每个 claim 必须列出直接支撑它的 `source_ids`" in prompt
    assert "只能使用 `evidence_passages` 中可见的 source_id" in prompt
    assert "不能用模型记忆补造" in prompt


def test_machine_ids_are_stable_when_model_reorders_semantic_rows():
    def candidate(claims):
        return {
            "result": {"claims": claims},
            "findings": [],
            "unresolved_items": [],
            "user_questions": [],
        }

    a = {"claim_text": "A", "claim_type": "PUBLIC_CLAIM", "subject_id": None,
         "temporal_status": "CURRENT", "qualifiers": [], "numeric_values": [],
         "source_refs": [], "knowledge_status": "CONFIRMED", "security_level": "PUBLIC"}
    b = {**a, "claim_text": "B"}
    first, _ = canonicalize_wf3_machine_fields(
        "P-PUBLIC-RESEARCH-SYNTHESIS", candidate([a, b]), {}
    )
    second, _ = canonicalize_wf3_machine_fields(
        "P-PUBLIC-RESEARCH-SYNTHESIS", candidate([b, a]), {}
    )
    assert {
        item["claim_text"]: item["claim_id"] for item in first["result"]["claims"]
    } == {
        item["claim_text"]: item["claim_id"] for item in second["result"]["claims"]
    }


def test_critic_control_never_allows_advisory_only_revise_or_blocking_pass():
    advisory = {
        "status": "REVISE",
        "result": {"verdict": "REVISE"},
        "findings": [{"blocking": False}],
        "unresolved_items": [],
        "user_questions": [],
        "warnings": [],
    }
    normalized, report = canonicalize_wf3_critic_control(
        "P-PUBLIC-RESEARCH-CRITIC", advisory
    )
    assert normalized["status"] == "PASS"
    assert normalized["result"]["verdict"] == "ACCEPT_FOR_IMPORT_REVIEW"
    assert report["after"] == "PASS"

    blocking = copy.deepcopy(advisory)
    blocking["status"] = "PASS"
    blocking["findings"] = [
        {
            "blocking": True,
            "suggested_route": "ORIGINAL_PRODUCER",
            "target_type": "CLAIM",
            "target_path_or_span": "payload.synthesis_candidate.claims",
        }
    ]
    normalized, _ = canonicalize_wf3_critic_control(
        "P-PUBLIC-RESEARCH-CRITIC", blocking
    )
    assert normalized["status"] == "REVISE"
    assert normalized["result"]["verdict"] == "REVISE"


def test_wf3_request_budgets_are_node_specific_and_include_retry_feedback():
    plan = wf3_provider_request_budget_report(
        "P-PUBLIC-RESEARCH-PLAN", "x" * 100, {"payload": {"query": "y" * 100}}
    )
    synthesis = wf3_provider_request_budget_report(
        "P-PUBLIC-RESEARCH-SYNTHESIS", "x" * 100, {"payload": {}}
    )
    assert plan["within_budget"] is True
    assert plan["limit_chars"] < synthesis["limit_chars"]
    over = wf3_provider_request_budget_report(
        "P-PUBLIC-RESEARCH-PLAN", "x" * (plan["limit_chars"] + 1), {}
    )
    assert over["within_budget"] is False
    assert over["provider_visible_chars"] > over["limit_chars"]


def test_plan_runtime_owns_time_scope_from_input_constraints():
    candidate = copy.deepcopy(PACK.replay_output("P-PUBLIC-RESEARCH-PLAN", "normal"))
    candidate["result"]["time_scope"] = None
    normalized, report = canonicalize_wf3_machine_fields(
        "P-PUBLIC-RESEARCH-PLAN",
        candidate,
        {
            "payload": {
                "time_constraints": {
                    "start_date": "2021-08-28",
                    "end_date": "2026-08-28",
                    "freshness_required": True,
                }
            }
        },
    )
    assert normalized["result"]["time_scope"] == "2021-08-28/2026-08-28"
    assert "/result/time_scope" in report["changes"]


def test_import_security_finding_controls_flags_and_rejects_claims():
    candidate = copy.deepcopy(
        PACK.replay_output("P-ONLINE-RESULT-IMPORT-CRITIC", "high_risk")
    )
    candidate["result"].update(
        {
            "import_recommendation": "IMPORT_PUBLIC_CLAIM_CANDIDATES",
            "accepted_claim_ids": ["claim-1"],
            "prompt_injection_detected": False,
        }
    )
    normalized, report = canonicalize_wf3_machine_fields(
        "P-ONLINE-RESULT-IMPORT-CRITIC",
        candidate,
        {
            "payload": {
                "result_package": {
                    "claims": [{"claim_id": "claim-1"}],
                }
            }
        },
    )
    assert normalized["result"]["prompt_injection_detected"] is True
    assert normalized["result"]["accepted_claim_ids"] == []
    assert normalized["result"]["rejected_claim_ids"] == ["claim-1"]
    assert normalized["result"]["import_recommendation"] == "REJECT"
    assert normalized["findings"][0]["blocking"] is True
    assert report["change_count"] >= 4


def test_synthesis_projection_bounds_thirty_sources_without_dropping_ids():
    passages = [
        {
            "passage_id": f"passage-{index}",
            "source_ref": {
                "source_id": f"source-{index}",
                "source_type": "PUBLIC_SOURCE",
                "source_hash": "a" * 64,
            },
            "text": (f"evidence-{index}-" + "x" * 5980),
        }
        for index in range(30)
    ]
    envelope = {
        "payload": {
            "extracted_passages": passages,
            "retrieved_sources": [
                {
                    "source_id": f"source-{index}",
                    "source_type": "PUBLIC_SOURCE",
                    "quoted_text": "duplicate",
                }
                for index in range(30)
            ],
        }
    }
    compact, report = compact_wf3_research_envelope(
        "P-PUBLIC-RESEARCH-SYNTHESIS", envelope
    )
    compact_passages = compact["payload"]["extracted_passages"]
    assert len(compact_passages) == 30
    assert {item["source_ref"]["source_id"] for item in compact_passages} == {
        f"source-{index}" for index in range(30)
    }
    assert report["model_passage_chars"] <= 42_000
    assert report["quality_guard_uses_full_context"] is True
    assert len(passages[0]["text"]) > len(compact_passages[0]["text"])
    budget = wf3_provider_request_budget_report(
        "P-PUBLIC-RESEARCH-SYNTHESIS",
        PACK.prompt_text("P-PUBLIC-RESEARCH-SYNTHESIS"),
        compact,
    )
    assert budget["within_budget"] is True


def _plan(
    *,
    questions=("rq1", "rq2"),
    queries=(("human AI decision research", (0,)), ("decision support evaluation", (1,))),
):
    return {
        "result": {
            "research_questions": list(questions),
            "queries": [
                {"query": text, "linked_question_indexes": list(bindings)}
                for text, bindings in queries
            ],
            "source_priorities": ["paper"],
            "time_scope": "2021-01-01/2026-01-01",
            "evidence_requirements": ["full text"],
            "prohibited_inferences": ["no unsupported causality"],
        }
    }


def test_plan_non_regression_freezes_question_order_and_query_bindings():
    assert compare_wf3_plan_candidates(
        _plan(),
        _plan(
            questions=("rq2", "rq1"),
            queries=(("human AI decision research", (0,)),),
        ),
    )["accepted"] is False
    additive = _plan(
        queries=(
            ("human AI decision research", (0,)),
            ("decision support evaluation", (1,)),
            ("human AI decision comparison", (0, 1)),
        )
    )
    assert compare_wf3_plan_candidates(_plan(), additive)["accepted"] is True
    invalid = _plan(
        queries=(
            ("human AI decision research", (7,)),
            ("decision support evaluation", (1,)),
        )
    )
    invalid_comparison = compare_wf3_plan_candidates(_plan(), invalid)
    assert invalid_comparison["accepted"] is False
    assert "PLAN_SEARCH_PREFLIGHT_FAILED" in invalid_comparison["regressions"]


def _synthesis(claims, *, limitations=("limited sample",), conflicts=("mixed evidence",)):
    return {
        "result": {
            "claims": [
                {
                    "claim_text": text,
                    "claim_type": "PUBLIC_CLAIM",
                    "source_refs": [{"source_id": source_id}],
                }
                for text, source_id in claims
            ],
            "source_comparisons": [
                {"topic": "performance", "source_ids": ["s1", "s2"]}
            ],
            "limitations": list(limitations),
            "conflicts": list(conflicts),
            "coverage_summary": "rq1 and rq2 covered",
        }
    }


def test_synthesis_non_regression_does_not_trade_claim_loss_for_fewer_findings():
    accepted = _synthesis((("claim A", "s1"), ("claim B", "s2")))
    shrunk = _synthesis((("claim A", "s1"),), limitations=(), conflicts=())
    comparison = compare_wf3_synthesis_candidates(accepted, shrunk)
    assert comparison["accepted"] is False
    assert "VALIDATED_CLAIMS_SHRANK_OR_CHANGED" in comparison["regressions"]
    assert "LIMITATIONS_DISAPPEARED" in comparison["regressions"]


def test_workflow_keeps_exact_plan_baseline_and_archives_rejected_complete_candidate(tmp_path):
    db = make_executor_db(tmp_path)
    now = utc_now()
    state = {"step_results": {}}
    db.execute(
        "INSERT INTO workflows(id,project_id,workflow_type,status,current_step,state_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (
            "wf-plan-baseline",
            "project-1",
            "WF-3_HYBRID_ONLINE_ASSIST",
            "RUNNING",
            2,
            json.dumps(state),
            now,
            now,
        ),
    )
    wf = {
        "id": "wf-plan-baseline",
        "project_id": "project-1",
        "workflow_type": "WF-3_HYBRID_ONLINE_ASSIST",
    }
    engine = WorkflowEngine(db, SimpleNamespace(), SimpleNamespace(), SimpleNamespace(), SimpleNamespace())
    baseline_output = {"status": "PASS", **_plan()}
    baseline_result = {
        "run_id": "run-plan-baseline",
        "status": "PASS",
        "output": baseline_output,
    }
    accepted = engine._wf3_accept_complete_candidate(
        wf, state, "P-PUBLIC-RESEARCH-PLAN", baseline_result
    )
    assert accepted is baseline_result
    db.execute(
        """INSERT INTO prompt_runs(id,project_id,workflow_id,prompt_id,status,input_hash,output_hash,input_json,output_json,duration_ms,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "run-plan-baseline",
            "project-1",
            "wf-plan-baseline",
            "P-PUBLIC-RESEARCH-PLAN",
            "PASS",
            "input",
            state["wf3_accepted_model_baselines"]["P-PUBLIC-RESEARCH-PLAN"]["output_hash"],
            "{}",
            json.dumps(baseline_output),
            1,
            now,
        ),
    )
    regressed_output = {
        "status": "PASS",
        **_plan(questions=("rq2", "rq1"), queries=(("q1", (0,)),)),
    }
    fallback = engine._wf3_accept_complete_candidate(
        wf,
        state,
        "P-PUBLIC-RESEARCH-PLAN",
        {"run_id": "run-plan-bad", "status": "PASS", "output": regressed_output},
    )
    assert fallback["run_id"] == "run-plan-baseline"
    assert fallback["output"] == baseline_output
    assert fallback["wf3_rejected_candidate"]["run_id"] == "run-plan-bad"
    assert state["wf3_model_candidate_history"][-1]["decision"] == "REJECT"

    additive = {"status": "PASS", **_plan(queries=(("q1", (0,)), ("q2", (1,)), ("q3", (0, 1))))}
    fallback = engine._wf3_accept_complete_candidate(
        wf,
        state,
        "P-PUBLIC-RESEARCH-PLAN",
        {"run_id": "run-plan-invalid", "status": "PASS", "output": additive},
        candidate_preflight_errors=["/next/input: deterministic preflight failed"],
    )
    assert fallback["run_id"] == "run-plan-baseline"
    assert state["wf3_model_candidate_history"][-1]["comparison"]["regressions"][0] == (
        "NEXT_STEP_PREFLIGHT_FAILED"
    )
    db.execute(
        "UPDATE workflows SET state_json=? WHERE id=?",
        (json.dumps(state), "wf-plan-baseline"),
    )
    assert ContextBuilder(db, PACK)._latest_output(
        "project-1",
        "P-PUBLIC-RESEARCH-PLAN",
        workflow_id="wf-plan-baseline",
        exact_workflow=True,
    ) == baseline_output


@pytest.mark.parametrize("prompt_id", WF3_PROMPT_IDS)
@pytest.mark.parametrize("malformed", [None, [], "truncated-json", {}])
def test_all_wf3_model_nodes_reject_non_object_or_partial_containers_with_exact_errors(
    prompt_id, malformed
):
    executor = object.__new__(PromptExecutor)
    executor.pack = PACK
    executor.db = None
    if malformed == {}:
        normalized = executor._normalize_output(
            prompt_id, malformed, PACK.replay_input(prompt_id)
        )
        errors = PACK.validate(prompt_id, "output", normalized)
        assert errors
        assert any(str(item).startswith(("/", "$")) for item in errors)
        return
    with pytest.raises(PromptExecutionError) as caught:
        executor._normalize_output(prompt_id, malformed, PACK.replay_input(prompt_id))
    errors = caught.value.validation_errors
    assert errors
    assert any(str(item).startswith(("/", "$")) for item in errors)


@pytest.mark.parametrize("prompt_id", WF3_PROMPT_IDS)
def test_all_wf3_model_nodes_replace_provider_authored_top_level_source_ids(prompt_id):
    executor = object.__new__(PromptExecutor)
    executor.pack = PACK
    executor.db = None
    envelope = PACK.replay_input(prompt_id)
    output = copy.deepcopy(PACK.replay_output(prompt_id, "normal"))
    output["source_refs"] = [
        {
            "source_id": "invented-source-not-in-input",
            "source_type": "PUBLIC_SOURCE",
            "authority_rank": 50,
            "security_level": "PUBLIC",
        }
    ]
    normalized = executor._normalize_output(prompt_id, output, envelope)
    catalog_by_path = {
        item["object_path"]: item["source_id"]
        for item in build_trusted_source_catalog(envelope)
    }
    expected_ids = [
        catalog_by_path[f"payload.{field}"]
        for field in WF3_PROVENANCE_PAYLOAD_FIELDS[prompt_id]
        if f"payload.{field}" in catalog_by_path
    ]
    assert [item["source_id"] for item in normalized["source_refs"]] == expected_ids
    assert "invented-source-not-in-input" not in expected_ids


@pytest.mark.parametrize(
    ("run_id", "provider_source_ids"),
    (
        (
            "run-bbace040b0ad4a17",
            (
                "payload.allowed_topics",
                "payload.human_resolutions.human-cb3f7e173a315497c009",
                "payload.security_policy.profile_id",
                "payload.research_need.need_id",
            ),
        ),
        (
            "run-4a8258310e274e4a",
            (
                "doc-46545e6862d5497b",
                "artifact-2a6badf4c4f945c9",
                "payload.human_resolutions",
                "payload.allowed_topics",
                "payload.research_need.need_id",
                "payload.security_policy.profile_id",
            ),
        ),
        (
            "run-9d4263fe9ee84396",
            (
                "payload.allowed_topics",
                "payload.target_task_type",
                "payload.human_resolutions[human-cb3f7e173a315497c009]",
                "payload.research_need.reason_online_needed",
                "payload.research_need.desired_output",
            ),
        ),
    ),
)
def test_safe_package_20260827_live_source_failures_are_runtime_projected(
    run_id, provider_source_ids
):
    executor = object.__new__(PromptExecutor)
    executor.pack = PACK
    executor.db = None
    envelope = copy.deepcopy(PACK.replay_input("P-SAFE-ONLINE-PACKAGE"))
    envelope["payload"].update(
        {
            "allowed_topics": ["human-machine collaborative decision making"],
            "prohibited_fields": ["private applicant data"],
            "human_resolutions": [
                {
                    "resolution_id": "human-resolution-1",
                    "gate_id": "gate-1",
                    "answer": "approved",
                }
            ],
        }
    )
    output = copy.deepcopy(PACK.replay_output("P-SAFE-ONLINE-PACKAGE", "normal"))
    output["source_refs"] = [
        {"source_id": source_id} for source_id in provider_source_ids
    ]

    normalized = executor._normalize_output("P-SAFE-ONLINE-PACKAGE", output, envelope)

    actual_ids = {item["source_id"] for item in normalized["source_refs"]}
    catalog = build_trusted_source_catalog(envelope)
    expected_ids = {
        item["source_id"]
        for item in catalog
        if item["object_path"]
        in {
            f"payload.{field}"
            for field in WF3_PROVENANCE_PAYLOAD_FIELDS["P-SAFE-ONLINE-PACKAGE"]
        }
    }
    assert actual_ids == expected_ids
    assert not actual_ids.intersection(set(provider_source_ids))
    assert run_id.startswith("run-")


def test_safe_package_20260827_live_finding_paths_are_bound_to_input_owner_ids():
    envelope = copy.deepcopy(PACK.replay_input("P-SAFE-ONLINE-PACKAGE"))
    output = copy.deepcopy(PACK.replay_output("P-SAFE-ONLINE-PACKAGE", "normal"))
    output["findings"] = [
        {
            "finding_instance_id": "provider-id",
            "defect_namespace": "provider-namespace",
            "code": "SAFE_PACKAGE_EXCESS_CONTEXT",
            "category": "SECURITY",
            "target_type": "payload.research_need.question",
            "target_path_or_span": "payload.research_need.question",
            "description": "input text needs review",
            "evidence_refs": ["payload.research_need.question"],
            "repairable": True,
            "suggested_route": "ORIGINAL_PRODUCER",
            "blocking": False,
            "severity": "P2",
        },
        {
            "finding_instance_id": "provider-id-2",
            "defect_namespace": "provider-namespace",
            "code": "SAFE_PACKAGE_EXCESS_CONTEXT",
            "category": "SECURITY",
            "target_type": "payload.research_need.reason_online_needed",
            "target_path_or_span": "payload.research_need.reason_online_needed",
            "description": "input reason needs review",
            "evidence_refs": ["payload.research_need.reason_online_needed"],
            "repairable": True,
            "suggested_route": "ORIGINAL_PRODUCER",
            "blocking": False,
            "severity": "P2",
        },
    ]

    normalized, _ = canonicalize_wf3_machine_fields(
        "P-SAFE-ONLINE-PACKAGE", output, envelope
    )
    research_need_id = next(
        item["source_id"]
        for item in build_trusted_source_catalog(envelope)
        if item["object_path"] == "payload.research_need"
    )

    assert normalized["findings"][0]["evidence_refs"] == [
        f"{research_need_id}.question"
    ]
    assert normalized["findings"][1]["evidence_refs"] == [
        f"{research_need_id}.reason_online_needed"
    ]
    assert normalized["findings"][0]["target_path_or_span"] == (
        "/payload/research_need/question"
    )


def test_wf3_cross_list_ids_cannot_be_self_authorized_or_misplaced():
    critic_input = copy.deepcopy(PACK.replay_input("P-PUBLIC-RESEARCH-CRITIC"))
    critic_output = copy.deepcopy(PACK.replay_output("P-PUBLIC-RESEARCH-CRITIC", "normal"))
    critic_output["result"]["source_quality_summary"] = [
        {"source_id": "invented-source", "quality": "LOW", "reason": "irrelevant"}
    ]
    errors = wf3_output_semantic_errors(
        "P-PUBLIC-RESEARCH-CRITIC", critic_input, critic_output
    )
    assert errors[0].startswith("/result/source_quality_summary/0/source_id")

    import_input = copy.deepcopy(PACK.replay_input("P-ONLINE-RESULT-IMPORT-CRITIC"))
    import_input["payload"]["result_package"]["claims"] = [
        {"claim_id": "claim-1"},
        {"claim_id": "claim-2"},
    ]
    import_output = copy.deepcopy(
        PACK.replay_output("P-ONLINE-RESULT-IMPORT-CRITIC", "normal")
    )
    import_output["result"]["accepted_claim_ids"] = ["claim-1", "src-001"]
    import_output["result"]["rejected_claim_ids"] = ["claim-1"]
    errors = wf3_output_semantic_errors(
        "P-ONLINE-RESULT-IMPORT-CRITIC", import_input, import_output
    )
    assert any("overlap" in item for item in errors)
    assert any("not present" in item for item in errors)
    assert any("not classified" in item for item in errors)


def test_wf3_synthesis_requires_claim_sources_from_retrieved_input():
    envelope = {
        "payload": {
            "retrieved_sources": [{"source_id": "source-1"}],
            "extracted_passages": [
                {"source_ref": {"source_id": "source-2"}, "text": "evidence"}
            ],
        }
    }
    output = {
        "result": {
            "claims": [{"claim_id": "claim-1", "source_refs": []}],
            "source_comparisons": [],
        }
    }
    errors = wf3_output_semantic_errors(
        "P-PUBLIC-RESEARCH-SYNTHESIS", envelope, output
    )
    assert errors == [
        "/result/claims/0/source_refs: substantive claim requires at least one retrieved input source"
    ]

    output["result"]["claims"][0]["source_refs"] = [{"source_id": "invented"}]
    output["result"]["source_comparisons"] = [
        {"source_ids": ["source-1", "invented"]}
    ]
    errors = wf3_output_semantic_errors(
        "P-PUBLIC-RESEARCH-SYNTHESIS", envelope, output
    )
    assert any("/result/claims/0/source_refs/0/source_id" in item for item in errors)
    assert any("/result/source_comparisons/0/source_ids/1" in item for item in errors)

    output["result"]["claims"][0]["source_refs"] = [{"source_id": "source-2"}]
    output["result"]["source_comparisons"][0]["source_ids"] = [
        "source-1",
        "source-2",
    ]
    assert not wf3_output_semantic_errors(
        "P-PUBLIC-RESEARCH-SYNTHESIS", envelope, output
    )


def test_prompt_trace_distinguishes_validation_and_provider_envelopes(tmp_path):
    db = make_executor_db(tmp_path)
    executor = object.__new__(PromptExecutor)
    executor.db = db
    validation_envelope = {
        "security_context": {"input_max_security_level": "INTERNAL"},
        "payload": {"research_need": {"question": "q"}},
        "trusted_source_catalog": [{"source_id": "runtime-only"}],
    }
    provider_envelope = {
        "security_context": {"input_max_security_level": "INTERNAL"},
        "payload": {"research_need": {"question": "q"}},
    }

    executor._save_trace(
        "project-1",
        "wf-1",
        "P-SAFE-ONLINE-PACKAGE",
        validation_envelope,
        "system prompt",
        "raw response",
        {},
        "ONLINE_PUBLIC",
        "model-1",
        "endpoint-1",
        1,
        "ERROR",
        "failure",
        provider_request_envelope=provider_envelope,
    )

    row = db.fetchone(
        "SELECT content_json FROM artifacts WHERE artifact_type='PROMPT_TRACE'"
    )
    trace = json.loads(row["content_json"])
    assert trace["input_envelope_kind"] == "VALIDATION_ENVELOPE"
    assert trace["input_envelope"] == validation_envelope
    assert trace["validation_envelope"] == validation_envelope
    assert trace["provider_request_envelope"] == provider_envelope
    assert "trusted_source_catalog" not in trace["provider_request_envelope"]
    assert trace["provider_request_hash"]


def test_runtime_prompt_trace_uses_the_same_unambiguous_provider_request_names():
    executor = object.__new__(RuntimePromptExecutor)
    executor.quality_guard_enabled = False
    executor.policy = SimpleNamespace(enabled=False)
    validation_envelope = {
        "payload": {"research_need": {"question": "q"}},
        "trusted_source_catalog": [{"source_id": "runtime-only"}],
    }
    provider_envelope = {"payload": {"research_need": {"question": "q"}}}

    trace = executor._trace_payload(
        prompt_id="P-SAFE-ONLINE-PACKAGE",
        version=1,
        status="ERROR",
        duration_ms=1,
        model_envelope=validation_envelope,
        provider_envelope=provider_envelope,
        call_key="call-1",
        input_hash="input-hash",
        provider_output=None,
        consumed_output=None,
    )

    assert trace["input_envelope_kind"] == "VALIDATION_ENVELOPE"
    assert trace["validation_envelope"] == validation_envelope
    assert trace["provider_input_envelope"] == provider_envelope
    assert trace["provider_request_envelope"] == provider_envelope
    assert trace["provider_input_sha256"] == trace["provider_request_sha256"]
