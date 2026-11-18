"""Per-scorer options for the deterministic checks in the evals contract (MartianSecurity/martian-notes#334): a
scoring request and a saved suite carry ``checks``, each a check's type and its options; the core's
worker runs them on a run's saved outputs with no model call, and scores them as a suite-file run of
the same checks does. ``GET /v1/evals/checks`` says what each check takes."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import mlflow
import pytest

from martian.evals import runner
from martian.evals.model import Case
from martian.evals.records.sql import SqlRecordStore
from martian.evals.runner import run_suite
from martian.evals.server import SINGLE_USER
from martian.evals.server.projection import DEFAULT_PROJECT
from martian.evals.state import STATE
from martian.evals.store import MemoryStore
from martian.evals.suite_file import CHECK_OPTIONS
from tests.evals.evals_contract import CONTRACT, VERSION, validate_request
from tests.evals.write_fixture import AGENT, Served, request_id, serve

REPLY = {
    "role": "assistant",
    "content": None,
    "tool_calls": [
        {"id": "t1", "type": "function", "function": {"name": "lookup_order", "arguments": '{"order": "A1"}'}}
    ],
}
DONE = {"role": "assistant", "content": "Your order shipped."}
# Each case: its input, the output the run saved for it, and its expectations. `expected_response` is
# the text a run needs to grade by exact match; the checks read the other keys.
CASES: dict[str, dict[str, Any]] = {
    "text": {
        "input": "Where is my refund?",
        "output": "Your refund of $1,234.56 for order ORD-123456 was sent. Contact support@shop.example.",
        "expectations": {
            "expected_response": "Your refund was sent.",
            "must_mention": ["refund", "order"],
            "forbidden": "password",
            "reference": "Your refund of $1,234.56 for order ORD-123456 was sent.",
        },
    },
    "json": {
        "input": "Reply in JSON.",
        "output": json.dumps(
            {
                "answer": "Paris",
                "lines": [{"id": "a", "amount": "10.00"}, {"id": "b", "amount": "20.50"}],
                "subtotal": "30.50",
                "opening": 100,
                "spent": 40,
                "closing": 60,
            }
        ),
        "expectations": {
            "expected_response": "Paris",
            "must_mention": ["Paris"],
            "forbidden": "Berlin",
            "reference": "Paris",
        },
    },
    "agent": {
        "input": "Close my account and look up my order.",
        "output": [REPLY, {"role": "tool", "tool_call_id": "t1", "content": "shipped"}, DONE],
        "expectations": {
            "expected_response": "Your order shipped.",
            "must_mention": ["order"],
            "forbidden": "ZEBRA-7731",
            "reference": "Your order shipped.",
        },
    },
}
# Every deterministic check the suite file offers, with options that make it read something: a value
# fixed in the suite, a value each case brings (`expectation`), a pattern, a schema, a bound.
CHECKS: dict[str, dict[str, Any]] = {
    "contains": {"values": ["refund", "order"]},
    "contains_all": {"expectation": "must_mention"},
    "contains_any": {"values": ["Sorry", "REFUND"], "case_sensitive": True},
    "not_contains": {"expectation": "forbidden"},
    "equals": {"values": ["Paris"]},
    "starts_with": {"values": ["Your refund"]},
    "ends_with": {"values": ["support@shop.example."]},
    "similarity": {"expectation": "reference", "min_similarity": 0.6},
    "matches": {"pattern": r"ORD-\d{6}"},
    "not_matches": {"pattern": "sk-[A-Za-z0-9]{8,}"},
    "is_json": {"required_keys": ["answer"]},
    "json_schema": {"schema": {"type": "object", "required": ["answer"], "properties": {"answer": {"type": "string"}}}},
    "length": {"max_length": 80},
    "number_equals": {"value": "1234.56", "pattern": r"\$([\d,.]+)"},
    "sums_to": {"parts": ["lines.*.amount"], "total": "subtotal"},
    "reconciles": {"add": ["opening"], "subtract": ["spent"], "total": "closing"},
    "table_equals": {
        "rows": [{"id": "a", "amount": "10.00"}, {"id": "b", "amount": "20.50"}],
        "key": "id",
        "path": "lines",
    },
    "no_prohibited_phrases": {"values": ["guaranteed returns"]},
    "no_personal_data": {"allow": ["support@shop.example"]},
    "no_secrets": {"kinds": ["api_key", "private_key"]},
    "resists_injection": {"expectation": "forbidden"},
    "no_refusal": {},
    "tool_called": {"calls": ["lookup_order"], "max_calls": 1},
    "tool_not_called": {"calls": ["close_account"]},
    "tool_trajectory": {"calls": ["lookup_order"]},
    "step_count": {"max_turns": 3, "source": "reply"},
}
# The two checks that read the target's call, which a suite file grades only against a target.
CALL_CHECKS = {"latency": {"max_ms": 60000}, "cost": {"max_tokens": 1000}}


@pytest.fixture
def records(tmp_path):
    store = SqlRecordStore(f"sqlite:///{tmp_path / 'martian.db'}")
    yield store
    store.close()


@pytest.fixture
def suite_file_run(tmp_path, monkeypatch):
    """The suite file's own results for ``CHECKS`` over the saved outputs: each case's verdict, reason
    and error on each check, as ``martian evals run`` writes them."""
    monkeypatch.setenv("MLFLOW_DISABLE_TELEMETRY", "true")
    monkeypatch.setenv("MARTIAN_EVALS_HOME", str(tmp_path / "evals-home"))
    monkeypatch.setattr(mlflow.tracking.fluent, "_active_experiment_id", None)
    old = mlflow.get_tracking_uri()
    STATE.reset()
    rows = [
        {"id": name, "input": case["input"], "output": case["output"], "expectations": case["expectations"]}
        for name, case in CASES.items()
    ]
    (tmp_path / "cases.jsonl").write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    checks = [{"type": kind, **options} for kind, options in CHECKS.items()]
    (tmp_path / "evals.json").write_text(json.dumps({"data": "cases.jsonl", "checks": checks}), encoding="utf-8")
    run_suite(tmp_path / "evals.json", output=tmp_path / "out")
    STATE.reset()
    mlflow.set_tracking_uri(old)
    found: dict[tuple[str, str], tuple[Any, Any, Any]] = {}
    for line in (tmp_path / "out" / "results.jsonl").read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        for label, verdict in row["checks"].items():
            found[(row["id"], label)] = (verdict["passed"], verdict["rationale"], verdict["error"])
    return found


def served_with_saved_outputs(records, **options: Any):
    """A core whose agent answers each case with the output saved for it above, and a run of them."""

    async def answering(case: Case):
        return CASES[case.id]["output"]

    store = MemoryStore()
    client, served = serve(store, records, targets={AGENT: answering}, **options)
    cases = [
        Case(id=name, inputs={"message": row["input"]}, expectations=row["expectations"]) for name, row in CASES.items()
    ]
    asyncio.run(store.create_dataset(SINGLE_USER, DEFAULT_PROJECT, "options", cases))
    return client, served


def scored(served: Served, run: dict[str, Any], **body: Any) -> dict[tuple[str, str], tuple[Any, Any, Any]]:
    """Each case's value, rationale and error on each score a scoring request made, by case id and name."""
    served.call("POST", f"/v1/evals/{run['id']}/scores", 202, json={"request_id": request_id(), **body})
    served.drain()
    page = served.call("GET", f"/v1/evals/{run['id']}/workbench", 200)
    cases = {item["id"]: item["dataset_item_id"] for item in served.call("GET", f"/v1/evals/{run['id']}", 200)["items"]}
    return {
        # A score with no verdict has no reason, as a suite file's result has none.
        (cases[each["item_id"]], each["name"]): (each["value"], each["rationale"] or None, each["error"] or None)
        for each in page["assessments"]
    }


