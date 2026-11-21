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
    _TOOL_CALLS,
    ASSERTION_CHECKS,
    CALL_CHECKS,
    UNSUPPORTED,
    PromptfooConfigError,
    convert_promptfoo_config,
    recorded_checks,
    write_conversion,
)
from martian.evals.promptfoo_import import promptfoo_runs, read_promptfoo_results
from martian.evals.runner import run_suite
from martian.evals.suite_file import load_suite_file

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "promptfoo"
CONFIG = EXAMPLE / "promptfooconfig.yaml"
TARGET = f"{EXAMPLE / 'agent.py'}:answer"
# A config with two prompts and two providers, and the results promptfoo 0.125.1 wrote for it.
WRITTEN = Path(__file__).resolve().parent / "promptfoo"

# Everything in the sample config the suites leave out, by where it is in the config.
SAMPLE_UNCONVERTED = [
    "evaluateOptions",
    "providers[0] mini (openai:gpt-4.1-mini)",
    "providers[1] agent (file://agent.py)",
    "tests[0].assert[2] llm-rubric",
    "tests[0].assert[1].weight",
    "tests[1].assert[2] llm-rubric",
    "tests[2].assert[2] llm-rubric",
    "tests[2].assert[3] similar",
    "tests[4].assert[2] javascript",
    "tests[4]",
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


def test_derived_metrics_keep_prompt_columns_and_recompute_from_reused_results(tmp_path):
    out = tmp_path / "converted"
    source = config_file(
        tmp_path,
        {
            "prompts": ["Rail: {{ stop }}", "Walking: {{ stop }}"],
            "providers": ["echo"],
            "defaultTest": {
                "assert": [
                    {"type": "contains", "value": "Rail", "metric": "label", "weight": 2},
                    {"type": "contains", "value": "park", "metric": "label", "weight": 0},
                ]
            },
            "tests": [{"vars": {"stop": name}} for name in ("park", "station")],
            "derivedMetrics": [
                {"name": "mean_points", "value": "label / __count"},
                {"name": "normalized", "value": "mean_points > 1 ? mean_points / 3 : 0"},
            ],
        },
    )
    write_conversion(convert_promptfoo_config(source, output=out), out, strict=True)
    path = out / "evals.yaml"
    home = tmp_path / "evals-home"
    first = run_suite(path, home=home)
    assert {(row["group"], row["name"]): (row["cases"], row["score"]) for row in first["derived_metrics"]} == {
        ("Prompt 1", "mean_points"): (2, 2.5),
        ("Prompt 1", "normalized"): (2, 2.5 / 3),
        ("Prompt 2", "mean_points"): (2, 0.5),
        ("Prompt 2", "normalized"): (2, 0),
    }
    results = out / "results" / "results.jsonl"
    saved = results.read_bytes()
    config = yaml.safe_load(path.read_text())
    config["derived_metrics"]["formulas"][1]["value"] = "mean_points * 2"
    path.write_text(yaml.safe_dump(config))

    messages = []
    second = run_suite(path, home=home, progress=messages.append)
    assert not any("cases graded" in message for message in messages)
    assert results.read_bytes() == saved
    assert [row["score"] for row in second["derived_metrics"] if row["name"] == "normalized"] == [5, 1]


def test_strict_conversion_keeps_each_cases_checks_and_rolls_up_the_whole_prompt(tmp_path):
    out = tmp_path / "converted"
    source = config_file(
        tmp_path,
        {
            "prompts": ["Rail"],
            "providers": ["echo"],
            "tests": [
                {"assert": [{"type": "equals", "value": "Rail", "metric": "quality"}]},
                {"assert": [{"type": "contains", "value": "Rail", "metric": "quality", "weight": 2}]},
                {"assert": [{"type": "contains", "value": "Bus", "metric": "quality"}]},
            ],
            "derivedMetrics": [{"name": "average", "value": "quality / __count"}],
        },
    )
    conversion = convert_promptfoo_config(source, output=out)
    write_conversion(conversion, out, strict=True)
    (one,) = conversion.suites
    assert one.tests == (0, 1, 2)
    path = out / one.suite_file
    suite, _ = load_suite_file(path)
    assert [selected.cases for selected in suite.check_sets] == [("test 1",), ("test 2",), ("test 3",)]
    assert suite.check_sets[2].aliases == {"contains_set_3": "contains"}
    found = run_suite(path, home=tmp_path / "evals-home")
    assert (found["passed"], found["failed"], found["errors"]) == (2, 1, 0)
    records = [json.loads(line) for line in (out / "results" / "results.jsonl").read_text().splitlines()]
    assert [list(row["checks"]) for row in records] == [
        ["equals", "case_gate"],
        ["contains", "case_gate"],
        ["contains_set_3", "case_gate"],
    ]
    (average,) = found["derived_metrics"]
    assert (average["score"], average["cases"], average["group"]) == (1, 3, "Prompt 1")


def test_the_sample_config_converts_and_its_suites_run_against_a_function(tmp_path):
    out = tmp_path / "converted"
    conversion = convert_promptfoo_config(CONFIG, output=out, target=TARGET)
    write_conversion(conversion, out)

    # Tests asserting different things are in different suites; every check is a built-in check.
    # defaultTest's latency applies to every test, and only test 5 limits its cost.
    common = ("not_icontains", "not_regex", "latency")
    assert [(s.suite_file, s.data_file, s.tests, s.checks) for s in conversion.suites] == [
        ("evals.yaml", "cases.jsonl", (0, 1, 2), (*common, "starts_with", "icontains")),
        ("evals-2.yaml", "cases-2.jsonl", (3,), (*common, "is_json", "contains_all", "contains_json")),
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
        {
            "type": "not_contains",
            "name": "not_icontains",
            "values": ["as an AI language model"],
            "case_mapping": "lower",
        },
        {"type": "not_matches", "name": "not_regex", "pattern": r"(?a)\b(?:sorry|apologi[sz]e)\b"},
        {"type": "latency", "max_ms": 3000},
        {"type": "matches", "name": "starts_with", "pattern": r"\AThe capital of"},
        {"type": "contains", "name": "icontains", "expectation": "icontains_values", "case_mapping": "lower"},
    ]
    second = yaml.safe_load((out / "evals-2.yaml").read_text(encoding="utf-8"))
    # {{traveller}} in an assertion's value is filled from the test's vars, as promptfoo does.
    assert second["checks"][3:] == [
        {
            "type": "json_schema",
            "name": "is_json",
            "schema": {"type": "object", "required": ["city", "traveller", "stops"]},
            "plain_json": True,
        },
        {"type": "contains", "name": "contains_all", "values": ["Louvre", "Sam"], "case_sensitive": True},
        {"type": "json_schema", "name": "contains_json", "schema": {}, "embedded": "promptfoo"},
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


def test_llm_rubric_becomes_a_scored_rubric_on_the_judge_model(tmp_path):
    out = tmp_path / "converted"
    conversion = convert_promptfoo_config(CONFIG, output=out, target=TARGET, judge_model="openai:/judge-model")
    write_conversion(conversion, out)
    weighted, _ = load_suite_file(out / conversion.suites[0].suite_file)
    assert weighted.case_gate.weights == {"icontains": 2.0}
    assert weighted.scorers[-1].criteria == {"rubric": "Names Paris as the capital of France"}

    suite, cases = load_suite_file(out / conversion.suites[1].suite_file)
    rubric = suite.scorers[-1]
    assert (rubric.name, rubric.kind, rubric.type, rubric.model, rubric.criteria) == (
        "llm_rubric",
        "llm_judge",
        "scored_rubric",
        "openai:/judge-model",
        {"expectation": "llm_rubric_rubric"},
    )
    # Each test's rubric differs, so each case carries its own.
    assert [case.expectations["llm_rubric_rubric"] for case in cases] == [
        "Names Tokyo as the capital of Japan",
        "Names Lisbon as the capital of Portugal",
    ]
    assert not any(note.what == "llm-rubric" for note in conversion.unconverted)
    assert any("uses the SDK grading prompt" in note.why for note in conversion.strict_blockers)


def test_conversion_keeps_weights_for_multiple_scored_rubrics_in_an_assertion_set(tmp_path):
    assertions = [
        {"type": "llm-rubric", "value": "Includes the date", "threshold": 0.6, "weight": 3},
        {"type": "llm-rubric", "value": "Acknowledges the recipient"},
        {"type": "not-llm-rubric", "value": "Includes irrelevant text", "threshold": 0},
    ]
    config = config_file(
        tmp_path,
        {
            "prompts": ["Summarize the meeting"],
            "tests": [
                {
                    "threshold": 0.7,
                    "providerOutput": "The meeting is Tuesday.",
                    "assert": [{"type": "assert-set", "threshold": 0.8, "assert": assertions}],
                },
            ],
        },
    )
    out = tmp_path / "out"
    conversion = convert_promptfoo_config(config, output=out, judge_model="openai:/fixture")
    write_conversion(conversion, out)
    suite, _ = load_suite_file(out / "evals.yaml")
    assert conversion.unconverted == []
    assert len(conversion.strict_blockers) == 3
    assert [scorer.criteria for scorer in suite.scorers] == [
        {"rubric": "Includes the date", "score_threshold": 0.6},
        {"rubric": "Acknowledges the recipient"},
        {"rubric": "Includes irrelevant text", "score_threshold": 0.0, "negate": True},
    ]
    assert suite.case_gate.weights == {"llm_rubric": 3.0}
    assert suite.case_gate.assertion_sets[0].checks == ["llm_rubric", "llm_rubric_2", "not_llm_rubric"]


def test_rubric_prompt_inheritance_and_files(tmp_path):
    default = [{"role": "user", "content": "Grade {{ output }} on {{ rubric }} for {{ team }}"}]
    test_prompt = "Return a grade for {{ output }}.\n"
    assertion_prompt = [{"role": "user", "content": "Override: {{ output }}."}]
    (tmp_path / "grader.txt").write_text(test_prompt, encoding="utf-8")
    (tmp_path / "assertion.json").write_text(json.dumps(assertion_prompt), encoding="utf-8")
    config = config_file(
        tmp_path,
        {
            "prompts": ["Summarize the meeting"],
            "providers": ["echo"],
            "defaultTest": {
                "options": {"rubricPrompt": default},
                "assert": [{"type": "llm-rubric", "value": "Includes {{ topic }}"}],
            },
            "tests": [
                {"vars": {"topic": "the date", "team": "Sales"}},
                {"options": {"rubricPrompt": "file://grader.txt"}, "vars": {"topic": "the time"}},
                {
                    "options": {"rubricPrompt": "file://grader.txt", "disableDefaultAsserts": True},
                    "assert": [{"type": "llm-rubric", "rubricPrompt": "file://assertion.json"}],
                },
                {"vars": {"topic": "the room", "team": "Support"}},
            ],
        },
    )
    out = tmp_path / "out"
    conversion = convert_promptfoo_config(config, output=out, judge_model="openai:/fixture")
    assert conversion.unconverted == []
    assert len(conversion.strict_blockers) == 2
    assert all("custom grading prompt and uses the SDK reply schema" in note.why for note in conversion.strict_blockers)
    write_conversion(conversion, out)
    assert [item.tests for item in conversion.suites] == [(0, 3), (1,), (2,)]
    suites = [load_suite_file(out / item.suite_file) for item in conversion.suites]
    assert suites[0][0].scorers[0].criteria == {"judge_prompt": default, "expectation": "llm_rubric_rubric"}
    assert [case.expectations["llm_rubric_rubric"] for case in suites[0][1]] == [
        "Includes the date",
        "Includes the room",
    ]
    assert suites[1][0].scorers[0].criteria == {"judge_prompt": test_prompt, "rubric": "Includes the time"}
    assert suites[2][0].scorers[0].criteria == {"judge_prompt": assertion_prompt, "rubric": ""}


@pytest.mark.parametrize(
    ("prompt", "problem"),
    [
        ("Grade {{ output | lower }}", "simple {{ name }} placeholders"),
        ("file://grader.js", "literal local .txt, .md or .json file reference"),
        ([{"role": "system", "content": "{{ output }}"}], "puts {{ output }} in a user message"),
    ],
)
def test_conversion_reports_unsupported_grader_prompt_in_required_checks(tmp_path, prompt, problem):
    config = config_file(
        tmp_path,
        {
            "prompts": ["Summarize the meeting"],
            "providers": ["echo"],
            "tests": [
                {
                    "threshold": 0.75,
                    "assert": [
                        {"type": "llm-rubric", "value": "Includes the date", "rubricPrompt": prompt},
                        {"type": "contains", "value": "meeting"},
                    ],
                }
            ],
        },
    )
    conversion = convert_promptfoo_config(config, output=tmp_path / "out", judge_model="openai:/fixture")
    assert conversion.suites == []
    assert any(problem in note.why for note in conversion.strict_blockers)
    assert any("required score could not be preserved" in note.why for note in conversion.strict_blockers)


def test_all_providers_preserve_the_scoped_test_and_prompt_matrix(tmp_path):
    config = config_file(
        tmp_path,
        {
            "providers": [
                {"id": "echo", "label": "Sample:Short", "prompts": ["faq-short"]},
                {"id": "echo", "label": "Sample:Wide", "prompts": ["FAQ:*"]},
            ],
            "prompts": [
                {"id": "faq-full", "label": "FAQ:Full", "raw": "Full answer for {{subject}}"},
                {"id": "faq-short", "label": "FAQ:Short", "raw": "Short answer for {{subject}}"},
                {"id": "faq-extra", "label": "Detail", "raw": "Extra detail for {{subject}}"},
            ],
            "defaultTest": {
                "providers": ["Sample"],
                "prompts": ["FAQ"],
                "assert": [{"type": "contains", "value": "{{subject}}"}],
            },
            "tests": [
                {"vars": {"subject": "shipping"}},
                {"vars": {"subject": "returns"}, "providers": ["Sample:Short"], "prompts": ["faq-short"]},
                {"vars": {"subject": "warranty"}, "prompts": ["faq-full"]},
                {"vars": {"subject": "receipt"}, "providers": []},
                {"vars": {"subject": "help"}, "prompts": []},
            ],
        },
    )
    out = tmp_path / "out"
    conversion = convert_promptfoo_config(config, output=out, all_providers=True)
    assert conversion.strict_blockers == []
    assert [suite.source_provider for suite in conversion.suites] == ["Sample:Short (echo)", "Sample:Wide (echo)"]
    assert [suite.suite_file for suite in conversion.suites] == ["evals-provider-1.yaml", "evals-provider-2.yaml"]
    write_conversion(conversion, out, strict=True)
    short, short_cases = load_suite_file(out / "evals-provider-1.yaml")
    wide, wide_cases = load_suite_file(out / "evals-provider-2.yaml")
    assert (short.settings.output, wide.settings.output) == ("results-provider-1", "results-provider-2")
    assert [case.id for case in short_cases] == ["test 1, prompt 2", "test 2, prompt 2"]
    assert [case.id for case in wide_cases] == ["test 1, prompt 1", "test 1, prompt 2", "test 3, prompt 1"]
    assert [case.outputs for case in wide_cases] == [
        "Full answer for shipping",
        "Short answer for shipping",
        "Full answer for warranty",
    ]
    one = convert_promptfoo_config(config, output=out, provider="Sample:Wide")
    assert not one.strict_blockers
    assert one.suites[0].data == conversion.suites[1].data


@pytest.mark.parametrize("field", ["providers", "prompts"])
def test_a_scope_typo_is_reported_before_writing_cases(tmp_path, field):
    config = config_file(
        tmp_path,
        {
            "prompts": [{"id": "brief", "raw": "Send a brief reply"}],
            "providers": ["echo"],
            "tests": [{"assert": [{"type": "contains", "value": "reply"}], field: ["mistyped"]}],
        },
    )
    out = tmp_path / "out"
    result = CliRunner().invoke(
        app, ["evals", "convert", "promptfoo", str(config), "--all-providers", "--output", str(out)]
    )
    assert result.exit_code == 2 and f"tests[0].{field}" in result.output
    assert not out.exists()


def test_strict_all_providers_does_not_write_an_incomplete_matrix(tmp_path):
    config = config_file(
        tmp_path,
        {
            "prompts": ["An offline fixture"],
            "providers": ["echo", "openai:other-model"],
            "tests": [{"assert": [{"type": "contains", "value": "fixture"}]}],
        },
    )
    out = tmp_path / "out"
    cli = ["evals", "convert", "promptfoo", str(config), "--all-providers", "--output", str(out)]
    strict = CliRunner().invoke(app, [*cli, "--strict"])
    assert strict.exit_code == 2 and "providers[1]" in strict.output
    assert not out.exists()
    partial = CliRunner().invoke(app, cli)
    assert partial.exit_code == 0 and "has no native SDK mapping" in partial.output
    assert sorted(p.name for p in out.iterdir()) == ["cases-provider-1.jsonl", "evals-provider-1.yaml"]


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


def test_cases_have_the_ids_each_providers_imported_results_give(tmp_path):
    conversion = convert_promptfoo_config(WRITTEN / "promptfooconfig.yaml", output=tmp_path / "out", target=TARGET)
    rows = [json.loads(line) for suite in conversion.suites for line in suite.data.splitlines()]
    converted = {row["id"]: row["inputs"]["prompt"] for row in rows}
    assert len(converted) == 6
    # promptfoo numbers bot-v2's prompt columns after bot-v1's; both providers' cases still have
    # the converted cases' ids, each on the same prompt.
    runs = promptfoo_runs(read_promptfoo_results(WRITTEN / "results.json"), codes={"bot-v1": "v1", "bot-v2": "v2"})
    for run in runs:
        assert {case.result.case_id: case.result.inputs["prompt"] for case in run.cases} == converted


# Each assertion type that converts, a reply, and promptfoo's verdict on it.
VERDICTS = [
    ({"type": "contains", "value": "Paris"}, "Paris, France", True),
    ({"type": "contains", "value": "Paris"}, "paris, France", False),
    ({"type": "contains", "value": 42}, "The answer is 42.", True),
    ({"type": "icontains", "value": "PARIS"}, "paris", True),
    ({"type": "icontains", "value": "STRASSE"}, "Straße", False),
    ({"type": "contains-any", "value": ["Rome", "Paris"]}, "Paris", True),
    ({"type": "contains-any", "value": "Rome, Oslo"}, "Paris", False),
    ({"type": "contains-all", "value": "Paris, France"}, "Paris", False),
    (
        {"type": "contains-all", "value": r',,"Paris, France", "say \"bonjour\"", " rue "'},
        'Paris, France. say "bonjour" on the rue .',
        True,
    ),
    ({"type": "icontains-all", "value": ["paris", "FRANCE"]}, "Paris, France", True),
    ({"type": "not-contains", "value": "secret"}, "The Secret is safe", True),
    ({"type": "not-icontains", "value": "secret"}, "The Secret is safe", False),
    ({"type": "not-contains-any", "value": ["key", "token"]}, "here is a token", False),
    ({"type": "not-icontains-any", "value": ["KEY", "TOKEN"]}, "nothing to see", True),
    ({"type": "regex", "value": r"^\d{3}$"}, "123", True),
    ({"type": "regex", "value": r"^\d{3}$"}, "1234", False),
    ({"type": "regex", "value": r"^\d{3}$"}, "١٢٣", False),
    ({"type": "regex", "value": r"^\d{3}$"}, "123\n", False),
    ({"type": "not-regex", "value": "error"}, "all good", True),
    ({"type": "equals", "value": "JPY"}, "JPY", True),
    ({"type": "equals", "value": "JPY"}, "JPY.", False),
    ({"type": "equals", "value": "a+b (c)"}, "a+b (c)", True),
    ({"type": "equals", "value": 3.0}, "3", True),
    ({"type": "equals", "value": {"a": [True, 1], "b": "Lyon"}}, '{"b":"Lyon","a":[true,1.0]}', True),
    ({"type": "not-equals", "value": [1, True]}, "[1, 1]", True),
    ({"type": "not-equals", "value": {"a": 1}}, "not json", True),
    ({"type": "not-equals", "value": "JPY"}, "EUR", True),
    ({"type": "starts-with", "value": "The"}, "The end", True),
    ({"type": "starts-with", "value": "The"}, "the end", False),
    ({"type": "not-starts-with", "value": "Sorry"}, "Sorry, no", False),
    ({"type": "is-json"}, '{"a": 1}', True),
    ({"type": "is-json"}, "not json", False),
    ({"type": "is-json"}, '```json\n{"a": 1}\n```', False),
    ({"type": "is-json"}, '{"a": NaN}', False),
    ({"type": "not-is-json"}, "not json", True),
    ({"type": "not-is-json", "value": {"type": "array"}}, "[]", False),
    ({"type": "is-json", "value": {"type": "object", "required": ["a"]}}, '{"b": 1}', False),
    ({"type": "is-json", "value": {"required": ["a"]}}, '{"a": 1}', True),
    ({"type": "is-json", "value": {"required": ["a"]}}, "[]", True),
    ({"type": "contains-json"}, "[1, 2, 3]", False),
    (
        {"type": "contains-json", "value": {"required": ["latitude"]}},
        'Map: {"note":"Closed } gate"} {"latitude":14.6}',
        True,
    ),
    (
        {"type": "not-contains-json", "value": {"required": ["latitude"]}},
        'Map: {"point":{"latitude":14.6}}',
        True,
    ),
    ({"type": "not-contains-json"}, "Map: {label: 'West Gate'", False),
    ({"type": "is-xml", "value": "catalog.title, catalog.author"}, "<catalog><title/><author/></catalog>", True),
    ({"type": "not-is-xml"}, "Note <catalog/> and more text", True),
    (
        {"type": "contains-xml", "value": {"requiredElements": ["catalog.book.1.title"]}},
        "Read: <catalog><book/><book><title>Atlas</title></book></catalog>",
        True,
    ),
    ({"type": "not-contains-xml", "value": ["catalog.author"]}, "Note: <catalog><title/></catalog>", True),
    ({"type": "levenshtein", "value": "kitten"}, "sitting", True),
    ({"type": "levenshtein", "value": "Ada", "threshold": 0}, "ada", False),
    ({"type": "levenshtein", "value": "", "threshold": 1}, "🗺", False),
    ({"type": "not-levenshtein", "value": "Ada", "threshold": 0.9}, "Ad", True),
    ({"type": "word-count", "value": 0}, "\t \ufeff\n", True),
    ({"type": "word-count", "value": {"min": 2, "max": 3}}, "north\ufeffeast", True),
    ({"type": "not-word-count", "value": {"max": 1}}, "north\x85east", False),
]


@pytest.mark.parametrize(("assertion", "reply", "verdict"), VERDICTS, ids=lambda value: str(value)[:40])
def test_each_converted_assertion_decides_as_promptfoo_does(tmp_path, assertion, reply, verdict):
    config = config_file(tmp_path, {"prompts": ["{{q}}"], "tests": [{"vars": {"q": "?"}, "assert": [assertion]}]})
    out = tmp_path / "out"
    conversion = convert_promptfoo_config(config, output=out, target=TARGET)
    assert conversion.strict_blockers == [] and len(conversion.suites) == 1
    write_conversion(conversion, out, strict=True)
    suite, base = runner.load_suite(out / "evals.yaml")
    _, cases = load_suite_file(out / "evals.yaml")
    (check,) = runner._build_scorers(suite, base)
    (spec,) = suite.checks
    expected_check = ASSERTION_CHECKS[assertion["type"]]
    if assertion["type"] in ("equals", "not-equals", "is-json") and isinstance(assertion.get("value"), dict | list):
        expected_check = "json_schema"
    assert spec.type == expected_check
    expectations = cases[0].expectations
    feedback = check(outputs=reply, expectations=expectations) if spec.expectation else check(outputs=reply)
    assert feedback.value is verdict


def test_json_unicode_escapes_preserve_text_in_written_suites(tmp_path):
    source = tmp_path / "promptfooconfig.yaml"
    label = "🗺\x85map"
    source.write_text(
        json.dumps(
            {
                "prompts": ["Map legend"],
                "providers": ["echo"],
                "tests": [
                    {"providerOutput": label, "assert": [{"type": "levenshtein", "value": label, "threshold": 0}]},
                ],
            }
        ),
        encoding="utf-8",
    )
    out = tmp_path / "converted"
    write_conversion(convert_promptfoo_config(source, output=out), out, strict=True)
    suite, (case,) = load_suite_file(out / "evals.yaml")
    assert case.outputs == label and suite.scorers[0].parameters["values"] == [label]


def test_csv_edit_distance_shorthand_preserves_its_default_and_explicit_threshold(tmp_path):
    (tmp_path / "distances.csv").write_text(
        "__providerOutput,__expected\nPariss,levenshtein:Paris\nPariss,levenshtein(1):Paris\n", encoding="utf-8"
    )
    source = config_file(tmp_path, {"prompts": ["Map label"], "providers": ["echo"], "tests": "distances.csv"})
    out = tmp_path / "converted"
    write_conversion(convert_promptfoo_config(source, output=out), out, strict=True)
    suites = [load_suite_file(out / filename)[0] for filename in ("evals.yaml", "evals-2.yaml")]
    assert [suite.scorers[0].parameters["max_edits"] for suite in suites] == [0, 1]


def test_schema_file_is_pinned_during_conversion_and_stays_unverified_in_archives(tmp_path):
    schema = tmp_path / "map-schema.yaml"
    schema.write_text("type: object\nrequired: [city]\n", encoding="utf-8")
    assertion = {"type": "contains-json", "value": "file://map-schema.yaml"}
    source = config_file(
        tmp_path,
        {"prompts": ["Map label"], "providers": ["echo"], "tests": [{"assert": [assertion]}]},
    )
    out = tmp_path / "converted"
    write_conversion(convert_promptfoo_config(source, output=out), out, strict=True)
    schema.write_text("type: object\nrequired: [country]\n", encoding="utf-8")
    converted, _ = load_suite_file(out / "evals.yaml")
    assert converted.scorers[0].parameters["schema"] == {"type": "object", "required": ["city"]}
    assert recorded_checks([assertion], None, {}) is None


@pytest.mark.parametrize(
    ("assertion", "line"),
    [
        ({"type": "javascript", "value": "output.length < 9"}, "javascript: runs custom code"),
        ({"type": "python", "value": "file://check.py"}, "write the rule as a custom Python scorer"),
        ({"type": "ruby", "value": "true"}, "`def scorer(inputs, outputs, expectations)`"),
        ({"type": "webhook", "value": "https://x.test"}, "with `type: python` (`file:` and `function:`)"),
        ({"type": "trajectory:tool-used", "value": "search"}, "the closest check is tool_called"),
        ({"type": "trajectory:tool-sequence", "value": ["a"]}, "the closest check is tool_trajectory"),
        ({"type": "trajectory:step-count", "value": {"max": 3}}, "the closest check is step_count"),
        ({"type": "is-valid-openai-tools-call"}, "against the schemas of the provider's own tools or functions"),
        ({"type": "tool-call-f1", "value": ["a", "b"]}, "only when the two sets are equal"),
        ({"type": "trajectory:tool-args-match", "value": {"name": "a"}}, "(add tool_called by hand to use it)"),
        ({"type": "trajectory:tool-sequence", "value": ["a", "b"]}, "in parallel in one turn are in order"),
        ({"type": "finish-reason", "value": "stop"}, "reads the provider's trace or metadata"),
        ({"type": "contains-json", "value": "file://schema.js"}, "needs a literal local .json, .yaml or .yml schema"),
        ({"type": "is-xml", "value": {"requiredElements": "catalog"}}, "needs an object with a requiredElements list"),
        ({"type": "levenshtein", "value": "Paris", "threshold": True}, "levenshtein: needs a finite nonnegative"),
        ({"type": "word-count", "value": {"min": 2, "max": 1}}, "word-count: has a min greater than max"),
        ({"type": "word-count", "value": 1.5}, "word-count: needs whole-number counts"),
        ({"type": "is-refusal"}, "is-refusal: passes only a refusal; no built-in check does"),
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
        ({"type": "not-latency", "threshold": 500}, "not-latency: passes when latency fails; no built-in check does"),
        ({"type": "not-contains-all", "value": ["a", "b"]}, "not-contains-all: passes when contains-all fails"),
        ({"type": "made-up"}, "made-up: is not an assertion type in promptfoo's documented list"),
        ({"type": "icontains", "value": "a", "transform": "output.trim()"}, ".transform: transforms the reply"),
        ({"type": "llm-rubric", "value": "Polite", "threshold": "high"}, "llm-rubric: needs a finite"),
        ({"type": "equals", "value": "file://expected.json"}, "equals: reads its value from a file"),
        ({"type": "regex", "value": "(?<name>x)"}, "regex: is not a regular expression Python reads"),
        ({"type": "icontains", "value": "{{ name | upper }}"}, "icontains: uses Nunjucks beyond"),
        (
            {"type": "is-json", "value": {"type": "object", "properties": {"a": {"format": "email"}}}},
            "is-json: enforces a schema format",
        ),
        ({"type": "is-json", "value": {"type": "string", "pattern": r"^\w+$"}}, "is-json: uses a schema pattern"),
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
    assert {"javascript", "is-html", "similar", "python", "is-refusal", "not-is-refusal"} <= set(UNSUPPORTED)
    # No reason says custom code or tool calls have no equivalent: both name the check to use, and
    # none says it is still to come. No tool-call assertion matches a check exactly, so none converts.
    for kind in ("javascript", "python", "ruby", "webhook"):
        assert "custom Python scorer" in UNSUPPORTED[kind]
    for kind, (check, _) in _TOOL_CALLS.items():
        assert f"closest check is {check}" in UNSUPPORTED[kind]
        assert f"(add {check} by hand to use it)" in UNSUPPORTED[kind]
    assert not [kind for kind, why in UNSUPPORTED.items() if "release" in why]
    # The checks are the SDK's built-in ones, not a branch's.
    assert not [kind for kind, why in UNSUPPORTED.items() if "on main" in why]


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
    assert conversion.unconverted == []
    write_conversion(conversion, out)
    suite, base = runner.load_suite(out / "evals.yaml")
    (spec,) = suite.checks
    assert suite.case_gate is not None and suite.case_gate.metrics == {spec.label: "m"}
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
        "tests[1].vars.q: is a list, which promptfoo runs once per value; the test is not converted",
        "tests[2].vars.q: is read from a file, which this converter does not read; the test is not converted",
        "tests[3].assert[0] javascript: " + UNSUPPORTED["javascript"],
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


def test_strict_cli_reports_omissions_and_approximations_without_writing(tmp_path):
    config = config_file(
        tmp_path,
        {
            "prompts": ["{{reply}}"],
            "providers": ["echo"],
            "tests": [
                {
                    "vars": {"reply": "Ottawa"},
                    "assert": [
                        {"type": "contains", "value": "Ottawa"},
                        {"type": "regex", "value": r"^Ott.*"},
                        {"type": "is-html"},
                    ],
                },
            ],
        },
    )
    out = tmp_path / "converted"
    args = ["evals", "convert", "promptfoo", str(config), "--output", str(out)]
    result = CliRunner().invoke(app, [*args, "--strict"])
    assert result.exit_code == 2, result.output
    assert "tests[0].assert[2] is-html" in result.output
    assert "tests[0].assert[1] regex: uses regex syntax outside the portable subset" in result.output
    assert not out.exists()
    preview = CliRunner().invoke(app, [*args, "--dry-run"])
    assert preview.exit_code == 0 and "Would write" in preview.output and not out.exists()
    assert CliRunner().invoke(app, args).exit_code == 0
    suite = (out / "evals.yaml").read_text(encoding="utf-8")
    assert "# - tests[0].assert[2] is-html" in suite
    assert "# - tests[0].assert[1] regex: uses regex syntax outside the portable subset" in suite


def test_file_backed_tests_order_and_default_assertions_are_preserved(tmp_path):
    (tmp_path / "defaults.yaml").write_text("assert:\n  - type: contains\n    value: approved\n", encoding="utf-8")
    (tmp_path / "dataset.yaml").write_text(
        "- vars: {reply: on}\n  options: {disableDefaultAsserts: true}\n  assert: [{type: equals, value: 'on'}]\n",
        encoding="utf-8",
    )
    (tmp_path / "dataset.json").write_text('[{"vars":{"reply":"approved JSON"}}]', encoding="utf-8")
    (tmp_path / "dataset.jsonl").write_text(
        '\n{"vars":{"reply":"ignored"},"providerOutput":"approved JSONL"}\n', encoding="utf-8"
    )
    (tmp_path / "dataset.csv").write_text(
        'reply,__providerOutput,__expected\nignored,"approved, csv","starts-with:approved"\n', encoding="utf-8"
    )
    config = config_file(
        tmp_path,
        {
            "prompts": ["{{reply}}"],
            "targets": [{"id": "echo", "label": "offline"}, "other"],
            "defaultTest": "file://defaults.yaml",
            "tests": [
                {"vars": {"reply": "approved inline\n"}},
                "file://dataset.yaml",
                "dataset.json",
                "dataset.jsonl",
                {"path": "dataset.csv"},
            ],
        },
    )
    out = tmp_path / "converted"
    ambiguous = convert_promptfoo_config(config, output=out)
    with pytest.raises(PromptfooConfigError, match="strict conversion"):
        write_conversion(ambiguous, out, strict=True)
    assert not out.exists()

    conversion = convert_promptfoo_config(config, output=out, provider="offline")
    assert not conversion.unconverted
    write_conversion(conversion, out, strict=True)
    row_text: dict[str, str] = {}
    for suite in conversion.suites:
        manifest, cases = load_suite_file(out / suite.suite_file)
        assert manifest.target is not None and manifest.target.kind == "stored"
        row_text.update((case.id, case.outputs) for case in cases)
        summary = run_suite(out / suite.suite_file, home=tmp_path / "evals-home")
        assert summary["passed"] == suite.cases and summary["failed"] == summary["errors"] == 0
    assert row_text == {
        "test 1": "approved inline",
        "test 2": "on",
        "test 3": "approved JSON",
        "test 4": "approved JSONL",
        "test 5": "approved, csv",
    }


def test_malformed_source_has_a_file_and_line_diagnostic(tmp_path):
    config = config_file(tmp_path, {"prompts": ["plain"], "providers": ["echo"], "tests": "data.yaml"})
    (tmp_path / "data.yaml").write_text("- vars:\n    city: Oslo\n    city: Paris\n", encoding="utf-8")
    with pytest.raises(PromptfooConfigError, match="data.yaml: invalid YAML at line 3: duplicate mapping key"):
        convert_promptfoo_config(config, output=tmp_path / "converted")


def test_weighted_conversion_inherits_threshold_and_preserves_assertion_set_scores(tmp_path):
    checks = [
        {"type": "equals", "value": "answer", "weight": 0},
        {
            "type": "assert-set",
            "threshold": 0.8,
            "weight": 2,
            "assert": [
                {"type": "contains", "value": "correct", "weight": 3},
                {"type": "contains", "value": "extra"},
            ],
        },
        {"type": "starts-with", "value": "correct"},
    ]
    config = config_file(
        tmp_path,
        {
            "prompts": ["{{text}}"],
            "providers": ["echo"],
            "defaultTest": {
                "threshold": 0.8,
                "assert": [{"type": "contains", "value": "default assertion to omit"}],
            },
            "tests": [
                {
                    "vars": {"text": "correct"},
                    "options": {"disableDefaultAsserts": True},
                    "assert": checks,
                },
                {
                    "vars": {"text": "correct"},
                    "threshold": 0.9,
                    "options": {"disableDefaultAsserts": True},
                    "assert": checks,
                },
            ],
        },
    )
    out = tmp_path / "converted"
    conversion = convert_promptfoo_config(config, output=out)
    write_conversion(conversion, out, strict=True)
    assert [suite.tests for suite in conversion.suites] == [(0,), (1,)]
    for converted, expected, threshold in zip(conversion.suites, (True, False), (0.8, 0.9), strict=True):
        path = out / converted.suite_file
        suite, _ = load_suite_file(path)
        assert suite.case_gate is not None and suite.case_gate.threshold == threshold
        assert suite.case_gate.weights == {"equals": 0, "contains": 3}
        assert suite.case_gate.assertion_sets[0].checks == ["contains", "contains_2"]
        summary = run_suite(path, home=tmp_path / "evals-home")
        record = json.loads((Path(summary["output"]) / "results.jsonl").read_text(encoding="utf-8"))
        assert record["passed"] is expected
        assert record["checks"]["equals"]["passed"] is False
        assert record["checks"]["case_gate"]["value"] == pytest.approx(5 / 6)
        assert record["checks"]["case_gate"]["metadata"]["assertion_sets.assert_set.score"] == "0.75"


def test_partial_weighted_cases_cannot_turn_into_weaker_gates(tmp_path):
    config = config_file(
        tmp_path,
        {
            "prompts": ["matches"],
            "providers": ["echo"],
            "tests": [
                {
                    "threshold": 0.5,
                    "assert": [{"type": "equals", "value": "matches"}, {"type": "is-html", "weight": 3}],
                },
                {"assert": [{"type": "equals", "value": "matches"}]},
            ],
        },
    )
    out = tmp_path / "converted"
    conversion = convert_promptfoo_config(config, output=out)
    assert [suite.tests for suite in conversion.suites] == [(1,)]
    with pytest.raises(PromptfooConfigError, match="weighted test is skipped"):
        write_conversion(conversion, out, strict=True)
    assert not out.exists()
