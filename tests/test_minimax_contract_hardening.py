from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings
from app.db import Database
from app.executor import PromptExecutionError, PromptExecutor
from app.llm import ModelGateway
from app.pack import PromptPack
from app.security import SecurityRouter


@pytest.fixture()
def hardening_runtime(tmp_path: Path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("MODEL_RUNTIME_MODE", "SIMULATED")
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PROMPT_PACK_DIR", str(root / "prompt_pack"))
    settings = Settings.load()
    pack = PromptPack(settings.prompt_pack_dir)
    db = Database(settings.db_path)
    router = SecurityRouter(pack)
    gateway = ModelGateway(settings, pack)
    executor = PromptExecutor(db, pack, router, gateway)
    return pack, executor


def test_argument_revise_with_blocking_user_question_routes_to_need_user_input(hardening_runtime):
    pack, executor = hardening_runtime
    output = pack.replay_output("P-ARGUMENT-ARCHITECTURE", "normal")
    output["status"] = "REVISE"
    output["user_questions"] = [{
        "question_id": "UQ-HARDEN-001",
        "question_type": "MISSING_INFORMATION",
        "question": "请提供团队前期研究基础的可核验来源。",
        "reason": "缺少必须由项目负责人提供的团队基础证据。",
        "target_paths": ["/payload/confirmed_facts"],
        "answer_schema": {"type": "ARRAY", "allowed_values": []},
        "blocking": True,
        "priority": "P0",
    }]

    normalized = executor._normalize_output(
        "P-ARGUMENT-ARCHITECTURE",
        output,
        pack.replay_input("P-ARGUMENT-ARCHITECTURE"),
    )

    assert normalized["status"] == "NEED_USER_INPUT"
    assert normalized["user_questions"] == output["user_questions"]


def test_argument_revise_with_blocking_user_routed_finding_routes_to_need_user_input(hardening_runtime):
    pack, executor = hardening_runtime
    output = pack.replay_output("P-ARGUMENT-ARCHITECTURE", "normal")
    output["status"] = "REVISE"
    output["user_questions"] = []
    output["findings"] = [{
        "finding_instance_id": "F-HARDEN-001",
        "code": "FOUNDATION_EVIDENCE_MISSING",
        "severity": "P0",
        "category": "ARGUMENT",
        "target_type": "ARGUMENT_NODE",
        "target_path_or_span": "/result/argument_architecture/nodes/0",
        "description": "团队研究基础缺少可核验来源。",
        "evidence_refs": [],
        "repairable": False,
        "repair_instruction": "由项目负责人提供论文、专利、项目或预实验来源。",
        "suggested_route": "USER",
        "blocking": True,
    }]

    normalized = executor._normalize_output(
        "P-ARGUMENT-ARCHITECTURE",
        output,
        pack.replay_input("P-ARGUMENT-ARCHITECTURE"),
    )

    assert normalized["status"] == "NEED_USER_INPUT"
    assert normalized["findings"] == output["findings"]


def test_argument_dangling_design_id_error_explains_node_definition_location(hardening_runtime):
    pack, executor = hardening_runtime
    output = pack.replay_output("P-ARGUMENT-ARCHITECTURE", "normal")
    envelope = pack.replay_input("P-ARGUMENT-ARCHITECTURE")
    output["result"]["research_design_matrix"][0]["method_ids"] = ["PRD-METHOD-001"]

    with pytest.raises(PromptExecutionError) as exc_info:
        executor._normalize_output("P-ARGUMENT-ARCHITECTURE", output, envelope)

    assert any(
        "PRD-METHOD-001" in error
        and "/result/argument_architecture/nodes" in error
        for error in exc_info.value.validation_errors
    )


def test_argument_prompt_has_weak_model_hardening_contract(hardening_runtime):
    pack, _ = hardening_runtime
    prompt = pack.prompt_text("P-ARGUMENT-ARCHITECTURE")
    shared = pack.shared_prompt

    assert "不得替代其他智能体完成事实确认、论证架构" not in prompt
    assert "result.argument_architecture.nodes[]" in prompt
    assert "user_questions[*].blocking=true" in prompt
    assert "最终`status`必须为`NEED_USER_INPUT`" in prompt
    assert "无问题" in shared
    assert "紧凑JSON" in shared
