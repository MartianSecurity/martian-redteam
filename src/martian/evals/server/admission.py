"""How much of an organisation's work may be in progress at once: the edition's limits on runs and
scoring jobs, checked where work is admitted (MorningHQ/martian-notes#145).

The limits are entitlement limits (:mod:`martian.evals.server.entitlements`):

- ``concurrent_runs`` counts the organisation's runs that are ``queued`` or ``running``, in every
  project, a run that another process records itself (``martian evals run``) included. A launch,
  a run again or a resume beyond it is ``409``.
- ``concurrent_scoring_jobs`` counts the ``queued`` or ``running`` scoring jobs of the
  organisation's completed runs: the jobs a worker can take. A scoring request or a resume beyond
  it is ``409``. The local edition's suite job is admitted with its run, under the run limit, and
  counts once its run has completed.

An edition that sets no value for a key has no limit there. Each check is one indexed count in the
record store (:meth:`RecordStore.count_active_runs`, :meth:`RecordStore.count_active_scoring_jobs`),
whatever the number of runs, and then admits, so two requests at the same moment can both be
admitted at the limit's last place.
What runs at once is the worker's: a :class:`~martian.evals.worker.QueueWorker` runs as many
pieces as its ``concurrency`` and takes the oldest work of the organisation with the fewest
running first.
"""

from __future__ import annotations

from fastapi import HTTPException

from martian.evals.model import RunState
from martian.evals.records import RecordStore
from martian.evals.server.entitlements import LIMITS, Entitlements
from martian.evals.server.principal import Principal
from martian.evals.store import EvalStore

ACTIVE: tuple[RunState, ...] = ("queued", "running")
"""The states of a run or scoring job in progress."""

RUNS = "concurrent_runs"
SCORING_JOBS = "concurrent_scoring_jobs"

_AT_ONE = {
    RUNS: "An eval run is already in progress",
    SCORING_JOBS: "A scoring job is already in progress",
}


def at_limit(key: str, limit: int) -> str:
    """The refusal of work beyond the organisation's ``limit`` for ``key``. A limit of one keeps the
    server's wording, "An eval run is already in progress"."""
    if limit == 1:
        return _AT_ONE[key]
    return f"{limit} {LIMITS[key]} are already in progress, as many as this organisation may have at once"


async def ensure_run_place(
    store: EvalStore, records: RecordStore, principal: Principal, entitlements: Entitlements
) -> None:
    """Refuse (``409``) another run while the organisation has its edition's ``concurrent_runs``
    queued or running."""
    limit = entitlements.limit(RUNS)
    if limit is not None and await records.count_active_runs(principal, up_to=limit) >= limit:
        raise HTTPException(409, at_limit(RUNS, limit))


async def ensure_scoring_place(
    store: EvalStore, records: RecordStore, principal: Principal, entitlements: Entitlements
) -> None:
    """Refuse (``409``) another scoring job while the organisation has its edition's
    ``concurrent_scoring_jobs`` queued or running."""
    limit = entitlements.limit(SCORING_JOBS)
    if limit is not None and await records.count_active_scoring_jobs(principal, up_to=limit) >= limit:
        raise HTTPException(409, at_limit(SCORING_JOBS, limit))


__all__ = [
    "ACTIVE",
    "RUNS",
    "SCORING_JOBS",
    "at_limit",
    "ensure_run_place",
    "ensure_scoring_place",
]
