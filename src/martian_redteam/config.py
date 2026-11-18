"""Environment and YAML configuration."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Self
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ALLOW_INSECURE_API_URL_ENV = "MARTIAN_ALLOW_INSECURE_API_URL"
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1"})


def require_secure_api_url(value: str, *, label: str) -> str:
    """Return the normalised API URL, or raise if it would expose the API key.

    The client attaches ``X-API-Key`` to every request to this URL, so a plain
    ``http://`` URL sends the key in clear text and lets a one-line config
    change redirect it. ``https://`` is required unless the host is loopback.

    ``MARTIAN_ALLOW_INSECURE_API_URL=1`` lifts the rule for development. It is
    read from the process environment only, never from ``.env`` or YAML, so a
    file committed to a repository cannot switch it on.
    """
    trimmed = value.strip().rstrip("/")
    parts = urlsplit(trimmed)
    if parts.username is not None or parts.password is not None or parts.query or parts.fragment:
        raise ValueError(f"{label} must not contain credentials, query parameters or a fragment")
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError(f"{label} must start with http:// or https://")
    if parts.scheme == "https" or parts.hostname in _LOOPBACK_HOSTS:
        return trimmed
    if os.environ.get(ALLOW_INSECURE_API_URL_ENV) == "1":
        return trimmed
    raise ValueError(
        f"{label} must use https:// (http:// is accepted only for localhost and 127.0.0.1; "
        f"set {ALLOW_INSECURE_API_URL_ENV}=1 in the process environment for development)"
    )


class Settings(BaseSettings):
    """API location and credentials. Loaded from the environment."""

    model_config = SettingsConfigDict(
        env_prefix="MARTIAN_REDTEAM_",
        env_file=None,
        extra="ignore",
    )

    api_url: str = ""
    api_key: SecretStr = Field(default=SecretStr(""))
    request_timeout_seconds: float = Field(default=120.0, ge=1.0, le=240.0)

    def __init__(self, **values: Any) -> None:
        if ("api_url" in values) != ("api_key" in values):
            raise ValueError(
                "provide the complete explicit legacy api_url/api_key pair, or the dedicated environment pair"
            )
        super().__init__(**values)

    @field_validator("api_url")
    @classmethod
    def api_url_must_be_secure(cls, value: str) -> str:
        return require_secure_api_url(value, label=f"{cls.model_config['env_prefix']}API_URL")


class CallbackRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file: str
    function: str = "model_callback"


class TargetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    purpose: str
    callback: CallbackRef


class NamedSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    types: list[str] | None = None
    weight: float | None = None
    id: str | None = None
    criteria: str | None = None
    gated: bool | None = None


class SystemConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_concurrent: int = 2
    attacks_per_vulnerability_type: int = 1
    output_folder: str = "results"
    # Upper bound for one target callback call; a hung target becomes a target error.
    target_timeout_seconds: float = Field(default=120.0, ge=1.0, allow_inf_nan=False)

    @field_validator("max_concurrent")
    @classmethod
    def concurrent_must_be_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("max_concurrent must be >= 1")
        return value

    @field_validator("attacks_per_vulnerability_type")
    @classmethod
    def attacks_must_be_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("attacks_per_vulnerability_type must be >= 1")
        return value


class GateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fail_on_infrastructure_error: bool = True
    ignore_target_errors: bool = False
    pass_rate_threshold: float = 1.0

    @field_validator("pass_rate_threshold")
    @classmethod
    def threshold_must_be_unit_interval(cls, value: float) -> float:
        if not 0.0 <= value <= 1.0:
            raise ValueError("pass_rate_threshold must be between 0 and 1")
        return value


class ApiConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: HttpUrl | None = None

    @field_validator("url")
    @classmethod
    def url_must_be_secure(cls, value: HttpUrl | None) -> HttpUrl | None:
        # settings_overlay copies this URL into Settings without re-validating,
        # so the YAML override has to be checked here.
        if value is not None:
            require_secure_api_url(str(value), label="api.url")
        return value


class RunConfig(BaseModel):
    """YAML file loaded by `martian-redteam run`."""

    model_config = ConfigDict(extra="forbid")

    target: TargetConfig
    system_config: SystemConfig = Field(default_factory=SystemConfig)
    vulnerabilities: list[NamedSpec]
    attacks: list[NamedSpec]
    gate: GateConfig = Field(default_factory=GateConfig)
    api: ApiConfig | None = None

    @classmethod
    def from_yaml(cls, path: str | Path) -> Self:
        config_path = Path(path).resolve()
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("config YAML must be a mapping")
        config = cls.model_validate(raw)
        callback_file = Path(config.target.callback.file)
        if not callback_file.is_absolute():
            callback_file = (config_path.parent / callback_file).resolve()
        callback = config.target.callback.model_copy(update={"file": str(callback_file)})
        target = config.target.model_copy(update={"callback": callback})
        return config.model_copy(update={"target": target})

    def settings_overlay(self, settings: Settings) -> Settings:
        if self.api is None or self.api.url is None:
            return settings
        if str(self.api.url).rstrip("/") != settings.api_url:
            raise ValueError("YAML cannot override the approved direct-Agents origin; configure it explicitly")
        return settings

    def require_platform_origin(self) -> None:
        if self.api is not None and self.api.url is not None:
            raise ValueError("Platform YAML cannot choose an API origin; use MartianConfig or MARTIAN_API_URL")
