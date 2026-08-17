from __future__ import annotations

import copy
import json
import ast
from pathlib import Path

import pytest

from app.contracts import ReferenceSemantic, get_semantic_contract
from app.contracts.semantic_contract import load_semantic_contract
from app.executor import PromptExecutionError, PromptExecutor
from app.output_integrity import validate_reference_ids
from app.pack import PromptPack


ROOT = Path(__file__).resolve().parents[1]


def _executor() -> PromptExecutor:
    executor = PromptExecutor.__new__(PromptExecutor)
    executor.pack = PromptPack(ROOT / "prompt_pack")
    executor.db = None
    return executor


def test_every_schema_reference_field_is_contract_annotated() -> None:
    pack = PromptPack(ROOT / "prompt_pack")
    contract = get_semantic_contract()
    assert contract.version == "2.9.0"
    assert contract.rule_registry_version == "1.8.0"
    assert len(contract.reference_field_semantics) >= 90
    # PromptPack construction performs the complete inlined-schema coverage and
    # x-reference-semantic agreement check.  This assertion keeps the object live.
    assert pack.prompt_ids()


def test_diagnostic_text_is_not_forced_into_an_entity_id() -> None:
    pack = PromptPack(ROOT / "prompt_pack")
    output = pack.replay_output("P-WRITE-BLUEPRINT-CRITIC")
    output["result"]["invalid_slot_refs"] = [
        "段落 P-1 的 fact_slots 指向了当前输入中不存在的事实"
    ]
    errors = pack.validate("P-WRITE-BLUEPRINT-CRITIC", "output", output)
    assert errors == []
    assert (
        get_semantic_contract().field_semantic("invalid_slot_refs")
        is ReferenceSemantic.UNRESOLVED_DESCRIPTOR
    )


def test_executor_preserves_critic_status_verdict_and_findings() -> None:
    executor = _executor()
    output = executor.pack.replay_output("P-WRITE-BLUEPRINT-CRITIC")
    output["status"] = "REVISE"
    output["result"]["verdict"] = "REVISE"
    output["findings"] = [{
        "code": "CONTRACT_CONFLICT",
        "severity": "ERROR",
        "message": "critic finding must remain immutable",
        "evidence_refs": [],
        "suggested_fix": "repair producer output",
        "owner": "WRITING_AGENT",
    }]
    before = copy.deepcopy(output)
    normalized = executor._normalize_output("P-WRITE-BLUEPRINT-CRITIC", output)
    assert normalized["status"] == before["status"]
    assert normalized["result"]["verdict"] == before["result"]["verdict"]
    assert normalized["findings"] == before["findings"]


def test_executor_does_not_create_project_entities_or_rewrite_ids() -> None:
    executor = _executor()
    output = executor.pack.replay_output("P-PROJECT-DEFINITION-EXTRACT")
    output["result"]["project_definition"]["items"] = []
    output["result"]["project_definition"]["relations"] = []
    output["result"]["unmapped_source_spans"] = ["natural language is not an id"]
    before = copy.deepcopy(output["result"])
    normalized = executor._normalize_output("P-PROJECT-DEFINITION-EXTRACT", output)
    assert normalized["result"]["project_definition"]["items"] == []
    assert normalized["result"]["project_definition"]["relations"] == []
    assert normalized["result"]["unmapped_source_spans"] == before["unmapped_source_spans"]


def test_enum_normalization_never_inferrs_other_semantic_fields() -> None:
    output = {
        "warnings": [],
        "result": {
            "fact": {
                "knowledge_status": "PROJECT_DESIGN",
                "claim_type": "FACT",
                "temporal_status": "UNKNOWN",
                "source_refs": [],
            }
        },
    }
    normalized = PromptExecutor._normalize_semantic_enum_tree(output)
    fact = normalized["result"]["fact"]
    assert fact["claim_type"] == "FACT"
    assert fact["temporal_status"] == "UNKNOWN"


def test_dangling_reference_is_rejected_without_synthesizing_entity() -> None:
    output = {
        "result": {
            "paragraphs": [{
                "paragraph_id": "P-1",
                "required_evidence_ids": ["MISSING-FACT"],
            }]
        }
    }
    errors = validate_reference_ids(output, {"payload": {"facts": []}})
    assert errors
    assert "MISSING-FACT" in errors[0]
    assert output["result"]["paragraphs"][0]["required_evidence_ids"] == ["MISSING-FACT"]


