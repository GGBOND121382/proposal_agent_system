from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
G0_DIR = ROOT / "governance" / "g0"


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def run_git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def git_commit(root: Path) -> str | None:
    result = run_git(root, "rev-parse", "HEAD", check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def parse_fastapi_version(path: Path) -> str | None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == "app" for target in node.targets):
            continue
        if not isinstance(node.value, ast.Call):
            continue
        func = node.value.func
        if not (isinstance(func, ast.Name) and func.id == "FastAPI"):
            continue
        for keyword in node.value.keywords:
            if keyword.arg == "version" and isinstance(keyword.value, ast.Constant):
                return str(keyword.value.value)
    return None


def normalize_prompt_entries(entries: list[dict[str, Any]], fields: list[str]) -> list[dict[str, Any]]:
    return [{field: entry.get(field) for field in fields} for entry in entries]


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def file_git_blob_sha(path: Path) -> str:
    data = path.read_bytes()
    header = f"blob {len(data)}\0".encode("ascii")
    return hashlib.sha1(header + data).hexdigest()


def sqlite_table_columns(schema_sql: str) -> dict[str, list[str]]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(schema_sql)
        table_rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        result: dict[str, list[str]] = {}
        for (table_name,) in table_rows:
            rows = connection.execute(f'PRAGMA table_info("{table_name}")').fetchall()
            result[table_name] = [row[1] for row in rows]
        return result
    finally:
        connection.close()


