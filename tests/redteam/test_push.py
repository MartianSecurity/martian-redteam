"""Push a red team run to a Martian server (martian-notes#313): the core's push route, and
`martian redteam push`. The server is the core's own route on a SQLite record store, on loopback."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from martian.cli import app as cli
from martian.evals.records.sql import SqlRecordStore
from martian.evals.server.dependencies import current_principal
from martian.evals.server.entitlements import COMMUNITY
from martian.evals.server.principal import SINGLE_USER
from martian.redteam.model.maps import from_risk_assessment
from martian.redteam.server import router
from martian.redteam.server.push import CLIENT_EXECUTED
from martian.redteam.server.push import router as push_router
from martian.redteam.store import RedteamStore
from martian_redteam.types import RiskAssessment

FIXTURES = Path(__file__).parent / "fixtures"


def local_run(run_id: str = "redteam-0123456789ab", **changes):
    report = RiskAssessment.model_validate_json((FIXTURES / "risk-assessment-0.2.json").read_text("utf-8"))
    return from_risk_assessment(report, run_id=run_id, project_id="default").model_copy(update=changes)


@pytest.fixture
def server(tmp_path) -> Iterator[tuple[FastAPI, SqlRecordStore]]:
    records = SqlRecordStore(f"sqlite:///{tmp_path / 'server.db'}")
    app = FastAPI()
    app.state.martian_evals_records = records
    app.state.martian_evals_entitlements = lambda principal: COMMUNITY
    app.dependency_overrides[current_principal] = lambda: SINGLE_USER
    app.include_router(router)
    app.include_router(push_router)
    yield app, records
    records.close()


def put(client: TestClient, run) -> object:
    return client.put(f"/v1/push/redteam/runs/{run.id}", json={"run": run.model_dump(mode="json")})


def test_a_pushed_run_is_listed_once_and_labelled_as_the_clients(server):
    app, records = server
    client = TestClient(app)
    first = put(client, local_run())
    assert first.status_code == 200 and first.json() == {
        "run_id": "redteam-0123456789ab",
        "project_id": "default",
        "created": True,
    }
    again = put(client, local_run())
    assert again.status_code == 200 and again.json()["created"] is False
    assert [item["id"] for item in client.get("/v1/redteam/runs").json()["items"]] == ["redteam-0123456789ab"]
    kept = asyncio.run(RedteamStore(records).get("default", "redteam-0123456789ab"))
    assert kept is not None and kept.provenance["execution"] == CLIENT_EXECUTED


def test_other_content_an_unfinished_run_and_another_id_are_refused(server):
    client = TestClient(server[0])
    put(client, local_run())
    assert put(client, local_run(purpose="Something else")).status_code == 409
    assert put(client, local_run("redteam-running00001", state="running")).status_code == 422
    run = local_run("redteam-elsewhere0001")
    wrong = client.put("/v1/push/redteam/runs/another-id", json={"run": run.model_dump(mode="json")})
    assert wrong.status_code == 422


def test_martian_redteam_push_sends_a_run_once(server, tmp_path, monkeypatch):
    from martian.redteam._mcp import LoopbackServer

    app, records = server
    path = tmp_path / "redteam-run.json"
    path.write_text(local_run().model_dump_json())
    loopback = LoopbackServer(app)
    loop = asyncio.new_event_loop()
    import threading

    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    asyncio.run_coroutine_threadsafe(loopback.start(), loop).result(10)
    try:
        monkeypatch.setenv("MARTIAN_API_URL", f"http://127.0.0.1:{loopback.port}")
        monkeypatch.setenv("MARTIAN_API_TOKEN", "mce_push_test")
        first = CliRunner().invoke(cli, ["redteam", "push", str(path)])
        again = CliRunner().invoke(cli, ["redteam", "push", str(path)])
        running = tmp_path / "running.json"
        running.write_text(local_run("redteam-running00001", state="running").model_dump_json())
        refused = CliRunner().invoke(cli, ["redteam", "push", str(running)])
    finally:
        asyncio.run_coroutine_threadsafe(loopback.stop(), loop).result(10)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(5)
    assert first.exit_code == 0, first.output
    assert "Red team run redteam-0123456789ab pushed to the Martian server." in " ".join(first.output.split())
    assert again.exit_code == 0 and "already on the Martian server" in " ".join(again.output.split())
    assert refused.exit_code == 2 and "has not finished" in " ".join(refused.output.split())
    listed = asyncio.run(RedteamStore(records).list("default"))
    assert [item.id for item in listed.items] == ["redteam-0123456789ab"]


def test_a_push_without_a_connection_stops(tmp_path, monkeypatch):
    for name in ("MARTIAN_API_URL", "MARTIAN_API_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "redteam-run.json"
    path.write_text(local_run().model_dump_json())
    result = CliRunner().invoke(cli, ["redteam", "push", str(path)])
    assert result.exit_code == 2 and "Not pushed" in result.output
    assert json.loads(path.read_text())["id"] == "redteam-0123456789ab"