def test_runtime_has_no_project_specific_identifier_repair() -> None:
    prohibited = ("RC-", "RQ-", "OBJ-", "EXP-", "F-077", "new-abstract")
    for relative in ("app/executor.py", "app/output_integrity.py"):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert not any(token in text for token in prohibited), relative



@pytest.mark.parametrize(
    "rule_id",
    [
        "SC-ARGUMENT-DETERMINISTIC-CHAINS",
        "SC-ARGUMENT-DESIGN-MATRIX-COMPLETENESS",
        "SC-ARGUMENT-EVIDENCE-REQUIREMENTS",
        "SC-ARGUMENT-CRITIC-ISSUE-TAXONOMY",
        "SC-ARGUMENT-DETERMINISTIC-DEFECTS",
        "SC-ARGUMENT-TARGETED-REPAIR-POLICY",
    ],
)
def test_v6_deterministic_argument_rule_must_be_registered(rule_id: str, tmp_path: Path) -> None:
    import yaml

    source = ROOT / "app" / "contracts" / "semantic_contract.yaml"
    raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    raw["rules"].pop(rule_id)
    mutated = tmp_path / "semantic_contract.yaml"
    mutated.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="missing required rules"):
        load_semantic_contract(mutated)


def _write_mutated_contract(tmp_path: Path, mutate) -> Path:
    import yaml

    source = ROOT / "app" / "contracts" / "semantic_contract.yaml"
    raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    mutate(raw)
    mutated = tmp_path / "semantic_contract.yaml"
    mutated.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return mutated


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("subject", "LEGACY_MODE", "unknown subject"),
        ("presence", "SOMETIMES", "unknown presence"),
        ("coverage", "SOME", "unknown coverage"),
    ],
)
def test_v6_evidence_quantifier_contract_rejects_unknown_policy(
    field: str,
    value: str,
    message: str,
    tmp_path: Path,
) -> None:
    def mutate(raw):
        raw["rules"]["SC-ARGUMENT-EVIDENCE-REQUIREMENTS"]["config"]["requirements"][0][
            field
        ] = value

    with pytest.raises(ValueError, match=message):
        load_semantic_contract(_write_mutated_contract(tmp_path, mutate))


def test_v6_evidence_requirement_must_reference_registered_defect_family(
    tmp_path: Path,
) -> None:
    def mutate(raw):
        raw["rules"]["SC-ARGUMENT-EVIDENCE-REQUIREMENTS"]["config"]["requirements"][0][
            "deterministic_defect_family"
        ] = "MISSING_FAMILY"

    with pytest.raises(ValueError, match="unknown deterministic_defect_family"):
        load_semantic_contract(_write_mutated_contract(tmp_path, mutate))


def test_v6_deterministic_defect_route_is_closed_enum(tmp_path: Path) -> None:
    def mutate(raw):
        raw["rules"]["SC-ARGUMENT-DETERMINISTIC-DEFECTS"]["config"]["families"][
            "CHAIN_SOURCE_SET_MISSING"
        ]["route"] = "MODEL_DECIDES"

    with pytest.raises(ValueError, match="unknown route"):
        load_semantic_contract(_write_mutated_contract(tmp_path, mutate))


def test_v6_argument_contract_schema_closure() -> None:
    contract = get_semantic_contract()
    meta = contract.rule("SC-ARGUMENT-DETERMINISTIC-DEFECTS").config
    schema = json.loads(
        (
            ROOT
            / "prompt_pack"
            / "schemas"
            / "prompts"
            / "argument_architecture_critic_output.schema.json"
        ).read_text(encoding="utf-8")
    )

    result_schema = schema["properties"]["result"]
    assert set(meta["critic_result_required_fields"]) <= set(result_schema["required"])

    receipt_schema = result_schema["properties"]["deterministic_receipts"]["items"]
    assert set(meta["receipt_required_fields"]) == set(receipt_schema["required"])

    finding_items = schema["properties"]["findings"]["items"]
    prompt_required = {
        field
        for branch in finding_items.get("allOf") or []
        for field in branch.get("required") or []
    }
    assert set(meta["critic_finding_required_fields"]) == prompt_required


