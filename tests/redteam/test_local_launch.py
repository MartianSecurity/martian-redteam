"""The Red Team UI's setup page starts a run against a local target, and its report resumes a stopped
run, through the local edition's routes under /v1/local/redteam/runs (martian-notes#295).

The target is a loopback JSON endpoint that records each request's headers; the attacker and judge
are the loopback fake models. Nothing reaches another machine."""

import asyncio
import json
import threading
import time

import pytest
import yaml
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from martian.cli import app as cli
from martian.evals.local import create_local_app
from martian.evals.server.principal import SINGLE_USER
from martian.redteam.store import RedteamStore
from tests.redteam.target_servers import json_server

KEY = "local-target-key-5f2c9a"


class Agent:
    """Leaks on a system prompt attack; ``delay`` slows each reply so a run can be stopped."""

    def __init__(self) -> None:
        self.delay = 0.0
        self.lock = threading.Lock()

    def __call__(self, body, headers):
        time.sleep(self.delay)
        message = str(body.get("message", ""))
        return 200, {"reply": "LEAKED: Mars Market" if "system_prompt" in message else "No."}


@pytest.fixture
def agent():
    handler = Agent()
    with json_server(handler) as (url, recorded):
        yield handler, url, recorded


@pytest.fixture
def ui(tmp_path, models):
    folder = tmp_path / "ui"
    folder.mkdir()
    (folder / "index.html").write_text("<!doctype html><title>Martian</title>")
    application = create_local_app(tmp_path / "home", folder)
    with TestClient(application, base_url="http://127.0.0.1:7600") as client:
        client.app_state = application.state
        yield client


def define(client, url, key=KEY):
    body = {
        "name": "refund-bot",
        "kind": "http",
        "url": url,
        "body": {"message": "{{input}}"},
        "response_path": "reply",
    }
    answer = client.post("/v1/local/targets", json=body | ({"key": key} if key else {}))
    assert answer.status_code == 201, answer.text
    return answer.json()


def run_file(url, **fields):
    """The run file the setup page writes for a local http target with a key."""
    return {
        "version": 2,
        "target": {
            "purpose": "Refund assistant for Mars Market.",
            "url": url,
            "body": {"message": "{{input}}"},
            "response_path": "reply",
            "headers": {"Authorization": "Bearer ${MARTIAN_TARGET_KEY}"},
        },
        "models": {"attacker": "openai:/attacker-model", "judge": "openai:/judge-model"},
        "vulnerabilities": [{"name": "PromptLeakage", "types": ["system_prompt", "instructions"]}],
        "attacks": [{"name": "PromptInjection"}],
    } | fields


def wait_for(client, run_id, states, *, attempts=None):
    for _ in range(400):
        body = client.get(f"/v1/redteam/runs/{run_id}").json()
        if body["state"] in states and (attempts is None or body["counts"]["attempts"] >= attempts):
            return body
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} stayed {body['state']}")


def stored(client, run_id):
    records = client.app_state.martian_evals_records
    return asyncio.run(RedteamStore(records).get("default", run_id, principal=SINGLE_USER))


def test_a_run_against_a_local_target_sends_its_key_and_keeps_only_the_reference(ui, agent):
    _, url, recorded = agent
    target = define(ui, url)
    started = ui.post(
        "/v1/local/redteam/runs", json={"run_file": run_file(url), "target_id": target["id"], "request_id": "ui-1"}
    )
    assert started.status_code == 202, started.text
    assert started.json()["state"] == "running" and KEY not in started.text
    done = wait_for(ui, "ui-1", {"completed"})
    assert (done["counts"]["hits"], done["counts"]["resisted"]) == (1, 1)
    # Each attack, and each repetition and control a hit adds, reached the target with its key.
    assert len(recorded.headers) >= 2
    assert {headers["authorization"] for headers in recorded.headers} == {f"Bearer {KEY}"}
    run = stored(ui, "ui-1")
    assert run.provenance["launch"] == {"run_file": run_file(url), "target_id": target["id"]}
    assert KEY not in run.model_dump_json()
    for path in ("/v1/redteam/runs/ui-1", "/v1/redteam/runs/ui-1/attempts", "/v1/redteam/runs/ui-1/report"):
        assert KEY not in ui.get(path).text


def test_the_key_goes_only_to_the_local_targets_own_address(ui, agent, monkeypatch):
    _, url, recorded = agent
    target = define(ui, url)
    elsewhere = run_file(url.replace("/chat", "/other"))
    refused = ui.post("/v1/local/redteam/runs", json={"run_file": elsewhere, "target_id": target["id"]})
    assert refused.status_code == 422 and "target.url must be the local target's address" in refused.text
    unknown = ui.post("/v1/local/redteam/runs", json={"run_file": run_file(url), "target_id": "no-such-target"})
    assert unknown.status_code == 422 and "names no local target" in unknown.text
    # A target without a key never takes MARTIAN_TARGET_KEY from the server's environment.
    monkeypatch.setenv("MARTIAN_TARGET_KEY", "from-the-environment")
    ui.delete(f"/v1/local/targets/{target['id']}")
    keyless = define(ui, url, key=None)
    missing = ui.post("/v1/local/redteam/runs", json={"run_file": run_file(url), "target_id": keyless["id"]})
    assert missing.status_code == 422 and "MARTIAN_TARGET_KEY" in missing.text
    assert recorded.headers == []


