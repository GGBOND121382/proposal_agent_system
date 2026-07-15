from __future__ import annotations

import asyncio
import json
import subprocess
import tarfile
from pathlib import Path

from app.config import Settings
from app.db import Database
from app.executor import PromptExecutor
from app.llm import ModelGateway
from app.pack import PromptPack
from app.security import SecurityRouter
from app.util import utc_now
from scripts.audit_prompt_traces import audit
import scripts.f_recovery_bundle as recovery

ROOT = Path(__file__).resolve().parents[1]


def create_runtime(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MODEL_RUNTIME_MODE", "SIMULATED")
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PROMPT_PACK_DIR", str(ROOT / "prompt_pack"))
    settings = Settings.load()
    pack = PromptPack(settings.prompt_pack_dir)
    db = Database(settings.db_path)
    executor = PromptExecutor(
        db,
        pack,
        SecurityRouter(pack),
        ModelGateway(settings, pack),
        quality_guard_enabled=False,
    )
    now = utc_now()
    config = {
        "internet_access_allowed": True,
        "anonymized_external_processing_allowed": True,
        "allowed_public_topics": ["公开政策"],
        "prohibited_external_fields": ["真实项目名称"],
        "recipient_scope": ["内部用户"],
        "allowed_model_endpoint_ids": ["offline-primary", "online-public-primary"],
        "retention_days": 365,
    }
    db.execute(
        "INSERT INTO projects(id,name,description,security_level,config_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        ("project-f-trace", "F Trace", "", "INTERNAL", json.dumps(config), now, now),
    )
    return settings, pack, db, executor


async def create_calls(pack: PromptPack, executor: PromptExecutor):
    for prompt_id in ("P-SCHEME-EXTRACT", "P-WRITE-CONTENT", "P-INTEGRATION-CRITIC"):
        result = await executor.execute(
            prompt_id,
            pack.replay_input(prompt_id),
            project_id="project-f-trace",
        )
        assert result["status"] != "ERROR"


def test_trace_audit_and_recovery_bundle(tmp_path: Path, monkeypatch):
    settings, pack, _, executor = create_runtime(tmp_path, monkeypatch)
    asyncio.run(create_calls(pack, executor))

    trace_dir = tmp_path / "trace"
    report = audit(settings.db_path, trace_dir)
    assert report["status"] == "PASS", report["errors"]
    assert report["run_count"] == report["trace_count"] == report["evidence_count"] == 3
    calls = [json.loads(line) for line in (trace_dir / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
    required = {
        "run_id", "trace_artifact_id", "prompt_id", "responsibility_agent", "status",
        "duration_ms", "environment", "model_id", "endpoint_id", "input_hash",
        "output_hash", "input_envelope", "raw_response_text", "parsed_output",
    }
    assert all(required <= set(item) for item in calls)

    test_log = tmp_path / "pytest.log"
    test_log.write_text("F trace and recovery test evidence\n", encoding="utf-8")
    bundle = tmp_path / "recovery_evidence" / "f-recovery.zip"
    built = recovery.build(settings.db_path, bundle, [test_log])
    assert built["status"] == "PASS"
    restored = tmp_path / "restored"
    checked = recovery.verify(bundle, restored)
    assert checked["status"] == "PASS", checked["errors"]
    assert (restored / "workflow_checkpoint.sqlite").is_file()
    assert (restored / "source" / "source.tar.gz").is_file()
    assert (restored / "prompt_traces" / "calls.jsonl").is_file()


def test_gitless_source_snapshot_fallback(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SOURCE_COMMIT", "snapshot-test-commit")

    def no_git(*_args, **_kwargs):
        raise subprocess.CalledProcessError(128, "git")

    monkeypatch.setattr(recovery, "git", no_git)
    monkeypatch.setattr(recovery.subprocess, "run", no_git)

    assert recovery.resolve_source_commit() == "snapshot-test-commit"
    assert recovery.material_paths()

    archive = tmp_path / "source.tar.gz"
    mode = recovery.create_source_archive(archive, "snapshot-test-commit")
    assert mode == "filesystem-snapshot"
    assert archive.is_file()
    with tarfile.open(archive, "r:gz") as stream:
        names = set(stream.getnames())
    assert "requirements-dev.txt" in names
    assert "scripts/f_recovery_bundle.py" in names
