"""Deterministic checks: no model call, the same answer every time.

Use these for anything that can be decided by looking at the reply or at the recorded call ("the
reply must not contain the secret", "the reply must be JSON", "the call must take under two
seconds"), and keep the LLM judges for questions of meaning.

Each factory returns an MLflow scorer for ``evaluate(..., scorers=[...])``. Values to look for
are given when the scorer is created, or per row through ``expectation="<key>"``, which reads
``expectations[<key>]`` (one string or a list of strings). :func:`number_equals`,
:func:`sums_to`, :func:`reconciles` and :func:`table_equals` read the reply's numbers and tables
as exact decimals (:mod:`martian.evals._numeric`). :func:`latency` and :func:`cost` read the
trace MLflow records for the row's call instead of the reply. :func:`no_prohibited_phrases`,
:func:`no_personal_data`, :func:`no_secrets`, :func:`resists_injection` and :func:`no_refusal`
are the policy checks (:mod:`martian.evals._policy`). :func:`tool_called`,
:func:`tool_not_called`, :func:`tool_trajectory` and :func:`step_count` read the agent's tool
calls, model turn by model turn, from the trace of its call or from its reply
(:mod:`martian.evals._tool_calls`).
"""

from __future__ import annotations

import copy
import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
from typing import Any

from mlflow.entities import Feedback
from mlflow.genai.scorers import Scorer, scorer

from martian.evals import _json_schema, _numeric, _policy, _tool_calls
from martian.evals.scorers import output_text


def _values(
    fixed: Sequence[str], expectation: str | None, expectations: dict[str, Any] | None, *, stripped: bool = False
) -> list[str]:
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
    if stripped:
        # These checks ignore surrounding whitespace, so a blank value would match any output.
        found = [value.strip() for value in found]
        if not all(found):
            raise ValueError("a check needs values that are not only whitespace")
    return found


def _named(name: str) -> None:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("a check needs a nonempty name")


def _expectation_key(name: str, expectation: str | None) -> None:
    _named(name)
    if expectation is not None and (not isinstance(expectation, str) or not expectation.strip()):
        raise ValueError("expectation must be a nonempty expectations key")
    if expectation == name:
        raise ValueError("the check's name must differ from its expectations key")


def _configured(name: str, fixed: Sequence[str], expectation: str | None) -> None:
    _expectation_key(name, expectation)
    if not fixed and expectation is None:
        raise ValueError("give the values to look for, or expectation='<key>' to read them from each row")


def _configured_stripped(name: str, fixed: Sequence[str], expectation: str | None) -> None:
    """:func:`_configured` for a check that ignores surrounding whitespace: its fixed values are
    checked now, so a blank one is refused before any row runs."""
    _configured(name, fixed, expectation)
    if fixed:
        _values(fixed, None, None, stripped=True)


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


def _fold(text: str, case_sensitive: bool) -> str:
    return text if case_sensitive else text.casefold()


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _limit(name: str, value: Any, *, whole: bool = False, zero: bool = False) -> None:
    """A limit is a finite number above zero (or zero, where allowed); a count is a whole number."""
    if not _number(value) or (whole and not float(value).is_integer()) or value < 0 or (value == 0 and not zero):
        kind = "a whole number" if whole else "a number"
        raise ValueError(f"{name} must be {kind} {'of 0 or more' if zero else 'above 0'}")


# --- the reply's text -------------------------------------------------------------------------


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
        haystack = _fold(text, case_sensitive)
        missing = [v for v in wanted if _fold(v, case_sensitive) not in haystack]
        passed = len(missing) < len(wanted) if any_of else not missing
        if not missing:
            rationale = "all values found"
        elif any_of:
            rationale = f"{len(wanted) - len(missing)} of {len(wanted)} values found"
        else:
            rationale = f"{len(missing)} of {len(wanted)} values not found"
        return Feedback(value=passed, rationale=rationale)

    return _scorer(name, expectation, decide)


def contains_all(
    *values: str, expectation: str | None = None, name: str = "contains_all", case_sensitive: bool = False
) -> Scorer:
    """Pass when the output contains every value."""
    return contains(*values, expectation=expectation, name=name, case_sensitive=case_sensitive)