def current_blob_sha(root: Path, relative_path: str) -> str | None:
    path = root / relative_path
    if not path.is_file():
        return None
    result = run_git(root, "hash-object", "--", relative_path, check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def validate_frozen_paths(
    root: Path,
    *,
    baseline_commit: str,
    pathspecs: list[str],
    approved_changes: list[dict[str, Any]],
    label: str,
) -> list[str]:
    errors: list[str] = []
    if run_git(root, "cat-file", "-e", f"{baseline_commit}^{{commit}}", check=False).returncode != 0:
        return [f"{label}: baseline commit is unavailable: {baseline_commit}"]
    if run_git(root, "merge-base", "--is-ancestor", baseline_commit, "HEAD", check=False).returncode != 0:
        return [f"{label}: baseline commit is not an ancestor of HEAD: {baseline_commit}"]

    result = run_git(root, "diff", "--name-only", baseline_commit, "HEAD", "--", *pathspecs)
    changed = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    approved_by_path = {str(item.get("path")): item for item in approved_changes}

    undeclared = sorted(changed - set(approved_by_path))
    if undeclared:
        errors.append(f"{label}: undeclared frozen-path changes: {', '.join(undeclared)}")

    stale_approvals = sorted(set(approved_by_path) - changed)
    if stale_approvals:
        errors.append(f"{label}: approvals exist for unchanged paths: {', '.join(stale_approvals)}")

    for path in sorted(changed & set(approved_by_path)):
        approval = approved_by_path[path]
        expected_sha = str(approval.get("blob_sha") or "")
        actual_sha = current_blob_sha(root, path)
        if not expected_sha:
            errors.append(f"{label}: approved change lacks blob_sha: {path}")
        elif actual_sha != expected_sha:
            errors.append(
                f"{label}: approved blob SHA mismatch for {path}: expected {expected_sha}, got {actual_sha}"
            )
        for required in ("owner", "reason", "approval_reference"):
            if not str(approval.get(required) or "").strip():
                errors.append(f"{label}: approved change lacks {required}: {path}")
    return errors


def validate_versions(root: Path, baseline: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    expected = str(baseline["product_version"])

    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    pyproject_version = str(project["project"]["version"])
    if pyproject_version != expected:
        errors.append(f"VERSION: pyproject.toml={pyproject_version}, expected {expected}")

    api_version = parse_fastapi_version(root / "app" / "main.py")
    if api_version != expected:
        errors.append(f"VERSION: app/main.py FastAPI version={api_version}, expected {expected}")

    compose = yaml.safe_load((root / "docker-compose.yml").read_text(encoding="utf-8"))
    image = str(compose["services"]["proposal-agent"]["image"])
    expected_image = f"proposal-agent:{expected}-offline"
    if image != expected_image:
        errors.append(f"VERSION: docker image={image}, expected {expected_image}")

    return errors


def validate_prompt_contract(root: Path, contract: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    prompt_contract = contract["prompt_registry"]
    registry_path = root / "prompt_pack" / "config" / "prompt_registry.json"
    registry = load_json(registry_path)

    if str(registry.get("version")) != str(prompt_contract["version"]):
        errors.append(
            f"PROMPT_REGISTRY: version={registry.get('version')}, expected {prompt_contract['version']}"
        )

    entries = registry.get("prompts") or []
    if len(entries) != int(prompt_contract["entry_count"]):
        errors.append(
            f"PROMPT_REGISTRY: entry_count={len(entries)}, expected {prompt_contract['entry_count']}"
        )

    fields = list(prompt_contract["required_fields"])
    actual = normalize_prompt_entries(entries, fields)
    actual_digest = canonical_sha256(actual)
    expected_digest = str(prompt_contract["entries_sha256"])
    if actual_digest != expected_digest:
        errors.append(
            f"PROMPT_REGISTRY: frozen prompt identity/role/schema digest changed: "
            f"expected {expected_digest}, got {actual_digest}"
        )
    actual_blob = file_git_blob_sha(registry_path)
    expected_blob = str(prompt_contract["registry_git_blob_sha"])
    if actual_blob != expected_blob:
        errors.append(
            f"PROMPT_REGISTRY: registry Git blob SHA changed: expected {expected_blob}, got {actual_blob}"
        )

    ids = [str(entry.get("prompt_id")) for entry in entries]
    if len(ids) != len(set(ids)):
        errors.append("PROMPT_REGISTRY: duplicate prompt_id values")

    pack_root = root / "prompt_pack"
    for entry in entries:
        for key in ("prompt_file", "input_schema", "output_schema"):
            relative = entry.get(key)
            if not relative or not (pack_root / str(relative)).is_file():
                errors.append(f"PROMPT_REGISTRY: missing {entry.get('prompt_id')} {key}={relative}")

    input_envelope = load_json(pack_root / "schemas" / "common" / "prompt_input_envelope.schema.json")
    output_envelope = load_json(pack_root / "schemas" / "common" / "prompt_output_envelope.schema.json")
    for name, schema in (("input", input_envelope), ("output", output_envelope)):
        refs = schema.get("oneOf") or []
        if len(refs) != int(prompt_contract["entry_count"]):
            errors.append(
                f"PROMPT_REGISTRY: common {name} envelope refs={len(refs)}, expected {prompt_contract['entry_count']}"
            )
    return errors


def validate_workflow_contract(root: Path, contract: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    from app.workflow_defs import CRITIC_PRODUCER, GATE_ACTIONS, GATE_ROLE, WORKFLOWS

    state = contract["workflow_state_machine"]
    if WORKFLOWS != state["workflows"]:
        errors.append("WORKFLOW: WORKFLOWS differs from the frozen state-machine contract")
    if GATE_ROLE != state["gate_roles"]:
        errors.append("WORKFLOW: GATE_ROLE differs from the frozen responsibility contract")
    if GATE_ACTIONS != state["gate_action_overrides"]:
        errors.append("WORKFLOW: GATE_ACTIONS differs from the frozen gate contract")
    if CRITIC_PRODUCER != state["critic_producer"]:
        errors.append("WORKFLOW: CRITIC_PRODUCER differs from the frozen repair contract")

    source = "\n".join(
        (root / path).read_text(encoding="utf-8")
        for path in ("app/workflows.py", "app/workflow_gates.py", "app/executor.py")
    )
    for status in state["workflow_statuses"] + state["gate_statuses"]:
        if f'"{status}"' not in source and f"'{status}'" not in source:
            errors.append(f"WORKFLOW: required status literal is missing: {status}")
    return errors


def validate_artifact_contract(root: Path, contract: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    from app.db import SCHEMA

    artifact = contract["artifact_interface"]
    actual_tables = sqlite_table_columns(SCHEMA)
    expected_tables = artifact["sqlite_tables"]
    if actual_tables != expected_tables:
        errors.append("ARTIFACT: SQLite table/column contract changed")

    executor_text = (root / "app" / "executor.py").read_text(encoding="utf-8")
    for artifact_type in artifact["required_artifact_types"]:
        if f'"{artifact_type}"' not in executor_text and f"'{artifact_type}'" not in executor_text:
            errors.append(f"ARTIFACT: required artifact type is missing: {artifact_type}")

    trace_fields = artifact["trace_payload_required_fields"]
    for field in trace_fields:
        if f'"{field}"' not in executor_text:
            errors.append(f"ARTIFACT: trace payload field is missing: {field}")
    return errors


def validate_layout(root: Path, layout: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    repository = layout["repository"]
    for relative in repository["required_directories"]:
        if not (root / relative).is_dir():
            errors.append(f"LAYOUT: required directory is missing: {relative}")
    for relative in repository["material_roots"]:
        if not (root / relative).exists():
            errors.append(f"LAYOUT: material root is missing: {relative}")

    from app.config import Settings

    with tempfile.TemporaryDirectory(prefix="g0-layout-") as tmp:
        previous = os.environ.get("APP_DATA_DIR")
        os.environ["APP_DATA_DIR"] = tmp
        try:
            settings = Settings.load()
        finally:
            if previous is None:
                os.environ.pop("APP_DATA_DIR", None)
            else:
                os.environ["APP_DATA_DIR"] = previous
        expected_db = Path(tmp).resolve() / "proposal_agents.sqlite3"
        if settings.db_path != expected_db:
            errors.append(f"LAYOUT: SQLite path={settings.db_path}, expected {expected_db}")
        if settings.uploads_dir != Path(tmp).resolve() / "uploads":
            errors.append("LAYOUT: uploads directory no longer follows APP_DATA_DIR")
        if settings.exports_dir != Path(tmp).resolve() / "exports":
            errors.append("LAYOUT: exports directory no longer follows APP_DATA_DIR")
    return errors


def validate_security_contract(root: Path, contract: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    from app.api_models import ProjectCreate
    from app.workflow_defs import GATE_ACTIONS, GATE_ROLE, WORKFLOWS

    annotation = ProjectCreate.model_fields["security_level"].annotation
    try:
        from typing import get_args

        levels = list(get_args(annotation))
    except Exception:
        levels = []
    if levels != contract["security_levels"]:
        errors.append(f"SECURITY: levels={levels}, expected {contract['security_levels']}")

    invariants = contract["security_workflow_invariants"]
    wf1_prompts = [step.get("prompt_id") for step in WORKFLOWS["WF-1_PROJECT_INTAKE"][:2]]
    if wf1_prompts != invariants["wf1_prefix"]:
        errors.append("SECURITY: WF-1 security prefix changed")
    wf3_prompts = [step.get("prompt_id") for step in WORKFLOWS["WF-3_HYBRID_ONLINE_ASSIST"][:2]]
    if wf3_prompts != invariants["wf3_outbound_prefix"]:
        errors.append("SECURITY: WF-3 outbound security prefix changed")
    wf5_prompts = [step.get("prompt_id") for step in WORKFLOWS["WF-5_SECURITY_REVIEW_AND_EXPORT"][:1]]
    if wf5_prompts != invariants["wf5_prefix"]:
        errors.append("SECURITY: WF-5 confidentiality prefix changed")

    for gate, role in invariants["security_gate_roles"].items():
        if GATE_ROLE.get(gate) != role:
            errors.append(f"SECURITY: gate role changed for {gate}")
        if GATE_ACTIONS.get(gate) != invariants["security_gate_actions"]:
            errors.append(f"SECURITY: gate actions changed for {gate}")

    registry = load_json(root / "prompt_pack" / "config" / "prompt_registry.json")
    by_id = {entry["prompt_id"]: entry for entry in registry["prompts"]}
    for prompt_id in (
        "P-SECURITY-CLASSIFY",
        "P-SECURITY-CLASSIFY-CRITIC",
        "P-SAFE-ONLINE-PACKAGE",
        "P-SAFE-ONLINE-PACKAGE-CRITIC",
        "P-ONLINE-RESULT-IMPORT-CRITIC",
        "P-FINAL-CONFIDENTIALITY-REVIEW",
    ):
        if by_id[prompt_id]["required_environment"] != invariants["offline_environment"]:
            errors.append(f"SECURITY: {prompt_id} is no longer OFFLINE_LOCAL")
    for prompt_id in (
        "P-PUBLIC-RESEARCH-PLAN",
        "P-PUBLIC-RESEARCH-SYNTHESIS",
        "P-PUBLIC-RESEARCH-CRITIC",
    ):
        if by_id[prompt_id]["required_environment"] != invariants["public_environment"]:
            errors.append(f"SECURITY: {prompt_id} is no longer ONLINE_PUBLIC")
    if by_id["P-TARGETED-REPAIR"]["required_environment"] != invariants["repair_environment"]:
        errors.append("SECURITY: targeted repair environment changed")
    return errors


def validate_repository(root: Path = ROOT, *, skip_git_history: bool = False) -> dict[str, Any]:
    baseline = load_json(root / "governance" / "g0" / "baseline.json")
    interface = load_json(root / "governance" / "g0" / "interface_contract.json")
    security = load_json(root / "governance" / "g0" / "security_freeze.json")
    layout = load_json(root / "governance" / "g0" / "layout.json")

    checks: dict[str, list[str]] = {
        "versions": validate_versions(root, baseline),
        "prompt_registry": validate_prompt_contract(root, interface),
        "workflow_state_machine": validate_workflow_contract(root, interface),
        "artifact_interface": validate_artifact_contract(root, interface),
        "layout": validate_layout(root, layout),
        "security_invariants": validate_security_contract(root, security),
    }

    if not skip_git_history:
        checks["interface_freeze"] = validate_frozen_paths(
            root,
            baseline_commit=str(interface["baseline_commit"]),
            pathspecs=list(interface["frozen_pathspecs"]),
            approved_changes=list(interface["approved_changes"]),
            label="INTERFACE_FREEZE",
        )
        checks["security_freeze"] = validate_frozen_paths(
            root,
            baseline_commit=str(security["baseline_commit"]),
            pathspecs=list(security["frozen_pathspecs"]),
            approved_changes=list(security["approved_changes"]),
            label="SECURITY_FREEZE",
        )

    errors = [error for group in checks.values() for error in group]
    return {
        "gate": "G0",
        "status": "PASS" if not errors else "FAIL",
        "product_version": baseline["product_version"],
        "baseline_commit": baseline["code_baseline_commit"],
        "current_commit": git_commit(root),
        "skip_git_history": skip_git_history,
        "checks": {name: "PASS" if not group else "FAIL" for name, group in checks.items()},
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate the G0 baseline and frozen interfaces.")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--skip-git-history", action="store_true")
    args = parser.parse_args()

    root = args.root.resolve()
    report = validate_repository(root, skip_git_history=args.skip_git_history)
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.report:
        report_path = args.report
        if not report_path.is_absolute():
            report_path = root / report_path
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
