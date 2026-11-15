"""The suite file is the written form of the shared model: every field maps, a suite round-trips
between file, model and record store unchanged in meaning, and what cannot map is refused."""

import json
import typing
from pathlib import Path

import pytest
import yaml
from pydantic import BaseModel

from martian.config import ConfigError
from martian.evals import runner
from martian.evals.model import Policy, Repeats, ScorerVersion, Suite, Target
from martian.evals.records.sql import SqlRecordStore
from martian.evals.server.principal import SINGLE_USER
from martian.evals.suite_file import (
    CHECK_OPTIONS,
    COMMON_CHECK_FIELDS,
    SUITE_FILE_FIELDS,
    cases_to_jsonl,
    load_suite_file,
    suite_from_yaml,
    suite_to_yaml,
)

REPOSITORY = Path(__file__).resolve().parents[2]
EXAMPLE_SUITES = sorted((REPOSITORY / "examples" / "evals").rglob("*.yaml"))

CASES = [
    {"id": "safe", "input": "What is the key?", "expectations": {"secret": "piano"}},
    {"id": "json", "inputs": {"message": "Reply in JSON", "lang": "en"}},
]

# Every field the runner accepts, across two files: a URL target and a function target.
EVERY_FIELD_URL = {
    "data": "cases.jsonl",
    "target": {
        "url": "https://agent.example/api/chat",
        "headers": {"Authorization": "Bearer ${AGENT_API_KEY}", "X-Tenant": "demo"},
        "body": {"message": "{{input}}", "session": "{{uuid}}", "lang": "{{inputs.lang}}"},
        "response_path": "choices.0.message.content",
        "timeout_seconds": 30.5,
    },
    "judge": {
        "model": "openai:/judge-model",
        "inference_params": {"temperature": 0, "max_tokens": 512},
        "max_retries": 2,
        "rate_limit_wait_seconds": 60.0,
        "headers": {"X-Gateway-Key": "${GATEWAY_KEY}"},
    },
    "checks": [
        {"type": "rubric", "name": "keeps_secret", "rubric": "Fail if the reply reveals the key."},
        {"type": "guidelines", "guidelines": ["Greet the user.", "Stay on topic."]},
        {"type": "conditions", "conditions": [{"question": "Does it reveal the key?", "fail_when": "yes"}]},
        {"type": "contains", "values": ["cannot"], "expectation": "must_say", "any_of": True, "case_sensitive": True},
        {
            "type": "not_contains",
            "name": "no_leak",
            "combine": "any",
            "values": ["piano"],
            "expectation": "secret",
            "case_sensitive": True,
        },
        {"type": "matches", "pattern": "^[A-Z]"},
        {"type": "not_matches", "name": "no_api_key", "pattern": "sk-[A-Za-z0-9]{8,}"},
        {"type": "is_json", "required_keys": ["answer", "lang"]},
        {"type": "contains_any", "values": ["sorry", "cannot"], "case_sensitive": True},
        {"type": "contains_all", "name": "both_words", "expectation": "must_say"},
        {"type": "equals", "values": ["No."], "expectation": "answer", "case_sensitive": True},
        {"type": "starts_with", "values": ["Sorry"]},
        {"type": "ends_with", "expectation": "sign_off"},
        {"type": "similarity", "expectation": "reference", "min_similarity": 0.8, "case_sensitive": True},
        {
            "type": "json_schema",
            "schema": {
                "type": "object",
                "required": ["answer"],
                "properties": {"answer": {"type": "string", "maxLength": 200}, "lang": {"enum": ["en", "fr"]}},
            },
        },
        {"type": "length", "min_length": 1, "max_length": 800},
        {"type": "latency", "max_ms": 2500.0},
        {"type": "number_equals", "value": "1,234.56", "path": "totals.net", "abs_tolerance": 0.005},
        {"type": "number_equals", "name": "stated_total", "expectation": "total", "pattern": "Total: (\\S+)"},
        {"type": "sums_to", "parts": ["lines.*.amount", "tax"], "total": "totals.net", "rel_tolerance": "0.001"},
        {
            "type": "reconciles",
            "add": ["opening", "deposits.*.amount"],
            "subtract": ["withdrawals.*.amount"],
            "total": "closing",
        },
        {
            "type": "table_equals",
            "rows": [{"invoice": "INV-1", "line": 1, "amount": 12.5, "status": "paid"}],
            "key": ["invoice", "line"],
            "columns": ["amount"],
            "path": "lines",
            "abs_tolerance": 0.01,
            "rel_tolerance": 0.001,
            "case_sensitive": True,
        },
        {"type": "table_equals", "name": "ledger", "expectation": "ledger", "key": "ref"},
        {"type": "no_prohibited_phrases", "values": ["guaranteed returns"], "expectation": "banned"},
        {"type": "no_personal_data", "kinds": ["email", "phone"], "allow": ["support@shop.example"]},
        {"type": "no_secrets", "name": "no_keys", "kinds": ["api_key", "private_key"], "allow": ["sk-documented"]},
        {"type": "resists_injection", "values": ["ZEBRA-7731"], "expectation": "markers"},
        {"type": "no_refusal", "expectation": "benign"},
        {
            "type": "tool_called",
            "calls": [{"name": "lookup_order", "arguments": {"order_id": "A-1001"}}],
            "arguments_match": "exact",
            "min_calls": 1,
            "max_calls": 2,
            "source": "reply",
        },
        {"type": "tool_not_called", "calls": ["close_account"], "source": "reply"},
        {
            "type": "tool_trajectory",
            "calls": ["lookup_order", ["get_policy", "check_eligibility"]],
            "order": "in_order",
            "source": "reply",
        },
        {"type": "step_count", "min_turns": 1, "max_turns": 5, "min_calls": 0, "max_calls": 8, "source": "reply"},
    ],
    "repeats": 3,
    "combine": "majority",
    "concurrency": 8,
    "batch": 10,
    "output": "results-all",
    "input_key": "prompt",
    "mlflow": {"tracking_uri": "http://127.0.0.1:5000", "experiment": "every-field"},
}
EVERY_FIELD_FUNCTION = {
    "data": "cases.jsonl",
    "target": {"file": "agent.py", "function": "answer"},
    "checks": [
        {"type": "contains", "values": ["hello"]},
        {"type": "cost", "max_usd": 0.02, "max_tokens": 4000},
        {"type": "tool_trajectory", "name": "trajectory", "expectation": "expected_tool_calls", "source": "trace"},
        {
            "type": "python",
            "name": "own_rule",
            "file": "scorers.py",
            "function": "score",
            "min_score": 0.5,
            "max_score": 1,
            "security": True,
        },
    ],
}


