"""A panel of judges: several judge models grade one check, a member that errors does not vote, and
the strict majority of the votes decides, from more than half of the members; no majority is an
error, never a pass. The panel works the same in Python, in a suite file and for a registered judge
the core's worker grades, records every member's vote on the score, and is calibrated and reported
as one judge.

A loopback OpenAI-compatible endpoint answers for the SDK judges, and a fake judge for the core's
worker. Nothing calls a real model.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import mlflow
import pytest
import yaml

from martian.config import ConfigError
from martian.evals import judge_panel, response_rubric
from martian.evals.agreement import MINIMUM_LABELLED
from martian.evals.calibration import CALIBRATION_KEY, NO_CALIBRATION, calibration_state
from martian.evals.gate import STATE
from martian.evals.judges import RESPONSE_RUBRICS_VERSION
from martian.evals.local import open_stores
from martian.evals.model import CaseResult, PanelMember, Review, Run, Score, ScorerVersion
from martian.evals.panel import NO_MAJORITY, RULE, Vote, decide, panel_source
from martian.evals.records.sql import SqlRecordStore
from martian.evals.runner import run_suite
from martian.evals.server import DEFAULT_PROJECT, SINGLE_USER
from martian.evals.store import MemoryStore
from martian.evals.suite_file import load_suite_file
from martian.evals.worker import JudgeError, JudgeRequest, JudgeResult, Verdict
from tests.evals.test_calibration import calibration, on
from tests.evals.test_worker_judges import KEY, REPORTED, Loopback, rubrics_of
from tests.evals.write_fixture import Served, request_id, serve

T0 = datetime(2026, 11, 15, 9, 0, tzinfo=UTC)


# --- the rule ------------------------------------------------------------------------------------


def votes(*verdicts: bool | None) -> list[Vote]:
    return [Vote(f"openai:/judge-{i}", verdict, f"reason {i}") for i, verdict in enumerate(verdicts, 1)]


def test_a_panel_where_one_judge_errors_still_decides():
    assert decide(votes(True, True, None)).passed is True
    assert decide(votes(False, None, False)).passed is False
    # Two of three votes, from three of five members: more than half voted and agreed.
    assert decide(votes(True, True, False, None, None)).passed is True
    assert decide(votes(True, False, True)).passed is True


@pytest.mark.parametrize(
    ("verdicts", "said"),
    [
        ((True, False, None), "1 passed and 1 failed, a tie"),
        ((True, True, False, False), "2 passed and 2 failed, a tie"),
        ((True, None, None), "1 of 3 judges gave a verdict, and more than half must"),
        ((True, None), "1 of 2 judges gave a verdict"),
        ((True, True, None, None), "2 of 4 judges gave a verdict"),
        ((None, None, None), "0 of 3 judges gave a verdict"),
    ],
    ids=["tie", "even-tie", "one-of-three-voted", "one-of-two-voted", "half-voted", "none-voted"],
)
def test_no_majority_is_an_error_never_a_pass(verdicts, said):
    decision = decide(votes(*verdicts))
    assert decision.passed is None
    assert decision.error.startswith(f"Judge panel has no majority [{NO_MAJORITY}]") and said in decision.error


def test_every_vote_is_recorded_as_flat_strings():
    decision = decide([Vote("openai:/a", True, "Polite.", model_version="a-2026"), Vote("openai:/b", None, "Failed.")]
                      + [Vote("openai:/c", True, "Kind.", endpoint="http://127.0.0.1:4000/v1")])  # fmt: skip
    assert decision.metadata() == {
        "martian.panel.rule": RULE,
        "martian.panel.size": "3",
        "martian.panel.passed": "2",
        "martian.panel.failed": "0",
        "martian.panel.errors": "1",
        "martian.panel.verdict": "pass",
        "martian.panel.1.model": "openai:/a",
        "martian.panel.1.model_version": "a-2026",
        "martian.panel.1.verdict": "pass",
        "martian.panel.1.reason": "Polite.",
        "martian.panel.2.model": "openai:/b",
        "martian.panel.2.verdict": "error",
        "martian.panel.2.reason": "Failed.",
        "martian.panel.3.model": "openai:/c",
        "martian.panel.3.endpoint": "http://127.0.0.1:4000/v1",
        "martian.panel.3.verdict": "pass",
        "martian.panel.3.reason": "Kind.",
    }
    assert "3. openai:/c passed: Kind." in decision.rationale
    # The same members in another order are the same panel.
    assert panel_source([("openai:/a", None), ("openai:/b", None)]) == panel_source(
        [("openai:/b", None), ("openai:/a", None)]
    )
    assert panel_source([("openai:/a", None), ("openai:/b", None)]) != panel_source(
        [("openai:/a", None), ("openai:/c", None)]
    )


def test_a_panel_names_two_to_nine_different_models():
    with pytest.raises(ValueError, match="2 to 9"):
        ScorerVersion(name="tone", kind="llm_judge", type="instructions", panel=[PanelMember(model="openai:/a")])
    with pytest.raises(ValueError, match="once"):
        ScorerVersion(name="tone", kind="llm_judge", type="instructions", panel=[PanelMember(model="openai:/a")] * 2)
    with pytest.raises(ValueError, match="not in model"):
        ScorerVersion(name="tone", kind="llm_judge", type="instructions", model="openai:/a",
                      panel=[PanelMember(model="openai:/b"), PanelMember(model="openai:/c")])  # fmt: skip
    # A scorer without a panel is written as it was before panels existed.
    assert "panel" not in ScorerVersion(name="tone", kind="llm_judge", type="instructions").model_dump()
    with pytest.raises(ValueError, match="2 to 9"):
        judge_panel(response_rubric, models=["openai:/a"])
    with pytest.raises(ValueError, match="once"):
        judge_panel(response_rubric, models=["openai:/a", "openai:/a"])


# --- the SDK judges, in Python and in a suite file -----------------------------------------------

ANSWERS = {"pass-a": "pass", "pass-b": "pass", "fail-a": "fail"}
"""What each loopback model answers to every rubric. Any other model's reply is not JSON."""


