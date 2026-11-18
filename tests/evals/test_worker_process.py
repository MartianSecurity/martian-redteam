"""One process per evaluation run (MartianSecurity/martian-notes#140).

Two organisations' runs in flight at once, each calling an agent traced with MLflow tracing, keep
every trace in their own organisation's workspace when each run has a process of its own. The
same work on two threads of one process does not: MLflow's workspace and active experiment are
the process's, so one organisation's traces land in the other's workspace.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from martian.evals.model import Project, Target, TraceQuery
from martian.evals.server import DEFAULT_PROJECT, Principal, create_app, organisation_principal
from martian.evals.store import StoreError
from martian.evals.store.mlflow_local import MlflowStore, workspace_name
from martian.evals.worker import ProcessWorker, Work, Worker, WorkerServices, dump_work, load_work
from martian.evals.worker.process import ENDED_EARLY, WORKSPACE_ENV, WRONG_WORKSPACE
from tests.evals.process_fixture import ORGANISATIONS, broken, cases, member, open_stores, services
from tests.evals.write_fixture import AGENT, Inert, request_id

PROCESS_SECONDS = 300


def _publish(store: MlflowStore, organisations: tuple[str, ...] = ORGANISATIONS) -> None:
    for organisation in organisations:
        principal = member(organisation)
        asyncio.run(store.create_project(principal, Project(id=DEFAULT_PROJECT, name="Default")))
        asyncio.run(store.create_dataset(principal, DEFAULT_PROJECT, "support-faq", cases(organisation)))


def _launch(client: TestClient, holder: dict[str, Principal], organisation: str) -> str:
    holder["current"] = member(organisation)
    response = client.post(
        "/v1/evals", json={"agent_id": AGENT, "dataset_name": "support-faq", "request_id": request_id()}
    )
    assert response.status_code == 202, response.text
    run_id: str = response.json()["id"]
    return run_id


def _owner(text: str) -> str | None:
    return next((organisation for organisation in ORGANISATIONS if organisation in text), None)


def _where_traces_landed(store: MlflowStore) -> dict[str, list[tuple[str | None, str]]]:
    """Every trace in each organisation's workspace, as (the organisation its request names, whether
    the agent's MLflow tracing or the worker recorded it)."""
    landed = {}
    for organisation in ORGANISATIONS:
        query = TraceQuery(project_id=DEFAULT_PROJECT, limit=100)
        page = asyncio.run(store.search_traces(member(organisation), query))
        landed[organisation] = sorted(
            (_owner(trace.request), "agent" if trace.name == "agent" else "case") for trace in page.items
        )
    return landed


@pytest.mark.parametrize(
    "mode",
    [
        "processes",
        pytest.param(
            "threads",
            marks=pytest.mark.xfail(
                strict=True,
                raises=AssertionError,
                reason="MLflow keeps the workspace and the active experiment for the whole process",
            ),
        ),
    ],
)
def test_two_organisations_at_once_keep_every_trace_in_their_own_workspace(tmp_path: Path, mode: str) -> None:
    store, records = open_stores(tmp_path)
    _publish(store)
    holder = {"current": member(ORGANISATIONS[0])}
    worker: Worker
    if mode == "processes":
        worker = ProcessWorker(services, records, agents=[AGENT], judges=True, options={"folder": str(tmp_path)})
    else:
        worker = Inert()
    app = create_app(store=store, records=records, principal=lambda: holder["current"], worker=worker)
    with TestClient(app) as client:
        runs = {organisation: _launch(client, holder, organisation) for organisation in ORGANISATIONS}
        if isinstance(worker, ProcessWorker):
            assert client.portal is not None
            client.portal.call(worker.drain)
        else:
            assert isinstance(worker, Inert)
            works = [json.dumps(dump_work(work)) for work in worker.submitted]
            subprocess.run(
                [sys.executable, "-m", "tests.evals.process_fixture", str(tmp_path), *works],
                check=True,
                timeout=PROCESS_SECONDS,
            )
        for organisation, run_id in runs.items():
            holder["current"] = member(organisation)
            run = client.get(f"/v1/evals/{run_id}").json()
            assert (run["state"], run["passed"]) == ("completed", 3), run
    expected = {
        organisation: sorted([(organisation, "agent")] * 3 + [(organisation, "case")] * 3)
        for organisation in ORGANISATIONS
    }
    assert _where_traces_landed(store) == expected
    records.close()


def test_work_whose_process_exits_with_an_error_is_failed_not_left_queued(tmp_path: Path) -> None:
    store, records = open_stores(tmp_path)
    _publish(store, ORGANISATIONS[:1])
    worker = ProcessWorker(broken, records, agents=[AGENT], options={"folder": str(tmp_path)})
    holder = {"current": member(ORGANISATIONS[0])}
    app = create_app(store=store, records=records, principal=lambda: holder["current"], worker=worker)
    with TestClient(app) as client:
        run_id = _launch(client, holder, ORGANISATIONS[0])
        assert client.portal is not None
        client.portal.call(worker.drain)
    run = asyncio.run(records.get_run(member(ORGANISATIONS[0]), DEFAULT_PROJECT, run_id))
    assert run is not None and (run.state, run.error) == ("failed", ENDED_EARLY)
    records.close()


def test_a_runs_process_refuses_work_under_another_organisations_workspace(tmp_path: Path) -> None:
    work = Work(kind="run", principal=member("org-a"), project_id=DEFAULT_PROJECT, run_id=str(uuid.uuid4()))
    request = {"setup": "tests.evals.process_fixture:services", "options": {"folder": str(tmp_path)}}
    result = subprocess.run(
        [sys.executable, "-m", "martian.evals.worker"],
        input=json.dumps({**request, "work": dump_work(work)}),
        env={**os.environ, WORKSPACE_ENV: workspace_name(member("org-b"))},
        capture_output=True,
        text=True,
        timeout=PROCESS_SECONDS,
    )
    assert (result.returncode, WRONG_WORKSPACE in result.stderr) == (2, True), result.stderr
    assert not (tmp_path / "mlflow.db").exists()  # refused before the setup ran


def _nested() -> Any:
    def setup() -> WorkerServices:
        raise AssertionError("never called")

    return setup


async def test_the_process_worker_knows_its_agents_and_takes_only_a_setup_another_process_can_import(
    tmp_path: Path,
) -> None:
    _, records = open_stores(tmp_path)
    worker = ProcessWorker(services, records, agents=[AGENT], judges=True)
    assert isinstance(worker, Worker) and worker.judges
    principal = member("org-a")
    pinned = await worker.check_target(principal, Target(kind="agent", agent_id=AGENT))
    assert pinned == {"kind": "agent", "agent_id": AGENT}
    with pytest.raises(StoreError) as refused:
        await worker.check_target(principal, Target(kind="agent", agent_id=str(uuid.uuid4())))
    assert (refused.value.kind, refused.value.detail) == ("not_found", "Agent not found")
    assert not ProcessWorker(services, records).judges
    for setup in (lambda: None, _nested()):
        with pytest.raises(TypeError, match="module-level function"):
            ProcessWorker(setup, records)
    with pytest.raises(TypeError):
        ProcessWorker(services, records, options={"folder": object()})  # options travel as JSON
    named = organisation_principal(
        "user-1", "org-a", frozenset({"member", "admin"}), email="a@example.com", organisation_name="A"
    )
    work = Work(kind="scoring", principal=named, project_id=DEFAULT_PROJECT, run_id="run-1", job_id="job-1")
    assert load_work(json.loads(json.dumps(dump_work(work)))) == work
    records.close()
