"""A suite-file run compared with a baseline: the verdicts file, the comparison and the gate."""

import json
import random

import mlflow
import pytest
from typer.testing import CliRunner

from martian.cli import app
from martian.evals import baseline
from martian.evals.runner import run_suite
from martian.evals.state import STATE


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    for name in ("MARTIAN_API_URL", "MARTIAN_API_TOKEN", "MARTIAN_AGENT_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MLFLOW_DISABLE_TELEMETRY", "true")
    monkeypatch.setenv("MARTIAN_EVALS_HOME", str(tmp_path / "evals-home"))
    monkeypatch.setattr(mlflow.tracking.fluent, "_active_experiment_id", None)
    old_uri = mlflow.get_tracking_uri()
    STATE.reset()
    yield
    STATE.reset()
    mlflow.set_tracking_uri(old_uri)


SUITE = """
data: cases.jsonl
checks:
  - type: contains
    name: answers
    expectation: answer
"""


def stored_suite(folder, *, broken=(), fixed=(), extra=()):
    """Forty stored replies to forty questions, a contains check of each one's answer, and every
    fifth reply wrong; ``broken`` replies are wrong too and ``fixed`` ones right. ``extra`` adds
    cases after them."""
    rows = []
    for n in range(40):
        wrong = n in broken or (n % 5 == 0 and n not in fixed)
        reply = "I am not sure." if wrong else f"Answer {n}."
        rows.append(
            {"id": f"q{n}", "input": f"Question {n}", "output": reply, "expectations": {"answer": f"Answer {n}."}}
        )
    rows += list(extra)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "cases.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    (folder / "evals.yaml").write_text(SUITE, encoding="utf-8")
    return folder / "evals.yaml"


def run(suite, output, *extra):
    STATE.reset()
    return CliRunner().invoke(app, ["evals", "run", str(suite), "--output", str(output), *extra])


def test_every_run_writes_verdicts_without_inputs_replies_or_quoting_reasons(tmp_path):
    (tmp_path / "cases.jsonl").write_text(
        json.dumps({"id": "leak", "input": "What is the key?", "output": "It is Piano, total 41.", "expectations": {}})
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "evals.yaml").write_text(
        "data: cases.jsonl\nchecks:\n  - type: not_contains\n    values: [Piano]\n"
        "  - type: number_equals\n    value: 42\n",
        encoding="utf-8",
    )
    run_suite(tmp_path / "evals.yaml", output=tmp_path / "out")
    text = (tmp_path / "out" / baseline.VERDICTS).read_text(encoding="utf-8")
    found = json.loads(text)
    assert (found["format"], found["attempts"], list(found["checks"])) == (
        baseline.FORMAT,
        1,
        ["not_contains", "number_equals"],
    )
    [case] = found["cases"]
    assert (case["id"], case["passed"], case["error"]) == ("leak", False, None)
    assert case["digest"] == baseline.case_digest({"message": "What is the key?"}, {})
    # A deterministic check's own wording is kept; a numeric check's, which quotes the reply's
    # figure, is not. Neither the input nor the reply is in the file.
    assert case["checks"] == {
        "not_contains": {"passed": False, "reason": "1 of 1 forbidden values found"},
        "number_equals": {"passed": False, "reason": None},
    }
    for private in ("Piano", "total 41", "What is the key?"):
        assert private not in text
    # A run without a baseline leaves no comparison behind.
    (tmp_path / "out" / baseline.COMPARISON).write_text("{}", encoding="utf-8")
    run_suite(tmp_path / "evals.yaml", output=tmp_path / "out")
    assert not (tmp_path / "out" / baseline.COMPARISON).exists()


def test_a_regression_beyond_noise_fails_the_gate_and_names_each_case(tmp_path):
    main = run(stored_suite(tmp_path / "suite"), tmp_path / "main")
    assert main.exit_code == 1  # eight known failures: on its own, the run fails
    # The pull request breaks twelve answers that passed, chosen by a fixed seed, and fixes two.
    rng = random.Random(203)
    broken = sorted(rng.sample([n for n in range(40) if n % 5], 12))
    fixed = rng.sample([n for n in range(40) if n % 5 == 0], 2)
    suite = stored_suite(tmp_path / "suite", broken=broken, fixed=fixed)
    result = run(suite, tmp_path / "pr", "--baseline", str(tmp_path / "main"))
    assert result.exit_code == 1, result.output
    found = json.loads((tmp_path / "pr" / baseline.COMPARISON).read_text(encoding="utf-8"))
    assert (found["label"], found["gate"]) == ("worse", "failed")
    significance = found["significance"]
    assert (significance["compared"], significance["improved"], significance["regressed"]) == (40, 2, 12)
    assert significance["p_value"] == 212 / 16384  # 2 × (1 + 14 + 91) / 2¹⁴
    assert [case["id"] for case in found["regressed"]] == [f"q{n}" for n in broken]
    assert found["regressed"][0]["checks"] == {"answers": "1 of 1 values not found"}
    assert sorted(found["improved"]) == sorted(f"q{n}" for n in fixed)
    assert "worse, beyond noise. 40 cases compared: 12 from pass to fail, 2 from fail to pass" in result.output
    assert f"From pass to fail: q{broken[0]} (answers), " in result.output