def contains_any(
    *values: str, expectation: str | None = None, name: str = "contains_any", case_sensitive: bool = False
) -> Scorer:
    """Pass when the output contains at least one of the values."""
    return contains(*values, expectation=expectation, name=name, any_of=True, case_sensitive=case_sensitive)


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
        haystack = _fold(text, case_sensitive)
        found = [v for v in forbidden if _fold(v, case_sensitive) in haystack]
        return Feedback(
            value=not found,
            rationale="no forbidden value found"
            if not found
            else f"{len(found)} of {len(forbidden)} forbidden values found",
        )

    return _scorer(name, expectation, decide)


def _whole_text(
    name: str,
    values: Sequence[str],
    expectation: str | None,
    case_sensitive: bool,
    test: Callable[[str, str], bool],
    what: str,
) -> Scorer:
    """A check of the whole output, without surrounding whitespace, against one value at a time."""
    _configured_stripped(name, values, expectation)

    def decide(text: str, expectations: dict[str, Any] | None) -> Feedback:
        accepted = _values(values, expectation, expectations, stripped=True)
        reply = _fold(text.strip(), case_sensitive)
        passed = any(test(reply, _fold(value, case_sensitive)) for value in accepted)
        return Feedback(
            value=passed,
            rationale=f"the output {what} one of the values"
            if passed
            else f"the output {what} none of the {len(accepted)} values",
        )

    return _scorer(name, expectation, decide)


def equals(*values: str, expectation: str | None = None, name: str = "equals", case_sensitive: bool = False) -> Scorer:
    """Pass when the output is one of the values, ignoring surrounding whitespace (and case,
    unless ``case_sensitive=True``)."""
    return _whole_text(name, values, expectation, case_sensitive, str.__eq__, "equals")


def starts_with(
    *values: str, expectation: str | None = None, name: str = "starts_with", case_sensitive: bool = False
) -> Scorer:
    """Pass when the output, without leading whitespace, starts with one of the values."""
    return _whole_text(name, values, expectation, case_sensitive, str.startswith, "starts with")


def ends_with(
    *values: str, expectation: str | None = None, name: str = "ends_with", case_sensitive: bool = False
) -> Scorer:
    """Pass when the output, without trailing whitespace, ends with one of the values."""
    return _whole_text(name, values, expectation, case_sensitive, str.endswith, "ends with")


def _edit_distance(one: str, other: str, limit: int) -> int | None:
    """The Levenshtein distance between two texts, or ``None`` when it is more than ``limit``.

    Only the cells within ``limit`` of the diagonal can hold a distance that small, so only they
    are computed: the time grows with the length of the texts times the limit.
    """
    if len(one) < len(other):
        one, other = other, one
    if len(one) - len(other) > limit:
        return None
    beyond = limit + 1
    previous = [column if column <= limit else beyond for column in range(len(other) + 1)]
    for row in range(1, len(one) + 1):
        current = [beyond] * (len(other) + 1)
        if row <= limit:
            current[0] = row
        character = one[row - 1]
        for column in range(max(1, row - limit), min(len(other), row + limit) + 1):
            current[column] = min(
                previous[column] + 1,
                current[column - 1] + 1,
                previous[column - 1] + (character != other[column - 1]),
                beyond,
            )
        if min(current) > limit:
            return None
        previous = current
    return previous[-1] if previous[-1] <= limit else None


def similarity(
    *values: str,
    min_similarity: float,
    expectation: str | None = None,
    name: str = "similarity",
    case_sensitive: bool = False,
) -> Scorer:
    """Pass when the output is at least ``min_similarity`` similar to one of the values.

    Similarity is one minus the edit (Levenshtein) distance divided by the length of the longer
    text: 1 for the same text, 0 for texts with no character in place. Surrounding whitespace is
    ignored, and case too unless ``case_sensitive=True``.
    """
    _configured_stripped(name, values, expectation)
    if not (_number(min_similarity) and 0 <= min_similarity <= 1):
        raise ValueError("min_similarity must be a number from 0 to 1")

    def decide(text: str, expectations: dict[str, Any] | None) -> Feedback:
        accepted = _values(values, expectation, expectations, stripped=True)
        reply = _fold(text.strip(), case_sensitive)
        best: tuple[float, int] | None = None
        for value in accepted:
            other = _fold(value, case_sensitive)
            longer = max(len(reply), len(other))
            # The most edits a text this long can take and still be similar enough. The small
            # margin keeps 0.2 * 10 from rounding down to one edit.
            allowed = math.floor((1 - min_similarity) * longer + 1e-9)
            distance = _edit_distance(reply, other, allowed)
            if distance is not None and (best is None or 1 - distance / longer > best[0]):
                best = (1 - distance / longer, distance)
        if best is None:
            return Feedback(value=False, rationale=f"less than {min_similarity} similar to each of the values")
        return Feedback(value=True, rationale=f"{best[0]:.3f} similar to the closest value ({best[1]} edits)")

    return _scorer(name, expectation, decide)


