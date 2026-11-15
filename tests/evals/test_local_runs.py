"""Run and run again from the local UI (MorningHQ/martian-notes#116): a launch from `martian ui` runs
in a process of its own against a local target, with a saved suite, and runs again with the same
settings. The target is a loopback HTTP endpoint the test starts; nothing calls a model."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from martian.evals.local import create_local_app, open_stores
from martian.evals.local_runs import LocalWorker
from martian.evals.local_targets import LocalTargets
from martian.evals.model import Case, Run, Target
from martian.evals.server import DEFAULT_PROJECT, SINGLE_USER
from tests.evals.local_fixture import DATASET, SUITE_SCORERS, FakeTarget, publish, save_suite

LOCAL = "http://127.0.0.1:7600"
RUNS = "/v1/local/runs"
KEY = f"sk-local-{uuid.uuid4().hex}"


@pytest.fixture(autouse=True)
def no_telemetry(monkeypatch):
    monkeypatch.setenv("MLFLOW_DISABLE_TELEMETRY", "true")
    monkeypatch.delenv("MLFLOW_WORKSPACE", raising=False)


@pytest.fixture
def ui(tmp_path) -> Path:
    folder = tmp_path / "dist-evals"
    folder.mkdir()
    (folder / "index.html").write_text("<!doctype html><title>Martian Evals</title>", encoding="utf-8")
    return folder


@pytest.fixture
def home(tmp_path) -> Path:
    return tmp_path / "home"


@pytest.fixture
def target() -> Iterator[FakeTarget]:
    with FakeTarget(KEY) as served:
        yield served


@pytest.fixture
def client(home, ui, monkeypatch) -> Iterator[TestClient]:
    """`martian ui`'s application, with the dataset published. The server's own process must never
    read a key: only each run's process does."""

    def refused(self: LocalTargets, target_id: str) -> str | None:
        raise AssertionError("the server's process read a target's key")

    publish(home)
    monkeypatch.setattr(LocalTargets, "key", refused)
    with TestClient(create_local_app(home, ui), base_url=LOCAL) as served:
        yield served


def call(client: TestClient, method: str, path: str, status: int, **kwargs: Any) -> Any:
    response = client.request(method, path, **kwargs)
    assert response.status_code == status, response.text
    return response.json() if response.content else None


def drain(client: TestClient) -> None:
    """Wait until every run's process, and its suite's scoring, has finished."""
    worker = client.app.state.martian_evals_worker  # type: ignore[attr-defined]
    assert isinstance(worker, LocalWorker) and client.portal is not None
    client.portal.call(worker.drain)


def launch(client: TestClient, target_id: str, suite: dict[str, Any] | None = None, **changes: Any) -> Any:
    body = {"request_id": str(uuid.uuid4()), "agent_id": target_id, "dataset_name": DATASET, **changes}
    if suite is not None:
        body["suite_version_id"] = suite["id"]
    return call(client, "POST", RUNS, 202, json=body)


def again(client: TestClient, run_id: str, status: int = 202, request_id: str | None = None) -> Any:
    return call(client, "POST", f"{RUNS}/{run_id}/again", status, json={"request_id": request_id or str(uuid.uuid4())})


