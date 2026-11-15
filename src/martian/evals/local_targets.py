"""Local targets: what a run of the local edition calls, defined in the Evals UI with the user's own key.

The hosted console starts a run against an agent from its Agents registry. The local edition
(:mod:`martian.evals.local`) has no registry, so its user defines targets instead, of two kinds:

- ``http``: an HTTP endpoint that takes one JSON ``POST`` per case. ``url``, ``body`` and
  ``response_path`` follow the rules of a suite file's URL target (:class:`martian.evals.runner.TargetRef`).
- ``model``: a model behind an OpenAI-compatible chat completions endpoint, named by ``base_url``
  (the endpoint's ``/v1`` address) and ``model``.

Either may carry the user's key for the endpoint. The data folder keeps them in two files:

- ``targets.json``: the definitions. It never holds a key.
- ``target-keys.json``: each target's key by the target's id. Every write creates a new file with
  mode ``0600`` and renames it over the old one, so only the user who runs ``martian ui`` can read
  it. On Windows the file has the permissions of the data folder.

A key enters with the request that defines its target and leaves only through
:meth:`LocalTargets.key`, for the worker that calls the target. No route answers with it, nothing
logs it, and ``martian.db`` and ``mlflow.db`` never hold it: as the shared model's
:class:`~martian.evals.model.Target`, a local target names its key as ``${MARTIAN_TARGET_KEY}`` in
a header (:meth:`LocalTargets.model_target`), filled in only when the target is called, as a suite
file's ``${NAME}`` is filled from the environment. A target's addresses cannot hold ``${...}``, so
the key only ever goes in that header.

The routes are the local edition's, outside the frozen ``/v1/evals`` contract:

- ``GET /v1/local/targets``: every target, oldest first, each with ``has_key`` in place of its key;
- ``POST /v1/local/targets``: define a target (``201``). A name already in use is ``409``;
- ``DELETE /v1/local/targets/{target_id}``: remove a target and its key (``204``), or ``404``.

A request these routes cannot read is answered ``422`` with the problems as one line of text,
never with the values sent: FastAPI's own answer repeats them, and a target's values include its key.

A launch from the local UI names a local target by its id in the launch request's ``agent_id``,
the frozen contract's one target field, which a target's id fits. The local edition's worker
resolves it with :meth:`LocalTargets.get`: MorningHQ/martian-notes#116.

Route functions have no docstrings: FastAPI would publish one as the operation's description.
"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from martian.evals import runner
from martian.evals.model import Target

DEFINITIONS_FILE = "targets.json"
KEYS_FILE = "target-keys.json"
KEY_REFERENCE = "MARTIAN_TARGET_KEY"
"""The name a local target's key goes by in the shared model's ``Target.headers``."""

DEFAULT_KEY_HEADER = "Authorization"
"""Where an ``http`` target sends its key unless it names another header, and where a ``model``
target always does. In ``Authorization`` the key goes as ``Bearer <key>``; in any other header as it is."""

MODEL_RESPONSE_PATH = "choices.0.message.content"

TargetKind = Literal["http", "model"]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]

_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,100}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_FIELDS: dict[str, frozenset[str]] = {
    "http": frozenset({"url", "body", "response_path", "key_header"}),
    "model": frozenset({"base_url", "model"}),
}


class LocalTargetError(Exception):
    """A target the store refuses. The message names why and never holds a key."""


def _check_address(value: str, field: str) -> None:
    try:
        parts = urlsplit(value)
        _ = parts.port
    except ValueError:
        raise ValueError(f"{field} is not a valid address") from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"{field} must be an http or https address")
    if parts.username is not None or parts.password is not None:
        raise ValueError(f"{field} must not contain credentials; give the key instead")
    if "${" in value:
        raise ValueError(f"{field} cannot hold ${{...}}: a local target's key goes only in its key header")


def _message(item: Any) -> str:
    return str(item["msg"]).removeprefix("Value error, ")


def _problem(item: Any) -> str:
    """One problem of a request, where it is and what is wrong, without the value sent."""
    if item["type"] == "json_invalid":
        return "the request body is not valid JSON"
    where = list(item["loc"])
    if where[:1] == ["body"]:
        where = where[1:]
    return f"{'.'.join(str(part) for part in where)}: {_message(item)}" if where else _message(item)


class TargetDefinition(BaseModel):
    """A local target as its user defines it, without the key."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    kind: TargetKind
    url: str | None = Field(default=None, max_length=2000)
    body: dict[str, Any] | None = None
    response_path: Text | None = None
    key_header: str | None = None
    base_url: str | None = Field(default=None, max_length=2000)
    model: Text | None = None

    @model_validator(mode="before")
    @classmethod
    def default_key_header(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("kind") == "http" and data.get("key_header") is None:
            return {**data, "key_header": DEFAULT_KEY_HEADER}
        return data

    @model_validator(mode="after")
    def fields_for_kind(self) -> TargetDefinition:
        given = {
            name
            for name in ("url", "body", "response_path", "key_header", "base_url", "model")
            if getattr(self, name) is not None
        }
        if extra := sorted(given - _FIELDS[self.kind]):
            raise ValueError(
                f"{', '.join(extra)} does not apply to {'an http' if self.kind == 'http' else 'a model'} target"
            )
        if self.kind == "model":
            if missing := [name for name in ("base_url", "model") if name not in given]:
                raise ValueError(f"a model target needs {' and '.join(missing)}")
            _check_address(str(self.base_url), "base_url")
            return self
        if missing := [name for name in ("url", "body") if name not in given]:
            raise ValueError(f"an http target needs {' and '.join(missing)}")
        _check_address(str(self.url), "url")
        if not _HEADER_NAME.fullmatch(str(self.key_header)):
            raise ValueError("key_header must be a header name, such as Authorization or X-API-Key")
        try:  # the body and response_path rules of a suite file's URL target
            runner.TargetRef(url=self.url, body=self.body, response_path=self.response_path)
        except ValidationError as error:
            raise ValueError("; ".join(_message(item) for item in error.errors(include_input=False))) from None
        return self


class LocalTarget(TargetDefinition):
    """A defined target, as ``targets.json`` keeps it."""

    id: str
    created_at: datetime


class TargetIn(TargetDefinition):
    """``POST /v1/local/targets``: a definition and, optionally, its key."""

    key: SecretStr | None = None

    @field_validator("key")
    @classmethod
    def usable_key(cls, key: SecretStr | None) -> SecretStr | None:
        if key is None:
            return None
        value = key.get_secret_value().strip()
        if not 1 <= len(value) <= 4096 or _CONTROL.search(value):
            raise ValueError("key must be 1 to 4096 characters, without line breaks or other control characters")
        return SecretStr(value)

    def definition(self) -> TargetDefinition:
        return TargetDefinition.model_validate(self.model_dump(exclude={"key"}))


class TargetOut(LocalTarget):
    """A target as the routes answer with it: ``has_key`` says whether a key is stored, never what."""

    has_key: bool


class TargetList(BaseModel):
    items: list[TargetOut]


def _write_private(path: Path, value: object) -> None:
    """Write ``value`` as JSON to a new file that only its owner can read, renamed over ``path``."""
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(value, file, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


class LocalTargets:
    """The targets of the local edition whose data folder is ``home``."""

    def __init__(self, home: Path) -> None:
        self._definitions = home / DEFINITIONS_FILE
        self._keys = home / KEYS_FILE
        self._lock = threading.Lock()

    def _read_targets(self) -> list[LocalTarget]:
        if not self._definitions.is_file():
            return []
        stored = json.loads(self._definitions.read_text(encoding="utf-8"))
        return [LocalTarget.model_validate(item) for item in stored["targets"]]

    def _read_keys(self) -> dict[str, str]:
        if not self._keys.is_file():
            return {}
        keys: dict[str, str] = json.loads(self._keys.read_text(encoding="utf-8"))["keys"]
        return keys

    def defined(self) -> list[LocalTarget]:
        """Every target, oldest first."""
        with self._lock:
            return self._read_targets()

    def get(self, target_id: str) -> LocalTarget | None:
        return next((target for target in self.defined() if target.id == target_id), None)

    def has_key(self, target_id: str) -> bool:
        with self._lock:
            return target_id in self._read_keys()

    def key(self, target_id: str) -> str | None:
        """The target's key, for the worker that calls the target. Never answer a request with it."""
        with self._lock:
            return self._read_keys().get(target_id)

    def define(self, definition: TargetDefinition, key: SecretStr | None = None) -> LocalTarget:
        """Keep a new target, and its key in the key file. A name already in use is refused."""
        with self._lock:
            targets = self._read_targets()
            if any(target.name == definition.name for target in targets):
                raise LocalTargetError(f"A target named {definition.name!r} already exists")
            target = LocalTarget(**definition.model_dump(), id=str(uuid.uuid4()), created_at=datetime.now(UTC))
            if key is not None:
                keys = self._read_keys() | {target.id: key.get_secret_value()}
                _write_private(self._keys, {"version": 1, "keys": keys})
            self._write_targets([*targets, target])
            return target

    def remove(self, target_id: str) -> bool:
        """Remove a target and its key; ``False`` when there is no such target."""
        with self._lock:
            targets = self._read_targets()
            kept = [target for target in targets if target.id != target_id]
            if len(kept) == len(targets):
                return False
            keys = self._read_keys()
            if keys.pop(target_id, None) is not None:
                _write_private(self._keys, {"version": 1, "keys": keys})
            self._write_targets(kept)
            return True

    def _write_targets(self, targets: list[LocalTarget]) -> None:
        _write_private(
            self._definitions, {"version": 1, "targets": [target.model_dump(mode="json") for target in targets]}
        )

    def out(self, target: LocalTarget) -> TargetOut:
        return TargetOut(**target.model_dump(), has_key=self.has_key(target.id))

    def model_target(self, target: LocalTarget) -> Target:
        """The target as the shared model's ``http`` target, which a run records. A key is the
        reference ``${MARTIAN_TARGET_KEY}`` in its header; the worker fills it in from :meth:`key`."""
        if target.kind == "model":
            url = str(target.base_url).rstrip("/") + "/chat/completions"
            body: dict[str, Any] = {"model": target.model, "messages": [{"role": "user", "content": "{{input}}"}]}
            response_path: str | None = MODEL_RESPONSE_PATH
            header = DEFAULT_KEY_HEADER
        else:
            url, body, response_path, header = (
                str(target.url),
                dict(target.body or {}),
                target.response_path,
                str(target.key_header),
            )
        headers = {}
        if self.has_key(target.id):
            reference = f"${{{KEY_REFERENCE}}}"
            headers[header] = f"Bearer {reference}" if header.lower() == "authorization" else reference
        return Target(kind="http", url=url, body=body, response_path=response_path, headers=headers)


class _WithoutValues(APIRoute):
    """Answers a request the route cannot read with ``422`` and the problems as text, without the
    values: FastAPI's own answer repeats each one, and a target's values include its key."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def route(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError as error:
                raise HTTPException(422, "; ".join(_problem(item) for item in error.errors())) from None

        return route


def current_targets(request: Request) -> LocalTargets:
    targets: LocalTargets = request.app.state.martian_local_targets
    return targets


TargetsDep = Annotated[LocalTargets, Depends(current_targets)]

router = APIRouter(prefix="/v1/local/targets", tags=["local targets"], route_class=_WithoutValues)


@router.get("", response_model=TargetList)
def list_targets(targets: TargetsDep) -> TargetList:
    return TargetList(items=[targets.out(target) for target in targets.defined()])


@router.post("", response_model=TargetOut, status_code=201)
def define_target(body: TargetIn, targets: TargetsDep) -> TargetOut:
    try:
        target = targets.define(body.definition(), body.key)
    except LocalTargetError as error:
        raise HTTPException(409, str(error)) from None
    return targets.out(target)


@router.delete("/{target_id}", status_code=204)
def remove_target(target_id: uuid.UUID, targets: TargetsDep) -> None:
    if not targets.remove(str(target_id)):
        raise HTTPException(404, "Target not found")


__all__ = [
    "DEFAULT_KEY_HEADER",
    "DEFINITIONS_FILE",
    "KEYS_FILE",
    "KEY_REFERENCE",
    "LocalTarget",
    "LocalTargetError",
    "LocalTargets",
    "TargetDefinition",
    "TargetIn",
    "TargetList",
    "TargetOut",
    "router",
]
