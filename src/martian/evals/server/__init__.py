"""The evals core: the ``/v1/evals`` API over an injected store, principal and entitlements.

The same core serves every edition. An edition builds it with ``create_app`` and supplies three
things the core never imports:

- ``store``: an :class:`~martian.evals.store.EvalStore`;
- ``principal``: a FastAPI dependency returning the :class:`Principal` of a request, such as
  :data:`SINGLE_USER` for the local edition, or the server's session lookup;
- ``entitlements``: a function from a principal to its :class:`Entitlements`.

Routes read them through :func:`current_principal`, :func:`current_entitlements` and
:func:`current_store`. ``GET /v1/evals/status`` is the first route, in the shape of the frozen
evals API contract; the rest are ported from the server in MorningHQ/martian-notes#100 and #101.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Request

from martian.evals.server.entitlements import COMMUNITY, Edition, Entitlements
from martian.evals.server.principal import SINGLE_USER, Principal, organisation_principal
from martian.evals.store import EvalStore, StoreStatus

PrincipalProvider = Callable[..., Principal | Awaitable[Principal]]
EntitlementsProvider = Callable[[Principal], Entitlements]


def current_store(request: Request) -> EvalStore:
    store: EvalStore = request.app.state.martian_evals_store
    return store


def current_principal() -> Principal:
    """Replaced by the edition's principal provider in :func:`create_app`."""
    raise RuntimeError("create_app binds the principal provider")


def current_entitlements(request: Request, principal: Annotated[Principal, Depends(current_principal)]) -> Entitlements:
    provider: EntitlementsProvider = request.app.state.martian_evals_entitlements
    return provider(principal)


router = APIRouter(prefix="/v1/evals", tags=["evals"])


@router.get("/status", response_model=StoreStatus)
async def status(
    principal: Annotated[Principal, Depends(current_principal)],
    store: Annotated[EvalStore, Depends(current_store)],
) -> StoreStatus:
    return await store.status(principal)


def create_app(
    *,
    store: EvalStore,
    principal: PrincipalProvider,
    entitlements: EntitlementsProvider = lambda _principal: COMMUNITY,
    **fastapi_options: Any,
) -> FastAPI:
    """The evals core as an application. ``principal`` is a FastAPI dependency, so it can read the
    request (a session cookie, a header) through its own parameters."""
    if not isinstance(store, EvalStore):
        raise TypeError("store must implement martian.evals.store.EvalStore")
    app = FastAPI(title="Martian Evals", **fastapi_options)
    app.state.martian_evals_store = store
    app.state.martian_evals_entitlements = entitlements
    app.dependency_overrides[current_principal] = principal
    app.include_router(router)
    return app


__all__ = [
    "COMMUNITY",
    "SINGLE_USER",
    "Edition",
    "Entitlements",
    "Principal",
    "create_app",
    "current_entitlements",
    "current_principal",
    "current_store",
    "organisation_principal",
    "router",
]
