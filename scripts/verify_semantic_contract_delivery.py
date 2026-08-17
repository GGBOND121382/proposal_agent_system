from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import tempfile
import zipfile
from pathlib import Path
from typing import Iterable


_EPHEMERAL_PARTS = {"__pycache__", ".pytest_cache"}


class DeliveryVerificationError(RuntimeError):
    pass


def _is_ephemeral(relative_path: Path) -> bool:
    return (
        any(part in _EPHEMERAL_PARTS for part in relative_path.parts)
        or relative_path.suffix == ".pyc"
    )


def _source_files(root: Path) -> dict[str, Path]:
    root = Path(root)
    files: dict[str, Path] = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if _is_ephemeral(relative):
            continue
        files[relative.as_posix()] = path
    return dict(sorted(files.items()))


def file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def normalized_tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for relative, path in _source_files(Path(root)).items():
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_sha256(path).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def changed_paths(baseline_root: Path, candidate_root: Path) -> list[str]:
    baseline = _source_files(Path(baseline_root))
    candidate = _source_files(Path(candidate_root))
    changed: list[str] = []
    for relative in sorted(set(baseline) | set(candidate)):
        left = baseline.get(relative)
        right = candidate.get(relative)
        if left is None or right is None or left.read_bytes() != right.read_bytes():
            changed.append(relative)
    return changed


def _text_lines(path: Path | None) -> list[str]:
    if path is None:
        return []
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise DeliveryVerificationError(
            f"binary changed file is not supported by deterministic text patch: {path}"
        ) from exc


def build_unified_patch(baseline_root: Path, candidate_root: Path) -> str:
    baseline = _source_files(Path(baseline_root))
    candidate = _source_files(Path(candidate_root))
    output: list[str] = []
    for relative in changed_paths(baseline_root, candidate_root):
        left = baseline.get(relative)
        right = candidate.get(relative)
        for path in (left, right):
            if path is None:
                continue
            data = path.read_bytes()
            try:
                data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise DeliveryVerificationError(
                    f"binary changed file is not supported by deterministic text patch: {path}"
                ) from exc
            if data and not data.endswith(b"\n"):
                raise DeliveryVerificationError(
                    f"changed text file must end with LF for deterministic patch replay: {path}"
                )
        from_name = f"a/{relative}" if left is not None else "/dev/null"
        to_name = f"b/{relative}" if right is not None else "/dev/null"
        output.extend(
            difflib.unified_diff(
                _text_lines(left),
                _text_lines(right),
                fromfile=from_name,
                tofile=to_name,
                lineterm="",
            )
        )
    return ("\n".join(output) + "\n") if output else ""


def _parse_prompt_pack_sha256(source_root: Path) -> dict[str, str]:
    checksum_path = Path(source_root) / "prompt_pack" / "SHA256SUMS.txt"
    result: dict[str, str] = {}
    for raw_line in checksum_path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            continue
        digest, relative = raw_line.split("  ", 1)
        result[relative] = digest
    return result


