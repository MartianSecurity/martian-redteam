"""`martian ui`: the evals core on SQLite and the Evals UI from one local process, with no login."""

import os
import re
import socket
import subprocess
import sys
import threading
import tomllib
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
    # The local user `local`, as the API names it: a UUID derived from it.
    "user": {"id": "bf0808be-f788-5dd7-8fc2-2d28ed350045", "email": None, "display_name": None},
    "organisation": None,
    "role": "owner",
    "platform_admin": False,
    "session_id": "local",
}


@pytest.fixture(autouse=True)
def no_telemetry(monkeypatch):
    monkeypatch.setenv("MLFLOW_DISABLE_TELEMETRY", "true")
    monkeypatch.delenv("MLFLOW_WORKSPACE", raising=False)
    # The container image's settings: unset unless a test sets them.
    monkeypatch.delenv(local.BIND_ENV, raising=False)
    monkeypatch.delenv(local.ALLOWED_HOSTS_ENV, raising=False)
    monkeypatch.delenv(local.CONCURRENCY_ENV, raising=False)


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


def test_the_page_is_revalidated_and_the_hashed_build_files_are_kept(tmp_path, ui):
    # After an SDK upgrade a cached page would name the previous build's assets, which are gone.
    (ui / "ui-version.json").write_text("{}", encoding="utf-8")
    client = TestClient(create_local_app(tmp_path / "home", ui), base_url=LOCAL)
    assert client.get("/").headers["cache-control"] == "no-cache"
    assert client.get("/index.html").headers["cache-control"] == "no-cache"
    assert client.get("/ui-version.json").headers["cache-control"] == "no-cache"
    assert client.get("/assets/index.js").headers["cache-control"] == "public, max-age=31536000, immutable"
    assert "cache-control" not in client.get("/assets/missing.js").headers


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
    assert served["host"] == "127.0.0.1"
    assert (tmp_path / ".martian" / "evals" / "mlflow.db").is_file()


def test_the_concurrency_is_how_many_runs_and_scoring_jobs_may_be_in_progress_at_once(monkeypatch, tmp_path, ui):
    served = {}
    monkeypatch.setattr(local, "serve", lambda app, **options: served.update(options, app=app))
    command = ["ui", "--data-dir", str(tmp_path / "home"), "--ui-dir", str(ui), "--no-open"]
    for options, setting, expected in (([], None, 1), (["--concurrency", "3"], None, 3), ([], "2", 2)):
        if setting is not None:
            monkeypatch.setenv(local.CONCURRENCY_ENV, setting)
        result = CliRunner().invoke(cli, [*command, *options])
        assert result.exit_code == 0, result.output
        bootstrap = TestClient(served["app"], base_url=LOCAL).get("/v1/evals/bootstrap").json()
        assert bootstrap["edition"]["key"] == "community"
        assert bootstrap["entitlements"]["limits"] == {"concurrent_runs": expected, "concurrent_scoring_jobs": expected}
        assert served["app"].state.martian_evals_worker.concurrency == expected
    monkeypatch.delenv(local.CONCURRENCY_ENV)
    refused = CliRunner().invoke(cli, [*command, "--concurrency", "0"])
    assert refused.exit_code == 2 and "--concurrency" in refused.output


