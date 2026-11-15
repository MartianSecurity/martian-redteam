"""Local targets in `martian ui`: defined in the UI in place of the Agents registry, with the user's
own key, which only the key file holds."""

import logging
import os
import stat
import threading
import time
import uuid
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from martian.evals.local import create_local_app, serve
from martian.evals.local_targets import DEFINITIONS_FILE, KEYS_FILE, LocalTargets
from martian.evals.model import Target
from martian.evals.server import SINGLE_USER
from tests.evals.local_fixture import DATASET, FakeTarget, publish

LOCAL = "http://127.0.0.1:7600"
TARGETS = "/v1/local/targets"
# A distinctive key, so a copy of it anywhere is found.
KEY = f"sk-local-{uuid.uuid4().hex}"

HTTP_TARGET = {
    "name": "Support agent",
    "kind": "http",
    "url": "https://agent.example/api/chat",
    "body": {"message": "{{input}}", "session": "{{uuid}}"},
    "response_path": "reply.text",
}
MODEL_TARGET = {"name": "Hosted model", "kind": "model", "base_url": "https://models.example/v1/", "model": "example-1"}


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
def client(home, ui) -> TestClient:
    return TestClient(create_local_app(home, ui), base_url=LOCAL)


def free_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


def test_a_target_is_defined_listed_and_removed_with_its_key(client, home):
    created = client.post(TARGETS, json={**HTTP_TARGET, "key": KEY})
    assert created.status_code == 201
    target = created.json()
    assert uuid.UUID(target["id"])
    assert target == {
        **HTTP_TARGET,
        "id": target["id"],
        "created_at": target["created_at"],
        "key_header": "Authorization",
        "base_url": None,
        "model": None,
        "has_key": True,
    }
    assert client.get(TARGETS).json() == {"items": [target]}
    assert LocalTargets(home).key(target["id"]) == KEY

    assert client.delete(f"{TARGETS}/{target['id']}").status_code == 204
    assert client.get(TARGETS).json() == {"items": []}
    assert LocalTargets(home).key(target["id"]) is None
    assert KEY not in (home / KEYS_FILE).read_text(encoding="utf-8")
    assert client.delete(f"{TARGETS}/{target['id']}").json() == {"detail": "Target not found"}


def test_a_target_without_a_key_has_none(client, home):
    target = client.post(TARGETS, json=MODEL_TARGET).json()
    assert (target["has_key"], target["key_header"], target["url"]) == (False, None, None)
    assert not (home / KEYS_FILE).exists()
    assert LocalTargets(home).key(target["id"]) is None


def test_a_name_in_use_is_refused(client):
    assert client.post(TARGETS, json=HTTP_TARGET).status_code == 201
    refused = client.post(TARGETS, json={**MODEL_TARGET, "name": " Support agent ", "key": KEY})
    assert (refused.status_code, refused.json()) == (409, {"detail": "A target named 'Support agent' already exists"})


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"url": "ftp://agent.example/chat"}, "url must be an http or https address"),
        ({"url": "https://user:pass@agent.example/chat"}, "url must not contain credentials; give the key instead"),
        ({"url": "https://agent.example/chat?key=${MARTIAN_TARGET_KEY}"}, "url cannot hold ${...}"),
        ({"body": {"message": "fixed"}}, "a url target's body must use {{input}}, {{inputs.NAME}} or {{messages}}"),
        ({"body": {"message": "{{prompt}}"}}, "unknown body placeholder(s) prompt"),
        ({"key_header": "X Key"}, "key_header must be a header name"),
        ({"model": "example-1"}, "model does not apply to an http target"),
        ({"body": None}, "an http target needs body"),
        ({"key": "two\nlines"}, "key: key must be 1 to 4096 characters, without line breaks"),
        ({"key": "   "}, "key: key must be 1 to 4096 characters"),
        ({"name": ""}, "name: String should have at least 1 character"),
    ],
)
def test_a_target_that_would_not_work_is_refused_with_the_reason(client, change, problem):
    refused = client.post(TARGETS, json={**HTTP_TARGET, "key": KEY, **change})
    assert refused.status_code == 422
    assert problem in refused.json()["detail"]
    assert client.get(TARGETS).json() == {"items": []}


def test_a_model_target_needs_its_address_and_model(client):
    refused = client.post(TARGETS, json={"name": "Model", "kind": "model", "key_header": "X-Key"})
    assert refused.json() == {"detail": "key_header does not apply to a model target"}
    refused = client.post(TARGETS, json={"name": "Model", "kind": "model", "base_url": "https://models.example/v1"})
    assert refused.json() == {"detail": "a model target needs model"}


