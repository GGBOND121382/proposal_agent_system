from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any


class JsonPointerError(ValueError):
    """Raised when an RFC 6901 JSON Pointer is malformed or cannot resolve."""


def escape_token(token: object) -> str:
    """Encode one reference token according to RFC 6901 section 4."""

    return str(token).replace("~", "~0").replace("/", "~1")


def unescape_token(token: str) -> str:
    """Decode one reference token and reject non-RFC escape sequences."""

    text = str(token)
    index = 0
    while index < len(text):
        if text[index] == "~":
            if index + 1 >= len(text) or text[index + 1] not in {"0", "1"}:
                raise JsonPointerError(f"invalid JSON Pointer escape in token: {text!r}")
            index += 2
            continue
        index += 1
    return text.replace("~1", "/").replace("~0", "~")


def parse_pointer(pointer: str, *, allow_root: bool = False) -> tuple[str, ...]:
    """Return decoded reference tokens for a strict RFC 6901 pointer.

    The Targeted Repair protocol intentionally disallows the empty root pointer,
    because every repair path must name a member beneath ``original_object``.
    Callers that need generic RFC 6901 root support may opt in with
    ``allow_root=True``.
    """

    if not isinstance(pointer, str):
        raise JsonPointerError("JSON Pointer must be a string")
    if pointer == "":
        if allow_root:
            return ()
        raise JsonPointerError("root JSON Pointer is not allowed here")
    if not pointer.startswith("/"):
        raise JsonPointerError(f"JSON Pointer must start with '/': {pointer!r}")
    return tuple(unescape_token(token) for token in pointer[1:].split("/"))


def format_pointer(tokens: Iterable[object], *, allow_root: bool = False) -> str:
    """Encode decoded reference tokens as an RFC 6901 pointer."""

    values = tuple(tokens)
    if not values:
        if allow_root:
            return ""
        raise JsonPointerError("root JSON Pointer is not allowed here")
    return "/" + "/".join(escape_token(token) for token in values)


def join_pointer(*tokens: object) -> str:
    """Convenience wrapper for constructing a non-root pointer."""

    return format_pointer(tokens)


def is_ancestor_or_same(root: str, candidate: str) -> bool:
    """Return whether ``candidate`` is ``root`` or one of its descendants.

    Comparison is token based, so ``/items/1`` never authorizes ``/items/10``.
    Invalid pointers are rejected rather than silently normalized.
    """

    root_tokens = parse_pointer(root)
    candidate_tokens = parse_pointer(candidate)
    return len(root_tokens) <= len(candidate_tokens) and candidate_tokens[: len(root_tokens)] == root_tokens


def paths_overlap(first: str, second: str) -> bool:
    """Return whether either pointer is an ancestor of the other."""

    return is_ancestor_or_same(first, second) or is_ancestor_or_same(second, first)


def _sequence_index(token: str, size: int) -> int:
    if token == "-":
        raise JsonPointerError("the append token '-' cannot resolve an existing value")
    if not token.isdigit() or (len(token) > 1 and token.startswith("0")):
        raise JsonPointerError(f"invalid array index token: {token!r}")
    index = int(token)
    if index >= size:
        raise JsonPointerError(f"array index out of range: {index}")
    return index


def resolve_pointer(document: Any, pointer: str) -> Any:
    """Resolve an RFC 6901 pointer against JSON-like Python data."""

    current = document
    for token in parse_pointer(pointer):
        if isinstance(current, Mapping):
            if token not in current:
                raise JsonPointerError(f"object key does not exist: {token!r}")
            current = current[token]
            continue
        if isinstance(current, Sequence) and not isinstance(current, (str, bytes, bytearray)):
            current = current[_sequence_index(token, len(current))]
            continue
        raise JsonPointerError(f"cannot traverse token {token!r} through {type(current).__name__}")
    return current


def pointer_exists(document: Any, pointer: str) -> bool:
    try:
        resolve_pointer(document, pointer)
    except JsonPointerError:
        return False
    return True
