from __future__ import annotations

import re
from typing import Any


_REDACTED = "[REDACTED_CREDENTIAL]"

# High-confidence credential forms only.  The redactor deliberately does not
# treat ordinary words such as "token", "secret", or "api key" as sensitive
# unless an actual credential-like value is attached.
_BEARER_RE = re.compile(
    r"(?i)(\bAuthorization\s*:\s*Bearer\s+|\bBearer\s+)([A-Za-z0-9._~+/=-]{12,})"
)
_SK_RE = re.compile(r"(?<![A-Za-z0-9])(?:sk-api-|sk-)[A-Za-z0-9_-]{20,}")
_ASSIGNMENT_RE = re.compile(
    r"(?i)(\b(?:api[_ -]?key|access[_ -]?token|auth[_ -]?token|client[_ -]?secret|secret)\b"
    r"\s*[:=]\s*[\"']?)([A-Za-z0-9._~+/=-]{12,})"
)


def redact_secret_text(value: str) -> str:
    """Remove credential material while retaining useful diagnostic context."""

    text = str(value)
    text = _BEARER_RE.sub(lambda match: match.group(1) + _REDACTED, text)
    text = _SK_RE.sub(_REDACTED, text)
    text = _ASSIGNMENT_RE.sub(lambda match: match.group(1) + _REDACTED, text)
    return text


def contains_secret_material(value: str) -> bool:
    text = str(value)
    return bool(_BEARER_RE.search(text) or _SK_RE.search(text) or _ASSIGNMENT_RE.search(text))


def redact_secrets(value: Any) -> Any:
    """Recursively redact credential strings before low-classification logging."""

    if isinstance(value, dict):
        return {key: redact_secrets(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_secrets(item) for item in value)
    if isinstance(value, str):
        return redact_secret_text(value)
    return value
