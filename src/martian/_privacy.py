"""Small, dependency-free reporting boundary shared by both SDK modules."""

from __future__ import annotations

import hashlib
import os
import re
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from pydantic import SecretStr

_SECRETS = (
    "MARTIAN_API_TOKEN",
    "MARTIAN_API_KEY",
    "MARTIAN_REDTEAM_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_BEARER_TOKEN_BEDROCK",
)


@dataclass(frozen=True)
class RedactionPolicy:
    """Small private report context; never retain a client or mutate credentials."""

    _secrets: tuple[SecretStr, ...] = field(default=(), repr=False)

    @classmethod
    def from_secrets(cls, *values: str | SecretStr) -> RedactionPolicy:
        return cls(tuple(SecretStr(v.get_secret_value() if isinstance(v, SecretStr) else v) for v in values if v))

    def combine(self, other: RedactionPolicy) -> RedactionPolicy:
        return RedactionPolicy((*self._secrets, *other._secrets))


REDACTION: ContextVar[RedactionPolicy | None] = ContextVar("martian_redaction", default=None)


def safe_text(value: object, *, policy: RedactionPolicy | None = None) -> str:
    text = str(value).replace("\x00", "").encode("utf-8", "replace").decode("utf-8")
    for name in _SECRETS:
        secret = os.environ.get(name, "")
        if secret:
            text = text.replace(secret, "[redacted]")
    active = REDACTION.get() or RedactionPolicy()
    if policy is not None:
        active = active.combine(policy)
    for hidden in active._secrets:
        if secret := hidden.get_secret_value():
            text = text.replace(secret, "[redacted]")
    return re.sub(r"(?i)(bearer\s+|(?:api[_-]?key|token|password)\s*[=:]\s*)[^\s,;]+", r"\1[redacted]", text)


def redact_data(value: Any, *, policy: RedactionPolicy) -> Any:
    """Scrub report metadata as well as bodies without serializing private context."""
    if isinstance(value, str):
        return safe_text(value, policy=policy)
    if isinstance(value, list):
        return [redact_data(item, policy=policy) for item in value]
    if isinstance(value, dict):
        return {key: redact_data(item, policy=policy) for key, item in value.items()}
    return value


def report_text(
    value: str, *, include_content: bool = False, redact_env: str = "", policy: RedactionPolicy | None = None
) -> str:
    """Target content is opt-in. Legacy explicit redaction always takes precedence."""
    text = safe_text(value, policy=policy)
    include = include_content or os.environ.get("MARTIAN_REPORT_INCLUDE_CONTENT") == "1"
    if text and (not include or (redact_env and os.environ.get(redact_env) == "1")):
        return "sha256:" + hashlib.sha256(text.encode()).hexdigest()[:16]
    return text
