from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "prompt_pack"
GENERATED = {Path("MANIFEST.md"), Path("SHA256SUMS.txt")}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def included_files() -> list[Path]:
    files: list[Path] = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(ROOT)
        if relative in GENERATED or "__pycache__" in relative.parts or path.suffix == ".pyc":
            continue
        files.append(relative)
    return sorted(files, key=lambda item: item.as_posix())


def main() -> None:
    files = included_files()
    rows = [(path, (ROOT / path).stat().st_size, sha256(ROOT / path)) for path in files]

    registry = json.loads((ROOT / "config" / "prompt_registry.json").read_text(encoding="utf-8"))
    replay = json.loads((ROOT / "replay" / "manifest.json").read_text(encoding="utf-8"))
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    json_files = sum(1 for path in files if path.suffix == ".json")
    yaml_files = sum(1 for path in files if path.suffix in {".yaml", ".yml"})

    checksum_text = "".join(f"{digest}  {path.as_posix()}\n" for path, _, digest in rows)
    (ROOT / "SHA256SUMS.txt").write_text(checksum_text, encoding="utf-8")

    lines = [
        "# Prompt Pack 文件清单",
        "",
        f"- 版本：`{version}`",
        "- 校验状态：`PASS`",
        f"- 文件数（不含本清单与校验和文件）：`{len(rows)}`",
        f"- Prompt：`{len(registry.get('prompts') or [])}`",
        f"- Replay：`{len(replay.get('cases') or [])}`",
        f"- JSON文件：`{json_files}`",
        f"- YAML文件：`{yaml_files}`",
        "- 生成文件 `MANIFEST.md` 与 `SHA256SUMS.txt` 不参与自身校验，避免循环哈希。",
        "",
        "## 文件",
        "",
        "| 路径 | 字节 | SHA-256 |",
        "|---|---:|---|",
    ]
    lines.extend(
        f"| `{path.as_posix()}` | {size} | `{digest}` |"
        for path, size, digest in rows
    )
    lines.append("")
    (ROOT / "MANIFEST.md").write_text("\n".join(lines), encoding="utf-8")

    print(
        json.dumps(
            {
                "status": "PASS",
                "version": version,
                "files": len(rows),
                "prompts": len(registry.get("prompts") or []),
                "replays": len(replay.get("cases") or []),
                "json_files": json_files,
                "yaml_files": yaml_files,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
