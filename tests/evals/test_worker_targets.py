"""HTTP targets called from the worker, and registered agents through martian-agents
(MartianSecurity/martian-notes#137).

A run of an HTTP target needs no martian-agents service: the worker POSTs each case to the
target itself, once, with no retry, and fills in the target's credential from its own
``Credentials`` in the process that calls it, never from the launch request or a record. A
registered agent still runs through the worker's ``AgentExecutor``, the martian-agents path,
and never over the direct HTTP path. The targets here are a tiny HTTP server on loopback and a
fake martian-agents; nothing calls a model.

A run calls only the target it was admitted with: before it calls its target, on its first start
and on every resume, a worker that now has another target under the run's id fails the run
without calling it (MartianSecurity/martian-notes#344).
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from martian.evals.model import Case, Run, RunLimits, Target, digest
from martian.evals.records.queue import SqlWorkQueue
from martian.evals.records.sql import SqlRecordStore
from martian.evals.server import COMMUNITY, DEFAULT_PROJECT, SINGLE_USER, Principal, create_app
from martian.evals.server.launch import admit_run, resume_run
from martian.evals.store import EvalStore, MemoryStore
from martian.evals.store.mlflow_local import MlflowStore
from martian.evals.worker import Credentials, InProcessWorker, ProcessWorker, QueueWorker, Runner, Worker
from martian.evals.worker.targets import http_call
from tests.evals.process_fixture import (
    HTTP_TARGET,
    KEY_NAME,
    LEASE_PREFIX,
    REGISTERED_AGENT,
    FakeMartianAgents,
    agent_services,
    http_services,
    member,
    open_stores,
)
from tests.evals.write_fixture import ANSWERS, Served, agent, publish, request_id, serve

KEY = "test-target-key-5f1c0e"
CALLED_CASES = 2  # support-faq's third case has no expected text and is skipped before any call
UNKNOWN_AGENT = str(uuid.UUID(int=10))


class Endpoint:
    """A tiny HTTP target on loopback. It answers a POST that carries the key with ``status`` and
    the write route tests' agent's answer under ``reply``, after ``delay`` seconds, and one that
    does not with 401. A request whose message is ``hold`` waits until :meth:`release`. It keeps
    every request it was sent."""

    def __init__(self, *, status: int = 200, delay: float = 0.0, hold: str | None = None) -> None:
        self.status, self.delay, self.hold = status, delay, hold
        self.requests: list[dict[str, Any]] = []
        self._released = threading.Event()
        endpoint = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - the name http.server calls
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                endpoint.requests.append(
                    {"path": self.path, "authorization": self.headers.get("Authorization"), "body": body}
                )
                if endpoint.hold is not None and body.get("message") == endpoint.hold:
                    endpoint._released.wait(60)
                time.sleep(endpoint.delay)
                status = endpoint.status if self.headers.get("Authorization") == f"Bearer {KEY}" else 401
                reply = json.dumps({"reply": ANSWERS.get(body.get("message"), "I am not sure.")}).encode()
                self.send_response(status)
                if 300 <= status < 400:
                    self.send_header("Location", "/elsewhere")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - the base class's name
                pass

        class Server(ThreadingHTTPServer):
            def handle_error(self, request: Any, client_address: Any) -> None:
                pass  # a client that stopped waiting leaves a broken pipe behind

        self._server = Server(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host!s}:{port}/chat"

    def release(self) -> None:
        """Answer the held requests, and every later one at once."""
        self._released.set()

    def close(self) -> None:
        self.release()
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def endpoint() -> Iterator[Endpoint]:
    server = Endpoint()
    yield server
    server.close()


@pytest.fixture
def records(tmp_path: Path) -> Iterator[SqlRecordStore]:
    store = SqlRecordStore(f"sqlite:///{tmp_path / 'martian.db'}")
    yield store
    store.close()


def http_target(url: str) -> Target:
    """The shared model's HTTP target, as a run keeps it: the key is ``${TARGET_KEY}``, unexpanded."""
    return Target(
        kind="http",
        url=url,
        body={"message": "{{input}}", "request": "{{uuid}}"},
        response_path="reply",
        headers={"Authorization": f"Bearer ${{{KEY_NAME}}}"},
    )


