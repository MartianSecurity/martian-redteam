"""What each edition lets a principal use: one map from edition to feature keys and limits.

The edition comes from a verified licence (on-premises), else the organisation's plan (cloud),
else nothing, which is Community. Roles stay separate: a request needs the edition to include the
feature and the role to allow the action.

A free feature has no key: every edition serves it, and a free feature never becomes paid. A key
exists for a feature that some edition leaves out, and every key is enforced by
:func:`martian.evals.server.require_entitlement` on the routes that serve it. ``docs/evals-core.md``
lists the keys and the routes.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal, cast, get_args

Edition = Literal["community", "enterprise", "on_premises"]
Plan = Literal["community", "enterprise"]
"""An organisation's plan on the cloud. On-Premises is not a plan; it comes from a licence."""

EDITION_NAMES: Mapping[Edition, str] = MappingProxyType(
    {"community": "Community", "enterprise": "Enterprise", "on_premises": "On-Premises"}
)

FEATURES: Mapping[str, str] = MappingProxyType(
    {
        "review_queues": "review queues",
        "monitoring": "monitoring",
    }
)
"""Every feature key, with the name a refusal uses.

- ``review_queues``: review queues with an assignee, and the library of review questions they ask.
- ``monitoring``: the Monitoring page: scoring jobs and their scorecards across runs.
"""

LIMITS: Mapping[str, str] = MappingProxyType({})
"""Every limit key, with what it counts. No edition sets a limit yet; a limit key is added with the
code that enforces it."""


@dataclass(frozen=True)
class Entitlements:
    edition: Edition
    features: frozenset[str] = field(default_factory=frozenset)
    limits: Mapping[str, int] = field(default_factory=lambda: MappingProxyType({}), hash=False)

    def __post_init__(self) -> None:
        if unknown_features := self.features - FEATURES.keys():
            raise ValueError(f"unknown feature keys: {sorted(unknown_features)}")
        if unknown_limits := self.limits.keys() - LIMITS.keys():
            raise ValueError(f"unknown limit keys: {sorted(unknown_limits)}")

    def allows(self, feature: str) -> bool:
        return feature in self.features

    def limit(self, key: str) -> int | None:
        """The edition's limit for ``key``, or ``None`` when the edition sets none."""
        return self.limits.get(key)


PAID = frozenset({"review_queues", "monitoring"})

COMMUNITY = Entitlements(edition="community")
ENTERPRISE = Entitlements(edition="enterprise", features=PAID)
ON_PREMISES = Entitlements(edition="on_premises", features=PAID)

EDITIONS: Mapping[Edition, Entitlements] = MappingProxyType(
    {"community": COMMUNITY, "enterprise": ENTERPRISE, "on_premises": ON_PREMISES}
)


def resolve_edition(*, plan: str | None = None, licence: str | None = None) -> Edition:
    """The edition of an installation or organisation.

    ``licence`` is the edition named by a licence the caller has already verified; it decides for
    the whole installation. Otherwise ``plan`` is the organisation's plan. With neither, the
    edition is Community.
    """
    if licence is not None:
        if licence not in get_args(Edition):
            raise ValueError(f"a licence names one of {get_args(Edition)}, not {licence!r}")
        return cast(Edition, licence)
    if plan is not None:
        if plan not in get_args(Plan):
            raise ValueError(f"an organisation plan is one of {get_args(Plan)}, not {plan!r}")
        return cast(Edition, plan)
    return "community"


def entitlements_for(edition: Edition) -> Entitlements:
    return EDITIONS[edition]
