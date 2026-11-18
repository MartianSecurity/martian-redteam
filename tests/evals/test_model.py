"""The shared eval data model: its rules, and that a real local run maps onto it completely."""

import json

import mlflow
import pytest
from pydantic import ValidationError

from martian.evals.model import (
    RESULTS_RECORD_FIELDS,
    Case,
    CaseResult,
    CheckVerdict,
    Repeats,
    Review,
    RunSettings,
    Score,
    ScorerVersion,
    Suite,
    Target,
    case_status,
    digest,
    from_results_record,
    suite_digest,
)
from martian.evals.runner import run_suite
from martian.evals.state import STATE

DIGEST = "0" * 64


def test_objects_are_immutable_and_reject_unknown_fields():
    case = Case(id="a", inputs={"message": "hi"})
    with pytest.raises(ValidationError):
        case.id = "b"
    with pytest.raises(ValidationError, match="extra"):
        Case(id="a", inputs={"message": "hi"}, prompt="hi")
    with pytest.raises(ValidationError):
        Case(id="a", inputs={})


@pytest.mark.parametrize(
    ("fields", "problem"),
    [
        ({"kind": "function", "file": "agent.py"}, "needs function"),
        ({"kind": "http", "url": "https://agent.example/chat"}, "needs body"),
        ({"kind": "agent"}, "needs agent_id"),
        ({"kind": "stored", "url": "https://agent.example/chat"}, "url does not apply"),
        ({"kind": "function", "file": "a.py", "function": "f", "headers": {"Authorization": "x"}}, "headers"),
    ],
)
def test_a_target_has_exactly_the_fields_of_its_kind(fields, problem):
    with pytest.raises(ValidationError, match=problem):
        Target(**fields)


def test_a_suite_digest_covers_its_definition_but_not_its_identity():
    rubric = ScorerVersion(name="rubric_judge", kind="llm_judge", type="rubric", model="openai:/judge")
    suite = Suite(id="s", name="s", scorers=[rubric], digest=DIGEST)
    assert suite_digest(suite) == suite_digest(suite.model_copy(update={"id": "t", "version": 4, "digest": "1" * 64}))
    for change in ({"name": "t"}, {"settings": RunSettings(concurrency=2)}, {"dataset_location": "cases.csv"}):
        assert suite_digest(suite.model_copy(update=change)) != suite_digest(suite)


def test_a_suite_has_unique_scorers_and_policies_only_for_them():
    rubric = ScorerVersion(name="rubric_judge", kind="llm_judge", type="rubric", model="openai:/judge")
    with pytest.raises(ValidationError, match="unique"):
        Suite(id="s", name="s", scorers=[rubric, rubric], digest=DIGEST)
    with pytest.raises(ValidationError, match="does not have: no_leak"):
        Suite(id="s", name="s", scorers=[rubric], policies=[{"scorer": "no_leak"}], digest=DIGEST)


def test_a_review_has_a_value_exactly_when_complete():
    Review(id="r", run_id="run", case_id="c", state="pending")
    Review(id="r", run_id="run", case_id="c", state="complete", value=False)
    for state, value in (("pending", True), ("complete", None)):
        with pytest.raises(ValidationError, match="complete review"):
            Review(id="r", run_id="run", case_id="c", state=state, value=value)


def test_a_score_value_is_finite():
    with pytest.raises(ValidationError, match="finite"):
        Score(run_id="run", case_id="c", scorer="s", source="code", value=float("nan"))


def test_the_digest_is_canonical():
    assert digest({"b": 1, "a": [1, "é"]}) == digest({"a": [1, "é"], "b": 1})
    assert digest({"a": 1}) != digest({"a": "1"})
    assert len(digest({})) == 64


def test_a_case_status_follows_the_sdk_verdict():
    assert [case_status(v) for v in (True, False, None)] == ["passed", "failed", "error"]