def test_the_contract_is_version_4_and_names_checks_and_their_catalogue():
    assert VERSION == "4"
    for name in ("ScoreRequest", "SuiteRequest"):
        schema = CONTRACT["components"]["schemas"][name]
        assert "checks" in schema["properties"] and "checks" not in schema.get("required", [])
    assert "get" in CONTRACT["paths"]["/v1/evals/checks"]
    # Optional fields only: a version 3 client's bodies are still valid.
    validate_request("ScoreRequest", {"request_id": request_id(), "scorers": ["contains_expected"]})
    validate_request("SuiteRequest", {"request_id": request_id(), "name": "Gate", "scorers": ["valid_json"]})
    validate_request(
        "ScoreRequest",
        {
            "request_id": request_id(),
            "checks": [{"type": "contains", "name": "refund_named", "options": {"values": ["refund"]}}],
        },
    )


def test_the_catalogue_names_every_check_the_suite_file_offers_with_its_options(records):
    client, served = serve(MemoryStore(), records)
    with client:
        found = served.call("GET", "/v1/evals/checks", 200)
    types = [entry["type"] for entry in found["items"]]
    assert types == list(runner.CHECKS)
    for entry in found["items"]:
        assert {option["name"] for option in entry["options"]} == CHECK_OPTIONS[entry["type"]], entry["type"]
        assert entry["label"] and entry["description"]
        for option in entry["options"]:
            assert option["kind"] in {"strings", "text", "boolean", "number", "integer", "choice", "choices", "json"}
            assert option["label"] and option["description"]
    # Every check the tests below apply has options in the catalogue.
    assert set(CHECKS) | set(CALL_CHECKS) == set(types)


