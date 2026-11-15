"""One process per evaluation run: each piece of work runs in a new process of its own.

MLflow keeps this state for the whole process, not for a request or a thread:

- The client workspace, in the ``MLFLOW_WORKSPACE`` environment variable. ``mlflow.set_workspace``
  writes it, and every thread that has not set MLflow's per-request context reads it: the threads
  MLflow's evaluation harness calls the target on, the thread that exports traces in the
  background, and the SDK judge's gateway call, which sends it as the workspace header.
- The active experiment (``mlflow.set_experiment``, which also sets ``MLFLOW_EXPERIMENT_ID``),
  where every new trace and evaluation run is recorded.
- The tracking URI (``mlflow.set_tracking_uri``) and the trace destination
  (``mlflow.tracing.set_destination``).

Two organisations' evaluations on threads of one process therefore record one organisation's
traces in the other's workspace, or lose them; ``tests/evals/test_worker_process.py`` shows it.
So :class:`ProcessWorker` starts a new Python process for each piece of work, ``python -m
martian.evals.worker``, with ``MLFLOW_WORKSPACE`` set to the workspace of the work's organisation
(:func:`~martian.evals.store.mlflow_local.workspace_name`) and no experiment selected, before the
interpreter starts. The process refuses to start under any other workspace, runs that one piece
of work with an :class:`~martian.evals.worker.inprocess.InProcessWorker` that takes only that
organisation's work, and exits. Whatever MLflow selects inside it stays with that organisation.

Each process builds what it runs the work with by calling the worker's *setup*: a module-level
function, named by its import path so that the new process can import it, which takes the
worker's ``options`` (JSON) as keyword arguments and returns :class:`WorkerServices`. The work
reaches the process as JSON on its standard input (:func:`dump_work`), never on its command line,
with the HTTP target its run names, as written. Its credentials come from the setup's
``credentials``, in that process. A process that exits with an error leaves no work queued or
running: the worker marks it ``failed``.

Custom Python scorers are the setup's too (``WorkerServices.scorers``): the setup imports them in
the work's process, so their code never runs in the process that serves the API. That process
imports the setup's *module*, to check that a new process can import the setup, so the setup
imports a scorer's module inside the function, not at the top of its own module.

Under a :class:`~martian.evals.worker.queue.QueueWorker`, which calls :meth:`ProcessWorker.run`,
the run's process ends with the process that started it, the one holding the work's claim in the
queue: it keeps the read end of a pipe from that process open as its standard input, and exits
as soon as the pipe closes, however that process ended. It runs in a session of its own, so a
signal meant for that process, such as the terminal's Ctrl+C, reaches only that process, which
ends it. So the run's process never outlives the claim, and the worker that takes the work next
is the only one running it.
"""

from __future__ import annotations

import asyncio
import dataclasses
import importlib
import json
import logging
import os
import sys
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from martian.evals.alerts import AlertSettings
from martian.evals.model import RunLimits, Target
from martian.evals.records import RecordStore
from martian.evals.server.principal import Principal
from martian.evals.store import EvalStore
from martian.evals.store.mlflow_local import workspace_name
from martian.evals.worker import AgentExecutor, Credentials, Judge, TargetCall, Work
from martian.evals.worker.execute import InstalledScorers, fail_unfinished
from martian.evals.worker.inprocess import InProcessWorker
from martian.evals.worker.monitor import SETTLE_SECONDS
from martian.evals.worker.targets import http_target, target_pin

log = logging.getLogger("martian.evals.worker")

WORKSPACE_ENV = "MLFLOW_WORKSPACE"
"""The environment variable MLflow's client reads its workspace from in every thread."""

UNSELECTED = ("MLFLOW_EXPERIMENT_ID", "MLFLOW_EXPERIMENT_NAME")
"""Selections a run's process never inherits from the process that starts it."""

ENDED_EARLY = "The worker process ended before this work finished"
NOT_STARTED = "The worker could not start a process for this work"
PARENT_GONE = 3
"""The exit status of a run's process that stopped because the process that started it ended."""
WRONG_WORKSPACE = (
    "martian.evals.worker: this process's MLflow workspace is not the work's organisation's; "
    "ProcessWorker fixes it when it starts the process"
)


@dataclass(frozen=True)
class WorkerServices:
    """What a run's process runs its work with, built in that process by the worker's setup: the
    store and the record store, the targets it calls itself by id, the judge, if any, the time
    limit of each target call, the credentials of HTTP targets, the executor of registered agents,
    if any, the custom Python scorers it runs, by name, imported by the setup in this process, the
    limits of a run that has none of its own, how long after its request time a monitor first reads
    a trace, and how the alerts of thresholds on monitors' scores are delivered, none by default
    (:class:`~martian.evals.alerts.AlertSettings`)."""

    store: EvalStore
    records: RecordStore
    targets: Mapping[str, TargetCall | Target] = field(default_factory=dict)
    judge: Judge | None = None
    target_timeout_seconds: float = 120.0
    credentials: Credentials | None = None
    executor: AgentExecutor | None = None
    scorers: InstalledScorers = field(default_factory=dict)
    limits: RunLimits | None = None
    monitor_settle_seconds: float = SETTLE_SECONDS
    alerts: AlertSettings | None = None


