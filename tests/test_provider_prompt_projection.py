from __future__ import annotations

import copy
import json
from pathlib import Path

from app.executor import (
    MODEL_CONTEXT_PROJECTION_VERSION,
    MODEL_SYSTEM_PROMPT_VERSION,
    PromptExecutor,
)
from app.output_integrity import attach_trusted_source_catalog
from app.pack import PromptPack


ROOT = Path(__file__).resolve().parents[1]
WF3_MODEL_PROMPTS = (
    "P-SAFE-ONLINE-PACKAGE",
    "P-SAFE-ONLINE-PACKAGE-CRITIC",
    "P-PUBLIC-RESEARCH-PLAN",
    "P-PUBLIC-RESEARCH-SYNTHESIS",
    "P-PUBLIC-RESEARCH-CRITIC",
    "P-ONLINE-RESULT-IMPORT-CRITIC",
)


def _executor() -> tuple[PromptExecutor, PromptPack]:
    pack = PromptPack(ROOT / "prompt_pack")
    executor = PromptExecutor.__new__(PromptExecutor)
    executor.pack = pack
    return executor, pack


def _source_refs(node):
    found = []
    if isinstance(node, list):
        for item in node:
            found.extend(_source_refs(item))
    elif isinstance(node, dict):
        for key, value in node.items():
            if key == "source_ref" and isinstance(value, dict):
                found.append(value)
            elif key == "source_refs" and isinstance(value, list):
                found.extend(item for item in value if isinstance(item, dict))
            found.extend(_source_refs(value))
    return found


def _hash_paths(node, path=""):
    found = []
    if isinstance(node, list):
        for index, item in enumerate(node):
            found.extend(_hash_paths(item, f"{path}/{index}"))
    elif isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}/{key}"
            if key == "hash" or key.endswith("_hash"):
                found.append(child)
            found.extend(_hash_paths(value, child))
    return found




def _unexpected_hash_paths(node):
    unexpected = []
    for path in _hash_paths(node):
        if path.endswith("/source_hash") and "/trace_links/" in path:
            continue
        if path.endswith("/hash") and "/payload/protected_hashes/" in path:
            continue
        unexpected.append(path)
    return unexpected

def _two_stage_projection(executor: PromptExecutor, validation: dict):
    contract, contract_report = executor._prepare_provider_contract_envelope(validation)
    provider, business_report = executor._prepare_provider_envelope(contract)
    return contract, provider, contract_report, business_report


def test_provider_contract_projection_is_schema_valid_for_every_prompt_and_business_projection_does_not_mutate_validation_context() -> None:
    executor, pack = _executor()
    for prompt_id in pack.prompt_ids():
        validation = attach_trusted_source_catalog(pack.replay_input(prompt_id))
        snapshot = copy.deepcopy(validation)

        contract, provider, contract_report, business_report = _two_stage_projection(
            executor, validation
        )

        assert validation == snapshot, prompt_id
        assert "trusted_source_catalog" in validation, prompt_id
        assert "trusted_source_catalog" not in contract, prompt_id
        assert "trusted_source_catalog" not in provider, prompt_id

        # Full input validation is retained.  The schema-valid intermediate
        # contract projection is validated before machine-only fields are
        # removed from the actual model request.
        assert pack.validate(prompt_id, "input", validation) == [], prompt_id
        assert pack.validate(prompt_id, "input", contract) == [], prompt_id

        assert business_report["projection_version"] == MODEL_CONTEXT_PROJECTION_VERSION
        assert business_report["provider_contract_was_schema_validated"] is True
        assert business_report["provider_envelope_chars"] <= contract_report["provider_contract_chars"]

        # Runtime-only hashes are removed.  Only hashes that the current
        # output contract explicitly requires the model to preserve may remain.
        assert _unexpected_hash_paths(provider) == [], prompt_id

        for ref in _source_refs(provider):
            assert "source_hash" not in ref, prompt_id
            assert "document_version_id" not in ref, prompt_id
            assert "span_start" not in ref, prompt_id
            assert "span_end" not in ref, prompt_id
            assert ref.get("source_id"), prompt_id


