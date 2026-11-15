"""Nothing leaves the machine, and MLflow's telemetry is off (MorningHQ/martian-notes#169).

The product's own processes run here with the guard in ``tests/evals/outbound_guard``, which
refuses and records every connection, datagram and name lookup for an address that is not
loopback, and records MLflow's telemetry client if it starts. A test fails on any record.

Each process starts in a clean environment: a new home folder and no CI or pytest variables,
under which MLflow turns its telemetry off by itself and would hide the product's own default,
and no telemetry settings. The first test shows that the guard catches each kind of attempt in
that environment, so the others prove something.

- A local run (``martian evals run``) of a function target with a deterministic check, then the
  local edition (``martian ui``) serving the Evals UI and that run.
- The evals core with a process worker, running a function target in a run's own process.
- The Evals UI the wheel ships loads nothing from another host.

Calls to a model or a target the user configures are the user's choice; nothing here makes one.
"""

from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest

from martian.evals.local import packaged_ui
from tests.evals.outbound_guard.sitecustomize import ATTEMPTS, FOLDER_ENV, TELEMETRY_STARTED

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the clean environment and SIGINT are POSIX's")

ROOT = Path(__file__).resolve().parents[2]
GUARD = Path(__file__).resolve().parent / "outbound_guard"
PROCESS_SECONDS = 300
KEPT = ("PATH", "LANG", "LC_ALL", "TMPDIR")
"""The only variables a guarded process inherits from the test's environment."""

TEXT_HOSTS = frozenset({"react.dev", "base-ui.com", "www.w3.org", "local.invalid"})
"""Hosts the built UI's scripts name without loading anything from them: the links in React's and
Base UI's error messages, XML namespace names, and the placeholder base the UI resolves relative
addresses against. A new host in a rebuilt UI fails the bundle test until someone checks it."""


def guarded(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """The guard's folder and the clean environment that runs a process under the guard."""
    folder = tmp_path / "guard"
    folder.mkdir()
    environment = {name: os.environ[name] for name in KEPT if name in os.environ}
    environment.update(HOME=str(tmp_path / "home"), PYTHONPATH=str(GUARD), **{FOLDER_ENV: str(folder)})
    return folder, environment


def attempts(folder: Path) -> str:
    log = folder / ATTEMPTS
    return log.read_text(encoding="utf-8") if log.exists() else ""


def commands(folder: Path) -> list[list[str]]:
    """The command line of every process that ran under the guard."""
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(folder.glob("started-*.json"))]


def ran(folder: Path, *words: str) -> bool:
    """Whether a guarded process's command line held ``words`` in order."""
    joined = " ".join(words)
    return any(joined in " ".join(command) for command in commands(folder))


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


def wait_until_listening(process: subprocess.Popen[str], port: int) -> None:
    deadline = time.monotonic() + PROCESS_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(f"martian ui exited with {process.returncode}: {process.communicate()[0]}")
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return
        except OSError:
            time.sleep(0.2)
    raise AssertionError("martian ui did not start listening")


def write_suite(folder: Path) -> Path:
    """A suite of a function target and a deterministic check: no judge, no URL target."""
    folder.mkdir()
    (folder / "agent.py").write_text('def answer(message: str) -> str:\n    return f"You asked: {message}"\n')
    cases = [{"id": name, "input": name, "expectations": {"echo": "You asked"}} for name in ("hi", "bye")]
    (folder / "cases.jsonl").write_text("".join(json.dumps(case) + "\n" for case in cases))
    suite = folder / "evals.yaml"
    suite.write_text(
        "data: cases.jsonl\n"
        "target:\n  file: agent.py\n  function: answer\n"
        "checks:\n  - type: contains\n    name: echoes\n    expectation: echo\n"
        "output: results\n"
    )
    return suite


def test_the_guard_records_a_connection_a_lookup_and_mlflow_telemetry_in_the_clean_environment(tmp_path):
    folder, environment = guarded(tmp_path)
    # Reserved addresses, so that nothing would leave even without the guard: TEST-NET-1 and .invalid.
    attempt = (
        "import socket\n"
        "def connect():\n"
        "    with socket.socket() as sock:\n"
        "        sock.settimeout(1)\n"
        "        sock.connect(('192.0.2.1', 9))\n"
        "def lookup():\n"
        "    socket.getaddrinfo('example.invalid', 443)\n"
        "for call in (connect, lookup):\n"
        "    try:\n"
        "        call()\n"
        "    except OSError:\n"
        "        pass\n"
        "import mlflow\n"  # without martian.evals first: MLflow's own default
    )
    subprocess.run([sys.executable, "-c", attempt], env=environment, check=True, timeout=PROCESS_SECONDS)
    recorded = attempts(folder)
    assert "socket.connect ('192.0.2.1', 9)" in recorded
    assert "socket.getaddrinfo 'example.invalid'" in recorded
    assert TELEMETRY_STARTED in recorded