def test_each_kind_is_the_shared_models_http_target_with_the_key_as_a_reference(client, home):
    agent = client.post(TARGETS, json={**HTTP_TARGET, "key_header": "X-API-Key", "key": KEY}).json()
    model = client.post(TARGETS, json={**MODEL_TARGET, "key": KEY}).json()
    open_endpoint = client.post(TARGETS, json={**HTTP_TARGET, "name": "Open agent"}).json()
    targets = LocalTargets(home)
    found = {item["id"]: targets.get(item["id"]) for item in (agent, model, open_endpoint)}
    assert targets.model_target(found[agent["id"]]) == Target(
        kind="http",
        url="https://agent.example/api/chat",
        body={"message": "{{input}}", "session": "{{uuid}}"},
        response_path="reply.text",
        headers={"X-API-Key": "${MARTIAN_TARGET_KEY}"},
    )
    assert targets.model_target(found[model["id"]]) == Target(
        kind="http",
        url="https://models.example/v1/chat/completions",
        body={"model": "example-1", "messages": [{"role": "user", "content": "{{input}}"}]},
        response_path="choices.0.message.content",
        headers={"Authorization": "Bearer ${MARTIAN_TARGET_KEY}"},
        model_path="model",
    )
    assert targets.model_target(found[open_endpoint["id"]]).headers == {}


def test_the_key_is_the_credential_of_its_reference_only(client, home):
    target = client.post(TARGETS, json={**HTTP_TARGET, "key": KEY}).json()
    targets = LocalTargets(home)
    assert targets.credential(SINGLE_USER, target["id"], "MARTIAN_TARGET_KEY") == KEY
    assert targets.credential(SINGLE_USER, target["id"], "HOME") is None
    assert targets.credential(SINGLE_USER, str(uuid.uuid4()), "MARTIAN_TARGET_KEY") is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_only_the_user_can_read_the_key_file(client, home):
    previous = os.umask(0o022)  # what a shell usually leaves: new files readable by everyone
    try:
        client.post(TARGETS, json={**HTTP_TARGET, "key": KEY})
        client.post(TARGETS, json={**MODEL_TARGET, "key": KEY})
    finally:
        os.umask(previous)
    assert stat.S_IMODE((home / KEYS_FILE).stat().st_mode) == 0o600
    assert [path.name for path in home.iterdir() if path.name.startswith(".")] == []  # no temporary file left


def _finished(client: httpx.Client, run_id: str, answers: list[httpx.Response]) -> dict:
    """Poll a run until it and its scoring jobs have finished, keeping every answer."""
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        answers.append(client.get(f"/v1/evals/{run_id}"))
        answers.append(client.get(f"/v1/evals/{run_id}/workbench"))
        run, work = answers[-2].json(), answers[-1].json()
        if run["state"] not in ("queued", "running") and all(
            job["state"] not in ("queued", "running") for job in work["jobs"]
        ):
            return dict(run)
        time.sleep(0.5)
    raise AssertionError("the run did not finish")