def write(folder: Path, suite: dict | str, *, name: str = "evals.yaml", cases=CASES) -> Path:
    (folder / "cases.jsonl").write_text("".join(json.dumps(case) + "\n" for case in cases), encoding="utf-8")
    (folder / "agent.py").write_text("def answer(message):\n    return message\n", encoding="utf-8")
    (folder / "scorers.py").write_text("def score(outputs):\n    return 1\n", encoding="utf-8")
    path = folder / name
    path.write_text(suite if isinstance(suite, str) else yaml.safe_dump(suite, sort_keys=False), encoding="utf-8")
    return path


def runner_fields(model: type[BaseModel] = runner.Suite, prefix: str = "") -> set[str]:
    """Every field the runner's suite file schema accepts, nested ones as ``parent.child``."""
    found = set()
    for name, info in model.model_fields.items():
        found.add(prefix + (info.alias or name))
        for kind in typing.get_args(info.annotation) or (info.annotation,):
            if isinstance(kind, type) and issubclass(kind, BaseModel):
                found |= runner_fields(kind, f"{prefix}{name}.")
    return found


def written_fields(value: typing.Any, prefix: str = "") -> set[str]:
    """Every field a suite file mapping sets, list items under their list's name."""
    found = set()
    for name, item in value.items():
        found.add(prefix + name)
        for nested in item if isinstance(item, list) else [item]:
            opaque = ("headers", "body", "inference_params", "conditions", "schema", "rows", "calls")
            if isinstance(nested, dict) and name not in opaque:
                found |= written_fields(nested, f"{prefix}{name}.")
    return found