def test_the_image_settings_listen_on_the_named_address_keep_the_host_check_and_warn(monkeypatch, tmp_path, ui):
    # What the Dockerfile sets, and a name a user adds for a server reached by name.
    served = {}

    def record(app, **options):
        served.update(options, app=app)
        options["announce"](f"http://{options['host']}:{options['port']}/")

    monkeypatch.setattr(local, "serve", record)
    monkeypatch.setenv(local.BIND_ENV, "0.0.0.0")
    monkeypatch.setenv(local.ALLOWED_HOSTS_ENV, " evals.internal, Box.lan ,")
    result = CliRunner().invoke(cli, ["ui", "--data-dir", str(tmp_path / "home"), "--ui-dir", str(ui), "--no-open"])
    assert result.exit_code == 0, result.output
    assert served["host"] == "0.0.0.0"
    assert "Martian Evals is listening on 0.0.0.0:7600" in result.output
    assert local.EXPOSURE_WARNING in result.output
    assert "WARNING: Martian Evals has no login." in local.EXPOSURE_WARNING
    client = TestClient(served["app"], base_url=LOCAL)
    for host in ("127.0.0.1:7600", "localhost:8080", "evals.internal:7600", "box.lan"):
        assert client.post("/v1/auth/refresh", headers={"host": host}).status_code == 200, host
    for host in ("attacker.example:7600", "evals.internal.attacker.example", "0.0.0.0:7600"):
        assert client.post("/v1/auth/refresh", headers={"host": host}).status_code == 400, host


def test_the_default_command_does_not_warn(monkeypatch, tmp_path, ui):
    monkeypatch.setattr(local, "serve", lambda app, **options: options["announce"]("http://127.0.0.1:7600/"))
    result = CliRunner().invoke(cli, ["ui", "--data-dir", str(tmp_path / "home"), "--ui-dir", str(ui), "--no-open"])
    assert result.exit_code == 0, result.output
    assert "Martian Evals is at http://127.0.0.1:7600/" in result.output
    assert "WARNING" not in result.output


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("MARTIAN_EVALS_BIND", "evals.internal", "MARTIAN_EVALS_BIND must be an IPv4 address"),
        ("MARTIAN_EVALS_BIND", "::", "MARTIAN_EVALS_BIND must be an IPv4 address"),
        ("MARTIAN_EVALS_ALLOWED_HOSTS", "*", "without a port, scheme or wildcard: '*'"),
        ("MARTIAN_EVALS_ALLOWED_HOSTS", "evals.internal,*.lan", "without a port, scheme or wildcard: '*.lan'"),
        ("MARTIAN_EVALS_ALLOWED_HOSTS", "evals.internal:7600", "without a port, scheme or wildcard"),
        ("MARTIAN_EVALS_ALLOWED_HOSTS", "http://evals.internal", "without a port, scheme or wildcard"),
    ],
)
def test_an_image_setting_that_would_not_hold_is_refused(monkeypatch, tmp_path, ui, name, value, message):
    monkeypatch.setattr(local, "serve", lambda app, **options: pytest.fail("served"))
    monkeypatch.setenv(name, value)
    result = CliRunner().invoke(cli, ["ui", "--data-dir", str(tmp_path / "home"), "--ui-dir", str(ui), "--no-open"])
    assert result.exit_code == 2
    assert message in result.output


def test_the_server_listens_on_the_address_it_is_given():
    port = free_port()
    with listen(port, "0.0.0.0") as listener:
        assert listener.getsockname() == ("0.0.0.0", port)


def test_the_command_serves_the_ui_the_package_ships(monkeypatch, tmp_path):
    served = {}
    monkeypatch.setattr(local, "serve", lambda app, **options: served.update(options, app=app))
    monkeypatch.delenv("MARTIAN_EVALS_UI_DIR", raising=False)
    result = CliRunner().invoke(cli, ["ui", "--data-dir", str(tmp_path / "home"), "--no-open"])
    assert result.exit_code == 0, result.output
    client = TestClient(served["app"], base_url=LOCAL)
    assert "<title>Martian Evals</title>" in client.get("/").text
    stamp = client.get("/ui-version.json").json()
    pin = tomllib.loads((Path(__file__).resolve().parents[2] / "pyproject.toml").read_text())["tool"]["martian"]["ui"]
    assert (stamp["version"], stamp["commit"], stamp["dirty"]) == (pin["version"], pin["commit"], False)
    for asset in re.findall(r'(?:src|href)="\./(assets/[^"]+)"', client.get("/").text):
        assert client.get(f"/{asset}").status_code == 200, asset


