"""`martian ui`: the evals core on SQLite and the Evals UI from one local process, with no login."""

import socket
import threading
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import martian
from martian.cli import app as cli
from martian.evals import local
from martian.evals.local import LocalEditionError, create_local_app, listen, serve

LOCAL = "http://127.0.0.1:7600"

SESSION = {
    "kind": "single_user",
    "user": {"id": "local", "email": None, "display_name": None},
    "organisation": None,
    "role": "owner",
    "platform_admin": False,
    "session_id": "local",
}


@pytest.fixture(autouse=True)
def no_telemetry(monkeypatch):
    monkeypatch.setenv("MLFLOW_DISABLE_TELEMETRY", "true")
    monkeypatch.delenv("MLFLOW_WORKSPACE", raising=False)


@pytest.fixture
def ui(tmp_path) -> Path:
    folder = tmp_path / "dist-evals"
    (folder / "assets").mkdir(parents=True)
    (folder / "index.html").write_text("<!doctype html><title>Martian Evals</title>", encoding="utf-8")
    (folder / "assets" / "index.js").write_text("export {};", encoding="utf-8")
    return folder


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


def test_the_session_call_answers_with_the_fixed_principal(tmp_path, ui):
    client = TestClient(create_local_app(tmp_path / "home", ui), base_url=LOCAL)
    response = client.post("/v1/auth/refresh")
    assert response.status_code == 200
    assert response.json() == {"access_token": "local", "session": SESSION}


def test_the_bootstrap_answers_from_the_sqlite_store_in_the_data_folder(tmp_path, ui):
    home = tmp_path / "home"
    client = TestClient(create_local_app(home, ui), base_url=LOCAL)
    body = client.get("/v1/evals/bootstrap").json()
    session = {key: value for key, value in SESSION.items() if key != "session_id"}
    assert body["principal"] == session
    assert body["edition"] == {"key": "community", "name": "Community"}
    assert (body["status"]["status"], body["status"]["provider"]) == ("ready", "mlflow")
    assert (home / "mlflow.db").is_file()
    assert (home / "artifacts").is_dir()


def test_the_built_ui_is_served_beside_the_api(tmp_path, ui):
    client = TestClient(create_local_app(tmp_path / "home", ui), base_url=LOCAL)
    page = client.get("/")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert "<title>Martian Evals</title>" in page.text
    assert client.get("/assets/index.js").text == "export {};"
    assert client.get("/assets/missing.js").status_code == 404


@pytest.mark.parametrize("host", ["127.0.0.1:7600", "localhost:7600"])
def test_the_local_names_are_answered(tmp_path, ui, host):
    client = TestClient(create_local_app(tmp_path / "home", ui), base_url=LOCAL)
    assert client.post("/v1/auth/refresh", headers={"host": host}).status_code == 200


def test_another_host_name_is_refused(tmp_path, ui):
    # A page on another site that resolves its own name to this machine reaches the server with
    # that name in Host.
    client = TestClient(create_local_app(tmp_path / "home", ui), base_url=LOCAL)
    for path in ("/v1/auth/refresh", "/v1/evals/bootstrap", "/"):
        method = client.post if path == "/v1/auth/refresh" else client.get
        assert method(path, headers={"host": "attacker.example:7600"}).status_code == 400


def test_a_folder_without_a_built_ui_is_refused(tmp_path):
    with pytest.raises(LocalEditionError, match="index.html is missing"):
        create_local_app(tmp_path / "home", tmp_path / "empty")
    assert not (tmp_path / "home").exists()


