"""Public runners over loopback contract fixtures, deliberately not live-model qualification."""

import json
import os
import subprocess
import sys

import pytest

from .stub_backend import StubBackend
from .test_plugin import SAMPLE


@pytest.mark.parametrize(
    "interface,score,upload_failure",
    [("cli", 1, False), ("python", 1, False), ("evals", 1, False), ("cli", None, False), ("cli", 1, True)],
)
def test_one_session_preserves_suite_results_errors_and_failed_upload_receipt(
    tmp_path, interface, score, upload_failure
):
    (tmp_path / "test_agent.py").write_text(SAMPLE)
    (tmp_path / "callback.py").write_text(
        "from pathlib import Path\ndef model_callback(prompt):\n"
        '    with Path(__file__).with_suffix(".calls").open("a") as f: f.write("call\\n")\n'
        '    return "private-target-output"\n'
    )
    (tmp_path / "redteam.yml").write_text(
        "target:\n  purpose: Safe test assistant\n  callback:\n    file: callback.py\n"
        "vulnerabilities:\n  - name: PromptLeakage\n    types: [system_prompt]\n"
        "attacks:\n  - name: PromptInjection\n"
    )
    environment = {k: v for k, v in os.environ.items() if not k.startswith(("MARTIAN_", "MLFLOW_", "GITHUB_"))}
    with StubBackend() as backend:
        backend.redteam_score = score
        requests = backend.redteam_requests
        environment.update(
            SCENARIO="pass",
            MARTIAN_API_URL=backend.url,
            MARTIAN_API_TOKEN=backend.token,
            MARTIAN_AGENT_ID="11111111-1111-4111-8111-111111111111",
            MARTIAN_EVALS_GIT_SHA="0123456789abcdef0123456789abcdef01234567",
            MARTIAN_EVALS_STORE_DIR=str(tmp_path / "store"),
            MARTIAN_REDTEAM_API_URL="invalid-unused-legacy-url",
            MARTIAN_REDTEAM_API_KEY="test-only-agents-credential",
        )
        if interface == "evals":
            environment["MARTIAN_REDTEAM_API_URL"] = "invalid-unused-service-url"
        if upload_failure:
            backend.session_statuses = [500]
        if interface in ("cli", "evals"):
            command = [
                sys.executable,
                "-m",
                "martian",
                "ci",
                "--evals-path",
                "test_agent.py",
                "--redteam-config",
                "redteam.yml",
                "--output",
                "results",
            ]
            if interface == "evals":
                command.remove("--redteam-config")
                command.remove("redteam.yml")
        else:
            for name in ("MARTIAN_API_URL", "MARTIAN_API_TOKEN", "MARTIAN_AGENT_ID", "MARTIAN_EVALS_GIT_SHA"):
                environment.pop(name)
            (tmp_path / "runner.py").write_text(
                "from pathlib import Path\nfrom martian import MartianConfig, CiContext\n"
                "from martian.ci import CiSuite, run\n"
                f"config = MartianConfig({backend.url!r}, {backend.token!r})\n"
                "ci = CiContext(agent_id='11111111-1111-4111-8111-111111111111', git_sha='0123456789abcdef')\n"
                'suite = CiSuite(redteam_config=Path("redteam.yml"), output_dir=Path("results"))\n'
                "result = run(suite, config=config, ci=ci)\n"
                "raise SystemExit(result.exit_code)\n"
            )
            command = [sys.executable, "runner.py"]
        process = subprocess.run(command, cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=120)
        expected = 1 if score is None or upload_failure else 0
        assert process.returncode == expected, process.stdout + process.stderr
        assert len(backend.sessions()) == 1, "the shared runner must make one session upload"
        tests = backend.sessions()[0][3]["tests"]
        assert any(t["kind"] == "redteam" for t in tests) is (interface != "evals")
        assert any(t["kind"] == "dataset" for t in tests) is (interface != "python")
        if interface == "evals":
            assert requests == []
            assert not (tmp_path / "callback.calls").exists()
        else:
            assert [r[1] for r in backend.requests if r[1].startswith("/v1/redteam/")] == [
                "/v1/redteam/simulate",
                "/v1/redteam/evaluate",
            ]
            assert all(r[2]["Authorization"] == "Bearer " + backend.token for r in backend.requests)
            assert (tmp_path / "callback.calls").read_text() == "call\n"
        receipt = json.loads((tmp_path / "results/ci-session.json").read_text())
        assert receipt["state"] == ("failed" if expected else "passed")
        assert receipt["upload"] == ("failed" if upload_failure else "persisted")
        reports = "\n".join(p.read_text() for p in (tmp_path / "results").rglob("*") if p.is_file())
        for private in (
            "private-target-output",
            "Protected private content",
            backend.token,
            environment["MARTIAN_REDTEAM_API_KEY"],
        ):
            assert private not in reports
        assert (tmp_path / "results/redteam/probes.jsonl").is_file() is (interface != "evals")


def test_evals_requests_use_connection_timeout(tmp_path):
    (tmp_path / "test_agent.py").write_text(SAMPLE)
    (tmp_path / "runner.py").write_text(
        "from pathlib import Path\n"
        "import httpx\n"
        "from martian import CiContext, MartianConfig\n"
        "from martian.ci import CiSuite, run\n"
        "from os import environ\n"
        "requests = []\n"
        "send = httpx.Client.send\n"
        "def check_timeout(client, request, **kwargs):\n"
        "    if request.url.path.startswith('/v1/evals/ci/'):\n"
        "        requests.append(request.url.path)\n"
        "        assert set(request.extensions['timeout'].values()) == {7.0}\n"
        "    return send(client, request, **kwargs)\n"
        "httpx.Client.send = check_timeout\n"
        "connection = MartianConfig(\n"
        "    environ['MARTIAN_API_URL'], environ['MARTIAN_API_TOKEN'], request_timeout_seconds=7\n"
        ")\n"
        "ci = CiContext(agent_id='11111111-1111-4111-8111-111111111111', git_sha='0123456789abcdef')\n"
        "suite = CiSuite(evals_paths=(Path('test_agent.py'),), output_dir=Path('results'))\n"
        "result = run(suite, config=connection, ci=ci)\n"
        "assert result.exit_code == 0, result.problems\n"
        "assert requests == ['/v1/evals/ci/datasets/support', '/v1/evals/ci/runs', '/v1/evals/ci/sessions']\n"
    )
    environment = {k: v for k, v in os.environ.items() if not k.startswith(("MARTIAN_", "MLFLOW_", "GITHUB_"))}
    with StubBackend() as backend:
        environment.update(SCENARIO="pass", MARTIAN_API_URL=backend.url, MARTIAN_API_TOKEN=backend.token)
        process = subprocess.run(
            [sys.executable, "runner.py"], cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=120
        )
    assert process.returncode == 0, process.stdout + process.stderr
