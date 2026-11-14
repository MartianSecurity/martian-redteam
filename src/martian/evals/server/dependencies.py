"""What a route of the evals core reads from its request: the edition's principal, entitlements,
store and record store, bound by :func:`martian.evals.server.create_app`."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, HTTPException, Request

from martian.evals.records import RecordStore
from martian.evals.server.entitlements import EDITION_NAMES, FEATURES, Entitlements
from martian.evals.server.principal import Principal
from martian.evals.store import EvalStore

PrincipalProvider = Callable[..., Principal | Awaitable[Principal]]
EntitlementsProvider = Callable[[Principal], Entitlements]


def current_store(request: Request) -> EvalStore:
    store: EvalStore = request.app.state.martian_evals_store
    return store


def current_records(request: Request) -> RecordStore:
    records: RecordStore = request.app.state.martian_evals_records
    return records


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


PrincipalDep = Annotated[Principal, Depends(current_principal)]
StoreDep = Annotated[EvalStore, Depends(current_store)]
RecordsDep = Annotated[RecordStore, Depends(current_records)]
