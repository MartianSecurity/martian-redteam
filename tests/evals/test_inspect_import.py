"""Inspect AI logs imported as runs: opened, compared and reviewed through the local edition, with
every score as the log holds it.

The logs in ``tests/fixtures/inspect`` are real Inspect AI 0.3.289 logs of ``fixture_task.py``
there: ``glm5.json`` one run on GLM 5 in Inspect's JSON format, ``mockllm-epochs.eval`` two epochs
on Inspect's mock model in its ``.eval`` format, compressed with Zstandard. Only the local paths
in their error tracebacks were replaced.
"""

import io
import json
import math
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from martian.cli import app
from martian.evals.inspect_import import (
    InspectLogError,
    InspectScore,
    import_inspect_log,
    import_inspect_logs,
    inspect_scores,
    read_inspect_log,
)
from martian.evals.local import create_local_app
from tests.evals.evals_contract import validate_response

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "inspect"
GLM5 = FIXTURES / "glm5.json"
MOCK = FIXTURES / "mockllm-epochs.eval"

# Every sample's scores as Inspect reads them (``read_eval_log``), by case id. A dict value is
# one score per key; None is Inspect's unscored NaN.
GLM5_SCORES = {
    "sum": {
        "includes": "C",
        "model_graded_qa": "C",
        "tool_use/as_expected": True,
        "tool_use/calls": 1,
        "similarity": 1.0,
    },
    "capital": {
        "includes": "C",
        "model_graded_qa": "C",
        "tool_use/as_expected": True,
        "tool_use/calls": 0,
        "similarity": 1.0,
    },
    "secret": {
        "includes": "I",
        "model_graded_qa": "C",
        "tool_use/as_expected": True,
        "tool_use/calls": 0,
        "similarity": 0.2902,
    },
    "broken": {},
}
MOCK_EPOCH = {
    "sum": {
        "includes": "I",
        "model_graded_qa": None,
        "tool_use/as_expected": False,
        "tool_use/calls": 0,
        "similarity": 0.0,
    },
    "capital": {
        "includes": "I",
        "model_graded_qa": None,
        "tool_use/as_expected": True,
        "tool_use/calls": 0,
        "similarity": 0.1053,
    },
    "secret": {
        "includes": "I",
        "model_graded_qa": None,
        "tool_use/as_expected": True,
        "tool_use/calls": 0,
        "similarity": 0.0769,
    },
    "broken": {},
}
MOCK_SCORES = {
    **MOCK_EPOCH,
    **{f"{case} (epoch 2)": scores for case, scores in MOCK_EPOCH.items()},
}


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("MLFLOW_DISABLE_TELEMETRY", "true")
    monkeypatch.setenv("MARTIAN_EVALS_HOME", str(tmp_path / "evals-home"))


def local_ui(home: Path) -> TestClient:
    """The local edition on the data folder the logs were imported into, as `martian ui` serves it."""
    ui = home.parent / "ui"
    ui.mkdir(exist_ok=True)
    (ui / "index.html").write_text("<!doctype html><title>Martian Evals</title>", encoding="utf-8")
    return TestClient(create_local_app(home, ui), base_url="http://127.0.0.1:7600")


def items_of(client: TestClient, run_id: str) -> dict[str, dict]:
    detail = client.get(f"/v1/evals/{run_id}").json()
    validate_response("get", "/v1/evals/{run_id}", detail)
    return {item["dataset_item_id"]: item for item in detail["items"]}


def scores_of(client: TestClient, run_id: str) -> dict[str, dict[str, dict]]:
    """The run's scores by case id and scorer, as the workbench serves them."""
    items = {item["id"]: case for case, item in items_of(client, run_id).items()}
    found: dict[str, dict[str, dict]] = {case: {} for case in items.values()}
    for assessment in client.get(f"/v1/evals/{run_id}/workbench").json()["assessments"]:
        found[items[assessment["item_id"]]][assessment["name"]] = assessment
    return found


def inspect_metric(values: list) -> float:
    """Inspect's ``accuracy`` and ``mean`` over one score's values: C is 1, P 0.5, I and N 0;
    booleans and numbers as themselves. Unscored values are left out."""
    scale = {"C": 1.0, "P": 0.5, "I": 0.0, "N": 0.0}
    numbers = [scale[v] if isinstance(v, str) else float(v) for v in values if v is not None]
    return sum(numbers) / len(numbers) if numbers else math.nan


