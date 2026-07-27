from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.contract_registry import (
    CONTRACT_REGISTRY_VERSION,
    FIELD_ALIAS_CANDIDATES,
    normalize_against_schema,
)
from app.status_ontology import (
    CANONICAL_CLAIM_TYPES,
    CANONICAL_KNOWLEDGE_STATUSES,
    CANONICAL_TEMPORAL_STATUSES,
)

CORE_CONTRACTS = {
    "knowledge_status": set(CANONICAL_KNOWLEDGE_STATUSES),
    "claim_type": set(CANONICAL_CLAIM_TYPES),
    "temporal_status": set(CANONICAL_TEMPORAL_STATUSES),
}
STAGED_FILES = [
    "stage1_tools/stage1_design_input.py",
    "stage2_tools/stage2_guide_fact_base.py",
    "stage3_tools/stage3_project_definition.py",
    "stage4_tools/stage4_argument_architecture.py",
    "stage4a_tools/stage4a_evidence_completion.py",
    "stage5_tools/stage5_section_planning.py",
    "stage6a_tools/stage6a_drafting.py",
    "stage6b_tools/stage6b_drafting.py",
    "stage6c_tools/stage6c_drafting.py",
    "stage6d_tools/stage6d_drafting.py",
    "stage7_tools/stage7_integration.py",
]


def schema_files(root: Path) -> list[Path]:
    files = list((root / "prompt_pack" / "schemas").rglob("*.json"))
    files += list(root.glob("stage*_tools/*.schema.json"))
    return sorted(set(files))


def walk_schema(node: Any, path: str = "$") -> Iterable[tuple[str, str, list[Any]]]:
    if isinstance(node, list):
        for index, item in enumerate(node):
            yield from walk_schema(item, f"{path}/{index}")
        return
    if not isinstance(node, dict):
        return
    for key, value in node.items():
        current = f"{path}/{key}"
        if isinstance(value, dict) and ("enum" in value or "const" in value):
            allowed = list(value.get("enum", [value.get("const")]))
            yield key, current, allowed
        yield from walk_schema(value, current)


def audit(root: Path) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    enum_paths: list[dict[str, Any]] = []
    field_vocabularies: dict[str, set[tuple[str, ...]]] = defaultdict(set)

    for path in schema_files(root):
        document = json.loads(path.read_text(encoding="utf-8"))
        for field, json_path, allowed in walk_schema(document):
            enum_paths.append({
                "file": str(path.relative_to(root)),
                "field": field,
                "path": json_path,
                "allowed_values": allowed,
            })
            field_vocabularies[field].add(tuple(str(x) for x in allowed))
            if field in CORE_CONTRACTS:
                illegal = sorted(set(str(x) for x in allowed) - CORE_CONTRACTS[field])
                if illegal:
                    findings.append({
                        "code": "CORE_VOCABULARY_DRIFT",
                        "severity": "BLOCKING",
                        "file": str(path.relative_to(root)),
                        "field": field,
                        "path": json_path,
                        "illegal_values": illegal,
                    })

    # All staged model-response entry points must share the same pre-schema
    # normalizer and all request writers must inject the registry-derived enum
    # contract.
    staged_integration: list[dict[str, Any]] = []
    for rel in STAGED_FILES:
        path = root / rel
        text = path.read_text(encoding="utf-8")
        has_ingest = "def ingest_" in text
        checks = {
            "normalize_in_place": "normalize_in_place(" in text,
            "trace_context": "set_contract_trace_context(" in text,
            "prompt_contract_injection": "prepare_staged_artifact(" in text,
        }
        staged_integration.append({"file": rel, "has_ingest": has_ingest, **checks})
        if has_ingest:
            for key, passed in checks.items():
                if not passed:
                    findings.append({
                        "code": "STAGED_CONTRACT_GATEWAY_MISSING",
                        "severity": "BLOCKING",
                        "file": rel,
                        "missing": key,
                    })

    executor_text = (root / "app" / "executor.py").read_text(encoding="utf-8")
    prompt_pack_checks = {
        "schema_normalizer": "normalize_against_schema(" in executor_text,
        "prompt_contract_injection": "augment_prompt_with_enum_contract(" in executor_text,
        "inlined_output_schema": 'getattr(self.pack, "inlined_schema"' in executor_text,
    }
    for key, passed in prompt_pack_checks.items():
        if not passed:
            findings.append({
                "code": "PROMPT_PACK_CONTRACT_GATEWAY_MISSING",
                "severity": "BLOCKING",
                "file": "app/executor.py",
                "missing": key,
            })

    # Verify every registered alias that has a target in a schema can actually
    # be consumed by the normalizer.  This catches dead mappings and field-name
    # dispatch regressions.
    alias_test_count = 0
    alias_failures: list[dict[str, Any]] = []
    field_allowed_sets: dict[str, list[list[Any]]] = defaultdict(list)
    for item in enum_paths:
        field_allowed_sets[item["field"]].append(item["allowed_values"])
    for field, aliases in FIELD_ALIAS_CANDIDATES.items():
        for alias, targets in aliases.items():
            for allowed in field_allowed_sets.get(field, []):
                expected = next((target for target in targets if target in allowed), None)
                if expected is None:
                    continue
                alias_test_count += 1
                parent = {field: alias}
                if field == "status" and "CONFIRMED_DESIGN" in allowed:
                    parent["node_type"] = "MECHANISM"
                schema = {
                    "type": "object",
                    "properties": {
                        key: ({"type": "string"} if key != field else {"enum": allowed})
                        for key in parent
                    },
                }
                normalized, report = normalize_against_schema(parent, schema, contract_id="registry-self-test")
                if normalized.get(field) != expected and normalized.get(field) not in allowed:
                    alias_failures.append({
                        "field": field,
                        "alias": alias,
                        "allowed": allowed,
                        "expected_one_of": [x for x in targets if x in allowed],
                        "actual": normalized.get(field),
                        "report": report,
                    })
    for failure in alias_failures:
        findings.append({"code": "REGISTERED_ALIAS_NOT_CONSUMABLE", "severity": "BLOCKING", **failure})

    ambiguous_fields = {
        field: [list(values) for values in sorted(vocabularies)]
        for field, vocabularies in field_vocabularies.items()
        if len(vocabularies) > 1
    }
    blocking = [item for item in findings if item.get("severity") == "BLOCKING"]
    return {
        "result": "PASS" if not blocking else "FAIL",
        "registry_version": CONTRACT_REGISTRY_VERSION,
        "schema_file_count": len(schema_files(root)),
        "enum_or_const_path_count": len(enum_paths),
        "unique_enum_field_count": len(field_vocabularies),
        "core_contracts": {k: sorted(v) for k, v in CORE_CONTRACTS.items()},
        "prompt_pack_gateway": prompt_pack_checks,
        "staged_gateways": staged_integration,
        "registered_alias_self_tests": {
            "executed": alias_test_count,
            "failed": len(alias_failures),
        },
        "path_scoped_multiple_vocabularies": ambiguous_fields,
        "findings": findings,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--output")
    args = parser.parse_args()
    result = audit(Path(args.root).resolve())
    text = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if result["result"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