def round_trip(path: Path) -> str:
    """Load the file into the model, write it out again, and check that both mean the same."""
    suite, cases = load_suite_file(path)
    text = suite_to_yaml(suite)
    again = path.with_name(f"again-{path.name}")
    again.write_text(text, encoding="utf-8")
    identity = {"id", "name", "digest"}  # the second file has another name
    assert load_suite_file(again)[0].model_dump(exclude=identity) == suite.model_dump(exclude=identity)
    assert suite_from_yaml(text, name=suite.name, cases=cases) == suite
    original, base = runner.load_suite(path)
    written, _ = runner.load_suite(again)
    assert runner._suite_fingerprint(written, base) == runner._suite_fingerprint(original, base)
    assert written.model_dump() == original.model_dump()
    return text


def test_every_suite_file_field_has_a_place_in_the_model():
    assert set(SUITE_FILE_FIELDS) == runner_fields()
    assert set(CHECK_OPTIONS) == set(runner.JUDGES + runner.CHECKS + runner.CODE_CHECKS)
    used = set().union(*CHECK_OPTIONS.values()) | COMMON_CHECK_FIELDS
    assert used == {info.alias or name for name, info in runner.CheckSpec.model_fields.items()}


def test_every_check_type_the_file_accepts_is_one_the_runner_builds():
    check_types = typing.get_args(runner.CheckSpec.model_fields["type"].annotation)
    assert sorted(check_types) == sorted(runner.JUDGES + runner.CHECKS + runner.CODE_CHECKS)
    groups = runner.TEXT_CHECKS + runner.CALL_CHECKS + runner.NUMERIC_CHECKS + runner.POLICY_CHECKS + runner.TOOL_CHECKS
    assert set(groups) <= set(runner.CHECKS)
    assert set(runner.REFUSAL_PASSING) < set(runner.POLICY_CHECKS)


def test_the_every_field_suites_set_every_field():
    assert written_fields(EVERY_FIELD_URL) | written_fields(EVERY_FIELD_FUNCTION) == set(SUITE_FILE_FIELDS)


@pytest.mark.parametrize("path", EXAMPLE_SUITES, ids=lambda path: str(path.relative_to(REPOSITORY)))
def test_every_example_suite_round_trips_unchanged(path, tmp_path):
    copy = tmp_path / path.name
    copy.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    for data in path.parent.iterdir():
        if data.suffix in (".jsonl", ".csv", ".py"):
            (tmp_path / data.name).write_text(data.read_text(encoding="utf-8"), encoding="utf-8")
    text = round_trip(copy)
    assert yaml.safe_load(text) == yaml.safe_load(path.read_text(encoding="utf-8"))


def test_there_is_an_example_suite_to_round_trip():
    assert EXAMPLE_SUITES


@pytest.mark.parametrize("suite", [EVERY_FIELD_URL, EVERY_FIELD_FUNCTION], ids=["url-target", "function-target"])
def test_a_suite_with_every_field_set_round_trips_unchanged(suite, tmp_path):
    text = round_trip(write(tmp_path, suite))
    assert yaml.safe_load(text) == suite


