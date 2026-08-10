from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.pack import PromptPack
from app.prompt_contracts import documented_finding_codes, replay_finding_code_errors


PACK_ROOT = ROOT / "prompt_pack"


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _schema_stats(node: Any, path: str = "$") -> dict[str, Any]:
    object_nodes = 0
    required_fields = 0
    missing_additional: list[str] = []
    if isinstance(node, dict):
        node_type = node.get("type")
        is_object = node_type == "object" or (
            isinstance(node_type, list) and "object" in node_type
        )
        if is_object or isinstance(node.get("properties"), dict):
            object_nodes += 1
            required_fields += len(node.get("required") or [])
            if "additionalProperties" not in node:
                missing_additional.append(path)
        for key, value in node.items():
            child = _schema_stats(value, f"{path}/{key}")
            object_nodes += child["object_nodes"]
            required_fields += child["required_fields"]
            missing_additional.extend(child["missing_additional_properties"])
    elif isinstance(node, list):
        for index, value in enumerate(node):
            child = _schema_stats(value, f"{path}/{index}")
            object_nodes += child["object_nodes"]
            required_fields += child["required_fields"]
            missing_additional.extend(child["missing_additional_properties"])
    return {
        "object_nodes": object_nodes,
        "required_fields": required_fields,
        "missing_additional_properties": sorted(set(missing_additional)),
    }


