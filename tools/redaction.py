from __future__ import annotations

import re
from typing import Any

REDACTED = "***REDACTED***"

_SENSITIVE_KEYWORDS = {
    "password",
    "passwd",
    "pwd",
    "storepassword",
    "keypassword",
    "apikey",
    "accesskey",
    "secret",
    "clientsecret",
    "token",
    "authorization",
    "username",
}

# Covers common Gradle/Groovy/JSON/properties/env assignment styles.
_QUOTED_ASSIGNMENT = re.compile(
    r"(?i)(\b(?:storePassword|keyPassword|password|passwd|pwd|api[_-]?key|"
    r"access[_-]?key|secret|client[_-]?secret|token|authorization|username)\b"
    r"\s*(?:=|:)?\s*)([\"'])(.*?)(\2)"
)
_ENV_ASSIGNMENT = re.compile(
    r"(?i)(\b[A-Z0-9_.-]*(?:PASSWORD|PASSWD|PWD|API[_-]?KEY|ACCESS[_-]?KEY|"
    r"SECRET|TOKEN|USERNAME)[A-Z0-9_.-]*\b\s*[:=]\s*)([^\s,#}\]]+)"
)
_UNQUOTED_ASSIGNMENT = re.compile(
    r"(?i)(\b(?:storePassword|keyPassword|password|passwd|pwd|api[_-]?key|"
    r"access[_-]?key|secret|client[_-]?secret|token|username)\b\s*(?:=|:)\s*)"
    r"([^\s,#}\]]+)"
)
_AUTHORIZATION_HEADER = re.compile(r"(?i)(\bauthorization\s*:\s*)(.+)$")
_URI_CREDENTIALS = re.compile(r"(https?://)([^/\s:@]+):([^@\s/]+)@", re.I)


def is_sensitive_key(key: object) -> bool:
    if not isinstance(key, str):
        return False
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return any(
        normalized == keyword or normalized.endswith(keyword)
        for keyword in _SENSITIVE_KEYWORDS
    )


def redact_text(value: str) -> str:
    if not value:
        return value

    value = _URI_CREDENTIALS.sub(
        lambda match: f"{match.group(1)}{REDACTED}:{REDACTED}@",
        value,
    )
    value = _AUTHORIZATION_HEADER.sub(
        lambda match: f"{match.group(1)}{REDACTED}",
        value,
    )
    value = _QUOTED_ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}{match.group(2)}{REDACTED}{match.group(2)}",
        value,
    )
    value = _ENV_ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}{REDACTED}",
        value,
    )
    value = _UNQUOTED_ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}{REDACTED}",
        value,
    )
    return value


def redact_value(value: Any, *, key: object | None = None) -> Any:
    """Recursively redact secrets before data reaches logs, UI or the LLM."""
    if key is not None and is_sensitive_key(key):
        return REDACTED
    if isinstance(value, dict):
        return {
            item_key: redact_value(item_value, key=item_key)
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item) for item in value)
    if isinstance(value, str):
        return redact_text(value)
    return value
