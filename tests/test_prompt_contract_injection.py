from __future__ import annotations

import re
from pathlib import Path

from app.contracts.semantic_contract import (
    SEMANTIC_CONTRACT_PROMPT_MARKER,
    get_semantic_contract,
)
from app.executor import PromptExecutor
from app.contract_registry import augment_prompt_with_reference_integrity_contract


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
    assert "P-ABS-005.must_answer" in prompt
    assert "never 'must_answer' alone" in prompt


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


def test_blueprint_critic_treats_blueprint_fields_as_the_paragraph_design() -> None:
    prompt = (ROOT / "prompt_pack/prompts/writing/write_blueprint_critic.md").read_text(
        encoding="utf-8"
    )
    assert "合起来就是“段落设计”" in prompt
    assert "不得把“质量不足”表述为“字段不存在”" in prompt
    assert "禁止沿用修复前的结论" in prompt


def test_blueprint_producer_explains_information_key_children_and_self_evidence() -> None:
    prompt = (ROOT / "prompt_pack/prompts/writing/write_blueprint.md").read_text(
        encoding="utf-8"
    )

    assert "先逐字复制`section_contract.unique_information_keys`" in prompt
    assert "<逐字根键>:<本段唯一子键>" in prompt
    assert "RQ-1至RQ-4与GAP-1至GAP-4的映射:RQ-1与GAP-1" in prompt
    assert "数量必须等于去重后的数量" in prompt
    assert "primary_claim_id`不得出现在该段`required_evidence_ids" in prompt


def test_generated_reference_contract_names_diagnostic_evidence_paths() -> None:
    schema = {
        "type": "object",
        "properties": {
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "evidence_refs": {
                            "type": "array",
                            "items": {"type": "string"},
                            "x-reference-semantic": "ENTITY_OR_FIELD_PATH",
                        }
                    },
                },
            }
        },
    }

    prompt = augment_prompt_with_reference_integrity_contract(
        "base", schema, contract_id="test"
    )

    assert "$.findings[*].evidence_refs" in prompt
    assert "禁止在同一字符串中追加括号说明" in prompt
    assert "description或repair_instruction" in prompt
    assert "禁止把input-*合成目录ID写入evidence_refs" in prompt
    assert "例如polished_candidate" in prompt