@pytest.mark.parametrize(("log", "expected"), [(GLM5, GLM5_SCORES), (MOCK, MOCK_SCORES)])
def test_scores_match_the_inspect_log(tmp_path, log, expected):
    run = import_inspect_log(log, tmp_path / "evals-home")
    found = scores_of(local_ui(tmp_path / "evals-home"), run.id)
    # Every value is kept exactly, its type too: a boolean stays a boolean, a count an integer.
    assert {case: {name: a["value"] for name, a in scores.items()} for case, scores in found.items()} == expected
    for case, scores in found.items():
        for name, assessment in scores.items():
            assert type(assessment["value"]) is type(expected[case][name])
            if assessment["value"] is None:
                assert assessment["error"] == "unscored: grader_failed"
                assert assessment["provenance"]["inspect.value"] == "NaN"
    # Inspect's own metrics, which the run keeps, follow from the imported values.
    results = run.provenance["inspect"]["results"]["scores"]
    assert len(results) == 5
    for result in results:
        name = result["scorer"] if result["name"] == result["scorer"] else f"{result['scorer']}/{result['name']}"
        metric = next(m["value"] for key, m in result["metrics"].items() if key in ("accuracy", "mean"))
        recomputed = inspect_metric([scores[name]["value"] for scores in found.values() if name in scores])
        assert (metric == "NaN" and math.isnan(recomputed)) or metric == pytest.approx(recomputed)


