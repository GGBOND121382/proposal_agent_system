from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.db import Database
from app.llm import ProviderError
from app.private_storage import private_path_is_restricted, secure_private_tree
from app.privacy import OutboundPrivacyError, assert_online_payload_safe, sanitize_safe_online_package
from app.runtime_evidence import ModelCallEvidenceStore
from app.runtime_failures import (
    ProviderFailureKind,
    classify_runtime_failure,
    persistence_safe_failure_classification,
)
from app.secret_redaction import redact_secret_text


FAKE_KEY = "sk-api-TEST_ONLY_0123456789abcdefghijklmnopqrstuvwxyz"
FAKE_BEARER = "Bearer TESTTOKEN0123456789abcdefghijklmnop"


def test_online_payload_rejects_credential_shapes() -> None:
    for value in (FAKE_KEY, FAKE_BEARER, f"api_key={FAKE_KEY}"):
        with pytest.raises(OutboundPrivacyError) as exc_info:
            assert_online_payload_safe({"payload": {"human_resolutions": [{"value": value}]}}, {})
        assert any(item.entity_type == "CREDENTIAL" for item in exc_info.value.matches)


def test_safe_online_package_redacts_credentials_without_echoing_them() -> None:
    output = {
        "result": {
            "task_description": f"public task {FAKE_KEY}",
            "queries": [f"query Authorization: {FAKE_BEARER}"],
            "entity_placeholders": [],
            "removed_fields": [],
        }
    }
    sanitized, matches = sanitize_safe_online_package(output, {})
    serialized = json.dumps(sanitized, ensure_ascii=False)
    assert FAKE_KEY not in serialized
    assert "TESTTOKEN0123456789" not in serialized
    assert any(item.entity_type == "CREDENTIAL" for item in matches)
    assert "认证凭据" in sanitized["result"]["removed_fields"]


def test_database_audit_redacts_nested_credentials(tmp_path: Path) -> None:
    db = Database(tmp_path / "audit.sqlite3")
    db.audit(
        "TEST_EVENT",
        object_id="object-1",
        metadata={"error": f"provider failed with {FAKE_KEY}", "nested": {"authorization": FAKE_BEARER}},
    )
    row = db.fetchone("SELECT metadata_json FROM audit_events WHERE object_id='object-1'")
    assert row is not None
    assert FAKE_KEY not in row["metadata_json"]
    assert "TESTTOKEN0123456789" not in row["metadata_json"]
    assert "REDACTED_CREDENTIAL" in row["metadata_json"]

    with db.transaction() as tx:
        tx.audit(
            "TEST_TX_EVENT",
            object_id="object-2",
            metadata={"detail": f"Authorization: {FAKE_BEARER}"},
        )
    tx_row = db.fetchone("SELECT metadata_json FROM audit_events WHERE object_id='object-2'")
    assert tx_row is not None
    assert "TESTTOKEN0123456789" not in tx_row["metadata_json"]


def test_model_call_evidence_is_private_on_disk(tmp_path: Path) -> None:
    store = ModelCallEvidenceStore(tmp_path / "model_calls")
    request = {"call_key": "call-1", "input_envelope": {"private": "INTERNAL MATERIAL"}}
    store.write_request("call-1", request)
    store.write_response(
        "call-1",
        raw_text='{"status":"PASS"}',
        parsed_output={"status": "PASS"},
        raw_parsed_output={"status": "PASS"},
        metadata={"model_id": "fake", "endpoint_id": "fake"},
    )
    store.mark_committed("call-1", {"run_id": "run-1"})

    for directory in (
        store.root,
        store.requests_dir,
        store.responses_dir,
        store.commits_dir,
    ):
        assert private_path_is_restricted(directory)

    files = [path for path in store.root.rglob("*") if path.is_file()]
    assert files
    for path in files:
        assert private_path_is_restricted(path), path


def test_failure_classification_redacts_credentials_from_persistable_metadata() -> None:
    exc = ProviderError(
        f"HTTP 401 api_key={FAKE_KEY}",
        kind=ProviderFailureKind.HTTP_STATUS,
        http_status=401,
        response_excerpt=f"Authorization: {FAKE_BEARER}",
        retryable_hint=False,
    )
    payload = classify_runtime_failure(exc).to_dict()
    serialized = json.dumps(payload, ensure_ascii=False)
    assert FAKE_KEY not in serialized
    assert "TESTTOKEN0123456789" not in serialized
    assert "REDACTED_CREDENTIAL" in serialized


def test_secret_redactor_keeps_noncredential_diagnostics() -> None:
    text = "API key is missing; token budget 7000; model output contract failed"
    assert redact_secret_text(text) == text


