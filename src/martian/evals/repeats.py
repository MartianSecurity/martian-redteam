"""Repeated attempts of a case, and the rules that combine them into the case's verdict.

A suite can run each case more than once (``repeats``). A check's verdicts over a case's attempts
combine by a rule (:data:`~martian.evals.model.CombineRule`):

- ``all``: the check passes the case only when every attempt passed it;
- ``majority``: when more than half of the attempts passed it, so a tie fails;
- ``any``: when at least one attempt passed it.

An attempt with no verdict (the target call failed, or a judge gave no answer) is open. The
combined verdict is the rule's answer when that answer is the same whichever way the open attempts
would have gone, and none otherwise: under ``all`` one failed attempt fails the check whatever
the others did, and under ``any`` one passing attempt passes it. The case passes when every check,
combined, passes it, as with one attempt.

A check that names no rule of its own takes ``all`` when it is a security check, and the suite's
rule otherwise (:func:`rule_for`). The suite's rule is ``all`` unless the suite names another.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from martian.evals.model import CombineRule

RULES: tuple[CombineRule, ...] = ("all", "majority", "any")

RuleSource = Literal["check", "security", "suite"]
"""Where a check's rule came from: the check's own ``combine``, the security default, or the suite."""

_MEANING = {
    "all": "every attempt must pass",
    "majority": "more than half must pass",
    "any": "one passing attempt is enough",
}


def _holds(rule: CombineRule, passed: int, count: int) -> bool:
    if rule == "all":
        return passed == count
    if rule == "majority":
        return 2 * passed > count
    return passed > 0


def combine(verdicts: Sequence[bool | None], rule: CombineRule) -> bool | None:
    """The verdicts of one check over a case's attempts, combined by ``rule``. None when the
    attempts without a verdict could change the answer."""
    if not verdicts:
        raise ValueError("there is nothing to combine without an attempt")
    passed = sum(verdict is True for verdict in verdicts)
    unknown = sum(verdict is None for verdict in verdicts)
    worst, best = _holds(rule, passed, len(verdicts)), _holds(rule, passed + unknown, len(verdicts))
    return worst if worst == best else None


def representative(verdicts: Sequence[bool | None], verdict: bool | None) -> int:
    """The position of the attempt that stands for the case: the first whose verdict is the case's
    verdict, else the first. Its reply, target error and trace are the case's."""
    return next((position for position, found in enumerate(verdicts) if found is verdict), 0)


def describe(verdicts: Sequence[bool | None], rule: CombineRule) -> str:
    """How the attempts went and what the rule asks, such as ``4 of 5 attempts passed, 1 failed
    (all: every attempt must pass)``."""
    passed = sum(verdict is True for verdict in verdicts)
    failed = sum(verdict is False for verdict in verdicts)
    counts = f"{passed} of {len(verdicts)} attempts passed, {failed} failed"
    if unknown := len(verdicts) - passed - failed:
        counts += f", {unknown} had no verdict"
    return f"{counts} ({rule}: {_MEANING[rule]})"


def rule_for(own: CombineRule | None, *, security: bool, suite: CombineRule) -> tuple[CombineRule, RuleSource]:
    """The rule a check's attempts combine by, and where it came from: the check's own rule, else
    ``all`` for a security check, else the suite's rule."""
    if own is not None:
        return own, "check"
    if security:
        return "all", "security"
    return suite, "suite"


def plan(attempts: int, suite: CombineRule, checks: Sequence[tuple[str, CombineRule | None, bool]]) -> dict[str, Any]:
    """The repeats of a run as its summary and its run record state them: the attempts of each
    case, the suite's rule, and each check's rule and where it came from. ``checks`` holds each
    check's label, its own rule and whether it is a security check."""
    rules = {}
    for label, own, security in checks:
        rule, source = rule_for(own, security=security, suite=suite)
        rules[label] = {"combine": rule, "from": source}
    return {"attempts": attempts, "combine": suite, "checks": rules}


def combine_attempts(attempts: Sequence[Mapping[str, Any]], rules: Mapping[str, CombineRule]) -> dict[str, Any]:
    """One case's verdict, check verdicts, reply and target error from its attempts' records
    (``passed``, ``checks``, ``output`` and ``error`` each). Each check's verdicts combine by its
    rule in ``rules``; the reply and the target error are those of the :func:`representative`
    attempt."""
    checks: dict[str, dict[str, Any]] = {}
    for label, rule in rules.items():
        verdicts = [attempt["checks"][label]["passed"] for attempt in attempts]
        passed = combine(verdicts, rule)
        text = describe(verdicts, rule)
        checks[label] = {
            "passed": passed,
            "rationale": text if passed is not None else None,
            "error": None if passed is not None else f"the attempts without a verdict leave it open: {text}",
        }
    values = [check["passed"] for check in checks.values()]
    verdict = None if any(value is None for value in values) else all(values)
    chosen = attempts[representative([attempt["passed"] for attempt in attempts], verdict)]
    return {"passed": verdict, "checks": checks, "output": chosen["output"], "error": chosen["error"]}


__all__ = ["RULES", "RuleSource", "combine", "combine_attempts", "describe", "plan", "representative", "rule_for"]