def provider_answer(path, headers, body):
    if path != "/v1/chat/completions" or headers.get("authorization") != f"Bearer {KEY}":
        return 404, {"detail": "Not Found"}
    verdict = ANSWERS.get(body["model"])
    results = [
        {"rubric_index": i, "reason": f"{body['model']} checked rubric {i}.", "verdict": verdict, "evidence_ids": []}
        for i, _ in enumerate(rubrics_of(body), 1)
    ]
    content = json.dumps({"result": results}) if verdict else "This is not a judgment."
    message = {"role": "assistant", "content": content}
    return 200, {
        "id": "chatcmpl-fixture",
        "object": "chat.completion",
        "created": 0,
        "model": f"{body['model']}-{REPORTED}",
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


@pytest.fixture
def provider(monkeypatch, tmp_path):
    for name in ("MARTIAN_API_URL", "MARTIAN_API_TOKEN", "MARTIAN_AGENT_ID", "OPENAI_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MLFLOW_DISABLE_TELEMETRY", "true")
    monkeypatch.setenv("MARTIAN_EVALS_HOME", str(tmp_path / "evals-home"))
    monkeypatch.setattr(mlflow.tracking.fluent, "_active_experiment_id", None)
    old_uri = mlflow.get_tracking_uri()
    STATE.reset()
    server = Loopback(provider_answer)
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    monkeypatch.setenv("OPENAI_API_BASE", f"{server.origin}/v1")
    yield server
    server.close()
    STATE.reset()
    mlflow.set_tracking_uri(old_uri)


def panel_of(*models: str):
    return judge_panel(
        response_rubric, models=[f"openai:/{m}" for m in models], max_retries=0, rate_limit_wait_seconds=0
    )


def judged(panel):
    return panel.run(inputs={"message": "Hi"}, outputs="Hello there.", expectations={"rubric": "Pass if polite."})


def test_a_python_panel_where_one_judge_errors_still_decides(provider):
    feedback = judged(panel_of("pass-a", "pass-b", "broken"))
    assert (feedback.name, feedback.value, feedback.error) == ("response_rubric", True, None)
    found = feedback.metadata
    assert all(isinstance(value, str) for value in found.values())
    assert (found["martian.panel.passed"], found["martian.panel.errors"], found["martian.panel.verdict"]) == (
        "2",
        "1",
        "pass",
    )
    assert [found[f"martian.panel.{n}.model"] for n in (1, 2, 3)] == [
        "openai:/pass-a",
        "openai:/pass-b",
        "openai:/broken",
    ]
    assert [found[f"martian.panel.{n}.verdict"] for n in (1, 2, 3)] == ["pass", "pass", "error"]
    assert "pass-a checked rubric 1." in found["martian.panel.1.reason"]
    assert found["martian.panel.1.model_version"] == f"pass-a-{REPORTED}"
    assert "[judge_invalid_schema]" in found["martian.panel.3.reason"]
    assert found["martian.judge.version"] == RESPONSE_RUBRICS_VERSION
    assert feedback.source.source_id == panel_source([("openai:/pass-a", None), ("openai:/pass-b", None),
                                                      ("openai:/broken", None)])  # fmt: skip
    # Each member graded the case on its own model, once.
    assert sorted(r["body"]["model"] for r in provider.requests) == ["broken", "pass-a", "pass-b"]


def test_a_python_panel_with_no_majority_is_an_error(provider):
    tie = judged(panel_of("pass-a", "fail-a", "broken"))
    assert tie.value is None and tie.error.error_code == NO_MAJORITY
    assert "1 passed and 1 failed, a tie" in tie.error.error_message
    assert [tie.metadata[f"martian.panel.{n}.verdict"] for n in (1, 2, 3)] == ["pass", "fail", "error"]
    assert tie.metadata["martian.panel.verdict"] == "none"
    too_few = judged(panel_of("pass-a", "broken", "also-broken"))
    assert too_few.value is None and "1 of 3 judges gave a verdict" in too_few.error.error_message


def write_suite(folder: Path, panel: list[str], *, check_type: str = "rubric", **judge: Any) -> Path:
    cases = [{"id": f"case-{i}", "input": "Hi", "output": "Hello there."} for i in (1, 2)]
    (folder / "cases.jsonl").write_text("".join(json.dumps(case) + "\n" for case in cases), encoding="utf-8")
    check: dict[str, Any] = {"type": check_type, "panel": [f"openai:/{m}" for m in panel]}
    if check_type == "rubric":
        check["rubric"] = "Pass if polite."
    suite = {
        "data": "cases.jsonl",
        "judge": {"max_retries": 0, "rate_limit_wait_seconds": 0, **judge},
        "checks": [check],
    }
    (folder / "evals.yaml").write_text(yaml.safe_dump(suite, sort_keys=False), encoding="utf-8")
    return folder / "evals.yaml"


def local_scores(tmp_path: Path, run_id: str) -> list[Score]:
    _, records = open_stores(tmp_path / "evals-home")
    try:
        return asyncio.run(records.list_scores(SINGLE_USER, DEFAULT_PROJECT, run_id))
    finally:
        records.close()


def test_a_suite_file_panel_where_one_judge_errors_still_decides(provider, tmp_path):
    summary = run_suite(write_suite(tmp_path, ["pass-a", "pass-b", "broken"]))
    assert (summary["passed"], summary["failed"], summary["errors"]) == (2, 0, 0)
    records = [json.loads(line) for line in (tmp_path / "results" / "results.jsonl").read_text().splitlines()]
    check = records[0]["checks"]["rubric_judge"]
    assert check["passed"] is True and "3. openai:/broken gave no verdict" in check["rationale"]
    assert check["metadata"]["martian.panel.3.verdict"] == "error"
    assert check["metadata"]["martian.panel.1.model"] == "openai:/pass-a"
    # The local edition's score keeps every vote, names the panel, and no calibration.
    (score, _) = local_scores(tmp_path, summary["run"]["id"])
    assert score.scorer == "rubric_judge" and score.passed is True
    assert score.source_id == panel_source((f"openai:/{m}", None) for m in ("pass-a", "pass-b", "broken"))
    assert score.metadata[CALIBRATION_KEY] == NO_CALIBRATION
    assert score.metadata["martian.panel.2.verdict"] == "pass"


def test_a_suite_file_panel_with_no_majority_is_an_error_never_a_pass(provider, tmp_path):
    summary = run_suite(write_suite(tmp_path, ["pass-a", "fail-a", "broken"]))
    assert (summary["passed"], summary["failed"], summary["errors"]) == (0, 0, 2)
    (reason,) = summary["check_errors"]["rubric_judge"]
    assert reason.startswith(f"Judge panel has no majority [{NO_MAJORITY}]: 1 passed and 1 failed")
    scores = local_scores(tmp_path, summary["run"]["id"])
    assert {(s.passed, s.metadata["martian.panel.verdict"]) for s in scores} == {(None, "none")}


def test_a_suite_file_panel_is_checked_when_the_suite_loads(tmp_path):
    with pytest.raises(ConfigError, match="nothing uses it"):
        load_suite_file(write_suite(tmp_path, ["pass-a", "pass-b"], model="openai:/unused"))
    with pytest.raises(ConfigError, match="2 to 9"):
        load_suite_file(write_suite(tmp_path, ["pass-a"]))
    with pytest.raises(ConfigError, match="no LLM judge"):
        load_suite_file(write_suite(tmp_path, ["pass-a", "pass-b"], check_type="is_json"))
    suite, _ = load_suite_file(write_suite(tmp_path, ["pass-a", "pass-b"]))
    (rubric,) = suite.scorers
    assert (rubric.model, rubric.panel) == (
        None,
        [PanelMember(model="openai:/pass-a"), PanelMember(model="openai:/pass-b")],
    )


def test_a_suite_file_panel_and_a_python_check_grade_side_by_side(provider, tmp_path):
    path = write_suite(tmp_path, ["pass-a", "pass-b", "broken"])
    suite = yaml.safe_load(path.read_text(encoding="utf-8"))
    (tmp_path / "scorers.py").write_text("def greets(outputs):\n    return 'Hello' in outputs, 'greets'\n")
    suite["checks"].append({"type": "python", "name": "greets", "file": "scorers.py", "function": "greets"})
    path.write_text(yaml.safe_dump(suite, sort_keys=False), encoding="utf-8")
    summary = run_suite(path)
    assert (summary["passed"], summary["failed"], summary["errors"]) == (2, 0, 0)
    record = json.loads((tmp_path / "results" / "results.jsonl").read_text().splitlines()[0])
    assert record["checks"]["rubric_judge"]["metadata"]["martian.panel.1.model"] == "openai:/pass-a"
    assert (record["checks"]["greets"]["passed"], record["checks"]["greets"]["rationale"]) == (True, "greets")
    # One judge call per member and case: a python check is the team's code, never a judge's call.
    assert len(provider.requests) == 6
    # A panel names judge models, so a python check takes none.
    suite["checks"][1]["panel"] = ["openai:/pass-a", "openai:/pass-b"]
    path.write_text(yaml.safe_dump(suite, sort_keys=False), encoding="utf-8")
    with pytest.raises(ConfigError, match="panel"):
        load_suite_file(path)


def test_every_combination_of_a_matrix_is_graded_by_the_panel_on_its_own_replies(provider, tmp_path):
    (tmp_path / "agent.py").write_text("def answer(message, greeting):\n    return f'{greeting} How can I help?'\n")
    cases = [{"id": f"case-{i}", "input": f"Question {i}"} for i in (1, 2)]
    (tmp_path / "cases.jsonl").write_text("".join(json.dumps(case) + "\n" for case in cases), encoding="utf-8")
    suite = {
        "data": "cases.jsonl",
        "target": {"file": "agent.py", "function": "answer"},
        "matrix": {"greeting": {"plain": "Hi.", "warm": "Hello, and welcome!"}},
        "judge": {"max_retries": 0, "rate_limit_wait_seconds": 0},
        "checks": [
            {
                "type": "rubric",
                "rubric": "Pass if polite.",
                "panel": ["openai:/pass-a", "openai:/pass-b", "openai:/fail-a"],
            }
        ],
    }
    (tmp_path / "evals.yaml").write_text(yaml.safe_dump(suite, sort_keys=False), encoding="utf-8")
    summary = run_suite(tmp_path / "evals.yaml")
    # Each member graded each case of each combination once, the combination's own reply.
    assert len(provider.requests) == 2 * 2 * 3
    graded = [json.dumps(sent["body"]["messages"]) for sent in provider.requests]
    assert (
        sum("Hello, and welcome!" in text for text in graded)
        == sum("Hi. How can I help?" in text for text in graded)
        == 6
    )
    members = [f"openai:/{m}" for m in ("pass-a", "pass-b", "fail-a")]
    for entry in summary["combinations"]:
        assert (entry["summary"]["passed"], entry["gate"]) == (2, "passed")
        lines = (tmp_path / "results" / entry["folder"] / "results.jsonl").read_text().splitlines()
        for record in map(json.loads, lines):
            votes = record["checks"]["rubric_judge"]["metadata"]
            assert (votes["martian.panel.passed"], votes["martian.panel.failed"]) == ("2", "1")
        # Each combination's recorded run names the panel as its scores' source.
        scores = local_scores(tmp_path, entry["run"]["id"])
        assert {score.source_id for score in scores} == {panel_source((model, None) for model in members)}


# --- a registered judge the core's worker grades -------------------------------------------------

MEMBERS = {"tone": ["a", "b", "c"], "strict": ["a", "b", "d"]}
"""Two panel judges: ``tone``'s members a and c pass and b errors; ``strict``'s a passes, d fails."""


async def fake_judge(request: JudgeRequest) -> JudgeResult:
    """Answers from the model the request names: a and c pass every scorer, d fails it, b fails
    with a message it can show."""
    model = (request.model or "").removeprefix("openai:/")
    if model == "b":
        raise JudgeError("Judge draft failed [judge_provider_auth]. Check the judge's endpoint.")
    passed = model != "d"
    verdicts = {s.name: Verdict(passed, f"{model} says {'pass' if passed else 'fail'}.") for s in request.scorers}
    return JudgeResult(
        model=request.model or "", verdicts=verdicts, model_version=f"{model}-1", judge_version="rubric-v1"
    )


@pytest.fixture
def core(tmp_path):
    records = SqlRecordStore(f"sqlite:///{tmp_path / 'martian.db'}")
    asked: list[tuple[str | None, tuple[str, ...]]] = []

    async def judge(request: JudgeRequest) -> JudgeResult:
        asked.append((request.model, tuple(s.name for s in request.scorers)))
        return await fake_judge(request)

    client, served = serve(MemoryStore(), records, judge_with=judge)
    served.asked = asked  # type: ignore[attr-defined]
    with client:
        yield served
    records.close()


def register_panel(core: Served, name: str, members: list[str], status: int = 201, **body: Any) -> Any:
    panel = [{"model": f"openai:/{m}"} for m in members]
    request = {"request_id": request_id(), "name": name, "instructions": f"Is {{{{ outputs }}}} {name}?",
               "panel": panel, **body}  # fmt: skip
    return core.call("POST", "/v1/evals/judge-panels", status, json=request)


def panel_scores(core: Served, *names: str) -> dict[str, list[dict[str, Any]]]:
    run = core.completed_run()
    body = {"request_id": request_id(), "acknowledge_model_cost": True,
            "judges": [{"name": name, "version": 1} for name in names]}  # fmt: skip
    core.call("POST", f"/v1/evals/{run['id']}/scores", 202, json=body)
    core.drain()
    found: dict[str, list[dict[str, Any]]] = {}
    for assessment in core.call("GET", f"/v1/evals/{run['id']}/workbench", 200)["assessments"]:
        found.setdefault(assessment["name"], []).append(assessment)
    return found


def judged_ones(assessments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The assessments of the cases that have a saved output."""
    return [a for a in assessments if a["error"] != "No successful saved target output"]


def test_a_registered_panel_where_one_judge_errors_still_decides(core):
    registered = register_panel(core, "tone", MEMBERS["tone"])
    assert registered["definition"]["model"] is None
    assert registered["definition"]["panel"] == [{"model": f"openai:/{m}", "endpoint": None} for m in "abc"]
    listed = core.call("GET", "/v1/evals/judges", 200)["items"]
    assert [item["definition"]["panel"] for item in listed] == [registered["definition"]["panel"]]

    tone = judged_ones(panel_scores(core, "tone")["tone"])
    assert len(tone) == 2 and {(a["value"], a["error"]) for a in tone} == {(True, "")}
    found = tone[0]["provenance"]
    assert [found[f"martian.panel.{n}.verdict"] for n in (1, 2, 3)] == ["pass", "error", "pass"]
    assert found["martian.panel.1.reason"] == "a says pass." and found["martian.panel.1.model_version"] == "a-1"
    assert "[judge_provider_auth]" in found["martian.panel.2.reason"]
    assert found["martian.judge.version"] == "rubric-v1" and found[CALIBRATION_KEY] == NO_CALIBRATION
    assert "martian.judge.model" not in found
    assert tone[0]["source_id"] == panel_source((f"openai:/{m}", None) for m in "abc")
    # Each member graded each case through its own judge request.
    assert sorted(model for model, _ in core.asked) == ["openai:/a"] * 2 + ["openai:/b"] * 2 + ["openai:/c"] * 2


def test_a_scoring_job_grades_a_panel_judge_and_an_installed_python_scorer_side_by_side(tmp_path):
    from fastapi.testclient import TestClient

    from martian.evals.server import create_app
    from martian.evals.worker import InProcessWorker, Work
    from martian.evals.worker.execute import NO_OUTPUT
    from martian.evals.worker.scorers import builtin_scorer
    from tests.evals.test_custom_scorers import custom, scoring_job
    from tests.evals.write_fixture import AGENT, agent, judge, publish

    asked: list[tuple[str | None, tuple[str, ...]]] = []

    async def recording(request: JudgeRequest) -> JudgeResult:
        asked.append((request.model, tuple(s.name for s in request.scorers)))
        return await judge(request)

    def cites_link(outputs):
        return "link" in outputs, "mentions the reset link"

    members = [PanelMember(model=f"openai:/{m}") for m in "abc"]
    panel = builtin_scorer("correctness").model_copy(update={"model": None, "endpoint": None, "panel": members})
    store, records = MemoryStore(), SqlRecordStore(f"sqlite:///{tmp_path / 'martian.db'}")
    asyncio.run(publish(store))
    worker = InProcessWorker(
        store, records, targets={AGENT: agent}, judge=recording, scorers={"cites_link": cites_link}
    )
    app = create_app(store=store, records=records, principal=lambda: SINGLE_USER, worker=worker)
    with TestClient(app) as client:
        assert client.portal is not None
        body = {"agent_id": AGENT, "dataset_name": "support-faq", "request_id": request_id()}
        run_id = client.post("/v1/evals", json=body).json()["id"]
        client.portal.call(worker.drain)
        job = scoring_job(run_id, [custom("cites_link"), panel])
        client.portal.call(records.create_scoring_job, SINGLE_USER, DEFAULT_PROJECT, job)
        work = Work(kind="scoring", principal=SINGLE_USER, project_id=DEFAULT_PROJECT, run_id=run_id, job_id=job.id)
        client.portal.call(worker.submit, work)
        client.portal.call(worker.drain)
        bench = client.get(f"/v1/evals/{run_id}/workbench").json()
    records.close()
    found: dict[str, list[dict[str, Any]]] = {}
    for assessment in bench["assessments"]:
        if assessment["error"] != NO_OUTPUT:
            found.setdefault(assessment["name"], []).append(assessment)
    # The python scorer ran in the worker; the panel's members each graded the judge, and only it.
    assert {(a["rationale"], a["source_id"]) for a in found["cites_link"]} == {
        ("mentions the reset link", "python:cites_link")
    }
    assert not any(key.startswith("martian.panel.") for a in found["cites_link"] for key in a["provenance"])
    assert CALIBRATION_KEY not in found["cites_link"][0]["provenance"]
    votes = found["correctness"][0]["provenance"]
    assert [votes[f"martian.panel.{n}.model"] for n in (1, 2, 3)] == [f"openai:/{m}" for m in "abc"]
    assert votes[CALIBRATION_KEY] == NO_CALIBRATION
    assert {names for _, names in asked} == {("correctness",)}
    assert sorted({model for model, _ in asked}) == [f"openai:/{m}" for m in "abc"]


def test_a_registered_panel_with_no_majority_is_an_error_never_a_pass(core):
    register_panel(core, "tone", MEMBERS["tone"])
    register_panel(core, "strict", MEMBERS["strict"])
    scored = panel_scores(core, "tone", "strict")
    strict = judged_ones(scored["strict"])
    assert len(strict) == 2 and {a["value"] for a in strict} == {None}
    assert all(
        a["error"].startswith(f"Judge panel has no majority [{NO_MAJORITY}]: 1 passed and 1 failed") for a in strict
    )
    assert strict[0]["provenance"]["martian.panel.verdict"] == "none"
    assert {a["value"] for a in judged_ones(scored["tone"])} == {True}
    # One request per member's model and case, carrying every judge that member sits on.
    both = ("strict", "tone")
    expected = [("openai:/a", both), ("openai:/b", both), ("openai:/c", ("tone",)), ("openai:/d", ("strict",))]
    assert sorted(core.asked) == sorted(expected * 2)


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"panel": [{"model": "openai:/a"}]}, "at least 2"),
        ({"model": "openai:/a"}, "not in model or endpoint"),
        ({"kind": "numeric"}, "kind is boolean"),
        ({"panel": [{"model": "openai:/a"}, {"model": "openai:/a"}]}, "each judge model once"),
        ({"panel": [{"model": "openai:/a"}, {"model": "openai:/b", "endpoint": "http://u:p@host/v1"}]}, "pattern"),
    ],
    ids=["one-member", "own-model", "numeric", "duplicate", "key-in-endpoint"],
)
def test_a_panel_judge_is_refused_unless_it_is_a_pass_fail_panel(core, change, problem):
    request = {"request_id": request_id(), "name": "tone", "instructions": "Is {{ outputs }} polite?",
               "panel": [{"model": "openai:/a"}, {"model": "openai:/b"}], **change}  # fmt: skip
    response = core.client.post("/v1/evals/judge-panels", json=request)
    assert response.status_code == 422 and problem in response.text


def test_a_panel_request_again_is_the_same_version_and_its_request_id_is_its_own(core):
    body = {"request_id": request_id(), "name": "tone", "instructions": "Is {{ outputs }} polite?",
            "panel": [{"model": "openai:/a"}, {"model": "openai:/b"}]}  # fmt: skip
    first = core.call("POST", "/v1/evals/judge-panels", 201, json=body)
    assert core.call("POST", "/v1/evals/judge-panels", 201, json=body) == first
    alone = {key: value for key, value in body.items() if key != "panel"}
    assert "another judge version" in core.call("POST", "/v1/evals/judges", 409, json=alone)["detail"]


# --- calibrated and reported as one judge --------------------------------------------------------


async def labelled_panel_run(core: Served, judge: str, members: list[str]) -> None:
    """A completed run whose every case the panel judged and a person labelled, enough to measure."""
    records, run_id = core.records, str(uuid.uuid4())
    run = Run(id=run_id, project_id=DEFAULT_PROJECT, code="EV-9", source="console", state="completed", created_at=T0)
    await records.create_run(SINGLE_USER, run)
    cases = [f"labelled-{i}" for i in range(MINIMUM_LABELLED)]
    results = [CaseResult(run_id=run_id, case_id=case, position=i, status="passed") for i, case in enumerate(cases)]
    await records.put_results(SINGLE_USER, DEFAULT_PROJECT, results)
    source = panel_source((f"openai:/{m}", None) for m in members)
    scores = []
    for i, case in enumerate(cases):
        decision = decide([Vote(f"openai:/{m}", i % 4 != 0, "Checked.", model_version=f"{m}-1") for m in members])
        scores.append(Score(run_id=run_id, case_id=case, scorer=judge, scorer_version="1", source="llm_judge",
                            source_id=source, value=decision.passed, passed=decision.passed,
                            metadata=decision.metadata(), scoring_job_id="job-0", created_at=T0))  # fmt: skip
    await records.put_scores(SINGLE_USER, DEFAULT_PROJECT, scores)
    for i, case in enumerate(cases):
        review = Review(id=f"review-{i}", run_id=run_id, case_id=case, criterion=judge, state="complete",
                        value=i % 5 != 0, created_at=T0)  # fmt: skip
        await records.add_review(SINGLE_USER, DEFAULT_PROJECT, review)


def test_a_panel_is_reported_and_calibrated_as_one_judge(core):
    register_panel(core, "tone", MEMBERS["tone"])
    asyncio.run(labelled_panel_run(core, "tone", MEMBERS["tone"]))

    (item,) = core.call("GET", "/v1/evals/judge-agreement", 200, params={"name": "tone"})["items"]
    assert (item["version"], item["panel"], item["status"], item["labelled"]) == ("1", True, "measured", 30)
    assert item["models"] == ["openai:/a@a-1", "openai:/b@b-1", "openai:/c@c-1"]

    body = {"request_id": request_id(), "name": "tone", "version": "1"}
    saved = core.call("POST", "/v1/evals/judge-calibrations", 201, json=body)
    members = [{"model": f"openai:/{m}", "endpoint": None} for m in MEMBERS["tone"]]
    assert (saved["model"], saved["endpoint"], saved["panel"]) == (None, None, members)
    (listed,) = core.call("GET", "/v1/evals/judge-calibrations", 200, params={"name": "tone"})["items"]
    assert (listed["status"], listed["panel"], listed["calibration"]["id"]) == ("checked", members, saved["id"])

    # The panel's scores now name the calibration of the panel as a whole.
    tone = judged_ones(panel_scores(core, "tone")["tone"])
    assert {a["provenance"][CALIBRATION_KEY] for a in tone} == {saved["id"]}


def test_a_panel_calibration_checks_only_the_members_it_was_measured_on():
    measured = [PanelMember(model="openai:/a"), PanelMember(model="openai:/b")]
    found = calibration("c1", model=None).model_copy(update={"panel": measured})
    assert calibration_state(found, on(None, panel=measured)) == ("checked", None)
    other = [PanelMember(model="openai:/a"), PanelMember(model="openai:/c")]
    assert calibration_state(found, on(None, panel=other)) == ("unchecked", "model_changed")
    # A single judge's calibration does not check a panel, nor a panel's a single judge.
    assert calibration_state(calibration("c2", model=None), on(None, panel=measured)) == ("unchecked", "model_changed")
    assert calibration_state(found, on(None)) == ("unchecked", "model_changed")
