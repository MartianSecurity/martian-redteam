"""A worker inside the calling process: each piece of work is a task of the running event loop.

It runs one organisation's work, named when it is built (``None`` is the single local user), and
refuses any other: MLflow keeps the workspace, the active experiment and the tracking URI for the
whole process, so two organisations' evaluations must never share one
(MorningHQ/martian-notes#140). That makes it right for the local edition, for tests, and inside
each run's own process, where :class:`~martian.evals.worker.process.ProcessWorker` uses it; an
installation that serves organisations uses ``ProcessWorker``.

Work runs while the process does and is lost with it; a durable queue is MorningHQ/martian-notes#139.
Targets are registered agents, each given as a ``TargetCall`` by its agent id, and the judge, if
any, is one ``Judge`` for every model scorer.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from martian.evals.model import Run, Target
from martian.evals.records import RecordStore
from martian.evals.store import EvalStore, StoreError
from martian.evals.worker import Judge, TargetCall, Work
from martian.evals.worker.execute import execute_run, execute_scoring

if TYPE_CHECKING:
    from martian.evals.server.principal import Principal

OTHER_ORGANISATION = "This worker runs one organisation's work; another organisation's work needs a process of its own"


class InProcessWorker:
    def __init__(
        self,
        store: EvalStore,
        records: RecordStore,
        *,
        organisation_id: str | None = None,
        targets: Mapping[str, TargetCall] | None = None,
        judge: Judge | None = None,
        target_timeout_seconds: float = 120.0,
    ) -> None:
        self._store = store
        self._records = records
        self._organisation = organisation_id
        self._targets = dict(targets or {})
        self._judge = judge
        self._timeout = target_timeout_seconds
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def judges(self) -> bool:
        return self._judge is not None

    def _check_organisation(self, principal: Principal) -> None:
        if principal.organisation_id != self._organisation:
            raise ValueError(OTHER_ORGANISATION)

    async def check_target(self, principal: Principal, target: Target) -> dict[str, Any]:
        self._check_organisation(principal)
        if target.kind != "agent" or target.agent_id not in self._targets:
            raise StoreError("not_found", "Agent not found")
        return {"kind": target.kind, "agent_id": target.agent_id}

    async def submit(self, work: Work) -> None:
        self._check_organisation(work.principal)
        task = asyncio.get_running_loop().create_task(self._execute(work))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """Wait until every piece of work submitted so far, and any it led to, has finished."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks))

    def _call_for(self, run: Run) -> TargetCall | None:
        target = run.target
        if target is None or target.kind != "agent" or target.agent_id is None:
            return None
        return self._targets.get(target.agent_id)

    async def _execute(self, work: Work) -> None:
        if work.kind == "run":
            await execute_run(self._store, self._records, work, call_for=self._call_for, timeout_seconds=self._timeout)
        else:
            await execute_scoring(self._store, self._records, work, judge=self._judge)