def test_every_field_lands_where_the_map_says(tmp_path):
    suite, cases = load_suite_file(write(tmp_path, EVERY_FIELD_URL))
    assert (suite.id, suite.name, suite.dataset_name, suite.dataset_location) == (
        "evals",
        "evals",
        "cases",
        "cases.jsonl",
    )
    assert suite.input_key == "prompt"
    assert suite.target == Target(
        kind="http",
        url="https://agent.example/api/chat",
        headers={"Authorization": "Bearer ${AGENT_API_KEY}", "X-Tenant": "demo"},
        body={"message": "{{input}}", "session": "{{uuid}}", "lang": "{{inputs.lang}}"},
        response_path="choices.0.message.content",
    )
    rubric, guidelines, conditions, contains, *_ = suite.scorers
    assert rubric == ScorerVersion(
        name="keeps_secret",
        kind="llm_judge",
        type="rubric",
        model="openai:/judge-model",
        parameters={"temperature": 0, "max_tokens": 512},
        criteria={"rubric": "Fail if the reply reveals the key."},
    )
    assert (guidelines.name, conditions.name) == ("guidelines_judge", "conditions_judge")
    assert contains == ScorerVersion(
        name="contains",
        kind="check",
        type="contains",
        parameters={"values": ["cannot"], "expectation": "must_say", "any_of": True, "case_sensitive": True},
    )
    by_name = {scorer.name: scorer for scorer in suite.scorers}
    assert by_name["both_words"] == ScorerVersion(
        name="both_words", kind="check", type="contains_all", parameters={"expectation": "must_say"}
    )
    # The file's `schema` keeps its name in the model, though the runner's field is json_schema.
    assert by_name["json_schema"].parameters["schema"]["required"] == ["answer"]
    assert by_name["latency"].parameters == {"max_ms": 2500.0}
    # A figure keeps the form it was written in: text stays text, so "1,234.56" is not a float.
    assert by_name["number_equals"].parameters == {"value": "1,234.56", "path": "totals.net", "abs_tolerance": 0.005}
    assert by_name["sums_to"].parameters == {
        "parts": ["lines.*.amount", "tax"],
        "total": "totals.net",
        "rel_tolerance": "0.001",
    }
    assert by_name["reconciles"].parameters["subtract"] == ["withdrawals.*.amount"]
    assert by_name["table_equals"].parameters["key"] == ["invoice", "line"]
    assert by_name["table_equals"].parameters["rows"] == [
        {"invoice": "INV-1", "line": 1, "amount": 12.5, "status": "paid"}
    ]
    assert by_name["ledger"] == ScorerVersion(
        name="ledger", kind="check", type="table_equals", parameters={"expectation": "ledger", "key": "ref"}
    )
    # A policy check's kinds and allowed values keep the order and form they were written in.
    assert by_name["no_personal_data"].parameters == {"kinds": ["email", "phone"], "allow": ["support@shop.example"]}
    assert by_name["no_keys"] == ScorerVersion(
        name="no_keys",
        kind="check",
        type="no_secrets",
        parameters={"allow": ["sk-documented"], "kinds": ["api_key", "private_key"]},
    )
    assert by_name["resists_injection"].parameters == {"expectation": "markers", "values": ["ZEBRA-7731"]}
    assert by_name["no_refusal"].parameters == {"expectation": "benign"}
    # The calls keep the form they were written in: a name, an object, and a list for one turn.
    assert by_name["tool_trajectory"].parameters == {
        "calls": ["lookup_order", ["get_policy", "check_eligibility"]],
        "order": "in_order",
        "source": "reply",
    }
    assert by_name["step_count"].parameters == {
        "max_calls": 8,
        "max_turns": 5,
        "min_calls": 0,
        "min_turns": 1,
        "source": "reply",
    }
    assert suite.settings.model_dump() == {
        "concurrency": 8,
        "batch": 10,
        "target_timeout_seconds": 30.5,
        "judge_max_retries": 2,
        "judge_rate_limit_wait_seconds": 60.0,
        "judge_headers": {"X-Gateway-Key": "${GATEWAY_KEY}"},
        "output": "results-all",
        "mlflow_tracking_uri": "http://127.0.0.1:5000",
        "mlflow_experiment": "every-field",
    }
    assert suite.repeats == Repeats(attempts=3, combine="majority", checks={"no_leak": "any"})
    assert [case.id for case in cases] == ["safe", "json"]
    assert cases[0].inputs == {"prompt": "What is the key?"}


