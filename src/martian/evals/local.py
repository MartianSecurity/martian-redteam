"""The local edition: the evals core and the Evals UI, served by one process on this machine.

``martian ui`` starts it. It has one fixed user (:data:`~martian.evals.server.SINGLE_USER`), no
sign-in and the Community edition. It keeps everything in one data folder, ``~/.martian/evals``
unless the caller names another:

- ``mlflow.db``: the core's store, :class:`~martian.evals.store.mlflow_local.MlflowStore` on SQLite;
- ``artifacts/``: that store's run artifacts.
- ``martian.db``: the core's records, :class:`~martian.evals.records.sql.SqlRecordStore` on SQLite:
  runs, case results, scores, scoring jobs, reviews, suites and dataset drafts.

The API is under ``/v1``: the core's ``/v1/evals`` routes and the client's session call,
``POST /v1/auth/refresh``, which answers with the fixed principal. Every other path is a file of
the built Evals UI, by default the one the wheel ships in ``martian/ui``. The server listens on
127.0.0.1 only and refuses a request whose ``Host`` header names another host, so a web page
cannot reach it through a name that resolves to this machine.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI
from pydantic import BaseModel, ConfigDict
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.staticfiles import StaticFiles

from martian.evals.server import SINGLE_USER, Principal, create_app, current_principal
from martian.evals.server.bootstrap import BootstrapPrincipal, principal_out
from martian.evals.records.sql import SqlRecordStore
from martian.evals.store.mlflow_local import MlflowStore

HOST = "127.0.0.1"
"""The only address the local server listens on."""

ALLOWED_HOSTS = ("127.0.0.1", "localhost")
"""The ``Host`` headers the local server answers."""

ACCESS_TOKEN = "local"
"""What the session call issues. Nothing checks it: the local edition has one user and no sign-in."""

SESSION_ID = "local"


class LocalEditionError(Exception):
    """The local edition cannot start. The message says why."""


def default_home() -> Path:
    """The local edition's data folder when none is named: ``~/.martian/evals``."""
    return Path.home() / ".martian" / "evals"


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


def create_local_app(home: Path, ui: Path) -> FastAPI:
    """The local edition as an application: the core on SQLite in ``home`` for the single local
    user, the session call, and the built UI in ``ui``, a folder holding ``index.html``."""
    if not (ui / "index.html").is_file():
        raise LocalEditionError(f"there is no built Evals UI in {ui}: index.html is missing")
    # The local edition sends nothing off this machine.
    os.environ.setdefault("MLFLOW_DISABLE_TELEMETRY", "true")
    home.mkdir(parents=True, exist_ok=True)
    store = MlflowStore(f"sqlite:///{home / 'mlflow.db'}", home / "artifacts")
    records = SqlRecordStore(f"sqlite:///{home / 'martian.db'}")
    app = create_app(store=store, records=records, principal=lambda: SINGLE_USER)
    app.include_router(session_router)
    app.mount("/", StaticFiles(directory=ui, html=True), name="ui")
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(ALLOWED_HOSTS))
    return app


def listen(port: int) -> socket.socket:
    """A socket listening on 127.0.0.1:``port``. A port another process holds is refused."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name != "nt":  # on Windows this option would let a second server share the port
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((HOST, port))
        listener.listen(128)
    except OSError as error:
        listener.close()
        raise LocalEditionError(f"cannot listen on {HOST}:{port}: {error.strerror or error}") from None
    return listener


def serve(app: FastAPI, *, port: int, open_browser: bool, announce: Callable[[str], None]) -> None:
    """Serve ``app`` on 127.0.0.1:``port`` until interrupted. Once the port is listening, pass the
    UI's address to ``announce`` and, with ``open_browser``, open it in the default browser."""
    import threading
    import webbrowser

    import uvicorn

    listener = listen(port)
    url = f"http://{HOST}:{port}/"
    server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=port, log_level="warning"))
    announce(url)
    if open_browser:
        # The browser's first request waits in the listening socket until the server accepts it.
        # The browser gets a thread of its own: a browser command (``BROWSER``) may run until it
        # is closed, and the server must answer it meanwhile.
        threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
    server.run(sockets=[listener])
