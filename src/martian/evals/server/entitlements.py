"""What each edition lets a principal use: one map from edition to feature keys and limits.

The edition comes from a verified licence (on-premises), else the organisation's plan (cloud),
else nothing, which is Community. Roles stay separate: a request needs the edition to include the
feature and the role to allow the action.

A free feature has no key: every edition serves it, and a free feature never becomes paid. A key
exists for a feature that some edition leaves out, and every key is enforced by
:func:`martian.evals.server.require_entitlement` on the routes that serve it. A limit key is
enforced where the work it counts is admitted (:mod:`martian.evals.server.launch`).
``docs/evals-core.md`` lists the keys, the limits and the routes.
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
        "redteam_environments": "red team environments",
        "redteam_campaigns": "red team campaigns",
        "redteam_hosted_generation": "hosted red team generation",
    }
)
"""Every feature key, with the name a refusal uses.

- ``review_queues``: review queues with an assignee, and the library of review questions they ask.
- ``monitoring``: the Monitoring page: scoring jobs and their scorecards across runs, the
  monitors that score a project's live traces (:mod:`martian.evals.server.monitors`), and the
  thresholds on their scores, which send alerts (:mod:`martian.evals.server.thresholds`), and the
  schedules that run a saved suite against a target unattended (:mod:`martian.evals.server.schedules`).
- ``redteam_environments``: a red team run in a Martian environment, whose verdicts can rest on the
  world's state. ``POST /v1/redteam/runs`` refuses a run file that names an ``environment`` without
  it; a hosted server refuses its own environment runs too. Built only in the hosted product.
- ``redteam_campaigns``: planned red team campaigns from the hosted console. The key is enforced
  by the hosted server, which runs them; the core has no campaign route.
- ``redteam_hosted_generation``: attack generation and judging by Martian's engine for SDK red team
  runs (the hosted ``/v1/redteam/simulate`` and ``/evaluate`` operations). Enforced by the hosted
  server. A local run generates and judges with the user's own model and needs no key.
"""

LIMITS: Mapping[str, str] = MappingProxyType(
    {
        "concurrent_runs": "eval runs",
        "concurrent_scoring_jobs": "scoring jobs",
        "concurrent_redteam_operations": "hosted red team operations",
    }
)
"""Every limit key, with the name of what it counts. A limit key is added with the code that
enforces it; an edition that sets no value for a key has no limit there.

- ``concurrent_runs``: how many of an organisation's runs may be queued or running at once. A
  launch, a run again or a resume beyond it is refused with ``409``.
- ``concurrent_scoring_jobs``: how many of an organisation's scoring jobs of completed runs may be
  queued or running at once. A scoring request or a resume beyond it is refused with ``409``.
- ``concurrent_redteam_operations``: how many of an organisation's hosted red team generation and
  judging operations may be in flight at once. Enforced by the hosted server where it admits an
  operation (``429``); per-minute and per-day rate windows stay the server's own settings.
"""


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
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 1 for value in self.limits.values()):
            raise ValueError("a limit is a whole number of at least 1; leave a key out for no limit")

    def allows(self, feature: str) -> bool:
        return feature in self.features

    def limit(self, key: str) -> int | None:
        """The edition's limit for ``key``, or ``None`` when the edition sets none."""
        return self.limits.get(key)


REDTEAM_PAID = frozenset({"redteam_environments", "redteam_campaigns", "redteam_hosted_generation"})
"""Red Team's paid keys: an environment, a planned campaign and Martian's engine are hosted only."""

PAID = frozenset({"review_queues", "monitoring"}) | REDTEAM_PAID

COMMUNITY = Entitlements(
    edition="community",
    limits=MappingProxyType({"concurrent_runs": 1, "concurrent_scoring_jobs": 1}),
)
ENTERPRISE = Entitlements(
    edition="enterprise",
    features=PAID,
    limits=MappingProxyType({"concurrent_runs": 5, "concurrent_scoring_jobs": 5, "concurrent_redteam_operations": 2}),
)
ON_PREMISES = Entitlements(edition="on_premises", features=PAID)
"""An on-premises installation sets no limit: its own workers bound what runs at once."""

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
