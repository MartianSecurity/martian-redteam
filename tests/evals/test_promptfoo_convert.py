"""A promptfoo config converted into suite files that run, with every assertion, provider and
option that does not convert listed in the report and in each suite file.

``examples/promptfoo/promptfooconfig.yaml`` is written by hand from promptfoo's configuration
reference; ``agent.py`` there answers its tests from a table, so the suites run with no model call.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from typer.testing import CliRunner

from martian.cli import app
from martian.evals import runner
from martian.evals.promptfoo_convert import (
    ASSERTION_CHECKS,
    CALL_CHECKS,
    UNSUPPORTED,
    PromptfooConfigError,
    convert_promptfoo_config,
    write_conversion,
)
from martian.evals.promptfoo_import import read_promptfoo_results
from martian.evals.runner import run_suite
from martian.evals.suite_file import load_suite_file

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "promptfoo"
CONFIG = EXAMPLE / "promptfooconfig.yaml"
TARGET = f"{EXAMPLE / 'agent.py'}:answer"

# Everything in the sample config the suites leave out, by where it is in the config.
SAMPLE_UNCONVERTED = [
    "evaluateOptions",
    "providers[0] mini (openai:gpt-4.1-mini)",
    "providers[1] agent (file://agent.py)",
    "tests[0].assert[1].weight",
    "tests[0].assert[2] llm-rubric",
    "tests[1].assert[2] llm-rubric",
    "tests[2].assert[2] llm-rubric",
    "tests[2].assert[3] similar",
    "tests[3].assert[2] contains-json",
    "tests[4].assert[2] javascript",
    "tests[4].assert[3].metric",
]


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("MLFLOW_DISABLE_TELEMETRY", "true")
    monkeypatch.setenv("MARTIAN_EVALS_HOME", str(tmp_path / "evals-home"))


def counts(check: dict) -> tuple[int, int, int]:
    """A check's counts in summary.json, without its pass rate and interval."""
    return check["passed"], check["failed"], check["errors"]


def config_file(folder: Path, config: dict) -> Path:
    path = folder / "promptfooconfig.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return path