def keys(asked: list[tuple[Principal, str, str]]) -> Credentials:
    """The worker's credentials: ``KEY`` for the HTTP target's ``${TARGET_KEY}``, nothing else."""

    def credentials(principal: Principal, target_id: str, name: str, /) -> str | None:
        asked.append((principal, target_id, name))
        return KEY if (target_id, name) == (HTTP_TARGET, KEY_NAME) else None

    return credentials


def finished(core: Served, agent_id: str) -> dict[str, Any]:
    """Launch a run of ``agent_id`` through the launch route, wait for it, and read it back."""
    run = core.launch(agent_id=agent_id)
    assert KEY not in json.dumps(run)
    core.drain()
    detail: dict[str, Any] = core.call("GET", f"/v1/evals/{run['id']}", 200)
    return detail


def called(detail: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in detail["items"] if item["status"] != "skipped"]


def test_an_http_target_is_called_once_per_case_with_the_credential_the_worker_supplies(records, endpoint):
    asked: list[tuple[Principal, str, str]] = []
    target = http_target(endpoint.url)
    client, core = serve(MemoryStore(), records, targets={HTTP_TARGET: target}, credentials=keys(asked))
    with client:
        run = finished(core, HTTP_TARGET)
    assert (run["state"], run["passed"], run["failed"], run["skipped"], run["errored"]) == ("completed", 1, 1, 1, 0)
    assert run["provenance"]["target"] == {
        "kind": "http",
        "agent_id": HTTP_TARGET,
        "digest": digest(target.model_dump(mode="json")),
    }
    sent = endpoint.requests
    assert [request["body"]["message"] for request in sent] == [
        "How do I reset my password?",
        "Can I change my username?",
    ]
    assert {request["authorization"] for request in sent} == {f"Bearer {KEY}"}
    assert len({request["body"]["request"] for request in sent}) == CALLED_CASES  # {{uuid}} is new per case
    assert asked == [(SINGLE_USER, HTTP_TARGET, KEY_NAME)] * CALLED_CASES
    # The key reached the target and nothing else: not the run, its results or their traces.
    traces = [core.call("GET", f"/v1/evals/{run['id']}/items/{item['id']}/trace", 200) for item in called(run)]
    assert KEY not in json.dumps(run) + json.dumps(traces)


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, "the target returned HTTP 401; check the credentials in target.headers"),
        (404, "the target returned HTTP 404; check target.url"),
        (422, "the target returned HTTP 422; check target.body"),
        (500, "the target returned HTTP 500"),
        (307, "the target returned HTTP 307"),
    ],
)
def test_a_response_that_is_not_2xx_is_each_cases_error_naming_the_setting_with_no_retry(
    records, endpoint, status, error
):
    endpoint.status = status
    client, core = serve(MemoryStore(), records, targets={HTTP_TARGET: http_target(endpoint.url)}, credentials=keys([]))
    with client:
        run = finished(core, HTTP_TARGET)
    assert (run["state"], run["errored"]) == ("completed", CALLED_CASES)
    assert [(item["status"], item["error"]) for item in called(run)] == [("error", error)] * CALLED_CASES
    assert len(endpoint.requests) == CALLED_CASES  # once per case, never again
    assert {request["path"] for request in endpoint.requests} == {"/chat"}  # a redirect is not followed


def test_a_target_slower_than_the_workers_bound_times_out_with_no_retry(records):
    slow = Endpoint(delay=3)
    try:
        client, core = serve(
            MemoryStore(),
            records,
            targets={HTTP_TARGET: http_target(slow.url)},
            credentials=keys([]),
            target_timeout_seconds=0.3,
        )
        with client:
            run = finished(core, HTTP_TARGET)
        assert [(item["status"], item["error"]) for item in called(run)] == [("error", "item timed out")] * 2
        assert len(slow.requests) == CALLED_CASES
    finally:
        slow.close()


def _closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


def test_a_missing_credential_is_each_cases_error_naming_the_setting_and_nothing_is_sent(records, endpoint):
    client, core = serve(MemoryStore(), records, targets={HTTP_TARGET: http_target(endpoint.url)})  # no credentials
    with client:
        run = finished(core, HTTP_TARGET)
    assert [item["error"] for item in called(run)] == [
        f"target.headers.Authorization needs the credential {KEY_NAME}"
    ] * CALLED_CASES
    assert endpoint.requests == []


