from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Any


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_member_name(name: str) -> bool:
    path = Path(name)
    return not path.is_absolute() and ".." not in path.parts


def verify_bundle(bundle: Path) -> dict[str, Any]:
    errors: list[str] = []
    with zipfile.ZipFile(bundle) as archive:
        names = set(archive.namelist())
        if "manifest.json" not in names:
            return {"status": "FAIL", "errors": ["manifest.json is missing"]}
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        for name, metadata in manifest.get("files", {}).items():
            if name not in names:
                errors.append(f"missing member: {name}")
                continue
            data = archive.read(name)
            actual = sha256_bytes(data)
            expected = str(metadata.get("sha256"))
            if actual != expected:
                errors.append(f"sha256 mismatch for {name}: expected {expected}, got {actual}")
            if len(data) != int(metadata.get("size", -1)):
                errors.append(f"size mismatch for {name}")
        for name in names:
            if not safe_member_name(name):
                errors.append(f"unsafe ZIP member: {name}")
    return {
        "status": "PASS" if not errors else "FAIL",
        "source_commit": manifest.get("source_commit"),
        "product_version": manifest.get("product_version"),
        "errors": errors,
    }


def safe_extract_tar(archive_path: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
        for member in members:
            if not safe_member_name(member.name):
                raise ValueError(f"unsafe TAR member: {member.name}")
            if member.issym() or member.islnk():
                raise ValueError(f"links are not permitted in source archive: {member.name}")
        archive.extractall(destination, members=members)


def extract_bundle(bundle: Path, destination: Path) -> None:
    report = verify_bundle(bundle)
    if report["status"] != "PASS":
        raise ValueError("\n".join(report["errors"]))

    destination = destination.resolve()
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)

    with tempfile.TemporaryDirectory(prefix="g0-verify-") as tmp:
        temp_root = Path(tmp)
        with zipfile.ZipFile(bundle) as archive:
            for member in archive.infolist():
                if not safe_member_name(member.filename):
                    raise ValueError(f"unsafe ZIP member: {member.filename}")
                archive.extract(member, temp_root)
        safe_extract_tar(temp_root / "source" / "source.tar.gz", destination / "source")
        for relative in ("manifest.json", "RESTORE.md", "dependencies", "sqlite", "trace", "manifests"):
            source = temp_root / relative
            target = destination / relative
            if source.is_dir():
                shutil.copytree(source, target)
            elif source.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify and optionally extract a G0 recovery bundle.")
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--extract-dir", type=Path)
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()

    report = verify_bundle(args.bundle)
    if args.extract_dir and report["status"] == "PASS":
        extract_bundle(args.bundle, args.extract_dir)
        report["extract_dir"] = str(args.extract_dir.resolve())

    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