def test_wf3_actual_system_prompts_make_runtime_provenance_boundary_unambiguous() -> None:
    executor, pack = _executor()
    for prompt_id in WF3_MODEL_PROMPTS:
        validation = attach_trusted_source_catalog(pack.replay_input(prompt_id))
        _, provider, _, _ = _two_stage_projection(executor, validation)
        system_prompt = executor._system_prompt(
            prompt_id,
            pack.inlined_schema(prompt_id, "output"),
            provider,
            semantic_model_contract=False,
        )

        assert "顶层source_refs返回[]" in system_prompt, prompt_id
        assert "新建ID填runtime" in system_prompt, prompt_id
        assert "不要计算Hash或把路径当source_id" in system_prompt, prompt_id
        assert "trusted_source_catalog" not in system_prompt, prompt_id
        assert "trusted_source_catalog" not in json.dumps(provider, ensure_ascii=False), prompt_id
        assert "先验证每个对象的ID、版本、Hash与安全标签" not in system_prompt, prompt_id

    synthesis = pack.prompt_text("P-PUBLIC-RESEARCH-SYNTHESIS")
    assert "result.claims[].source_refs" in executor._system_prompt(
        "P-PUBLIC-RESEARCH-SYNTHESIS",
        pack.inlined_schema("P-PUBLIC-RESEARCH-SYNTHESIS", "output"),
        pack.replay_input("P-PUBLIC-RESEARCH-SYNTHESIS"),
        semantic_model_contract=False,
    )
    assert "逐结论选择的输入`source_id`" in synthesis


def test_argument_architecture_provider_request_is_materially_smaller_without_weakening_validation_context() -> None:
    executor, pack = _executor()
    prompt_id = "P-ARGUMENT-ARCHITECTURE"
    validation = attach_trusted_source_catalog(pack.replay_input(prompt_id))
    contract, provider, contract_report, business_report = _two_stage_projection(
        executor, validation
    )
    schema = pack.inlined_schema(prompt_id, "output")
    system_prompt = executor._system_prompt(prompt_id, schema, provider)

    original_chars = contract_report["validation_context_chars"]
    final_chars = business_report["provider_envelope_chars"]
    total_saved_ratio = (original_chars - final_chars) / original_chars

    assert total_saved_ratio >= 0.40
    assert len(pack.prompt_text(prompt_id)) < 1400
    assert len(system_prompt) < 20000
    assert MODEL_SYSTEM_PROMPT_VERSION.startswith("2026-08-13.")
    assert "# 运行时契约边界" not in system_prompt
    assert "语义任务通则" in system_prompt
    assert "FIELD_OWNERSHIP_CONTRACT:START" not in system_prompt
    assert "REFERENCE_INTEGRITY_CONTRACT:START" not in system_prompt
    assert "统一语义契约（运行时生成）" not in system_prompt
    assert "trusted_source_catalog" not in json.dumps(provider, ensure_ascii=False)
    assert _unexpected_hash_paths(provider) == []

    # Obvious runtime wrapper identities are not useful to the producer.
    assert "prompt_id" not in provider
    assert "task_id" not in provider.get("task", {})
    assert "project_id" not in provider.get("scope", {})
    assert (
        "task_instruction_id"
        not in provider.get("payload", {}).get("task_instruction", {})
    )
    assert "freshness" not in provider

    # Semantic graph identity remains available.
    graph = provider["payload"]["argument_graph_seed"]
    assert graph["graph_id"]
    assert graph["central_proposition"]["node_id"]
    assert graph["nodes"][0]["node_id"]
    assert graph["edges"][0]["source_id"]
    assert graph["edges"][0]["target_id"]

    # The deterministic validator still receives complete trusted/hash context.
    assert validation["trusted_source_catalog"]
    assert validation["freshness"]["project_definition_hash"]
    assert validation["payload"]["project_subgraph"]["items"][0]["item_hash"]
    assert validation["payload"]["current_sections"][0]["text_hash"]


def test_provider_projection_keeps_section_identity_and_evidence_text_visible() -> None:
    executor, _ = _executor()
    validation = {
        "trusted_source_catalog": [{"source_id": "src-1"}],
        "payload": {
            "source_refs": [
                {
                    "source_id": "src-1",
                    "source_type": "EVIDENCE_MATERIAL",
                    "authority_rank": 90,
                    "security_level": "INTERNAL",
                    "document_version_id": "ver-1",
                    "section_id": "sec-1",
                    "span_start": 1,
                    "span_end": 9,
                    "quoted_text": "关键证据",
                    "source_hash": "a" * 64,
                }
            ]
        },
    }
    contract, provider, _, _ = _two_stage_projection(executor, validation)
    assert contract["payload"]["source_refs"][0]["source_id"] == "src-1"
    ref = provider["payload"]["source_refs"][0]
    assert ref == {
        "source_id": "src-1",
        "source_type": "EVIDENCE_MATERIAL",
        "authority_rank": 90,
        "security_level": "INTERNAL",
        "section_id": "sec-1",
        "quoted_text": "关键证据",
    }


