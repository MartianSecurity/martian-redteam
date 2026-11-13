"""Deterministic checks: no model call, the same answer every time.

Use these for anything that can be decided by looking at the text ("the reply must not contain
the secret", "the reply must be JSON"), and keep the LLM judges for questions of meaning.

Each factory returns an MLflow scorer for ``evaluate(..., scorers=[...])``. Values to look for
are given when the scorer is created, or per row through ``expectation="<key>"``, which reads
``expectations[<key>]`` (one string or a list of strings).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from typing import Any

from mlflow.entities import Feedback
from mlflow.genai.scorers import Scorer, scorer

from martian.evals.scorers import output_text


def _values(fixed: Sequence[str], expectation: str | None, expectations: dict[str, Any] | None) -> list[str]:
    found = list(fixed)
    if expectation is not None:
        given = (expectations or {}).get(expectation)
        if isinstance(given, str):
            found.append(given)
        elif isinstance(given, (list, tuple)) and all(isinstance(item, str) for item in given):
            found.extend(given)
        else:
            raise ValueError(f"expectations.{expectation} must be a string or a list of strings")
    if not found or not all(isinstance(value, str) and value for value in found):
        raise ValueError("a check needs at least one nonempty string to look for")
    return found


def _configured(name: str, fixed: Sequence[str], expectation: str | None) -> None:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("a check needs a nonempty name")
    if expectation is not None and (not isinstance(expectation, str) or not expectation.strip()):
        raise ValueError("expectation must be a nonempty expectations key")
    if expectation == name:
        raise ValueError("the check's name must differ from its expectations key")
    if not fixed and expectation is None:
        raise ValueError("give the values to look for, or expectation='<key>' to read them from each row")


def _scorer(name: str, expectation: str | None, decide: Callable[[str, dict[str, Any] | None], Feedback]) -> Scorer:
    """Declare ``expectations`` only when it is read, so rows without expectations still score."""
    if expectation is None:

        @scorer(name=name)
        def check(outputs: Any) -> Feedback:
            return decide(output_text(outputs), None)

        return check

    @scorer(name=name)
    def check_row(outputs: Any, expectations: dict[str, Any]) -> Feedback:
        return decide(output_text(outputs), expectations)

    return check_row


def contains(
    *values: str,
    expectation: str | None = None,
    name: str = "contains",
    any_of: bool = False,
    case_sensitive: bool = False,
) -> Scorer:
    """Pass when the output contains every value (or, with ``any_of=True``, at least one)."""
    _configured(name, values, expectation)

    def decide(text: str, expectations: dict[str, Any] | None) -> Feedback:
        wanted = _values(values, expectation, expectations)
        haystack = text if case_sensitive else text.casefold()
        missing = [v for v in wanted if (v if case_sensitive else v.casefold()) not in haystack]
        passed = len(missing) < len(wanted) if any_of else not missing
        return Feedback(
            value=passed,
            rationale="all values found" if not missing else f"{len(missing)} of {len(wanted)} values not found",
        )

    return _scorer(name, expectation, decide)


def not_contains(
    *values: str,
    expectation: str | None = None,
    name: str = "not_contains",
    case_sensitive: bool = False,
) -> Scorer:
    """Pass when the output contains none of the values. A refusal that quotes one still fails."""
    _configured(name, values, expectation)

    def decide(text: str, expectations: dict[str, Any] | None) -> Feedback:
        forbidden = _values(values, expectation, expectations)
        haystack = text if case_sensitive else text.casefold()
        found = [v for v in forbidden if (v if case_sensitive else v.casefold()) in haystack]
        return Feedback(
            value=not found,
            rationale="no forbidden value found"
            if not found
            else f"{len(found)} of {len(forbidden)} forbidden values found",
        )

    return _scorer(name, expectation, decide)


def _pattern(pattern: str, flags: int) -> re.Pattern[str]:
    try:
        return re.compile(pattern, flags)
    except re.error as error:
        raise ValueError(f"invalid regular expression: {error}") from None


def matches(pattern: str, *, name: str = "matches", flags: int = 0) -> Scorer:
    """Pass when the regular expression is found anywhere in the output (``re.search``)."""
    _configured(name, (pattern,), None)
    compiled = _pattern(pattern, flags)

    def decide(text: str, expectations: dict[str, Any] | None) -> Feedback:
        found = compiled.search(text) is not None
        return Feedback(value=found, rationale="pattern found" if found else "pattern not found")

    return _scorer(name, None, decide)


def not_matches(pattern: str, *, name: str = "not_matches", flags: int = 0) -> Scorer:
    """Pass when the regular expression is found nowhere in the output."""
    _configured(name, (pattern,), None)
    compiled = _pattern(pattern, flags)

    def decide(text: str, expectations: dict[str, Any] | None) -> Feedback:
        found = compiled.search(text) is not None
        return Feedback(value=not found, rationale="pattern found" if found else "pattern not found")

    return _scorer(name, None, decide)


def is_json(*, required_keys: Sequence[str] = (), name: str = "is_json") -> Scorer:
    """Pass when the output is valid JSON; with ``required_keys``, a JSON object holding each key.

    An output that is already a dict or list counts as JSON. One enclosing Markdown code fence
    around JSON text is accepted, because many models add it.
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError("a check needs a nonempty name")
    keys = list(required_keys)
    if not all(isinstance(key, str) and key for key in keys):
        raise ValueError("required_keys must be nonempty strings")

    @scorer(name=name)
    def check(outputs: Any) -> Feedback:
        value = outputs
        if isinstance(outputs, str):
            text = outputs.strip()
            fence, _, body = text.partition("\n")
            if fence.strip().lower() in {"```", "```json"} and body.rstrip().endswith("```"):
                text = body.rstrip()[:-3]
            try:
                value = json.loads(text)
            except ValueError:
                return Feedback(value=False, rationale="the output is not valid JSON")
        if not keys:
            return Feedback(value=True, rationale="valid JSON")
        if not isinstance(value, dict):
            return Feedback(value=False, rationale="the output is JSON but not an object")
        missing = [key for key in keys if key not in value]
        return Feedback(
            value=not missing,
            rationale="all required keys present" if not missing else f"missing keys: {', '.join(missing)}",
        )

    return check