def length(*, min_length: int | None = None, max_length: int | None = None, name: str = "length") -> Scorer:
    """Pass when the output has at least ``min_length`` and at most ``max_length`` characters.

    The length is the number of Unicode characters of the output as recorded, whitespace
    included; a structured output counts as its JSON text.
    """
    _named(name)
    if min_length is None and max_length is None:
        raise ValueError("a length check needs min_length, max_length or both")
    for option, value in (("min_length", min_length), ("max_length", max_length)):
        if value is not None:
            _limit(option, value, whole=True, zero=True)
    if min_length is not None and max_length is not None and min_length > max_length:
        raise ValueError("min_length is more than max_length")

    def decide(text: str, expectations: dict[str, Any] | None) -> Feedback:
        size = len(text)
        short = min_length is not None and size < min_length
        long = max_length is not None and size > max_length
        bound = f"under the minimum of {min_length}" if short else f"over the maximum of {max_length}"
        return Feedback(
            value=not (short or long),
            rationale=f"{size} characters" if not (short or long) else f"{size} characters, {bound}",
        )

    return _scorer(name, None, decide)


# --- the reply as data ------------------------------------------------------------------------


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


def _parsed_json(outputs: Any, *, exact: bool = False) -> tuple[bool, Any]:
    """Whether the output is JSON, and its value. An output that is already a dict or list is
    JSON; one enclosing Markdown code fence around JSON text is accepted. ``exact`` reads JSON
    text's fractional numbers as ``Decimal``, as written."""
    if not isinstance(outputs, str):
        return True, outputs
    text = outputs.strip()
    fence, _, body = text.partition("\n")
    if fence.strip().lower() in {"```", "```json"} and body.rstrip().endswith("```"):
        text = body.rstrip()[:-3]
    try:
        return True, json.loads(text, parse_float=Decimal if exact else None)
    except ValueError:
        return False, None


def is_json(*, required_keys: Sequence[str] = (), name: str = "is_json") -> Scorer:
    """Pass when the output is valid JSON; with ``required_keys``, a JSON object holding each key.

    An output that is already a dict or list counts as JSON. One enclosing Markdown code fence
    around JSON text is accepted, because many models add it.
    """
    _named(name)
    keys = list(required_keys)
    if not all(isinstance(key, str) and key for key in keys):
        raise ValueError("required_keys must be nonempty strings")

    @scorer(name=name)
    def check(outputs: Any) -> Feedback:
        parsed, value = _parsed_json(outputs)
        if not parsed:
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


def json_schema(schema: Mapping[str, Any], *, name: str = "json_schema") -> Scorer:
    """Pass when the output is JSON that matches ``schema``, a JSON Schema.

    The output is read as :func:`is_json` reads it. The schema may use the keywords
    :mod:`martian.evals._json_schema` lists, which cover the usual structure, type, length, range
    and pattern constraints and ``$ref`` into ``$defs``; any other keyword is refused here, when
    the check is created. ``format`` is not checked, as in the JSON Schema standard's default.
    """
    _named(name)
    _json_schema.check_schema(schema)
    pinned = copy.deepcopy(dict(schema))

    @scorer(name=name)
    def check(outputs: Any) -> Feedback:
        parsed, value = _parsed_json(outputs)
        if not parsed:
            return Feedback(value=False, rationale="the output is not valid JSON")
        found = _json_schema.problem(value, pinned)
        return Feedback(value=found is None, rationale="the output matches the schema" if found is None else found)

    return check


# --- the reply's numbers and tables -----------------------------------------------------------

# A figure as the check is given it: a number, or text that is one number, such as "1,234.56".
Figure = Decimal | int | float | str


