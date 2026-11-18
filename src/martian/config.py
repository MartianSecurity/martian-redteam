"""Shared connection from PLAN_MARTIAN_SDK_SINGLE_KEY.md; CI attribution stays separate."""

from __future__ import annotations

import math
import os
import re
import subprocess
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import SecretStr

from martian._privacy import RedactionPolicy

_SHA = re.compile(r"^[0-9a-fA-F]{7,64}$")
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class ConfigError(ValueError):
    """A controlled configuration error whose message contains no supplied value."""


def backend_origin(value: str) -> str:
    try:
        parts = urlsplit(value.strip())
        _ = parts.port
    except (ValueError, AttributeError):
        raise ConfigError("MARTIAN_API_URL must be a valid Backend origin") from None
    if (
        not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or parts.path not in ("", "/")
    ):
        raise ConfigError("MARTIAN_API_URL must be a Backend origin without credentials, path, query or fragment")
    if parts.scheme != "https" and not (parts.scheme == "http" and parts.hostname in _LOCAL_HOSTS):
        raise ConfigError("MARTIAN_API_URL must use HTTPS (HTTP is allowed only for loopback development)")
    return value.strip().rstrip("/")


@dataclass(frozen=True, init=False)
class MartianConfig:
    api_url: str
    token: SecretStr = field(repr=False)
    request_timeout_seconds: float
    trust_env: bool

    def __init__(
        self,
        api_url: str,
        token: str | SecretStr,
        *,
        request_timeout_seconds: float = 120.0,
        trust_env: bool = False,
    ) -> None:
        normalized = backend_origin(api_url)
        secret = token.get_secret_value() if isinstance(token, SecretStr) else token
        if not isinstance(secret, str) or not secret.strip():
            raise ConfigError("MARTIAN_API_TOKEN is missing")
        if any(not 33 <= ord(char) <= 126 for char in secret.strip()):
            raise ConfigError("MARTIAN_API_TOKEN must be an opaque ASCII token without whitespace")
        if (
            not isinstance(request_timeout_seconds, (int, float))
            or isinstance(request_timeout_seconds, bool)
            or not math.isfinite(request_timeout_seconds)
            or not 1 <= request_timeout_seconds <= 240
        ):
            raise ConfigError("request_timeout_seconds must be finite and between 1 and 240")
        if type(trust_env) is not bool:
            raise ConfigError("trust_env must be a boolean")
        object.__setattr__(self, "api_url", normalized)
        object.__setattr__(self, "token", SecretStr(secret.strip()))
        object.__setattr__(self, "request_timeout_seconds", float(request_timeout_seconds))
        object.__setattr__(self, "trust_env", trust_env)

    @classmethod
    def from_env(cls, *, api_url: str | None = None, token: str | SecretStr | None = None) -> MartianConfig:
        if (api_url is None) != (token is None):
            raise ConfigError("provide api_url and token together, or use the canonical environment pair")
        chosen_url = api_url if api_url is not None else os.environ.get("MARTIAN_API_URL", "")
        chosen_token = token if token is not None else os.environ.get("MARTIAN_API_TOKEN", "")
        missing = [
            name for name, value in (("MARTIAN_API_URL", chosen_url), ("MARTIAN_API_TOKEN", chosen_token)) if not value
        ]
        if missing:
            raise ConfigError("missing " + ", ".join(missing))
        return cls(chosen_url, chosen_token)

    @property
    def redaction_policy(self) -> RedactionPolicy:
        return RedactionPolicy.from_secrets(self.token)


def git_sha() -> str:
    for name in ("MARTIAN_EVALS_GIT_SHA", "GITHUB_SHA"):
        if value := os.environ.get(name, "").strip():
            return value
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True, timeout=10
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


@dataclass(frozen=True)
class CiContext:
    agent_id: UUID | str
    git_sha: str
    repo: str = ""
    ref: str = ""
    run_url: str = ""

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "agent_id", UUID(str(self.agent_id)))
        except ValueError:
            raise ConfigError("CI agent_id must be a UUID") from None
        if not self.git_sha:
            raise ConfigError("no commit to attribute the run to: set git_sha or MARTIAN_EVALS_GIT_SHA")
        if not _SHA.fullmatch(self.git_sha):
            raise ConfigError("CI git_sha must name the tested revision")

    @classmethod
    def from_env(cls) -> CiContext:
        repo, run_id = os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GITHUB_RUN_ID", "")
        server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
        return cls(
            agent_id=os.environ.get("MARTIAN_AGENT_ID", ""),
            git_sha=git_sha(),
            repo=repo,
            ref=os.environ.get("GITHUB_REF", "")[:255],
            run_url=f"{server}/{repo}/actions/runs/{run_id}" if repo and run_id else "",
        )
