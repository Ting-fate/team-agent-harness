from __future__ import annotations

import re


_SECRET_ASSIGNMENT_KEY = (
    r"[A-Za-z0-9_.-]*(?:api[_-]?key|apikey|access[_-]?key|client[_-]?secret|credential|"
    r"database[_-]?url|db[_-]?url|dsn|connection[_-]?string|password|passwd|private[_-]?key|"
    r"pwd|secret|token)"
)
_REDACTION_ASSIGNMENT_KEY = rf"(?:{_SECRET_ASSIGNMENT_KEY}|payload)"
_QUOTED_SECRET_ASSIGNMENT = re.compile(
    rf"(?i)(?P<prefix>['\"]?\b{_REDACTION_ASSIGNMENT_KEY}['\"]?\s*[:=]\s*)"
    r"(?P<quote>['\"])(?P<value>.*?)(?P=quote)"
)
_UNQUOTED_SECRET_ASSIGNMENT = re.compile(
    rf"(?i)(?P<prefix>\b{_REDACTION_ASSIGNMENT_KEY}\s*[:=]\s*)(?P<value>[^\s,;&]+)"
)
_URL_PASSWORD = re.compile(
    r"(?i)\b(?P<prefix>[a-z][a-z0-9+.-]*://[^/\s:@]+:)(?P<value>[^@/\s]+)(?P<suffix>@)"
)
_AUTHORIZATION_PREFIX = r"['\"]?\bauthorization['\"]?\s*[:=]\s*"
_AUTHORIZATION_VALUE = re.compile(rf"(?i){_AUTHORIZATION_PREFIX}\S+")
_QUOTED_AUTHORIZATION_ASSIGNMENT = re.compile(
    rf"(?i)(?P<prefix>{_AUTHORIZATION_PREFIX})(?P<quote>['\"])(?P<value>.*?)(?P=quote)"
)
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----.*?"
    r"-----END (?:[A-Z0-9 ]+ )?PRIVATE KEY-----",
    flags=re.DOTALL,
)
_SECRET_VALUE_PATTERNS = (
    re.compile(
        rf"(?i)\b{_SECRET_ASSIGNMENT_KEY}\s*[:=]\s*\S+"
    ),
    _AUTHORIZATION_VALUE,
    _URL_PASSWORD,
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9][A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bASIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----"),
)


def contains_secret_like_text(value: str) -> bool:
    return any(pattern.search(value) for pattern in _SECRET_VALUE_PATTERNS)


def redact_secret_like_text(value: str) -> str:
    redacted = value.replace("\x00", "")
    redacted = _PRIVATE_KEY_BLOCK.sub("[REDACTED PRIVATE KEY]", redacted)
    redacted = _QUOTED_AUTHORIZATION_ASSIGNMENT.sub(
        lambda match: (
            f"{match.group('prefix')}{match.group('quote')}[REDACTED]{match.group('quote')}"
        ),
        redacted,
    )
    redacted = re.sub(
        rf"(?i)({_AUTHORIZATION_PREFIX}(?:Bearer|Basic)\s+)[^\s,;]+",
        r"\1[REDACTED]",
        redacted,
    )
    redacted = re.sub(
        rf"(?i)({_AUTHORIZATION_PREFIX})(?!(?:Bearer|Basic)\b|['\"])[^\s,;]+",
        r"\1[REDACTED]",
        redacted,
    )
    redacted = re.sub(r"(?i)(Bearer\s+)[^\s,;]+", r"\1[REDACTED]", redacted)
    redacted = _URL_PASSWORD.sub(
        lambda match: f"{match.group('prefix')}[REDACTED]{match.group('suffix')}",
        redacted,
    )
    redacted = _QUOTED_SECRET_ASSIGNMENT.sub(
        lambda match: (
            f"{match.group('prefix')}{match.group('quote')}[REDACTED]{match.group('quote')}"
        ),
        redacted,
    )
    redacted = _UNQUOTED_SECRET_ASSIGNMENT.sub(
        lambda match: f"{match.group('prefix')}[REDACTED]",
        redacted,
    )
    redacted = re.sub(r"\bsk-[A-Za-z0-9_-]+\b", "sk-[REDACTED]", redacted)
    redacted = re.sub(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b", "[REDACTED]", redacted)
    redacted = re.sub(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b", "[REDACTED]", redacted)
    redacted = re.sub(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b", "[REDACTED]", redacted)
    redacted = re.sub(r"\bAIza[0-9A-Za-z_-]{30,}\b", "[REDACTED]", redacted)
    redacted = re.sub(
        r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b",
        "[REDACTED]",
        redacted,
    )
    return redacted
