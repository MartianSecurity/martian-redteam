"""The one response the UI reads at start: who is signed in, the edition, what it includes, what the
principal may do, and whether the store is ready.

The shape is ``EvalsBootstrapOut`` in the server's frozen evals contract (``contracts/evals-api.json``
in MartianSecurity/MartianBackend); both servers answer ``GET /v1/evals/bootstrap`` with it. The core's
capabilities also carry ``hostedRoutes``, which the contract does not name and its schema allows.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from martian.evals.server.capabilities import capabilities_for, highest_role
from martian.evals.server.entitlements import EDITION_NAMES, Edition, Entitlements
from martian.evals.server.principal import Principal, Role
from martian.evals.server.projection import user_out
from martian.evals.store import StoreStatus


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BootstrapUser(_Out):
    id: str
    email: str | None
    display_name: str | None


class BootstrapOrganisation(_Out):
    id: str
    name: str | None


class BootstrapPrincipal(_Out):
    kind: Literal["single_user", "organisation"]
    user: BootstrapUser
    organisation: BootstrapOrganisation | None
    role: Role | None
    platform_admin: bool


class BootstrapEdition(_Out):
    key: Edition
    name: str


class BootstrapEntitlements(_Out):
    features: list[str]
    """The feature keys the edition includes, sorted."""
    limits: dict[str, int]


class EvalCapabilities(_Out):
    """Whether the principal's role and edition allow each action. The names are the Evals UI's.
    ``hostedRoutes`` is not one: it says whether the server serves the Martian server's own routes
    the Evals UI calls (CI sessions, API keys and the traces export), and the core serves none of
    them. The Martian server answers the contract's capabilities, which have no such key."""

    run: bool
    score: bool
    review: bool
    createReviewQueues: bool
    saveSuites: bool
    saveViews: bool
    editDrafts: bool
    publishDatasets: bool
    assignReviews: bool
    manageViews: bool
    manageApiKeys: bool
    retryCiExport: bool
    manageProjects: bool
    manageJudges: bool
    hostedRoutes: bool


class Bootstrap(_Out):
    principal: BootstrapPrincipal
    edition: BootstrapEdition
    entitlements: BootstrapEntitlements
    capabilities: EvalCapabilities
    status: StoreStatus


def principal_out(principal: Principal) -> BootstrapPrincipal:
    """The principal as the UI reads it: in the bootstrap, and in the local edition's session call."""
    organisation = (
        None
        if principal.organisation_id is None
        else BootstrapOrganisation(id=principal.organisation_id, name=principal.organisation_name)
    )
    return BootstrapPrincipal(
        kind=principal.kind,
        user=BootstrapUser(id=user_out(principal.user_id), email=principal.email, display_name=principal.display_name),
        organisation=organisation,
        role=highest_role(principal.roles),
        platform_admin=principal.platform_admin,
    )


def build_bootstrap(principal: Principal, entitlements: Entitlements, status: StoreStatus) -> Bootstrap:
    role = highest_role(principal.roles)
    return Bootstrap(
        principal=principal_out(principal),
        edition=BootstrapEdition(key=entitlements.edition, name=EDITION_NAMES[entitlements.edition]),
        entitlements=BootstrapEntitlements(features=sorted(entitlements.features), limits=dict(entitlements.limits)),
        capabilities=EvalCapabilities(**capabilities_for(role, entitlements), hostedRoutes=False),
        status=status,
    )
