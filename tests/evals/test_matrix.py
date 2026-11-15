"""A suite file's matrix: one suite runs every combination of its prompts, models or other values,
each combination recorded as its own run, resumed, repeated and compared with a baseline on its own."""

import json
import shutil
import threading
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import mlflow
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from typer.testing import CliRunner

from martian.cli import app
from martian.config import ConfigError
from martian.evals import baseline, runner
from martian.evals.local import create_local_app
from martian.evals.local_targets import TargetDefinition
from martian.evals.matrix import MAX_COMBINATIONS
from martian.evals.runner import load_suite, run_suite
from martian.evals.state import STATE
from martian.evals.stats import rate_text, wilson
from martian.evals.suite_file import load_suite_file
from tests.evals.evals_contract import validate_response

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "evals" / "matrix"
COMBINATIONS = [
    "model=small-model,prompt=terse",
    "model=small-model,prompt=friendly",
    "model=large-model,prompt=terse",
    "model=large-model,prompt=friendly",
]


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    for name in ("MARTIAN_API_URL", "MARTIAN_API_TOKEN", "MARTIAN_AGENT_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MLFLOW_DISABLE_TELEMETRY", "true")
    # Runs are recorded in the local edition's data folder; never the real one in a test.
    monkeypatch.setenv("MARTIAN_EVALS_HOME", str(tmp_path / "evals-home"))
    monkeypatch.setattr(mlflow.tracking.fluent, "_active_experiment_id", None)
    old_uri = mlflow.get_tracking_uri()
    STATE.reset()
    yield
    STATE.reset()
    mlflow.set_tracking_uri(old_uri)


def example(folder: Path) -> Path:
    """A copy of examples/evals/matrix: two models and two prompts, a function target that stands
    in for the model, and deterministic checks."""
    folder.mkdir(parents=True, exist_ok=True)
    for name in ("evals.yaml", "cases.jsonl", "agent.py"):
        shutil.copy(EXAMPLE / name, folder / name)
    return folder / "evals.yaml"


def invoke(suite: Path, *extra: str):
    STATE.reset()
    # Wide enough that each table row stays on one line.
    return CliRunner().invoke(app, ["evals", "run", str(suite), *extra], env={"COLUMNS": "200"})


def local_ui(folder: Path) -> TestClient:
    ui = folder / "ui"
    ui.mkdir(exist_ok=True)
    (ui / "index.html").write_text("<!doctype html><title>Martian Evals</title>", encoding="utf-8")
    return TestClient(create_local_app(folder / "evals-home", ui), base_url="http://127.0.0.1:7600")


def records(folder: Path) -> dict[str, dict]:
    lines = (folder / "results.jsonl").read_text(encoding="utf-8").splitlines()
    return {record["id"]: record for record in map(json.loads, lines)}