def test_the_key_never_appears_in_a_response_a_log_line_a_trace_or_a_stored_record(home, ui, caplog, capfd):
    """Every route a target touches, and a full run of a target with a key and a run again, on
    the server `martian ui` runs, with every log at DEBUG. Only each run's process reads the key:
    the server's own process never does."""
    import uvicorn

    def refused(self: LocalTargets, target_id: str) -> str | None:
        raise AssertionError("the server's process read a target's key")

    caplog.set_level(logging.DEBUG)
    publish(home)
    servers: list[uvicorn.Server] = []
    run = uvicorn.Server.run

    def keep(self, sockets=None):
        servers.append(self)
        run(self, sockets)

    port = free_port()
    with pytest.MonkeyPatch.context() as patch, FakeTarget(KEY) as fake:
        patch.setattr(uvicorn.Server, "run", keep)
        patch.setattr(LocalTargets, "key", refused)
        thread = threading.Thread(
            target=serve,
            args=(create_local_app(home, ui),),
            kwargs={"port": port, "open_browser": False, "announce": print},
            daemon=True,
        )
        thread.start()
        answers: list[httpx.Response] = []
        ran: list[httpx.Response] = []
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10, trust_env=False) as client:
                send = [
                    ("POST", {**HTTP_TARGET, "key": KEY}),
                    ("POST", {**MODEL_TARGET, "key": KEY}),
                    ("POST", {**MODEL_TARGET, "key": KEY}),  # a name in use
                    ("POST", {**HTTP_TARGET, "name": "Wrong", "url": "ftp://agent.example", "key": KEY}),
                    ("POST", {**HTTP_TARGET, "name": "Wrong", "body": {"message": "fixed"}, "key": KEY}),
                    ("POST", {"name": "Wrong", "kind": "elsewhere", "key": KEY, "unknown": KEY}),
                    ("POST", {**HTTP_TARGET, "name": "Wrong", "key": f"{KEY}\nsecond line"}),
                    ("POST", {**HTTP_TARGET, "name": "Wrong", "key": [KEY]}),
                ]
                answers += [client.request(method, TARGETS, json=body) for method, body in send]
                answers.append(
                    client.post(
                        TARGETS, content=f'{{"key": "{KEY}", '.encode(), headers={"content-type": "application/json"}
                    )
                )
                answers.append(client.get(TARGETS))
                answers.append(client.get("/openapi.json"))
                answers.append(client.get("/v1/evals/bootstrap"))
                removed = answers[1].json()["id"]
                answers.append(client.delete(f"{TARGETS}/{removed}"))
                answers.append(client.delete(f"{TARGETS}/{removed}"))
                answers.append(client.get(TARGETS))

                # A full run of a target that takes the key, scored with a suite, and a run again.
                ran.append(client.post(TARGETS, json=fake.definition("Fake agent")))
                suite = {"request_id": str(uuid.uuid4()), "name": "Checks", "scorers": ["contains_expected"]}
                ran.append(client.post("/v1/evals/suites", json=suite))
                launch = {
                    "request_id": str(uuid.uuid4()),
                    "agent_id": ran[0].json()["id"],
                    "dataset_name": DATASET,
                    "suite_version_id": ran[1].json()["id"],
                }
                ran.append(client.post("/v1/local/runs", json=launch))
                first = _finished(client, ran[-1].json()["id"], ran)
                [called, *_] = [item for item in first["items"] if item["evals_trace_id"]]
                ran.append(client.get(f"/v1/evals/{first['id']}/items/{called['id']}/trace"))
                ran.append(client.get(f"/v1/evals/{first['id']}/provenance"))
                ran.append(client.post(f"/v1/local/runs/{first['id']}/again", json={"request_id": str(uuid.uuid4())}))
                second = _finished(client, ran[-1].json()["id"], ran)
                ran.append(client.get("/v1/evals"))
        finally:
            for server in servers:
                server.should_exit = True
            thread.join(10)
        assert not thread.is_alive()

    assert [answer.status_code for answer in answers] == [
        201,
        201,
        409,
        422,
        422,
        422,
        422,
        422,
        422,
        200,
        200,
        200,
        204,
        404,
        200,
    ]
    assert [answer.status_code for answer in ran if answer.request.method == "POST"] == [201, 201, 202, 202]
    assert all(answer.status_code == 200 for answer in ran if answer.request.method == "GET")
    for run_ in (first, second):
        assert (run_["state"], run_["passed"], run_["failed"]) == ("completed", 1, 1), run_
    # The key reached the target, with each called case of both runs.
    assert [request["headers"]["Authorization"] for request in fake.requests] == [f"Bearer {KEY}"] * 4
    for answer in answers + ran:
        assert KEY not in answer.text
        assert KEY not in str(answer.headers)
        assert KEY not in str(answer.request.headers) + answer.request.url.query.decode()
    # The only requests that carried it are those that defined a target.
    defining = [answer.request for answer in answers + ran if KEY.encode() in answer.request.content]
    assert {(request.method, request.url.path) for request in defining} == {("POST", TARGETS)}
    # Every log record, at every level, and everything printed, by the server and each run's process.
    assert caplog.records
    assert KEY not in caplog.text
    assert all(KEY not in str(record.args) for record in caplog.records)
    printed = capfd.readouterr()
    assert KEY not in printed.out + printed.err
    # Every file in the data folder but the key file: martian.db, mlflow.db with every trace,
    # artifacts, targets.json.
    stored = [path for path in home.rglob("*") if path.is_file() and path.name != KEYS_FILE]
    assert {path.name for path in stored} >= {"martian.db", "mlflow.db", DEFINITIONS_FILE}
    for path in stored:
        assert KEY.encode() not in path.read_bytes(), path
    targets = LocalTargets(home)
    kept = targets.defined()
    assert [target.name for target in kept] == ["Support agent", "Fake agent"]
    assert [targets.key(target.id) for target in kept] == [KEY, KEY]  # only the key file holds it
    for target in kept:
        assert KEY not in repr(target) + targets.model_target(target).model_dump_json()