def verify_prompt_pack_sha256(source_root: Path) -> int:
    source_root = Path(source_root)
    prompt_root = source_root / "prompt_pack"
    checksums = _parse_prompt_pack_sha256(source_root)
    mismatches: list[str] = []
    for relative, expected in checksums.items():
        path = prompt_root / relative
        if not path.is_file() or file_sha256(path) != expected:
            mismatches.append(relative)
    if mismatches:
        raise DeliveryVerificationError(
            "Prompt Pack SHA256 mismatch: " + ", ".join(mismatches[:10])
        )

    manifest = prompt_root / "MANIFEST.md"
    manifest_rows: dict[str, tuple[int, str]] = {}
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if not line.startswith("| `"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 3:
            continue
        relative = cells[0].strip("`")
        try:
            size = int(cells[1])
        except ValueError:
            continue
        digest = cells[2].strip("`")
        manifest_rows[relative] = (size, digest)
    if set(manifest_rows) != set(checksums):
        raise DeliveryVerificationError(
            "Prompt Pack MANIFEST path set differs from SHA256SUMS"
        )
    for relative, (size, digest) in manifest_rows.items():
        path = prompt_root / relative
        if size != path.stat().st_size or digest != checksums[relative]:
            raise DeliveryVerificationError(
                f"Prompt Pack MANIFEST metadata mismatch: {relative}"
            )
    return len(checksums)


def _hash_or_none(path: Path | None) -> str | None:
    return file_sha256(path) if path is not None and path.is_file() else None


def verify_changeset(
    baseline_root: Path,
    candidate_root: Path,
    patch_path: Path,
    changeset_path: Path,
) -> dict:
    baseline_root = Path(baseline_root)
    candidate_root = Path(candidate_root)
    patch_path = Path(patch_path)
    changeset_path = Path(changeset_path)
    changeset = json.loads(changeset_path.read_text(encoding="utf-8"))

    baseline_files = _source_files(baseline_root)
    candidate_files = _source_files(candidate_root)
    actual_changed = changed_paths(baseline_root, candidate_root)
    baseline_hash = normalized_tree_hash(baseline_root)
    final_hash = normalized_tree_hash(candidate_root)

    baseline_meta = changeset.get("baseline") or {}
    source_meta = changeset.get("source_integrity") or {}
    if baseline_meta.get("source_file_count") != len(baseline_files):
        raise DeliveryVerificationError("changeset baseline source_file_count mismatch")
    if baseline_meta.get("source_tree_sha256") != baseline_hash:
        raise DeliveryVerificationError("changeset baseline source_tree_sha256 mismatch")
    if source_meta.get("current_file_count") != len(candidate_files):
        raise DeliveryVerificationError("changeset current_file_count mismatch")
    if source_meta.get("final_source_tree_sha256") != final_hash:
        raise DeliveryVerificationError("changeset final_source_tree_sha256 mismatch")
    if source_meta.get("changed_path_count") != len(actual_changed):
        raise DeliveryVerificationError("changeset changed_path_count mismatch")
    if list(changeset.get("changed_paths") or []) != actual_changed:
        raise DeliveryVerificationError("changeset changed_paths mismatch")

    declared_hashes = changeset.get("changed_file_hashes") or {}
    if set(declared_hashes) != set(actual_changed):
        raise DeliveryVerificationError("changeset changed_file_hashes path set mismatch")
    for relative in actual_changed:
        entry = declared_hashes[relative]
        left = baseline_files.get(relative)
        right = candidate_files.get(relative)
        if entry.get("baseline_sha256") != _hash_or_none(left):
            raise DeliveryVerificationError(
                f"changeset baseline file hash mismatch: {relative}"
            )
        if entry.get("final_sha256") != _hash_or_none(right):
            raise DeliveryVerificationError(
                f"changeset final file hash mismatch: {relative}"
            )

    patch_meta = changeset.get("patch") or {}
    if patch_meta.get("sha256") != file_sha256(patch_path):
        raise DeliveryVerificationError("changeset patch SHA256 mismatch")
    regenerated_patch = build_unified_patch(baseline_root, candidate_root)
    if patch_path.read_text(encoding="utf-8") != regenerated_patch:
        raise DeliveryVerificationError(
            "published patch is not the deterministic baseline-to-candidate diff"
        )

    tracked = verify_prompt_pack_sha256(candidate_root)
    return {
        "baseline_source_files": len(baseline_files),
        "candidate_source_files": len(candidate_files),
        "changed_paths": len(actual_changed),
        "baseline_tree_sha256": baseline_hash,
        "candidate_tree_sha256": final_hash,
        "patch_sha256": file_sha256(patch_path),
        "prompt_pack_sha256_tracked": tracked,
    }


def verify_archive_matches_source(archive_path: Path, candidate_root: Path) -> str:
    archive_path = Path(archive_path)
    candidate_root = Path(candidate_root)
    with tempfile.TemporaryDirectory(prefix="semantic-delivery-verify-") as tmp:
        extracted = Path(tmp)
        with zipfile.ZipFile(archive_path) as archive:
            source_members = [
                name
                for name in archive.namelist()
                if name.startswith("source/") and not name.endswith("/")
            ]
            ephemeral = [
                name
                for name in source_members
                if _is_ephemeral(Path(name).relative_to("source"))
            ]
            if ephemeral:
                raise DeliveryVerificationError(
                    "archive source contains ephemeral test/cache files: "
                    + ", ".join(ephemeral[:10])
                )
            archive.extractall(extracted)
        archived_source = extracted / "source"
        candidate_files = _source_files(candidate_root)
        archived_files = _source_files(archived_source)
        if set(candidate_files) != set(archived_files):
            raise DeliveryVerificationError("archive source path set differs from candidate source")
        for relative in candidate_files:
            if candidate_files[relative].read_bytes() != archived_files[relative].read_bytes():
                raise DeliveryVerificationError(
                    f"archive source differs from candidate source: {relative}"
                )
        archived_hash = normalized_tree_hash(archived_source)
        candidate_hash = normalized_tree_hash(candidate_root)
        if archived_hash != candidate_hash:
            raise DeliveryVerificationError("archive source tree hash differs from candidate source")
        return archived_hash


def verify_archive_artifacts(
    archive_path: Path,
    artifacts: Iterable[Path],
) -> int:
    expected = [Path(path) for path in artifacts]
    if not expected:
        return 0
    with zipfile.ZipFile(Path(archive_path)) as archive:
        names = set(archive.namelist())
        for path in expected:
            member = path.name
            if member not in names:
                raise DeliveryVerificationError(
                    f"archive is missing published artifact: {member}"
                )
            if archive.read(member) != path.read_bytes():
                raise DeliveryVerificationError(
                    f"archive published artifact differs from external file: {member}"
                )
    return len(expected)


_PROMPT_AUDIT_SUMMARY_FIELDS = (
    "prompt_count",
    "replay_count",
    "blocking_issue_count",
    "informational_issue_count",
    "replay_status_counts",
)


def _prompt_audit_summary(prompt_audit_path: Path) -> dict:
    audit = json.loads(Path(prompt_audit_path).read_text(encoding="utf-8"))
    summary = audit.get("summary") or {}
    result = {"status": audit.get("status")}
    for field in _PROMPT_AUDIT_SUMMARY_FIELDS:
        if field not in summary:
            raise DeliveryVerificationError(
                f"prompt audit summary is missing required field: {field}"
            )
        result[field] = summary[field]
    if not result.get("status"):
        raise DeliveryVerificationError("prompt audit status is missing")
    return result


def verify_published_semantics(
    changeset_path: Path,
    report_path: Path,
    prompt_audit_path: Path,
) -> dict:
    """Verify that human/machine handoff artifacts report the same audit facts."""
    changeset = json.loads(Path(changeset_path).read_text(encoding="utf-8"))
    audit_summary = _prompt_audit_summary(Path(prompt_audit_path))
    declared = ((changeset.get("validation") or {}).get("prompt_contract_audit") or {})
    for field, expected in audit_summary.items():
        if declared.get(field) != expected:
            raise DeliveryVerificationError(
                f"changeset prompt_contract_audit {field} mismatch"
            )

    report = Path(report_path).read_text(encoding="utf-8")
    expected_lines = [
        f"- status：**{audit_summary['status']}**",
        f"- prompts：**{audit_summary['prompt_count']}**",
        f"- replay cases：**{audit_summary['replay_count']}**",
        f"- blocking issues：**{audit_summary['blocking_issue_count']}**",
        f"- informational issues：**{audit_summary['informational_issue_count']}**",
    ]
    missing = [line for line in expected_lines if line not in report]
    if missing:
        raise DeliveryVerificationError(
            "report prompt audit summary mismatch: " + "; ".join(missing)
        )
    return audit_summary


def verify_delivery(
    *,
    baseline_root: Path,
    candidate_root: Path,
    patch_path: Path,
    changeset_path: Path,
    archive_path: Path,
    published_artifacts: Iterable[Path] = (),
    report_path: Path | None = None,
    prompt_audit_path: Path | None = None,
) -> dict:
    result = verify_changeset(
        baseline_root=baseline_root,
        candidate_root=candidate_root,
        patch_path=patch_path,
        changeset_path=changeset_path,
    )
    result["archive_tree_sha256"] = verify_archive_matches_source(
        archive_path, candidate_root
    )
    result["archive_published_artifacts_verified"] = verify_archive_artifacts(
        archive_path, published_artifacts
    )
    if (report_path is None) != (prompt_audit_path is None):
        raise DeliveryVerificationError(
            "report_path and prompt_audit_path must be supplied together"
        )
    if report_path is not None and prompt_audit_path is not None:
        result["prompt_contract_audit"] = verify_published_semantics(
            changeset_path, report_path, prompt_audit_path
        )
        result["published_semantics_verified"] = True
    else:
        result["published_semantics_verified"] = False
    result["status"] = "PASS"
    return result


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-root", type=Path, required=True)
    parser.add_argument("--candidate-root", type=Path, required=True)
    parser.add_argument("--patch", type=Path, required=True)
    parser.add_argument("--changeset", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, action="append", default=[])
    parser.add_argument("--report", type=Path)
    parser.add_argument("--prompt-audit", type=Path)
    args = parser.parse_args(list(argv) if argv is not None else None)
    result = verify_delivery(
        baseline_root=args.baseline_root,
        candidate_root=args.candidate_root,
        patch_path=args.patch,
        changeset_path=args.changeset,
        archive_path=args.archive,
        published_artifacts=args.artifact,
        report_path=args.report,
        prompt_audit_path=args.prompt_audit,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
