"""The scorers the console offers by name, and the code scorers the worker runs itself.

A port of ``code_score`` and ``exact_match`` in the server's ``app/evals_workbench.py`` and
``app/evals_items.py`` (MartianSecurity/MartianBackend). Code scorers read only the saved output and the
case's expected text; the judge scorers need a model and go to the worker's judge.
"""

from __future__ import annotations

import json
from typing import Any

from martian.evals._custom import INSTALLED_TYPES
from martian.evals.model import ScorerVersion, Value

CODE_SCORERS = ("exact_match", "contains_expected", "valid_json", "response_length")
JUDGE_SCORERS = ("correctness", "safety", "policy")
BUILTIN_SCORERS = CODE_SCORERS + JUDGE_SCORERS
BUILTIN_VERSION = "v1"


def builtin_scorer(name: str, *, guidelines: str = "") -> ScorerVersion:
    """A scorer the console offers by name. The policy judge keeps its guidelines in ``criteria``."""
    if name not in BUILTIN_SCORERS:
        raise ValueError(f"unknown scorer {name!r}")
    return ScorerVersion(
        name=name,
        kind="check" if name in CODE_SCORERS else "llm_judge",
        type=name,
        version=BUILTIN_VERSION,
        criteria={"guidelines": guidelines} if name == "policy" else {},
    )


def is_builtin(scorer: ScorerVersion) -> bool:
    return scorer.name in BUILTIN_SCORERS and scorer.type == scorer.name


def scorer_source(scorer: ScorerVersion) -> str:
    """The ``source_id`` of a code score, and of a judge score that has no verdict. A custom
    Python scorer's is ``python:<name>`` and an installed check's ``installed:<name>``: the worker's
    setup installs the function under that name."""
    if is_builtin(scorer):
        return f"martian.{scorer.name}:{BUILTIN_VERSION}"
    if scorer.kind == "code" and scorer.type in INSTALLED_TYPES:
        return f"installed:{scorer.name}"
    if scorer.kind == "code":
        return f"python:{scorer.name}"
    return f"{scorer.name}@{scorer.version}"


def exact_match(actual: str, expected: str) -> bool:
    return actual.strip().casefold() == expected.strip().casefold()


def _refuse_constant(value: str) -> Any:
    raise ValueError(f"non-finite JSON number {value}")


def code_score(name: str, output: str, expected: str) -> tuple[Value, str]:
    """A code scorer's value and rationale for one saved output."""
    if name == "exact_match":
        matched = exact_match(output, expected)
        return matched, (
            "Output equals the expectation after stripping whitespace and case folding."
            if matched
            else "Output differs from the expectation after stripping whitespace and case folding."
        )
    if name == "contains_expected":
        found = bool(expected.strip()) and expected.strip().casefold() in output.casefold()
        return found, "Expected text is present in the output." if found else "Expected text is absent from the output."
    if name == "valid_json":
        try:
            json.loads(output, parse_constant=_refuse_constant)
            valid = True
        except (ValueError, TypeError, RecursionError):
            valid = False
        return valid, "Output is valid JSON." if valid else "Output is not valid JSON."
    if name == "response_length":
        return len(output), "Number of Unicode characters in the saved output."
    raise ValueError(f"{name!r} is not a code scorer")
