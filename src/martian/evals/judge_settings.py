"""The default judge model: the model a judge uses when it names none of its own.

A judge names its model as an MLflow model URI (``openai:/<model>``) and, optionally, the base
address of the OpenAI-compatible endpoint that serves it. A judge that names no model uses the
default, which an organisation sets on the server. The local edition has no organisation, so its
default is a local setting in its data folder (:class:`FileJudgeSettings`). With no default either,
the worker's judge uses its own.

The core reads and writes the default through :class:`JudgeSettings`, which the edition supplies to
``create_app``. Neither a judge nor the default ever holds a key: the judge reads the provider's key
from its environment. An endpoint is a plain ``http`` or ``https`` address, so no key can travel in
it either: one with a user name, a password, a query or a fragment is refused.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

if TYPE_CHECKING:
    from martian.evals.server.principal import Principal

MODEL_URI_PATTERN = r"^[a-z][a-z0-9_-]{0,39}:/\S{1,200}$"
"""An MLflow model URI, ``<provider>:/<model>``, such as ``openai:/gpt-4o-mini``."""

ENDPOINT_PATTERN = r"^https?://[^\s/?#@]+(/[^\s?#]*[^\s?#/])?$"
"""An endpoint's base address: ``http`` or ``https``, a host, and a path with no trailing ``/``.
No user name or password, query or fragment, so no key travels in it."""

ENDPOINT_PROVIDER = "openai"
"""The provider an endpoint serves: the judge calls it as an OpenAI-compatible endpoint."""


def judge_model_problem(model: str | None, endpoint: str | None) -> str | None:
    """Why a model and endpoint cannot go together, or None when they can."""
    if endpoint is not None and model is None:
        return "An endpoint needs a model: name the model it serves"
    if endpoint is not None and model is not None and model.split(":/", 1)[0] != ENDPOINT_PROVIDER:
        return f"An endpoint serves {ENDPOINT_PROVIDER}:/ models: the judge calls it as an OpenAI-compatible endpoint"
    return None


class JudgeModel(BaseModel):
    """A judge model and the endpoint that serves it. Both empty: no model is named."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str | None = Field(default=None, max_length=240, pattern=MODEL_URI_PATTERN)
    endpoint: str | None = Field(default=None, max_length=500, pattern=ENDPOINT_PATTERN)

    @model_validator(mode="after")
    def consistent(self) -> JudgeModel:
        if problem := judge_model_problem(self.model, self.endpoint):
            raise ValueError(problem)
        return self


NO_MODEL = JudgeModel()


@runtime_checkable
class JudgeSettings(Protocol):
    """Where an edition keeps each organisation's default judge model."""

    async def default_model(self, principal: Principal) -> JudgeModel:
        """The principal's organisation's default, or :data:`NO_MODEL` when none is set."""
        ...

    async def set_default_model(self, principal: Principal, value: JudgeModel) -> JudgeModel:
        """Set the default for the principal's organisation; :data:`NO_MODEL` clears it."""
        ...


def _space(principal: Principal) -> str:
    return principal.organisation_id or "local"


class MemoryJudgeSettings:
    """Each organisation's default in memory, and one for the single local user. For tests and for
    an edition that keeps its settings elsewhere."""

    def __init__(self) -> None:
        self._defaults: dict[str, JudgeModel] = {}

    async def default_model(self, principal: Principal) -> JudgeModel:
        return self._defaults.get(_space(principal), NO_MODEL)

    async def set_default_model(self, principal: Principal, value: JudgeModel) -> JudgeModel:
        self._defaults[_space(principal)] = value
        return value


class FileJudgeSettings:
    """The local edition's setting: the single local user's default, in a JSON file of the data
    folder (``settings.json``, under ``judge_model``). A principal of an organisation is refused,
    since the file belongs to one user. Other keys of the file are kept as they are."""

    KEY = "judge_model"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = asyncio.Lock()

    @staticmethod
    def _check(principal: Principal) -> None:
        if principal.organisation_id is not None:
            raise ValueError("the local judge setting belongs to the single local user")

    def _read(self) -> dict[str, object]:
        try:
            found = json.loads(self.path.read_text("utf-8"))
        except FileNotFoundError:
            return {}
        if not isinstance(found, dict):
            raise ValueError(f"{self.path} is not a JSON object")
        return found

    async def default_model(self, principal: Principal) -> JudgeModel:
        self._check(principal)
        stored = self._read().get(self.KEY)
        if stored is None:
            return NO_MODEL
        try:
            return JudgeModel.model_validate(stored)
        except ValidationError as error:
            raise ValueError(f"{self.path} holds a judge model that cannot be used: {error}") from None

    async def set_default_model(self, principal: Principal, value: JudgeModel) -> JudgeModel:
        self._check(principal)
        async with self._lock:
            settings = self._read()
            if value == NO_MODEL:
                settings.pop(self.KEY, None)
            else:
                settings[self.KEY] = value.model_dump(mode="json")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Write a whole new file, so a reader never sees half of one.
            handle, name = tempfile.mkstemp(dir=self.path.parent, prefix=".settings-", suffix=".json")
            with os.fdopen(handle, "w", encoding="utf-8") as file:
                json.dump(settings, file, indent=2, sort_keys=True)
                file.write("\n")
            os.replace(name, self.path)
        return value


__all__ = [
    "ENDPOINT_PATTERN",
    "ENDPOINT_PROVIDER",
    "MODEL_URI_PATTERN",
    "NO_MODEL",
    "FileJudgeSettings",
    "JudgeModel",
    "JudgeSettings",
    "MemoryJudgeSettings",
    "judge_model_problem",
]