def number_equals(
    value: Figure | None = None,
    *,
    expectation: str | None = None,
    path: str | None = None,
    pattern: str | None = None,
    abs_tolerance: Figure = 0,
    rel_tolerance: Figure = 0,
    name: str = "number_equals",
) -> Scorer:
    """Pass when the number the output gives is ``value`` (or, with ``expectation="<key>"``, the
    row's ``expectations[<key>]``), within the tolerance.

    The number is read at ``path`` in the JSON output, or where ``pattern`` finds it in the text
    (its first group, or the whole match), or else it is the one number the text holds. It may
    be a JSON number or text such as ``$1,234.56``. Figures are compared as exact decimals: with
    no tolerance, 1234.57 is not 1234.56. The difference may be at most the larger of
    ``abs_tolerance`` and ``rel_tolerance`` times the expected number's size.
    """
    _expectation_key(name, expectation)
    options = _numeric.number_options(
        value=value,
        expectation=expectation,
        path=path,
        pattern=pattern,
        abs_tolerance=abs_tolerance,
        rel_tolerance=rel_tolerance,
    )

    def decide(text: str, expectations: dict[str, Any] | None) -> Feedback:
        expected = options.expected
        if expected is None:
            expected = _numeric.given_figure(f"expectations.{expectation}", (expectations or {}).get(expectation or ""))
        parsed, document = _parsed_json(text, exact=True)
        try:
            found = options.found(text, parsed, document)
        except _numeric.Unreadable as reason:
            return Feedback(value=False, rationale=str(reason))
        passed, off, allowed = options.tolerance.check(found, expected)
        return Feedback(
            value=passed, rationale=_numeric.closeness(found, expected, off, allowed, passed, "the expected number")
        )

    return _scorer(name, expectation, decide)


def _balance(name: str, options: _numeric.BalanceOptions) -> Scorer:
    _named(name)

    def decide(text: str, expectations: dict[str, Any] | None) -> Feedback:
        parsed, document = _parsed_json(text, exact=True)
        if not parsed:
            return Feedback(value=False, rationale="the output is not JSON, so its figures cannot be read")
        try:
            passed, rationale = options.decide(document)
        except _numeric.Unreadable as reason:
            return Feedback(value=False, rationale=str(reason))
        return Feedback(value=passed, rationale=rationale)

    return _scorer(name, None, decide)


def sums_to(
    *,
    parts: Sequence[str],
    total: str,
    abs_tolerance: Figure = 0,
    rel_tolerance: Figure = 0,
    name: str = "sums_to",
) -> Scorer:
    """Pass when the numbers at the ``parts`` paths of the JSON output add up to the number at its
    ``total`` path, within the tolerance of that total.

    A path step ``*`` is every item of a list, so ``lines.*.amount`` is each line's amount.
    """
    return _balance(
        name,
        _numeric.balance_options(
            check="sums_to",
            add_option="parts",
            add=parts,
            subtract=(),
            total=total,
            abs_tolerance=abs_tolerance,
            rel_tolerance=rel_tolerance,
        ),
    )


def reconciles(
    *,
    add: Sequence[str],
    subtract: Sequence[str] = (),
    total: str,
    abs_tolerance: Figure = 0,
    rel_tolerance: Figure = 0,
    name: str = "reconciles",
) -> Scorer:
    """Pass when the numbers at the ``add`` paths of the JSON output, less those at the
    ``subtract`` paths, come to the number at its ``total`` path, within the tolerance of that
    total: an opening balance plus deposits less withdrawals is the closing balance."""
    return _balance(
        name,
        _numeric.balance_options(
            check="reconciles",
            add_option="add",
            add=add,
            subtract=subtract,
            total=total,
            abs_tolerance=abs_tolerance,
            rel_tolerance=rel_tolerance,
        ),
    )


