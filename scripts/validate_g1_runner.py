from __future__ import annotations

import re

try:
    from . import validate_g1 as impl
except ImportError:  # Direct script execution.
    import validate_g1 as impl


GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class _GitOrContentHashMatcher:
    """Compatibility matcher for the original controller implementation.

    The original controller used one 40-character matcher for both Git object IDs
    and SHA-256 content digests.  Runtime callers still use ``impl.SHA_RE`` in both
    places, so this matcher accepts either representation while the patched
    manifest validator below continues to require exactly 40 characters for Git
    commits.
    """

    @staticmethod
    def fullmatch(value: str):
        return GIT_SHA_RE.fullmatch(value) or SHA256_RE.fullmatch(value)


_original_validate_manifest = impl.validate_manifest
impl.SHA_RE = _GitOrContentHashMatcher()


def validate_manifest_strict(manifest):
    errors = _original_validate_manifest(manifest)
    baseline = str(manifest.get("controller_baseline") or "")
    if not GIT_SHA_RE.fullmatch(baseline) and "G1_CONTROLLER_BASELINE_SHA" not in errors:
        errors.append("G1_CONTROLLER_BASELINE_SHA")
    for item in manifest.get("tracks") or []:
        if not isinstance(item, dict):
            continue
        track_id = str(item.get("id") or "UNKNOWN")
        code = f"G1_{track_id}_SHA"
        if not GIT_SHA_RE.fullmatch(str(item.get("sha") or "")) and code not in errors:
            errors.append(code)
    return errors


impl.validate_manifest = validate_manifest_strict


if __name__ == "__main__":
    raise SystemExit(impl.main())
