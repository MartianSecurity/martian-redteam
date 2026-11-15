"""Run the MLflow compatibility suite on the pinned mlflow-skinny and on candidate releases.

    python3 scripts/mlflow_compat.py pinned next
    python3 scripts/mlflow_compat.py pinned 3.19.0rc0

Each release is ``pinned`` (the version in pyproject.toml), ``next`` (the newest mlflow-skinny
release on PyPI, skipped when that is the pin) or an exact version. Each gets its own environment
under ``.venv-mlflow/``, built from uv.lock with only mlflow-skinny replaced, so the project's
environment is untouched. The first release run writes a store into the upgrade folder and every
later one must read it back, which is the path an existing local or on-premises store takes on
upgrade. Pass the same ``--upgrade-store`` to separate invocations (as CI does) to carry it from
one to the next. With ``MARTIAN_TEST_POSTGRES_URL`` set, the store tests also run on Postgres.
Exits 0 only if the suite passed on every release. Needs uv on PATH and Python 3.11 or later.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Every test module that runs MLflow itself: the store binding, the judge transport and the judges
# built on it, the private-import guard, the pytest kit and suite runner end to end, and the copy
# of an MLflow tracking store that the release under test writes.
SUITE = (
    "tests/evals/test_mlflow_compat.py",
    "tests/evals/test_mlflow_private.py",
    "tests/evals/test_store_mlflow.py",
    "tests/evals/test_mlflow_import.py",
    "tests/evals/test_judge_provider_boundary.py",
    "tests/evals/test_response_guidelines.py",
    "tests/evals/test_response_conditions.py",
    "tests/evals/test_response_rubric.py",
    "tests/evals/test_plugin.py",
    "tests/evals/test_runner.py",
)


def pinned_version() -> str:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    for requirement in project["optional-dependencies"]["evals"]:
        if found := re.fullmatch(r"mlflow-skinny==(\S+)", requirement):
            return found.group(1)
    raise SystemExit("pyproject.toml has no exact mlflow-skinny pin in the evals extra")


def newest_release() -> str:
    with urllib.request.urlopen("https://pypi.org/pypi/mlflow-skinny/json", timeout=30) as response:
        release: str = json.load(response)["info"]["version"]
    return release


def environment(label: str, release: str, pinned: str) -> Path:
    """An environment from uv.lock with the evals and Postgres extras and this mlflow-skinny."""
    folder = ROOT / ".venv-mlflow" / label
    command = ["uv", "sync", "--locked", "--extra", "postgres", "--quiet"]
    subprocess.run(command, cwd=ROOT, check=True, env={**os.environ, "UV_PROJECT_ENVIRONMENT": str(folder)})
    python = folder / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    if release != pinned:
        install = ["uv", "pip", "install", "--quiet", "--python", str(python), f"mlflow-skinny=={release}"]
        subprocess.run(install, cwd=ROOT, check=True)
    return python


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("releases", nargs="+", help="pinned, next or an exact mlflow-skinny version")
    parser.add_argument(
        "--upgrade-store",
        type=Path,
        help="folder the first release writes a store into (default: a new temporary folder). Keep its path "
        "short: on Postgres, MLflow keeps a run's artifact location in 200 characters",
    )
    args = parser.parse_args()

    pinned = pinned_version()
    upgrade_store = args.upgrade_store or Path(tempfile.mkdtemp(prefix="mlflow-upgrade-"))
    results: list[tuple[str, str, bool]] = []
    for label in args.releases:
        release = pinned if label == "pinned" else newest_release() if label == "next" else label
        if label == "next" and release == pinned:
            print(f"== the newest mlflow-skinny release is the pin, {pinned}: nothing newer to test", flush=True)
            continue
        safe_label = re.sub(r"[^A-Za-z0-9_.-]", "_", label)
        print(f"== mlflow-skinny {release} ({label}; pinned {pinned})", flush=True)
        python = environment(safe_label, release, pinned)
        env = {
            **os.environ,
            "MARTIAN_MLFLOW_VERSION": release,
            "MARTIAN_MLFLOW_UPGRADE_STORE": str(upgrade_store),
            "MLFLOW_DISABLE_TELEMETRY": "true",
        }
        tests = subprocess.run([str(python), "-m", "pytest", "-q", "-p", "no:cacheprovider", *SUITE], cwd=ROOT, env=env)
        results.append((label, release, tests.returncode == 0))

    print(f"== upgrade store: {upgrade_store}")
    for label, release, passed in results:
        print(f"{label:>8}  mlflow-skinny {release:<12} {'passed' if passed else 'FAILED'}")
    return 0 if all(passed for _, _, passed in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
