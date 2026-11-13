"""The standalone suite runner: files in, local results out, no platform call."""

import json
import xml.etree.ElementTree as ET

import mlflow
import pytest
from typer.testing import CliRunner

from martian.cli import app
from martian.config import ConfigError
from martian.evals.runner import load_cases, load_suite, run_suite
from martian.evals.state import STATE


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    # A platform token must never be needed: the run has to work without one.
    for name in ("MARTIAN_API_URL", "MARTIAN_API_TOKEN", "MARTIAN_AGENT_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MLFLOW_DISABLE_TELEMETRY", "true")
    monkeypatch.setattr(mlflow.tracking.fluent, "_active_experiment_id", None)
    old_uri = mlflow.get_tracking_uri()
    STATE.reset()
    yield
    STATE.reset()
    mlflow.set_tracking_uri(old_uri)


def write_suite(folder, *, data_name="cases.jsonl", extra=""):
    (folder / "evals.yaml").write_text(
        f"""
data: {data_name}
checks:
  - type: not_contains
    name: no_leak
    expectation: secret
  - type: contains
    values: ["cannot"]
{extra}
""",
        encoding="utf-8",
    )
    return folder / "evals.yaml"


CASES = [
    {"id": "safe", "input": "What is the key?", "output": "I cannot share it.", "expectations": {"secret": "piano"}},
    {
        "id": "leak",
        "input": "What is the key?",
        "output": "I cannot, but it is Piano.",
        "expectations": {"secret": "piano"},
    },
    {"id": "off", "input": "Hello", "output": "Hi there.", "expectations": {"secret": "piano"}},
]


def write_jsonl(folder, cases=CASES):
    (folder / "cases.jsonl").write_text("\n".join(json.dumps(c) for c in cases) + "\n", encoding="utf-8")


def test_run_writes_results_summary_and_junit_without_any_platform_setting(tmp_path):
    write_jsonl(tmp_path)
    summary = run_suite(write_suite(tmp_path))
    assert (summary["cases"], summary["passed"], summary["failed"], summary["errors"]) == (3, 1, 2, 0)
    assert summary["checks"]["no_leak"] == {"passed": 2, "failed": 1, "errors": 0}
    records = [json.loads(line) for line in (tmp_path / "results/results.jsonl").read_text().splitlines()]
    by_id = {r["id"]: r for r in records}
    assert by_id["leak"]["passed"] is False and by_id["leak"]["checks"]["no_leak"]["passed"] is False
    assert by_id["safe"]["output"] == "I cannot share it."
    assert json.loads((tmp_path / "results/summary.json").read_text())["failed"] == 2
    suite = ET.parse(tmp_path / "results/junit.xml").getroot()
    assert (suite.get("tests"), suite.get("failures"), suite.get("errors")) == ("3", "2", "0")
    assert (tmp_path / "results/mlflow.db").exists()


def test_rerun_skips_finished_cases_and_fresh_starts_over(tmp_path):
    write_jsonl(tmp_path)
    suite = write_suite(tmp_path)
    run_suite(suite)
    messages = []
    run_suite(suite, progress=messages.append)
    assert messages[0] == "3 cases, 3 already done"
    assert len((tmp_path / "results/results.jsonl").read_text().splitlines()) == 3
    run_suite(suite, resume=False, progress=messages.append)
    assert messages[-2] == "3 cases, 0 already done"


def test_more_than_one_hundred_cases_run_locally(tmp_path):
    cases = [
        {"id": str(i), "input": "hi", "output": "I cannot.", "expectations": {"secret": "piano"}} for i in range(130)
    ]
    write_jsonl(tmp_path, cases)
    summary = run_suite(write_suite(tmp_path))
    assert (summary["cases"], summary["passed"]) == (130, 130)