def test_v6_deterministic_receipt_calls_are_registry_driven() -> None:
    contract = get_semantic_contract()
    meta = contract.rule("SC-ARGUMENT-DETERMINISTIC-DEFECTS").config
    families = set(meta["families"])
    tree = ast.parse((ROOT / "app" / "model_semantic_contracts.py").read_text(encoding="utf-8"))

    constant_families: set[str] = set()
    dynamic_family_calls = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == "_argument_deterministic_receipt"):
            continue
        keywords = {keyword.arg: keyword.value for keyword in node.keywords if keyword.arg}
        assert "failure_code" not in keywords
        assert "defect_family" in keywords
        family_node = keywords["defect_family"]
        if isinstance(family_node, ast.Constant) and isinstance(family_node.value, str):
            constant_families.add(family_node.value)
        else:
            dynamic_family_calls += 1

    assert constant_families <= families
    # Direct chain/matrix families are literal calls. Registry-driven evaluators
    # select their family dynamically from evidence/structural policy. Every
    # registered family must therefore be reachable from exactly one of these
    # deterministic sources rather than being an inert registry entry.
    evidence_requirements = contract.rule(
        "SC-ARGUMENT-EVIDENCE-REQUIREMENTS"
    ).config["requirements"]
    evidence_families = {
        str(requirement["deterministic_defect_family"])
        for requirement in evidence_requirements
    }
    structural_config = contract.rule(
        "SC-ARGUMENT-STRUCTURAL-REQUIREMENTS"
    ).config
    structural_families = {
        str(requirement["deterministic_defect_family"])
        for requirement in structural_config["requirements"]
    }
    graph_family = str(structural_config["graph_ownership"]["deterministic_defect_family"])
    assert constant_families | evidence_families | structural_families | {graph_family} == families
    assert dynamic_family_calls >= 1


def test_v6_contract_completeness_matrix_has_no_unowned_argument_rule() -> None:
    contract = get_semantic_contract()

    chain_config = contract.rule("SC-ARGUMENT-DETERMINISTIC-CHAINS").config
    assert chain_config["chains"]
    for chain in chain_config["chains"]:
        assert {
            "chain_type",
            "source_field",
            "target_fields",
            "relation",
            "coverage",
        } <= set(chain)
        assert str(chain["coverage"]).upper() in {"SOURCE", "TARGET", "BOTH"}

    evidence_config = contract.rule("SC-ARGUMENT-EVIDENCE-REQUIREMENTS").config
    defect_families = contract.rule(
        "SC-ARGUMENT-DETERMINISTIC-DEFECTS"
    ).config["families"]
    for requirement in evidence_config["requirements"]:
        assert {
            "requirement_id",
            "selector",
            "subject",
            "presence",
            "coverage",
            "deterministic_defect_family",
            "source_policy",
            "finding_code",
        } <= set(requirement)
        assert requirement["deterministic_defect_family"] in defect_families

    assert all("model_issue_owners" not in family for family in defect_families.values())
    state_config = contract.rule("SC-ARGUMENT-STATE-OWNERSHIP").config
    assert state_config["overlap_policy"] == "COEXIST"
    assert state_config["repair_mode"] == "AUTHORITATIVE_STATE_TRANSACTION"
    assert state_config["namespace_writers"] == {
        "MACHINE_DEFECT": "DETERMINISTIC_RUNTIME",
        "SEMANTIC_OBSERVATION": "ARGUMENT_CRITIC_RUNTIME_FROM_MODEL_ISSUES",
    }

    repair_config = contract.rule("SC-ARGUMENT-TARGETED-REPAIR-POLICY").config
    assert repair_config["editable_fields"]
    assert repair_config["machine_fields"]
    assert repair_config["machine_suffixes"]
    assert set(repair_config["editable_fields"]).isdisjoint(repair_config["machine_fields"])

def _schema_for_argument_selector(schema: dict, selector: str) -> dict:
    current = schema
    for part in [piece for piece in selector.split("/") if piece]:
        if part == "*":
            assert current.get("type") == "array", (selector, part, current)
            current = current["items"]
            continue
        assert current.get("type") == "object", (selector, part, current)
        properties = current.get("properties") or {}
        assert part in properties, f"selector {selector!r} references unknown schema field {part!r}"
        current = properties[part]
    return current


