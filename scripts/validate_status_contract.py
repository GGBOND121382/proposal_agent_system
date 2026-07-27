from __future__ import annotations

"""Backward-compatible entry point for the v3 full contract audit."""

import argparse
import json
from pathlib import Path

from validate_contract_registry import ROOT, audit


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