def test_an_imported_run_opens_with_its_cases_messages_and_tool_calls(tmp_path):
    home = tmp_path / "evals-home"
    run = import_inspect_log(GLM5, home)
    client = local_ui(home)

    runs = client.get("/v1/evals").json()
    validate_response("get", "/v1/evals", runs)
    assert [(r["id"], r["run_code"], r["state"], r["source"]) for r in runs] == [
        (run.id, "inspect-importer_fixture-1", "completed", "local")
    ]
    assert (runs[0]["planned_count"], runs[0]["passed"], runs[0]["failed"], runs[0]["errored"]) == (4, 2, 1, 1)
    items = items_of(client, run.id)
    assert [(case, item["status"]) for case, item in items.items()] == [
        ("sum", "passed"),
        ("capital", "passed"),
        ("secret", "failed"),
        ("broken", "error"),
    ]
    assert (items["sum"]["prompt"], items["sum"]["expected"], items["sum"]["output"]) == (
        "Use the add tool to work out 17 + 25. Answer with the number only.",
        "42",
        "42",
    )
    assert items["broken"]["error"] == "RuntimeError('this sample fails on purpose')"

    # The sample's messages and its tool call are the trace's spans, timed by Inspect's events.
    trace = client.get(f"/v1/evals/{run.id}/items/{items['sum']['id']}/trace").json()
    assert [(s["name"], s["type"], s["depth"]) for s in trace["spans"]] == [
        ("importer_fixture", "AGENT", 0),
        ("zai.glm-5", "CHAT_MODEL", 1),
        ("add", "TOOL", 1),
        ("zai.glm-5", "CHAT_MODEL", 1),
    ]
    first, tool, second = trace["spans"][1:]
    assert [m["role"] for m in first["inputs"]] == ["system", "user"]
    assert first["outputs"]["tool_calls"][0]["arguments"] == {"x": 17, "y": 25}
    assert (tool["inputs"], tool["outputs"], tool["status"]["code"]) == ({"x": 17, "y": 25}, "42", "STATUS_CODE_OK")
    assert [m["role"] for m in second["inputs"]] == ["tool"] and second["outputs"]["content"] == "42"
    assert all(s["duration_ms"] > 0 for s in trace["spans"]) and first["token_usage"]["total_tokens"] > 0
    assert {(a["assessment_name"], a["source"]["source_type"]) for a in trace["annotations"]} == {
        ("includes", "CODE"),
        ("model_graded_qa", "LLM_JUDGE"),
        ("tool_use/as_expected", "CODE"),
        ("tool_use/calls", "CODE"),
        ("similarity", "CODE"),
    }
    # The grader's model is the judge's source, and its transcript stays with the score.
    graded = next(a for a in trace["annotations"] if a["assessment_name"] == "model_graded_qa")
    assert graded["source"]["source_id"] == "openai-api/glm/zai.glm-5" and "GRADE: C" in graded["rationale"]
    assert [m["role"] for m in json.loads(graded["metadata"]["inspect.metadata"])["grading"]] == ["user", "assistant"]
    # A sample that failed before the model answered keeps its messages and its error.
    broken = client.get(f"/v1/evals/{run.id}/items/{items['broken']['id']}/trace").json()
    assert [(s["name"], s["status"]["code"]) for s in broken["spans"]] == [
        ("importer_fixture", "STATUS_CODE_ERROR"),
        ("messages with no model reply", "STATUS_CODE_ERROR"),
    ]
    assert "Traceback" in broken["spans"][0]["attributes"]["inspect.error"]["traceback"]
    # Its trace starts when the sample did, to the millisecond, and lasts as long, though the
    # messages' span has no start time.
    sample = next(s for s in json.loads(GLM5.read_text(encoding="utf-8"))["samples"] if s["id"] == "broken")
    started = datetime.fromisoformat(sample["started_at"])
    started = started.replace(microsecond=started.microsecond // 1000 * 1000)
    info = client.get(f"/v1/evals/traces/{broken['trace_id']}").json()["info"]
    assert datetime.fromisoformat(info["request_time"]) == started
    assert float(info["execution_duration"].removesuffix("s")) == pytest.approx(sample["total_time"], abs=0.002)

    # The run keeps the log it came from.
    source = client.get(f"/v1/evals/{run.id}/provenance").json()["admission"]["inspect"]
    assert (source["log"], source["format"], source["eval_id"], source["task"], source["model"]) == (
        str(GLM5),
        "json",
        "KygpdcMPCzTh6VwUgamdt3",
        "importer_fixture",
        "openai-api/glm/zai.glm-5",
    )
    assert len(source["log_sha256"]) == 64


def test_two_imported_runs_compare_case_by_case(tmp_path):
    home = tmp_path / "evals-home"
    glm5, mock = import_inspect_logs([GLM5, MOCK], home)
    client = local_ui(home)
    assert [r["run_code"] for r in client.get("/v1/evals").json()] == [
        "inspect-importer_fixture-2",
        "inspect-importer_fixture-1",
    ]
    compared = client.get("/v1/evals/compare", params={"baseline_id": glm5.id, "candidate_id": mock.id}).json()
    # The same four samples: the first epochs pair, the mock run's second epochs have no partner.
    assert (compared["compatible"], compared["coverage"]) == (
        True,
        {"baseline": 4, "candidate": 8, "paired": 4, "truncated": False},
    )
    assert {row["dataset_item_id"]: row["change"] for row in compared["items"]} == {
        "sum": "regressed",
        "sum (epoch 2)": "not_comparable",
        "capital": "regressed",
        "capital (epoch 2)": "not_comparable",
        "secret": "unchanged",
        "secret (epoch 2)": "not_comparable",
        "broken": "not_comparable",
        "broken (epoch 2)": "not_comparable",
    }


def test_an_imported_run_takes_a_review(tmp_path):
    home = tmp_path / "evals-home"
    run = import_inspect_log(GLM5, home)
    client = local_ui(home)
    item = items_of(client, run.id)["secret"]
    review = {
        "request_id": str(uuid.uuid4()),
        "state": "complete",
        "value": True,
        "comment": "It declined; the includes check wants the criterion text itself.",
    }
    saved = client.post(f"/v1/evals/{run.id}/items/{item['id']}/reviews", json=review)
    assert saved.status_code == 201, saved.text
    work = client.get(f"/v1/evals/{run.id}/workbench", params={"item_id": item["id"]}).json()
    assert [(r["criterion"], r["value"], r["comment"]) for r in work["latest_reviews"]] == [
        ("human_review", True, review["comment"])
    ]
    assert ("human_review", True, "HUMAN") in {(a["name"], a["value"], a["source"]) for a in work["assessments"]}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("C", [("s", "C", True)]),
        ("I", [("s", "I", False)]),
        ("P", [("s", "P", False)]),
        ("N", [("s", "N", False)]),
        (True, [("s", True, True)]),
        (0, [("s", 0, None)]),
        (0.25, [("s", 0.25, None)]),
        ("yes", [("s", "yes", None)]),
        ({"a": "C", "b": 2, "c": None}, [("s/a", "C", True), ("s/b", 2, None), ("s/c", None, None)]),
        ([1, False], [("s/0", 1, None), ("s/1", False, False)]),
    ],
)
def test_each_inspect_value_is_one_score_per_entry_kept_exactly(value, expected):
    found = inspect_scores("s", InspectScore(value=value, explanation="why"), run_id="r", case_id="c")
    assert [(s.scorer, s.value, s.passed) for s in found] == expected
    assert all(
        type(s.value) is type(e[1]) and s.rationale == "why" and not s.error
        for s, e in zip(found, expected, strict=True)
    )