def test_the_default_folders(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert local.default_home() == tmp_path / ".martian" / "evals"
    assert local.packaged_ui() == Path(martian.__file__).resolve().parent / "ui"


def test_a_port_another_process_holds_is_refused():
    with socket.socket() as other:
        other.bind(("127.0.0.1", 0))
        other.listen()
        with pytest.raises(LocalEditionError, match="cannot listen on 127.0.0.1"):
            listen(other.getsockname()[1])


def test_one_process_serves_the_api_and_the_ui_on_loopback_and_opens_the_browser(monkeypatch, tmp_path, ui):
    import uvicorn

    opened: list[str] = []
    announced: list[str] = []
    servers: list[uvicorn.Server] = []
    closed = threading.Event()
    run = uvicorn.Server.run

    def keep(self, sockets=None):
        servers.append(self)
        assert sockets is not None and sockets[0].getsockname()[0] == "127.0.0.1"
        run(self, sockets)

    def browser(url):
        # A browser command that runs until it is closed: the server answers it meanwhile.
        opened.append(url)
        closed.wait(10)

    monkeypatch.setattr("webbrowser.open", browser)
    monkeypatch.setattr(uvicorn.Server, "run", keep)
    port = free_port()
    thread = threading.Thread(
        target=serve,
        args=(create_local_app(tmp_path / "home", ui),),
        kwargs={"port": port, "open_browser": True, "announce": announced.append},
        daemon=True,
    )
    thread.start()
    url = f"http://127.0.0.1:{port}/"
    try:
        with httpx.Client(base_url=url, timeout=10, trust_env=False) as client:
            assert "<title>Martian Evals</title>" in client.get("/").text
            assert client.post("/v1/auth/refresh").json()["session"] == SESSION
            assert client.get("/v1/evals/bootstrap").json()["principal"]["kind"] == "single_user"
    finally:
        closed.set()
        for server in servers:
            server.should_exit = True
        thread.join(10)
    assert not thread.is_alive()
    assert announced == opened == [url]


def test_no_open_leaves_the_browser_alone(monkeypatch, tmp_path, ui):
    import uvicorn

    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", opened.append)
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, sockets=None: sockets[0].close())
    serve(create_local_app(tmp_path / "home", ui), port=free_port(), open_browser=False, announce=lambda url: None)
    assert opened == []


def test_the_command_serves_the_named_folders(monkeypatch, tmp_path, ui):
    served = {}

    def record(app, **options):
        served.update(options, app=app)
        options["announce"]("http://127.0.0.1:7601/")

    monkeypatch.setattr(local, "serve", record)
    monkeypatch.setenv("MARTIAN_EVALS_HOME", str(tmp_path / "from-env"))
    result = CliRunner().invoke(cli, ["ui", "--port", "7601", "--ui-dir", str(ui), "--no-open"])
    assert result.exit_code == 0, result.output
    assert served["port"] == 7601 and served["open_browser"] is False
    client = TestClient(served["app"], base_url=LOCAL)
    assert client.post("/v1/auth/refresh").json()["session"] == SESSION
    assert client.get("/").text.startswith("<!doctype html>")
    assert "Martian Evals is at http://127.0.0.1:7601/" in result.output
    assert (tmp_path / "from-env" / "mlflow.db").is_file()


def test_the_command_defaults_to_the_data_folder_in_home_and_opens_the_browser(monkeypatch, tmp_path, ui):
    served = {}
    monkeypatch.setattr(local, "serve", lambda app, **options: served.update(options))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MARTIAN_EVALS_HOME", raising=False)
    result = CliRunner().invoke(cli, ["ui", "--ui-dir", str(ui)])
    assert result.exit_code == 0, result.output
    assert served["port"] == 7600 and served["open_browser"] is True
    assert (tmp_path / ".martian" / "evals" / "mlflow.db").is_file()


def test_the_command_says_how_to_name_a_ui_when_the_package_has_none(monkeypatch, tmp_path):
    monkeypatch.setattr(local, "packaged_ui", lambda: tmp_path / "no-ui")
    monkeypatch.delenv("MARTIAN_EVALS_UI_DIR", raising=False)
    result = CliRunner().invoke(cli, ["ui", "--data-dir", str(tmp_path / "home"), "--no-open"])
    assert result.exit_code == 2
    assert "there is no built Evals UI in" in result.output
    assert "Pass --ui-dir" in result.output
