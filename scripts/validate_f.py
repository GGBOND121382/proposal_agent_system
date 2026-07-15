from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "governance" / "f" / "test_evidence_manifest.json"


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def validate(root: Path = ROOT) -> dict[str, Any]:
    spec = load_json(root / "governance" / "f" / "test_evidence_manifest.json")
    registry = load_json(root / spec["prompt_pack"]["registry"])
    replay = load_json(root / spec["prompt_pack"]["replay_manifest"])
    contract = load_json(root / spec["trace"]["contract"])
    errors: list[str] = []

    prompts = registry.get("prompts") or []
    prompt_ids = [str(item.get("prompt_id")) for item in prompts]
    expected_prompts = int(spec["prompt_pack"]["expected_prompt_count"])
    if len(prompts) != expected_prompts:
        errors.append(f"PROMPT_COUNT expected={expected_prompts} actual={len(prompts)}")
    if len(prompt_ids) != len(set(prompt_ids)):
        errors.append("PROMPT_ID_DUPLICATE")

    for item in prompts:
        prompt_id = str(item.get("prompt_id"))
        for key in ("prompt_file", "input_schema", "output_schema"):
            relative = item.get(key)
            if not relative or not (root / "prompt_pack" / str(relative)).is_file():
                errors.append(f"REGISTERED_FILE_MISSING {prompt_id} {key}={relative}")

    cases = replay.get("cases") or []
    expected_replays = int(spec["prompt_pack"]["expected_replay_count"])
    if len(cases) != expected_replays:
        errors.append(f"REPLAY_COUNT expected={expected_replays} actual={len(cases)}")
    required_types = set(spec["prompt_pack"]["required_replay_case_types"])
    matrix: dict[str, set[str]] = {prompt_id: set() for prompt_id in prompt_ids}
    for case in cases:
        prompt_id = str(case.get("prompt_id"))
        case_type = str(case.get("case_type"))
        if prompt_id not in matrix:
            errors.append(f"REPLAY_UNKNOWN_PROMPT {prompt_id}")
            continue
        matrix[prompt_id].add(case_type)
        fixture = root / "prompt_pack" / str(case.get("fixture_path"))
        if not fixture.is_file():
            errors.append(f"REPLAY_FIXTURE_MISSING {case.get('fixture_path')}")
    for prompt_id, actual_types in matrix.items():
        missing = sorted(required_types - actual_types)
        extra = sorted(actual_types - required_types)
        if missing:
            errors.append(f"REPLAY_CASE_MISSING {prompt_id}: {','.join(missing)}")
        if extra:
            errors.append(f"REPLAY_CASE_EXTRA {prompt_id}: {','.join(extra)}")

    frozen = contract["prompt_registry"]
    fields = list(frozen["required_fields"])
    normalized = [{field: item.get(field) for field in fields} for item in prompts]
    registry_digest = digest(normalized)
    if registry.get("version") != frozen.get("version"):
        errors.append("G0_REGISTRY_VERSION_DRIFT")
    if len(prompts) != int(frozen.get("entry_count", -1)):
        errors.append("G0_REGISTRY_COUNT_DRIFT")
    if registry_digest != frozen.get("entries_sha256"):
        errors.append("G0_REGISTRY_IDENTITY_DRIFT")

    workflow_path = root / spec["ci"]["workflow"]
    jobs: set[str] = set()
    if not workflow_path.is_file():
        errors.append(f"CI_WORKFLOW_MISSING {workflow_path.relative_to(root)}")
    else:
        workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8")) or {}
        jobs = set((workflow.get("jobs") or {}).keys())
        expected_jobs = set(spec["ci"]["parallel_jobs"] + spec["ci"]["integration_jobs"])
        missing_jobs = sorted(expected_jobs - jobs)
        if missing_jobs:
            errors.append("CI_JOB_MISSING " + ",".join(missing_jobs))
        for name in spec["ci"]["integration_jobs"]:
            needs = set(((workflow.get("jobs") or {}).get(name) or {}).get("needs") or [])
            missing_needs = set(spec["ci"]["parallel_jobs"]) - needs
            if missing_needs:
                errors.append(f"CI_INTEGRATION_NEEDS {name}: {','.join(sorted(missing_needs))}")

    matrix_counts = {key: len(value) for key, value in spec["agent_matrix"].items()}
    for key, minimum in (("positive", 3), ("negative", 5), ("edge", 1), ("restart", 1)):
        if matrix_counts.get(key, 0) < minimum:
            errors.append(f"AGENT_MATRIX_{key.upper()} expected>={minimum} actual={matrix_counts.get(key, 0)}")

    return {
        "gate": "F",
        "status": "PASS" if not errors else "FAIL",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "counts": {
            "prompts": len(prompts),
            "replay_cases": len(cases),
            "workflows": len(contract["workflow_state_machine"]["workflows"]),
            "gates": len(contract["workflow_state_machine"]["gate_roles"]),
            "ci_jobs": len(jobs),
            "agent_matrix": matrix_counts,
        },
        "registry_entries_sha256": registry_digest,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate F-track test/evidence contracts.")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = validate()
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