def test_an_unscored_value_is_a_score_with_an_error_for_each_entry_the_scorer_gives():
    nan = InspectScore(value=math.nan, reason="grader_failed")
    (alone,) = inspect_scores("s", nan, run_id="r", case_id="c")
    assert (alone.value, alone.passed, alone.error, alone.metadata["inspect.value"]) == (
        None,
        None,
        "unscored: grader_failed",
        "NaN",
    )
    keyed = inspect_scores("s", nan, run_id="r", case_id="c", keys=["a", "b"])
    assert [(s.scorer, s.value, s.error) for s in keyed] == [("s/a", None, alone.error), ("s/b", None, alone.error)]
    with pytest.raises(InspectLogError, match="must be finite"):
        inspect_scores("s", InspectScore(value=math.inf), run_id="r", case_id="c")
    with pytest.raises(InspectLogError, match="is a dict"):
        inspect_scores("s", InspectScore(value={"a": {"b": 1}}), run_id="r", case_id="c")


def write_log(folder: Path, change) -> Path:
    log = json.loads(GLM5.read_text(encoding="utf-8"))
    change(log)
    path = folder / "changed.json"
    path.write_text(json.dumps(log), encoding="utf-8")
    return path


def test_chat_inputs_choices_and_the_single_scorer_placeholder(tmp_path):
    def change(log):
        capital = next(s for s in log["samples"] if s["id"] == "capital")
        capital["input"] = [{"id": "per-run", "role": "user", "content": "The capital of France?"}]
        capital["choices"] = ["Paris", "Lyon"]
        capital["scores"] = {"88F74D2C": {"value": "C"}}

    found = read_inspect_log(write_log(tmp_path, change))
    run = import_inspect_log(found.path, tmp_path / "evals-home")
    client = local_ui(tmp_path / "evals-home")
    item = items_of(client, run.id)["capital"]
    # Message ids change every run, so the inputs keep the messages without them.
    assert json.loads(item["prompt"]) == {
        "choices": ["Paris", "Lyon"],
        "messages": [{"content": "The capital of France?", "role": "user"}],
    }
    assert {name: a["value"] for name, a in scores_of(client, run.id)["capital"].items()} == {"includes": "C"}


def test_same_named_scores_of_different_sources_link_to_their_own_assessments(tmp_path):
    # tool_use's dict entry `calls` is the code score tool_use/calls; a scorer of that very name
    # that grades with a model gives a judge's score of the same name on the same sample.
    def change(log):
        sample = next(s for s in log["samples"] if s["id"] == "sum")
        sample["scores"]["tool_use/calls"] = {"value": 3, "explanation": "three calls"}
        begin = next(e for e in sample["events"] if e.get("event") == "span_begin" and e.get("type") == "scorer")
        model = next(e for e in sample["events"] if e.get("event") == "model")
        sample["events"] += [
            {**begin, "id": "judge-span", "name": "tool_use/calls"},
            {**model, "span_id": "judge-span", "uuid": "judge-call"},
        ]

    run = import_inspect_log(write_log(tmp_path, change), tmp_path / "evals-home")
    client = local_ui(tmp_path / "evals-home")
    case = items_of(client, run.id)["sum"]["id"]
    scores = [
        a
        for a in client.get(f"/v1/evals/{run.id}/workbench").json()["assessments"]
        if (a["item_id"], a["name"]) == (case, "tool_use/calls")
    ]
    assert sorted((a["source"], a["value"]) for a in scores) == [("CODE", 1), ("LLM_JUDGE", 3)]
    trace = client.get(f"/v1/evals/{run.id}/items/{case}/trace").json()
    stored = {
        a["assessment_id"]: (a["source"]["source_type"], a["feedback"]["value"])
        for a in trace["annotations"]
        if a["assessment_name"] == "tool_use/calls"
    }
    assert {stored[a["native_id"]] for a in scores} == {("CODE", 1), ("LLM_JUDGE", 3)}


