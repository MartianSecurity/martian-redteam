"""The evals core: the ``/v1/evals`` API over an injected store, principal and entitlements.

The same core serves every edition. An edition builds it with ``create_app`` and supplies three
things the core never imports:

- ``store``: an :class:`~martian.evals.store.EvalStore`;
- ``principal``: a FastAPI dependency returning the :class:`Principal` of a request, such as
  :data:`SINGLE_USER` for the local edition, or the server's session lookup;
- ``entitlements``: a function from a principal to its :class:`Entitlements`, such as
  ``lambda p: entitlements_for(resolve_edition(plan=...))``. The default is Community.

Routes read them through :func:`current_principal`, :func:`current_entitlements` and
:func:`current_store`, and a route that serves a paid feature declares
``dependencies=[Depends(require_entitlement(key))]``. ``GET /v1/evals/status`` is the first
route, in the shape of the frozen evals API contract; the rest are ported from the server in
MorningHQ/martian-notes#100 and #101.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request

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


def ensure_entitled(entitlements: Entitlements, feature: str) -> None:
    """Refuse with ``403`` when the edition leaves ``feature`` out. The detail names the edition and
    the feature, for example "The Community edition does not include review queues". Use it inside a
    route when only part of a request needs the feature."""
    if not entitlements.allows(feature):
        edition = EDITION_NAMES[entitlements.edition]
        raise HTTPException(403, f"The {edition} edition does not include {FEATURES[feature]}")


def require_entitlement(feature: str) -> Callable[[Entitlements], None]:
    """A route dependency for :func:`ensure_entitled`. It runs after the principal provider, so
    sign-in is checked first, and it adds nothing to the route's OpenAPI operation."""
    if feature not in FEATURES:
        raise ValueError(f"unknown feature key {feature!r}")

    def check(entitlements: Annotated[Entitlements, Depends(current_entitlements)]) -> None:
        ensure_entitled(entitlements, feature)

    return check


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
    "EDITIONS",
    "EDITION_NAMES",
    "ENTERPRISE",
    "FEATURES",
    "LIMITS",
    "ON_PREMISES",
    "SINGLE_USER",
    "Edition",
    "Entitlements",
    "Plan",
    "Principal",
    "create_app",
    "current_entitlements",
    "current_principal",
    "current_store",
    "ensure_entitled",
    "entitlements_for",
    "organisation_principal",
    "require_entitlement",
    "resolve_edition",
    "router",
]
