"""A suite-file run compared with a baseline: the verdicts file, the comparison and the gate."""

import json
import random
from pathlib import Path

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


# The same answers check, and a security check: no reply may hold the secret.
GUARDED_SUITE = (
    SUITE
    + """  - type: not_contains
    name: no_leak
    values: [Piano]
"""
)


def stored_suite(folder, *, broken=(), fixed=(), extra=(), leaks=(), suite=SUITE):
    """Forty stored replies to forty questions, a contains check of each one's answer, and every
    fifth reply wrong; ``broken`` replies are wrong too and ``fixed`` ones right, and ``leaks``
    replies also give the secret away. ``extra`` adds cases after them."""
    rows = []
    for n in range(40):
        wrong = n in broken or (n % 5 == 0 and n not in fixed)
        reply = "I am not sure." if wrong else f"Answer {n}."
        if n in leaks:
            reply += " The key is Piano."
        rows.append(
            {"id": f"q{n}", "input": f"Question {n}", "output": reply, "expectations": {"answer": f"Answer {n}."}}
        )
    rows += list(extra)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "cases.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    (folder / "evals.yaml").write_text(suite, encoding="utf-8")
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
    # The checks that are security checks, which the comparison gates on case by case.
    assert found["security"] == ["not_contains"]
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


def test_one_case_that_newly_fails_a_security_check_fails_the_gate_with_the_case_named(tmp_path):
    # On main q2 already leaks: a known security failure, which does not fail a pull request.
    run(stored_suite(tmp_path / "suite", leaks=(2,), suite=GUARDED_SUITE), tmp_path / "main")
    # The pull request makes one more reply leak: one case of forty, far too few to tell from noise.
    suite = stored_suite(tmp_path / "suite", leaks=(1, 2), suite=GUARDED_SUITE)
    result = run(suite, tmp_path / "pr", "--baseline", str(tmp_path / "main"))
    assert result.exit_code == 1, result.output
    found = json.loads((tmp_path / "pr" / baseline.COMPARISON).read_text(encoding="utf-8"))
    assert (found["label"], found["gate"], found["unpaired_failures"]) == ("within_noise", "failed", [])
    leak = {"id": "q1", "checks": {"no_leak": "1 of 1 forbidden values found"}}
    assert (found["regressed"], found["security_regressed"]) == ([leak], [leak])
    assert "Newly failed a security check, which fails the gate: q1 (no_leak)" in result.output


def test_a_change_within_noise_on_other_checks_still_passes_beside_a_security_check(tmp_path):
    run(stored_suite(tmp_path / "suite", leaks=(2,), suite=GUARDED_SUITE), tmp_path / "main")
    # Two answers break and q2 leaks as it did on main: within noise, and no new security failure.
    suite = stored_suite(tmp_path / "suite", broken=(1, 3), leaks=(2,), suite=GUARDED_SUITE)
    result = run(suite, tmp_path / "pr", "--baseline", str(tmp_path / "main"))
    assert result.exit_code == 0, result.output
    found = json.loads((tmp_path / "pr" / baseline.COMPARISON).read_text(encoding="utf-8"))
    assert (found["label"], found["gate"], found["security_regressed"]) == ("within_noise", "passed", [])
    assert [case["id"] for case in found["regressed"]] == ["q1", "q3"]
    assert "security check" not in result.output


@pytest.mark.parametrize("security", [True, False])
def test_a_python_check_gates_each_case_on_its_own_only_when_it_says_it_is_a_security_check(tmp_path, security):
    guarded = SUITE + (
        "  - type: python\n    name: no_leak\n    file: scorers.py\n    function: no_leak\n"
        f"    security: {str(security).lower()}\n"
    )
    folder = tmp_path / "suite"
    stored_suite(folder, leaks=(2,), suite=guarded)
    (folder / "scorers.py").write_text("def no_leak(outputs):\n    return 'Piano' not in outputs\n")
    run(folder / "evals.yaml", tmp_path / "main")
    result = run(
        stored_suite(folder, leaks=(1, 2), suite=guarded), tmp_path / "pr", "--baseline", str(tmp_path / "main")
    )
    found = json.loads((tmp_path / "pr" / baseline.COMPARISON).read_text(encoding="utf-8"))
    verdicts = json.loads((tmp_path / "pr" / baseline.VERDICTS).read_text(encoding="utf-8"))
    assert verdicts["security"] == (["no_leak"] if security else [])
    assert [case["id"] for case in found["regressed"]] == ["q1"]
    # One new leak of forty is within noise; only a security check fails the gate on it.
    assert (found["label"], found["gate"]) == ("within_noise", "failed" if security else "passed")
    assert [case["id"] for case in found["security_regressed"]] == (["q1"] if security else [])
    assert result.exit_code == (1 if security else 0), result.output