def test_targeted_repair_model_projection_removes_hash_bookkeeping_but_keeps_repair_authority() -> None:
    executor, pack = _executor()
    validation = attach_trusted_source_catalog(pack.replay_input("P-TARGETED-REPAIR"))
    contract, provider, _, _ = _two_stage_projection(executor, validation)

    assert pack.validate("P-TARGETED-REPAIR", "input", contract) == []
    assert _unexpected_hash_paths(provider) == []
    payload = provider["payload"]

    # Protected hashes remain because the current repair output contract
    # requires an exact unchanged_protected_hashes receipt.  Other hash
    # bookkeeping is removed.
    assert payload["protected_hashes"]
    assert all(item.get("hash") for item in payload["protected_hashes"])
    assert payload["allowed_paths"]
    assert payload["protected_paths"]
    assert payload["findings_to_repair"]
    assert payload["original_object"]["content"]
    assert "object_hash" not in payload["original_object"]

    for entry in payload.get("inherited_source_catalog") or []:
        assert "source_hash" not in entry
        assert "document_version_id" not in entry


def test_business_projection_only_removes_explicit_runtime_ids_not_semantic_ids() -> None:
    executor, pack = _executor()
    validation = attach_trusted_source_catalog(pack.replay_input("P-ARGUMENT-ARCHITECTURE"))
    _, provider, _, _ = _two_stage_projection(executor, validation)

    assert "prompt_id" not in provider
    assert "task_id" not in provider["task"]
    assert "project_id" not in provider["scope"]
    assert "task_instruction_id" not in provider["payload"]["task_instruction"]

    subgraph_item = provider["payload"]["project_subgraph"]["items"][0]
    assert subgraph_item["item_id"]

    graph = provider["payload"]["argument_graph_seed"]
    assert graph["graph_id"]
    assert graph["central_proposition"]["node_id"]
    assert all(node["node_id"] for node in graph["nodes"])
    assert all(edge["edge_id"] for edge in graph["edges"])
    assert all(edge["source_id"] and edge["target_id"] for edge in graph["edges"])


def test_shared_skill_modules_are_loaded_only_for_relevant_capabilities() -> None:
    _, pack = _executor()

    argument_shared = pack.shared_prompt_for("P-ARGUMENT-ARCHITECTURE")
    assert "语义任务通则" in argument_shared
    assert "Mermaid图形技能" not in argument_shared
    assert "公共研究技能" not in argument_shared

    write_shared = pack.shared_prompt_for("P-WRITE-CONTENT")
    assert "弱模型任务边界" in write_shared
    assert "Mermaid图形技能" in write_shared
    assert "公共研究技能" not in write_shared

    expression_shared = pack.shared_prompt_for("P-EXPRESSION-POLISH")
    assert "Mermaid图形技能" not in expression_shared
    assert "公共研究技能" not in expression_shared

    research_shared = pack.shared_prompt_for("P-PUBLIC-RESEARCH-SYNTHESIS")
    assert "弱模型任务边界" in research_shared
    assert "公共研究技能" in research_shared
    assert "Mermaid图形技能" not in research_shared


def test_argument_architecture_projection_removes_only_redundant_container_metadata() -> None:
    executor, pack = _executor()
    validation = attach_trusted_source_catalog(pack.replay_input("P-ARGUMENT-ARCHITECTURE"))
    _, provider, _, business_report = _two_stage_projection(executor, validation)

    payload = provider["payload"]

    for section in payload.get("current_sections") or []:
        assert "section_id" in section
        assert "section_key" in section
        assert "title" in section
        assert "level" in section
        assert "text" in section
        for field in (
            "block_ids",
            "contains_table",
            "contains_formula",
            "contains_image",
            "contains_comment",
            "contains_revision",
            "security_level",
        ):
            assert field not in section

    subgraph = payload["project_subgraph"]
    assert "item_ids" not in subgraph
    assert "relation_ids" not in subgraph
    assert subgraph["items"]
    assert isinstance(subgraph["relations"], list)

    item = subgraph["items"][0]
    assert item["item_id"]
    assert item["item_type"]
    assert item["content"]
    assert "owner_ref" not in item
    assert "security_level" not in item
    if item.get("source_refs"):
        assert "security_level" in item["source_refs"][0]
    # Semantically meaningful lifecycle and confidence fields stay visible.
    assert "locked" in item
    assert "knowledge_status" in item
    assert "confidence" in item

    removed = business_report["removed_machine_metadata_fields"]
    assert removed


def test_argument_architecture_system_prompt_does_not_load_unrelated_skill_modules() -> None:
    executor, pack = _executor()
    prompt_id = "P-ARGUMENT-ARCHITECTURE"
    validation = attach_trusted_source_catalog(pack.replay_input(prompt_id))
    contract, _, _, _ = _two_stage_projection(executor, validation)
    provider, _ = executor._prepare_provider_envelope(contract)
    system_prompt = executor._system_prompt(
        prompt_id, pack.inlined_schema(prompt_id, "output"), provider
    )

    assert "Mermaid图形技能" not in system_prompt
    assert "公共研究技能" not in system_prompt
    assert "语义任务通则" in system_prompt
    assert "# 运行时契约边界" not in system_prompt
    assert len(system_prompt) < 18000
