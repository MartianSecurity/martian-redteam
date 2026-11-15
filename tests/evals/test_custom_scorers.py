"""Custom Python scorers (MartianSecurity/martian-notes#177): a suite file names a function as a check,
the Python API takes a callable, and a worker runs the scorers its setup installs. A scorer that
raises is a scorer error for that case, never a crash. On a server the code runs only in the
work's own process: the process that serves the API never imports a scorer file. Nothing here
calls a model."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import mlflow
import pytest
import yaml
from fastapi.testclient import TestClient

from martian.config import ConfigError
from martian.evals import GateFailure, assert_checks, evaluate, python_check
from martian.evals._custom import ScorerError
from martian.evals.model import Project, ScorerVersion, ScoringJob
from martian.evals.records.sql import SqlRecordStore
from martian.evals.runner import run_suite
from martian.evals.server import DEFAULT_PROJECT, SINGLE_USER, create_app
from martian.evals.state import STATE
from martian.evals.store import MemoryStore
from martian.evals.suite_file import load_suite_file, suite_from_yaml, suite_to_yaml
from martian.evals.worker import InProcessWorker, JudgeRequest, ProcessWorker, Work
from martian.evals.worker.execute import NO_OUTPUT
from martian.evals.worker.scorers import BUILTIN_SCORERS, builtin_scorer
from tests.evals.process_fixture import SCORERS, cases, member, open_stores, scorer_services
from tests.evals.test_runner import flaky_suite, results_of
from tests.evals.write_fixture import AGENT, agent, judge, publish, request_id


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("MLFLOW_DISABLE_TELEMETRY", "true")
    monkeypatch.setenv("MARTIAN_EVALS_HOME", str(tmp_path / "evals-home"))
    monkeypatch.setattr(mlflow.tracking.fluent, "_active_experiment_id", None)
    old_uri = mlflow.get_tracking_uri()
    STATE.reset()
    yield
    STATE.reset()
    mlflow.set_tracking_uri(old_uri)


# A scorer file that records, in imported.txt beside it, the process id of every process that
# imports it, so a test can tell which processes ran its code.
MARKER = """\
import os
from pathlib import Path

with open(Path(__file__).with_name("imported.txt"), "a", encoding="utf-8") as marker:
    marker.write(f"{os.getpid()}\\n")