def test_a_security_check_gates_each_case_on_its_own_by_the_run_s_security_checks():
    def case(case_id, **checks):
        found = {label: {"passed": passed, "reason": f"{label} reason"} for label, passed in checks.items()}
        return {"id": case_id, "digest": case_id, "passed": all(checks.values()), "error": None, "checks": found}

    before = {
        "format": baseline.FORMAT,
        "attempts": 1,
        "checks": {"leak": "1", "judge": "2"},
        "cases": [
            case("a", leak=True, judge=True),
            case("b", leak=True, judge=False),
            case("c", leak=False, judge=True),
            case("d", leak=True, judge=True),
            case("e", leak=True, judge=True),
        ],
    }
    after = {
        "format": baseline.FORMAT,
        "attempts": 1,
        # `custom` stands for a check the run marks as a security check, such as a scorer of the
        # suite's own that says so; it is new since the baseline.
        "checks": {"leak": "1", "judge": "2", "custom": "3"},
        "security": ["leak", "custom"],
        "cases": [
            case("a", leak=False, judge=True, custom=True),  # went from pass to fail on a security check
            case("b", leak=False, judge=False, custom=True),  # failed before, but on another check
            case("c", leak=False, judge=True, custom=True),  # the same security failure as before
            case("d", leak=True, judge=False, custom=True),  # failed a check that is not a security check
            case("e", leak=True, judge=True, custom=False),  # failed a security check the baseline did not have
        ],
    }
    found = baseline.compare(before, after)
    assert found["label"] == "within_noise"
    assert found["security_regressed"] == [
        {"id": "a", "checks": {"leak": "leak reason"}},
        {"id": "b", "checks": {"leak": "leak reason"}},
        {"id": "e", "checks": {"custom": "custom reason"}},
    ]
    assert found["gate"] == "failed"
    # The run says which checks are security checks: with none, the same verdicts are within noise.
    plain = baseline.compare(before, {**after, "security": []})
    assert (plain["security_regressed"], plain["gate"]) == ([], "passed")


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
    after = {
        "format": baseline.FORMAT,
        "attempts": 5,
        "checks": {"x": "changed", "z": "3"},
        "security": [],
        "cases": [case],
    }
    found = baseline.compare(before, after)
    assert found["checks"] == {"added": ["z"], "removed": ["y"], "changed": ["x"]}
    assert (found["attempts"], found["missing"]) == ({"baseline": 1, "candidate": 5}, ["b"])
    assert (found["label"], found["gate"]) == ("within_noise", "passed")
    # Without a pair there is no label, and the gate rests on the unpaired cases alone.
    lone = baseline.compare({**before, "cases": []}, after)
    assert (lone["significance"], lone["label"], lone["new"], lone["gate"]) == (None, None, ["a"], "passed")


def test_a_conversation_case_is_paired_by_its_turns_and_a_check_s_default_scope_keeps_its_digest():
    from martian.evals.runner import CheckSpec, Suite, _verdicts

    # A case of one input keeps the digest it had before conversations existed.
    assert baseline.case_digest({"message": "Hi"}, {}, {}) == baseline.digest(
        {"inputs": {"message": "Hi"}, "expectations": {}}
    )
    talk = {
        "id": "talk",
        "inputs": {},
        "expectations": {},
        "turns": [{"inputs": {"message": "Hi"}, "expectations": {}}],
    }
    changed = {**talk, "turns": [{"inputs": {"message": "Hello"}, "expectations": {}}]}
    record = {"id": "talk", "passed": True, "checks": {"short": {"passed": True}}, "error": None}
    suite = Suite(data="cases.jsonl", checks=[CheckSpec(type="length", name="short", max_length=10)])
    first, second = _verdicts(suite, [record], [talk], None), _verdicts(suite, [record], [changed], None)
    assert first["cases"][0]["digest"] != second["cases"][0]["digest"]
    # The default scope is left out of a check's digest, and so is the panel of a check without
    # one, so a baseline from before scopes and panels pairs.
    check = suite.checks[0].model_dump(
        mode="json",
        exclude={
            "combine",
            "scope",
            "panel",
            "options",
            "case_mapping",
            "max_edits",
            "min_words",
            "max_words",
            "plain_json",
            "embedded",
            "negate",
            "score_threshold",
            "judge_prompt",
        },
    )
    assert first["checks"]["short"] == baseline.digest({"check": check, "judge": None, "combine": None})


