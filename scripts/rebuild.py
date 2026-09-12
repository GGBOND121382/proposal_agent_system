from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request


def _post(url: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload or {}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"rebuild request failed: HTTP {exc.code}: {detail}") from exc


def _print_result(result: dict) -> None:
    plan = result.get("plan") or {}
    nodes = plan.get("nodes") or []
    print(f"operation: {result.get('id')}")
    print(f"status:    {result.get('status')}")
    print(f"branch:    {result.get('branch_id')}")
    for node in nodes:
        child = node.get("new_workflow_id") or "(not started)"
        print(
            f"{node.get('workflow_type')}: "
            f"{node.get('source_workflow_id')} -> {child} "
            f"[{node.get('new_status') or 'PLANNED'}]"
        )
    if result.get("status") == "PAUSED":
        active = result.get("active_node") or {}
        print(
            "resume:    python scripts/rebuild.py resume "
            + str(result.get("id"))
        )
        if active.get("new_workflow_id"):
            print(
                f"paused_at: {active.get('new_workflow_id')} "
                f"({active.get('new_status')})"
            )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="One-click workflow branch rebuild with frozen prerequisite remapping."
    )
    parser.add_argument("target", help="workflow id, or the literal 'resume'")
    parser.add_argument("operation_id", nargs="?", help="rebuild operation id when target=resume")
    parser.add_argument("--service-url", default="http://127.0.0.1:8080")
    parser.add_argument("--self-only", action="store_true", help="rebuild only this workflow")
    args = parser.parse_args()

    base = args.service_url.rstrip("/")
    if args.target == "resume":
        if not args.operation_id:
            parser.error("resume requires an operation id")
        result = _post(f"{base}/api/workflow-rebuilds/{args.operation_id}/resume")
    else:
        if args.operation_id:
            parser.error("unexpected second positional argument")
        scope = "SELF" if args.self_only else "ALL_DOWNSTREAM"
        result = _post(
            f"{base}/api/workflows/{args.target}/rebuild",
            {"scope": scope, "auto_advance": True},
        )
    _print_result(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
