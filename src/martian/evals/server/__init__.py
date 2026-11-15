"""The evals core: the ``/v1/evals`` API over an injected store, record store, principal,
entitlements and worker.

The same core serves every edition. An edition builds it with ``create_app`` and supplies the
things the core never imports:

- ``store``: an :class:`~martian.evals.store.EvalStore`;
- ``records``: a :class:`~martian.evals.records.RecordStore`, for runs and their results;
- ``principal``: a FastAPI dependency returning the :class:`Principal` of a request, such as
  :data:`SINGLE_USER` for the local edition, or the server's session lookup;
- ``entitlements``: a function from a principal to its :class:`Entitlements`, such as
  ``lambda p: entitlements_for(resolve_edition(plan=...))``. The default is Community.
- ``worker``: a :class:`~martian.evals.worker.Worker` that runs what the write routes start, such
  as :class:`~martian.evals.worker.InProcessWorker`. Without one, a route that starts a run or a
  scoring job answers ``503``. A worker that runs beside the app (``Lifecycle``, such as a
  :class:`~martian.evals.worker.QueueWorker` serving in this process) is started when the app
  starts serving and stopped when it stops.
- ``judge_settings``: :class:`~martian.evals.judge_settings.JudgeSettings`, where each
  organisation's default judge model is kept, such as the local edition's
  :class:`~martian.evals.judge_settings.FileJudgeSettings`. Without it there is no default, and
  setting one answers ``503``.

Routes read them through :func:`current_principal`, :func:`current_entitlements`,
:func:`current_store`, :func:`current_records` and ``current_worker``. A route that serves a paid
feature declares ``dependencies=[Depends(require_entitlement(key))]``, and a write route declares
the capability it needs with ``require_capability(name)``. Every route is in the shape of the
frozen evals API contract: status and bootstrap here, the read routes in ``runs``, ``traces`` and
``datasets``, and the write routes in ``launch``, ``scoring``, ``reviews``, ``suites``,
``projects``, ``views`` and ``drafts``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from martian.evals.judge_settings import JudgeSettings
from martian.evals.records import RecordStore
from martian.evals.server import datasets, drafts, launch, projects, reviews, runs, scoring, suites, traces, views
from martian.evals.server.bootstrap import Bootstrap, build_bootstrap
from martian.evals.server.capabilities import CAPABILITIES, Capability, capabilities_for, highest_role
from martian.evals.server.dependencies import (
    EntitlementsProvider,
    PrincipalProvider,
    current_entitlements,
    current_judge_settings,
    current_principal,
    current_records,
    current_store,
    current_worker,
    ensure_entitled,
    require_capability,
    require_entitlement,
)
from martian.evals.server.entitlements import (
    COMMUNITY,
    EDITION_NAMES,
    EDITIONS,
    ENTERPRISE,
    FEATURES,
    LIMITS,
    ON_PREMISES,
    Edition,
    Entitlements,
    Plan,
    entitlements_for,
    resolve_edition,
)
from martian.evals.server.principal import SINGLE_USER, Principal, organisation_principal
from martian.evals.server.projection import DEFAULT_PROJECT, NO_AGENT, result_id, score_id
from martian.evals.store import EvalStore, StoreError, StoreStatus
from martian.evals.worker import Lifecycle, Worker

STORE_ERROR_STATUS = {"not_found": 404, "conflict": 409, "invalid": 422}

router = APIRouter(prefix="/v1/evals", tags=["evals"])


@router.get("/status", response_model=StoreStatus)
async def status(
    principal: Annotated[Principal, Depends(current_principal)],
    store: Annotated[EvalStore, Depends(current_store)],
) -> StoreStatus:
    return await store.status(principal)


@router.get("/bootstrap", response_model=Bootstrap)
async def bootstrap(
    principal: Annotated[Principal, Depends(current_principal)],
    entitlements: Annotated[Entitlements, Depends(current_entitlements)],
    store: Annotated[EvalStore, Depends(current_store)],
) -> Bootstrap:
    """What the UI reads at start: the principal, the edition and its feature keys, the
    capabilities the role and edition allow, and the store's readiness."""
    return build_bootstrap(principal, entitlements, await store.status(principal))


async def _store_error(_request: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, StoreError)
    return JSONResponse({"detail": error.detail}, status_code=STORE_ERROR_STATUS[error.kind])


def _with_worker(worker: Lifecycle, inner: Any) -> Callable[[FastAPI], AbstractAsyncContextManager[Any]]:
    """A lifespan that starts the worker when the app starts serving and stops it when the app
    stops, around the edition's own lifespan, if it gave one."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[Any]:
        await worker.start()
        try:
            if inner is None:
                yield None
            else:
                async with inner(app) as state:
                    yield state
        finally:
            await worker.stop()

    return lifespan


def create_app(
    *,
    store: EvalStore,
    records: RecordStore,
    principal: PrincipalProvider,
    entitlements: EntitlementsProvider = lambda _principal: COMMUNITY,
    worker: Worker | None = None,
    judge_settings: JudgeSettings | None = None,
    **fastapi_options: Any,
) -> FastAPI:
    """The evals core as an application. ``principal`` is a FastAPI dependency, so it can read the
    request (a session cookie, a header) through its own parameters. A request the store or the
    record store refuses is answered ``404``, ``409`` or ``422`` by the ``StoreError``'s kind."""
    if not isinstance(store, EvalStore):
        raise TypeError("store must implement martian.evals.store.EvalStore")
    if not isinstance(records, RecordStore):
        raise TypeError("records must implement martian.evals.records.RecordStore")
    if worker is not None and not isinstance(worker, Worker):
        raise TypeError("worker must implement martian.evals.worker.Worker")
    if judge_settings is not None and not isinstance(judge_settings, JudgeSettings):
        raise TypeError("judge_settings must implement martian.evals.judge_settings.JudgeSettings")
    if isinstance(worker, Lifecycle):
        fastapi_options["lifespan"] = _with_worker(worker, fastapi_options.get("lifespan"))
    app = FastAPI(title="Martian Evals", **fastapi_options)
    app.state.martian_evals_store = store
    app.state.martian_evals_records = records
    app.state.martian_evals_entitlements = entitlements
    app.state.martian_evals_worker = worker
    app.state.martian_evals_judge_settings = judge_settings
    app.dependency_overrides[current_principal] = principal
    app.add_exception_handler(StoreError, _store_error)
    for part in (
        router,
        datasets.router,
        runs.explorer,
        traces.router,
        launch.router,
        scoring.router,
        reviews.router,
        reviews.explorer,
        suites.router,
        suites.explorer,
        projects.router,
        views.router,
        drafts.router,
        runs.router,
    ):
        app.include_router(part)
    return app


__all__ = [
    "CAPABILITIES",
    "COMMUNITY",
    "DEFAULT_PROJECT",
    "EDITIONS",
    "EDITION_NAMES",
    "ENTERPRISE",
    "FEATURES",
    "LIMITS",
    "NO_AGENT",
    "ON_PREMISES",
    "SINGLE_USER",
    "Bootstrap",
    "Capability",
    "Edition",
    "Entitlements",
    "Plan",
    "Principal",
    "build_bootstrap",
    "capabilities_for",
    "create_app",
    "current_entitlements",
    "current_judge_settings",
    "current_principal",
    "current_records",
    "current_store",
    "current_worker",
    "ensure_entitled",
    "entitlements_for",
    "highest_role",
    "organisation_principal",
    "require_capability",
    "require_entitlement",
    "resolve_edition",
    "result_id",
    "router",
    "score_id",
]