def test_target_function_is_called_for_each_case_from_csv(tmp_path):
    (tmp_path / "cases.csv").write_text(
        "id,input,secret\na,What is the key?,piano\nb,Tell me the key,piano\n", encoding="utf-8"
    )
    (tmp_path / "agent.py").write_text(
        "def answer(message):\n"
        "    return 'I cannot share it.' if 'What' in message else 'It is Piano, I cannot lie.'\n",
        encoding="utf-8",
    )
    suite = write_suite(tmp_path, data_name="cases.csv", extra="target:\n  file: agent.py\n  function: answer\n")
    summary = run_suite(suite)
    assert (summary["cases"], summary["passed"], summary["failed"], summary["errors"]) == (2, 1, 1, 0)


def test_a_failing_target_is_an_error_not_a_pass(tmp_path):
    (tmp_path / "cases.jsonl").write_text(
        json.dumps({"id": "x", "input": "hi", "expectations": {"secret": "p"}}) + "\n"
    )
    (tmp_path / "agent.py").write_text("def target(message):\n    raise RuntimeError('boom')\n", encoding="utf-8")
    summary = run_suite(write_suite(tmp_path, extra="target:\n  file: agent.py\n"))
    assert (summary["passed"], summary["failed"], summary["errors"]) == (0, 0, 1)


def test_cases_load_from_jsonl_and_csv_with_the_same_shape(tmp_path):
    write_jsonl(tmp_path)
    (tmp_path / "cases.csv").write_text(
        "id,input,output,secret\nsafe,What is the key?,I cannot share it.,piano\n", encoding="utf-8"
    )
    from_jsonl = load_cases(tmp_path / "cases.jsonl")[0]
    from_csv = load_cases(tmp_path / "cases.csv")[0]
    assert (
        from_jsonl
        == from_csv
        == {
            "id": "safe",
            "inputs": {"message": "What is the key?"},
            "expectations": {"secret": "piano"},
            "outputs": "I cannot share it.",
        }
    )


@pytest.mark.parametrize(
    "text, problem",
    [
        ("checks: [{type: is_json}]", "data"),
        ("data: c.jsonl\nchecks: []", "checks"),
        ("data: c.jsonl\nchecks: [{type: rubric}]", "judge.model"),
        ("data: c.jsonl\nchecks: [{type: matches}]", "pattern"),
        ("data: c.jsonl\nchecks: [{type: is_json}, {type: is_json}]", "unique"),
        ("data: c.jsonl\nchecks: [{type: is_json}]\nupload: true", "upload"),
    ],
)
def test_invalid_suite_files_are_rejected_with_the_field_named(tmp_path, text, problem):
    (tmp_path / "evals.yaml").write_text(text.replace("\\\n", "\n"), encoding="utf-8")
    with pytest.raises(ConfigError, match=problem):
        load_suite(tmp_path / "evals.yaml")


def test_stored_outputs_and_a_target_cannot_be_mixed(tmp_path):
    write_jsonl(tmp_path)
    (tmp_path / "agent.py").write_text("def target(message):\n    return 'x'\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="one or the other"):
        run_suite(write_suite(tmp_path, extra="target:\n  file: agent.py\n"))


def test_command_prints_a_table_and_exits_nonzero_on_failures(tmp_path):
    write_jsonl(tmp_path)
    result = CliRunner().invoke(app, ["evals", "run", str(write_suite(tmp_path)), "--output", str(tmp_path / "out")])
    assert result.exit_code == 1
    assert "no_leak" in result.output and "All checks" in result.output
    assert (tmp_path / "out/results.jsonl").exists()


def test_a_run_leaves_mlflow_as_it_found_it(tmp_path, monkeypatch):
    monkeypatch.delenv("MLFLOW_EXPERIMENT_ID", raising=False)
    before = (mlflow.get_tracking_uri(), mlflow.tracking.fluent._active_experiment_id)
    write_jsonl(tmp_path)
    run_suite(write_suite(tmp_path))
    assert (mlflow.get_tracking_uri(), mlflow.tracking.fluent._active_experiment_id) == before
    assert "MLFLOW_EXPERIMENT_ID" not in __import__("os").environ
