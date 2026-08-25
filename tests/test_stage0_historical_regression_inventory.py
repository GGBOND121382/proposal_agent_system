from __future__ import annotations

import ast
from pathlib import Path


# Every Stage-0 failure observed during the 2026-08 hardening cycle must keep
# an executable regression test.  This inventory makes deleting or renaming a
# historical guard an explicit test failure instead of silently losing it.
HISTORICAL_STAGE0_REGRESSIONS = {
    "browser_true_string_keeps_boolean_type": (
        "tests/test_workflow_input_integrity.py",
        "test_enum_human_answer_recovers_browser_string_types",
    ),
    "frontend_gate_decodes_json_scalar_types": (
        "tests/test_workflow_input_integrity.py",
        "test_frontend_gate_decoder_preserves_json_scalar_types",
    ),
    "underpowered_composite_gate_is_widened": (
        "tests/test_gate_answer_contract.py",
        "test_generic_gate_boundary_widens_controls_that_cannot_carry_answer",
    ),
    "compound_boolean_question_is_not_boolean": (
        "tests/test_semantic_model_contracts_v1.py",
        "test_compound_or_conditional_boolean_question_deterministically_uses_text",
    ),
    "question_metadata_is_runtime_owned": (
        "tests/test_argument_two_stage_runtime_step4b.py",
        "test_user_question_gate_metadata_is_runtime_owned_and_gap_derived",
    ),
    "machine_ids_and_graph_are_runtime_owned": (
        "tests/test_semantic_model_contracts_v1.py",
        "test_argument_model_output_runtime_derives_graph_matrix_and_ids",
    ),
    "argument_prompt_excludes_runtime_ids_and_hashes": (
        "tests/test_semantic_model_contracts_v1.py",
        "test_argument_model_input_is_semantic_minimum_not_runtime_envelope",
    ),
    "repair_prompt_excludes_original_object_and_hashes": (
        "tests/test_semantic_model_contracts_v1.py",
        "test_targeted_repair_model_sees_write_target_and_read_context_not_original_object",
    ),
    "exact_null_string_is_normalized": (
        "tests/test_semantic_model_contracts_v1.py",
        "test_argument_two_stage_normalizes_exact_null_string_without_mutating_provider_candidate",
    ),
    "skeleton_collection_fragment_is_compacted": (
        "tests/test_argument_two_stage_runtime_step4b.py",
        "test_skeleton_collection_fragment_is_compacted_without_model_retry",
    ),
    "skeleton_question_fragments_do_not_gain_defaults": (
        "tests/test_argument_two_stage_runtime_step4b.py",
        "test_skeleton_retry_compacts_fragmented_question_rows_without_default_pollution",
    ),
    "design_question_fragments_do_not_gain_defaults": (
        "tests/test_argument_two_stage_runtime_step4b.py",
        "test_design_retry_compacts_fragmented_question_rows_without_default_pollution",
    ),
    "success_criteria_cannot_move_into_evidence_ids": (
        "tests/test_argument_two_stage_runtime_step4b.py",
        "test_argument_stage_repair_restores_unreported_success_criteria_regression",
    ),
    "repair_cannot_reorder_unreported_rows": (
        "tests/test_argument_two_stage_runtime_step4b.py",
        "test_argument_stage_repair_does_not_apply_positional_patch_after_row_reorder",
    ),
    "unknown_evidence_is_removed_exactly": (
        "tests/test_argument_two_stage_runtime_step4b.py",
        "test_step4b_runtime_drops_explicit_unknown_evidence_without_model_retry",
    ),
    "design_parent_references_fail_closed": (
        "tests/test_semantic_model_contracts_v1.py",
        "test_argument_design_reference_validation_is_local_and_fail_closed",
    ),
    "thirteen_missing_design_parents_preserve_valid_rows": (
        "tests/test_semantic_model_contracts_v1.py",
        "test_argument_full_regeneration_replays_thirteen_missing_parent_failures_monotonically",
    ),
    "foundation_requires_qualified_evidence": (
        "tests/test_semantic_model_contracts_v1.py",
        "test_argument_two_stage_design_rejects_unqualified_foundation_before_assembly",
    ),
    "foundation_and_cross_stage_errors_share_one_repair": (
        "tests/test_semantic_model_contracts_v1.py",
        "test_argument_two_stage_design_combines_foundation_and_cross_stage_feedback_in_one_retry",
    ),
    "empty_skeleton_gets_full_stage_retry": (
        "tests/test_argument_two_stage_runtime_step4b.py",
        "test_empty_skeleton_response_retries_the_full_stage_without_a_fake_draft",
    ),
    "empty_design_retries_design_only": (
        "tests/test_argument_two_stage_runtime_step4b.py",
        "test_empty_design_response_retries_only_design_with_skeleton_frozen",
    ),
    "missing_json_object_gets_full_stage_retry": (
        "tests/test_argument_two_stage_runtime_step4b.py",
        "test_step4b_runtime_allows_a_third_internal_stage_attempt",
    ),
    "partial_stage_failure_never_enters_targeted_repair": (
        "tests/test_argument_two_stage_runtime_step4b.py",
        "test_partial_stage_failure_never_enters_targeted_repair",
    ),
    "empty_stage_retries_are_bounded": (
        "tests/test_argument_two_stage_runtime_step4b.py",
        "test_repeated_empty_stage_responses_exhaust_full_retries_without_targeted_repair",
    ),
    "design_retry_never_regenerates_skeleton": (
        "tests/test_argument_two_stage_runtime_step4b.py",
        "test_step4b_runtime_design_retry_never_regenerates_skeleton",
    ),
    "full_stage_retry_prompt_stays_bounded": (
        "tests/test_argument_two_stage_runtime_step4b.py",
        "test_step4b_runtime_design_retry_never_regenerates_skeleton",
    ),
}


def test_every_known_stage0_failure_keeps_an_executable_regression() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    functions_by_file: dict[str, set[str]] = {}

    for relative_path, _test_name in HISTORICAL_STAGE0_REGRESSIONS.values():
        if relative_path in functions_by_file:
            continue
        source_path = repository_root / relative_path
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        functions_by_file[relative_path] = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

    missing = {
        incident: f"{relative_path}::{test_name}"
        for incident, (relative_path, test_name) in HISTORICAL_STAGE0_REGRESSIONS.items()
        if test_name not in functions_by_file[relative_path]
    }
    assert not missing, f"Historical Stage-0 regression tests were removed: {missing}"