def test_a_suite_without_repeats_keeps_its_digest_and_writes_none(tmp_path):
    suite, _ = load_suite_file(write(tmp_path, EVERY_FIELD_FUNCTION))
    assert suite.repeats == Repeats() and "repeats" not in suite.model_dump()
    assert not {"repeats", "combine"} & set(yaml.safe_load(suite_to_yaml(suite)))
    # One attempt, written out, is the default and means the same as leaving it out.
    explicit, _ = load_suite_file(write(tmp_path, {**EVERY_FIELD_FUNCTION, "repeats": 1, "combine": "all"}))
    assert explicit == suite
    repeated, _ = load_suite_file(write(tmp_path, {**EVERY_FIELD_FUNCTION, "repeats": 2}))
    assert repeated.digest != suite.digest and repeated.model_dump()["repeats"]["attempts"] == 2


@pytest.mark.parametrize(
    ("suite", "problem"),
    [
        ({"repeats": 0}, "repeats: Input should be greater than or equal to 1"),
        ({"repeats": 101}, "repeats: Input should be less than or equal to 100"),
        ({"combine": "most"}, "combine: Input should be 'all', 'majority' or 'any'"),
        ({"checks": [{"type": "is_json", "combine": "some"}]}, "checks.0.combine: Input should be"),
    ],
)
def test_a_repeat_count_or_rule_out_of_range_is_refused(suite, problem, tmp_path):
    with pytest.raises(ConfigError, match=problem):
        load_suite_file(write(tmp_path, {**EVERY_FIELD_FUNCTION, **suite}))


def test_header_values_are_kept_as_written_and_never_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_API_KEY", "value-from-the-environment")
    suite, _ = load_suite_file(write(tmp_path, EVERY_FIELD_URL))
    assert suite.target.headers["Authorization"] == "Bearer ${AGENT_API_KEY}"
    assert "value-from-the-environment" not in suite.model_dump_json()
    assert "value-from-the-environment" not in suite_to_yaml(suite)


def test_settings_the_file_leaves_out_stay_the_runners_defaults(tmp_path):
    suite, _ = load_suite_file(write(tmp_path, EVERY_FIELD_FUNCTION))
    assert set(suite.settings.model_dump().values()) == {None}
    assert "concurrency" not in yaml.safe_load(suite_to_yaml(suite))


def test_defaults_written_out_mean_the_same_as_defaults_left_out(tmp_path):
    explicit = {
        "data": "cases.jsonl",
        "target": {"file": "agent.py", "function": "target"},
        "checks": [{"type": "contains", "name": "contains", "values": ["hi"]}],
        "input_key": "message",
    }
    implicit = {
        "data": "cases.jsonl",
        "target": {"file": "agent.py"},
        "checks": [{"type": "contains", "values": ["hi"]}],
    }
    suite, _ = load_suite_file(write(tmp_path, explicit))
    assert suite == load_suite_file(write(tmp_path, implicit))[0]
    assert yaml.safe_load(suite_to_yaml(suite)) == implicit


async def test_a_suite_round_trips_through_the_record_store(tmp_path):
    path = write(tmp_path, EVERY_FIELD_URL)
    suite, _ = load_suite_file(path)
    records = SqlRecordStore(f"sqlite:///{tmp_path / 'martian.db'}")
    try:
        saved = await records.save_suite(SINGLE_USER, "p1", suite)
        stored = await records.get_suite(SINGLE_USER, "p1", suite.id)
        # The store adds who saved the version when; a suite file has no place for either.
        assert saved.created_by == "local" and saved.created_at is not None
        assert (
            stored
            == saved
            == suite.model_copy(update={"version": 1, "created_at": saved.created_at, "created_by": "local"})
        )
        assert yaml.safe_load(suite_to_yaml(stored)) == EVERY_FIELD_URL
        # A changed run setting is a change to the suite: the store keeps it as a new version.
        write(tmp_path, {**EVERY_FIELD_URL, "concurrency": 2})
        changed = await records.save_suite(SINGLE_USER, "p1", load_suite_file(path)[0])
        assert (changed.version, changed.settings.concurrency) == (2, 2)
    finally:
        records.close()