def test_two_scores_of_one_name_and_source_on_a_sample_refuse_the_log_before_anything_is_written(tmp_path):
    # tool_use's dict entry `calls` is the code score tool_use/calls; a scorer of that very name
    # that grades without a model is a second code score of the same name on the same sample.
    def change(log):
        sample = next(s for s in log["samples"] if s["id"] == "sum")
        sample["scores"]["tool_use/calls"] = {"value": 3, "explanation": "three calls"}

    path = write_log(tmp_path, change)
    with pytest.raises(
        InspectLogError,
        match=r"sample sum has two code scores named tool_use/calls: the entry calls of the scorer tool_use and "
        r"the scorer tool_use/calls; a case holds one score of a name and source",
    ):
        import_inspect_log(path, tmp_path / "evals-home")
    assert not (tmp_path / "evals-home").exists()


def eval_archive(folder: Path, entries: dict[str, object], name="log.eval") -> Path:
    """An .eval log as older Inspect versions wrote it, with Deflate entries."""
    path = folder / name
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for entry, content in entries.items():
            archive.writestr(entry, json.dumps(content))
    return path


def test_an_eval_log_with_deflate_entries_imports_like_its_json_log(tmp_path):
    log = json.loads(GLM5.read_text(encoding="utf-8"))
    samples = log.pop("samples")
    path = eval_archive(
        tmp_path, {"header.json": log, **{f"samples/{s['id']}_epoch_{s['epoch']}.json": s for s in samples}}
    )
    run = import_inspect_log(path, tmp_path / "evals-home")
    found = scores_of(local_ui(tmp_path / "evals-home"), run.id)
    assert {case: {name: a["value"] for name, a in scores.items()} for case, scores in found.items()} == GLM5_SCORES
    assert run.provenance["inspect"]["format"] == "eval"


def damaged(folder: Path) -> Path:
    """The mock log with one byte of a sample's compressed data changed."""
    data = bytearray(MOCK.read_bytes())
    info = zipfile.ZipFile(io.BytesIO(bytes(data))).getinfo("samples/sum_epoch_1.json")
    data[info.header_offset + 30 + len(info.filename) + info.compress_size // 2] ^= 0xFF
    path = folder / "damaged.eval"
    path.write_bytes(bytes(data))
    return path


@pytest.mark.parametrize(
    ("make", "problem"),
    [
        (lambda folder: write_log(folder, lambda log: log.update(status="started")), "the eval has not finished"),
        (lambda folder: write_log(folder, lambda log: log.update(version=1)), "format version 1; this importer reads"),
        (lambda folder: write_log(folder, lambda log: log.update(samples=[])), "has no samples"),
        (lambda folder: write_log(folder, lambda log: log["eval"].pop("task")), "eval.task: Field required"),
        (lambda folder: eval_archive(folder, {"samples/a_epoch_1.json": {}}), "has no header.json"),
        (damaged, "samples/sum_epoch_1.json"),
        (lambda folder: (folder / "log.txt").write_text("{}") and folder / "log.txt", "ends in .eval or .json"),
        (lambda folder: (folder / "log.json").write_text("not json") and folder / "log.json", "it is not JSON"),
    ],
)
def test_files_that_are_not_finished_inspect_logs_are_refused(tmp_path, make, problem):
    with pytest.raises(InspectLogError, match=problem.replace("(", r"\(").replace(".", r"\.")):
        import_inspect_logs([GLM5, make(tmp_path)], tmp_path / "evals-home")
    # Every log is read before anything is written.
    assert not (tmp_path / "evals-home").exists()


def test_the_command_imports_each_log_into_the_data_folder_it_names(tmp_path):
    folder = tmp_path / "elsewhere"
    result = CliRunner().invoke(app, ["evals", "import", "inspect", str(GLM5), str(MOCK), "--data-dir", str(folder)])
    assert result.exit_code == 0, result.output
    assert (
        "glm5.json: run inspect-importer_fixture-1, 4 cases: 2 passed, 1 failed, 1 errors, 0 without a verdict\n"
        "mockllm-epochs.eval: run inspect-importer_fixture-2, 8 cases: 0 passed, 6 failed, 2 errors, "
        "0 without a verdict\n"
        f"Open them in the local Evals UI: martian ui --data-dir {folder}\n"
    ) in result.output
    assert (folder / "martian.db").exists() and not (tmp_path / "evals-home").exists()
    refused = CliRunner().invoke(app, ["evals", "import", "inspect", str(GLM5), str(tmp_path / "missing.eval")])
    assert refused.exit_code == 2 and "Import failed: cannot read" in refused.output
    assert not (tmp_path / "evals-home").exists()
