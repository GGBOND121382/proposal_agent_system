"""Targeted-repair closure must compare model semantics, not runtime metadata.

Regression for the audit case run-f5d8c735bb684166: the model returned
ESCALATE with ``changes=[]``, then ``bind_trusted_source_refs`` completed 140
source-metadata fields on the output side only, and the closure check reported
those runtime-made differences as out-of-scope model modifications.
"""

from __future__ import annotations

import copy

import pytest

from app.executor import PromptExecutionError, PromptExecutor


def _envelope(content, *, allowed_paths=None, protected_paths=None, protected_hashes=None):
    return {
        "payload": {
            "findings_to_repair": [{"finding_instance_id": "FIND-1"}],
            "original_object": {"object_type": "TEST", "content": content},
            "allowed_paths": allowed_paths or [],
            "protected_paths": protected_paths or [],
            "protected_hashes": protected_hashes or [],
        }
    }


def _output(repaired, *, status="REVISE", changed_paths=None, protected_hashes=None):
    return {
        "status": status,
        "result": {
            "repaired_object": repaired,
            "changed_paths": changed_paths or [],
            "unchanged_protected_hashes": protected_hashes or [],
            "resolved_finding_ids": [],
            "unresolved_finding_ids": ["FIND-1"],
        },
    }


_BASELINE_CONTENT = {
    "result": {
        "scheme_profile": {
            "project_id": "proj-invented-by-model",
            "profile_hash": "0" * 64,
            "rules": [
                {
                    "rule_id": "rule-a",
                    "statement": "必须绑定真实来源",
                    "source_refs": [{"source_id": "doc-1", "section_id": "sec-1"}],
                }
            ],
        }
    }
}


def test_closure_ignores_runtime_completed_source_metadata():
    # Candidate = baseline after the runtime rebound trusted source metadata
    # and canonicalized the protocol project_id.  The model changed nothing.
    repaired = copy.deepcopy(_BASELINE_CONTENT)
    repaired["result"]["scheme_profile"]["project_id"] = "project-real-001"
    rule = repaired["result"]["scheme_profile"]["rules"][0]
    rule["rule_hash"] = "a" * 64
    rule["source_refs"][0].update(
        {
            "document_version_id": "docv-1",
            "quoted_text": "必须绑定真实来源",
            "span_start": 0,
            "span_end": 8,
            "source_hash": "b" * 64,
            "authority_rank": 85,
            "security_level": "INTERNAL",
            "source_type": "CURRENT_PROPOSAL",
        }
    )

    PromptExecutor._validate_output_semantics(
        "P-TARGETED-REPAIR",
        _envelope(_BASELINE_CONTENT),
        _output(repaired),
    )


def test_closure_still_rejects_protected_business_field_change():
    repaired = copy.deepcopy(_BASELINE_CONTENT)
    repaired["result"]["scheme_profile"]["rules"][0]["statement"] = "改写后的规则"

    with pytest.raises(PromptExecutionError) as excinfo:
        PromptExecutor._validate_output_semantics(
            "P-TARGETED-REPAIR",
            _envelope(
                _BASELINE_CONTENT,
                protected_paths=["/content/result/scheme_profile/rules"],
            ),
            _output(repaired),
        )

    assert "overlaps protected_paths" in str(excinfo.value.validation_errors)


def test_closure_accepts_declared_change_inside_allowed_paths():
    repaired = copy.deepcopy(_BASELINE_CONTENT)
    repaired["result"]["scheme_profile"]["rules"][0]["statement"] = "修订后的规则表述"

    PromptExecutor._validate_output_semantics(
        "P-TARGETED-REPAIR",
        _envelope(
            _BASELINE_CONTENT,
            allowed_paths=["/content/result/scheme_profile/rules/0/statement"],
        ),
        _output(
            repaired,
            changed_paths=["/content/result/scheme_profile/rules/0/statement"],
        ),
    )


def test_closure_catches_model_changing_cited_source_identity():
    # Which source backs a rule is a semantic choice, not runtime metadata.
    repaired = copy.deepcopy(_BASELINE_CONTENT)
    repaired["result"]["scheme_profile"]["rules"][0]["source_refs"][0]["source_id"] = "doc-2"

    with pytest.raises(PromptExecutionError) as excinfo:
        PromptExecutor._validate_output_semantics(
            "P-TARGETED-REPAIR",
            _envelope(_BASELINE_CONTENT),
            _output(repaired),
        )

    assert "outside allowed_paths" in str(excinfo.value.validation_errors)


def _repair_envelope_with_items():
    content = {
        "items": [
            {"local_key": "I1", "item_type": "PROJECT_BASIC", "summary": "调研任务"},
            {"local_key": "I2", "item_type": "DEMAND", "summary": "调研目标"},
        ]
    }
    return _envelope(
        content,
        allowed_paths=["/content/items"],
        protected_paths=["/content/status"],
    )


def test_repair_may_add_missing_field_on_existing_object():
    from app.model_semantic_contracts import (
        expand_targeted_repair_model_output,
        targeted_repair_semantic_errors,
    )

    envelope = _repair_envelope_with_items()
    semantic = {
        "decision": "APPLY",
        "changes": [
            {"path": "/items/0/evidence_ids", "value": ["S1"]},
            {"path": "/items/1/evidence_ids", "value": ["S2"]},
        ],
    }

    assert targeted_repair_semantic_errors(envelope, semantic) == []
    expanded = expand_targeted_repair_model_output(envelope, semantic)
    repaired = expanded["result"]["repaired_object"]
    assert repaired["items"][0]["evidence_ids"] == ["S1"]
    assert repaired["items"][1]["evidence_ids"] == ["S2"]
    assert "/content/items/0/evidence_ids" in expanded["result"]["changed_paths"]


def test_repair_still_rejects_insertion_below_missing_parent():
    from app.model_semantic_contracts import targeted_repair_semantic_errors

    semantic = {
        "decision": "APPLY",
        "changes": [{"path": "/items/9/evidence_ids", "value": ["S1"]}],
    }
    errors = targeted_repair_semantic_errors(_repair_envelope_with_items(), semantic)

    assert any("structural insertion is not a local repair" in e for e in errors)