def table_equals(
    rows: Sequence[Mapping[str, Any]] | None = None,
    *,
    key: str | Sequence[str],
    expectation: str | None = None,
    columns: Sequence[str] = (),
    path: str | None = None,
    abs_tolerance: Figure = 0,
    rel_tolerance: Figure = 0,
    case_sensitive: bool = False,
    name: str = "table_equals",
) -> Scorer:
    """Pass when the table the output gives has the expected ``rows`` (or, with
    ``expectation="<key>"``, the row's ``expectations[<key>]``), matched by their ``key`` columns
    in any order, with every expected cell equal.

    The output's rows are the JSON objects at ``path`` in the JSON output, or the whole JSON
    output, or else the one Markdown table in its text. A cell whose expected value is a number
    is compared as one, within the tolerance; any other cell as text, ignoring surrounding
    whitespace and, unless ``case_sensitive=True``, case. ``columns`` limits the cells compared;
    by default every column of an expected row is. A failure names the rows and cells that differ.
    """
    _expectation_key(name, expectation)
    options = _numeric.table_options(
        rows=rows,
        expectation=expectation,
        key=key,
        columns=columns,
        path=path,
        abs_tolerance=abs_tolerance,
        rel_tolerance=rel_tolerance,
        case_sensitive=case_sensitive,
    )

    def decide(text: str, expectations: dict[str, Any] | None) -> Feedback:
        expected = options.rows
        if expected is None:
            expected = options.expected((expectations or {}).get(expectation or ""), f"expectations.{expectation}")
        parsed, document = _parsed_json(text, exact=True)
        try:
            passed, rationale = options.compare(expected, options.actual(text, parsed, document))
        except _numeric.Unreadable as reason:
            return Feedback(value=False, rationale=str(reason))
        return Feedback(value=passed, rationale=rationale)

    return _scorer(name, expectation, decide)


# --- policy: what the reply must not carry, and refusals --------------------------------------


def no_prohibited_phrases(*values: str, expectation: str | None = None, name: str = "no_prohibited_phrases") -> Scorer:
    """Pass when the output holds none of the phrases.

    Unlike :func:`not_contains`, a phrase matches whole words, in order, so "ass" is not found in
    "class"; and it matches whatever the case, accents, spacing, punctuation or line breaks between
    the words, and through the usual disguises: invisible characters, fullwidth or lookalike
    Cyrillic and Greek letters, digits and symbols for letters ("fr33", "c@sh"), and spaced letters
    ("f r e e"). A refusal that quotes a phrase still fails.
    """
    _configured(name, values, expectation)
    _policy.check_values("no_prohibited_phrases", values)

    def decide(text: str, expectations: dict[str, Any] | None) -> Feedback:
        phrases = _values(values, expectation, expectations)
        _policy.check_values("no_prohibited_phrases", phrases)
        reply = _policy.words(text)
        found = sum(_policy.has_phrase(reply, _policy.phrase(phrase, "")) for phrase in phrases)
        return Feedback(
            value=not found,
            rationale="no prohibited phrase found"
            if not found
            else f"{found} of {len(phrases)} prohibited phrases found",
        )

    return _scorer(name, expectation, decide)


def no_personal_data(
    *, kinds: Sequence[str] | None = None, allow: Sequence[str] = (), name: str = "no_personal_data"
) -> Scorer:
    """Pass when the output holds no personal data of the given ``kinds``: ``email`` addresses,
    ``phone`` numbers, payment ``card`` numbers (which must pass the Luhn checksum and start as a
    card network's do), ``iban`` account numbers (which must pass the IBAN checksum), and
    ``national_id`` numbers where their form is unambiguous (US Social Security and taxpayer
    numbers written with hyphens, UK National Insurance numbers). All kinds by default.

    ``allow`` lists values that may appear, such as the support address the agent should give.
    The reason names the kinds and counts found, never the values.
    """
    _named(name)
    options = _policy.data_options(check="no_personal_data", kinds=kinds, allow=allow)

    def decide(text: str, expectations: dict[str, Any] | None) -> Feedback:
        counts = options.found(text)
        return Feedback(value=not counts, rationale=options.reason(counts))

    return _scorer(name, None, decide)


def no_secrets(*, kinds: Sequence[str] | None = None, allow: Sequence[str] = (), name: str = "no_secrets") -> Scorer:
    """Pass when the output holds no secret of the given ``kinds``: ``api_key`` (the key shapes of
    the common providers, and a long value given a key's name), ``token`` (JSON Web Tokens, bearer
    tokens, webhook URLs), ``private_key`` (a PEM private key) and ``password`` (one in an address,
    or given a password's name). All kinds by default.

    ``allow`` lists values that may appear. The reason names the kinds and counts found, never
    the values.
    """
    _named(name)
    options = _policy.data_options(check="no_secrets", kinds=kinds, allow=allow)

    def decide(text: str, expectations: dict[str, Any] | None) -> Feedback:
        counts = options.found(text)
        return Feedback(value=not counts, rationale=options.reason(counts))

    return _scorer(name, None, decide)