def test_the_sample_config_converts_and_its_suites_run_against_a_function(tmp_path):
    out = tmp_path / "converted"
    conversion = convert_promptfoo_config(CONFIG, output=out, target=TARGET)
    write_conversion(conversion, out)

    # Tests asserting different things are in different suites; every check is a check on main.
    # defaultTest's latency applies to every test, and only test 5 limits its cost.
    common = ("not_icontains", "not_regex", "latency")
    assert [(s.suite_file, s.data_file, s.tests, s.checks) for s in conversion.suites] == [
        ("evals.yaml", "cases.jsonl", (0, 1, 2), (*common, "starts_with", "icontains")),
        ("evals-2.yaml", "cases-2.jsonl", (3,), (*common, "is_json", "contains_all")),
        ("evals-3.yaml", "cases-3.jsonl", (4,), (*common, "is_json", "contains_all", "cost")),
    ]
    assert [f"{note.where} {note.what}".strip() for note in conversion.unconverted] == SAMPLE_UNCONVERTED
    # ...and each suite file says so at its top, as comments.
    for suite in conversion.suites:
        text = (out / suite.suite_file).read_text(encoding="utf-8")
        assert all(f"# - {note.line()}" in text for note in conversion.unconverted)

    first = yaml.safe_load((out / "evals.yaml").read_text(encoding="utf-8"))
    # The target's path is written relative to the suite file.
    assert first["target"]["function"] == "answer" and not Path(first["target"]["file"]).is_absolute()
    assert (out / first["target"]["file"]).resolve() == EXAMPLE / "agent.py"
    assert first["checks"] == [
        {"type": "not_contains", "name": "not_icontains", "values": ["as an AI language model"]},
        {"type": "not_matches", "name": "not_regex", "pattern": r"\b(?:sorry|apologi[sz]e)\b"},
        {"type": "latency", "max_ms": 3000},
        {"type": "matches", "name": "starts_with", "pattern": r"\AThe capital of"},
        {"type": "contains", "name": "icontains", "expectation": "icontains_values"},
    ]
    second = yaml.safe_load((out / "evals-2.yaml").read_text(encoding="utf-8"))
    # {{traveller}} in an assertion's value is filled from the test's vars, as promptfoo does.
    assert second["checks"][3:] == [
        {"type": "is_json", "required_keys": ["city", "traveller", "stops"]},
        {"type": "contains", "name": "contains_all", "values": ["Louvre", "Sam"], "case_sensitive": True},
    ]
    assert second["output"] == "results-2"
    third = yaml.safe_load((out / "evals-3.yaml").read_text(encoding="utf-8"))
    assert third["checks"][4:] == [
        {"type": "contains", "name": "contains_all", "values": ["Alfama", "Sam"], "case_sensitive": True},
        {"type": "cost", "max_usd": 0.002},
    ]

    # The cases have the ids `martian evals import promptfoo` gives the same tests' results.
    results = read_promptfoo_results(EXAMPLE / "results.json").file.results.results
    imported = {f"test {row.test_idx + 1}": row.prompt.raw for row in results}
    _, cases = load_suite_file(out / "evals.yaml")
    assert {case.id: case.inputs["prompt"] for case in cases} == {
        case_id: prompt for case_id, prompt in imported.items() if case_id in {"test 1", "test 2", "test 3"}
    }

    # The suites run against agent.py: test 3's reply does not start with "The capital of", and
    # every call is within the latency limit.
    home = tmp_path / "evals-home"
    summary = run_suite(out / "evals.yaml", home=home)
    assert (summary["cases"], summary["passed"], summary["failed"], summary["errors"]) == (3, 2, 1, 0)
    assert counts(summary["checks"]["starts_with"]) == (2, 1, 0)
    assert counts(summary["checks"]["latency"]) == (3, 0, 0)
    summary = run_suite(out / "evals-2.yaml", home=home)
    assert (summary["cases"], summary["passed"], summary["failed"], summary["errors"]) == (1, 1, 0, 0)
    # agent.py makes no model call that MLflow could price, so the cost check has nothing to
    # measure: an error, as promptfoo's is on a provider that reports no cost.
    summary = run_suite(out / "evals-3.yaml", home=home)
    assert (summary["cases"], summary["passed"], summary["failed"], summary["errors"]) == (1, 0, 0, 1)
    assert counts(summary["checks"]["cost"]) == (0, 0, 1)
    assert "the trace records no cost" in json.dumps(summary["check_errors"]["cost"])
    assert all((out / folder / "summary.json").exists() for folder in ("results", "results-2", "results-3"))


def test_llm_rubric_becomes_the_rubric_judge_on_the_judge_model(tmp_path):
    out = tmp_path / "converted"
    conversion = convert_promptfoo_config(CONFIG, output=out, target=TARGET, judge_model="openai:/judge-model")
    write_conversion(conversion, out)
    suite, cases = load_suite_file(out / "evals.yaml")
    rubric = suite.scorers[-1]
    assert (rubric.name, rubric.kind, rubric.type, rubric.model, rubric.criteria) == (
        "llm_rubric",
        "llm_judge",
        "rubric",
        "openai:/judge-model",
        {},
    )
    # Each test's rubric differs, so each case carries its own.
    assert [case.expectations["rubric"] for case in cases] == [
        "Names Paris as the capital of France",
        "Names Tokyo as the capital of Japan",
        "Names Lisbon as the capital of Portugal",
    ]
    assert not any(note.what == "llm-rubric" for note in conversion.unconverted)