def test_the_command_says_how_to_name_a_ui_when_the_package_has_none(monkeypatch, tmp_path):
    monkeypatch.setattr(local, "packaged_ui", lambda: tmp_path / "no-ui")
    monkeypatch.delenv("MARTIAN_EVALS_UI_DIR", raising=False)
    result = CliRunner().invoke(cli, ["ui", "--data-dir", str(tmp_path / "home"), "--no-open"])
    assert result.exit_code == 2
    assert "there is no built Evals UI in" in result.output
    assert "has lost the Evals UI it ships: reinstall it, or pass --ui-dir" in result.output


# A `/v1` path in a template literal of the built UI, with the method when the literal is the first
# argument of the API client's get, post, put, patch or delete. A literal holding a nested template
# ends at its inner backtick.
UI_LITERAL = re.compile(r"(?:\.(get|post|put|patch|delete)\(\s*)?`(/v1/[^`]*)")

# The routes the packaged UI names that the local edition does not serve, each in a view the UI leaves
# out of the local edition. The runs page's CI sessions and API keys views and the traces export call
# the Martian server's CI gate, API keys and trace export: the UI shows them only where the bootstrap's
# `hostedRoutes` is not false, and the core answers false.
HOSTED_ROUTES = {
    ("GET", "/v1/evals/ci-sessions"),
    ("GET", "/v1/evals/ci-sessions/{}"),
    ("POST", "/v1/evals/ci-sessions/{}/export"),
    ("GET", "/v1/api-keys"),
    ("POST", "/v1/api-keys"),
    ("GET", "/v1/api-keys/readiness"),
    ("DELETE", "/v1/api-keys/{}"),
    (None, "/v1/evals/traces/export"),
}
# The dataset drafts' admin scopes, which only the console's admin pages reach: the stand-alone app
# has no route to them.
ADMIN_ROUTES = {
    (None, "/v1/admin/evals/global-dataset-drafts"),
    (None, "/v1/admin/organisations/{}/evals/dataset-drafts"),
    (None, "/v1/admin/organisations/{}/evals/dataset-operations"),
}
# The known gaps: routes the packaged UI names that the local edition does not serve and that a view
# of the local edition calls.
UI_ROUTES_NOT_SERVED: set[tuple[str | None, str]] = set()


def route_of(literal: str) -> str:
    """A literal's path, each `${...}` that is a whole path segment as `{}`. Any other `${...}`, or a
    `?`, ends it: what follows is the query."""
    path, at = "", 0
    while at < len(literal) and literal[at] != "?":
        if not literal.startswith("${", at):
            path, at = path + literal[at], at + 1
            continue
        if not path.endswith("/"):
            break
        depth, end = 0, at + 1
        while end < len(literal):
            depth += {"{": 1, "}": -1}.get(literal[end], 0)
            if depth == 0:
                break
            end += 1
        path, at = path + "{}", end + 1
    return path


def test_every_route_the_packaged_ui_names_is_served_or_in_a_view_the_local_edition_leaves_out(tmp_path):
    ui = local.packaged_ui()
    named = {
        (method.upper() or None, route_of(literal))
        for script in (ui / "assets").glob("*.js")
        for method, literal in UI_LITERAL.findall(script.read_text(encoding="utf-8"))
    }
    app = create_local_app(tmp_path / "home", ui)
    paths = app.openapi()["paths"]
    served = {(method.upper(), re.sub(r"\{[^}]+\}", "{}", path)) for path, item in paths.items() for method in item}
    served_paths = {path for _, path in served}
    assert ("GET", "/v1/evals/bootstrap") in named & served
    # A literal that is not a call's first argument names a path only: any method serves it.
    missing = {call for call in named if (call not in served if call[0] else call[1] not in served_paths)}
    assert missing == HOSTED_ROUTES | ADMIN_ROUTES | UI_ROUTES_NOT_SERVED
    assert not UI_ROUTES_NOT_SERVED
    # The build reads the bootstrap's answer, and the local edition's answer hides those views.
    assert any("hostedRoutes" in script.read_text(encoding="utf-8") for script in (ui / "assets").glob("*.js"))
    bootstrap = TestClient(app, base_url=LOCAL).get("/v1/evals/bootstrap")
    assert bootstrap.json()["capabilities"]["hostedRoutes"] is False


