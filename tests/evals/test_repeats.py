"""The rules that combine a case's attempts: each rule's answer, attempts without a verdict, where a
check's rule comes from, and which attempt stands for the case; and over the attempts' own verdicts,
whether a case passed every attempt and whether it is flaky."""

import pytest

from martian.evals.repeats import (
    combine,
    combine_attempts,
    describe,
    flaky,
    passed_every_attempt,
    plan,
    reliability,
    representative,
    rule_for,
)

T, F, E = True, False, None  # an attempt that passed, failed, or has no verdict


@pytest.mark.parametrize(
    ("verdicts", "all_", "majority", "any_"),
    [
        ([T, T, T, T, T], T, T, T),
        ([T, T, T, T, F], F, T, T),  # one failure in five
        ([F, F, F, F, T], F, F, T),
        ([F, F, F, F, F], F, F, F),
        ([T, T, F, F], F, F, T),  # a tie is not a majority
        ([T], T, T, T),  # one attempt: every rule agrees
        ([F], F, F, F),
        ([E], E, E, E),
        # Without a verdict, an attempt could have gone either way: the answer stands only when
        # it would be the same both ways.
        ([T, T, T, T, E], E, T, T),
        ([F, T, T, T, E], F, T, T),
        ([T, T, E, E, F], F, E, T),
        ([F, F, F, E, E], F, F, E),
        ([T, T, T, E, E], E, T, T),
    ],
)
def test_each_rule_combines_the_attempts(verdicts, all_, majority, any_):
    assert (combine(verdicts, "all"), combine(verdicts, "majority"), combine(verdicts, "any")) == (all_, majority, any_)


def test_there_is_nothing_to_combine_without_an_attempt():
    with pytest.raises(ValueError, match="without an attempt"):
        combine([], "all")


def test_a_check_names_its_rule_else_a_security_check_takes_all_else_the_suites():
    assert rule_for("any", security=True, suite="majority") == ("any", "check")
    assert rule_for(None, security=True, suite="any") == ("all", "security")
    assert rule_for(None, security=False, suite="majority") == ("majority", "suite")
    assert plan(5, "any", [("no_leak", None, True), ("polite", None, False), ("tone", "majority", False)]) == {
        "attempts": 5,
        "combine": "any",
        "checks": {
            "no_leak": {"combine": "all", "from": "security"},
            "polite": {"combine": "any", "from": "suite"},
            "tone": {"combine": "majority", "from": "check"},
        },
    }


def test_the_first_attempt_with_the_cases_verdict_stands_for_it():
    assert representative([T, F, T, F], F) == 1
    assert representative([T, E, F], E) == 1
    assert representative([F, F, T], T) == 2
    assert representative([F, E], T) == 0  # none has it: the first attempt


def test_the_combined_verdict_says_how_the_attempts_went_and_what_the_rule_asks():
    assert describe([T, T, T, T, F], "all") == "4 of 5 attempts passed, 1 failed (all: every attempt must pass)"
    assert describe([T, E, F], "majority") == (
        "1 of 3 attempts passed, 1 failed, 1 had no verdict (majority: more than half must pass)"
    )


def test_a_case_combines_each_check_by_its_own_rule():
    def attempt(leak, polite, output, error=None):
        checks = {"no_leak": {"passed": leak}, "polite": {"passed": polite}}
        values = [leak, polite]
        passed = None if None in values else all(values)
        return {"passed": passed, "checks": checks, "output": output, "error": error}

    attempts = [attempt(T, T, "fine"), attempt(F, F, "leak"), attempt(E, E, None, "the target call failed")]
    case = combine_attempts(attempts, {"no_leak": "all", "polite": "any"})
    # One leak fails the security check whatever the third attempt would have said; one polite
    # reply passes the other. The case shows the first attempt that failed.
    assert case["passed"] is False
    assert (case["checks"]["no_leak"]["passed"], case["checks"]["polite"]["passed"]) == (False, True)
    assert case["checks"]["no_leak"]["rationale"].startswith("1 of 3 attempts passed, 1 failed, 1 had no verdict")
    assert (case["output"], case["error"]) == ("leak", None)
    open_case = combine_attempts(attempts[:1] + attempts[2:], {"no_leak": "all", "polite": "any"})
    assert open_case["passed"] is None and open_case["checks"]["no_leak"]["rationale"] is None
    assert open_case["checks"]["no_leak"]["error"].startswith("the attempts without a verdict leave it open")
    assert (open_case["output"], open_case["error"]) == (None, "the target call failed")


@pytest.mark.parametrize(
    ("verdicts", "every", "changed"),
    [
        ([T, T, T], T, False),
        ([T, F, T], F, True),  # passed one attempt and failed another
        ([F, F, F], F, False),  # always wrong is not flaky
        ([T, T, E], E, False),  # an attempt without a verdict counts neither way
        ([F, T, E], F, True),
        ([F, E], F, False),
        ([T], T, False),
    ],
)
def test_a_case_passed_every_attempt_or_not_and_is_flaky_when_its_verdict_changed(verdicts, every, changed):
    assert (passed_every_attempt(verdicts), flaky(verdicts)) == (every, changed)


def test_reliability_counts_cases_and_lists_the_flaky_ones_in_order():
    cases = [("a", [T, T, T]), ("b", [T, F, T]), ("c", [F, F, F]), ("d", [T, E, T]), ("e", [E, F, T])]
    assert reliability(cases) == {"passed": 1, "failed": 3, "errors": 1, "flaky": ["b", "e"]}
    assert reliability([]) == {"passed": 0, "failed": 0, "errors": 0, "flaky": []}