def test_without_a_target_the_suite_grades_each_tests_provider_output(tmp_path):
    config = config_file(
        tmp_path,
        {
            "prompts": ["Say hello to {{name}}", "Greet {{name}} warmly"],
            "providers": ["openai:gpt-4.1-mini"],
            "defaultTest": {"assert": [{"type": "icontains", "value": "{{name}}"}]},
            "tests": [
                {"vars": {"name": "Ada"}, "providerOutput": "Hello, Ada!"},
                {"vars": {"name": "Linus"}, "providerOutput": "Hi there."},
                {"vars": {"name": "Grace"}},
            ],
        },
    )
    out = tmp_path / "stored"
    conversion = convert_promptfoo_config(config, output=out)
    write_conversion(conversion, out)
    assert [note.line() for note in conversion.unconverted] == [
        "providers[0] openai:gpt-4.1-mini: not called: the suite grades each test's providerOutput instead",
        "tests[2]: has no providerOutput to grade; give --target FILE.py[:FUNCTION] to call a function",
    ]
    suite, cases = load_suite_file(out / "evals.yaml")
    assert suite.target is not None and suite.target.kind == "stored"
    # Each test once per prompt, with the ids the imported results give them.
    assert [(case.id, case.inputs["prompt"], case.outputs) for case in cases] == [
        ("test 1, prompt 1", "Say hello to Ada", "Hello, Ada!"),
        ("test 1, prompt 2", "Greet Ada warmly", "Hello, Ada!"),
        ("test 2, prompt 1", "Say hello to Linus", "Hi there."),
        ("test 2, prompt 2", "Greet Linus warmly", "Hi there."),
    ]
    summary = run_suite(out / "evals.yaml", home=tmp_path / "evals-home")
    assert (summary["cases"], summary["passed"], summary["failed"]) == (4, 2, 2)


# Each assertion type that converts, a reply, and promptfoo's verdict on it.
VERDICTS = [
    ({"type": "contains", "value": "Paris"}, "Paris, France", True),
    ({"type": "contains", "value": "Paris"}, "paris, France", False),
    ({"type": "contains", "value": 42}, "The answer is 42.", True),
    ({"type": "icontains", "value": "PARIS"}, "paris", True),
    ({"type": "contains-any", "value": ["Rome", "Paris"]}, "Paris", True),
    ({"type": "contains-any", "value": "Rome, Oslo"}, "Paris", False),
    ({"type": "contains-all", "value": "Paris, France"}, "Paris", False),
    ({"type": "icontains-all", "value": ["paris", "FRANCE"]}, "Paris, France", True),
    ({"type": "not-contains", "value": "secret"}, "The Secret is safe", True),
    ({"type": "not-icontains", "value": "secret"}, "The Secret is safe", False),
    ({"type": "not-contains-any", "value": ["key", "token"]}, "here is a token", False),
    ({"type": "not-icontains-any", "value": ["KEY", "TOKEN"]}, "nothing to see", True),
    ({"type": "regex", "value": r"^\d{3}$"}, "123", True),
    ({"type": "regex", "value": r"^\d{3}$"}, "1234", False),
    ({"type": "not-regex", "value": "error"}, "all good", True),
    ({"type": "equals", "value": "JPY"}, "JPY", True),
    ({"type": "equals", "value": "JPY"}, "JPY.", False),
    ({"type": "equals", "value": "a+b (c)"}, "a+b (c)", True),
    ({"type": "equals", "value": 3.0}, "3", True),
    ({"type": "not-equals", "value": "JPY"}, "EUR", True),
    ({"type": "starts-with", "value": "The"}, "The end", True),
    ({"type": "starts-with", "value": "The"}, "the end", False),
    ({"type": "not-starts-with", "value": "Sorry"}, "Sorry, no", False),
    ({"type": "is-json"}, '{"a": 1}', True),
    ({"type": "is-json"}, "not json", False),
    ({"type": "is-json", "value": {"type": "object", "required": ["a"]}}, '{"b": 1}', False),
    ({"type": "is-json", "value": {"required": ["a"]}}, '{"a": 1}', True),
]


@pytest.mark.parametrize(("assertion", "reply", "verdict"), VERDICTS, ids=lambda value: str(value)[:40])
def test_each_converted_assertion_decides_as_promptfoo_does(tmp_path, assertion, reply, verdict):
    config = config_file(tmp_path, {"prompts": ["{{q}}"], "tests": [{"vars": {"q": "?"}, "assert": [assertion]}]})
    out = tmp_path / "out"
    conversion = convert_promptfoo_config(config, output=out, target=TARGET)
    assert conversion.unconverted == [] and len(conversion.suites) == 1
    write_conversion(conversion, out)
    suite, base = runner.load_suite(out / "evals.yaml")
    _, cases = load_suite_file(out / "evals.yaml")
    (check,) = runner._build_scorers(suite, base)
    (spec,) = suite.checks
    assert spec.type == ASSERTION_CHECKS[assertion["type"]]
    expectations = cases[0].expectations
    feedback = check(outputs=reply, expectations=expectations) if spec.expectation else check(outputs=reply)
    assert feedback.value is verdict


