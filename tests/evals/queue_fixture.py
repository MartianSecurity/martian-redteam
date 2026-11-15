"""The serving process of the queue tests: the core with a queue-backed worker, in a process of its
own that a test stops and starts again.

``python -m tests.evals.queue_fixture FOLDER RUNNER DATABASE_URL SCHEMA MODE PORT_FILE`` serves
the core for the single local user on 127.0.0.1, on a free port it writes to ``PORT_FILE``. The
store is ``MlflowStore`` on SQLite in ``FOLDER``, and the records and the queue are in
``DATABASE_URL`` (tables in ``SCHEMA`` on Postgres). ``MODE`` is ``serve``, a worker beside the
app that takes the queued work, or ``queue``, an API process that only queues it. ``RUNNER`` is
``in-process``, the work as tasks of the serving process, or ``processes``, each piece in a
process of its own (:func:`services` is that process's setup).

The agent writes ``called-<case id>-<pid>`` in ``FOLDER`` when it is called, and answers as the
write route tests' agent does once ``FOLDER/release`` exists, so a test can stop the server while
a case is in flight.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from fastapi import FastAPI

from martian.evals.local import listen
from martian.evals.model import Case
from martian.evals.records.queue import SqlWorkQueue
from martian.evals.records.sql import SqlRecordStore
from martian.evals.server import SINGLE_USER, create_app
from martian.evals.store.mlflow_local import MlflowStore
from martian.evals.worker import InProcessWorker, ProcessWorker, QueueWorker, Runner, TargetCall, WorkerServices
from tests.evals.write_fixture import AGENT, agent

LEASE_SECONDS = 1.0
POLL_SECONDS = 0.1
CASE_SECONDS = 120


def store_in(folder: Path) -> MlflowStore:
    os.environ.setdefault("MLFLOW_DISABLE_TELEMETRY", "true")
    return MlflowStore(f"sqlite:///{folder / 'mlflow.db'}", folder / "artifacts")


def waiting_agent(folder: Path) -> TargetCall:
    """The write route tests' agent, which answers each case only once ``folder/release`` exists."""

    async def call(case: Case) -> str:
        (folder / f"called-{case.id}-{os.getpid()}").touch()
        while not (folder / "release").exists():
            await asyncio.sleep(0.05)
        return await agent(case)

    return call


def services(folder: str, database_url: str, schema: str) -> WorkerServices:
    """The setup of each run's process: the same stores and the waiting agent."""
    home = Path(folder)
    return WorkerServices(
        store_in(home),
        SqlRecordStore(database_url, schema=schema),
        targets={AGENT: waiting_agent(home)},
        target_timeout_seconds=CASE_SECONDS,
    )


def build(folder: Path, runner: str, database_url: str, schema: str, mode: str) -> FastAPI:
    store = store_in(folder)
    records = SqlRecordStore(database_url, schema=schema)
    inner: Runner
    if runner == "in-process":
        inner = InProcessWorker(
            store, records, targets={AGENT: waiting_agent(folder)}, target_timeout_seconds=CASE_SECONDS
        )
    else:
        options = {"folder": str(folder), "database_url": database_url, "schema": schema}
        inner = ProcessWorker(services, records, agents=[AGENT], options=options)
    worker = QueueWorker(
        SqlWorkQueue(database_url, schema=schema),
        inner,
        records,
        in_process=mode == "serve",
        lease_seconds=LEASE_SECONDS,
        poll_seconds=POLL_SECONDS,
    )
    return create_app(store=store, records=records, principal=lambda: SINGLE_USER, worker=worker)


def main(folder: str, runner: str, database_url: str, schema: str, mode: str, port_file: str) -> None:
    import uvicorn

    app = build(Path(folder), runner, database_url, schema, mode)
    listener = listen(0)
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    written = Path(f"{port_file}.partial")
    written.write_text(str(listener.getsockname()[1]), encoding="utf-8")
    os.replace(written, port_file)
    server.run(sockets=[listener])


if __name__ == "__main__":
    # From the module by its import path, so that each run's process can import the setup too.
    from tests.evals.queue_fixture import main as serve

    serve(*sys.argv[1:7])
