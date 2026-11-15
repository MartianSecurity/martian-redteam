"""The evals core with a process worker, for ``tests/evals/test_no_outbound.py``. Nothing calls a model.

Run as ``python -m tests.evals.no_outbound_fixture FOLDER``, it does what a server that serves the
core does: it opens the store and the record store in ``FOLDER`` on SQLite, publishes the write
route tests' dataset, serves the core with a :class:`ProcessWorker` whose setup is
:func:`services`, starts a run of the write route tests' agent, a function target, through the
write route, and waits for the run's process (``python -m martian.evals.worker``) to end. It prints
the run as the read route returns it.

This module imports no MLflow before the core, as a server must not: MLflow decides when it is
imported whether its telemetry runs.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from martian.evals.model import Project
from martian.evals.records.sql import SqlRecordStore
from martian.evals.server import DEFAULT_PROJECT, SINGLE_USER, create_app
from martian.evals.store.mlflow_local import MlflowStore
from martian.evals.worker import ProcessWorker, WorkerServices
from tests.evals.write_fixture import AGENT, CASES, agent, request_id


def open_stores(folder: Path) -> tuple[MlflowStore, SqlRecordStore]:
    return MlflowStore(f"sqlite:///{folder / 'mlflow.db'}", folder / "artifacts"), SqlRecordStore(
        f"sqlite:///{folder / 'martian.db'}"
    )


def services(folder: str) -> WorkerServices:
    """The worker's setup: the stores in ``folder`` and the agent as a function target, no judge."""
    store, records = open_stores(Path(folder))
    return WorkerServices(store, records, targets={AGENT: agent})


def run_once(folder: str) -> dict[str, Any]:
    """Serve the core on the stores in ``folder``, run the agent once and return the run."""
    store, records = open_stores(Path(folder))
    asyncio.run(store.create_project(SINGLE_USER, Project(id=DEFAULT_PROJECT, name="Default")))
    asyncio.run(store.create_dataset(SINGLE_USER, DEFAULT_PROJECT, "support-faq", CASES))
    worker = ProcessWorker(services, records, agents=[AGENT], options={"folder": folder})
    app = create_app(store=store, records=records, principal=lambda: SINGLE_USER, worker=worker)
    with TestClient(app) as client:
        started = client.post(
            "/v1/evals", json={"agent_id": AGENT, "dataset_name": "support-faq", "request_id": request_id()}
        )
        started.raise_for_status()
        assert client.portal is not None
        client.portal.call(worker.drain)
        run: dict[str, Any] = client.get(f"/v1/evals/{started.json()['id']}").json()
    records.close()
    return run


if __name__ == "__main__":
    # Imported by its name, so that the worker can name the setup to each run's process.
    from tests.evals import no_outbound_fixture

    print(json.dumps(no_outbound_fixture.run_once(sys.argv[1])))