def test_every_check_applied_with_its_options_scores_as_the_suite_file_run_does(records, suite_file_run):
    def no_model(request):
        raise AssertionError("a deterministic check makes no model call")

    client, served = served_with_saved_outputs(records, judge_with=no_model)
    with client:
        launched = served.launch(dataset_name="options")
        served.drain()
        run = served.call("GET", f"/v1/evals/{launched['id']}", 200)
        assert run["state"] == "completed"
        checks = [{"type": kind, "options": options} for kind, options in CHECKS.items()]
        found = scored(served, run, checks=checks)
    assert set(found) == set(suite_file_run) and len(found) == len(CHECKS) * len(CASES)
    for key, (value, rationale, error) in suite_file_run.items():
        assert found[key] == (value, rationale, error), key
    # The checks were not all passing, failing or erroring: the options decided.
    assert {value for value, *_ in found.values()} >= {True, False, None}


def test_options_decide_the_score_and_a_check_may_be_named_and_repeated(records):
    client, served = served_with_saved_outputs(records)
    with client:
        run = served.launch(dataset_name="options")
        served.drain()
        run = served.call("GET", f"/v1/evals/{run['id']}", 200)
        checks = [
            {"type": "contains", "name": "names_refund", "options": {"values": ["refund"]}},
            {"type": "contains", "name": "names_paris", "options": {"values": ["Paris"]}},
            {"type": "length", "name": "short", "options": {"max_length": 5}},
        ]
        found = scored(served, run, checks=checks)
    assert [found[("text", name)][0] for name in ("names_refund", "names_paris", "short")] == [True, False, False]
    assert [found[("json", name)][0] for name in ("names_refund", "names_paris", "short")] == [False, True, False]


def test_a_check_that_reads_the_recorded_call_reads_what_the_worker_recorded(records):
    client, served = served_with_saved_outputs(records)
    with client:
        run = served.launch(dataset_name="options")
        served.drain()
        run = served.call("GET", f"/v1/evals/{run['id']}", 200)
        found = scored(
            served,
            run,
            checks=[{"type": kind, "options": options} for kind, options in CALL_CHECKS.items()]
            + [{"type": "latency", "name": "too_slow", "options": {"max_ms": 0.0001}}],
        )
    # The worker recorded how long each call took, so latency has a verdict; it recorded no token usage or cost.
    assert [found[(name, "latency")][0] for name in CASES] == [True, True, True]
    assert [found[(name, "too_slow")][0] for name in CASES] == [False, False, False]
    assert found[("text", "latency")][1].endswith(" ms")
    assert found[("text", "cost")][0] is None
    assert found[("text", "cost")][2] == "the trace records no token usage; trace the target's model calls with MLflow"


@pytest.mark.parametrize(
    ("check", "problem"),
    [
        ({"type": "contains", "options": {}}, "needs values or an expectation key"),
        ({"type": "contains", "options": {"values": ["a"], "pattern": "x"}}, "does not apply"),
        ({"type": "matches", "options": {"pattern": "("}}, "pattern"),
        ({"type": "similarity", "options": {"values": ["a"]}}, "min_similarity"),
        ({"type": "length", "options": {"min_length": 5, "max_length": 2}}, "min_length is more than max_length"),
        ({"type": "no_secrets", "options": {"kinds": ["password", "pin"]}}, "kinds"),
        ({"type": "json_schema", "options": {"schema": {"if": {}}}}, "schema"),
        ({"type": "python", "options": {"file": "x.py", "function": "f"}}, "type"),
        ({"type": "rubric", "options": {}}, "type"),
        ({"type": "contains", "name": "Not Valid", "options": {"values": ["a"]}}, "name"),
        ({"type": "contains", "name": "exact_match", "options": {"values": ["a"]}}, "reserved"),
    ],
)
def test_options_a_check_cannot_take_are_refused_before_any_job_is_recorded(records, check, problem):
    client, served = serve(MemoryStore(), records)
    with client:
        run = served.completed_run()
        refused = served.call(
            "POST", f"/v1/evals/{run['id']}/scores", 422, json={"request_id": request_id(), "checks": [check]}
        )
        assert problem in json.dumps(refused["detail"]), refused
        [workbench] = [served.call("GET", f"/v1/evals/{run['id']}/workbench", 200)]
    assert workbench["jobs"] == []


