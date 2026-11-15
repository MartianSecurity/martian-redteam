"""The local edition: the evals core and the Evals UI, served by one process on this machine.

``martian ui`` starts it. It has one fixed user (:data:`~martian.evals.server.SINGLE_USER`), no
sign-in and the Community edition. It keeps everything in one data folder, ``~/.martian/evals``
unless the caller names another:

- ``mlflow.db``: the core's store, :class:`~martian.evals.store.mlflow_local.MlflowStore` on SQLite;
- ``artifacts/``: that store's run artifacts.
- ``martian.db``: the core's records, :class:`~martian.evals.records.sql.SqlRecordStore` on SQLite:
  runs, case results, scores, scoring jobs, reviews, suites and dataset drafts.
- ``targets.json`` and ``target-keys.json``: the targets the user defines in the UI, in place of
  the hosted console's Agents registry, and their keys, in a file only the user can read
  (:mod:`martian.evals.local_targets`).
- ``settings.json``: the local settings, :class:`~martian.evals.judge_settings.FileJudgeSettings`:
  the default judge model, which an organisation sets on the server. Written when it is first set.
- ``locks/``: an empty file per process working on a run, which that process holds an OS lock on
  (:class:`~martian.evals.local_runs.RunLock`), so that another process can tell it is alive.

``martian evals run`` records each suite-file run in the same folder
(:mod:`martian.evals.run_record`), so the UI shows it. Both create the default project when they
open the folder, so that an application can then record traces into the project's experiment with
MLflow tracing (``docs/evals-core.md`` gives the settings).

Runs started in the UI run in processes of their own, with the edition's worker
(:class:`~martian.evals.local_runs.LocalWorker`), against the targets the user defines, and their
suites' model judges run on the SDK judge, on the default judge model in ``settings.json`` or the
judge's own. When it starts, the server finishes as interrupted each queued or running run that no
process is working on any more (:func:`~martian.evals.local_runs.finish_interrupted`).

The API is under ``/v1``: the core's ``/v1/evals`` routes, its OpenTelemetry ``POST /v1/traces``,
which takes the fixed principal's traces without credentials, the client's session call,
``POST /v1/auth/refresh``, which answers with the fixed principal, the local targets under
``/v1/local/targets`` and the local launch and run again under ``/v1/local/runs``
(:mod:`martian.evals.local_runs`). Every other path is a file of the built Evals UI, by default
the one the wheel ships in ``martian/ui``. The server listens on
127.0.0.1 and refuses a request whose ``Host`` header names another host, so a web page cannot
reach it through a name that resolves to this machine. The container image (``Dockerfile``) sets
:data:`BIND_ENV` so that the server listens on the container's interfaces; the ``Host`` check
stays on there, with any names :data:`ALLOWED_HOSTS_ENV` adds.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import socket
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI
from pydantic import BaseModel, ConfigDict
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.staticfiles import StaticFiles

from martian.evals import local_runs, local_targets
from martian.evals.judge_settings import FileJudgeSettings
from martian.evals.model import Project
from martian.evals.records.sql import SqlRecordStore
from martian.evals.server import DEFAULT_PROJECT, SINGLE_USER, Principal, create_app, current_principal
from martian.evals.server.bootstrap import BootstrapPrincipal, principal_out
from martian.evals.store import StoreError
from martian.evals.store.mlflow_local import MlflowStore

HOST = "127.0.0.1"
"""The address the local server listens on unless :data:`BIND_ENV` names another."""

ALLOWED_HOSTS = ("127.0.0.1", "localhost")
"""The ``Host`` headers the local server answers, with any names :data:`ALLOWED_HOSTS_ENV` adds."""

BIND_ENV = "MARTIAN_EVALS_BIND"
"""The container image's setting: the IPv4 address to listen on. The image sets ``0.0.0.0``, the
container's interfaces, so that Docker can publish the port. Unset, the address is :data:`HOST`."""

ALLOWED_HOSTS_ENV = "MARTIAN_EVALS_ALLOWED_HOSTS"
"""The container image's setting: comma-separated host names to answer besides
:data:`ALLOWED_HOSTS`, for a server its users reach by name. Unset, there are none."""

EXPOSURE_WARNING = (
    "WARNING: Martian Evals has no login. Anyone who can reach this port can read and change "
    "everything in it. Do not expose it publicly: publish it on 127.0.0.1 only "
    "(docker run -p 127.0.0.1:7600:7600 ...) and reach it from another machine through an SSH tunnel."
)
"""What ``martian ui`` prints when it listens on an address other than loopback."""

_HOST_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*")

ACCESS_TOKEN = "local"
"""What the session call issues. Nothing checks it: the local edition has one user and no sign-in."""

SESSION_ID = "local"

SETTINGS_FILE = "settings.json"
"""The data folder's local settings: the default judge model."""


class LocalEditionError(Exception):
    """The local edition cannot start. The message says why."""


def default_home() -> Path:
    """The local edition's data folder when none is named: ``~/.martian/evals``."""
    return Path.home() / ".martian" / "evals"


def data_folder(home: str | Path | None = None) -> Path:
    """The data folder: ``home`` when one is named, else ``MARTIAN_EVALS_HOME``, else
    :func:`default_home`."""
    return Path(home or os.environ.get("MARTIAN_EVALS_HOME") or default_home()).expanduser().resolve()