def test_a_local_run_and_the_local_edition_send_nothing_out(tmp_path):
    folder, environment = guarded(tmp_path)
    data = tmp_path / "data"
    suite = write_suite(tmp_path / "suite")
    run = subprocess.run(
        [sys.executable, "-m", "martian", "evals", "run", str(suite), "--data-dir", str(data)],
        cwd=suite.parent,
        env=environment,
        capture_output=True,
        text=True,
        timeout=PROCESS_SECONDS,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    summary = json.loads((suite.parent / "results" / "summary.json").read_text(encoding="utf-8"))
    assert (summary["cases"], summary["passed"]) == (2, 2)

    port = free_port()
    ui = subprocess.Popen(
        [sys.executable, "-m", "martian", "ui", "--no-open", "--port", str(port), "--data-dir", str(data)],
        cwd=tmp_path,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        wait_until_listening(ui, port)
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=60, trust_env=False) as client:
            page = client.get("/")
            assert page.status_code == 200
            files = re.findall(r'(?:src|href)="\./([^"]+)"', page.text)  # the script, styles and icon
            loaded = [client.get(f"/{name}") for name in files]
            assert files and all(response.status_code == 200 for response in loaded)
            fonts = {name for file in loaded for name in re.findall(r"url\(\./([^)]+\.woff2)\)", file.text)}
            assert fonts and all(client.get(f"/assets/{name}").status_code == 200 for name in fonts)
            assert client.post("/v1/auth/refresh").status_code == 200
            assert client.get("/v1/evals/bootstrap").status_code == 200
            runs = client.get("/v1/evals").json()
            assert len(runs) == 1, runs
            assert client.get(f"/v1/evals/{runs[0]['id']}").status_code == 200
            assert client.get(f"/v1/evals/{runs[0]['id']}/workbench").status_code == 200
            traces = client.post("/v1/evals/traces/search", json={}).json()["items"]
            assert len(traces) == 2, traces
            assert client.get(f"/v1/evals/traces/{traces[0]['id']}").status_code == 200
    finally:
        ui.send_signal(signal.SIGINT)
        ui.communicate(timeout=PROCESS_SECONDS)
    assert attempts(folder) == ""
    assert ran(folder, "-m", "martian", "evals", "run") and ran(folder, "-m", "martian", "ui")


def test_the_core_and_its_process_worker_send_nothing_out(tmp_path):
    folder, environment = guarded(tmp_path)
    stores = tmp_path / "stores"
    stores.mkdir()
    served = subprocess.run(
        [sys.executable, "-m", "tests.evals.no_outbound_fixture", str(stores)],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=PROCESS_SECONDS,
    )
    assert served.returncode == 0, served.stdout + served.stderr
    run = json.loads(served.stdout.splitlines()[-1])
    assert (run["state"], run["passed"], run["failed"]) == ("completed", 1, 1), run
    assert attempts(folder) == ""
    assert ran(folder, "-m", "tests.evals.no_outbound_fixture") and ran(folder, "-m", "martian.evals.worker")


def test_the_packaged_ui_loads_nothing_from_another_host():
    ui = packaged_ui()
    page = (ui / "index.html").read_text(encoding="utf-8")
    references = re.findall(r'(?:src|href)="([^"]+)"', page)
    assert references and not [ref for ref in references if urlsplit(ref).scheme or urlsplit(ref).netloc]
    for style in (ui / "assets").glob("*.css"):
        text = style.read_text(encoding="utf-8")
        assert "@import" not in text, style.name
        loaded = re.findall(r"url\(\s*['\"]?([^'\")]+)", text)
        assert not [ref for ref in loaded if urlsplit(ref).scheme not in ("", "data") or urlsplit(ref).netloc]
    named = {
        host
        for script in (ui / "assets").glob("*.js")
        for host in re.findall(r"https?://([A-Za-z0-9.-]+)", script.read_text(encoding="utf-8"))
    }
    assert named <= TEXT_HOSTS, f"check what the UI does with {sorted(named - TEXT_HOSTS)}"