def test_one_suite_file_runs_every_combination_and_records_each_as_its_own_run(tmp_path):
    result = invoke(example(tmp_path / "suite"), "--output", str(tmp_path / "out"))
    assert result.exit_code == 1, result.output  # three combinations have failed cases

    found = json.loads((tmp_path / "out" / runner.MATRIX_SUMMARY).read_text(encoding="utf-8"))
    assert found["axes"] == [
        {"name": "model", "values": ["small-model", "large-model"]},
        {"name": "prompt", "values": ["terse", "friendly"]},
    ]
    entries = found["combinations"]
    assert [entry["id"] for entry in entries] == COMBINATIONS
    assert entries[1]["values"] == {"model": "small-model", "prompt": "friendly"}
    assert [entry["gate"] for entry in entries] == ["failed", "failed", "passed", "failed"]
    # One result per combination, each with its pass rate and interval, as a run without a matrix has.
    passed = [4, 3, 6, 5]
    for entry, count in zip(entries, passed, strict=True):
        summary = entry["summary"]
        assert (summary["cases"], summary["passed"], summary["failed"], summary["errors"]) == (6, count, 6 - count, 0)
        assert summary["pass_rate"] == count / 6 and summary["confidence_95"] == list(wilson(count, 6))
        folder = tmp_path / "out" / entry["folder"]
        assert {path.name for path in folder.iterdir()} >= {"results.jsonl", "summary.json", "junit.xml"}
        assert json.loads((folder / "summary.json").read_text(encoding="utf-8")) == summary
        # Each combination is its own testsuite in its own junit.xml.
        assert ET.parse(folder / "junit.xml").getroot().get("name") == f"evals[{entry['id']}]"
        assert f"{entry['id']} " in result.output and rate_text(count / 6, wilson(count, 6)) in result.output
    assert "4 combinations of model and prompt, 6 cases each" in result.output

    # The function target got each combination's values as keyword arguments: the prompt's text,
    # not its name, and the model's name.
    friendly = records(tmp_path / "out" / "model=large-model,prompt=friendly")
    assert friendly["hours"]["output"].startswith("Hello, and thank you")
    small = records(tmp_path / "out" / "model=small-model,prompt=terse")
    assert (small["warranty"]["passed"], small["refund"]["passed"]) == (False, True)

    # The top-level verdicts.json holds every combination's, for a later run's baseline.
    bundle = json.loads((tmp_path / "out" / baseline.VERDICTS).read_text(encoding="utf-8"))
    assert (bundle["format"], list(bundle["combinations"])) == (baseline.MATRIX_FORMAT, COMBINATIONS)
    assert bundle["combinations"][COMBINATIONS[0]]["format"] == baseline.FORMAT

    # In the local Evals UI each combination is a run named after it, with the combination in its
    # provenance; the frozen contract's run schemas carry it unchanged.
    client = local_ui(tmp_path)
    runs = client.get("/v1/evals").json()
    validate_response("get", "/v1/evals", runs)
    assert sorted(run["run_code"] for run in runs) == sorted(f"evals[{name}]-1" for name in COMBINATIONS)
    detail = client.get(f"/v1/evals/{runs[0]['id']}").json()
    validate_response("get", "/v1/evals/{run_id}", detail)
    provenances = [client.get(f"/v1/evals/{run['id']}").json()["provenance"]["matrix"] for run in runs]
    assert {provenance["id"] for provenance in provenances} == set(COMBINATIONS)
    assert len({provenance["invocation"] for provenance in provenances}) == 1
    by_id = {provenance["id"]: provenance for provenance in provenances}
    assert by_id[COMBINATIONS[1]] == {
        "id": COMBINATIONS[1],
        "values": {"model": "small-model", "prompt": "friendly"},
        "axes": found["axes"],
        "invocation": provenances[0]["invocation"],
    }
    # A second run numbers each combination's runs on their own.
    invoke(tmp_path / "suite" / "evals.yaml", "--output", str(tmp_path / "out"))
    codes = sorted(run["run_code"] for run in client.get("/v1/evals").json())
    assert codes == sorted(f"evals[{name}]-{n}" for name in COMBINATIONS for n in (1, 2))