def test_v6_evidence_selectors_are_closed_against_model_output_schema() -> None:
    contract = get_semantic_contract()
    requirements = contract.rule("SC-ARGUMENT-EVIDENCE-REQUIREMENTS").config["requirements"]
    model_schema = json.loads(
        (
            ROOT
            / "prompt_pack"
            / "schemas"
            / "model"
            / "argument_architecture_model_output.schema.json"
        ).read_text(encoding="utf-8")
    )

    for requirement in requirements:
        selector = str(requirement["selector"])
        terminal = _schema_for_argument_selector(model_schema, selector)
        subject = str(requirement["subject"]).upper()
        expected_type = "object" if subject == "OBJECT" else "array"
        assert terminal.get("type") == expected_type, (
            requirement["requirement_id"],
            selector,
            terminal.get("type"),
            expected_type,
        )


def test_v6_repair_policy_overlap_fails_contract_load(tmp_path: Path) -> None:
    def mutate(raw):
        policy = raw["rules"]["SC-ARGUMENT-TARGETED-REPAIR-POLICY"]["config"]
        policy["editable_fields"].append(policy["machine_fields"][0])

    with pytest.raises(ValueError, match="editable/machine fields overlap"):
        load_semantic_contract(_write_mutated_contract(tmp_path, mutate))


def test_v6_defect_schema_contract_fields_must_be_nonempty(tmp_path: Path) -> None:
    def mutate(raw):
        raw["rules"]["SC-ARGUMENT-DETERMINISTIC-DEFECTS"]["config"][
            "critic_result_required_fields"
        ] = []

    with pytest.raises(ValueError, match="critic_result_required_fields must be non-empty"):
        load_semantic_contract(_write_mutated_contract(tmp_path, mutate))


def test_v6_evidence_selector_grammar_fails_closed(tmp_path: Path) -> None:
    def mutate(raw):
        raw["rules"]["SC-ARGUMENT-EVIDENCE-REQUIREMENTS"]["config"]["requirements"][0][
            "selector"
        ] = "research_threads/**/gap"

    with pytest.raises(ValueError, match="invalid selector"):
        load_semantic_contract(_write_mutated_contract(tmp_path, mutate))

def test_v6_critic_issue_taxonomy_is_closed_against_model_schema() -> None:
    contract = get_semantic_contract()
    taxonomy = contract.rule("SC-ARGUMENT-CRITIC-ISSUE-TAXONOMY").config
    schema = json.loads(
        (
            ROOT
            / "prompt_pack"
            / "schemas"
            / "model"
            / "argument_architecture_critic_model_output.schema.json"
        ).read_text(encoding="utf-8")
    )
    issue_schema = schema["properties"]["issues"]["items"]
    quality_schema = schema["properties"]["quality_dimensions"]["items"]
    dimensions = {str(value) for value in taxonomy["dimensions"]}
    schema_issue_dimensions = set(issue_schema["properties"]["dimension"]["enum"])
    schema_quality_dimensions = set(quality_schema["properties"]["dimension"]["enum"])
    assert dimensions == schema_issue_dimensions == schema_quality_dimensions

    taxonomy_codes = {
        str(code)
        for config in taxonomy["dimensions"].values()
        for code in config["issue_codes"]
    }
    schema_codes = set(issue_schema["properties"]["code"]["enum"])
    assert taxonomy_codes == schema_codes
    assert set(taxonomy["revision_component_by_code"]) == taxonomy_codes

def test_v6_runtime_has_no_parallel_critic_taxonomy_tables() -> None:
    source = (ROOT / "app" / "model_semantic_contracts.py").read_text(encoding="utf-8")
    assert "_CRITIC_DIMENSION_ISSUE_CODES" not in source
    assert "_REVISION_COMPONENT_BY_CODE" not in source
    assert "SC-ARGUMENT-CRITIC-ISSUE-TAXONOMY" in source