def test_an_unreachable_target_is_each_cases_error_naming_the_setting(records):
    nowhere = http_target(f"http://127.0.0.1:{_closed_port()}/chat")
    client, core = serve(MemoryStore(), records, targets={HTTP_TARGET: nowhere}, credentials=keys([]))
    with client:
        run = finished(core, HTTP_TARGET)
    assert [item["error"] for item in called(run)] == [
        "the target could not be reached: ConnectError; check target.url"
    ] * CALLED_CASES


def test_registered_agents_run_through_martian_agents_and_never_over_direct_http(records, endpoint, tmp_path):
    martian_agents = FakeMartianAgents(tmp_path)
    client, core = serve(
        MemoryStore(),
        records,
        targets={HTTP_TARGET: http_target(endpoint.url)},
        credentials=keys([]),
        executor=martian_agents,
    )
    with client:
        run = finished(core, REGISTERED_AGENT)
        assert (run["state"], run["passed"], run["failed"]) == ("completed", 1, 1)
        assert run["provenance"]["target"]["target_id"] == f"agent-{REGISTERED_AGENT}"
        chats = martian_agents.chats()
        assert [(chat["target_id"], chat["conversation_id"]) for chat in chats] == [
            (f"agent-{REGISTERED_AGENT}", f"eval:{run['id']}:faq-1"),
            (f"agent-{REGISTERED_AGENT}", f"eval:{run['id']}:faq-2"),
        ]
        assert endpoint.requests == []  # a registered agent never goes over the direct path
        assert LEASE_PREFIX not in json.dumps(run)  # each call's lease stays with martian-agents
        # An HTTP target the worker knows goes direct, never through martian-agents.
        assert finished(core, HTTP_TARGET)["passed"] == 1
        assert len(endpoint.requests) == CALLED_CASES and len(martian_agents.chats()) == CALLED_CASES
        unknown = core.client.post("/v1/evals", json={"agent_id": UNKNOWN_AGENT, "dataset_name": "support-faq"})
    assert (unknown.status_code, unknown.json()["detail"]) == (404, "Agent not found")


async def test_a_worker_calls_a_target_itself_only_when_it_is_an_http_target(records, tmp_path):
    function = Target(kind="function", file="agent.py", function="target")
    with pytest.raises(ValueError, match="only when it is an http target"):
        InProcessWorker(MemoryStore(), records, targets={HTTP_TARGET: function})
    with pytest.raises(ValueError, match="both an agent and an http target"):
        ProcessWorker(http_services, records, agents=[HTTP_TARGET], targets={HTTP_TARGET: http_target("http://x")})
    worker = ProcessWorker(
        http_services,
        records,
        targets={HTTP_TARGET: http_target("http://127.0.0.1:9/chat")},
        executor=FakeMartianAgents(tmp_path),
    )
    principal = member("org-a")
    pinned = await worker.check_target(principal, Target(kind="agent", agent_id=HTTP_TARGET))
    assert pinned["kind"] == "http" and KEY_NAME not in json.dumps(pinned)
    registered = await worker.check_target(principal, Target(kind="agent", agent_id=REGISTERED_AGENT))
    assert registered["target_id"] == f"agent-{REGISTERED_AGENT}"


async def test_a_worker_refuses_a_response_path_that_indexes_a_list_with_other_digits(records):
    wrong = http_target("http://127.0.0.1:9/chat").model_copy(update={"response_path": "choices.².text"})
    problem = rf"targets\['{HTTP_TARGET}'\]\.response_path indexes lists with the digits 0-9 only; step 2"
    # Refused each time a check or a run names the target, so one wrong target never stops the others.
    for worker in (
        InProcessWorker(MemoryStore(), records, targets={HTTP_TARGET: wrong}),
        ProcessWorker(http_services, records, targets={HTTP_TARGET: wrong}),
    ):
        with pytest.raises(ValueError, match=problem):
            await worker.check_target(SINGLE_USER, Target(kind="agent", agent_id=HTTP_TARGET))
    with pytest.raises(ValueError, match=problem):
        http_call(SINGLE_USER, HTTP_TARGET, wrong, credentials=None, timeout_seconds=1)


# --- one process per run ------------------------------------------------------------------------


