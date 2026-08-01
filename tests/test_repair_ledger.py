from __future__ import annotations

from app.repair_ledger import RepairLedger


def test_semantic_budget_is_consumed_only_when_applied_repair_enters_rereview() -> None:
    state: dict = {}
    key = "section:s1:P-WRITE-BLUEPRINT-CRITIC"
    common = {
        "repair_id": "repair-1",
        "run_id": "run-repair-1",
        "details": {"producer_prompt": "P-WRITE-BLUEPRINT"},
    }

    RepairLedger.repair_created(state, key, repair_id="repair-1")
    RepairLedger.model_returned(state, key, **common)
    RepairLedger.schema_validated(state, key, **common)
    RepairLedger.diff_validated(state, key, **common)
    RepairLedger.applied(
        state,
        key,
        **common,
        application_artifact_id="artifact-1",
    )

    assert RepairLedger.count(state, "semantic_repairs", key) == 0

    count = RepairLedger.rereview_started(
        state,
        key,
        **common,
        application_artifact_id="artifact-1",
    )
    assert count == 1
    assert RepairLedger.count(state, "semantic_repairs", key) == 1

    # Resume/retry of the same review must not spend the budget twice.
    assert RepairLedger.rereview_started(
        state,
        key,
        **common,
        application_artifact_id="artifact-1",
    ) == 1

    RepairLedger.rereview_completed(
        state,
        key,
        status="PASS",
        **common,
        application_artifact_id="artifact-1",
    )
    assert [item["event"] for item in RepairLedger.events(state, repair_id="repair-1")] == [
        "CREATED",
        "MODEL_RETURNED",
        "SCHEMA_VALIDATED",
        "DIFF_VALIDATED",
        "APPLIED",
        "REREVIEW_STARTED",
        "REREVIEW_PASS",
    ]


def test_returned_or_schema_validated_candidate_does_not_consume_budget() -> None:
    state: dict = {}
    key = "P-FACT-CRITIC"

    RepairLedger.repair_created(state, key, repair_id="repair-revise")
    RepairLedger.model_returned(
        state,
        key,
        repair_id="repair-revise",
        run_id="run-revise",
        details={"status": "REVISE"},
    )
    RepairLedger.schema_validated(
        state,
        key,
        repair_id="repair-revise",
        run_id="run-revise",
    )

    assert RepairLedger.count(state, "semantic_repairs", key) == 0
    assert [item["event"] for item in RepairLedger.events(state, repair_id="repair-revise")] == [
        "CREATED",
        "MODEL_RETURNED",
        "SCHEMA_VALIDATED",
    ]


def test_each_distinct_applied_repair_can_consume_one_review_attempt() -> None:
    state: dict = {}
    key = "section:s1:P-WRITE-CRITIC"

    for index in (1, 2):
        RepairLedger.applied(
            state,
            key,
            repair_id=f"repair-{index}",
            run_id=f"run-{index}",
            application_artifact_id=f"artifact-{index}",
        )
        RepairLedger.rereview_started(
            state,
            key,
            repair_id=f"repair-{index}",
            run_id=f"run-{index}",
            application_artifact_id=f"artifact-{index}",
        )

    assert RepairLedger.count(state, "semantic_repairs", key) == 2


def test_provider_and_technical_retries_are_independent_from_semantic_budget() -> None:
    state: dict = {}
    key = "step:5:section:s1:BLUEPRINT_CRITIC"

    assert RepairLedger.provider_retry(state, key) == 1
    assert RepairLedger.technical_retry(state, key) == 1
    assert RepairLedger.count(state, "semantic_repairs", key) == 0
    assert RepairLedger.count(state, "provider_retries", key) == 1
    assert RepairLedger.count(state, "technical_retries", key) == 1


def test_rereview_cannot_consume_budget_before_application() -> None:
    state: dict = {}
    try:
        RepairLedger.rereview_started(
            state,
            "P-FACT-CRITIC",
            repair_id="repair-not-applied",
            run_id="run-1",
            application_artifact_id="artifact-1",
        )
    except ValueError as exc:
        assert "APPLIED" in str(exc)
    else:
        raise AssertionError("unapplied repair consumed semantic budget")
