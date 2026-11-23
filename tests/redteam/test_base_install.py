"""`martian redteam` on a base install, `pip install martian-sdk` without the `evals` extra
(martian-notes#745).

The version 2 engine is built on the evals core: its targets are the suite file's targets, and the
local attacker and judge call models through MLflow's provider adapters. On a base install every
command that runs it or validates a version 2 run file stops with exit 2 and the install command,
before it writes a file or calls a server, never with a traceback. What the base install does
cover (version 0.2 run files, the catalogue, a campaign on the server) keeps working.

The CLI runs in a subprocess that cannot import the `evals` and `mcp` extras, nor `requests`, which
only MLflow brings."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from martian.redteam.runfile import needs_evals_core
from tests.redteam.martian_server import TOKEN, FakeMartianServer
from tests.redteam.target_servers import json_server
from tests.redteam.test_push import local_run

ROOT = Path(__file__).resolve().parents[2]
HIDDEN = {
    # evals
    "alembic",
    "fastapi",
    "mlflow",
    "pandas",
    "pytest",
    "reportlab",
    "sqlalchemy",
    "uvicorn",
    "zstandard",
    # mcp
    "mcp",
    "httpx2",
    "jsonschema",
    "referencing",
    # brought by mlflow-skinny, not by the base install
    "requests",
}
BASE_INSTALL = f"""
from importlib.abc import MetaPathFinder
import sys

