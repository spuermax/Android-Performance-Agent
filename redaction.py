from __future__ import annotations

import re
from typing import Any


REDACTED = "***REDACTED***"
_SENSITIVE_KEY = re.compile(
    r"(?:storepassword|keypassword|password|token|secret|api[_-]?key)",
    re.IGNORECASE,
)
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?P<prefix>(?:[\"']?)(?:storepassword|keypassword|password|token|secret|"
    r"api[_-]?key)(?:[\"']?)\s*(?:=|:)\s*)"
    r"(?P<value>[\"'][^\"']*[\"']|[^\s,;]+)",
    re.IGNORECASE,
)
_SENSITIVE_QUOTED_ARGUMENT = re.compile(
    r"(?P<prefix>\b(?:storepassword|keypassword|password|token|secret|"
    r"api[_-]?key)\b\s+)(?P<value>[\"'][^\"']*[\"'])",
    re.IGNORECASE,
)


def redact_text(value: str) -> str:
    """Redact common credential assignments from human-readable output."""
    redacted = _SENSITIVE_ASSIGNMENT.sub(
        lambda match: match.group("prefix") + REDACTED,
        value,
    )
    return _SENSITIVE_QUOTED_ARGUMENT.sub(
        lambda match: match.group("prefix") + REDACTED,
        redacted,
    )


def redact_sensitive_data(value: Any) -> Any:
    """Return a JSON-compatible copy with credential-like values removed."""
    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and _SENSITIVE_KEY.search(key):
                redacted[key] = REDACTED
            else:
                redacted[key] = redact_sensitive_data(item)
        return redacted
    if isinstance(value, list):
        return [redact_sensitive_data(item) for item in value]
    if isinstance(value, tuple):
        return [redact_sensitive_data(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value