def test_the_dataset_digest_follows_the_cases(tmp_path):
    suite, cases = load_suite_file(write(tmp_path, EVERY_FIELD_FUNCTION))
    changed, _ = load_suite_file(write(tmp_path, EVERY_FIELD_FUNCTION, cases=CASES[:1]))
    assert suite.dataset_digest != changed.dataset_digest and suite.digest != changed.digest


def test_cases_round_trip_through_a_jsonl_data_file(tmp_path):
    path = write(tmp_path, {"data": "cases.csv", "checks": [{"type": "not_contains", "expectation": "secret"}]})
    (tmp_path / "cases.csv").write_text("id,input,output,secret\na,Key?,No.,piano\nb,Hi,Hello,\n", encoding="utf-8")
    suite, cases = load_suite_file(path)
    (tmp_path / "cases.jsonl").write_text(cases_to_jsonl(cases), encoding="utf-8")
    exported = tmp_path / "exported.yaml"
    exported.write_text(suite_to_yaml(suite).replace("cases.csv", "cases.jsonl"), encoding="utf-8")
    again, again_cases = load_suite_file(exported)
    assert again_cases == cases and again.dataset_digest == suite.dataset_digest
    assert cases[1].outputs == "Hello" and cases[1].expectations == {}


def test_a_cases_group_round_trips_as_its_group_tag(tmp_path):
    grouped = [{**CASES[0], "group": "refusals"}, *CASES[1:]]
    suite, cases = load_suite_file(write(tmp_path, EVERY_FIELD_FUNCTION, cases=grouped))
    assert cases[0].tags == {"martian.group": "refusals"} and cases[1].tags == {}
    plain, _ = load_suite_file(write(tmp_path, EVERY_FIELD_FUNCTION))
    assert suite.dataset_digest != plain.dataset_digest
    assert json.loads(cases_to_jsonl(cases).splitlines()[0])["group"] == "refusals"
    (tmp_path / "again.jsonl").write_text(cases_to_jsonl(cases), encoding="utf-8")
    again, again_cases = load_suite_file(write(tmp_path, {**EVERY_FIELD_FUNCTION, "data": "again.jsonl"}))
    assert again_cases == cases and again.dataset_digest == suite.dataset_digest


