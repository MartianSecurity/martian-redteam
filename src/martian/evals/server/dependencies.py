"""What a route of the evals core reads from its request: the edition's principal, entitlements,
store, record store, worker and judge settings, bound by :func:`martian.evals.server.create_app`, and the checks a
write route makes of the principal's role and edition."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, HTTPException, Request

from martian.evals.judge_settings import JudgeSettings
from martian.evals.records import RecordStore
from martian.evals.server.capabilities import CAPABILITIES, ROLE_RANK, highest_role
from martian.evals.server.entitlements import EDITION_NAMES, FEATURES, Entitlements
from martian.evals.server.principal import Principal
from martian.evals.store import EvalStore
from martian.evals.worker import Worker

PrincipalProvider = Callable[..., Principal | Awaitable[Principal]]
EntitlementsProvider = Callable[[Principal], Entitlements]
AgentNames = Callable[[Principal, list[str]], Awaitable[dict[str, str]]]
"""An edition's lookup of registered agents' display names: from a principal and agent ids to the
names it knows. The server reads its agent registry; the local edition has none."""


def current_store(request: Request) -> EvalStore:
    store: EvalStore = request.app.state.martian_evals_store
    return store


def current_records(request: Request) -> RecordStore:
    records: RecordStore = request.app.state.martian_evals_records
    return records


def current_worker(request: Request) -> Worker:
    """The edition's worker. A route that starts work refuses with ``503`` when there is none."""
    worker: Worker | None = request.app.state.martian_evals_worker
    if worker is None:
        raise HTTPException(503, "This installation runs no evaluation worker")
    return worker


def optional_worker(request: Request) -> Worker | None:
    """The edition's worker, or ``None`` when there is none, for a route that works without one."""
    worker: Worker | None = request.app.state.martian_evals_worker
    return worker


def current_judge_settings(request: Request) -> JudgeSettings | None:
    """Where the edition keeps the default judge model, or ``None`` when it keeps none."""
    settings: JudgeSettings | None = request.app.state.martian_evals_judge_settings
    return settings


def current_agent_names(request: Request) -> AgentNames | None:
    """The edition's agent name lookup, or ``None`` when it has none."""
    names: AgentNames | None = request.app.state.martian_evals_agent_names
    return names


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


def require_capability(name: str) -> Callable[[Principal, Entitlements], None]:
    """A route dependency that refuses with ``403`` unless the principal's most privileged role
    allows capability ``name`` (``martian.evals.server.capabilities``). A capability that serves a
    paid feature is refused first for an edition without it, as :func:`ensure_entitled` words it."""
    capability = CAPABILITIES[name]
    least = min(capability.roles, key=ROLE_RANK.__getitem__)

    def check(
        principal: Annotated[Principal, Depends(current_principal)],
        entitlements: Annotated[Entitlements, Depends(current_entitlements)],
    ) -> None:
        if capability.feature is not None:
            ensure_entitled(entitlements, capability.feature)
        if highest_role(principal.roles) not in capability.roles:
            raise HTTPException(403, f"Requires {least} role")

    return check


def allows(principal: Principal, name: str) -> bool:
    """Whether the principal's role allows capability ``name``, whatever the edition."""
    return highest_role(principal.roles) in CAPABILITIES[name].roles


PrincipalDep = Annotated[Principal, Depends(current_principal)]
EntitlementsDep = Annotated[Entitlements, Depends(current_entitlements)]
StoreDep = Annotated[EvalStore, Depends(current_store)]
RecordsDep = Annotated[RecordStore, Depends(current_records)]
WorkerDep = Annotated[Worker, Depends(current_worker)]
OptionalWorkerDep = Annotated[Worker | None, Depends(optional_worker)]
JudgeSettingsDep = Annotated[JudgeSettings | None, Depends(current_judge_settings)]
AgentNamesDep = Annotated[AgentNames | None, Depends(current_agent_names)]