def _in_own_process(tmp_path: Path, worker_for: Any, agent_id: str) -> tuple[dict[str, Any], bytes]:
    """Launch a run of ``agent_id`` through the core's launch route with a ``ProcessWorker``, wait
    for its process, and return the run and every byte of both stores."""
    store, records = open_stores(tmp_path)
    principal = member("org-a")
    asyncio.run(publish(store, principal))
    (tmp_path / "target-keys.json").write_text(json.dumps({"version": 1, "keys": {HTTP_TARGET: KEY}}))
    worker = worker_for(records)
    app = create_app(store=store, records=records, principal=lambda: principal, worker=worker)
    with TestClient(app) as client:
        launched = client.post(
            "/v1/evals", json={"agent_id": agent_id, "dataset_name": "support-faq", "request_id": request_id()}
        )
        assert launched.status_code == 202, launched.text
        assert client.portal is not None
        client.portal.call(worker.drain)
        run: dict[str, Any] = client.get(f"/v1/evals/{launched.json()['id']}").json()
    records.close()
    stored = (tmp_path / "martian.db").read_bytes() + (tmp_path / "mlflow.db").read_bytes()
    return run, stored


def test_a_run_completes_in_its_own_process_on_an_install_with_no_martian_agents(tmp_path, endpoint):
    run, stored = _in_own_process(
        tmp_path,
        lambda records: ProcessWorker(
            http_services, records, targets={HTTP_TARGET: http_target(endpoint.url)}, options={"folder": str(tmp_path)}
        ),
        HTTP_TARGET,
    )
    assert (run["state"], run["passed"], run["failed"], run["errored"]) == ("completed", 1, 1, 0), run
    assert {request["authorization"] for request in endpoint.requests} == {f"Bearer {KEY}"}
    assert len(endpoint.requests) == CALLED_CASES
    assert KEY.encode() not in stored  # the run's process read it from its own credentials only


@pytest.mark.parametrize("queued", [False, True], ids=["process-worker", "queue-worker"])
def test_a_run_reads_and_checks_only_the_target_it_names(tmp_path, endpoint, queued):
    """Another target the worker has, under some other id, that it would refuse is never read: not
    when the launch checks its own target, before the run is recorded, and not when the run's
    process starts (MartianSecurity/martian-notes#337)."""
    refused = Target(kind="function", file="agent.py", function="target")

    def worker_for(records: SqlRecordStore) -> Any:
        targets = {HTTP_TARGET: http_target(endpoint.url), str(uuid.uuid4()): refused}
        runner = ProcessWorker(http_services, records, targets=targets, options={"folder": str(tmp_path)})
        if not queued:
            return runner
        queue = SqlWorkQueue(f"sqlite:///{tmp_path / 'martian.db'}")
        return QueueWorker(queue, runner, records, in_process=True, lease_seconds=5, poll_seconds=0.05)

    run, _ = _in_own_process(tmp_path, worker_for, HTTP_TARGET)
    assert (run["state"], run["passed"], run["failed"], run["error"]) == ("completed", 1, 1, ""), run
    assert len(endpoint.requests) == CALLED_CASES


def test_registered_agents_still_run_through_martian_agents_in_their_own_process(tmp_path, endpoint):
    run, stored = _in_own_process(
        tmp_path,
        lambda records: ProcessWorker(
            agent_services,
            records,
            targets={HTTP_TARGET: http_target(endpoint.url)},
            executor=FakeMartianAgents(tmp_path),
            options={"folder": str(tmp_path)},
        ),
        REGISTERED_AGENT,
    )
    assert (run["state"], run["passed"], run["failed"]) == ("completed", 1, 1), run
    chats = FakeMartianAgents(tmp_path).chats()
    assert [chat["organisation"] for chat in chats] == ["org-a"] * CALLED_CASES
    assert endpoint.requests == []
    assert LEASE_PREFIX.encode() not in stored


# --- a target that changed since the run started ---------------------------------------------------

FIRST_PROMPT, SECOND_PROMPT = "How do I reset my password?", "Can I change my username?"


def changed_since_it_started(label: str) -> str:
    """The error of a run whose target changed since it started, naming the target."""
    return (
        f"The target {label} changed since this run started; the run did not call the changed target. "
        "Start a new run to evaluate it as it is now"
    )


async def until(check: Callable[[], bool], seconds: float = 60) -> None:
    deadline = time.monotonic() + seconds
    while not check():
        if time.monotonic() > deadline:
            raise TimeoutError("the condition never held")
        await asyncio.sleep(0.05)