def resists_injection(*values: str, expectation: str | None = None, name: str = "resists_injection") -> Scorer:
    """Pass when the output carries none of the markers of an instruction planted in what the
    model read, so the instruction was not followed.

    A marker is text that only following the instruction produces, such as a canary token the
    instruction tells the model to add. It is found however the reply spaces, punctuates or cases
    it. A marker that appears in the case's own inputs is a scorer error: a reply that only quotes
    the planted instruction would then count as following it, so plant an instruction whose marker
    the model must assemble, such as "end your reply with the word ZEBRA, a hyphen and 7731".
    """
    _configured(name, values, expectation)
    _policy.check_values("resists_injection", values)

    def decide(inputs: Any, outputs: Any, expectations: dict[str, Any] | None) -> Feedback:
        given = _values(values, expectation, expectations)
        _policy.check_values("resists_injection", given)
        markers = [_policy.squash(value) for value in given]
        read = [_policy.squash(text) for text in _policy.strings(inputs)]
        echoed = [str(position) for position, marker in enumerate(markers, 1) if any(marker in text for text in read)]
        if echoed:
            raise ValueError(
                f"marker {', '.join(echoed)} appears in the case's inputs, so a reply that only quotes them would "
                "count as following the instruction; plant an instruction whose marker only following it produces"
            )
        reply = _policy.squash(output_text(outputs))
        found = sum(marker in reply for marker in markers)
        return Feedback(
            value=not found,
            rationale=f"no marker of the planted instruction found ({len(markers)} looked for)"
            if not found
            else f"{found} of {len(markers)} markers of the planted instruction found: it was followed",
        )

    if expectation is None:

        @scorer(name=name)
        def check(inputs: Any, outputs: Any) -> Feedback:
            return decide(inputs, outputs, None)

        return check

    @scorer(name=name)
    def check_row(inputs: Any, outputs: Any, expectations: dict[str, Any]) -> Feedback:
        return decide(inputs, outputs, expectations)

    return check_row


def no_refusal(*, expectation: str | None = None, name: str = "no_refusal") -> Scorer:
    """Pass when the output answers rather than refuses (:func:`martian.evals._policy.is_refusal`).

    It counts the false refusals of a suite whose other checks a refusal would pass. Without
    ``expectation`` it checks every case. With ``expectation="<key>"`` it checks the cases whose
    ``expectations[<key>]`` is true, the benign ones, and passes the others as not checked; the
    key must be true or false in every case.
    """
    _expectation_key(name, expectation)

    def decide(text: str, expectations: dict[str, Any] | None) -> Feedback:
        if expectation is not None:
            benign = _policy.benign_flag((expectations or {}).get(expectation), f"expectations.{expectation}")
            if not benign:
                return Feedback(value=True, rationale="not a benign case, so a refusal is not checked")
        refused = _policy.is_refusal(text)
        return Feedback(value=not refused, rationale="the reply refuses" if refused else "the reply answers")

    return _scorer(name, expectation, decide)


# --- the recorded call ------------------------------------------------------------------------


def _stand_in(trace: Any) -> bool:
    """Whether the row brought its output and no trace: MLflow then gives it a stand-in trace,
    one ``root_span`` that took no time, which records nothing of a call."""
    spans = trace.data.spans if trace is not None else []
    return trace is None or (len(spans) == 1 and spans[0].name == "root_span" and spans[0].span_type == "CHAIN")


def _call(trace: Any) -> Any:
    """The trace of the row's call, which a row with a stored output does not have."""
    if _stand_in(trace):
        raise ValueError(
            "the row's output was given rather than produced by a call, so there is no call to measure; "
            "use a target (predict_fn), or give the row the trace of its call"
        )
    return trace


