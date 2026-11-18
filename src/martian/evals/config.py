"""Compatibility adapter for shared authentication, CI attribution and eval policy."""

from __future__ import annotations

import os
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field

from martian._version import __version__
from martian.config import CiContext, MartianConfig, git_sha
from martian.config import ConfigError as ConfigError

KIT_VERSION = __version__


def _float(name: str, default: float | None) -> float | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number between 0 and 1") from None
    if not 0 <= value <= 1:
        raise ConfigError(f"{name} must be between 0 and 1")
    return value


@dataclass(frozen=True)
class KitConfig:
    api_url: str
    token: str = field(repr=False)
    agent_id: str
    offline: bool = False
    dataset_file: str = ""
    threshold: float | None = None
    minimum_coverage: float = 1.0
    git_sha: str = ""
    ci: CiContext | None = field(default=None, repr=False)
    _connection: MartianConfig | None = field(default=None, repr=False, compare=False, init=False)

    def __post_init__(self) -> None:
        if self.threshold is not None and not 0 <= self.threshold <= 1:
            raise ConfigError("threshold must be finite and between 0 and 1")
        if not 0 <= self.minimum_coverage <= 1:
            raise ConfigError("minimum_coverage must be finite and between 0 and 1")
        if self.offline:
            return
        connection = MartianConfig(self.api_url, self.token)
        object.__setattr__(self, "api_url", connection.api_url)
        context = self.ci or CiContext(self.agent_id, self.git_sha or git_sha())
        if str(context.agent_id) != self.agent_id or (self.git_sha and context.git_sha != self.git_sha):
            raise ConfigError("EvalsConfig attribution conflicts with its CI context")
        object.__setattr__(self, "ci", context)

    @property
    def connection(self) -> MartianConfig:
        if self.offline:
            raise ConfigError("offline previews do not have a platform connection")
        return self._connection or MartianConfig(self.api_url, self.token)

    @classmethod
    def from_env(cls) -> KitConfig:
        offline = os.environ.get("MARTIAN_EVALS_OFFLINE", "") == "1"
        if offline:
            return cls(
                "",
                "",
                "",
                offline=True,
                dataset_file=os.environ.get("MARTIAN_EVALS_DATASET_FILE", "").strip(),
                threshold=_float("MARTIAN_EVALS_THRESHOLD", None),
                minimum_coverage=_float("MARTIAN_EVALS_MIN_COVERAGE", 1.0) or 0.0,
            )
        return cls.from_connection(MartianConfig.from_env(), CiContext.from_env())

    @classmethod
    def from_connection(cls, config: MartianConfig, ci: CiContext) -> KitConfig:
        adapted = cls(
            api_url=config.api_url,
            token=config.token.get_secret_value(),
            agent_id=str(ci.agent_id),
            git_sha=ci.git_sha,
            ci=ci,
            threshold=_float("MARTIAN_EVALS_THRESHOLD", None),
            minimum_coverage=_float("MARTIAN_EVALS_MIN_COVERAGE", 1.0) or 0.0,
        )
        object.__setattr__(adapted, "_connection", config)
        return adapted

    def provenance(self) -> dict[str, str]:
        context = self.ci or CiContext.from_env()
        return {
            "git_sha": context.git_sha,
            "ref": context.ref,
            "repo": context.repo,
            "run_url": context.run_url if context.run_url.startswith("https://") else "",
            "kit_version": KIT_VERSION,
        }

    def request_id(self, dataset_name: str, scope: str = "") -> str:
        """Stable per CI attempt, so a re-sent upload replays; random for local runs."""
        run_id = os.environ.get("GITHUB_RUN_ID", "")
        if not run_id:
            return str(uuid.uuid4())
        parts = (
            "martian-evals",
            os.environ.get("GITHUB_REPOSITORY", ""),
            run_id,
            os.environ.get("GITHUB_RUN_ATTEMPT", "1"),
            self.agent_id,
            dataset_name,
            # Matrix legs of one workflow run share run id, attempt, agent and dataset; the job
            # and an optional explicit key keep their uploads distinct instead of conflicting.
            os.environ.get("GITHUB_JOB", ""),
            os.environ.get("MARTIAN_EVALS_RUN_KEY", ""),
            scope,
        )
        return str(uuid.uuid5(uuid.NAMESPACE_URL, ":".join(parts)))


# A public runner can supply configuration without placing its credentials in process globals.
CONFIG: ContextVar[KitConfig | None] = ContextVar("martian_evals_config", default=None)
EvalsConfig = KitConfig


def current_config() -> KitConfig:
    return CONFIG.get() or KitConfig.from_env()


def resolve_ci_config(config: MartianConfig | KitConfig | None, ci: CiContext | None = None) -> KitConfig:
    """Adapt old EvalsConfig once; platform auth and CI attribution stay separate."""
    if isinstance(config, KitConfig):
        if ci is not None and config.ci != ci:
            raise ConfigError("config attribution conflicts with ci=")
        return config
    return KitConfig.from_connection(config or MartianConfig.from_env(), ci or CiContext.from_env())
