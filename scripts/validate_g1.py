from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "governance" / "g1" / "components.json"
TRACK_IDS = ["A", "B", "C", "D", "E", "F"]
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def canonical_sha256(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def track_map(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(item["id"]): item for item in manifest.get("tracks", [])}


def validate_manifest(manifest: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if manifest.get("schema_version") != "1.0":
        errors.append("G1_SCHEMA_VERSION")
    if manifest.get("gate") != "G1":
        errors.append("G1_GATE_ID")
    baseline = str(manifest.get("controller_baseline") or "")
    if not SHA_RE.fullmatch(baseline):
        errors.append("G1_CONTROLLER_BASELINE_SHA")

    tracks = manifest.get("tracks")
    if not isinstance(tracks, list):
        return [*errors, "G1_TRACKS_NOT_LIST"]
    ids = [str(item.get("id")) for item in tracks if isinstance(item, dict)]
    if ids != TRACK_IDS:
        errors.append(f"G1_TRACK_ORDER expected={TRACK_IDS} actual={ids}")
    if len(ids) != len(set(ids)):
        errors.append("G1_TRACK_DUPLICATE")

    for item in tracks:
        if not isinstance(item, dict):
            errors.append("G1_TRACK_NOT_OBJECT")
            continue
        tid = str(item.get("id"))
        if not SHA_RE.fullmatch(str(item.get("sha") or "")):
            errors.append(f"G1_{tid}_SHA")
        if not str(item.get("branch") or "").startswith("agent/"):
            errors.append(f"G1_{tid}_BRANCH")
        if not isinstance(item.get("pr_number"), int) or int(item["pr_number"]) <= 0:
            errors.append(f"G1_{tid}_PR")
        for field in ("required_files", "test_files", "acceptance_commands", "system_packages"):
            if not isinstance(item.get(field), list):
                errors.append(f"G1_{tid}_{field.upper()}_NOT_LIST")
        if not item.get("required_files"):
            errors.append(f"G1_{tid}_REQUIRED_FILES_EMPTY")
        if not item.get("test_files"):
            errors.append(f"G1_{tid}_TEST_FILES_EMPTY")
        categories = item.get("categories")
        if not isinstance(categories, dict):
            errors.append(f"G1_{tid}_CATEGORIES")
            continue
        if set(categories) != {"positive", "negative", "boundary", "restart"}:
            errors.append(f"G1_{tid}_CATEGORY_KEYS")
        for category in ("positive", "negative", "boundary", "restart"):
            evidence = categories.get(category)
            if not isinstance(evidence, list) or not evidence:
                errors.append(f"G1_{tid}_{category.upper()}_EMPTY")
                continue
            for index, entry in enumerate(evidence):
                if not isinstance(entry, dict):
                    errors.append(f"G1_{tid}_{category.upper()}_{index}_NOT_OBJECT")
                    continue
                if entry.get("type") == "test":
                    if not entry.get("path") or not entry.get("symbol"):
                        errors.append(f"G1_{tid}_{category.upper()}_{index}_TEST_FIELDS")
                elif entry.get("type") == "probe":
                    if not entry.get("name"):
                        errors.append(f"G1_{tid}_{category.upper()}_{index}_PROBE_NAME")
                else:
                    errors.append(f"G1_{tid}_{category.upper()}_{index}_TYPE")
    return errors


def matrix_payload(manifest: dict[str, Any]) -> dict[str, Any]:
    include = []
    for item in manifest["tracks"]:
        include.append(
            {
                "track": item["id"],
                "name": item["name"],
                "branch": item["branch"],
                "sha": item["sha"],
                "test_targets": " ".join(item["test_files"]),
                "system_packages": " ".join(item["system_packages"]),
            }
        )
    return {"include": include}


def append_github_output(path: Path, name: str, value: str) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{name}={value}\n")


def parse_junit(path: Path) -> dict[str, int | list[str]]:
    if not path.is_file():
        raise FileNotFoundError(path)
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.findall(".//testsuite"))
    tests = sum(int(suite.attrib.get("tests", "0")) for suite in suites)
    failures = sum(int(suite.attrib.get("failures", "0")) for suite in suites)
    errors = sum(int(suite.attrib.get("errors", "0")) for suite in suites)
    skipped = sum(int(suite.attrib.get("skipped", "0")) for suite in suites)
    names = sorted(
        {
            str(case.attrib.get("name") or "")
            for case in root.findall(".//testcase")
            if case.attrib.get("name")
        }
    )
    return {
        "tests": tests,
        "failures": failures,
        "errors": errors,
        "skipped": skipped,
        "testcase_names": names,
    }


def git_head(root: Path) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return completed.stdout.strip()


def evidence_entries(track: dict[str, Any]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for category, entries in track["categories"].items():
        for entry in entries:
            result.append({"category": category, **entry})
    return result


def verify_category_sources(
    track: dict[str, Any], component_root: Path, junit_names: set[str]
) -> tuple[list[dict[str, Any]], list[str]]:
    evidence: list[dict[str, Any]] = []
    errors: list[str] = []
    for entry in evidence_entries(track):
        category = entry["category"]
        if entry["type"] == "test":
            path = component_root / entry["path"]
            symbol = entry["symbol"]
            present = path.is_file() and symbol in path.read_text(encoding="utf-8")
            executed = any(name == symbol or name.startswith(symbol + "[") for name in junit_names)
            evidence.append(
                {
                    "category": category,
                    "type": "test",
                    "path": entry["path"],
                    "symbol": symbol,
                    "present": present,
                    "executed": executed,
                }
            )
            if not present:
                errors.append(f"G1_{track['id']}_{category.upper()}_TEST_MISSING:{symbol}")
            if not executed:
                errors.append(f"G1_{track['id']}_{category.upper()}_TEST_NOT_EXECUTED:{symbol}")
        else:
            evidence.append(
                {
                    "category": category,
                    "type": "probe",
                    "name": entry["name"],
                    "present": True,
                    "executed": True,
                }
            )
    return evidence, errors


def require_json_status(path: Path, label: str) -> tuple[dict[str, Any] | None, list[str]]:
    if not path.is_file():
        return None, [f"{label}_MISSING:{path}"]
    try:
        payload = load_json(path)
    except Exception as exc:
        return None, [f"{label}_INVALID_JSON:{exc}"]
    if payload.get("status") != "PASS":
        return payload, [f"{label}_STATUS:{payload.get('status')}"]
    return payload, []


def verify_special_audit(track_id: str, evidence_dir: Path) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    detail: dict[str, Any] = {}
    if track_id == "A":
        payload, found = require_json_status(
            evidence_dir / "runtime-evidence-audit.json", "G1_A_RUNTIME_AUDIT"
        )
        errors.extend(found)
        if payload:
            hashes = payload.get("hashes") or {}
            required = {
                "request_sha256",
                "raw_response_sha256",
                "parsed_object_sha256",
                "raw_parsed_object_sha256",
                "consumed_object_sha256",
            }
            if not required.issubset(hashes):
                errors.append("G1_A_HASH_FIELDS")
            if hashes.get("parsed_object_sha256") != hashes.get("raw_parsed_object_sha256"):
                errors.append("G1_A_RAW_PARSED_HASH_MISMATCH")
            if hashes.get("parsed_object_sha256") != hashes.get("consumed_object_sha256"):
                errors.append("G1_A_CONSUMED_HASH_MISMATCH")
            if not payload.get("raw_response_text"):
                errors.append("G1_A_RAW_RESPONSE_MISSING")
            detail = payload
    elif track_id == "B":
        payload, found = require_json_status(evidence_dir / "acceptance.json", "G1_B_ACCEPTANCE")
        errors.extend(found)
        restart, restart_errors = require_json_status(
            evidence_dir / "restart-probe.json", "G1_B_RESTART"
        )
        errors.extend(restart_errors)
        detail = {"acceptance": payload, "restart": restart}
    elif track_id == "C":
        detail = {"restart_test": "test_c6_restart_verification_detects_tampering"}
    elif track_id == "D":
        payload, found = require_json_status(
            evidence_dir / "delivery" / "D_TRACK_ACCEPTANCE.json", "G1_D_ACCEPTANCE"
        )
        errors.extend(found)
        if payload:
            records = payload.get("render_records") or []
            if len(records) < 3:
                errors.append("G1_D_RENDER_RECORD_COUNT")
            for record in records:
                if not record.get("cache_hit"):
                    errors.append(f"G1_D_CACHE_MISS:{record.get('section_id')}")
                for key in ("source_sha256", "svg_sha256", "png_sha256"):
                    if not SHA_RE.fullmatch(str(record.get(key) or "")):
                        errors.append(f"G1_D_HASH:{record.get('section_id')}:{key}")
            detail = payload
    elif track_id == "E":
        payload, found = require_json_status(
            evidence_dir / "track-e" / "acceptance.json", "G1_E_ACCEPTANCE"
        )
        errors.extend(found)
        restart, restart_errors = require_json_status(
            evidence_dir / "restart-probe.json", "G1_E_RESTART"
        )
        errors.extend(restart_errors)
        detail = {"acceptance": payload, "restart": restart}
    elif track_id == "F":
        payload, found = require_json_status(evidence_dir / "validate-f.json", "G1_F_ACCEPTANCE")
        errors.extend(found)
        if payload:
            matrix = ((payload.get("counts") or {}).get("agent_matrix") or {})
            for key, minimum in (("positive", 3), ("negative", 5), ("edge", 1), ("restart", 1)):
                if int(matrix.get(key, 0)) < minimum:
                    errors.append(f"G1_F_MATRIX_{key.upper()}")
            detail = payload
    return detail, errors


def render_component_markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# G1 Track {report['track']} Independent Acceptance",
        "",
        f"- Status: **{report['status']}**",
        f"- Commit: `{report['component_sha']}`",
        f"- Targeted tests: {report['targeted_junit']['tests']}",
        f"- Full regression tests: {report['full_junit']['tests']}",
        "",
        "| Category | Evidence | Present | Executed |",
        "|---|---|---:|---:|",
    ]
    for item in report["category_evidence"]:
        label = item.get("symbol") or item.get("name")
        lines.append(
            f"| {item['category']} | `{label}` | {item['present']} | {item['executed']} |"
        )
    if report["errors"]:
        lines.extend(["", "## Errors", ""])
        lines.extend(f"- `{error}`" for error in report["errors"])
    lines.append("")
    return "\n".join(lines)


def validate_component(args: argparse.Namespace) -> int:
    manifest = load_json(args.manifest)
    errors = validate_manifest(manifest)
    tracks = track_map(manifest)
    if args.track not in tracks:
        errors.append(f"G1_UNKNOWN_TRACK:{args.track}")
        track = {"id": args.track, "sha": "", "required_files": [], "categories": {}}
    else:
        track = tracks[args.track]

    component_root = args.component_root.resolve()
    evidence_dir = args.evidence_dir.resolve()
    evidence_dir.mkdir(parents=True, exist_ok=True)

    try:
        actual_head = git_head(component_root)
    except Exception as exc:
        actual_head = ""
        errors.append(f"G1_{args.track}_GIT_HEAD:{exc}")
    if actual_head != track.get("sha"):
        errors.append(
            f"G1_{args.track}_SHA_MISMATCH expected={track.get('sha')} actual={actual_head}"
        )

    missing_files = [
        relative
        for relative in track.get("required_files", [])
        if not (component_root / relative).is_file()
    ]
    errors.extend(f"G1_{args.track}_FILE_MISSING:{path}" for path in missing_files)

    try:
        targeted = parse_junit(args.targeted_junit)
    except Exception as exc:
        targeted = {
            "tests": 0,
            "failures": 1,
            "errors": 1,
            "skipped": 0,
            "testcase_names": [],
        }
        errors.append(f"G1_{args.track}_TARGETED_JUNIT:{exc}")
    try:
        full = parse_junit(args.full_junit)
    except Exception as exc:
        full = {
            "tests": 0,
            "failures": 1,
            "errors": 1,
            "skipped": 0,
            "testcase_names": [],
        }
        errors.append(f"G1_{args.track}_FULL_JUNIT:{exc}")

    for label, result in (("TARGETED", targeted), ("FULL", full)):
        if int(result["tests"]) <= 0:
            errors.append(f"G1_{args.track}_{label}_NO_TESTS")
        if int(result["failures"]) or int(result["errors"]):
            errors.append(
                f"G1_{args.track}_{label}_FAIL failures={result['failures']} errors={result['errors']}"
            )

    category_evidence, category_errors = verify_category_sources(
        track, component_root, set(targeted["testcase_names"])
    )
    errors.extend(category_errors)
    special, special_errors = verify_special_audit(args.track, evidence_dir)
    errors.extend(special_errors)

    report = {
        "schema_version": "1.0",
        "gate": "G1",
        "track": args.track,
        "status": "PASS" if not errors else "FAIL",
        "component_branch": track.get("branch"),
        "component_sha": actual_head,
        "expected_sha": track.get("sha"),
        "required_files": track.get("required_files", []),
        "missing_files": missing_files,
        "targeted_junit": targeted,
        "full_junit": full,
        "category_evidence": category_evidence,
        "special_audit": special,
        "errors": errors,
    }
    args.report_json.parent.mkdir(parents=True, exist_ok=True)
    args.report_json.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    args.report_md.write_text(render_component_markdown(report), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "PASS" else 1


def aggregate(args: argparse.Namespace) -> int:
    manifest = load_json(args.manifest)
    errors = validate_manifest(manifest)
    reports: dict[str, Any] = {}
    for tid in TRACK_IDS:
        candidates = list(args.reports_dir.glob(f"**/G1_TRACK_{tid}.json"))
        if len(candidates) != 1:
            errors.append(f"G1_{tid}_REPORT_COUNT:{len(candidates)}")
            continue
        payload = load_json(candidates[0])
        reports[tid] = payload
        if payload.get("status") != "PASS":
            errors.append(f"G1_{tid}_STATUS:{payload.get('status')}")
        expected = track_map(manifest)[tid]["sha"]
        if payload.get("component_sha") != expected:
            errors.append(f"G1_{tid}_REPORT_SHA")

    result = {
        "schema_version": "1.0",
        "gate": "G1",
        "status": "PASS" if not errors else "FAIL",
        "controller_baseline": manifest.get("controller_baseline"),
        "component_heads": {tid: track_map(manifest)[tid]["sha"] for tid in TRACK_IDS},
        "tracks": reports,
        "pass_conditions": {
            "all_tracks_independent": all(
                reports.get(tid, {}).get("status") == "PASS" for tid in TRACK_IDS
            ),
            "positive_negative_boundary_restart": all(
                {
                    item.get("category")
                    for item in reports.get(tid, {}).get("category_evidence", [])
                    if item.get("present") and item.get("executed")
                }
                == {"positive", "negative", "boundary", "restart"}
                for tid in TRACK_IDS
            ),
            "runtime_raw_response_hash_audit": reports.get("A", {})
            .get("special_audit", {})
            .get("status")
            == "PASS",
        },
        "errors": errors,
    }
    args.report_json.parent.mkdir(parents=True, exist_ok=True)
    args.report_json.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    lines = [
        "# G1 Component Independent Acceptance",
        "",
        f"- Overall: **{result['status']}**",
        f"- Controller baseline: `{result['controller_baseline']}`",
        "",
        "| Track | Head | Status | Targeted | Full regression |",
        "|---|---|---|---:|---:|",
    ]
    for tid in TRACK_IDS:
        item = reports.get(tid, {})
        lines.append(
            f"| {tid} | `{result['component_heads'][tid]}` | {item.get('status', 'MISSING')} | "
            f"{(item.get('targeted_junit') or {}).get('tests', 0)} | "
            f"{(item.get('full_junit') or {}).get('tests', 0)} |"
        )
    if errors:
        lines.extend(["", "## Errors", ""])
        lines.extend(f"- `{error}`" for error in errors)
    lines.append("")
    args.report_md.write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "PASS" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate G1 independent component acceptance.")
    sub = parser.add_subparsers(dest="command", required=True)

    manifest_parser = sub.add_parser("manifest")
    manifest_parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    manifest_parser.add_argument("--report", type=Path)
    manifest_parser.add_argument("--github-output", type=Path)

    component_parser = sub.add_parser("component")
    component_parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    component_parser.add_argument("--track", required=True, choices=TRACK_IDS)
    component_parser.add_argument("--component-root", type=Path, required=True)
    component_parser.add_argument("--evidence-dir", type=Path, required=True)
    component_parser.add_argument("--targeted-junit", type=Path, required=True)
    component_parser.add_argument("--full-junit", type=Path, required=True)
    component_parser.add_argument("--report-json", type=Path, required=True)
    component_parser.add_argument("--report-md", type=Path, required=True)

    aggregate_parser = sub.add_parser("aggregate")
    aggregate_parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    aggregate_parser.add_argument("--reports-dir", type=Path, required=True)
    aggregate_parser.add_argument("--report-json", type=Path, required=True)
    aggregate_parser.add_argument("--report-md", type=Path, required=True)

    args = parser.parse_args()
    if args.command == "manifest":
        manifest = load_json(args.manifest)
        errors = validate_manifest(manifest)
        report = {
            "schema_version": "1.0",
            "gate": "G1",
            "status": "PASS" if not errors else "FAIL",
            "track_count": len(manifest.get("tracks", [])),
            "component_heads": {
                item["id"]: item["sha"] for item in manifest.get("tracks", [])
            },
            "errors": errors,
        }
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        if args.github_output and not errors:
            append_github_output(
                args.github_output,
                "matrix",
                json.dumps(matrix_payload(manifest), ensure_ascii=False, separators=(",", ":")),
            )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["status"] == "PASS" else 1
    if args.command == "component":
        return validate_component(args)
    return aggregate(args)


if __name__ == "__main__":
    raise SystemExit(main())