def test_online_payload_rejects_nonpublic_nested_security_labels() -> None:
    envelope = {
        "security_context": {"input_max_security_level": "PUBLIC"},
        "payload": {
            "known_public_sources": [
                {
                    "source_id": "source-1",
                    "source_type": "TECHNICAL_MATERIAL",
                    "authority_rank": 80,
                    "security_level": "INTERNAL",
                    "quoted_text": "internal excerpt",
                }
            ]
        },
    }
    with pytest.raises(OutboundPrivacyError) as exc_info:
        assert_online_payload_safe(envelope, {})
    assert any(item.entity_type == "SECURITY_LABEL" for item in exc_info.value.matches)

    with pytest.raises(OutboundPrivacyError) as project_label_exc:
        assert_online_payload_safe(
            {
                "security_context": {
                    "input_max_security_level": "PUBLIC",
                    "project_security_level": "INTERNAL",
                },
                "payload": {},
            },
            {},
        )
    assert any(item.path.endswith("project_security_level") for item in project_label_exc.value.matches)


def test_router_denies_online_execution_when_endpoint_allowlist_is_explicitly_empty() -> None:
    from app.pack import PromptPack
    from app.security import RoutingDenied, SecurityRouter

    root = Path(__file__).resolve().parents[1]
    pack = PromptPack(root / "prompt_pack")
    router = SecurityRouter(pack)
    envelope = pack.replay_input("P-PUBLIC-RESEARCH-PLAN")
    envelope["security_context"]["input_max_security_level"] = "PUBLIC"
    envelope["security_context"]["project_security_level"] = "PUBLIC"
    envelope["security_context"]["online_transfer_approval_status"] = "APPROVED"
    envelope["security_context"]["allowed_model_endpoint_ids"] = []

    with pytest.raises(RoutingDenied, match="no explicitly allowed endpoint"):
        router.route("P-PUBLIC-RESEARCH-PLAN", envelope)


def test_database_storage_is_private_on_disk(tmp_path: Path) -> None:
    db_path = tmp_path / "private-data" / "proposal_agents.sqlite3"
    Database(db_path)
    assert private_path_is_restricted(db_path.parent)
    assert private_path_is_restricted(db_path)


def test_private_tree_hardens_existing_contents(tmp_path: Path) -> None:
    root = tmp_path / "preexisting-private-tree"
    nested = root / "nested"
    nested.mkdir(parents=True)
    existing = nested / "evidence.json"
    existing.write_text('{"private": true}', encoding="utf-8")

    secure_private_tree(root)

    assert private_path_is_restricted(root)
    assert private_path_is_restricted(nested)
    assert private_path_is_restricted(existing)


def test_router_requires_explicit_outbound_approval_for_online_execution() -> None:
    from app.pack import PromptPack
    from app.security import RoutingDenied, SecurityRouter

    root = Path(__file__).resolve().parents[1]
    pack = PromptPack(root / "prompt_pack")
    router = SecurityRouter(pack)
    envelope = pack.replay_input("P-PUBLIC-RESEARCH-PLAN")
    envelope["security_context"].update(
        {
            "input_max_security_level": "PUBLIC",
            "project_security_level": "PUBLIC",
            "online_transfer_approval_status": "NOT_REQUIRED",
            "allowed_model_endpoint_ids": ["online-public-primary"],
        }
    )
    with pytest.raises(RoutingDenied, match="approval is missing"):
        router.route("P-PUBLIC-RESEARCH-PLAN", envelope)


def test_persisted_failure_summary_keeps_recovery_identity_without_raw_provider_text() -> None:
    exc = ProviderError(
        f"HTTP 503 api_key={FAKE_KEY}",
        kind=ProviderFailureKind.HTTP_STATUS,
        http_status=503,
        response_excerpt=f"Authorization: {FAKE_BEARER}",
        retryable_hint=True,
    )
    full = classify_runtime_failure(exc).to_dict()
    persisted = persistence_safe_failure_classification(full)
    serialized = json.dumps(persisted, ensure_ascii=False)

    assert persisted["failure_kind"] == ProviderFailureKind.HTTP_STATUS.value
    assert persisted["http_status"] == 503
    assert persisted["retryable"] is True
    assert "cause_chain" not in persisted
    assert "response_excerpt" not in serialized
    assert FAKE_KEY not in serialized
    assert "TESTTOKEN0123456789" not in serialized


def test_portable_run_trace_is_private_and_redacts_failure_credentials(tmp_path: Path) -> None:
    from app.portable_run_trace import PortableRunTrace

    run_dir = tmp_path / "portable" / "run-1"
    trace = PortableRunTrace(
        run_dir,
        project_id="project-1",
        workflow_type="WF-TEST",
        idempotency_key="test-run",
        options={"mode": "local"},
    )
    trace.record_preflight_failure(RuntimeError(f"provider rejected api_key={FAKE_KEY}"))
    bundle = trace.finalize(status="FAILED", error=RuntimeError(f"Authorization: {FAKE_BEARER}"))

    assert private_path_is_restricted(run_dir)
    for path in run_dir.rglob("*"):
        if path.is_file():
            assert private_path_is_restricted(path), path
    assert private_path_is_restricted(bundle)

    failure_text = (run_dir / "FAILURE.json").read_text(encoding="utf-8")
    result_text = (run_dir / "RUN_RESULT.json").read_text(encoding="utf-8")
    assert FAKE_KEY not in failure_text
    assert "TESTTOKEN0123456789" not in result_text
    assert "REDACTED_CREDENTIAL" in failure_text
    assert "REDACTED_CREDENTIAL" in result_text