def dump_work(work: Work) -> dict[str, Any]:
    """``work`` as JSON, as a run's process reads it."""
    principal = {**dataclasses.asdict(work.principal), "roles": sorted(work.principal.roles)}
    return {
        "kind": work.kind,
        "principal": principal,
        "project_id": work.project_id,
        "run_id": work.run_id,
        "job_id": work.job_id,
        "monitor_id": work.monitor_id,
        "schedule_id": work.schedule_id,
    }


def load_work(data: Mapping[str, Any]) -> Work:
    """The work :func:`dump_work` wrote."""
    principal = Principal(**{**data["principal"], "roles": frozenset(data["principal"]["roles"])})
    return Work(
        kind=data["kind"],
        principal=principal,
        project_id=data["project_id"],
        run_id=data["run_id"],
        job_id=data["job_id"],
        monitor_id=data.get("monitor_id"),
        schedule_id=data.get("schedule_id"),
    )


def _resolve(name: str) -> Any:
    module, _, attribute = name.partition(":")
    found: Any = importlib.import_module(module)
    for part in attribute.split("."):
        found = getattr(found, part, None)
    return found


def _import_path(function: Callable[..., Any]) -> str:
    module = getattr(function, "__module__", None)
    name = f"{module}:{getattr(function, '__qualname__', '')}"
    if module in (None, "__main__") or _resolve(name) is not function:
        raise TypeError("the setup must be a module-level function, so that each run's process can import it")
    return name


def _environment(principal: Principal) -> dict[str, str]:
    """The environment a run's process starts with: this one's, with the organisation's workspace
    fixed and no experiment selected."""
    environment = {name: value for name, value in os.environ.items() if name not in UNSELECTED}
    environment[WORKSPACE_ENV] = workspace_name(principal)
    return environment