"""

SUITE_SCORERS = (
    MARKER
    + '''

def cites_policy(outputs, expectations):
    """Pass when the reply names the policy the case expects."""
    wanted = expectations["policy"]
    return wanted in outputs, f"looks for {wanted}"


def politeness(outputs):
    return outputs.lower().count("please") / 2


def needs_order(inputs):
    if "order" not in inputs:
        raise KeyError("order")
    return inputs["order"].startswith("A-")
'''
)

STORED_CASES = [
    {"id": "good", "inputs": {"message": "Refund?", "order": "A-1"}, "output": "Please see REF-7, please.",
     "expectations": {"policy": "REF-7"}},
    {"id": "bad", "inputs": {"message": "Refund?", "order": "B-2"}, "output": "No.",
     "expectations": {"policy": "REF-7"}},
    {"id": "no-order", "input": "Refund?", "output": "Please see REF-7.", "expectations": {"policy": "REF-7"}},
]  # fmt: skip

PYTHON_CHECKS = [
    {"type": "python", "name": "cites_policy", "file": "scorers.py", "function": "cites_policy"},
    {"type": "python", "name": "polite", "file": "scorers.py", "function": "politeness", "min_score": 0.5},
    {"type": "python", "name": "order_format", "file": "scorers.py", "function": "needs_order"},
]


def write_suite(folder: Path, checks=None, *, scorers: str = SUITE_SCORERS, cases_=STORED_CASES, **extra) -> Path:
    (folder / "cases.jsonl").write_text("".join(json.dumps(case) + "\n" for case in cases_), encoding="utf-8")
    (folder / "scorers.py").write_text(scorers, encoding="utf-8")
    suite = {"data": "cases.jsonl", "checks": checks or PYTHON_CHECKS, **extra}
    path = folder / "evals.yaml"
    path.write_text(yaml.safe_dump(suite, sort_keys=False), encoding="utf-8")
    return path


def imported_by(folder: Path) -> list[int]:
    marker = folder / "imported.txt"
    return [int(line) for line in marker.read_text(encoding="utf-8").split()] if marker.exists() else []


# --- the suite file ---------------------------------------------------------------------------


def test_a_suite_file_names_a_python_function_beside_it_as_a_check(tmp_path):
    summary = run_suite(write_suite(tmp_path))
    records = results_of(tmp_path)
    checks = {
        case: {label: found["passed"] for label, found in record["checks"].items()} for case, record in records.items()
    }
    assert checks == {
        "good": {"cites_policy": True, "polite": True, "order_format": True},
        "bad": {"cites_policy": False, "polite": False, "order_format": False},
        # The scorer raised for this case: that check has no verdict, so neither has the case.
        "no-order": {"cites_policy": True, "polite": True, "order_format": None},
    }
    good, bad, unordered = records["good"]["checks"], records["bad"]["checks"], records["no-order"]["checks"]
    assert good["cites_policy"]["rationale"] == "looks for REF-7"
    assert good["polite"]["rationale"] == "score 1.0 is at least 0.5"
    assert bad["polite"]["rationale"] == "score 0.0 is below min_score 0.5"
    assert bad["order_format"]["rationale"] == "the scorer failed the reply"
    assert "the scorer raised KeyError: 'order'" in unordered["order_format"]["error"]
    # The rest of the run went on, and the case without a verdict is an error, not a crash.
    assert (summary["passed"], summary["failed"], summary["errors"]) == (1, 1, 1)
    assert summary["checks"]["order_format"]["errors"] == 1
    # The run's own process imported the file once, for all three checks.
    assert imported_by(tmp_path) == [os.getpid()]


def test_a_python_check_counts_by_the_contents_of_its_file(tmp_path):
    path = write_suite(tmp_path)
    run_suite(path)
    assert run_suite(path)["invalidated"] == 0
    (tmp_path / "scorers.py").write_text(SUITE_SCORERS.replace("/ 2", "/ 4"), encoding="utf-8")
    again = run_suite(path)
    assert again["invalidated"] == 2  # the two cases with a verdict are graded again
    assert results_of(tmp_path)["good"]["checks"]["polite"]["passed"] is True


@pytest.mark.parametrize(
    ("scorers", "check", "problem"),
    [
        ("", {"function": "missing"}, "check python: the scorer file has no function 'missing'"),
        ("raise RuntimeError('broken')\n", {}, "check python: the scorer file failed to import: RuntimeError"),
        ("def score(reply):\n    return True\n", {}, "takes reply, which a scorer is not given"),
        ("def score(**given):\n    return True\n", {}, r"takes \*\*given; name the arguments it reads"),
        ("async def score(outputs):\n    return True\n", {}, "is async; a scorer is a plain function"),
        ("def score(threshold=1):\n    return True\n", {}, "reads none of inputs, outputs, expectations, trace"),
        ("def score(outputs, /):\n    return True\n", {}, "takes outputs by position only"),
        ("def score(outputs):\n    return True\n", {"file": "elsewhere.py"}, "could not load the scorer file"),
    ],
)
def test_a_scorer_that_cannot_be_called_stops_the_run_before_any_case(tmp_path, scorers, check, problem):
    checks = [{"type": "python", "file": "scorers.py", "function": "score", **check}]
    with pytest.raises(ConfigError, match=problem):
        run_suite(write_suite(tmp_path, checks, scorers=scorers))
    assert not (tmp_path / "results").exists() and not (tmp_path / "evals-home").exists()


@pytest.mark.parametrize(
    ("returned", "error"),
    [
        ("None", "the scorer returned None; return True or False, a number, or (value, reason)"),
        ("'yes'", "the scorer returned a str; return True or False"),
        ("float('nan')", "the scorer returned the number nan"),
        ("(True, 3)", "the scorer returned a tuple that is not (value, reason)"),
        ("0.9", "the scorer returned the number 0.9; give min_score or max_score to pass or fail it"),
    ],
)
def test_a_scorer_that_returns_what_it_may_not_is_a_scorer_error(tmp_path, returned, error):
    checks = [{"type": "python", "file": "scorers.py", "function": "score"}]
    summary = run_suite(write_suite(tmp_path, checks, scorers=f"def score(outputs):\n    return {returned}\n"))
    assert summary["errors"] == 3
    assert {error in record["checks"]["python"]["error"] for record in results_of(tmp_path).values()} == {True}


@pytest.mark.parametrize(
    ("check", "problem"),
    [
        ({"file": "scorers.py"}, "a python check needs a file and a function"),
        ({"function": "score"}, "a python check needs a file and a function"),
        ({"file": "scorers.py", "function": "scorers.score"}, "function must be the name of a function"),
        ({"file": "scorers.py", "function": "score", "min_score": 2, "max_score": 1}, "min_score is more than"),
        ({"file": "scorers.py", "function": "score", "values": ["a"]}, "values does not apply to python checks"),
    ],
)
def test_a_python_checks_options_are_checked_when_the_suite_loads(tmp_path, check, problem):
    with pytest.raises(ConfigError, match=problem):
        load_suite_file(write_suite(tmp_path, [{"type": "python", **check}]))


def test_a_python_check_option_on_another_check_is_refused(tmp_path):
    for option in ({"file": "scorers.py"}, {"function": "score"}, {"min_score": 1}, {"security": True}):
        name = next(iter(option))
        with pytest.raises(ConfigError, match=f"{name} does not apply to not_contains checks"):
            load_suite_file(write_suite(tmp_path, [{"type": "not_contains", "values": ["x"], **option}]))


def test_loading_and_writing_a_suite_never_imports_its_scorer_file(tmp_path):
    checks = [{**PYTHON_CHECKS[1], "max_score": 3, "security": True, "combine": "any"}, PYTHON_CHECKS[0]]
    suite, cases_ = load_suite_file(write_suite(tmp_path, checks, repeats=3))
    assert suite.scorers[0] == ScorerVersion(
        name="polite",
        kind="code",
        type="python",
        parameters={"file": "scorers.py", "function": "politeness", "max_score": 3, "min_score": 0.5, "security": True},
    )
    text = suite_to_yaml(suite)
    assert yaml.safe_load(text)["checks"] == checks
    assert suite_from_yaml(text, name=suite.name, cases=cases_) == suite
    assert imported_by(tmp_path) == []
    assert not any(name.startswith("martian_evals_scorer_") for name in sys.modules if "scorers.py" in name)


def test_a_python_check_is_a_security_check_only_when_it_says_so(tmp_path):
    # One call in five leaks the secret; the suite takes any passing attempt.
    keeps_secret = (
        "def keeps_secret(outputs, expectations):\n    return expectations['secret'] not in outputs.lower()\n"
    )
    (tmp_path / "scorers.py").write_text(keeps_secret, encoding="utf-8")
    entry = "  - type: python\n    file: scorers.py\n    function: keeps_secret\n"
    checks = f"{entry}    name: guarded\n    security: true\n{entry}    name: unguarded\n"
    summary = run_suite(flaky_suite(tmp_path, checks=checks, extra="combine: any\n"))
    assert summary["repeats"]["checks"] == {
        "guarded": {"combine": "all", "from": "security"},
        "unguarded": {"combine": "any", "from": "suite"},
    }
    assert {label: found["passed"] for label, found in summary["checks"].items()} == {"guarded": 0, "unguarded": 2}
    for record in results_of(tmp_path).values():
        assert record["checks"]["guarded"]["rationale"].startswith("4 of 5 attempts passed, 1 failed (all:")


EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "evals" / "custom-scorer"


def test_the_custom_scorer_example_passes_its_sample_and_fails_its_two_mistakes(tmp_path):
    for name in ("evals.yaml", "cases.jsonl", "scorers.py"):
        (tmp_path / name).write_text((EXAMPLE / name).read_text(encoding="utf-8"), encoding="utf-8")
    summary = run_suite(tmp_path / "evals.yaml")
    assert (summary["passed"], summary["failed"], summary["errors"]) == (1, 2, 0)
    records = results_of(tmp_path)
    assert records["refunds-ten-times-over"]["checks"]["refund_within_order"]["rationale"] == (
        "score 10.0 is above max_score 1.0: refund 250.00 of an order of 25.00"
    )
    assert records["no-reason-code"]["checks"]["reason_code"] == {
        "passed": False,
        "rationale": "reason 'goodwill'",
        "error": None,
    }


# --- the Python API ---------------------------------------------------------------------------


def test_python_check_takes_a_callable_with_the_same_rules():
    def grounded(outputs, expectations, trace=None, tolerance=2):
        return outputs.count(expectations["fact"]) / tolerance, "counts the fact"

    check = python_check(grounded, name="grounded", min_score=0.5, max_score=1)
    assert check.name == "grounded"
    passed = check(outputs="Paris. Paris.", expectations={"fact": "Paris"}, trace=None)
    assert (passed.value, passed.rationale) == (True, "score 1.0 is at least 0.5 and at most 1: counts the fact")
    over = check(outputs="Paris " * 3, expectations={"fact": "Paris"}, trace=None)
    assert (over.value, over.rationale) == (False, "score 1.5 is above max_score 1: counts the fact")
    with pytest.raises(ScorerError, match="the scorer raised KeyError: 'fact'"):
        check(outputs="Paris", expectations={}, trace=None)
    for refused, problem in (
        (lambda reply: True, "takes reply, which a scorer is not given"),
        (len, "takes obj, which a scorer is not given"),
        ("not a function", "a python check needs a function to call"),
    ):
        with pytest.raises(ValueError, match=problem):
            python_check(refused)
    with pytest.raises(ValueError, match="min_score must be a number"):
        python_check(grounded, min_score=float("inf"))
    with pytest.raises(ValueError, match="nonempty name"):
        python_check(grounded, name=" ")


def test_a_raising_python_check_in_evaluate_fails_the_gate_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("MLFLOW_GENAI_EVAL_SKIP_TRACE_VALIDATION", "true")
    mlflow.set_tracking_uri(f"sqlite:///{tmp_path / 'tracking.db'}")

    def mentions_order(inputs, outputs):
        return inputs["order"] in outputs  # KeyError for the row with no order

    data = [
        {"inputs": {"message": "Where is it?", "order": "A-1"}, "outputs": "A-1 ships today."},
        {"inputs": {"message": "Where is it?"}, "outputs": "It ships today."},
    ]
    result = evaluate(data=data, scorers=[python_check(mentions_order, name="mentions_order")])
    frame = result.result_df
    assert list(frame["mentions_order/value"].iloc[:1]) == [True]
    assert "the scorer raised KeyError: 'order'" in str(frame["mentions_order/error_message"].iloc[1])
    with pytest.raises(GateFailure, match="mentions_order"):
        assert_checks(result)
    assert_checks(evaluate(data=data[:1], scorers=[python_check(mentions_order, name="mentions_order")]))


# --- the core worker --------------------------------------------------------------------------


def custom(name: str) -> ScorerVersion:
    return ScorerVersion(name=name, kind="code", type="python")


def scoring_job(run_id: str, scorers: list[ScorerVersion]) -> ScoringJob:
    return ScoringJob(
        id=str(uuid.uuid4()),
        run_id=run_id,
        scorers=scorers,
        state="queued",
        requested_by=SINGLE_USER.user_id,
        created_at=datetime.now(UTC),
    )


def test_the_worker_runs_the_scorers_its_setup_installs_and_never_hands_them_to_a_judge(tmp_path):
    def cites_link(outputs):
        return "link" in outputs, "mentions the reset link"

    def words(outputs, expectations):
        return len(outputs.split())

    def broken(inputs):
        raise RuntimeError("no rule for this case")

    def unsure(outputs):
        return "maybe"

    asked: list[JudgeRequest] = []

    async def recording(request):
        asked.append(request)
        return await judge(request)

    store, records = MemoryStore(), SqlRecordStore(f"sqlite:///{tmp_path / 'martian.db'}")
    asyncio.run(publish(store))
    installed = {"cites_link": cites_link, "words": words, "broken": broken, "unsure": unsure}
    worker = InProcessWorker(store, records, targets={AGENT: agent}, judge=recording, scorers=installed)
    app = create_app(store=store, records=records, principal=lambda: SINGLE_USER, worker=worker)
    with TestClient(app) as client:
        assert client.portal is not None
        run = client.post(
            "/v1/evals", json={"agent_id": AGENT, "dataset_name": "support-faq", "request_id": request_id()}
        )
        client.portal.call(worker.drain)
        run_id = run.json()["id"]
        names = [*installed, "not_installed"]
        job = scoring_job(run_id, [*map(custom, names), builtin_scorer("correctness")])
        client.portal.call(records.create_scoring_job, SINGLE_USER, DEFAULT_PROJECT, job)
        work = Work(kind="scoring", principal=SINGLE_USER, project_id=DEFAULT_PROJECT, run_id=run_id, job_id=job.id)
        client.portal.call(worker.submit, work)
        client.portal.call(worker.drain)
        bench = client.get(f"/v1/evals/{run_id}/workbench").json()
        items = {item["id"]: item["dataset_item_id"] for item in client.get(f"/v1/evals/{run_id}").json()["items"]}
    records.close()
    scores = {
        (a["name"], items[a["item_id"]]): (a["value"], a["rationale"], a["error"], a["source_id"])
        for a in bench["assessments"]
        if a["name"] in names
    }
    assert scores[("cites_link", "faq-1")] == (True, "mentions the reset link", "", "python:cites_link")
    assert scores[("cites_link", "faq-2")][:3] == (False, "mentions the reset link", "")
    assert scores[("words", "faq-1")][:3] == (8, "", "")  # "Use the reset link on the sign-in page."
    assert scores[("broken", "faq-1")][:3] == (None, "", "the scorer raised RuntimeError: no rule for this case")
    assert scores[("unsure", "faq-2")][2].startswith("the scorer returned a str")
    assert scores[("not_installed", "faq-1")][2] == "This worker has no custom scorer named not_installed"
    assert scores[("cites_link", "faq-3")][2] == NO_OUTPUT  # the skipped case has no output
    assert bench["selected_job"]["state"] == "partial"
    # The judge graded only the judge scorer; a custom scorer never reaches it.
    assert asked and {scorer.name for request in asked for scorer in request.scorers} == {"correctness"}


def test_a_worker_refuses_an_installed_scorer_that_reads_a_trace(tmp_path):
    records = SqlRecordStore(f"sqlite:///{tmp_path / 'martian.db'}")
    with pytest.raises(ValueError, match="takes trace, which a scorer is not given; it takes inputs, outputs"):
        InProcessWorker(MemoryStore(), records, scorers={"traced": lambda outputs, trace: True})
    records.close()


SERVER_SCORERS = (
    MARKER
    + """

