from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

FIGURE_MARKER = "[[FIGURE]]"
ARTIFACT_SCHEME = "artifact://"
_SUPPORTED_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}


class FigureProtocolError(ValueError):
    """Raised when a figure directive is malformed or cannot be resolved safely."""


@dataclass(frozen=True)
class FigureDirective:
    reference: str
    caption: str
    width_cm: float
    source_reference: str | None = None

    def marker(self) -> str:
        parts = [self.reference, self.caption, f"{self.width_cm:g}"]
        if self.source_reference:
            parts.append(f"source={self.source_reference}")
        return FIGURE_MARKER + "|".join(parts)


def artifact_reference(path: Path, data_dir: Path) -> str:
    """Return a portable reference rooted at APP_DATA_DIR instead of an absolute path."""
    root = data_dir.resolve()
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise FigureProtocolError(f"Figure artifact is outside APP_DATA_DIR: {resolved.name}") from exc
    return ARTIFACT_SCHEME + relative.as_posix()


def parse_figure_block(text: str) -> list[FigureDirective]:
    """Parse one block containing one or more consecutive figure directives.

    The protocol intentionally requires figure directives to occupy the complete block.
    This prevents ordinary prose from being swallowed by a malformed marker. Multiple
    directives can be adjacent or separated by whitespace/newlines and are returned in
    their original order.
    """
    raw = (text or "").strip()
    if FIGURE_MARKER not in raw:
        return []
    if not raw.startswith(FIGURE_MARKER):
        raise FigureProtocolError("Figure directives must be standalone blocks")

    pieces = raw.split(FIGURE_MARKER)[1:]
    directives: list[FigureDirective] = []
    for index, piece in enumerate(pieces, 1):
        body = piece.strip()
        if not body:
            raise FigureProtocolError(f"Figure directive {index} is empty")
        if "\n" in body or "\r" in body:
            nonempty = [line.strip() for line in body.splitlines() if line.strip()]
            if len(nonempty) != 1:
                raise FigureProtocolError(
                    f"Figure directive {index} contains unexpected multiline content"
                )
            body = nonempty[0]
        directives.append(parse_figure_directive(body, index=index))
    return directives


def parse_figure_directive(raw: str, *, index: int = 1) -> FigureDirective:
    parts = [part.strip() for part in raw.split("|")]
    if len(parts) < 2 or not parts[0] or not parts[1]:
        raise FigureProtocolError(
            f"Figure directive {index} must include reference and caption"
        )
    reference = parts[0]
    caption = parts[1]
    try:
        width_cm = float(parts[2]) if len(parts) > 2 and parts[2] else 15.0
    except ValueError as exc:
        raise FigureProtocolError(f"Figure directive {index} has invalid width") from exc
    if not 2.0 <= width_cm <= 18.0:
        raise FigureProtocolError(
            f"Figure directive {index} width must be between 2 and 18 cm"
        )

    source_reference: str | None = None
    for extra in parts[3:]:
        if not extra:
            continue
        if extra.startswith("source="):
            source_reference = extra.removeprefix("source=").strip() or None
            continue
        raise FigureProtocolError(
            f"Figure directive {index} has unsupported option: {extra[:40]}"
        )
    _validate_reference(reference, index=index)
    if source_reference:
        _validate_reference(source_reference, index=index, allow_non_image=True)
    return FigureDirective(reference, caption, width_cm, source_reference)


def resolve_figure_reference(reference: str, data_dir: Path) -> Path:
    """Resolve a portable or legacy reference while preventing path traversal."""
    root = data_dir.resolve()
    if reference.startswith(ARTIFACT_SCHEME):
        relative = reference.removeprefix(ARTIFACT_SCHEME)
        candidate = root / relative
    else:
        supplied = Path(reference)
        candidate = supplied if supplied.is_absolute() else root / supplied
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise FigureProtocolError("Figure reference escapes APP_DATA_DIR") from exc
    if resolved.suffix.lower() not in _SUPPORTED_IMAGE_SUFFIXES:
        raise FigureProtocolError(
            f"Unsupported figure format: {resolved.suffix or '<none>'}"
        )
    if not resolved.is_file() or resolved.stat().st_size < 100:
        raise FigureProtocolError(f"Figure artifact is missing or invalid: {resolved.name}")
    return resolved


def visible_references(directives: Iterable[FigureDirective]) -> list[str]:
    """Return the protocol references suitable for manifests, never filesystem paths."""
    refs: list[str] = []
    for directive in directives:
        refs.append(directive.reference)
        if directive.source_reference:
            refs.append(directive.source_reference)
    return refs


def _validate_reference(reference: str, *, index: int, allow_non_image: bool = False) -> None:
    if not reference or "\x00" in reference:
        raise FigureProtocolError(f"Figure directive {index} has an invalid reference")
    if reference.startswith(ARTIFACT_SCHEME):
        relative = reference.removeprefix(ARTIFACT_SCHEME)
        path = Path(relative)
        if not relative or path.is_absolute() or ".." in path.parts:
            raise FigureProtocolError(
                f"Figure directive {index} has an unsafe artifact reference"
            )
    else:
        path = Path(reference)
        if ".." in path.parts:
            raise FigureProtocolError(f"Figure directive {index} contains path traversal")
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", reference) and not reference.startswith(
        ARTIFACT_SCHEME
    ):
        raise FigureProtocolError(f"Figure directive {index} uses an unsupported URI")
    if not allow_non_image:
        suffix = Path(reference.removeprefix(ARTIFACT_SCHEME)).suffix.lower()
        if suffix and suffix not in _SUPPORTED_IMAGE_SUFFIXES:
            raise FigureProtocolError(
                f"Figure directive {index} must reference PNG or JPEG"
            )