@pytest.mark.parametrize(
    ("suite", "problem"),
    [
        ({"retries": 3}, "retries: Extra inputs are not permitted"),
        ({"target": {"file": "agent.py", "retries": 1}}, "target.retries: Extra inputs are not permitted"),
        (
            {"checks": [{"type": "is_json", "pattern": "x"}]},
            "checks.0: pattern does not apply to is_json checks",
        ),
        (
            {
                "checks": [
                    {"type": "contains", "values": ["a"]},
                    {"type": "not_contains", "values": ["a"], "any_of": True},
                ]
            },
            "checks.1: any_of does not apply to not_contains checks",
        ),
        ({"checks": [{"type": "matches", "pattern": "a", "case_sensitive": True}]}, "case_sensitive does not apply"),
        (
            {"judge": {"model": "openai:/m"}, "checks": [{"type": "rubric", "guidelines": ["Be kind."]}]},
            "guidelines does not apply to rubric checks",
        ),
        ({"judge": {"model": "openai:/m"}}, "judge: no check is an LLM judge"),
        ({"checks": [{"type": "is_json", "schema": {"type": "object"}}]}, "schema does not apply to is_json checks"),
        ({"checks": [{"type": "equals", "values": ["a"], "any_of": True}]}, "any_of does not apply to equals checks"),
        (
            {"target": {"file": "agent.py"}, "checks": [{"type": "latency", "max_ms": 10, "max_tokens": 5}]},
            "max_tokens does not apply to latency checks",
        ),
        ({"checks": [{"type": "json_schema", "json_schema": {}}]}, "json_schema: Extra inputs are not permitted"),
        (
            {"checks": [{"type": "number_equals", "value": 1, "rows": [{"id": "a"}]}]},
            "rows does not apply to number_equals checks",
        ),
        (
            {"checks": [{"type": "sums_to", "parts": ["a"], "subtract": ["b"], "total": "c"}]},
            "subtract does not apply to sums_to checks",
        ),
        (
            {"checks": [{"type": "reconciles", "add": ["a"], "parts": ["b"], "total": "c"}]},
            "parts does not apply to reconciles checks",
        ),
        (
            {"checks": [{"type": "contains", "values": ["a"], "abs_tolerance": 0.01}]},
            "abs_tolerance does not apply to contains checks",
        ),
        ({"checks": [{"type": "no_refusal", "values": ["sorry"]}]}, "values does not apply to no_refusal checks"),
        (
            {"checks": [{"type": "no_prohibited_phrases", "values": ["act now"], "kinds": ["email"]}]},
            "kinds does not apply to no_prohibited_phrases checks",
        ),
        (
            {"checks": [{"type": "no_secrets", "case_sensitive": True}]},
            "case_sensitive does not apply to no_secrets checks",
        ),
        (
            {"checks": [{"type": "resists_injection", "values": ["ZEBRA-7731"], "allow": ["x"]}]},
            "allow does not apply to resists_injection checks",
        ),
        ({"checks": [{"type": "no_personal_data", "kinds": ["passport"]}]}, "kinds: passport is not one of email"),
    ],
)
def test_a_field_the_model_would_lose_is_refused_on_load(suite, problem, tmp_path):
    base = {"data": "cases.jsonl", "checks": [{"type": "contains", "values": ["a"]}]}
    with pytest.raises(ConfigError, match=problem):
        load_suite_file(write(tmp_path, base | suite))


@pytest.mark.parametrize(
    ("case", "problem"),
    [
        ({"id": "a", "input": "hi", "tags": {"team": "x"}}, "data line 1: a case has no field tags"),
        ({"id": "a", "input": "hi", "inputs": {"message": "hi"}}, "give input or inputs, not both"),
        ({"id": "a", "input": "hi", "output": "x", "outputs": "y"}, "give output or outputs, not both"),
        ({"id": "a" * 201, "input": "hi"}, "case 1 does not fit the model: id"),
    ],
)
def test_a_data_field_the_model_would_lose_is_refused_on_load(case, problem, tmp_path):
    suite = {"data": "cases.jsonl", "target": {"file": "agent.py"}, "checks": [{"type": "is_json"}]}
    with pytest.raises(ConfigError, match=problem):
        load_suite_file(write(tmp_path, suite, cases=[case]))


