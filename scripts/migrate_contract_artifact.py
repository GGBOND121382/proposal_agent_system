#!/usr/bin/env python3
from __future__ import annotations

"""Migrate historical model/stage artifacts through the unified v3 contract.

The migrator preserves the raw object in its report, applies the stage-aware
legacy migration for Stage 2/3 artifacts, then applies the same schema-guided
normalizer used by the runtime before validating the result strictly.
"""

import argparse
import copy
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.contract_registry import CONTRACT_REGISTRY_VERSION, normalize_against_schema
from app.status_ontology import normalize_stage2_candidate, normalize_stage3_candidate

STAGE_SCHEMAS: dict[str, Path] = {
    "STAGE_1_DESIGN_INPUT": ROOT / "stage1_tools" / "design_input.schema.json",
    "STAGE_2_GUIDE_AND_FACT_BASE": ROOT / "stage2_tools" / "guide_fact_base.schema.json",
    "STAGE_3_PROJECT_DEFINITION": ROOT / "stage3_tools" / "project_definition.schema.json",
    "STAGE_4_ARGUMENT_ARCHITECTURE": ROOT / "stage4_tools" / "argument_architecture.schema.json",
    "STAGE_4A_EVIDENCE_COMPLETION": ROOT / "stage4a_tools" / "evidence_completion.schema.json",
    "STAGE_5_PROVISIONAL_SECTION_PLANNING": ROOT / "stage5_tools" / "section_plan.schema.json",
}


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256_json(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def detect_payload(document: Any) -> tuple[Any, str | None]:
    """Return the schema-bound payload and optional envelope key.

    Historical files may contain a raw stage object, a model response envelope
    with ``output``, or a request/response trace with ``normalized_output``.
    """

    if not isinstance(document, Mapping):
        return document, None
    for key in ("normalized_output", "output", "candidate", "result"):
        value = document.get(key)
        if isinstance(value, (dict, list)):
            return value, key
    return document, None


def infer_schema(payload: Any, explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    if isinstance(payload, Mapping):
        stage = str(payload.get("stage") or "")
        if stage in STAGE_SCHEMAS:
            return STAGE_SCHEMAS[stage]
    supported = ", ".join(sorted(STAGE_SCHEMAS))
    raise ValueError(
        "无法从工件推断Schema；请传入 --schema。可自动识别的stage为：" + supported
    )


def stage_migrate(payload: Any) -> tuple[Any, dict[str, Any] | None]:
    if not isinstance(payload, Mapping):
        return copy.deepcopy(payload), None
    stage = str(payload.get("stage") or "")
    if stage == "STAGE_2_GUIDE_AND_FACT_BASE":
        return normalize_stage2_candidate(payload)
    if stage == "STAGE_3_PROJECT_DEFINITION":
        return normalize_stage3_candidate(payload)
    return copy.deepcopy(payload), None


def schema_errors(instance: Any, schema: Mapping[str, Any]) -> list[dict[str, Any]]:
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(instance), key=lambda e: list(e.absolute_path))
    return [
        {
            "path": "$" + "".join(f"/{x}" for x in error.absolute_path),
            "schema_path": "$" + "".join(f"/{x}" for x in error.absolute_schema_path),
            "message": error.message,
        }
        for error in errors
    ]


def migrate(document: Any, schema: Mapping[str, Any], *, contract_id: str) -> tuple[Any, dict[str, Any]]:
    raw_payload, envelope_key = detect_payload(document)
    stage_normalized, stage_report = stage_migrate(raw_payload)
    normalized_payload, registry_report = normalize_against_schema(
        stage_normalized, schema, contract_id=contract_id
    )
    errors = schema_errors(normalized_payload, schema)

    if envelope_key is None:
        output_document = normalized_payload
    else:
        output_document = copy.deepcopy(document)
        output_document[envelope_key] = normalized_payload

    report = {
        "schema_version": "1.0",
        "migration_tool": "scripts/migrate_contract_artifact.py",
        "normalizer_version": CONTRACT_REGISTRY_VERSION,
        "contract_id": contract_id,
        "envelope_key": envelope_key,
        "raw_payload_sha256": sha256_json(raw_payload),
        "normalized_payload_sha256": sha256_json(normalized_payload),
        "stage_migration": stage_report,
        "registry_normalization": registry_report,
        "schema_validation": {
            "valid": not errors,
            "error_count": len(errors),
            "errors": errors,
        },
        "raw_payload": raw_payload,
        "normalized_payload": normalized_payload,
    }
    return output_document, report


def main() -> int:
    parser = argparse.ArgumentParser(description="迁移历史模型/阶段工件到统一契约v3")
    parser.add_argument("--input", required=True, type=Path, help="原始JSON工件或模型响应")
    parser.add_argument("--schema", type=Path, help="目标JSON Schema；阶段主工件可自动推断")
    parser.add_argument("--output", required=True, type=Path, help="规范化后的JSON输出")
    parser.add_argument("--report", required=True, type=Path, help="迁移Trace报告")
    parser.add_argument("--contract-id", help="审计用契约ID；默认使用Schema文件名")
    parser.add_argument(
        "--allow-invalid",
        action="store_true",
        help="即使仍有未登记漂移或其他Schema错误也写出并返回0",
    )
    args = parser.parse_args()

    document = read_json(args.input)
    payload, _ = detect_payload(document)
    schema_path = infer_schema(payload, args.schema)
    schema = read_json(schema_path)
    contract_id = args.contract_id or f"migration:{schema_path.relative_to(ROOT) if schema_path.is_relative_to(ROOT) else schema_path}"

    output, report = migrate(document, schema, contract_id=contract_id)
    write_json(args.output, output)
    write_json(args.report, report)

    validation = report["schema_validation"]
    print(
        json.dumps(
            {
                "output": str(args.output),
                "report": str(args.report),
                "schema": str(schema_path),
                "valid": validation["valid"],
                "normalized_count": report["registry_normalization"]["normalized_count"],
                "unresolved_count": report["registry_normalization"]["unresolved_count"],
                "schema_error_count": validation["error_count"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if not validation["valid"] and not args.allow_invalid:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