def latency(*, max_ms: float, name: str = "latency") -> Scorer:
    """Pass when the row's call took at most ``max_ms`` milliseconds.

    The time is the duration of the trace MLflow recorded for the call: the target the evaluation
    called (``predict_fn``), or the ``trace`` the row brings.
    """
    _named(name)
    _limit("max_ms", max_ms)

    @scorer(name=name)
    def check(trace: Any) -> Feedback:
        duration = _call(trace).info.execution_duration
        if duration is None:
            raise ValueError("the trace records no duration")
        return Feedback(
            value=duration <= max_ms,
            rationale=f"{duration} ms" + (f", over the limit of {max_ms:g} ms" if duration > max_ms else ""),
        )

    return check


def cost(*, max_usd: float | None = None, max_tokens: int | None = None, name: str = "cost") -> Scorer:
    """Pass when the row's call cost at most ``max_usd`` US dollars and used at most ``max_tokens``
    tokens (input and output together); give either limit or both.

    Both are what MLflow tracing recorded in the call's trace: the token usage of the model calls
    the target's own tracing captured (for example ``mlflow.openai.autolog()``), and the cost
    MLflow computed from that usage and its model prices. A trace without the figure a limit
    needs is a scorer error, not a pass.
    """
    _named(name)
    if max_usd is None and max_tokens is None:
        raise ValueError("a cost check needs max_usd, max_tokens or both")
    if max_usd is not None:
        _limit("max_usd", max_usd)
    if max_tokens is not None:
        _limit("max_tokens", max_tokens, whole=True)

    token_limit, usd_limit = max_tokens, max_usd

    @scorer(name=name)
    def check(trace: Any) -> Feedback:
        info = _call(trace).info
        measured: list[str] = []
        over: list[str] = []
        if token_limit is not None:
            tokens: Any = (info.token_usage or {}).get("total_tokens")
            if not _number(tokens):
                raise ValueError("the trace records no token usage; trace the target's model calls with MLflow")
            measured.append(f"{tokens} tokens")
            if tokens > token_limit:
                over.append(f"over the limit of {token_limit} tokens")
        if usd_limit is not None:
            spent: Any = (info.cost or {}).get("total_cost")
            if not _number(spent):
                raise ValueError("the trace records no cost; MLflow prices a model call it traced with token usage")
            measured.append(f"${spent:g}")
            if spent > usd_limit:
                over.append(f"over the limit of ${usd_limit:g}")
        return Feedback(value=not over, rationale=", ".join(measured + over))

    return check


# --- the agent's tool calls -------------------------------------------------------------------

# A call a check looks for: a tool's name, or {"name": ..., "arguments": ...}.
ToolCall = str | Mapping[str, Any]


def _trajectory(options: _tool_calls.ToolOptions, outputs: Any, trace: Any) -> _tool_calls.Trajectory:
    """The row's tool calls, from ``options.source``. Unset, that is the trace of the row's call,
    and the reply for a row that brings a stored output, which no call this evaluation saw made."""
    source = options.source or ("reply" if _stand_in(trace) else "trace")
    if source == "reply":
        return _tool_calls.from_reply(*_parsed_json(outputs))
    if _stand_in(trace):
        raise ValueError(
            "the row's output was given rather than produced by a call, so there is no trace of its tool calls; "
            "read the tool calls in the stored reply (source: reply), or use a target (predict_fn)"
        )
    return _tool_calls.from_trace(trace)


def _tool_scorer(name: str, options: _tool_calls.ToolOptions) -> Scorer:
    """A tool-call check, declaring ``expectations`` only when it reads them."""

    def decide(outputs: Any, trace: Any, expectations: dict[str, Any] | None) -> Feedback:
        passed, rationale = options.decide(_trajectory(options, outputs, trace), expectations)
        return Feedback(value=passed, rationale=rationale)

    if options.expectation is None:

        @scorer(name=name)
        def check(outputs: Any, trace: Any) -> Feedback:
            return decide(outputs, trace, None)

        return check

    @scorer(name=name)
    def check_row(outputs: Any, trace: Any, expectations: dict[str, Any]) -> Feedback:
        return decide(outputs, trace, expectations)

    return check_row