def build_matrix(pack_root: Path = PACK_ROOT) -> dict[str, Any]:
    pack = PromptPack(pack_root)
    manifest = _json(pack_root / "replay" / "manifest.json")
    replay_by_prompt: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in manifest["cases"]:
        replay_by_prompt[item["prompt_id"]].append(item)

    prompts: list[dict[str, Any]] = []
    blocking_issues: list[dict[str, str]] = []
    informational_issues: list[dict[str, Any]] = []
    status_counts: Counter[str] = Counter()

    for prompt_id in pack.prompt_ids():
        entry = pack.entry(prompt_id)
        input_schema = pack.schema(prompt_id, "input")
        output_schema = pack.schema(prompt_id, "output")
        prompt_text = pack.prompt_text(prompt_id)
        documented_codes = sorted(documented_finding_codes(prompt_text))
        input_stats = _schema_stats(input_schema)
        output_stats = _schema_stats(output_schema)
        inlined_output_stats = _schema_stats(pack.inlined_schema(prompt_id, "output"))
        expected_schema_const = (
            input_schema.get("properties", {})
            .get("expected_output_schema", {})
            .get("const")
        )

        replay_rows: list[dict[str, Any]] = []
        replay_codes: set[str] = set()
        for manifest_item in replay_by_prompt[prompt_id]:
            fixture_path = pack_root / manifest_item["fixture_path"]
            case = _json(fixture_path)
            input_errors = pack.validate(prompt_id, "input", case["input"])
            output = case.get("expected_output")
            output_errors = (
                pack.validate(prompt_id, "output", output)
                if isinstance(output, dict)
                else []
            )
            finding_errors = (
                replay_finding_code_errors(
                    prompt_id=prompt_id,
                    output=output,
                    prompt_text=prompt_text,
                )
                if isinstance(output, dict)
                else []
            )
            if isinstance(output, dict):
                status_counts[str(output.get("status") or "<missing>")] += 1
                replay_codes.update(
                    str(item.get("code") or "")
                    for item in output.get("findings") or []
                    if isinstance(item, dict) and item.get("code")
                )
            replay_rows.append(
                {
                    "case_type": manifest_item["case_type"],
                    "fixture_path": manifest_item["fixture_path"],
                    "expected_input_valid": case["expected_validation"][
                        "input_schema_valid"
                    ],
                    "input_errors": input_errors,
                    "output_errors": output_errors,
                    "finding_code_errors": finding_errors,
                }
            )
            expected_input_valid = bool(
                case["expected_validation"]["input_schema_valid"]
            )
            if (not input_errors) != expected_input_valid:
                blocking_issues.append(
                    {
                        "prompt_id": prompt_id,
                        "code": "REPLAY_INPUT_EXPECTATION_MISMATCH",
                        "detail": manifest_item["fixture_path"],
                    }
                )
            if isinstance(output, dict) and (output_errors or finding_errors):
                blocking_issues.append(
                    {
                        "prompt_id": prompt_id,
                        "code": "REPLAY_OUTPUT_CONTRACT_ERROR",
                        "detail": f"{manifest_item['fixture_path']}: {output_errors + finding_errors}",
                    }
                )

        if expected_schema_const != entry["output_schema"]:
            blocking_issues.append(
                {
                    "prompt_id": prompt_id,
                    "code": "INPUT_OUTPUT_SCHEMA_BINDING_MISMATCH",
                    "detail": f"{expected_schema_const!r} != {entry['output_schema']!r}",
                }
            )
        if not documented_codes:
            blocking_issues.append(
                {
                    "prompt_id": prompt_id,
                    "code": "PROMPT_FINDING_VOCABULARY_EMPTY",
                    "detail": entry["prompt_file"],
                }
            )
        for kind, stats in (("input", input_stats), ("output", output_stats)):
            if stats["missing_additional_properties"]:
                informational_issues.append(
                    {
                        "prompt_id": prompt_id,
                        "code": "OBJECT_OPENNESS_IMPLICIT",
                        "schema_kind": kind,
                        "paths": stats["missing_additional_properties"],
                        "detail": (
                            "Object openness is implicit. Review whether these are "
                            "intentionally free-form; no automatic failure is assigned."
                        ),
                    }
                )

        prompts.append(
            {
                "prompt_id": prompt_id,
                "prompt_version": entry["prompt_version"],
                "schema_version": output_schema.get("properties", {})
                .get("schema_version", {})
                .get("const"),
                "executor_role": entry.get("executor_role"),
                "required_environment": entry.get("required_environment"),
                "model_profile": entry.get("model_profile"),
                "next_human_gate": entry.get("next_human_gate"),
                "prompt_file": entry["prompt_file"],
                "input_schema": entry["input_schema"],
                "output_schema": entry["output_schema"],
                "expected_output_schema_const": expected_schema_const,
                "documented_finding_codes": documented_codes,
                "replay_finding_codes": sorted(replay_codes),
                "input_schema_stats": input_stats,
                "output_schema_stats": output_stats,
                "inlined_output_schema_stats": inlined_output_stats,
                "replays": sorted(replay_rows, key=lambda row: row["case_type"]),
            }
        )

    return {
        "schema_version": "1.0",
        "status": "PASS" if not blocking_issues else "FAIL",
        "scope": "Prompt registry, prompt Markdown, input/output Schema, Replay and cross-field protocol contracts",
        "summary": {
            "prompt_count": len(prompts),
            "replay_count": len(manifest["cases"]),
            "blocking_issue_count": len(blocking_issues),
            "informational_issue_count": len(informational_issues),
            "replay_status_counts": dict(sorted(status_counts.items())),
            "normalization_boundary": {
                "structure_preflight": "container/scalar shape only",
                "final_validation": "strict Schema plus protocol semantic checks",
                "prompt_local_required_output_fields": sum(
                    item["output_schema_stats"]["required_fields"] for item in prompts
                ),
                "inlined_required_output_fields": sum(
                    item["inlined_output_schema_stats"]["required_fields"]
                    for item in prompts
                ),
                "field_synthesis_assessment": (
                    "Validated separately by test_schema_field_ownership.py and "
                    "output-container normalization tests; the matrix itself does "
                    "not infer runtime field ownership from source text."
                ),
            },
        },
        "blocking_issues": blocking_issues,
        "informational_issues": informational_issues,
        "prompts": prompts,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pack-root", type=Path, default=PACK_ROOT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_matrix(args.pack_root.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {"status": report["status"], **report["summary"]},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