async def admitted(
    store: EvalStore,
    records: SqlRecordStore,
    worker: Worker,
    agent_id: str = HTTP_TARGET,
    pinned_by: Worker | None = None,
) -> Run:
    """A run of ``agent_id`` over support-faq, admitted as a launch admits it and submitted to
    ``worker``, with the pin of the target ``pinned_by`` (``worker`` unless given) has now."""
    target = Target(kind="agent", agent_id=agent_id)
    dataset = await store.find_dataset(SINGLE_USER, DEFAULT_PROJECT, "support-faq")
    assert dataset is not None
    pin = await (pinned_by or worker).check_target(SINGLE_USER, target)
    return await admit_run(
        store,
        records,
        worker,
        SINGLE_USER,
        entitlements=COMMUNITY,
        run_id=str(uuid.uuid4()),
        project=DEFAULT_PROJECT,
        target=target,
        pin=pin,
        dataset=dataset,
        max_cases=100,
    )


def going_on(runner: str, store: EvalStore, records: SqlRecordStore, folder: Path, target: Target) -> Any:
    """The worker that goes on with a run, knowing ``HTTP_TARGET`` as ``target``: in this process,
    or with each piece of work in a process of its own, which receives the target with the work."""
    if runner == "in-process":
        return InProcessWorker(
            store, records, targets={HTTP_TARGET: target}, credentials=keys([]), target_timeout_seconds=30
        )
    return ProcessWorker(http_services, records, targets={HTTP_TARGET: target}, options={"folder": str(folder)})


def queued(queue: SqlWorkQueue, runner: Runner, records: SqlRecordStore) -> QueueWorker:
    return QueueWorker(queue, runner, records, in_process=True, lease_seconds=60, poll_seconds=0.05)


@pytest.fixture
def stores(tmp_path: Path) -> Iterator[tuple[MlflowStore, SqlRecordStore]]:
    """Stores in files, which a run's process opens too, and the HTTP target's key where
    :func:`http_services` reads it."""
    store, records = open_stores(tmp_path)
    (tmp_path / "target-keys.json").write_text(json.dumps({"version": 1, "keys": {HTTP_TARGET: KEY}}))
    yield store, records
    records.close()


@pytest.mark.parametrize("changed", [True, False], ids=["changed", "unchanged"])
@pytest.mark.parametrize("runner", ["in-process", "processes"])
async def test_a_run_taken_over_after_its_worker_stopped_calls_only_the_target_it_started_with(
    tmp_path, stores, runner, changed
):
    """The run's worker stops while its second case is in flight, as one whose process dies does,
    and the target under the run's id changes meanwhile, or does not. The next worker takes the run
    over: it fails the run without calling the changed target, or goes on as before."""
    store, records = stores
    first, other = Endpoint(hold=SECOND_PROMPT), Endpoint()
    queue = SqlWorkQueue(f"sqlite:///{tmp_path / 'martian.db'}")
    try:
        await publish(store)
        started = InProcessWorker(store, records, targets={HTTP_TARGET: http_target(first.url)}, credentials=keys([]))
        starting = queued(queue, started, records)
        run = await admitted(store, records, starting)
        await starting.start()
        await until(lambda: len(first.requests) == 2)  # faq-1 answered, faq-2 in flight
        await starting.stop()  # the run stays running, in the queue for the next worker
        first.release()
        taking = queued(
            queue, going_on(runner, store, records, tmp_path, http_target((other if changed else first).url)), records
        )
        await taking.start()
        try:
            await taking.drain()
        finally:
            await taking.stop()
        finished = await records.get_run(SINGLE_USER, DEFAULT_PROJECT, run.id)
        kept = await records.list_results(SINGLE_USER, DEFAULT_PROJECT, run.id)
    finally:
        queue.close()
        first.close()
        other.close()
    assert finished is not None
    if changed:
        assert (finished.state, finished.error) == ("failed", changed_since_it_started(HTTP_TARGET))
        assert other.requests == []  # the changed target was called for no case
        assert [(result.case_id, result.status) for result in kept.items] == [("faq-1", "passed")]
    else:
        assert (finished.state, finished.error, finished.counts.passed, finished.counts.failed) == (
            "completed",
            "",
            1,
            1,
        )
        # faq-1 had finished and is not called again; faq-2 was in flight and is.
        assert [request["body"]["message"] for request in first.requests] == [
            FIRST_PROMPT,
            SECOND_PROMPT,
            SECOND_PROMPT,
        ]


