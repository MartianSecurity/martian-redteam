"""What a principal may do in Evals: one table from capability to the roles and feature it needs.

A capability is allowed when the principal's role is in its roles and, for a capability that serves
a paid feature, the edition includes that feature. The names are the ones the Evals UI uses, so the
UI can read the server's answer in place of its own role table. ``docs/evals-core.md`` lists them.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from martian.evals.server.entitlements import FEATURES, Entitlements
from martian.evals.server.principal import Role

ROLE_RANK: Mapping[Role, int] = MappingProxyType({"viewer": 0, "member": 1, "admin": 2, "owner": 3})

WRITERS: frozenset[Role] = frozenset({"owner", "admin", "member"})
ADMINS: frozenset[Role] = frozenset({"owner", "admin"})


@dataclass(frozen=True)
class Capability:
    roles: frozenset[Role]
    feature: str | None = None

    def __post_init__(self) -> None:
        if self.feature is not None and self.feature not in FEATURES:
            raise ValueError(f"unknown feature key {self.feature!r}")


CAPABILITIES: Mapping[str, Capability] = MappingProxyType(
    {
        "run": Capability(WRITERS),  # start or cancel an eval run
        "score": Capability(WRITERS),  # score saved outputs, cancel or retry a scoring job
        "review": Capability(WRITERS),  # answer a review or annotate a trace; who may be assigned one
        "createReviewQueues": Capability(WRITERS, "review_queues"),
        "saveSuites": Capability(WRITERS),
        "saveViews": Capability(WRITERS),  # save a trace view of one's own
        "editDrafts": Capability(WRITERS),  # organisation dataset drafts
        "publishDatasets": Capability(ADMINS),  # organisation datasets and their publish activity
        "assignReviews": Capability(ADMINS),  # assign review work to someone else
        "manageViews": Capability(ADMINS),  # change or delete anyone's saved trace view
        "manageApiKeys": Capability(ADMINS),  # CI API keys
        "retryCiExport": Capability(ADMINS),
        "manageProjects": Capability(ADMINS),  # create and archive projects
        "manageJudges": Capability(ADMINS),  # create judges and their versions
    }
)


def highest_role(roles: Iterable[Role]) -> Role | None:
    """The most privileged of ``roles``, or ``None`` for a principal with none."""
    return max(roles, key=ROLE_RANK.__getitem__, default=None)


def capabilities_for(role: Role | None, entitlements: Entitlements) -> dict[str, bool]:
    """Every capability, and whether ``role`` under ``entitlements`` allows it."""
    return {
        name: role in capability.roles and (capability.feature is None or entitlements.allows(capability.feature))
        for name, capability in CAPABILITIES.items()
    }