TRACED_APPLICATION = """
import mlflow


@mlflow.trace(span_type="AGENT")
def answer(message, session, user):
    mlflow.update_current_trace(session_id=session, user=user)
    return "echo " + message


answer("Where is my order?", "conversation-1", "alice")
answer("And the invoice?", "conversation-1", "alice")
answer("Hello", "conversation-2", "bob")
"""


def tracing_settings(home: Path) -> dict[str, str]:
    """What docs/evals-core.md tells an application to set to record into the data folder."""
    return {
        "MLFLOW_TRACKING_URI": f"sqlite:///{home / 'mlflow.db'}",
        "MLFLOW_ENABLE_WORKSPACES": "true",
        "MLFLOW_WORKSPACE": "local",
        "MLFLOW_EXPERIMENT_NAME": "default",
    }


def test_an_application_using_mlflow_tracing_shows_in_traces_and_sessions(tmp_path, ui):
    home = tmp_path / "home"
    client = TestClient(create_local_app(home, ui), base_url=LOCAL)
    app = tmp_path / "application"
    app.mkdir()
    (app / "app.py").write_text(TRACED_APPLICATION, encoding="utf-8")
    # The application in a process of its own, with only MLflow and the documented settings.
    environment = {key: value for key, value in os.environ.items() if not key.startswith("MLFLOW_")}
    environment.update(tracing_settings(home), MLFLOW_DISABLE_TELEMETRY="true")
    done = subprocess.run(
        [sys.executable, "app.py"], cwd=app, env=environment, capture_output=True, text=True, timeout=120
    )
    assert done.returncode == 0, done.stderr

    traces = client.post("/v1/evals/traces/search", json={"order": "oldest"}).json()["items"]
    assert [(t["name"], t["session_id"], t["user_id"], t["state"]) for t in traces] == [
        ("answer", "conversation-1", "alice", "OK"),
        ("answer", "conversation-1", "alice", "OK"),
        ("answer", "conversation-2", "bob", "OK"),
    ]
    assert traces[0]["response"] == '"echo Where is my order?"' and traces[0]["run_id"] is None
    sessions = client.post("/v1/evals/sessions/search", json={}).json()
    assert [(s["id"], s["turns"]) for s in sessions["items"]] == [("conversation-2", 1), ("conversation-1", 2)]
    detail = client.get(f"/v1/evals/traces/{traces[0]['id']}").json()
    assert [(span["name"], span["type"]) for span in detail["spans"]] == [("answer", "AGENT")]
    assert detail["info"]["trace_metadata"]["mlflow.trace.user"] == "alice"
    # The project's experiment existed, so MLflow kept the traces' artifacts in the data folder.
    assert not (app / "mlruns").exists()


def test_a_data_folder_whose_default_experiment_mlflow_created_is_refused(tmp_path, ui):
    from martian.evals._mlflow import ServerWorkspaceContext
    from martian.evals.server.principal import SINGLE_USER
    from martian.evals.store.mlflow_local import MlflowStore

    # What MLflow tracing leaves when an application names the experiment before the project exists.
    home = tmp_path / "home"
    home.mkdir()
    store = MlflowStore(f"sqlite:///{home / 'mlflow.db'}", home / "artifacts")
    store._workspace(SINGLE_USER)
    with ServerWorkspaceContext("local"):
        store._store.create_experiment("default")
    with pytest.raises(LocalEditionError, match="cannot hold the default project: an MLflow experiment with this id"):
        create_local_app(home, ui)