class Endpoint:
    """A loopback endpoint that records each request's body and echoes its model."""

    def __init__(self) -> None:
        self.seen: list[dict] = []
        seen = self.seen

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                seen.append(body)
                payload = json.dumps({"reply": f"{body['model']} says {body['message']}"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/chat"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def endpoint():
    found = Endpoint()
    yield found
    found.server.shutdown()
    found.server.server_close()


def test_a_url_target_sends_each_combinations_values_in_its_body(tmp_path, endpoint):
    (tmp_path / "cases.jsonl").write_text(
        "".join(json.dumps({"id": f"q{n}", "input": f"question {n}"}) + "\n" for n in range(2)), encoding="utf-8"
    )
    (tmp_path / "evals.yaml").write_text(
        f"""
data: cases.jsonl
target:
  url: "{endpoint.url}"
  body:
    message: "{{{{input}}}}"
    model: "{{{{matrix.model}}}}"
    temperature: "{{{{matrix.temperature}}}}"
    system: "Prompt: {{{{matrix.prompt}}}}"
  response_path: reply
matrix:
  model: [alpha, beta]
  temperature: [0, 0.5]
  prompt:
    echo: "Repeat {{{{input}}}} back"
checks:
  - type: contains
    values: ["alpha"]
""",
        encoding="utf-8",
    )
    found = run_suite(tmp_path / "evals.yaml")
    assert [entry["id"] for entry in found["combinations"]] == [
        "model=alpha,prompt=echo,temperature=0",
        "model=alpha,prompt=echo,temperature=0.5",
        "model=beta,prompt=echo,temperature=0",
        "model=beta,prompt=echo,temperature=0.5",
    ]
    assert [entry["gate"] for entry in found["combinations"]] == ["passed", "passed", "failed", "failed"]
    sent = {(body["model"], body["temperature"], body["message"]) for body in endpoint.seen}
    # A value that is a whole placeholder keeps its type; each case's input goes in as before.
    assert sent == {(model, t, f"question {n}") for model in ("alpha", "beta") for t in (0, 0.5) for n in range(2)}
    # A matrix value is sent as data: the {{input}} in a prompt's text is not filled in.
    assert {body["system"] for body in endpoint.seen} == {"Prompt: Repeat {{input}} back"}


def test_resume_and_repeats_work_per_combination(tmp_path):
    suite = example(tmp_path)
    said: list[str] = []
    first = run_suite(suite, repeats=2, progress=said.append)
    # Repeats apply to every combination, each with its own reliability.
    for entry in first["combinations"]:
        assert entry["summary"]["repeats"]["attempts"] == 2
        assert entry["summary"]["reliability"]["pass_rate"] == entry["summary"]["pass_rate"]
    assert said[0] == "4 combinations of model and prompt"
    assert f"{COMBINATIONS[0]} (1 of 4): 6 cases of 2 attempts each, 0 already done" in said

    # Run again: every combination's cases are reused from its own folder.
    said.clear()
    again = run_suite(suite, repeats=2, progress=said.append)
    assert [entry["summary"]["invalidated"] for entry in again["combinations"]] == [0, 0, 0, 0]
    assert all(
        f"{name} ({n} of 4): 6 cases of 2 attempts each, 6 already done" in said
        for n, name in enumerate(COMBINATIONS, 1)
    )

    # A changed prompt text keeps the combination's id, but the verdicts it produced no longer
    # stand: only the two combinations of that prompt are graded again.
    text = suite.read_text(encoding="utf-8").replace("then answer in one sentence", "then answer briefly")
    suite.write_text(text, encoding="utf-8")
    changed = run_suite(suite, repeats=2)
    assert [entry["id"] for entry in changed["combinations"]] == COMBINATIONS
    assert [entry["summary"]["invalidated"] for entry in changed["combinations"]] == [0, 6, 0, 6]

    result = invoke(suite, "--repeats", "2")
    assert "Reliability (95% CI)" in result.output and "6 cases each, 2 attempts each" in result.output


FACTS = {"refund": "14 days", "shipping": "3 to 5 working days", "returns": "30 days"}
AGENT = """
FACTS = %r
BROKEN = %r


def answer(message, model, prompt):
    topic = message.removesuffix("?")
    return "I do not know." if (model, topic) in BROKEN else FACTS[topic]
"""
MATRIX = """matrix:
  model: [%s]
  prompt: {plain: "Answer."}
"""
CHECKS = """
data: cases.jsonl
target:
  file: agent.py
  function: answer
checks:
  - type: contains
    name: answers
    expectation: answer
"""


def compared_suite(folder: Path, *, broken=(), models=("small", "large")) -> Path:
    """Three questions; with ``models``, a matrix of them and one prompt, and ``broken`` the (model,
    topic) pairs answered wrongly. Without ``models`` the suite has no matrix and always answers
    with the refund window."""
    folder.mkdir(parents=True, exist_ok=True)
    rows = [{"id": topic, "input": f"{topic}?", "expectations": {"answer": fact}} for topic, fact in FACTS.items()]
    (folder / "cases.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    if models:
        (folder / "agent.py").write_text(AGENT % (FACTS, set(broken)), encoding="utf-8")
        text = CHECKS + MATRIX % ", ".join(models)
    else:
        (folder / "agent.py").write_text("def answer(message):\n    return '14 days'\n", encoding="utf-8")
        text = CHECKS
    (folder / "evals.yaml").write_text(text, encoding="utf-8")
    return folder / "evals.yaml"


def test_each_combination_is_compared_with_the_same_combination_of_the_baseline(tmp_path):
    # Main: the small model gets returns wrong, the large model gets everything right.
    main = invoke(compared_suite(tmp_path / "suite", broken=[("small", "returns")]), "--output", str(tmp_path / "main"))
    assert main.exit_code == 1
    # The pull request fixes the small model, breaks the large one on refunds, and adds a model.
    suite = compared_suite(
        tmp_path / "suite", broken=[("large", "refund"), ("new", "refund")], models=("small", "large", "new")
    )
    result = invoke(suite, "--output", str(tmp_path / "pr"), "--baseline", str(tmp_path / "main"))
    found = {
        entry["id"]: entry
        for entry in json.loads((tmp_path / "pr" / runner.MATRIX_SUMMARY).read_text())["combinations"]
    }
    small, large, new = (found[f"model={name},prompt=plain"] for name in ("small", "large", "new"))
    # Each combination is paired with its own result on main: the small model's fix and the
    # large model's break, not the other way round.
    assert (small["comparison"]["improved"], small["comparison"]["regressed"]) == (["returns"], [])
    assert large["comparison"]["regressed"] == [{"id": "refund", "checks": {"answers": "1 of 1 values not found"}}]
    assert large["comparison"]["improved"] == []  # paired with the small model, returns would have improved
    assert (small["gate"], large["gate"]) == ("passed", "passed")  # one changed case is within noise
    assert json.loads((tmp_path / "pr" / large["folder"] / baseline.COMPARISON).read_text()) == large["comparison"]
    # A combination main has no result of is gated on its own: its failed case fails the run.
    assert (new["comparison"], new["gate"]) == (None, "failed")
    assert not (tmp_path / "pr" / new["folder"] / baseline.COMPARISON).exists()
    assert result.exit_code == 1, result.output
    assert "Against the baseline" in result.output
    assert (
        f"model=new,prompt=plain: Compared with {tmp_path / 'main'}: it holds no result of this combination"
        in result.output
    )

    # A baseline of the suite before it had a matrix holds no combination's result, and a suite
    # without a matrix finds none in a matrix's baseline: each is gated on its own.
    (tmp_path / "plain").mkdir()
    (tmp_path / "plain" / baseline.VERDICTS).write_text(
        (tmp_path / "pr" / small["folder"] / baseline.VERDICTS).read_text(), encoding="utf-8"
    )
    alone = run_suite(suite, output=tmp_path / "again", baseline=tmp_path / "plain")
    assert {entry["comparison"] is None for entry in alone["combinations"]} == {True}
    without = compared_suite(tmp_path / "unmatrixed", models=())
    plain = invoke(without, "--output", str(tmp_path / "unmatrixed-out"), "--baseline", str(tmp_path / "main"))
    assert plain.exit_code == 1 and "it holds no result of this suite, so it is gated on its own" in plain.output
    assert not (tmp_path / "unmatrixed-out" / baseline.COMPARISON).exists()


def test_a_suite_without_a_matrix_runs_and_records_exactly_as_before(tmp_path):
    suite = compared_suite(tmp_path, models=())
    model, _ = load_suite_file(suite)
    assert model.matrix == {} and "matrix" not in model.model_dump()
    # A matrix.json an earlier matrix run left behind goes; nothing else of a matrix is written.
    (tmp_path / "results").mkdir()
    (tmp_path / "results" / runner.MATRIX_SUMMARY).write_text("{}", encoding="utf-8")
    summary = run_suite(suite)
    assert "combinations" not in summary and not (tmp_path / "results" / runner.MATRIX_SUMMARY).exists()
    assert json.loads((tmp_path / "results" / baseline.VERDICTS).read_text())["format"] == baseline.FORMAT
    assert summary["run"]["code"] == "evals-1"
    detail = local_ui(tmp_path).get(f"/v1/evals/{summary['run']['id']}").json()
    assert "matrix" not in detail["provenance"]
    # Its fingerprint is the one it had before matrices existed: no matrix in it.
    file_suite, base = load_suite(suite)
    assert runner._suite_fingerprint(file_suite, base) == runner._suite_fingerprint(file_suite, base, None)
    with_matrix = runner._suite_fingerprint(file_suite, base, {"model": "small"})
    assert with_matrix != runner._suite_fingerprint(file_suite, base)


SUITE_HEAD = "data: cases.jsonl\nchecks:\n  - type: contains\n    values: [x]\n"
URL_TARGET = 'target:\n  url: "http://127.0.0.1:9/chat"\n  body:\n    message: "{{input}}"\n'


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        (URL_TARGET + "matrix:\n  model: [a, b]\n", "target.body has no {{matrix.model}}, so every value"),
        (URL_TARGET + '    model: "{{matrix.modl}}"\nmatrix:\n  model: [a]\n', "{{matrix.modl}} names no axis"),
        (URL_TARGET + '    model: "{{matrix.model}}"\n', "{{matrix.model}} names no axis"),
        ("matrix:\n  model: [a, b]\n", "a matrix varies the target"),
        ("target:\n  file: agent.py\nmatrix: {}\n", "a matrix needs at least one axis"),
        ("target:\n  file: agent.py\nmatrix:\n  model: []\n", "matrix.model has no values"),
        ("target:\n  file: agent.py\nmatrix:\n  2model: [a]\n", "'2model' is not an axis name"),
        (
            'target:\n  file: agent.py\nmatrix:\n  prompt: ["Answer briefly."]\n',
            "give matrix.prompt as a mapping of short names to values",
        ),
        ("target:\n  file: agent.py\nmatrix:\n  prompt: {'a b': x}\n", "'a b' is not a value name"),
        ("target:\n  file: agent.py\nmatrix:\n  model: [1, '1']\n", "matrix.model names '1' twice"),
        ("target:\n  file: agent.py\nmatrix:\n  model: {a: null}\n", "matrix.model.a has no value"),
        ("target:\n  file: agent.py\nmatrix:\n  model: ['a:b', a_b]\n", "would share the results folder model=a_b"),
        (
            f"target:\n  file: agent.py\nmatrix:\n  a: {list(range(11))}\n  b: {list(range(10))}\n",
            f"the matrix has 110 combinations; at most {MAX_COMBINATIONS}",
        ),
    ],
)
def test_a_matrix_that_cannot_run_is_refused_before_any_case(tmp_path, text, problem):
    (tmp_path / "cases.jsonl").write_text(json.dumps({"id": "a", "input": "hi"}) + "\n", encoding="utf-8")
    (tmp_path / "agent.py").write_text("def target(message, **values):\n    return message\n", encoding="utf-8")
    (tmp_path / "evals.yaml").write_text(SUITE_HEAD + text, encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid suite file") as refused:
        run_suite(tmp_path / "evals.yaml")
    assert problem in str(refused.value)
    assert not (tmp_path / "results").exists() and not (tmp_path / "evals-home").exists()


def test_an_axis_a_function_target_cannot_tell_from_an_input_is_refused(tmp_path):
    case = {"id": "a", "inputs": {"message": "hi", "model": "x"}}
    (tmp_path / "cases.jsonl").write_text(json.dumps(case) + "\n", encoding="utf-8")
    (tmp_path / "agent.py").write_text("def target(message, model):\n    return message\n", encoding="utf-8")
    suite = SUITE_HEAD + "target:\n  file: agent.py\nmatrix:\n  model: [a]\n"
    (tmp_path / "evals.yaml").write_text(suite, encoding="utf-8")
    with pytest.raises(ConfigError, match="case a has an input named model, and the function target also takes"):
        run_suite(tmp_path / "evals.yaml")


TOOL_AGENT = """\
import mlflow
from mlflow.entities import SpanType


def answer(message, mode):
    tools = ["lookup_order"] if mode == "careful" else ["lookup_order", "close_account"]
    for calls in (tools, []):
        with mlflow.start_span(name="chat", span_type=SpanType.CHAT_MODEL) as model:
            model.set_outputs({"role": "assistant", "content": "ok"})
        for name in calls:
            with mlflow.start_span(name=name, span_type=SpanType.TOOL) as tool:
                tool.set_inputs({"order_id": "A-1001"})
    return "Done."
"""


def test_tool_call_and_python_checks_grade_each_combinations_own_call(tmp_path):
    (tmp_path / "cases.jsonl").write_text(json.dumps({"id": "a", "input": "Where is A-1001?"}) + "\n")
    (tmp_path / "agent.py").write_text(TOOL_AGENT, encoding="utf-8")
    (tmp_path / "scorers.py").write_text("def given(inputs):\n    return True, ', '.join(sorted(inputs))\n")
    suite = (
        "data: cases.jsonl\ntarget:\n  file: agent.py\n  function: answer\nmatrix:\n  mode: [careful, reckless]\n"
        "checks:\n  - type: tool_not_called\n    calls: [close_account]\n"
        "  - type: python\n    name: given\n    file: scorers.py\n    function: given\n"
    )
    (tmp_path / "evals.yaml").write_text(suite, encoding="utf-8")
    summary = run_suite(tmp_path / "evals.yaml")
    assert [(entry["id"], entry["gate"]) for entry in summary["combinations"]] == [
        ("mode=careful", "passed"),
        ("mode=reckless", "failed"),
    ]
    careful, reckless = (records(tmp_path / "results" / name)["a"] for name in ("mode=careful", "mode=reckless"))
    # Each combination's tool-call check reads the trace of that combination's call.
    assert careful["checks"]["tool_not_called"]["passed"] is True
    assert reckless["checks"]["tool_not_called"]["passed"] is False
    assert "close_account" in reckless["checks"]["tool_not_called"]["rationale"]
    # A python check is given the case's inputs, not the combination's values.
    assert careful["checks"]["given"]["rationale"] == reckless["checks"]["given"]["rationale"] == "message"


def test_a_target_defined_outside_a_suite_file_has_no_matrix_to_fill():
    """The local edition's targets share the suite file's body rules; they have no matrix, so a
    {{matrix.NAME}} there would never be filled and is refused when the target is defined."""
    body = {"message": "{{input}}", "model": "{{matrix.model}}"}
    with pytest.raises(ValidationError, match="names no axis of the suite file's matrix"):
        TargetDefinition(name="agent", kind="http", url="http://127.0.0.1:9/chat", body=body)