@pytest.mark.parametrize("changed", [True, False], ids=["changed", "unchanged"])
@pytest.mark.parametrize("runner", ["in-process", "processes"])
async def test_a_run_resumed_after_it_stopped_calls_only_the_target_it_started_with(
    tmp_path, stores, monkeypatch, runner, changed
):
    """The run stops at its time limit after its first case, and the target under its id changes
    before ``resume_run``, or does not. Resumed, the run fails without calling the changed target,
    or goes on as before."""
    import martian.evals.limits

    store, records = stores
    first, other = Endpoint(), Endpoint()
    real = time.monotonic
    # The run's clock runs an hour on once its first case was sent, so it stops before its second.
    monkeypatch.setattr(martian.evals.limits, "monotonic", lambda: real() + 3600 * bool(first.requests))
    try:
        await publish(store)
        started = InProcessWorker(
            store,
            records,
            targets={HTTP_TARGET: http_target(first.url)},
            credentials=keys([]),
            limits=RunLimits(max_seconds=60),
        )
        run = await admitted(store, records, started)
        await started.drain()
        stopped = await records.get_run(SINGLE_USER, DEFAULT_PROJECT, run.id)
        assert stopped is not None and stopped.state == "stopped"
        monkeypatch.undo()
        worker = going_on(runner, store, records, tmp_path, http_target((other if changed else first).url))
        await resume_run(
            store, records, worker, SINGLE_USER, stopped, entitlements=COMMUNITY, limits=RunLimits(max_seconds=600)
        )
        await worker.drain()
        finished = await records.get_run(SINGLE_USER, DEFAULT_PROJECT, run.id)
        kept = await records.list_results(SINGLE_USER, DEFAULT_PROJECT, run.id)
    finally:
        first.close()
        other.close()
    assert finished is not None
    if changed:
        assert (finished.state, finished.error) == ("failed", changed_since_it_started(HTTP_TARGET))
        assert other.requests == []
        assert [(result.case_id, result.status) for result in kept.items] == [("faq-1", "passed")]
    else:
        assert (finished.state, finished.error, finished.counts.passed, finished.counts.failed) == (
            "completed",
            "",
            1,
            1,
        )
        assert [request["body"]["message"] for request in first.requests] == [FIRST_PROMPT, SECOND_PROMPT]


@pytest.mark.parametrize("change", ["http-target", "function-to-http", "http-to-function", "registered-agent"])
async def test_a_run_whose_target_changed_before_its_first_start_fails_and_calls_it_for_no_case(
    records, tmp_path, change
):
    """Every kind of target is compared by its pin: an HTTP target by its digest, a function and an
    HTTP target by their kind, and a registered agent by the pin its executor gives."""
    endpoint = Endpoint()
    called: list[str] = []

    async def function(case: Case) -> Any:
        called.append(case.id)
        return await agent(case)

    http, moved = http_target(endpoint.url), http_target(endpoint.url + "/v2")
    before, after = {
        "http-target": ({HTTP_TARGET: http}, {HTTP_TARGET: moved}),
        "function-to-http": ({HTTP_TARGET: function}, {HTTP_TARGET: http}),
        "http-to-function": ({HTTP_TARGET: http}, {HTTP_TARGET: function}),
        "registered-agent": ({}, {}),
    }[change]
    agent_id = REGISTERED_AGENT if change == "registered-agent" else HTTP_TARGET
    revision = 2 if change == "registered-agent" else 1
    store = MemoryStore()
    try:
        await publish(store)
        admitting = InProcessWorker(store, records, targets=before, executor=FakeMartianAgents(tmp_path))
        running = InProcessWorker(
            store,
            records,
            targets=after,
            credentials=keys([]),
            executor=FakeMartianAgents(tmp_path, revision=revision),
        )
        run = await admitted(store, records, running, agent_id, pinned_by=admitting)
        await running.drain()
        finished = await records.get_run(SINGLE_USER, DEFAULT_PROJECT, run.id)
        kept = await records.list_results(SINGLE_USER, DEFAULT_PROJECT, run.id)
    finally:
        endpoint.close()
    assert finished is not None
    assert (finished.state, finished.error) == ("failed", changed_since_it_started(agent_id))
    assert endpoint.requests == [] and called == [] and FakeMartianAgents(tmp_path).chats() == []
    assert kept.items == []