def test_v8_state_ownership_mutations_fail_closed(tmp_path: Path) -> None:
    def bad_overlap(raw):
        raw["rules"]["SC-ARGUMENT-STATE-OWNERSHIP"]["config"]["overlap_policy"] = "SUPPRESS"

    with pytest.raises(ValueError, match="overlap_policy must be COEXIST"):
        load_semantic_contract(_write_mutated_contract(tmp_path, bad_overlap))

    def bad_identity(raw):
        raw["rules"]["SC-ARGUMENT-STATE-OWNERSHIP"]["config"]["semantic_observation_identity"] = "DESCRIPTION_HASH"

    with pytest.raises(ValueError, match="semantic_observation_identity must be RUN_SCOPED_INSTANCE"):
        load_semantic_contract(_write_mutated_contract(tmp_path, bad_identity))

    def bad_writer(raw):
        raw["rules"]["SC-ARGUMENT-STATE-OWNERSHIP"]["config"]["field_writers"]["result/authored_state"] = "TARGETED_REPAIR_MODEL"

    with pytest.raises(ValueError, match="field_writers must assign exactly one declared writer"):
        load_semantic_contract(_write_mutated_contract(tmp_path, bad_writer))

    def bad_namespace(raw):
        raw["rules"]["SC-ARGUMENT-STATE-OWNERSHIP"]["config"]["namespace_writers"]["SEMANTIC_OBSERVATION"] = "DETERMINISTIC_RUNTIME"

    with pytest.raises(ValueError, match="separate writer namespaces"):
        load_semantic_contract(_write_mutated_contract(tmp_path, bad_namespace))

    def bad_guard_mode(raw):
        raw["rules"]["SC-ARGUMENT-STATE-OWNERSHIP"]["config"]["quality_guard_mode"] = "AUTHORITATIVE"

    with pytest.raises(ValueError, match="quality_guard_mode must be OBSERVE_CANONICAL_ONLY"):
        load_semantic_contract(_write_mutated_contract(tmp_path, bad_guard_mode))

    def bad_arbiter_mode(raw):
        raw["rules"]["SC-ARGUMENT-STATE-OWNERSHIP"]["config"]["decision_arbiter_mode"] = "OVERRIDE"

    with pytest.raises(ValueError, match="decision_arbiter_mode must be AUDIT_ONLY"):
        load_semantic_contract(_write_mutated_contract(tmp_path, bad_arbiter_mode))


def test_v8_argument_projection_schema_requires_every_persisted_projection_field() -> None:
    state = get_semantic_contract().rule("SC-ARGUMENT-STATE-OWNERSHIP").config
    expected = {"authored_state", *state["derived_result_fields"]}

    producer_schema = json.loads(
        (ROOT / "prompt_pack" / "schemas" / "prompts" / "argument_architecture_output.schema.json").read_text(encoding="utf-8")
    )
    producer_result = producer_schema["properties"]["result"]
    assert set(producer_result["required"]) == expected
    assert expected <= set(producer_result["properties"])

    critic_schema = json.loads(
        (ROOT / "prompt_pack" / "schemas" / "prompts" / "argument_architecture_critic_input.schema.json").read_text(encoding="utf-8")
    )
    candidate = critic_schema["properties"]["payload"]["properties"]["architecture_candidate"]
    assert set(candidate["required"]) == expected
    assert expected <= set(candidate["properties"])


def test_v8_semantic_observation_schema_forbids_machine_defect_identity() -> None:
    schema = json.loads(
        (ROOT / "prompt_pack" / "schemas" / "prompts" / "argument_architecture_critic_output.schema.json").read_text(encoding="utf-8")
    )
    all_of = schema["properties"]["findings"]["items"]["allOf"]
    required = next(part["required"] for part in all_of if "required" in part and "$ref" not in part)
    assert set(required) == {"finding_instance_id", "defect_key", "defect_namespace"}
    semantic_rule = next(
        part for part in all_of
        if ((part.get("if") or {}).get("properties") or {}).get("defect_namespace", {}).get("const") == "SEMANTIC_OBSERVATION"
    )
    assert semantic_rule["then"]["properties"]["defect_key"] == {"type": "null"}


def test_v8_generic_reference_integrity_does_not_reinterpret_authoritative_business_collections() -> None:
    payload = {
        "result": {
            "authored_state": {
                "research_threads": [
                    {"work_packages": [{"methods": [{"statement": "business object, not a reference id"}]}]}
                ]
            }
        }
    }
    assert validate_reference_ids(payload, {}) == []
