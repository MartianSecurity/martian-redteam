"""What each run's process opens in the process worker tests, and the same work on threads.

:func:`services` is the ``ProcessWorker`` setup: the store and the record store in a folder the
test shares between processes, an agent instrumented with MLflow tracing, and the write route
tests' deterministic judge. It points MLflow tracing at the store's own database with workspaces
on and selects the project's experiment, as any MLflow evaluation or traced agent in a run's
process would: what it selects lands in the process's workspace. Nothing calls a model.

The agent waits, at its first case, until every organisation's run has reached its first case, so
the runs are in flight at once.

Run as ``python -m tests.evals.process_fixture FOLDER WORK...``, this module runs each piece of
work (JSON) on a thread of its own in this one process, each thread fixing its organisation's
workspace as a run's process has it from its start: the threaded worker that one process per run
replaces.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from pathlib import Path

import mlflow

from martian.evals.model import Case
from martian.evals.records.sql import SqlRecordStore
from martian.evals.server import DEFAULT_PROJECT, Principal, organisation_principal
from martian.evals.store.mlflow_local import MlflowStore, workspace_name
from martian.evals.worker import Work, WorkerServices, load_work
from martian.evals.worker.process import WORKSPACE_ENV, _run_work
from tests.evals.write_fixture import AGENT, judge

ORGANISATIONS = ("org-a", "org-b")
ARRIVAL_SECONDS = 30


def member(organisation: str) -> Principal:
    return organisation_principal(f"member-of-{organisation}", organisation, frozenset({"member"}))


def answer(message: str) -> str:
    return f"The answer to {message}."


def cases(organisation: str) -> list[Case]:
    """Three cases whose every prompt names the organisation, so each trace says whose it is."""
    return [
        Case(
            id=f"{organisation}-{number}",
            inputs={"message": f"{organisation} question {number}", "organisation": organisation},
            expectations={"expected_response": answer(f"{organisation} question {number}")},
        )
        for number in range(3)
    ]


def open_stores(folder: Path) -> tuple[MlflowStore, SqlRecordStore]:
    return MlflowStore(f"sqlite:///{folder / 'mlflow.db'}", folder / "artifacts"), SqlRecordStore(
        f"sqlite:///{folder / 'martian.db'}"
    )


async def _all_arrived(folder: Path, organisation: str) -> None:
    (folder / f"arrived-{organisation}").touch()
    deadline = time.monotonic() + ARRIVAL_SECONDS
    while not all((folder / f"arrived-{name}").exists() for name in ORGANISATIONS):
        if time.monotonic() > deadline:
            raise TimeoutError("the other organisation's run never reached its first case")
        await asyncio.sleep(0.05)


def services(folder: str) -> WorkerServices:
    home = Path(folder)
    store, records = open_stores(home)
    os.environ["MLFLOW_ENABLE_WORKSPACES"] = "true"
    mlflow.set_tracking_uri(f"sqlite:///{home / 'mlflow.db'}")
    mlflow.set_experiment(DEFAULT_PROJECT)

    @mlflow.trace(name="agent")
    def reply(message: str) -> str:
        return answer(message)

    async def agent(case: Case) -> str:
        await _all_arrived(home, str(case.inputs["organisation"]))
        return reply(str(case.inputs["message"]))

    return WorkerServices(store, records, targets={AGENT: agent}, judge=judge, target_timeout_seconds=ARRIVAL_SECONDS)


def broken(folder: str) -> WorkerServices:
    """A setup that fails in the run's process before the work starts."""
    raise RuntimeError("the run's process could not open its stores")


def run_on_threads(folder: str, works: list[Work]) -> None:
    """Each piece of work on a thread of its own in this process, as a threaded worker would run it.
    The setups take turns (opening a record store migrates it, which is not thread-safe); the work
    itself runs at once."""
    opening = threading.Lock()

    def one(work: Work) -> None:
        with opening:
            os.environ[WORKSPACE_ENV] = workspace_name(work.principal)
            opened = services(folder)
        asyncio.run(_run_work(opened, work))

    threads = [threading.Thread(target=one, args=(work,)) for work in works]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


if __name__ == "__main__":
    run_on_threads(sys.argv[1], [load_work(json.loads(argument)) for argument in sys.argv[2:]])
