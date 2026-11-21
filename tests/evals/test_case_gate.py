import json
import shutil
from pathlib import Path

import yaml

from martian.evals.case_gate import AssertionSet, CaseGate
from martian.evals.named_metrics import recorded_metrics, summarize
from martian.evals.runner import run_suite


def write_suite(folder, cases, checks, case_gate, **settings):
    (folder / "cases.jsonl").write_text("".join(json.dumps(case) + "\n" for case in cases), encoding="utf-8")
    path = folder / "evals.yaml"
    path.write_text(
        yaml.safe_dump({"data": "cases.jsonl", "checks": checks, "case_gate": case_gate, **settings}),
        encoding="utf-8",
    )
    return path


def test_weighted_gate_keeps_security_and_incomplete_results_fail_closed(tmp_path):
    path = write_suite(
        tmp_path,
        [
            {"id": "quality-allowed", "input": "question", "outputs": "good", "expectations": {"wanted": "extra"}},
            {
                "id": "security-required",
                "input": "question",
                "outputs": "good forbidden",
                "expectations": {"wanted": "good"},
            },
            {"id": "missing-evidence", "input": "question", "outputs": "good"},
        ],
        [
            {"type": "contains", "expectation": "wanted"},
            {"type": "no_prohibited_phrases", "values": ["forbidden"]},
        ],
        {"threshold": 0, "weights": {"no_prohibited_phrases": 0}, "metrics": {"contains": "Quality"}},
        repeats=2,
        combine="any",
    )
    result = run_suite(path, home=tmp_path / "local")
    assert (result["passed"], result["failed"], result["errors"]) == (1, 1, 1)
    saved = [json.loads(line) for line in (Path(result["output"]) / "results.jsonl").read_text().splitlines()]
    checks = {row["id"]: row["checks"]["case_gate"] for row in saved}
    assert checks["quality-allowed"]["value"] == 0.0
    assert "Required checks failed: no_prohibited_phrases" in checks["security-required"]["rationale"]
    assert checks["missing-evidence"]["passed"] is None
    assert all(len(row["attempts"]) == 2 for row in saved)
    assert saved[0]["attempts"][0]["named_metrics"] == {"Quality": {"score": 0.0, "weight": 1.0}}
    assert result["named_metrics"] == [{"name": "Quality", "cases": 2, "score": 0.5, "weight": 2.0, "score_total": 1.0}]


def test_named_metrics_use_measurement_weights_and_set_scope():
    gate = CaseGate(
        threshold=0.5,
        weights={"main": 3, "observation": 0, "detail": 2},
        metrics={"main": "accuracy", "observation": "accuracy", "detail": "detail", "format": "presentation"},
        assertion_sets=[AssertionSet(name="group", checks=["detail", "format"], weight=2, metric="presentation")],
    )
    record = {
        "checks": {
            label: {"passed": passed}
            for label, passed in (("main", True), ("observation", False), ("detail", False), ("format", True))
        }
    }
    gate.apply(record, list(record["checks"]), [])
    assert record["passed"] is True
    assert record["named_metrics"] == {
        "accuracy": {"score": 0.75, "weight": 4.0},
        "detail": {"score": 0.0, "weight": 4.0},
        "presentation": {"score": 1 / 3, "weight": 2.0},
    }
    legacy = recorded_metrics({"accuracy": 0.8}, {})
    archived = recorded_metrics({"accuracy": 0.75}, {"accuracy": 4})
    assert summarize([legacy, archived]) == [
        {"name": "accuracy", "cases": 2, "score": None, "weight": None, "score_total": None}
    ]


def test_a_changed_weighted_policy_invalidates_resumed_and_paired_verdicts(tmp_path):
    cases = [{"id": "example", "input": "question", "outputs": "one"}]
    checks = [{"type": "equals", "values": ["one"]}, {"type": "contains", "values": ["two"]}]
    path = write_suite(tmp_path, cases, checks, {"threshold": 0.5})
    previous = run_suite(path, home=tmp_path / "local")
    earlier = tmp_path / "baseline"
    earlier.mkdir()
    shutil.copy(Path(previous["output"]) / "verdicts.json", earlier)
    write_suite(tmp_path, cases, checks, {"threshold": 0.75})
    updated = run_suite(path, home=tmp_path / "local", baseline=earlier)
    assert previous["passed"] == 1 and updated["failed"] == 1
    assert updated["invalidated"] == 1
    assert updated["comparison"]["changed"] == ["example"]
    assert updated["comparison"]["unpaired_failures"] == ["example"]
    assert updated["comparison"]["gate"] == "failed"