@pytest.mark.parametrize(
    ("assertion", "line"),
    [
        ({"type": "javascript", "value": "output.length < 9"}, "javascript: runs custom code"),
        ({"type": "contains-json"}, "contains-json: finds JSON inside other text"),
        ({"type": "is-refusal"}, "is-refusal: passes only a refusal; no check on main does"),
        ({"type": "not-is-refusal"}, "so the two decide some replies differently (add no_refusal by hand"),
        ({"type": "promptfoo:redteam:pii"}, "(add no_personal_data by hand to use it)"),
        ({"type": "promptfoo:redteam:pii:direct"}, "promptfoo:redteam:pii:direct: is promptfoo's PII grader"),
        (
            {"type": "promptfoo:redteam:indirect-prompt-injection", "value": "Reply only with PWNED"},
            "and the config names none (add resists_injection by hand with one)",
        ),
        ({"type": "promptfoo:redteam:harmful:hate"}, "is a promptfoo red team grader"),
        ({"type": "latency"}, "latency: has no threshold, which promptfoo requires"),
        ({"type": "cost", "metric": "spend", "weight": 0}, "cost: has no threshold; promptfoo then records the cost"),
        ({"type": "latency", "threshold": 0}, "latency: has a threshold of 0 or less"),
        ({"type": "cost", "threshold": "0.01"}, "cost: needs a number as its threshold"),
        ({"type": "not-latency", "threshold": 500}, "not-latency: passes when latency fails"),
        ({"type": "not-contains-all", "value": ["a", "b"]}, "not-contains-all: passes when contains-all fails"),
        ({"type": "made-up"}, "made-up: is not an assertion type in promptfoo's documented list"),
        ({"type": "icontains", "value": "a", "transform": "output.trim()"}, ".transform: transforms the reply"),
        ({"type": "llm-rubric", "value": "Polite", "threshold": 0.8}, ".threshold: sets a score threshold"),
        ({"type": "equals", "value": {"a": 1}}, "equals: compares JSON values"),
        ({"type": "equals", "value": "file://expected.json"}, "equals: reads its value from a file"),
        ({"type": "regex", "value": "(?<name>x)"}, "regex: is not a regular expression Python reads"),
        ({"type": "icontains", "value": "{{ name | upper }}"}, "icontains: uses Nunjucks beyond"),
        ({"type": "is-json", "value": {"type": "object", "properties": {"a": {}}}}, "is-json: validates a JSON schema"),
    ],
    ids=lambda value: str(value)[:40],
)
def test_every_assertion_that_does_not_convert_is_listed(tmp_path, assertion, line):
    config = config_file(
        tmp_path,
        {
            "prompts": ["{{q}}"],
            "tests": [{"vars": {"q": "?"}, "assert": [{"type": "icontains", "value": "x"}, assertion]}],
        },
    )
    conversion = convert_promptfoo_config(config, output=tmp_path / "out", target=TARGET, judge_model="openai:/j")
    lines = [note.line() for note in conversion.unconverted]
    assert any(found.startswith("tests[0].assert[1]") and line in found for found in lines), lines
    # The test's other assertion still converts.
    assert conversion.suites[0].checks == ("icontains",)


def test_every_documented_type_converts_or_has_a_reason():
    assert not set(ASSERTION_CHECKS) & set(UNSUPPORTED)
    assert {"equals", "contains", "icontains", "regex", "is-json", "llm-rubric", "latency", "cost"} <= set(
        ASSERTION_CHECKS
    )
    assert {"javascript", "contains-json", "similar", "python", "is-refusal", "not-is-refusal"} <= set(UNSUPPORTED)


def call(milliseconds=None, spent=None):
    """A trace of one target call, as MLflow gives it to a scorer."""
    return SimpleNamespace(
        info=SimpleNamespace(execution_duration=milliseconds, token_usage=None, cost=spent),
        data=SimpleNamespace(spans=[SimpleNamespace(name="answer", span_type="UNKNOWN")]),
    )


