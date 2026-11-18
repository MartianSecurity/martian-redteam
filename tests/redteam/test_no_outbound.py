"""A local red team run connects only to the model it names and the target (MartianSecurity/martian-notes#328).

Each command runs in its own process under the guard of ``tests/evals/outbound_guard``, in a clean
environment with only a model key and the model's address: no Martian variable, no telemetry setting,
no CI or pytest variable. The guard refuses and records every connection, datagram and name lookup
for an address that is not loopback, records MLflow's telemetry client if it starts, and writes the
address of every loopback connection. The model (``fake_models``) and the target are loopback
servers in this test's process, so the test fails on any connection that is neither of them.

The commands are the local red team workflow end to end: validate, a run with every report format,
a second run, reports written again, two runs compared, a confirmed finding pinned and re-run, a
promptfoo config converted and its results imported, and ``martian ui`` serving the runs.
"""

from __future__ import annotations

import json
import re
import signal
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from tests.evals.outbound_guard.sitecustomize import CONNECTIONS
from tests.evals.test_no_outbound import PROCESS_SECONDS, attempts, free_port, guarded, ran, wait_until_listening
from tests.redteam.fake_models import KEY, FakeModels

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the clean environment and SIGINT are POSIX's")

ROOT = Path(__file__).resolve().parents[2]
SAMPLE = ROOT / "examples" / "promptfoo-redteam"


class Target:
    """A loopback HTTP target that leaks a secret when the attack is about its prompt."""

    def __init__(self) -> None:
        self.messages: list[str] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_POST(self) -> None:
                message = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["message"]
                owner.messages.append(message)
                reply = "LEAKED: SECRET-PLAN-42" if "prompt_leakage" in message else "No."
                data = json.dumps({"reply": reply}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def port(self) -> int:
        return self.server.server_port

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def servers():
    models, target = FakeModels(), Target()
    try:
        yield models, target
    finally:
        models.close()
        target.close()


def connected(folder: Path) -> set[tuple[str, int]]:
    log = folder / CONNECTIONS
    if not log.exists():
        return set()
    return {(host, int(port)) for host, port in (line.split() for line in log.read_text().splitlines())}


def test_a_local_red_team_workflow_reaches_only_its_model_and_its_target(tmp_path, servers):
    models, target = servers
    folder, environment = guarded(tmp_path)
    environment.update(OPENAI_API_KEY=KEY, OPENAI_API_BASE=models.base, MARTIAN_EVALS_HOME=str(tmp_path / "data"))
    environment["MARTIAN_REPORT_INCLUDE_CONTENT"] = "1"  # a finding is pinned from the attack's text
    work = tmp_path / "work"
    work.mkdir()
    run_file = work / "redteam.yaml"
    run_file.write_text(
        yaml.safe_dump(
            {
                "version": 2,
                "target": {
                    "purpose": "Help desk for a shop.",
                    "url": f"http://127.0.0.1:{target.port}/chat",
                    "body": {"message": "{{input}}"},
                    "response_path": "reply",
                },
                "models": {"attacker": "openai:/attacker-model", "judge": "openai:/judge-model"},
                "vulnerabilities": [
                    {
                        "name": "PromptLeakage",
                        "types": ["system_prompt"],
                        "checks": [{"type": "text", "all_of": ["SECRET-PLAN-42"]}, {"type": "judge"}],
                    },
                    {"name": "Toxicity", "types": ["insults"]},
                ],
                "attacks": [{"name": "PromptInjection"}],
            }
        )
    )

    def martian(*arguments: str, codes: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess[str]:
        done = subprocess.run(
            [sys.executable, "-m", "martian", *arguments],
            cwd=work,
            env=environment,
            capture_output=True,
            text=True,
            timeout=PROCESS_SECONDS,
        )
        assert done.returncode in codes, f"martian {' '.join(arguments)}: {done.returncode}\n{done.stdout}{done.stderr}"
        return done

    martian("redteam", "validate", str(run_file))
    martian("redteam", "catalog")
    # The run finds the leak (a text check, confirmed), so its gate fails with 1.
    martian("redteam", "run", str(run_file), "-f", "all", "-o", "first", codes=(1,))
    martian("redteam", "run", str(run_file), "-o", "second", codes=(1,))
    first = json.loads((work / "first" / "redteam-run.json").read_text())["id"]
    second = json.loads((work / "second" / "redteam-run.json").read_text())["id"]
    assert {path.suffix for path in (work / "first").iterdir()} >= {".json", ".md", ".html", ".csv", ".sarif", ".xml"}
    martian("redteam", "report", first, "-f", "all", "-o", "again")
    martian("redteam", "compare", first, second, "-o", "compared")
    martian("redteam", "rejudge", str(run_file), "--run", first, "-o", "rejudged", codes=(0, 1))
    martian("redteam", "pin", "add", first, "-o", "pins")
    # The pinned leak still reproduces, so the pins fail with 1.
    martian("redteam", "pin", "run", str(run_file), "--pins", "pins", "-o", "pinned", codes=(1,))
    martian("redteam", "convert", "promptfoo", str(SAMPLE / "promptfooconfig.yaml"), "--output", "converted")
    martian("redteam", "import", "promptfoo", str(SAMPLE / "results.json"))

    port = free_port()
    ui = subprocess.Popen(
        [sys.executable, "-m", "martian", "ui", "--no-open", "--port", str(port)],
        cwd=work,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        wait_until_listening(ui, port)
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=60, trust_env=False) as client:
            listed = {item["id"] for item in client.get("/v1/redteam/runs").json()["items"]}
            assert {first, second} <= listed and any(re.match(r"promptfoo-", item) for item in listed)
            assert client.get(f"/v1/redteam/runs/{first}/report").status_code == 200
            assert client.get("/v1/redteam/catalog").status_code == 200
    finally:
        ui.send_signal(signal.SIGINT)
        ui.communicate(timeout=PROCESS_SECONDS)

    assert attempts(folder) == ""  # nothing beyond loopback, and no telemetry client started
    assert connected(folder) == {("127.0.0.1", models.port), ("127.0.0.1", target.port)}
    assert models.requests and target.messages
    headers = [{name.lower(): value for name, value in request["headers"].items()} for request in models.requests]
    assert all(header.get("authorization") == f"Bearer {KEY}" for header in headers)
    for command in (
        ("redteam", "run"),
        ("redteam", "rejudge"),
        ("redteam", "pin", "run"),
        ("redteam", "import"),
        ("ui",),
    ):
        assert ran(folder, "-m", "martian", *command)


def test_the_self_test_sends_nothing_out(tmp_path):
    """The self-test's planted target sends its receipt to the run's own loopback listener."""
    (tmp_path / "selftest").mkdir()
    folder, environment = guarded(tmp_path / "selftest")
    environment["MARTIAN_EVALS_HOME"] = str(tmp_path / "data")
    done = subprocess.run(
        [sys.executable, "-m", "martian", "redteam", "selftest"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=PROCESS_SECONDS,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert attempts(folder) == ""
    assert all(host == "127.0.0.1" for host, _ in connected(folder))
    assert ran(folder, "-m", "martian", "redteam", "selftest")