class ProcessWorker:
    """Runs each piece of work in a new process of its own, which ends with it.

    ``setup`` builds the :class:`WorkerServices` in each process from ``options``. ``agents`` are
    the ids of the functions its targets call, which :meth:`check_target` accepts, ``judges``
    says whether it gives a judge, and ``alert_channels`` which alert channels its setup's
    ``alerts`` send. ``targets`` are the HTTP targets the worker calls itself, by
    id; they are read at each check and each run, so they may be a live view, and each run's
    process receives the one its run names with its work. ``executor`` admits the registered agents, which each
    run's process calls through its setup's executor. ``records`` is this process's record store,
    used to find the target a run names and to mark work failed when its process exits with an
    error.
    """

    def __init__(
        self,
        setup: Callable[..., WorkerServices],
        records: RecordStore,
        *,
        agents: Iterable[str] = (),
        judges: bool = False,
        options: Mapping[str, Any] | None = None,
        targets: Mapping[str, Target] | None = None,
        executor: AgentExecutor | None = None,
        alert_channels: Iterable[str] = (),
    ) -> None:
        self._setup = _import_path(setup)
        self._options = dict(options or {})
        json.dumps(self._options)  # each process reads them as JSON
        self._records = records
        self._agents = frozenset(agents)
        self._targets: Mapping[str, Target] = targets if targets is not None else {}
        if both := sorted(self._agents & set(self._targets)):
            raise ValueError(f"{', '.join(both)} cannot be both an agent and an http target")
        self._executor = executor
        self._judges = judges
        self._alert_channels = frozenset(alert_channels)
        if unknown := sorted(self._alert_channels - {"webhook", "email"}):
            raise ValueError(f"unknown alert channels: {', '.join(unknown)}")
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def judges(self) -> bool:
        return self._judges

    @property
    def alert_channels(self) -> frozenset[str]:
        """The channels its processes send thresholds' alerts on, as it was told (``SendsAlerts``)."""
        return self._alert_channels

    async def check_target(self, principal: Principal, target: Target) -> dict[str, Any]:
        return await target_pin(principal, target, calls=self._agents, http=self._targets, executor=self._executor)

    async def submit(self, work: Work) -> None:
        """Start the work's process and return; the process runs the work."""
        process, request = await self._start(work, tied=False)
        task = asyncio.get_running_loop().create_task(self._finish(work, process, request))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def run(self, work: Work) -> None:
        """Run the work in a process of its own and return once that process has ended
        (:class:`~martian.evals.worker.Runner`). The process ends with this one, and when ``run``
        is cancelled it is ended at once and the work is left as it is, for the queue to give to
        the next worker. A process that exits with an error fails the work, as with ``submit``, and
        so does one that cannot start, such as for a target the worker now refuses."""
        try:
            process, request = await self._start(work, tied=True)
        except Exception:  # noqa: BLE001 - work that cannot start must not wait in the queue
            log.exception("The process for %s work on %s could not start", work.kind, work.run_id or work.job_id)
            await fail_unfinished(self._records, work, NOT_STARTED)
            return
        assert process.stdin is not None
        try:
            process.stdin.write(request + b"\n")
            await process.stdin.drain()
            await process.wait()
        except BaseException:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        finally:
            process.stdin.close()
        await self._check_exit(work, process)

    async def drain(self) -> None:
        """Wait until every process started so far has exited."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks))

    async def _named_target(self, work: Work) -> dict[str, Any]:
        """The HTTP target the work's run names, as written, when it is one of this worker's: the
        only target the run's process receives, so a target the run does not name is never read or
        checked. ``check_target`` checked this one before the run was recorded."""
        if work.kind != "run":
            return {}
        run = await self._records.get_run(work.principal, work.project_id, work.run_id)
        named = run.target.agent_id if run is not None and run.target is not None else None
        if named is None or named not in self._targets:
            return {}
        return {named: http_target(self._targets[named]).model_dump(mode="json")}

    async def _start(self, work: Work, *, tied: bool) -> tuple[asyncio.subprocess.Process, bytes]:
        """Start the work's process and return it with the request to send it. A ``tied`` process
        has a session of its own and ends when its standard input closes."""
        request = json.dumps(
            {
                "setup": self._setup,
                "options": self._options,
                "work": dump_work(work),
                "targets": await self._named_target(work),
                "tied": tied,
            }
        )
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "martian.evals.worker",
            stdin=asyncio.subprocess.PIPE,
            env=_environment(work.principal),
            start_new_session=tied,
        )
        return process, request.encode()

    async def _finish(self, work: Work, process: asyncio.subprocess.Process, request: bytes) -> None:
        await process.communicate(request)
        await self._check_exit(work, process)

    async def _check_exit(self, work: Work, process: asyncio.subprocess.Process) -> None:
        if process.returncode != 0:
            named = work.run_id or work.job_id  # a re-check names no run
            log.error("The process for %s work on %s exited with %s", work.kind, named, process.returncode)
            await fail_unfinished(self._records, work, ENDED_EARLY)


async def _run_work(services: WorkerServices, work: Work, http: Mapping[str, Target] | None = None) -> None:
    """Run one piece of work in this process, with a worker that takes only its organisation's.
    ``http`` are the HTTP targets the work arrived with, beside the setup's own targets."""
    http = dict(http or {})
    if both := sorted(set(services.targets) & set(http)):
        raise ValueError(f"the setup's targets and the worker's http targets both name {', '.join(both)}")
    worker = InProcessWorker(
        services.store,
        services.records,
        organisation_id=work.principal.organisation_id,
        targets={**services.targets, **http},
        judge=services.judge,
        target_timeout_seconds=services.target_timeout_seconds,
        credentials=services.credentials,
        executor=services.executor,
        scorers=services.scorers,
        limits=services.limits,
        monitor_settle_seconds=services.monitor_settle_seconds,
        alerts=services.alerts,
    )
    await worker.submit(work)
    await worker.drain()


def _end_with_parent() -> None:
    """Exit with :data:`PARENT_GONE` once standard input closes: the process that started this one
    has ended."""
    sys.stdin.read()
    os._exit(PARENT_GONE)


def main() -> int:
    """``python -m martian.evals.worker``: run the piece of work on standard input in this process,
    then exit. ``ProcessWorker`` starts it. Exit status 0 means the work ran, whatever its outcome,
    which its record holds; 2 means this process's workspace is not the work's organisation's, and
    3 that it stopped because the process that started it ended (a tied process)."""
    request = json.loads(sys.stdin.readline())
    if request.get("tied"):
        threading.Thread(target=_end_with_parent, name="martian-parent-watch", daemon=True).start()
    work = load_work(request["work"])
    if os.environ.get(WORKSPACE_ENV) != workspace_name(work.principal):
        print(WRONG_WORKSPACE, file=sys.stderr)
        return 2
    http = {key: Target.model_validate(value) for key, value in request["targets"].items()}
    services = _resolve(request["setup"])(**request["options"])
    if not isinstance(services, WorkerServices):
        raise TypeError("the worker's setup must return WorkerServices")
    asyncio.run(_run_work(services, work, http))
    return 0
