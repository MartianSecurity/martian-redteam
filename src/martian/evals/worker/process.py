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
reaches the process as JSON on its standard input (:func:`dump_work`), never on its command line.
A process that exits with an error leaves no work queued or running: the worker marks it
``failed``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import importlib
import json
import logging
import os
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from martian.evals.records import RecordStore
from martian.evals.server.principal import Principal
from martian.evals.store import EvalStore, StoreError
from martian.evals.store.mlflow_local import workspace_name
from martian.evals.worker import Judge, TargetCall, Work
from martian.evals.worker.execute import fail_unfinished
from martian.evals.worker.inprocess import InProcessWorker

if TYPE_CHECKING:
    from martian.evals.model import Target

log = logging.getLogger("martian.evals.worker")

WORKSPACE_ENV = "MLFLOW_WORKSPACE"
"""The environment variable MLflow's client reads its workspace from in every thread."""

UNSELECTED = ("MLFLOW_EXPERIMENT_ID", "MLFLOW_EXPERIMENT_NAME")
"""Selections a run's process never inherits from the process that starts it."""

ENDED_EARLY = "The worker process ended before this work finished"
WRONG_WORKSPACE = (
    "martian.evals.worker: this process's MLflow workspace is not the work's organisation's; "
    "ProcessWorker fixes it when it starts the process"
)


@dataclass(frozen=True)
class WorkerServices:
    """What a run's process runs its work with, built in that process by the worker's setup: the
    store and the record store, the agents it can call by id, and the judge, if any."""

    store: EvalStore
    records: RecordStore
    targets: Mapping[str, TargetCall] = field(default_factory=dict)
    judge: Judge | None = None
    target_timeout_seconds: float = 120.0


def dump_work(work: Work) -> dict[str, Any]:
    """``work`` as JSON, as a run's process reads it."""
    principal = {**dataclasses.asdict(work.principal), "roles": sorted(work.principal.roles)}
    return {
        "kind": work.kind,
        "principal": principal,
        "project_id": work.project_id,
        "run_id": work.run_id,
        "job_id": work.job_id,
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
    the agent ids its targets call, which :meth:`check_target` accepts, and ``judges`` says whether
    it gives a judge. ``records`` is this process's record store, used only to mark work failed
    when its process exits with an error.
    """

    def __init__(
        self,
        setup: Callable[..., WorkerServices],
        records: RecordStore,
        *,
        agents: Iterable[str] = (),
        judges: bool = False,
        options: Mapping[str, Any] | None = None,
    ) -> None:
        self._setup = _import_path(setup)
        self._options = dict(options or {})
        json.dumps(self._options)  # each process reads them as JSON
        self._records = records
        self._agents = frozenset(agents)
        self._judges = judges
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def judges(self) -> bool:
        return self._judges

    async def check_target(self, principal: Principal, target: Target) -> dict[str, Any]:
        if target.kind != "agent" or target.agent_id not in self._agents:
            raise StoreError("not_found", "Agent not found")
        return {"kind": target.kind, "agent_id": target.agent_id}

    async def submit(self, work: Work) -> None:
        """Start the work's process and return; the process runs the work."""
        request = json.dumps({"setup": self._setup, "options": self._options, "work": dump_work(work)})
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "martian.evals.worker",
            stdin=asyncio.subprocess.PIPE,
            env=_environment(work.principal),
        )
        task = asyncio.get_running_loop().create_task(self._finish(work, process, request.encode()))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """Wait until every process started so far has exited."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks))

    async def _finish(self, work: Work, process: asyncio.subprocess.Process, request: bytes) -> None:
        await process.communicate(request)
        if process.returncode != 0:
            log.error("The process for %s work on run %s exited with %s", work.kind, work.run_id, process.returncode)
            await fail_unfinished(self._records, work, ENDED_EARLY)


async def _run_work(services: WorkerServices, work: Work) -> None:
    """Run one piece of work in this process, with a worker that takes only its organisation's."""
    worker = InProcessWorker(
        services.store,
        services.records,
        organisation_id=work.principal.organisation_id,
        targets=services.targets,
        judge=services.judge,
        target_timeout_seconds=services.target_timeout_seconds,
    )
    await worker.submit(work)
    await worker.drain()


def main() -> int:
    """``python -m martian.evals.worker``: run the piece of work on standard input in this process,
    then exit. ``ProcessWorker`` starts it. Exit status 0 means the work ran, whatever its outcome,
    which its record holds; 2 means this process's workspace is not the work's organisation's."""
    request = json.loads(sys.stdin.read())
    work = load_work(request["work"])
    if os.environ.get(WORKSPACE_ENV) != workspace_name(work.principal):
        print(WRONG_WORKSPACE, file=sys.stderr)
        return 2
    services = _resolve(request["setup"])(**request["options"])
    if not isinstance(services, WorkerServices):
        raise TypeError("the worker's setup must return WorkerServices")
    asyncio.run(_run_work(services, work))
    return 0
