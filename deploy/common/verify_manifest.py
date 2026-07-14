#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('root', type=Path)
    parser.add_argument('--manifest', default='SHA256SUMS.txt')
    args = parser.parse_args()
    root = args.root.resolve()
    manifest = root / args.manifest
    errors = []
    for line in manifest.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        expected, rel = line.split('  ', 1)
        path = root / rel
        if not path.exists():
            errors.append(f"MISSING {rel}")
        elif sha256(path) != expected:
            errors.append(f"MISMATCH {rel}")
    if errors:
        raise SystemExit('\n'.join(errors))
    print(f"PASS: {manifest}")


if __name__ == '__main__':
    main()