def results(client: TestClient, run_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """The run with its results, and its workbench: the scoring jobs and their assessments."""
    return call(client, "GET", f"/v1/evals/{run_id}", 200), call(client, "GET", f"/v1/evals/{run_id}/workbench", 200)


def settings(run: dict[str, Any]) -> tuple[Any, ...]:
    """What run again keeps: the target, the dataset version, the suite version and the case limit."""
    suite = run["provenance"].get("suite") or {}
    return run["agent_id"], run["dataset_id"], suite.get("version_id"), run["max_items"]


def test_a_run_started_in_the_ui_runs_in_its_own_process_and_completes_with_results(client, target):
    defined = call(client, "POST", "/v1/local/targets", 201, json=target.definition())
    suite = save_suite(client, str(uuid.uuid4()))

    queued = launch(client, defined["id"], suite)
    assert (queued["state"], queued["agent_id"], queued["agent_name"]) == ("queued", defined["id"], "Support agent")
    # The suite's scoring job is there from the start, waiting for the run.
    waiting = call(client, "GET", f"/v1/evals/{queued['id']}/workbench", 200)["jobs"]
    assert [(job["state"], job["config"]["suite_version_id"]) for job in waiting] == [("queued", suite["id"])]
    drain(client)

    run, work = results(client, queued["id"])
    assert (run["state"], run["passed"], run["failed"], run["skipped"], run["errored"]) == ("completed", 1, 1, 1, 0)
    assert {item["dataset_item_id"]: (item["status"], item["output"]) for item in run["items"]} == {
        "faq-1": ("passed", "Use the reset link on the sign-in page."),
        "faq-2": ("failed", "I am not sure."),
        "faq-3": ("skipped", ""),
    }
    assert all(item["evals_trace_id"] for item in run["items"] if item["status"] != "skipped")
    # The run pins the target by its definition, with the key as a reference, and the suite.
    pinned = run["provenance"]["target"]
    assert (pinned["name"], pinned["endpoint"]["headers"]) == (
        "Support agent",
        {"Authorization": "Bearer ${MARTIAN_TARGET_KEY}"},
    )
    assert run["provenance"]["suite"] == {
        "id": suite["suite_id"],
        "version": 1,
        "version_id": suite["id"],
        "name": "Deterministic checks",
        "digest": suite["digest"],
    }
    # Once the run completed, its suite scored it in a process of its own: each scorer for each
    # case, and an error for the skipped case, which has no output.
    [job] = work["jobs"]
    assert (job["state"], job["config"]["suite_version_id"], job["config"]["scorers"]) == (
        "partial",
        suite["id"],
        SUITE_SCORERS,
    )
    assert (job["total"], job["succeeded"], job["errors"]) == (6, 4, 2)
    contains = [row["value"] for row in work["assessments"] if row["name"] == "contains_expected"]
    assert sorted(contains, key=repr) == [False, None, True]
    # The target got the key with each called case, from the run's process.
    assert [request["body"] for request in target.requests] == [
        {"message": "How do I reset my password?"},
        {"message": "Can I change my username?"},
    ]
    assert {request["headers"]["Authorization"] for request in target.requests} == {f"Bearer {KEY}"}


def test_run_again_repeats_a_finished_run_with_its_saved_settings(client, home, target):
    defined = call(client, "POST", "/v1/local/targets", 201, json=target.definition())
    suite = save_suite(client, str(uuid.uuid4()))
    first = launch(client, defined["id"], suite, max_items=2)
    drain(client)

    # Newer versions of the dataset and the suite do not change what run again repeats.
    publish(home, [Case(id="new-1", inputs={"message": "Anything else?"}, expectations={"expected_response": "No."})])
    save_suite(client, str(uuid.uuid4()), ["valid_json"], suite_id=suite["suite_id"], expected_version=1)

    request_id = str(uuid.uuid4())
    second = again(client, first["id"], request_id=request_id)
    assert second["id"] != first["id"]
    assert (second["run_code"], second["state"], second["provenance"]["repeat_of"]) == (
        "EV-1002",
        "queued",
        first["id"],
    )
    assert settings(second) == settings(first) == (defined["id"], first["dataset_id"], suite["id"], 2)
    assert again(client, first["id"], request_id=request_id)["id"] == second["id"]  # the same request again
    drain(client)

    run, work = results(client, second["id"])
    assert (run["state"], run["planned_count"], run["passed"], run["failed"]) == ("completed", 2, 1, 1)
    assert [job["config"]["suite_version_id"] for job in work["jobs"]] == [suite["id"]]
    assert len(target.requests) == 4

    # A run again of a run again keeps the same settings. A request_id names one request only.
    third = again(client, second["id"])
    assert settings(third) == settings(first)
    drain(client)
    used = {"detail": "request_id was already used for a different eval"}
    assert again(client, second["id"], 409, request_id=request_id) == used
    launched = {"request_id": request_id, "agent_id": defined["id"], "dataset_name": DATASET, "max_items": 2}
    assert call(client, "POST", RUNS, 409, json={**launched, "suite_version_id": suite["id"]}) == used


def test_a_launch_names_a_known_target_dataset_and_suite_and_is_idempotent(client, home, target):
    defined = call(client, "POST", "/v1/local/targets", 201, json=target.definition())
    suite = save_suite(client, str(uuid.uuid4()))
    unknown = str(uuid.uuid4())
    body = {"agent_id": defined["id"], "dataset_name": DATASET}
    refusals = [
        ({**body, "agent_id": unknown}, 404, "Target not found"),
        ({**body, "dataset_name": "missing"}, 422, "Unknown dataset"),
        ({**body, "suite_version_id": unknown}, 404, "Suite version not found"),
    ]
    for sent, status, detail in refusals:
        assert call(client, "POST", RUNS, status, json={**sent, "request_id": str(uuid.uuid4())}) == {"detail": detail}
    judged = save_suite(client, str(uuid.uuid4()), ["correctness"])
    assert call(client, "POST", RUNS, 422, json={**body, "suite_version_id": judged["id"]}) == {
        "detail": "Model judges require a judge configured for this installation's worker"
    }

    request_id = str(uuid.uuid4())
    sent = {**body, "request_id": request_id, "suite_version_id": suite["id"]}
    first = call(client, "POST", RUNS, 202, json=sent)
    assert call(client, "POST", RUNS, 202, json=sent)["id"] == first["id"]
    used = {"detail": "request_id was already used for a different eval"}
    assert call(client, "POST", RUNS, 409, json={**sent, "suite_version_id": None}) == used
    drain(client)
    assert again(client, first["id"], 409, request_id=request_id) == used  # a launch's request_id

    # One run at a time, and run again repeats only a finished one.
    running = _run(kind="agent", agent_id=defined["id"], state="running")
    _record(home, running)
    assert call(client, "POST", RUNS, 409, json={**body, "request_id": str(uuid.uuid4())}) == {
        "detail": "An eval run is already in progress"
    }
    assert again(client, running.id, 409) == {"detail": "Run again repeats a finished run; this one has not finished"}


def _run(*, state: str = "completed", **target: Any) -> Run:
    """A run recorded without the worker, with ``target``."""
    return Run(
        id=str(uuid.uuid4()),
        project_id=DEFAULT_PROJECT,
        code="EV-9001",
        source="local" if target["kind"] == "http" else "console",
        state=state,  # type: ignore[arg-type]
        target=Target(**target),
        dataset_name=DATASET,
        max_cases=None if target["kind"] == "http" else 100,
        created_at=datetime.now(UTC),
    )


def _record(home: Path, run: Run) -> None:
    _, records = open_stores(home)
    try:
        asyncio.run(records.create_run(SINGLE_USER, run))
    finally:
        records.close()


def test_run_again_needs_a_run_started_in_the_ui_whose_target_is_still_defined(client, home, target):
    assert again(client, str(uuid.uuid4()), 404) == {"detail": "Not found"}
    # A run `martian evals run` recorded: a suite file's URL target, and no saved launch.
    recorded = _run(kind="http", url="http://127.0.0.1:9/chat", body={"message": "{{input}}"})
    _record(home, recorded)
    assert again(client, recorded.id, 422) == {
        "detail": "This run was not started in the UI, so it has no saved settings to run again"
    }

    defined = call(client, "POST", "/v1/local/targets", 201, json=target.definition())
    finished = launch(client, defined["id"])
    drain(client)
    assert call(client, "DELETE", f"/v1/local/targets/{defined['id']}", 204) is None
    assert again(client, finished["id"], 404) == {"detail": "Target not found"}


def test_a_run_that_does_not_complete_cancels_its_suites_scoring(client, target):
    defined = call(client, "POST", "/v1/local/targets", 201, json=target.definition())
    queued = launch(client, defined["id"], save_suite(client, str(uuid.uuid4())))
    call(client, "POST", f"/v1/evals/{queued['id']}/cancel", 202, json={})
    drain(client)
    run, work = results(client, queued["id"])
    [job] = work["jobs"]
    assert (run["state"], job["state"], job["error"]) == (
        "cancelled",
        "cancelled",
        "The run did not complete, so its suite did not score it",
    )
    assert not [row for row in work["assessments"] if row["job_id"] == job["id"]]


def test_the_frozen_launch_runs_too_with_exact_match_only(client, target):
    defined = call(client, "POST", "/v1/local/targets", 201, json=target.definition())
    body = {"request_id": str(uuid.uuid4()), "agent_id": defined["id"], "dataset_name": DATASET}
    queued = call(client, "POST", "/v1/evals", 202, json=body)
    drain(client)
    run, work = results(client, queued["id"])
    assert (run["state"], run["passed"], "suite" in run["provenance"], work["jobs"]) == ("completed", 1, False, [])
    assert settings(again(client, queued["id"])) == settings(run)
    drain(client)


def test_a_case_the_target_refuses_is_an_error_that_names_no_key(client, target):
    defined = call(client, "POST", "/v1/local/targets", 201, json=target.definition())
    target.key = "rotated"  # the target no longer takes the key the user gave
    queued = launch(client, defined["id"])
    drain(client)
    run, _ = results(client, queued["id"])
    assert (run["state"], run["errored"]) == ("completed", 2)
    errors = {item["error"] for item in run["items"] if item["status"] == "error"}
    assert errors == {"TargetCallError: the target returned HTTP 401"}
    assert {request["headers"]["Authorization"] for request in target.requests} == {f"Bearer {KEY}"}