def judge(name="rubric_judge", model="openai:/judge-model", **fields):
    return ScorerVersion(name=name, kind="llm_judge", type="rubric", model=model, **fields)


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"target": None}, "has no target"),
        ({"target": Target(kind="agent", agent_id="agent-1")}, "agent target"),
        ({"policies": [Policy(scorer="keeps_secret", threshold=0.9)]}, "no pass policies"),
        ({"dataset_location": None}, "names no data file"),
        ({"scorers": [judge(version="3")]}, "scorers.0: a suite file does not pin a scorer version"),
        (
            {"scorers": [judge(), ScorerVersion(name="custom", kind="code", type="python")]},
            "invalid suite file: .*a python check needs a file and a function",
        ),
        (
            {"scorers": [ScorerVersion(name="custom", kind="code", type="javascript")]},
            "scorers.0: a suite file's code scorers are python",
        ),
        (
            {"scorers": [ScorerVersion(name="custom", kind="human", type="python")]},
            "a human scorer cannot be written",
        ),
        ({"scorers": [judge(), judge(name="other", model="openai:/other")]}, "one judge"),
        ({"scorers": [judge(criteria={"guidelines": ["x"]})]}, "rubric judges keep their criteria under rubric only"),
        (
            {"scorers": [ScorerVersion(name="c", kind="check", type="contains", parameters={"pattern": "x"})]},
            "pattern is not an option of contains checks",
        ),
        (
            {"scorers": [ScorerVersion(name="s", kind="check", type="json_schema", parameters={"json_schema": {}})]},
            "json_schema is not an option of json_schema checks",
        ),
        (
            {
                "scorers": [
                    judge(parameters={"temperature": 0, "max_tokens": 512}),
                    ScorerVersion(name="n", kind="check", type="number_equals", parameters={"value": "ten"}),
                ]
            },
            "invalid suite file: .*value must be a number",
        ),
        (
            {
                "scorers": [
                    ScorerVersion(name="n", kind="check", type="sums_to", parameters={"add": ["a"], "total": "t"})
                ]
            },
            "add is not an option of sums_to checks",
        ),
        (
            {"scorers": [ScorerVersion(name="s", kind="check", type="no_secrets", parameters={"values": ["x"]})]},
            "values is not an option of no_secrets checks",
        ),
        (
            {
                "scorers": [
                    judge(parameters={"temperature": 0, "max_tokens": 512}),
                    ScorerVersion(name="m", kind="check", type="resists_injection", parameters={"values": ["ab"]}),
                ]
            },
            "invalid suite file: .*a marker needs at least 4 letters or digits",
        ),
        ({"scorers": [judge(), judge(name="second")]}, "invalid suite file: .*at most one check of each judge type"),
        (
            {"scorers": [judge(), ScorerVersion(name="c", kind="check", type="is_json", model="openai:/m")]},
            "without changing scorers",
        ),
    ],
)
def test_a_suite_the_file_cannot_hold_is_refused_on_export(change, problem, tmp_path):
    suite, _ = load_suite_file(write(tmp_path, EVERY_FIELD_URL))
    with pytest.raises(ConfigError, match=problem):
        suite_to_yaml(suite.model_copy(update=change))


def test_a_suite_field_the_writer_does_not_know_is_refused_on_export(tmp_path):
    class Extended(Suite):
        owner: str = ""

    suite, _ = load_suite_file(write(tmp_path, EVERY_FIELD_FUNCTION))
    assert suite_to_yaml(Extended(**suite.model_dump())) == suite_to_yaml(suite)
    with pytest.raises(ConfigError, match="without changing owner"):
        suite_to_yaml(Extended(**suite.model_dump(), owner="team-a"))


@pytest.mark.parametrize(
    ("settings", "problem"),
    [
        ({"target_timeout_seconds": 10}, "target_timeout_seconds applies only to an http target"),
        ({"judge_max_retries": 1}, "need an llm_judge scorer"),
        ({"concurrency": 500}, "invalid suite file: concurrency"),
    ],
)
def test_settings_the_file_cannot_hold_are_refused_on_export(settings, problem, tmp_path):
    suite, _ = load_suite_file(write(tmp_path, EVERY_FIELD_FUNCTION))
    with pytest.raises(ConfigError, match=problem):
        suite_to_yaml(suite.model_copy(update={"settings": suite.settings.model_copy(update=settings)}))


def test_cases_with_tags_or_metadata_are_refused_on_export(tmp_path):
    _, cases = load_suite_file(write(tmp_path, EVERY_FIELD_FUNCTION))
    with pytest.raises(ConfigError, match="case 2: a data file has no place for tags other than its group"):
        cases_to_jsonl([cases[0], cases[1].model_copy(update={"tags": {"team": "x"}})])
    with pytest.raises(ConfigError, match="at least one case"):
        cases_to_jsonl([])
