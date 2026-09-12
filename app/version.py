from __future__ import annotations

import tomllib
from pathlib import Path


def project_version(root: Path | None = None) -> str:
    root = root or Path(__file__).resolve().parents[1]
    try:
        data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        return str(data["project"]["version"])
    except (OSError, KeyError, TypeError, ValueError):
        return "0+unknown"


__version__ = project_version()