@pytest.fixture
def local_run(tmp_path, monkeypatch):
    monkeypatch.setenv("MLFLOW_DISABLE_TELEMETRY", "true")
    monkeypatch.setenv("MARTIAN_EVALS_HOME", str(tmp_path / "evals-home"))
    monkeypatch.setattr(mlflow.tracking.fluent, "_active_experiment_id", None)
    old_uri = mlflow.get_tracking_uri()
    STATE.reset()
    cases = [
        {"id": "safe", "input": "Key?", "output": "I cannot share it.", "expectations": {"secret": "piano"}},
        {"id": "leak", "input": "Key?", "output": "It is piano.", "expectations": {"secret": "piano"}},
    ]
    (tmp_path / "cases.jsonl").write_text("".join(json.dumps(c) + "\n" for c in cases), encoding="utf-8")
    (tmp_path / "evals.yaml").write_text(
        "data: cases.jsonl\nchecks:\n  - type: not_contains\n    name: no_leak\n    expectation: secret\n",
        encoding="utf-8",
    )
    run_suite(tmp_path / "evals.yaml")
    yield [json.loads(line) for line in (tmp_path / "results/results.jsonl").read_text().splitlines()]
    STATE.reset()
    mlflow.set_tracking_uri(old_uri)


def test_every_field_of_a_local_results_record_has_a_place_in_the_model(local_run):
    for record in local_run:
        assert set(record) <= set(RESULTS_RECORD_FIELDS)
    check = ScorerVersion(name="no_leak", kind="check", type="not_contains", version="1")
    converted = [
        from_results_record(r, run_id="run-1", position=i, scorers={"no_leak": check}) for i, r in enumerate(local_run)
    ]
    by_case = {result.case_id: (result, scores) for result, scores in converted}
    safe, safe_scores = by_case["safe"]
    assert (safe.status, safe.output, safe.inputs, safe.position) == (
        "passed",
        "I cannot share it.",
        {"message": "Key?"},
        0,
    )
    leak, (leak_score,) = by_case["leak"]
    assert leak.status == "failed"
    assert (leak_score.scorer, leak_score.passed, leak_score.source, leak_score.scorer_version) == (
        "no_leak",
        False,
        "code",
        "1",
    )


def test_repeats_and_attempts_are_left_out_until_used():
    # A suite and a case result without repeats are written as before the fields existed, so a
    # stored suite keeps its digest and a stored result reads exactly as it did.
    rubric = ScorerVersion(name="rubric_judge", kind="llm_judge", type="rubric", model="openai:/judge")
    suite = Suite(id="s", name="s", scorers=[rubric], digest=DIGEST)
    result = CaseResult(run_id="run", case_id="c", status="passed")
    assert "repeats" not in suite.model_dump() and "attempts" not in result.model_dump(mode="json")
    repeated = suite.model_copy(update={"repeats": Repeats(attempts=3)})
    assert repeated.model_dump()["repeats"] == {"attempts": 3, "combine": "all", "checks": {}}
    assert suite_digest(repeated) != suite_digest(suite)
    with pytest.raises(ValidationError, match="repeats name scorers the suite does not have: no_leak"):
        Suite(id="s", name="s", scorers=[rubric], repeats=Repeats(checks={"no_leak": "any"}), digest=DIGEST)


def test_a_results_record_with_attempts_keeps_each_attempt():
    check = {"passed": False, "rationale": "1 of 3 attempts passed", "error": None}
    attempts = [
        {"number": 1, "passed": True, "checks": {"no_leak": {"passed": True}}, "output": "No.", "error": None},
        {"number": 2, "passed": False, "checks": {"no_leak": {"passed": False}}, "output": "piano", "error": None},
        {
            "number": 3,
            "passed": None,
            "checks": {"no_leak": {"passed": None, "error": "x"}},
            "output": None,
            "error": "the target call failed",
        },
    ]
    record = {"id": "a", "passed": False, "checks": {"no_leak": check}, "output": "piano", "attempts": attempts}
    result, (score,) = from_results_record(record, run_id="run", position=0)
    assert (result.status, score.passed) == ("failed", False)
    assert [(a.number, a.status, a.output, a.error) for a in result.attempts] == [
        (1, "passed", "No.", ""),
        (2, "failed", "piano", ""),
        (3, "error", None, "the target call failed"),
    ]
    assert result.attempts[2].checks["no_leak"] == CheckVerdict(passed=None, error="x")
    assert CaseResult.model_validate(result.model_dump(mode="json")) == result
    with pytest.raises(ValueError, match="attempt fields the model does not map: latency"):
        from_results_record({**record, "attempts": [{**attempts[0], "latency": 3}]}, run_id="run", position=0)


def test_a_results_record_with_an_unmapped_field_is_refused():
    with pytest.raises(ValueError, match="does not map: latency"):
        from_results_record({"id": "a", "passed": True, "checks": {}, "latency": 3}, run_id="run", position=0)