# promptfoo passes a call whose time in ms, or cost in US dollars, is at most the threshold, and
# errs on a call it has no figure for.
CALL_VERDICTS = [
    ({"type": "latency", "threshold": 3000}, call(milliseconds=3000), True),
    ({"type": "latency", "threshold": 3000}, call(milliseconds=3001), False),
    ({"type": "latency", "threshold": 250.5}, call(milliseconds=250), True),
    ({"type": "cost", "threshold": 0.002}, call(spent={"total_cost": 0.002}), True),
    ({"type": "cost", "threshold": 0.002}, call(spent={"total_cost": 0.0021}), False),
    ({"type": "cost", "threshold": 0.002}, call(spent=None), None),
]


@pytest.mark.parametrize(("assertion", "trace", "verdict"), CALL_VERDICTS, ids=lambda value: str(value)[:40])
def test_latency_and_cost_limit_the_targets_call_as_promptfoo_does(tmp_path, assertion, trace, verdict):
    config = config_file(
        tmp_path, {"prompts": ["{{q}}"], "tests": [{"vars": {"q": "?"}, "assert": [{**assertion, "metric": "m"}]}]}
    )
    out = tmp_path / "out"
    conversion = convert_promptfoo_config(config, output=out, target=TARGET)
    # The metric is listed; the threshold is the check's limit, not a score threshold.
    assert [note.line() for note in conversion.unconverted] == [
        "tests[0].assert[0].metric: names a promptfoo metric; the check is named after the assertion's type"
    ]
    write_conversion(conversion, out)
    suite, base = runner.load_suite(out / "evals.yaml")
    (spec,) = suite.checks
    limit = CALL_CHECKS[assertion["type"]]
    assert (spec.type, getattr(spec, limit)) == (assertion["type"], assertion["threshold"])
    (check,) = runner._build_scorers(suite, base)
    if verdict is None:
        with pytest.raises(ValueError, match="records no cost"):
            check(trace=trace)
    else:
        assert check(trace=trace).value is verdict


def test_latency_and_cost_need_a_call_to_measure(tmp_path):
    config = config_file(
        tmp_path,
        {
            "prompts": ["{{q}}"],
            "tests": [
                {
                    "vars": {"q": "?"},
                    "providerOutput": "stored",
                    "assert": [
                        {"type": "contains", "value": "stored"},
                        {"type": "latency", "threshold": 500},
                        {"type": "cost", "threshold": 0.01},
                    ],
                }
            ],
        },
    )
    conversion = convert_promptfoo_config(config, output=tmp_path / "out")
    why = "measures the provider's call, and the suite grades each test's stored providerOutput"
    assert [note.line() for note in conversion.unconverted] == [
        f"tests[0].assert[1] latency: {why}, which no call produced; give --target FILE.py[:FUNCTION]",
        f"tests[0].assert[2] cost: {why}, which no call produced; give --target FILE.py[:FUNCTION]",
    ]
    assert conversion.suites[0].checks == ("contains",)