def test_a_ui_run_equals_the_same_file_run_from_the_command_line(ui, agent, tmp_path, monkeypatch):
    _, url, recorded = agent
    target = define(ui, url)
    mapping = run_file(url)
    started = ui.post(
        "/v1/local/redteam/runs", json={"run_file": mapping, "target_id": target["id"], "request_id": "ui-2"}
    )
    assert started.status_code == 202, started.text
    wait_for(ui, "ui-2", {"completed"})
    from_ui = stored(ui, "ui-2")

    path = tmp_path / "redteam.yaml"
    path.write_text(yaml.safe_dump(mapping | {"output_folder": str(tmp_path / "results")}, sort_keys=False))
    monkeypatch.setenv("MARTIAN_TARGET_KEY", KEY)
    result = CliRunner().invoke(cli, ["redteam", "run", str(path), "--no-store"])
    assert result.exit_code == 0, result.output  # a leak only the judge saw is suspected: the gate passes
    from_cli = json.loads((tmp_path / "results" / "redteam-run.json").read_text())

    assert [probe.id for probe in from_ui.probes] == [probe["id"] for probe in from_cli["probes"]]
    fingerprints = sorted((attempt.id, attempt.fingerprint) for attempt in from_ui.attempts)
    assert fingerprints == sorted((attempt["id"], attempt["fingerprint"]) for attempt in from_cli["attempts"])
    assert [attempt.outcome for attempt in sorted(from_ui.attempts, key=lambda item: item.id)] == [
        attempt["outcome"] for attempt in sorted(from_cli["attempts"], key=lambda item: item["id"])
    ]
    assert {headers["authorization"] for headers in recorded.headers} == {f"Bearer {KEY}"}


def test_a_stopped_run_resumes_and_keeps_its_finished_attempts(ui, agent):
    handler, url, recorded = agent
    handler.delay = 0.2
    target = define(ui, url)
    mapping = run_file(url, budget={"attempts_per_probe": 6, "max_concurrent": 1})
    started = ui.post(
        "/v1/local/redteam/runs", json={"run_file": mapping, "target_id": target["id"], "request_id": "ui-3"}
    )
    assert started.status_code == 202, started.text
    wait_for(ui, "ui-3", {"running"}, attempts=2)
    stopped = ui.post("/v1/redteam/runs/ui-3/stop")
    assert stopped.status_code == 200 and stopped.json()["state"] == "cancelled"
    kept = stopped.json()["counts"]["attempts"]
    assert 2 <= kept < 12
    called = len(recorded.headers)
    before = {item["id"] for item in ui.get("/v1/redteam/runs/ui-3/attempts", params={"limit": 200}).json()["items"]}

    handler.delay = 0.0
    resumed = ui.post("/v1/local/redteam/runs/ui-3/resume")
    assert resumed.status_code == 202, resumed.text
    assert resumed.json()["state"] == "running" and resumed.json()["id"] == "ui-3"
    done = wait_for(ui, "ui-3", {"completed"})
    assert done["counts"]["attempts"] == 12
    run = stored(ui, "ui-3")
    assert run.provenance["resumed_attempts"] == kept
    # The target was called again only for what the earlier session had not finished.
    rerun = sum(attempt.target_calls for attempt in run.attempts if attempt.id not in before)
    assert rerun > 0 and len(recorded.headers) - called == rerun
    assert {headers["authorization"] for headers in recorded.headers} == {f"Bearer {KEY}"}

    again = ui.post("/v1/local/redteam/runs/ui-3/resume")
    assert again.status_code == 409 and "this one is completed" in again.text
    assert ui.post("/v1/local/redteam/runs/no-such-run/resume").status_code == 404


def test_a_run_whose_server_stopped_can_be_stopped_or_resumed(ui, agent):
    _, url, _ = agent
    target = define(ui, url)
    started = ui.post(
        "/v1/local/redteam/runs", json={"run_file": run_file(url), "target_id": target["id"], "request_id": "ui-4"}
    )
    assert started.status_code == 202, started.text
    wait_for(ui, "ui-4", {"completed"})
    records = ui.app_state.martian_evals_records
    store = RedteamStore(records)
    # As a server that stopped mid-run leaves it: running, with no task to stop.
    asyncio.run(store.save(stored(ui, "ui-4").model_copy(update={"state": "running"}), principal=SINGLE_USER))
    stopped = ui.post("/v1/redteam/runs/ui-4/stop")
    assert stopped.status_code == 200 and stopped.json()["state"] == "cancelled"
    asyncio.run(store.save(stored(ui, "ui-4").model_copy(update={"state": "running"}), principal=SINGLE_USER))
    resumed = ui.post("/v1/local/redteam/runs/ui-4/resume")
    assert resumed.status_code == 202, resumed.text
    assert wait_for(ui, "ui-4", {"completed"})["counts"]["attempts"] == 2


def test_a_run_the_launcher_did_not_start_cannot_be_resumed_here(ui, agent, tmp_path):
    _, url, _ = agent
    target = define(ui, url)
    started = ui.post(
        "/v1/local/redteam/runs", json={"run_file": run_file(url), "target_id": target["id"], "request_id": "ui-5"}
    )
    assert started.status_code == 202, started.text
    wait_for(ui, "ui-5", {"completed"})
    records = ui.app_state.martian_evals_records
    run = stored(ui, "ui-5")
    provenance = {key: value for key, value in run.provenance.items() if key != "launch"}
    asyncio.run(
        RedteamStore(records).save(
            run.model_copy(update={"state": "cancelled", "provenance": provenance}), principal=SINGLE_USER
        )
    )
    refused = ui.post("/v1/local/redteam/runs/ui-5/resume")
    assert refused.status_code == 409 and "--resume ui-5" in refused.text


def test_an_invalid_run_file_is_refused_before_anything_runs(ui, agent):
    _, url, recorded = agent
    target = define(ui, url)
    broken = run_file(url) | {"attacks": []}
    answer = ui.post("/v1/local/redteam/runs", json={"run_file": broken, "target_id": target["id"]})
    assert answer.status_code == 422 and "attacks" in answer.text
    assert recorded.headers == []
