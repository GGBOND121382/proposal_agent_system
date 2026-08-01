from __future__ import annotations

import re
from pathlib import Path

from app.contracts.semantic_contract import (
    SEMANTIC_CONTRACT_PROMPT_MARKER,
    get_semantic_contract,
)
from app.executor import PromptExecutor


ROOT = Path(__file__).resolve().parents[1]


class _Pack:
    shared_prompt = f"shared-before\n\n{SEMANTIC_CONTRACT_PROMPT_MARKER}\n\nshared-after"

    @staticmethod
    def prompt_text(prompt_id: str) -> str:
        return f"prompt:{prompt_id}"


def _executor() -> PromptExecutor:
    executor = PromptExecutor.__new__(PromptExecutor)
    executor.pack = _Pack()
    return executor


def _system_prompt(prompt_id: str) -> str:
    return _executor()._system_prompt(
        prompt_id,
        {"type": "object", "properties": {}},
        {"payload": {}, "task": {}, "scope": {}, "security_context": {}, "freshness": {}},
    )


def test_shared_markdown_contains_only_the_runtime_injection_marker() -> None:
    shared_path = ROOT / "prompt_pack" / "prompts" / "shared" / "semantic_contract.md"
    assert shared_path.read_text(encoding="utf-8").strip() == SEMANTIC_CONTRACT_PROMPT_MARKER


def test_contract_hash_is_canonical_and_traceable() -> None:
    contract = get_semantic_contract()
    assert re.fullmatch(r"[0-9a-f]{64}", contract.contract_hash)
    assert contract.contract_hash == get_semantic_contract().contract_hash
    assert contract.prompt_identity().splitlines() == [
        f"Semantic-Contract-Version: {contract.version}",
        f"Semantic-Rule-Registry-Version: {contract.rule_registry_version}",
        f"Semantic-Contract-SHA256: {contract.contract_hash}",
    ]


def test_system_prompt_injects_the_generated_contract_exactly_once() -> None:
    prompt = _system_prompt("P-WRITE-BLUEPRINT")
    contract = get_semantic_contract()
    assert SEMANTIC_CONTRACT_PROMPT_MARKER not in prompt
    assert prompt.count("# 统一语义契约（运行时生成）") == 1
    assert prompt.count(f"Semantic-Contract-SHA256: {contract.contract_hash}") == 1
    for rule_id in contract.rule_ids:
        assert prompt.count(f"`{rule_id}`") == 1


def test_producer_critic_and_repair_receive_the_same_contract_identity() -> None:
    contract = get_semantic_contract()
    identity = f"Semantic-Contract-SHA256: {contract.contract_hash}"
    for prompt_id in (
        "P-WRITE-BLUEPRINT",
        "P-WRITE-BLUEPRINT-CRITIC",
        "P-TARGETED-REPAIR",
    ):
        prompt = _system_prompt(prompt_id)
        assert identity in prompt
        assert prompt.count(identity) == 1


def test_missing_marker_falls_back_to_one_generated_contract() -> None:
    executor = PromptExecutor.__new__(PromptExecutor)

    class PackWithoutMarker:
        shared_prompt = "legacy shared prompt"

        @staticmethod
        def prompt_text(prompt_id: str) -> str:
            return prompt_id

    executor.pack = PackWithoutMarker()
    prompt = executor._system_prompt(
        "P-WRITE-BLUEPRINT",
        {"type": "object", "properties": {}},
        {"payload": {}, "task": {}, "scope": {}, "security_context": {}, "freshness": {}},
    )
    assert prompt.count("# 统一语义契约（运行时生成）") == 1
