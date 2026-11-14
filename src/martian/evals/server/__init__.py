"""The evals core: the ``/v1/evals`` API over an injected store, record store, principal and
entitlements.

The same core serves every edition. An edition builds it with ``create_app`` and supplies the
things the core never imports:

- ``store``: an :class:`~martian.evals.store.EvalStore`;
- ``records``: a :class:`~martian.evals.records.RecordStore`, for runs and their results;
- ``principal``: a FastAPI dependency returning the :class:`Principal` of a request, such as
  :data:`SINGLE_USER` for the local edition, or the server's session lookup;
- ``entitlements``: a function from a principal to its :class:`Entitlements`, such as
  ``lambda p: entitlements_for(resolve_edition(plan=...))``. The default is Community.

Routes read them through :func:`current_principal`, :func:`current_entitlements`,
:func:`current_store` and :func:`current_records`, and a route that serves a paid feature declares
``dependencies=[Depends(require_entitlement(key))]``. Every route is in the shape of the frozen
evals API contract: status and bootstrap here, the read routes in ``runs``, ``traces`` and
``datasets``. The write routes are ported from the server in MorningHQ/martian-notes#101.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from martian.evals.records import RecordStore
from martian.evals.server import datasets, runs, traces
from martian.evals.server.bootstrap import Bootstrap, build_bootstrap
from martian.evals.server.capabilities import CAPABILITIES, Capability, capabilities_for, highest_role
from martian.evals.server.dependencies import (
    EntitlementsProvider,
    PrincipalProvider,
    current_entitlements,
    current_principal,
    current_records,
    current_store,
    ensure_entitled,
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


def create_app(
    *,
    store: EvalStore,
    records: RecordStore,
    principal: PrincipalProvider,
    entitlements: EntitlementsProvider = lambda _principal: COMMUNITY,
    **fastapi_options: Any,
) -> FastAPI:
    """The evals core as an application. ``principal`` is a FastAPI dependency, so it can read the
    request (a session cookie, a header) through its own parameters. A request the store or the
    record store refuses is answered ``404``, ``409`` or ``422`` by the ``StoreError``'s kind."""
    if not isinstance(store, EvalStore):
        raise TypeError("store must implement martian.evals.store.EvalStore")
    if not isinstance(records, RecordStore):
        raise TypeError("records must implement martian.evals.records.RecordStore")
    app = FastAPI(title="Martian Evals", **fastapi_options)
    app.state.martian_evals_store = store
    app.state.martian_evals_records = records
    app.state.martian_evals_entitlements = entitlements
    app.dependency_overrides[current_principal] = principal
    app.add_exception_handler(StoreError, _store_error)
    for part in (router, datasets.router, runs.explorer, traces.router, runs.router):
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
    "current_principal",
    "current_records",
    "current_store",
    "ensure_entitled",
    "entitlements_for",
    "highest_role",
    "organisation_principal",
    "require_entitlement",
    "resolve_edition",
    "result_id",
    "router",
    "score_id",
]
