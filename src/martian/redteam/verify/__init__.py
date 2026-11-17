"""Verifiers and the red team verdict rules.

:mod:`martian.redteam.verify.rules` holds the rules every edition decides verdicts with, and
``vectors.json`` their test vectors. :func:`load_vectors` reads them and :func:`run_vector` applies
one to these rules, so another implementation (the hosted engine) can be checked against the same
file. The specification is ``docs/redteam-verdicts.md``.
"""

from __future__ import annotations

import json
from importlib import resources
from typing import Any

from martian.redteam.model import CheckResult, EvidenceLevel, ToolCall
from martian.redteam.verify.rules import (
    DETERMINISTIC,
    LEVEL_OF_KIND,
    REQUIRED_TIER,
    RULES_VERSION,
    TIER_RANK,
    Repetition,
    VerificationVerdict,
    argument_matches,
    attempt_outcome,
    canary_check,
    ceiling,
    confirmed,
    judge_result,
    label,
    text_check,
    tier_of,
    tool_call_check,
    verify,
)


def load_vectors() -> dict[str, Any]:
    """The test vectors that ship with the package: ``{"version", "description", "vectors"}``."""
    text = resources.files(__package__).joinpath("vectors.json").read_text(encoding="utf-8")
    loaded: dict[str, Any] = json.loads(text)
    return loaded


def run_vector(vector: dict[str, Any]) -> dict[str, Any]:
    """Apply one vector's input to these rules and return the output in the vector's shape."""
    rule, given = vector["rule"], vector["input"]
    if rule == "text_check":
        hit, _ = text_check(given["reply"], all_of=given["all_of"], none_of=given["none_of"], probe=given["probe"])
        return {"hit": hit}
    if rule == "canary_check":
        hit, _ = canary_check(given["reply"], canary=given["canary"], probe=given["probe"])
        return {"hit": hit}
    if rule == "tool_call_check":
        hit, detail = tool_call_check(
            [ToolCall.model_validate(call) for call in given["calls"]],
            tool=given["tool"],
            where=given.get("where", {}),
            executed=given.get("executed", False),
            observed=given.get("observed", True),
        )
        return {"hit": hit, "executed": detail["executed"]}
    if rule == "judge_result":
        return {"hit": judge_result(given["verdict"])}
    if rule == "attempt_outcome":
        results = [
            CheckResult(check_id=f"c{index}", kind=item["kind"], hit=item["hit"])
            for index, item in enumerate(given["results"])
        ]
        outcome, level = attempt_outcome(results)
        return {"outcome": outcome, "level": None if level is None else int(level)}
    if rule == "verify":
        repetitions = [
            Repetition(item["outcome"], None if item.get("level") is None else EvidenceLevel(item["level"]))
            for item in given["repetitions"]
        ]
        verdict = verify(
            given["objective"],
            repetitions,
            control=given.get("control"),
            required_successes=given.get("required_successes", 2),
            require_control=given.get("require_control", True),
            isolated=given.get("isolated", False),
        )
        return {
            "outcome": verdict.outcome,
            "reproduction_status": verdict.reproduction_status,
            "evidence_tier": verdict.evidence_tier,
            "evidence_level": None if verdict.evidence_level is None else int(verdict.evidence_level),
            "ceiling_applied": verdict.ceiling_applied,
            "negative_control_passed": verdict.negative_control_passed,
        }
    if rule == "ceiling":
        best, top = ceiling(given["objective"], environment=given["environment"])
        return {"outcome": best, "level": int(top)}
    if rule == "label":
        level = EvidenceLevel(given["level"])
        words = label(level, executed=given.get("executed"), objective=given.get("objective"))
        return {"label": words, "confirmed": confirmed(level)}
    raise ValueError(f"unknown rule {rule!r}")


__all__ = [
    "DETERMINISTIC",
    "LEVEL_OF_KIND",
    "REQUIRED_TIER",
    "RULES_VERSION",
    "TIER_RANK",
    "Repetition",
    "VerificationVerdict",
    "argument_matches",
    "attempt_outcome",
    "canary_check",
    "ceiling",
    "confirmed",
    "judge_result",
    "label",
    "load_vectors",
    "run_vector",
    "text_check",
    "tier_of",
    "tool_call_check",
    "verify",
]