class BaseInstall(MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.partition(".")[0] in {sorted(HIDDEN)!r}:
            raise ModuleNotFoundError(f"No module named {{fullname!r}}", name=fullname)

sys.meta_path.insert(0, BaseInstall())
from martian.cli import app
app()
"""
NEEDS_EVALS = "needs martian-sdk[evals]: python -m pip install 'martian-sdk[evals]'"
ENVIRONMENT_RUN = """\
version: 2
environment: refund-world
agent: 1b2c3d4e-0000-4000-8000-000000000001
target:
  purpose: "Refund assistant. It must not refund an order unless policy allows it."
  file: agent.py
  function: target
models:
  attacker: "openai:/scripted"
vulnerabilities:
  - name: ExcessiveAgency
    types: [functionality]
    checks:
      - { type: state, name: business }
    severity: high
attacks:
  - name: PromptInjection
budget: { target_queries: 20, attempts_per_probe: 1 }
output_folder: results
"""


def base_install(
    folder: Path, *arguments: str, environ: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """``martian`` with ``arguments`` in ``folder``, as a base install runs it."""
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("MARTIAN_", "OPENAI_", "MLFLOW_")) and name != "PYTHONPATH"
    }
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "src"), str(ROOT)])
    env.update(environ or {})
    return subprocess.run(
        [sys.executable, "-c", BASE_INSTALL, *arguments],
        cwd=folder,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    """The version 2 run file of the evals action's example, an environment run file like backend's
    SDK recipe, the version 0.2 example config, each with its target, and a promptfoo config."""
    shutil.copy(ROOT / "examples/github-action/redteam/redteam.yaml", tmp_path / "redteam.yaml")
    shutil.copy(ROOT / "examples/github-action/redteam/agent.py", tmp_path / "agent.py")
    (tmp_path / "environment.yaml").write_text(ENVIRONMENT_RUN, encoding="utf-8")
    shutil.copy(ROOT / "examples/config.yaml", tmp_path / "config.yaml")
    shutil.copy(ROOT / "examples/callback.py", tmp_path / "callback.py")
    shutil.copy(ROOT / "examples/promptfoo-redteam/promptfooconfig.yaml", tmp_path / "promptfooconfig.yaml")
    return tmp_path


@pytest.mark.parametrize(
    "arguments",
    [
        ["redteam", "run", "redteam.yaml"],
        ["redteam", "run", "redteam.yaml", "--no-store"],
        ["redteam", "run", "environment.yaml"],
        ["redteam", "validate", "redteam.yaml"],
        ["redteam", "rejudge", "redteam.yaml", "--run", "redteam-0123456789ab"],
        ["redteam", "pin", "run", "redteam.yaml"],
        ["redteam", "garak", "redteam.yaml", "--probes", "encoding"],
        ["redteam", "convert", "promptfoo", "promptfooconfig.yaml", "--output", "converted"],
        ["redteam", "selftest"],
    ],
    ids=lambda arguments: " ".join(arguments[1:3]),
)
def test_a_version_2_command_says_it_needs_the_evals_extra(folder: Path, arguments: list[str]) -> None:
    before = sorted(path.name for path in folder.iterdir())
    # A configured connection must not be reached: the environment run is admitted on the server.
    result = base_install(
        folder, *arguments, environ={"MARTIAN_API_URL": "http://127.0.0.1:9", "MARTIAN_API_TOKEN": TOKEN}
    )
    output = result.stdout + result.stderr
    assert result.returncode == 2, output
    assert NEEDS_EVALS in " ".join(result.stderr.split()), output
    assert "Traceback" not in output and "ModuleNotFoundError" not in output
    assert sorted(path.name for path in folder.iterdir()) == before


def test_a_version_0_2_file_validates_and_runs_past_the_imports(folder: Path) -> None:
    validated = base_install(folder, "redteam", "validate", "config.yaml")
    assert validated.returncode == 0, validated.stdout + validated.stderr
    assert "OK: 2 vulnerabilities, 2 attacks" in validated.stdout

    # No Martian connection: the run gets as far as asking for one.
    ran = base_install(folder, "redteam", "run", "config.yaml", "--no-store")
    assert ran.returncode == 2, ran.stdout + ran.stderr
    assert "configuration_error" in ran.stdout + ran.stderr
    assert "Traceback" not in ran.stdout + ran.stderr


def test_a_campaign_writes_its_reports_on_a_base_install(folder: Path) -> None:
    fake = FakeMartianServer("http://127.0.0.1:9/mcp")
    fake.campaign_run = local_run(fake.run_id, source="campaign").model_dump(mode="json")
    with json_server(fake) as (url, _):
        result = base_install(
            folder,
            "redteam",
            "campaign",
            "--agent",
            "1b2c3d4e-0000-4000-8000-000000000001",
            "--depth",
            "quick",
            "--poll",
            "1",
            "-o",
            "out",
            environ={"MARTIAN_API_URL": url.removesuffix("/chat"), "MARTIAN_API_TOKEN": TOKEN},
        )
    assert result.returncode == 0, result.stdout + result.stderr
    written = json.loads((folder / "out" / "redteam-run.json").read_text())
    assert (written["id"], written["source"]) == (fake.run_id, "campaign")
    assert (folder / "out" / "redteam-report.md").is_file()


def test_the_engine_loads_with_the_evals_extra() -> None:
    assert needs_evals_core("A version 2 run file") is None


@pytest.mark.parametrize("module", ["martian.redteam.engine.gone", "martian_redteam.gone", None])
def test_a_missing_module_of_the_sdk_itself_is_raised_not_blamed_on_the_extra(
    monkeypatch: pytest.MonkeyPatch, module: str | None
) -> None:
    import importlib

    def missing(name: str) -> None:
        raise ModuleNotFoundError(f"No module named {module!r}", name=module)

    monkeypatch.setattr(importlib, "import_module", missing)
    with pytest.raises(ModuleNotFoundError):
        needs_evals_core("A version 2 run file")


def test_a_missing_dependency_of_the_extra_names_the_install_command(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib

    def missing(name: str) -> None:
        raise ModuleNotFoundError("No module named 'mlflow.entities'", name="mlflow.entities")

    monkeypatch.setattr(importlib, "import_module", missing)
    assert needs_evals_core("A version 2 run file") == (
        "A version 2 run file needs martian-sdk[evals]: python -m pip install 'martian-sdk[evals]' (mlflow is missing)."
    )