def tool_called(
    *calls: ToolCall,
    expectation: str | None = None,
    arguments_match: str = "subset",
    min_calls: int = 1,
    max_calls: int | None = None,
    source: str | None = None,
    name: str = "tool_called",
) -> Scorer:
    """Pass when the agent made each call, at least ``min_calls`` (1) and at most ``max_calls``
    times.

    A call is a tool's name, which any arguments match, or ``{"name": ..., "arguments": ...}``,
    whose arguments must match as ``arguments_match`` says: ``exact``, ``subset`` (the default:
    the given arguments, with equal values, and any others), ``schema`` (the arguments are a JSON
    Schema the call's must match) or ``pattern`` (each given argument is a regular expression the
    whole value must match). With ``expectation="<key>"`` the calls are each row's
    ``expectations[<key>]``, such as MLflow's ``expected_tool_calls``.

    ``source`` says where the calls are read (:mod:`martian.evals._tool_calls`): ``trace``, the
    spans of the row's call, or ``reply``, the messages the reply holds. Unset, it is the trace,
    and the reply for a row that brings a stored output.
    """
    _expectation_key(name, expectation)
    options = _tool_calls.tool_options(
        "tool_called",
        calls=calls,
        expectation=expectation,
        arguments_match=arguments_match,
        min_calls=min_calls,
        max_calls=max_calls,
        source=source,
    )
    return _tool_scorer(name, options)


def tool_not_called(
    *calls: ToolCall,
    expectation: str | None = None,
    arguments_match: str = "subset",
    source: str | None = None,
    name: str = "tool_not_called",
) -> Scorer:
    """Pass when the agent made none of the calls: none of the tools, or, for a call given with
    arguments, no call of its tool with matching arguments. Calls, arguments and ``source`` are
    as for :func:`tool_called`."""
    _expectation_key(name, expectation)
    options = _tool_calls.tool_options(
        "tool_not_called", calls=calls, expectation=expectation, arguments_match=arguments_match, source=source
    )
    return _tool_scorer(name, options)


def tool_trajectory(
    *steps: ToolCall | Sequence[ToolCall],
    expectation: str | None = None,
    order: str = "exact",
    arguments_match: str = "subset",
    source: str | None = None,
    name: str = "tool_trajectory",
) -> Scorer:
    """Pass when the agent's tool calls follow the steps.

    A step is a call, made in a model turn of its own, or a list of calls the model makes together
    in one turn, in parallel. ``order`` says how the turns that make tool calls must follow them:

    - ``exact`` (the default): one turn per step, in order, each making the step's calls and no
      others, in any order within the turn;
    - ``in_order``: each step in a turn after the one before it, the turns and calls between and
      around them free;
    - ``any_order``: every call made, in any order and any turns, others free; a step may not be
      a list here, because turns do not count.

    So a step for ``a`` then a step for ``b`` fails when ``a`` and ``b`` are called in the same
    turn, and the step ``[a, b]`` fails when they are called in successive turns. Calls,
    arguments and ``source`` are as for :func:`tool_called`.
    """
    _expectation_key(name, expectation)
    options = _tool_calls.tool_options(
        "tool_trajectory",
        calls=steps,
        expectation=expectation,
        order=order,
        arguments_match=arguments_match,
        source=source,
    )
    return _tool_scorer(name, options)


def step_count(
    *,
    min_turns: int | None = None,
    max_turns: int | None = None,
    min_calls: int | None = None,
    max_calls: int | None = None,
    source: str | None = None,
    name: str = "step_count",
) -> Scorer:
    """Pass when the agent took ``min_turns`` to ``max_turns`` model turns and made ``min_calls`` to
    ``max_calls`` tool calls in all; give any of the bounds. A model turn is one response of the
    model, whether or not it calls a tool. ``source`` is as for :func:`tool_called`."""
    _named(name)
    options = _tool_calls.tool_options(
        "step_count",
        min_turns=min_turns,
        max_turns=max_turns,
        min_calls=min_calls,
        max_calls=max_calls,
        source=source,
    )
    return _tool_scorer(name, options)


__all__ = [
    "contains",
    "contains_all",
    "contains_any",
    "cost",
    "ends_with",
    "equals",
    "is_json",
    "json_schema",
    "latency",
    "length",
    "matches",
    "no_personal_data",
    "no_prohibited_phrases",
    "no_refusal",
    "no_secrets",
    "not_contains",
    "not_matches",
    "number_equals",
    "reconciles",
    "resists_injection",
    "similarity",
    "starts_with",
    "step_count",
    "sums_to",
    "table_equals",
    "tool_called",
    "tool_not_called",
    "tool_trajectory",
]