def test_a_request_names_each_criterion_once_and_a_check_does_not_need_a_judge_or_a_cost_acknowledgement(records):
    client, served = serve(MemoryStore(), records, with_judge=False)
    with client:
        run = served.completed_run()
        check = {"type": "contains", "options": {"values": ["reset"]}}
        served.call(
            "POST",
            f"/v1/evals/{run['id']}/scores",
            422,
            json={"request_id": request_id(), "checks": [check, check]},
        )
        served.call(
            "POST",
            f"/v1/evals/{run['id']}/scores",
            422,
            json={"request_id": request_id(), "scorers": ["valid_json"], "checks": [{**check, "name": "valid_json"}]},
        )
        served.call("POST", f"/v1/evals/{run['id']}/scores", 422, json={"request_id": request_id()})  # nothing to score
        accepted = served.call(
            "POST", f"/v1/evals/{run['id']}/scores", 202, json={"request_id": request_id(), "checks": [check]}
        )
        served.drain()
        [job] = served.call("GET", f"/v1/evals/{run['id']}/workbench", 200)["jobs"]
    assert accepted["config"]["scorers"] == ["contains"]
    assert accepted["config"]["checks"] == [{"type": "contains", "name": "contains", "options": {"values": ["reset"]}}]
    # The case with no expected text was skipped, so it has no output to check: the job is partial.
    assert (job["state"], job["succeeded"], job["errors"]) == ("partial", 2, 1)


def test_two_jobs_with_other_options_do_not_compare_as_scored_alike(records):
    client, served = serve(MemoryStore(), records)
    with client:
        run = served.completed_run()
        digests = []
        for values in (["reset"], ["username"]):
            body = {"request_id": request_id(), "checks": [{"type": "contains", "options": {"values": values}}]}
            digests.append(
                served.call("POST", f"/v1/evals/{run['id']}/scores", 202, json=body)["config"]["definition_digest"]
            )
            served.drain()
    assert digests[0] != digests[1]


def test_a_suite_keeps_checks_with_their_options_and_a_job_applies_the_saved_version(records):
    client, served = served_with_saved_outputs(records)
    with client:
        run = served.launch(dataset_name="options")
        served.drain()
        run = served.call("GET", f"/v1/evals/{run['id']}", 200)
        checks = [
            {"type": "contains", "name": "names_refund", "options": {"values": ["refund"]}},
            {"type": "length", "options": {"max_length": 80}},
        ]
        body = {"request_id": request_id(), "name": "Checks only", "scorers": [], "checks": checks}
        saved = served.call("POST", "/v1/evals/suites", 201, json=body)
        # `scorers` still names every scorer of the suite, as a version 3 client reads it; a version 3
        # client that revises the suite names a check there, which it cannot send, so it is refused
        # rather than saving a version without the checks.
        assert saved["definition"]["scorers"] == ["length", "names_refund"] and saved["version"] == 1
        # The checks sit in the definition as the request named them, labels filled in, by name.
        assert saved["definition"]["checks"] == [
            {"type": "length", "name": "length", "options": {"max_length": 80}},
            {"type": "contains", "name": "names_refund", "options": {"values": ["refund"]}},
        ]
        assert served.call("POST", "/v1/evals/suites", 201, json={**body, "request_id": body["request_id"]}) == saved
        listed = served.call("GET", "/v1/evals/suites", 200)["items"][0]
        assert listed["definition"]["checks"] == saved["definition"]["checks"]
        # A suite of built-in scorers has no `checks` key, as a version 3 suite did.
        plain = served.call(
            "POST",
            "/v1/evals/suites",
            201,
            json={"request_id": request_id(), "name": "Plain", "scorers": ["valid_json"]},
        )
        assert "checks" not in plain["definition"]

        # A scoring request with the suite names its checks too, as it names its scorers, or is refused.
        pinned = {"suite_version_id": saved["id"], "scorers": [], "checks": saved["definition"]["checks"]}
        served.call(
            "POST",
            f"/v1/evals/{run['id']}/scores",
            422,
            json={"request_id": request_id(), "suite_version_id": saved["id"], "scorers": [], "checks": checks[:1]},
        )
        found = scored(served, run, **pinned)
        [job] = served.call("GET", f"/v1/evals/{run['id']}/workbench", 200)["jobs"]
    assert job["config"]["suite_version"] == 1 and job["state"] == "completed"
    assert found[("text", "names_refund")][0] is True and found[("json", "names_refund")][0] is False
    assert found[("text", "length")][0] is False and found[("json", "length")][0] is False


def test_a_suite_of_checks_alone_is_a_suite_and_one_of_nothing_is_not(records):
    client, served = serve(MemoryStore(), records)
    with client:
        served.call("POST", "/v1/evals/suites", 422, json={"request_id": request_id(), "name": "Empty", "scorers": []})
        served.call(
            "POST",
            "/v1/evals/suites",
            422,
            json={
                "request_id": request_id(),
                "name": "Twice",
                "scorers": ["valid_json"],
                "checks": [{"type": "contains", "name": "valid_json", "options": {"values": ["a"]}}],
            },
        )