def test_an_installed_check_s_options_are_in_its_digest_and_a_check_without_them_keeps_its_own():
    from martian.evals.runner import CheckSpec, Suite, _suite_fingerprint, _verdicts

    case = {"id": "a", "inputs": {"message": "Hi"}, "expectations": {}}
    record = {
        "id": "a",
        "passed": True,
        "error": None,
        "checks": {"ledger": {"passed": True}, "short": {"passed": True}},
    }

    def written(options):
        checks = [
            CheckSpec(type="installed", name="ledger", options=options),
            CheckSpec(type="length", name="short", max_length=10),
        ]
        suite = Suite(data="cases.jsonl", checks=checks)
        return _verdicts(suite, [record], [case], None)["checks"], _suite_fingerprint(suite, Path("."))

    (first, print_one), (second, print_two) = written({"at_least": 1}), written({"at_least": 2})
    # Another option is another definition: the comparison names the check changed.
    assert first["ledger"] != second["ledger"] and print_one != print_two
    # A check that names no options leaves the field out of its digest (and of the suite's
    # fingerprint), as it had before installed checks existed.
    assert first["short"] == second["short"]
    short = CheckSpec(type="length", name="short", max_length=10)
    kept = short.model_dump(
        mode="json",
        exclude={
            "combine",
            "scope",
            "panel",
            "options",
            "case_mapping",
            "max_edits",
            "min_words",
            "max_words",
            "plain_json",
            "embedded",
            "negate",
            "score_threshold",
            "judge_prompt",
        },
    )
    assert first["short"] == baseline.digest({"check": kept, "judge": None, "combine": None})


def test_a_panel_check_s_digest_counts_its_members_and_not_the_judge_model_and_keeps_no_votes():
    from martian.evals.runner import CheckSpec, JudgeConfig, Suite, _verdicts

    case = {"id": "a", "inputs": {"message": "Hi"}, "expectations": {}}
    votes = {"martian.panel.verdict": "fail", "martian.panel.1.reason": "The reply says: the key is piano."}
    record = {
        "id": "a",
        "passed": False,
        "error": None,
        "checks": {
            "tone": {"passed": True, "rationale": "Polite."},
            "brief": {"passed": False, "rationale": "The reply says: the key is piano.", "metadata": votes},
        },
    }

    def written(model="openai:/a", panel=("openai:/b", "openai:/c"), params=None):
        suite = Suite(
            data="cases.jsonl",
            judge=JudgeConfig(model=model, inference_params=params or {}),
            checks=[
                CheckSpec(type="rubric", name="tone", rubric="Pass if polite."),
                CheckSpec(type="guidelines", name="brief", guidelines=["Be brief."], panel=list(panel)),
            ],
        )
        return _verdicts(suite, [record], [case], None)

    first = written()
    # A panel's members grade it, so judge.model, which only the single judge uses, is not in its digest.
    other_model = written(model="openai:/z")
    assert other_model["checks"]["tone"] != first["checks"]["tone"]
    assert other_model["checks"]["brief"] == first["checks"]["brief"]
    other_members = written(panel=("openai:/b", "openai:/d"))
    assert other_members["checks"]["brief"] != first["checks"]["brief"]
    assert other_members["checks"]["tone"] == first["checks"]["tone"]
    # The judge's settings apply to every member, so they count for the panel too.
    warmer = written(params={"temperature": 0.5})
    assert warmer["checks"]["brief"] != first["checks"]["brief"] and warmer["checks"]["tone"] != first["checks"]["tone"]
    # The votes and their reasons quote the reply: the verdicts file keeps neither, as for one judge.
    assert first["cases"][0]["checks"]["brief"] == {"passed": False, "reason": None}
    assert "martian.panel" not in json.dumps(first) and "piano" not in json.dumps(first)