def test_known_failures_and_a_few_broken_cases_are_within_noise_and_pass_the_gate(tmp_path):
    run(stored_suite(tmp_path / "suite"), tmp_path / "main")
    same = run(tmp_path / "suite" / "evals.yaml", tmp_path / "same", "--baseline", str(tmp_path / "main"))
    assert same.exit_code == 0, same.output  # the same eight failures as main
    found = json.loads((tmp_path / "same" / baseline.COMPARISON).read_text(encoding="utf-8"))
    assert (found["label"], found["significance"]["changed"], found["regressed"]) == ("within_noise", 0, [])
    # Two broken answers are named, and are too few to tell from chance.
    suite = stored_suite(tmp_path / "suite", broken=(1, 2))
    result = run(suite, tmp_path / "pr", "--baseline", str(tmp_path / "main"))
    assert result.exit_code == 0, result.output
    found = json.loads((tmp_path / "pr" / baseline.COMPARISON).read_text(encoding="utf-8"))
    assert (found["label"], found["gate"]) == ("within_noise", "passed")
    assert [case["id"] for case in found["regressed"]] == ["q1", "q2"]
    assert "From pass to fail: q1 (answers), q2 (answers)" in result.output


def test_a_failed_case_with_no_result_in_the_baseline_fails_the_gate(tmp_path):
    run(stored_suite(tmp_path / "suite"), tmp_path / "main")
    new = {"id": "new", "input": "Question new", "output": "No idea.", "expectations": {"answer": "Answer new."}}
    # A case whose expectations changed is not paired with main's result: q1 now expects another answer.
    suite = stored_suite(tmp_path / "suite", extra=[new])
    rows = [json.loads(line) for line in (tmp_path / "suite/cases.jsonl").read_text(encoding="utf-8").splitlines()]
    rows[1]["expectations"] = {"answer": "Answer one."}
    (tmp_path / "suite/cases.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    result = run(suite, tmp_path / "pr", "--baseline", str(tmp_path / "main"))
    assert result.exit_code == 1, result.output
    found = json.loads((tmp_path / "pr" / baseline.COMPARISON).read_text(encoding="utf-8"))
    assert (found["label"], found["gate"]) == ("within_noise", "failed")
    assert (found["new"], found["changed"], found["unpaired_failures"]) == (["new"], ["q1"], ["q1", "new"])
    assert found["significance"]["compared"] == 39
    assert "Failed with no result in the baseline to compare with, which fails the gate: q1, new" in result.output


def test_a_baseline_that_cannot_be_read_stops_the_run_before_any_case(tmp_path):
    suite = stored_suite(tmp_path / "suite")
    missing = run(suite, tmp_path / "pr", "--baseline", str(tmp_path / "nowhere"))
    assert missing.exit_code == 2
    assert "the baseline folder has no verdicts.json" in missing.output
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / baseline.VERDICTS).write_text('{"format": "something else"}', encoding="utf-8")
    other = run(suite, tmp_path / "pr", "--baseline", str(tmp_path / "other"))
    assert other.exit_code == 2 and "is not a martian-evals-verdicts/1 file" in other.output
    assert not (tmp_path / "pr").exists()


def test_the_baseline_applies_to_a_suite_file_only(tmp_path):
    result = CliRunner().invoke(app, ["evals", "run", str(tmp_path), "--baseline", str(tmp_path)])
    assert result.exit_code == 2 and "--baseline and --data-dir apply to a suite file" in result.output


def test_the_comparison_names_what_changed_in_the_suite():
    case = {"id": "a", "digest": "d", "passed": True, "error": None, "checks": {"x": {"passed": True, "reason": None}}}
    before = {
        "format": baseline.FORMAT,
        "attempts": 1,
        "checks": {"x": "1", "y": "2"},
        "cases": [case, {**case, "id": "b"}],
    }
    after = {"format": baseline.FORMAT, "attempts": 5, "checks": {"x": "changed", "z": "3"}, "cases": [case]}
    found = baseline.compare(before, after)
    assert found["checks"] == {"added": ["z"], "removed": ["y"], "changed": ["x"]}
    assert (found["attempts"], found["missing"]) == ({"baseline": 1, "candidate": 5}, ["b"])
    assert (found["label"], found["gate"]) == ("within_noise", "passed")
    # Without a pair there is no label, and the gate rests on the unpaired cases alone.
    lone = baseline.compare({**before, "cases": []}, after)
    assert (lone["significance"], lone["label"], lone["new"], lone["gate"]) == (None, None, ["a"], "passed")
