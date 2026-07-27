from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.status_ontology import CANONICAL_KNOWLEDGE_STATUSES


def walk(node: Any, path: str = "$") -> Iterable[tuple[str, dict[str, Any]]]:
    if isinstance(node, list):
        for index, item in enumerate(node):
            yield from walk(item, f"{path}/{index}")
    elif isinstance(node, dict):
        for key, value in node.items():
            current = f"{path}/{key}"
            if key == "knowledge_status" and isinstance(value, dict):
                yield current, value
            yield from walk(value, current)


def validate(root: Path) -> list[dict[str, Any]]:
    files = list((root / "prompt_pack" / "schemas").rglob("*.json"))
    files += list(root.glob("stage*_tools/*.schema.json"))
    allowed = set(CANONICAL_KNOWLEDGE_STATUSES)
    findings: list[dict[str, Any]] = []
    for path in sorted(files):
        document = json.loads(path.read_text(encoding="utf-8"))
        for field_path, definition in walk(document):
            values = set(definition.get("enum", []))
            if "const" in definition:
                values.add(str(definition["const"]))
            illegal = sorted(values - allowed)
            if illegal:
                findings.append({
                    "file": str(path.relative_to(root)),
                    "path": field_path,
                    "illegal_values": illegal,
                })
    return findings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(ROOT))
    args = parser.parse_args()
    root = Path(args.root).resolve()
    findings = validate(root)
    print(json.dumps({
        "result": "PASS" if not findings else "FAIL",
        "canonical_knowledge_statuses": list(CANONICAL_KNOWLEDGE_STATUSES),
        "findings": findings,
    }, ensure_ascii=False, indent=2))
    return 0 if not findings else 2


if __name__ == "__main__":
    raise SystemExit(main())