def open_stores(home: Path) -> tuple[MlflowStore, SqlRecordStore]:
    """The store and the record store in the data folder ``home``, created on first use, with the
    default project. ``martian ui`` serves them, and ``martian evals run`` records its runs in them.

    The default project is created here, before an application can record traces into its
    experiment with MLflow tracing: MLflow would otherwise create the experiment itself, with its
    artifacts outside the data folder, and the store does not take such an experiment as the
    project. A data folder holding one is refused with :class:`LocalEditionError`."""
    # The local edition sends nothing off this machine.
    os.environ.setdefault("MLFLOW_DISABLE_TELEMETRY", "true")
    home.mkdir(parents=True, exist_ok=True)
    store = MlflowStore(f"sqlite:///{home / 'mlflow.db'}", home / "artifacts")
    records = SqlRecordStore(f"sqlite:///{home / 'martian.db'}")
    default = Project(id=DEFAULT_PROJECT, name="Default", description="Organization evaluation project")
    try:
        asyncio.run(store.create_project(SINGLE_USER, default))
    except StoreError as error:
        records.close()
        raise LocalEditionError(f"the data folder {home} cannot hold the default project: {error.detail}") from None
    return store, records


def listen_address(value: str | None) -> str:
    """The address to listen on: the IPv4 address in ``value``, the value of :data:`BIND_ENV`, or
    :data:`HOST` when ``value`` is unset or empty."""
    if not value:
        return HOST
    try:
        return str(ipaddress.IPv4Address(value.strip()))
    except ValueError:
        raise LocalEditionError(f"{BIND_ENV} must be an IPv4 address, such as 0.0.0.0: {value!r}") from None


def host_names(value: str | None) -> tuple[str, ...]:
    """:data:`ALLOWED_HOSTS` and the comma-separated host names in ``value``, the value of
    :data:`ALLOWED_HOSTS_ENV`. A name with a port, a scheme or a wildcard is refused: the check
    compares the name alone, and a wildcard would switch it off."""
    names = [name.strip().lower() for name in (value or "").split(",") if name.strip()]
    for name in names:
        if not _HOST_NAME.fullmatch(name):
            raise LocalEditionError(
                f"{ALLOWED_HOSTS_ENV} takes host names without a port, scheme or wildcard: {name!r}"
            )
    return tuple(dict.fromkeys((*ALLOWED_HOSTS, *names)))


def packaged_ui() -> Path:
    """Where the package keeps the built Evals UI: the ``ui`` folder of the ``martian`` package, a
    build of the frontend's Evals app that the wheel ships, named in its ``ui-version.json``."""
    return Path(__file__).resolve().parents[1] / "ui"


class LocalSession(BootstrapPrincipal):
    """The bootstrap's principal, as the session the client keeps."""

    session_id: str


class LocalToken(BaseModel):
    """The answer to the client's session call: what ``refresh()`` in the UI's API client reads."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    access_token: str
    session: LocalSession


session_router = APIRouter(prefix="/v1/auth", tags=["session"])


@session_router.post("/refresh", response_model=LocalToken)
async def refresh(principal: Annotated[Principal, Depends(current_principal)]) -> LocalToken:
    """The client's session call. There is no sign-in, so it answers with the edition's principal."""
    session = LocalSession.model_validate({**principal_out(principal).model_dump(), "session_id": SESSION_ID})
    return LocalToken(access_token=ACCESS_TOKEN, session=session)


def create_local_app(home: Path, ui: Path, hosts: Sequence[str] = ALLOWED_HOSTS) -> FastAPI:
    """The local edition as an application: the core on SQLite in ``home`` for the single local
    user, with a worker that runs each run in a process of its own, the session call, the local
    targets in ``home`` and the local runs, and the built UI in ``ui``, a folder holding
    ``index.html``. It answers a request only when its ``Host`` header names one of ``hosts``.
    Runs of ``home`` that no process is working on any more are finished as interrupted first."""
    if not (ui / "index.html").is_file():
        raise LocalEditionError(f"there is no built Evals UI in {ui}: index.html is missing")
    store, records = open_stores(home)
    asyncio.run(local_runs.finish_interrupted(home, store, records))
    targets = local_targets.LocalTargets(home)
    judge_settings = FileJudgeSettings(home / SETTINGS_FILE)
    worker = local_runs.LocalWorker(home, records, targets, judge_settings)
    app = create_app(
        store=store,
        records=records,
        principal=lambda: SINGLE_USER,
        worker=worker,
        judge_settings=judge_settings,
    )
    app.state.martian_local_targets = targets
    app.include_router(session_router)
    app.include_router(local_targets.router)
    app.include_router(local_runs.router)
    app.mount("/", StaticFiles(directory=ui, html=True), name="ui")
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(hosts))
    return app


def listen(port: int, host: str = HOST) -> socket.socket:
    """A socket listening on ``host``:``port``. A port another process holds is refused."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name != "nt":  # on Windows this option would let a second server share the port
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((host, port))
        listener.listen(128)
    except OSError as error:
        listener.close()
        raise LocalEditionError(f"cannot listen on {host}:{port}: {error.strerror or error}") from None
    return listener


def serve(app: FastAPI, *, port: int, open_browser: bool, announce: Callable[[str], None], host: str = HOST) -> None:
    """Serve ``app`` on ``host``:``port`` until interrupted. Once the port is listening, pass the
    UI's address to ``announce`` and, with ``open_browser``, open it in the default browser."""
    import threading
    import webbrowser

    import uvicorn

    listener = listen(port, host)
    url = f"http://{host}:{port}/"
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))
    announce(url)
    if open_browser:
        # The browser's first request waits in the listening socket until the server accepts it.
        # The browser gets a thread of its own: a browser command (``BROWSER``) may run until it
        # is closed, and the server must answer it meanwhile.
        threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
    server.run(sockets=[listener])
