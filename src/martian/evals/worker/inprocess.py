"""A worker inside the calling process: each piece of work is a task of the running event loop.

It runs one organisation's work, named when it is built (``None`` is the single local user), and
refuses any other: MLflow keeps the workspace, the active experiment and the tracking URI for the
whole process, so two organisations' evaluations must never share one
(MartianSecurity/martian-notes#140). That makes it right for the local edition, for tests, and inside
each run's own process, where :class:`~martian.evals.worker.process.ProcessWorker` uses it; an
installation that serves organisations uses ``ProcessWorker``.

Work it is given with ``submit`` runs while the process does and is lost with it. Under a
:class:`~martian.evals.worker.queue.QueueWorker`, which calls :meth:`InProcessWorker.run`, the
work stays in the queue until it has ended, and a worker takes it again if this process stops
first. ``targets`` are the targets it calls itself, by id: a function target, which it runs in a
process of its own that cannot reach ``private_paths`` on Linux, such as the stores' files; a
``TargetCall`` in this process, which nothing isolates; or an HTTP target whose ``${NAME}`` values
come from ``credentials``. Each is given the case's id and inputs only. ``executor`` calls the
registered agents, through martian-agents, for any other id (:mod:`martian.evals.worker.targets`).
Before each start of a run it pins the run's target again, as :meth:`InProcessWorker.check_target`
pinned it when the run was admitted, and fails the run without calling a target whose pin changed. The judge, if
any, is one ``Judge`` for every model scorer. ``target_timeout_seconds`` bounds every target call, and
``limits`` are the installation's limits for a run that has none of its own
(:class:`~martian.evals.model.RunLimits`). ``scorers`` are the custom Python scorers it runs, by name
(:data:`~martian.evals.worker.execute.InstalledScorers`); each is checked when the worker is
built, so one that reads an argument a worker does not give is refused before any work. ``hooks``
run around each case of a run, and ``checks`` are the checks it grades with by name, checked the
same way (:mod:`martian.evals.worker.hooks`); neither takes part in a monitor's or a schedule's
work. A monitor's work is one pass over its project's new traces (:mod:`martian.evals.worker.monitor`),
which reads a trace ``monitor_settle_seconds`` after its request time, then evaluates the
thresholds on the monitor's scores and delivers their alerts with ``alerts``, an installation's
:class:`~martian.evals.alerts.AlertSettings`, which send nothing unless given. After a scheduled
run's suite job, it records the run's gate result and does the same for the thresholds on the
schedule's gate results (:func:`~martian.evals.worker.schedule.record_gate`).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from martian.evals import _custom
from martian.evals.alerts import AlertSender, AlertSettings
from martian.evals.model import Run, RunLimits, Target
from martian.evals.records import RecordStore
from martian.evals.store import EvalStore
from martian.evals.worker import AgentExecutor, Credentials, Judge, TargetCall, Work
from martian.evals.worker.execute import InstalledChecks, InstalledScorers, execute_run, execute_scoring
from martian.evals.worker.hooks import CaseHooks
from martian.evals.worker.monitor import SETTLE_SECONDS, execute_monitor
from martian.evals.worker.schedule import record_gate
from martian.evals.worker.targets import own_target, target_call, target_pin

if TYPE_CHECKING:
    from martian.evals.server.principal import Principal

OTHER_ORGANISATION = "This worker runs one organisation's work; another organisation's work needs a process of its own"
NOT_A_RUNNERS = "A schedule's turn admits a run; the queue's worker takes it itself and never hands it to a runner"


class InProcessWorker:
    def __init__(
        self,
        store: EvalStore,
        records: RecordStore,
        *,
        organisation_id: str | None = None,
        targets: Mapping[str, TargetCall | Target] | None = None,
        judge: Judge | None = None,
        target_timeout_seconds: float = 120.0,
        credentials: Credentials | None = None,
        executor: AgentExecutor | None = None,
        scorers: InstalledScorers | None = None,
        limits: RunLimits | None = None,
        private_paths: Iterable[str | Path] = (),
        hooks: Sequence[CaseHooks] = (),
        checks: InstalledChecks | None = None,
        monitor_settle_seconds: float = SETTLE_SECONDS,
        alerts: AlertSettings | None = None,
    ) -> None:
        if monitor_settle_seconds < 0:
            raise ValueError("monitor_settle_seconds is 0 or more")
        self._store = store
        self._records = records
        self._organisation = organisation_id
        self._targets = dict(targets or {})
        for target in self._targets.values():
            if isinstance(target, Target):
                own_target(target)
        self._private = tuple(private_paths)
        self._judge = judge
        self._timeout = target_timeout_seconds
        self._credentials = credentials
        self._executor = executor
        self._scorers = dict(scorers or {})
        for function in self._scorers.values():
            _custom.arguments(function, _custom.WORKER_ARGUMENTS)
        self._hooks = tuple(hooks)
        for hook in self._hooks:
            if not isinstance(hook, CaseHooks):
                raise TypeError("a case hook has async setup(context) and teardown(context, error) methods")
        self._checks = dict(checks or {})
        for function in self._checks.values():
            _custom.arguments(function, _custom.CHECK_ARGUMENTS)
        self._limits = limits
        self._settle = monitor_settle_seconds
        self._alerts = AlertSender(alerts)
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def judges(self) -> bool:
        return self._judge is not None

    @property
    def alert_channels(self) -> frozenset[str]:
        """The channels this worker sends thresholds' alerts on (``SendsAlerts``)."""
        return frozenset(self._alerts.channels)

    def _check_organisation(self, principal: Principal) -> None:
        if principal.organisation_id != self._organisation:
            raise ValueError(OTHER_ORGANISATION)

    async def check_target(self, principal: Principal, target: Target) -> dict[str, Any]:
        self._check_organisation(principal)
        return await self._pin(principal, target)

    async def _pin(self, principal: Principal, target: Target | None) -> dict[str, Any]:
        """The pin of ``target`` as this worker has it now: when a run is admitted, and again
        before each start of the run, which fails when the two differ."""
        own = {key: value for key, value in self._targets.items() if isinstance(value, Target)}
        return await target_pin(principal, target, calls=self._targets.keys(), own=own, executor=self._executor)

    async def submit(self, work: Work) -> None:
        self._check_organisation(work.principal)
        task = asyncio.get_running_loop().create_task(self._execute(work))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def run(self, work: Work) -> None:
        """Do the work in this task and return once it has ended (:class:`~martian.evals.worker.Runner`)."""
        self._check_organisation(work.principal)
        await self._execute(work)

    async def drain(self) -> None:
        """Wait until every piece of work submitted so far, and any it led to, has finished."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks))

    def _call_for(self, principal: Principal, run: Run) -> TargetCall | None:
        return target_call(
            principal,
            run,
            targets=self._targets,
            credentials=self._credentials,
            executor=self._executor,
            timeout_seconds=self._timeout,
            private=self._private,
        )

    async def _execute(self, work: Work) -> None:
        if work.kind == "run":
            await execute_run(
                self._store,
                self._records,
                work,
                call_for=lambda run: self._call_for(work.principal, run),
                pin_for=lambda run: self._pin(work.principal, run.target),
                timeout_seconds=self._timeout,
                limits=self._limits,
                hooks=self._hooks,
                checks=self._checks,
            )
        elif work.kind == "scoring":
            await execute_scoring(
                self._store,
                self._records,
                work,
                judge=self._judge,
                scorers=self._scorers,
                limits=self._limits,
                checks=self._checks,
            )
            await record_gate(self._records, work, alerts=self._alerts)
        elif work.kind == "monitor":
            await execute_monitor(
                self._store, self._records, work, judge=self._judge, settle_seconds=self._settle, alerts=self._alerts
            )
        else:
            raise ValueError(NOT_A_RUNNERS)