def test_tests_options_and_prompts_that_do_not_convert_are_listed(tmp_path):
    (tmp_path / "prompts.txt").write_text("First {{q}}\n---\nSecond {{q}}\n", encoding="utf-8")
    config = config_file(
        tmp_path,
        {
            "prompts": ["file://prompts.txt", "{% if q %}{{q}}{% endif %}", "file://prompt.py"],
            "providers": [{"id": "openai:gpt-4.1-mini", "config": {"temperature": 0}}],
            "scenarios": [{"config": [{"vars": {"q": "x"}}]}],
            "defaultTest": {"options": {"provider": "openai:gpt-4.1", "transform": "output.trim()"}},
            "tests": [
                {
                    "vars": {"q": "a"},
                    "threshold": 0.5,
                    "metadata": {"k": "v"},
                    "assert": [{"type": "contains", "value": "a"}],
                },
                {"vars": {"q": ["a", "b"]}, "assert": [{"type": "contains", "value": "a"}]},
                {"vars": {"q": "file://q.txt"}, "assert": [{"type": "contains", "value": "a"}]},
                {"vars": {"q": "c"}, "assert": [{"type": "javascript", "value": "true"}]},
                {"vars": {"q": "d"}, "providerOutput": "d", "assert": [{"type": "contains", "value": "d"}]},
            ],
        },
    )
    conversion = convert_promptfoo_config(config, output=tmp_path / "out", target=TARGET)
    assert [note.line() for note in conversion.unconverted] == [
        "scenarios: crosses tests with sets of vars; its tests are not converted",
        "prompts[1]: uses Nunjucks beyond {{ name }} and {{ name.key }}; the prompt is not converted",
        "prompts[2] prompt.py: is a prompt file this converter does not read (it reads .txt and .md files)",
        "providers[0] openai:gpt-4.1-mini: not called: the suite calls agent.py:answer instead",
        "defaultTest.options.provider: names the grading model; the suite's judge is --judge-model",
        "defaultTest.options.transform: transforms the reply before the assertions; the suite's checks see the "
        "reply as the target returns it",
        "tests[0].metadata: a suite's data file has no place for case metadata",
        "tests[0].threshold: passes the test on a weighted score; a suite's case passes when every check passes",
        "tests[1].vars.q: is a list, which promptfoo runs once per value; the test is not converted",
        "tests[2].vars.q: is read from a file, which this converter does not read; the test is not converted",
        "tests[3].assert[0] javascript: runs custom code, and a suite file's checks run none",
        "tests[3]: no assertion converts, so the test is in no suite",
        "tests[4].providerOutput: is not used: the suite calls agent.py:answer",
    ]
    # The prompt file's two prompts are prompts 1 and 2; the third and fourth do not convert.
    (suite,) = conversion.suites
    assert suite.tests == (0, 4)
    assert [json.loads(line)["id"] for line in suite.data.splitlines()] == [
        "test 1, prompt 1",
        "test 1, prompt 2",
        "test 5, prompt 1",
        "test 5, prompt 2",
    ]


def test_files_that_are_not_configs_and_folders_already_written_are_refused(tmp_path):
    with pytest.raises(PromptfooConfigError, match="it has no prompts"):
        convert_promptfoo_config(config_file(tmp_path, {"tests": []}), output=tmp_path / "out")
    with pytest.raises(PromptfooConfigError, match="give a Python file"):
        convert_promptfoo_config(CONFIG, output=tmp_path / "out", target="agent.js")
    with pytest.raises(PromptfooConfigError, match="there is no file"):
        convert_promptfoo_config(CONFIG, output=tmp_path / "out", target=str(tmp_path / "missing.py"))
    out = tmp_path / "out"
    out.mkdir()
    (out / "evals-2.yaml").write_text("mine", encoding="utf-8")
    with pytest.raises(PromptfooConfigError, match="already has evals-2.yaml"):
        write_conversion(convert_promptfoo_config(CONFIG, output=out, target=TARGET), out)
    assert sorted(path.name for path in out.iterdir()) == ["evals-2.yaml"]


def test_the_command_writes_the_suites_and_prints_what_it_left_out(tmp_path):
    out = tmp_path / "converted"
    result = CliRunner().invoke(
        app, ["evals", "convert", "promptfoo", str(CONFIG), "--target", TARGET, "--output", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert f"{out / 'evals.yaml'}: promptfoo tests 1, 2, 3, 3 cases in cases.jsonl, checks " in result.output
    assert "Not converted (also listed at the top of each suite file):\n- evaluateOptions:" in result.output
    assert "- tests[4].assert[2] javascript: runs custom code" in result.output
    assert sorted(path.name for path in out.iterdir()) == [
        "cases-2.jsonl",
        "cases-3.jsonl",
        "cases.jsonl",
        "evals-2.yaml",
        "evals-3.yaml",
        "evals.yaml",
    ]
    # Without a target or stored replies nothing converts, and nothing is written.
    empty = CliRunner().invoke(app, ["evals", "convert", "promptfoo", str(CONFIG), "--output", str(tmp_path / "none")])
    assert empty.exit_code == 2 and "Nothing written: no test converts." in empty.output
    assert not (tmp_path / "none").exists()
