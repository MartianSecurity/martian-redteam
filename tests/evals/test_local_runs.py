"""Run and run again from the local UI (MorningHQ/martian-notes#116): a launch from `martian ui` runs
in a process of its own against a local target, with a saved suite, and runs again with the same
settings. The target is a loopback HTTP endpoint the test starts, and the judge's model a loopback
OpenAI-compatible endpoint; nothing calls a real model."""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from martian.evals.judges import RESPONSE_RUBRICS_VERSION
from martian.evals.local import create_local_app, open_stores
from martian.evals.local_runs import INTERRUPTED, LocalWorker, held, open_queue
from martian.evals.local_targets import LocalTargets
from martian.evals.model import Case, Run, Suite, Target, digest
from martian.evals.run_record import RunRecorder
from martian.evals.server import DEFAULT_PROJECT, SINGLE_USER
from martian.evals.worker.scorers import builtin_scorer
from tests.evals.local_fixture import DATASET, SUITE_SCORERS, FakeTarget, publish, save_suite
from tests.evals.test_worker_judges import KEY as JUDGE_KEY
from tests.evals.test_worker_judges import REPORTED, Loopback, model_answer

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


def test_a_run_started_in_the_ui_runs_in_its_own_process_and_completes_with_results(client, home, target):
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
    # The run pins the target as the worker pins an HTTP target, by a digest of it as written, with
    # the key as a reference, and by its name; and it pins the suite.
    written = LocalTargets.model_target(LocalTargets(home), LocalTargets(home).get(defined["id"]))
    assert written.headers == {"Authorization": "Bearer ${MARTIAN_TARGET_KEY}"}
    assert run["provenance"]["target"] == {
        "kind": "http",
        "agent_id": defined["id"],
        "digest": digest(written.model_dump(mode="json")),
        "name": "Support agent",
    }
    assert run["provenance"]["suite"] == {
        "id": suite["suite_id"],
        "version": 1,
        "version_id": suite["id"],
        "name": "Deterministic checks",
        "digest": suite["digest"],
        "model_judges": False,
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
    # A suite with a model judge calls a model for each judged case: only with the cost acknowledged.
    judged = save_suite(client, str(uuid.uuid4()), ["correctness"])
    assert call(client, "POST", RUNS, 422, json={**body, "suite_version_id": judged["id"]}) == {
        "detail": "Model scoring requires cost acknowledgement"
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
        state=state,
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
    assert errors == {"the target returned HTTP 401; check the credentials in target.headers"}
    assert {request["headers"]["Authorization"] for request in target.requests} == {f"Bearer {KEY}"}


@pytest.fixture
def judge_model(monkeypatch) -> Iterator[Loopback]:
    """A loopback OpenAI-compatible endpoint for the judge's model. Its key is in the environment
    `martian ui` runs in, which each run's process inherits; the judge reads it there."""
    monkeypatch.setenv("OPENAI_API_KEY", JUDGE_KEY)
    monkeypatch.setenv("OPENAI_API_BASE", "http://127.0.0.1:9/v1")  # the judge must use the endpoint asked
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    server = Loopback(model_answer)
    yield server
    server.close()


def test_a_suite_with_a_model_judge_runs_on_the_sdk_judge_and_again(client, target, judge_model):
    defined = call(client, "POST", "/v1/local/targets", 201, json=target.definition())
    default = {"model": "openai:/fixture-judge", "endpoint": f"{judge_model.origin}/v1"}
    call(client, "PUT", "/v1/evals/judge-model", 200, json=default)
    suite = save_suite(client, str(uuid.uuid4()), ["correctness", "exact_match"])
    queued = launch(client, defined["id"], suite, acknowledge_model_cost=True)
    assert queued["provenance"]["suite"]["model_judges"] is True
    drain(client)

    run, work = results(client, queued["id"])
    [job] = work["jobs"]
    assert (run["state"], run["passed"], run["failed"], job["state"]) == ("completed", 1, 1, "partial")
    judged = [row for row in work["assessments"] if row["name"] == "correctness"]
    # Two cases had outputs and the loopback model passed both; the skipped case has none.
    assert sorted(repr(row["value"]) for row in judged) == ["None", "True", "True"]
    assert {row["source_id"] for row in judged if row["value"] is not None} == {f"openai:/fixture-judge@{REPORTED}"}
    assert {row["provenance"].get("martian.judge.model_version") for row in judged if row["value"]} == {REPORTED}
    assert {row["provenance"].get("martian.judge.version") for row in judged if row["value"]} == {
        RESPONSE_RUBRICS_VERSION
    }
    # The judge's model was called from the scoring job's process, once per judged case, at the
    # endpoint the default names, with the provider's key from the environment.
    assert [request["path"] for request in judge_model.requests] == ["/v1/chat/completions"] * 2
    assert {request["headers"]["authorization"] for request in judge_model.requests} == {f"Bearer {JUDGE_KEY}"}

    # Run again repeats it, model judges included, once the cost is acknowledged again.
    refused = {"detail": "Model scoring requires cost acknowledgement"}
    assert again(client, queued["id"], 422) == refused
    repeated = call(
        client,
        "POST",
        f"{RUNS}/{queued['id']}/again",
        202,
        json={"request_id": str(uuid.uuid4()), "acknowledge_model_cost": True},
    )
    drain(client)
    _, work = results(client, repeated["id"])
    assert [job["state"] for job in work["jobs"]] == ["partial"]
    assert len(judge_model.requests) == 4


# --- runs whose process stopped (MorningHQ/martian-notes#333, #139) --------------------------

HOLD = (
    "import asyncio, sys; from pathlib import Path; from martian.evals.local_runs import RunHold; "
    "hold = asyncio.run(RunHold.take(Path(sys.argv[1]), sys.argv[2])); print('held', flush=True); sys.stdin.read()"
)
"""A process that holds a run of a data folder, as `martian evals run` does while it records one."""


def _until(check: Callable[[], Any], what: str, timeout: float = 120.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if found := check():
            return found
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {what}")


def _listening(http: httpx.Client) -> bool:
    try:
        return http.get("/v1/local/targets").status_code == 200
    except httpx.TransportError:
        return False


def _stored(home: Path, run_id: str) -> Run:
    _, records = open_stores(home)
    try:
        found = asyncio.run(records.get_run(SINGLE_USER, DEFAULT_PROJECT, run_id))
    finally:
        records.close()
    assert found is not None
    return found


def test_a_run_whose_martian_ui_was_stopped_is_taken_again_when_it_starts_again(home, ui, target, tmp_path):
    """`martian ui` is killed, as a crash or a power cut would, while the run waits on its target,
    and its run's process ends with it. Started again, `martian ui` takes the run again once its
    lease runs out, runs it from the start in a new process, and scores it with its suite."""
    publish(home)
    target.gate = threading.Event()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    environment = {name: value for name, value in os.environ.items() if name != "MLFLOW_WORKSPACE"}
    command = [sys.executable, "-m", "martian", "ui", "--port", str(port), "--data-dir", str(home)]
    with (tmp_path / "ui.log").open("w") as output:
        # A process group of its own, to kill it with; the run's process has a session of its own.
        server = subprocess.Popen(
            [*command, "--ui-dir", str(ui), "--no-open"],
            env={**environment, "MLFLOW_DISABLE_TELEMETRY": "true"},
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30) as http:
                _until(lambda: _listening(http), "martian ui to listen")
                defined = call(http, "POST", "/v1/local/targets", 201, json=target.definition())
                suite = save_suite(http, str(uuid.uuid4()))
                queued = launch(http, defined["id"], suite)
                _until(lambda: target.requests, "the run's process to call the target")
            assert _stored(home, queued["id"]).state == "running"
            assert held(home, queued["id"])
        finally:
            os.killpg(server.pid, signal.SIGKILL)
            server.wait(30)
    # Nothing renews the run's claim any more; the record still says the run is running.
    _until(lambda: not held(home, queued["id"]), "the run's lease to run out", timeout=30)
    assert _stored(home, queued["id"]).state == "running"
    target.gate.set()

    with TestClient(create_local_app(home, ui), base_url=LOCAL) as client:
        drain(client)
        run, work = results(client, queued["id"])
        assert (run["state"], run["error"], run["passed"], run["failed"]) == ("completed", "", 1, 1)
        [job] = work["jobs"]
        assert (job["state"], job["succeeded"], job["errors"]) == ("partial", 4, 2)
    # The first process asked once and ended with the killed server; the new one asked each case.
    assert [request["body"]["message"] for request in target.requests] == [
        "How do I reset my password?",
        "How do I reset my password?",
        "Can I change my username?",
    ]


def test_a_run_another_live_process_holds_is_left_alone_until_cancelled_once_it_is_gone(home, ui, target):
    publish(home)
    # A run `martian evals run` is recording into the same data folder, from a process of its own.
    recorded = _run(kind="http", url=target.url, body={"message": "{{input}}"}, state="running")
    _record(home, recorded)
    holder = subprocess.Popen(
        [sys.executable, "-c", HOLD, str(home), recorded.id], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "held"
        with TestClient(create_local_app(home, ui), base_url=LOCAL) as client:
            # Started meanwhile, the UI leaves the run running, and admits no other run beside it.
            assert call(client, "GET", f"/v1/evals/{recorded.id}", 200)["state"] == "running"
            defined = call(client, "POST", "/v1/local/targets", 201, json=target.definition())
            body = {"agent_id": defined["id"], "dataset_name": DATASET}
            assert call(client, "POST", RUNS, 409, json={**body, "request_id": str(uuid.uuid4())}) == {
                "detail": "An eval run is already in progress"
            }
            # Cancelling it asks that process to stop; it is still running.
            asked = call(client, "POST", f"/v1/evals/{recorded.id}/cancel", 202, json={})
            assert (asked["state"], asked["error"], asked["cancel_requested_at"] is not None) == ("running", "", True)

            # Once that process is gone and its lease ran out, cancelling the run again finishes it,
            # if the UI's worker has not finished it first, and a new run is admitted.
            holder.kill()
            holder.wait(30)
            _until(lambda: not held(home, recorded.id), "the recording's lease to run out", timeout=30)
            stopped = call(client, "POST", f"/v1/evals/{recorded.id}/cancel", 202, json={})
            assert (stopped["state"], stopped["error"]) == ("cancelled", INTERRUPTED)
            assert stopped["finished_at"] is not None
            assert stopped["cancel_requested_at"] == asked["cancel_requested_at"]
            launch(client, defined["id"])
            drain(client)
    finally:
        holder.kill()
        holder.wait(30)


def test_runs_no_process_holds_are_finished_as_interrupted_when_martian_ui_starts(home, ui):
    """Runs an earlier release left queued or running are not in the queue at all: nothing holds them."""
    publish(home)
    agent = {"kind": "agent", "agent_id": str(uuid.uuid4())}
    queued, running = _run(**agent, state="queued"), _run(**agent, state="running")
    asked = _run(**agent, state="running").model_copy(update={"cancel_requested_at": datetime.now(UTC)})
    for run in (queued, running, asked):
        _record(home, run)
    with TestClient(create_local_app(home, ui), base_url=LOCAL) as client:
        found = {run.id: call(client, "GET", f"/v1/evals/{run.id}", 200) for run in (queued, running, asked)}
    assert {run_id: (run["state"], run["error"]) for run_id, run in found.items()} == {
        queued.id: ("failed", INTERRUPTED),
        running.id: ("failed", INTERRUPTED),
        asked.id: ("cancelled", INTERRUPTED),
    }


def test_martian_evals_run_holds_its_run_while_it_records_it(home):
    suite = Suite(id="evals", name="evals", scorers=[builtin_scorer("exact_match")], digest=digest({"suite": 1}))
    with RunRecorder(home, suite) as recorder:
        run = recorder.start(planned=0, total=0, limit=None, reused=[])
        assert held(home, run.id)
        recorder.finish()
    assert not held(home, run.id)
    queue = open_queue(home)
    assert asyncio.run(queue.pending()) == 0
    queue.close()
