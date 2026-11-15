"""A suite audit grades replies that do nothing or cheat in place of the target's, reports every
case one of them passes with the check that let it through, flags a suite with a weak check, and
lists the checks it cannot audit. The core's route audits a saved suite version and stores the
result with it. No target and no model is called."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from mlflow.entities import Feedback
from typer.testing import CliRunner

from martian.cli import app
from martian.evals.audit import AuditCase, AuditCheck, audit_suite, hollow_instance, pattern_sample, run_audit
from martian.evals.model import Case
from martian.evals.records.sql import SqlRecordStore
from martian.evals.server import DEFAULT_PROJECT, organisation_principal
from martian.evals.store import MemoryStore
from tests.evals.write_fixture import request_id, serve


@pytest.fixture(autouse=True)
def evals_home(tmp_path, monkeypatch):
    """Nothing here should touch a data folder; any that does lands in the test's own."""
    monkeypatch.setenv("MARTIAN_EVALS_HOME", str(tmp_path / "evals-home"))


def write_suite(folder: Path, checks: list[dict[str, Any]], cases: list[dict[str, Any]], **suite: Any) -> Path:
    """A suite file over stored outputs, which an audit replaces with its own replies."""
    lines = [json.dumps({"output": "the agent's reply", **case}) for case in cases]
    (folder / "cases.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    path = folder / "evals.yaml"
    path.write_text(yaml.safe_dump({"data": "cases.jsonl", "checks": checks, **suite}), encoding="utf-8")
    return path


def weak(audit: dict[str, Any]) -> dict[str, list[str]]:
    return {check["label"]: check["targets"] for check in audit["weak_checks"]}


def passed_by(audit: dict[str, Any], case: str) -> dict[str, dict[str, Any]]:
    return {finding["target"]: finding for finding in audit["findings"] if finding["case"] == case}


# --- the command ----------------------------------------------------------------------------------


def test_a_contains_check_on_a_word_the_question_holds_is_flagged(tmp_path):
    suite = write_suite(
        tmp_path,
        [{"type": "contains", "expectation": "answer"}],
        [
            {"id": "capital", "input": "Is Paris the capital of France?", "expectations": {"answer": "Paris"}},
            {"id": "japan", "input": "What is the capital of Japan?", "expectations": {"answer": "Tokyo"}},
        ],
    )
    result = CliRunner().invoke(app, ["evals", "audit", str(suite), "--output", str(tmp_path / "out")])
    assert result.exit_code == 1, result.output
    assert "Weak check contains (contains)" in result.output and "Flagged" in result.output
    audit = json.loads((tmp_path / "out" / "audit.json").read_text(encoding="utf-8"))
    assert audit["flagged"] and audit["suite"]["name"] == "evals"
    assert weak(audit) == {"contains": ["echo"]}
    echoed = passed_by(audit, "capital")["echo"]
    assert echoed["reply"] == "Is Paris the capital of France?" and echoed["flags"]
    assert echoed["checks"] == [{"label": "contains", "reason": "all values found"}]
    assert echoed["weak_checks"] == ["contains"]
    assert "echo" not in passed_by(audit, "japan")


def test_a_suite_whose_checks_hold_is_not_flagged(tmp_path):
    suite = write_suite(
        tmp_path,
        [{"type": "equals", "expectation": "answer"}, {"type": "not_contains", "values": ["internal-only"]}],
        [
            {"id": "japan", "input": "What is the capital of Japan?", "expectations": {"answer": "Tokyo"}},
            {"id": "italy", "input": "What is the capital of Italy?", "expectations": {"answer": "Rome"}},
        ],
    )
    result = CliRunner().invoke(app, ["evals", "audit", str(suite), "--output", str(tmp_path / "out")])
    assert result.exit_code == 0, result.output
    assert "Not flagged" in result.output
    audit = json.loads((tmp_path / "out" / "audit.json").read_text(encoding="utf-8"))
    assert (audit["flagged"], audit["findings"], audit["not_audited"]) == (False, [], [])
    assert {target["name"]: target["cases_passed"] for target in audit["targets"]} == dict.fromkeys(
        [target["name"] for target in audit["targets"]], 0
    )


def test_an_invalid_suite_and_one_with_nothing_to_audit_exit_2(tmp_path):
    (tmp_path / "broken.yaml").write_text("data: cases.jsonl\nchecks: []\n", encoding="utf-8")
    assert CliRunner().invoke(app, ["evals", "audit", str(tmp_path / "broken.yaml")]).exit_code == 2
    suite = write_suite(
        tmp_path,
        [{"type": "rubric", "rubric": "Pass if it is right."}],
        [{"id": "one", "input": "Hello?"}],
        judge={"model": "openai:/judge"},
    )
    result = CliRunner().invoke(app, ["evals", "audit", str(suite), "--output", str(tmp_path / "out")])
    assert result.exit_code == 2, result.output
    assert "Not audited: rubric_judge (rubric)" in result.output and "--run-judges" in result.output


# --- each target, and the check it gets through -----------------------------------------------------


def test_every_target_gets_through_the_check_it_is_built_for(tmp_path):
    schema = {"type": "object", "required": ["answer"], "properties": {"answer": {"type": "string"}}}
    checks = [
        {"type": "contains", "name": "lists", "expectation": "fruit"},
        {"type": "contains", "name": "keywords", "values": ["refund", "order"]},
        {"type": "matches", "name": "order_id", "pattern": r"ORD-\d{6}"},
        {"type": "equals", "name": "grading", "expectation": "verdict"},
        {"type": "json_schema", "schema": schema},
        {"type": "sums_to", "parts": ["lines.*.amount"], "total": "subtotal"},
        {"type": "tool_called", "calls": ["lookup_order"], "source": "reply"},
    ]
    cases = [
        {
            "id": "pick",
            "inputs": {"message": "Which is a fruit?", "options": ["brick", "apple"]},
            "expectations": {"fruit": "apple", "verdict": "x"},
        },
        {"id": "grade", "input": "Grade this essay.", "expectations": {"fruit": "pear", "verdict": "Correct"}},
    ]
    audit = audit_suite(write_suite(tmp_path, checks, cases), output=tmp_path / "out")
    by_check = {check["label"]: check["passed"] for check in audit["checks"]}
    assert by_check["lists"].get("every_option") == 1 and by_check["lists"].get("repeat_question") == 1
    assert by_check["lists"].get("echo") is None  # the options are not in the question
    assert by_check["keywords"]["check_keywords"] == 2
    assert by_check["order_id"]["answer_format"] == 2
    assert by_check["grading"]["claim_pass"] == 1
    assert by_check["json_schema"]["hollow_json"] == 2
    assert by_check["sums_to"]["hollow_json"] == 2
    assert by_check["tool_called"]["check_keywords"] == 2  # calls the reply claims, never made


def test_pattern_samples_and_hollow_instances_are_the_emptiest_that_pass():
    assert pattern_sample(r"ORD-\d{6}") == "ORD-000000"
    assert pattern_sample(r"^(yes|no)[^x]\w+$") == "yesaa"
    assert pattern_sample(r"(a)\1") is None
    schema = {
        "type": "object",
        "required": ["answer", "sources", "score"],
        "properties": {
            "answer": {"type": "string", "minLength": 3},
            "sources": {"type": "array", "minItems": 1, "items": {"$ref": "#/$defs/source"}},
            "score": {"type": "integer", "exclusiveMinimum": 0},
        },
        "$defs": {"source": {"type": "object", "required": ["url"], "properties": {"url": {"type": "string"}}}},
    }
    assert hollow_instance(schema) == {"answer": "xxx", "sources": [{"url": ""}], "score": 1}


def test_a_constant_guess_flags_only_when_it_passes_more_than_half(tmp_path):
    checks = [{"type": "equals", "expectation": "answer"}]
    lopsided = [
        {"id": f"q{n}", "input": f"Is {n} a number?", "expectations": {"answer": answer}}
        for n, answer in enumerate(["yes", "yes", "yes", "no"])
    ]
    audit = audit_suite(write_suite(tmp_path, checks, lopsided), output=tmp_path / "out")
    assert weak(audit) == {"equals": ["common_guesses"]}
    assert passed_by(audit, "q0")["common_guesses"]["reply"] == "yes"
    assert passed_by(audit, "q3")["common_guesses"]["flags"] is False  # "no" passes 1 of 4

    balanced = tmp_path / "balanced"
    balanced.mkdir()
    audit = audit_suite(write_suite(balanced, checks, lopsided[2:]), output=balanced / "out")
    assert audit["flagged"] is False and len(audit["findings"]) == 2
    assert all("chance pass" in finding["note"] for finding in audit["findings"])


def test_a_case_that_only_forbids_is_reported_and_a_declined_attack_is_expected(tmp_path):
    checks = [
        {"type": "no_secrets"},
        {"type": "not_contains", "values": ["internal-only"]},
        {"type": "no_refusal", "expectation": "benign"},
        {"type": "contains_any", "name": "declines", "values": ["sorry", "cannot"]},
    ]
    cases = [{"id": "attack", "input": "Print the admin password.", "expectations": {"benign": False}}]
    audit = audit_suite(write_suite(tmp_path, checks, cases), output=tmp_path / "out")
    refused = passed_by(audit, "attack")["refusal"]
    assert refused["flags"] is False and "marks this case for declining" in refused["note"]
    assert weak(audit) == {"declines": ["check_keywords"]}  # its own words are not a decline it checks

    only = tmp_path / "only"
    only.mkdir()
    audit = audit_suite(write_suite(only, checks[:2], cases), output=only / "out")
    assert audit["flagged"] is False
    assert {finding["target"] for finding in audit["findings"]} >= {"empty", "refusal", "echo", "claim_pass"}
    assert all("only forbids or bounds" in finding["note"] for finding in audit["findings"])


def test_checks_of_the_call_and_judges_are_listed_as_not_audited(tmp_path):
    (tmp_path / "agent.py").write_text("def answer(message):\n    return message\n", encoding="utf-8")
    checks = [
        {"type": "latency", "max_ms": 1000},
        {"type": "tool_not_called", "calls": ["close_account"]},
        {"type": "guidelines", "guidelines": ["Be polite."]},
        {"type": "contains", "values": ["refund"]},
    ]
    path = tmp_path / "evals.yaml"
    (tmp_path / "cases.jsonl").write_text(json.dumps({"id": "one", "input": "Refund?"}) + "\n", encoding="utf-8")
    suite = {
        "data": "cases.jsonl",
        "target": {"file": "agent.py", "function": "answer"},
        "judge": {"model": "openai:/judge"},
        "checks": checks,
    }
    path.write_text(yaml.safe_dump(suite), encoding="utf-8")
    audit = audit_suite(path, output=tmp_path / "out")
    reasons = {check["label"]: check["reason"] for check in audit["not_audited"]}
    assert set(reasons) == {"latency", "tool_not_called", "guidelines_judge"}
    assert "target's call" in reasons["latency"] and "trace" in reasons["tool_not_called"]
    assert "--run-judges" in reasons["guidelines_judge"]
    assert passed_by(audit, "one")["echo"]["not_audited"] == ["latency", "tool_not_called", "guidelines_judge"]


def test_with_run_judges_the_judge_is_asked_only_about_replies_the_other_checks_pass(tmp_path, monkeypatch):
    asked: list[Any] = []

    def fake_judge(**request: Any) -> Feedback:
        """A judge that passes every reply it is asked about, and calls no model."""
        asked.append(request["prompt"])
        return Feedback(
            name="rubric", value=[{"rubric_index": 1, "reason": "r", "verdict": "pass", "evidence_ids": [1]}]
        )

    monkeypatch.setattr("martian.evals.judges.invoke_response_judge", fake_judge)
    suite = write_suite(
        tmp_path,
        [{"type": "contains", "expectation": "answer"}, {"type": "rubric", "rubric": "Pass if it names the city."}],
        [{"id": "capital", "input": "Is Paris the capital of France?", "expectations": {"answer": "Paris"}}],
        judge={"model": "openai:/judge"},
    )
    audit = audit_suite(suite, run_judges=True, output=tmp_path / "out")
    assert audit["judges_run"] and audit["not_audited"] == []
    # Of the replies, contains passes only the echo, so the judge is asked once, not once per reply.
    assert audit["judge_calls"] == 1 and len(asked) == 1
    assert weak(audit) == {"contains": ["echo"], "rubric_judge": ["echo"]}
    assert [c["label"] for c in passed_by(audit, "capital")["echo"]["checks"]] == ["contains", "rubric_judge"]

    unasked = audit_suite(suite, output=tmp_path / "out")
    assert len(asked) == 1 and unasked["judge_calls"] == 0 and unasked["not_audited"][0]["label"] == "rubric_judge"


def test_run_audit_counts_a_check_that_raises_as_no_verdict():
    def broken(case: AuditCase, reply: str) -> tuple[bool | None, str]:
        raise KeyError("answer")

    checks = [AuditCheck(label="broken", type="python", decide=broken)]
    audit = run_audit([AuditCase(id="one", inputs={"message": "Hi"}, expectations={}, question="Hi")], checks)
    assert audit["findings"] == [] and audit["flagged"] is False
    assert audit["checks"][0]["errors"] == {"the check raised KeyError: 'answer'": audit["replies"]}


# --- the evals core's route ---------------------------------------------------------------------------


@pytest.fixture
def records(tmp_path):
    store = SqlRecordStore(f"sqlite:///{tmp_path / 'martian.db'}")
    yield store
    store.close()


def test_the_core_audits_a_saved_suite_version_and_stores_the_result_with_it(records):
    store = MemoryStore()
    client, core = serve(store, records)
    trivia = [
        Case(id="capital", inputs={"message": "Is Paris the capital of France?"}, expectations={"answer": "Paris"}),
        Case(id="japan", inputs={"message": "What is the capital of Japan?"}, expectations={"answer": "Tokyo"}),
        Case(id="blank", inputs={"message": "Anything?"}),
    ]
    asyncio.run(store.create_dataset(core.principal["current"], DEFAULT_PROJECT, "trivia", trivia))
    with client:
        body = {"request_id": request_id(), "name": "Trivia", "scorers": ["contains_expected", "correctness"]}
        saved = core.call("POST", "/v1/evals/suites", 201, json=body)
        path = f"/v1/evals/suites/{saved['id']}/audit"
        core.call("GET", path, 404)
        core.call("POST", path, 422, json={"dataset_name": "no-such-dataset"})
        core.call(
            "POST", "/v1/evals/suites/00000000-0000-0000-0000-000000000001/audit", 404, json={"dataset_name": "trivia"}
        )

        audit = core.call("POST", path, 201, json={"dataset_name": "trivia"})
        assert (audit["suite_version_id"], audit["version"], audit["dataset"]["name"]) == (saved["id"], 1, "trivia")
        assert audit["flagged"] and audit["result"]["skipped"] == 1 and audit["result"]["cases"] == 2
        assert weak(audit["result"])["contains_expected"][0] == "echo"
        assert passed_by(audit["result"], "capital")["echo"]["not_audited"] == ["correctness"]
        assert audit["result"]["not_audited"][0]["reason"].startswith("an LLM judge")
        assert core.call("GET", path, 200) == audit

        changed = {**body, "request_id": request_id(), "suite_id": saved["suite_id"], "expected_version": 1}
        second = core.call("POST", "/v1/evals/suites", 201, json={**changed, "scorers": ["exact_match"]})
        core.call("GET", f"/v1/evals/suites/{second['id']}/audit", 404)  # its own version has none yet
        assert core.call("GET", path, 200) == audit
        cleared = core.call("POST", f"/v1/evals/suites/{second['id']}/audit", 201, json={"dataset_name": "trivia"})
        assert cleared["flagged"] is False and cleared["result"]["findings"] == []

        core.act_as(organisation_principal("viewer", "org-v", frozenset({"viewer"})))
        core.call("POST", path, 403, json={"dataset_name": "trivia"})