def names_organisation(inputs, outputs):
    return inputs["organisation"] in outputs, "names the organisation"


def words(outputs):
    return len(outputs.split())


def broken(expectations):
    raise RuntimeError("no rule")
"""
)


def test_a_server_runs_custom_scorers_only_in_the_works_own_process(tmp_path):
    (tmp_path / "scorers.py").write_text(SERVER_SCORERS, encoding="utf-8")
    store, records = open_stores(tmp_path)
    principal = member("org-a")
    asyncio.run(store.create_project(principal, Project(id=DEFAULT_PROJECT, name="Default")))
    asyncio.run(store.create_dataset(principal, DEFAULT_PROJECT, "support-faq", cases("org-a")))
    worker = ProcessWorker(scorer_services, records, agents=[AGENT], options={"folder": str(tmp_path)})
    app = create_app(store=store, records=records, principal=lambda: principal, worker=worker)
    with TestClient(app) as client:
        assert client.portal is not None
        launched = client.post(
            "/v1/evals", json={"agent_id": AGENT, "dataset_name": "support-faq", "request_id": request_id()}
        )
        assert launched.status_code == 202, launched.text
        run_id = launched.json()["id"]
        client.portal.call(worker.drain)
        # Every route of the scoring dialog, suites and judges answers without the scorer code:
        # the scorers on offer are the built-in ones, and the frozen contract's scoring request and
        # suite name only those (a custom one by name is MartianSecurity/martian-notes#334).
        assert [s["name"] for s in client.get("/v1/evals/scorers").json()] == list(BUILTIN_SCORERS)
        named = {"request_id": request_id(), "scorers": ["names_organisation"]}
        assert client.post(f"/v1/evals/{run_id}/scores", json=named).status_code == 422
        suite = {"request_id": request_id(), "name": "Custom", "scorers": ["names_organisation"]}
        assert client.post("/v1/evals/suites", json=suite).status_code == 422
        assert client.get("/v1/evals/judges").status_code == 200
        # A job of the installed scorers, recorded as an edition's own code records one.
        job = scoring_job(run_id, [*map(custom, SCORERS), builtin_scorer("exact_match")])
        job = job.model_copy(update={"requested_by": principal.user_id})
        client.portal.call(records.create_scoring_job, principal, DEFAULT_PROJECT, job)
        work = Work(kind="scoring", principal=principal, project_id=DEFAULT_PROJECT, run_id=run_id, job_id=job.id)
        client.portal.call(worker.submit, work)
        client.portal.call(worker.drain)
        bench = client.get(f"/v1/evals/{run_id}/workbench").json()
        assert client.get(f"/v1/evals/{run_id}").json()["state"] == "completed"
    records.close()
    assert bench["selected_job"]["state"] == "partial"  # the broken scorer's scores are errors
    found = {(a["name"], a["value"], a["error"]) for a in bench["assessments"] if a["name"] in SCORERS}
    assert ("names_organisation", True, "") in found
    assert ("words", 6, "") in found  # "The answer to org-a question 0."
    assert ("broken", None, "the scorer raised RuntimeError: no rule") in found
    # Each work's process imported the scorer file; the process that serves the API never did.
    pids = imported_by(tmp_path)
    assert len(pids) == 2 and os.getpid() not in pids, pids
    assert str(tmp_path / "scorers.py") not in {getattr(module, "__file__", None) for module in sys.modules.values()}
