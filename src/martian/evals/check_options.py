"""The deterministic checks a request can apply, with the options each takes.

A suite file's ``checks`` (:mod:`martian.evals.suite_file`) and a scoring request's or saved suite's
``checks`` (MartianSecurity/martian-notes#334) are one thing: a check's ``type``, its label and its options. A
request's check becomes a :class:`~martian.evals.model.ScorerVersion` of kind ``check`` exactly as
loading a suite file makes one (:func:`check_scorer`), so the worker that scores a run's saved
outputs builds the same check from it as ``martian evals run`` does from the file
(:mod:`martian.evals.worker.checks`).

:data:`CATALOGUE` says what each check takes, for a form: every option of every type, in the order the
suite file's documentation lists them. ``tests/evals/test_scorer_options.py`` holds it to the suite
file's own table of options (``CHECK_OPTIONS``), so an option added there is missing here until it is
described. This module imports the suite runner, so a request's check is validated by the same code
that validates a suite file; callers import it when a request names a check, not at startup.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import ValidationError

from martian.config import ConfigError
from martian.evals import _policy, _tool_calls, runner
from martian.evals.model import ScorerVersion
from martian.evals.suite_file import CHECK_OPTIONS, _problems, _scorer

LABEL = re.compile(r"^[a-z][a-z0-9_]{0,59}$")
MAX_OPTIONS_CHARACTERS = 20000
"""The most characters a check's options may have, written as JSON: a schema, rows or calls can be long,
but a request is bounded."""

# What an option holds, for a form: a list of strings, one string, a yes or no, a number, a whole
# number, one choice, several choices, or JSON (a schema, rows or calls).
KINDS = ("strings", "text", "boolean", "number", "integer", "choice", "choices", "json")

OPTIONS: dict[str, dict[str, Any]] = {
    "values": {"kind": "strings", "label": "Values", "description": "The text to look for, one value per line."},
    "expectation": {
        "kind": "text",
        "label": "Expectation key",
        "description": "Read the values from this key of each case's expectations instead, or as well.",
    },
    "any_of": {
        "kind": "boolean",
        "label": "Any one is enough",
        "description": "Pass when at least one value is found.",
    },
    "case_sensitive": {
        "kind": "boolean",
        "label": "Match case",
        "description": "Treat upper and lower case as different. Without it, case is ignored.",
    },
    "case_mapping": {
        "kind": "choice",
        "label": "Case comparison",
        "description": "Casefold by default. Choose lower for lowercase comparison, as in Promptfoo.",
        "choices": ["casefold", "lower"],
    },
    "min_similarity": {
        "kind": "number",
        "label": "Minimum similarity",
        "description": "From 0 to 1: 1 is the same text, 0.8 is two edits in ten characters.",
    },
    "max_edits": {
        "kind": "integer",
        "label": "Most edits",
        "description": "Maximum Levenshtein distance in UTF-16 code units. Defaults to 5.",
    },
    "pattern": {"kind": "text", "label": "Regular expression", "description": "A Python regular expression."},
    "required_keys": {
        "kind": "strings",
        "label": "Required keys",
        "description": "Keys the JSON object must hold, one per line. Leave empty to accept any JSON.",
    },
    "required_elements": {
        "kind": "strings",
        "label": "Required elements",
        "description": "Dot-separated paths starting at the XML root, one per line.",
        "placeholder": "catalog.book.1.title",
    },
    "fragment": {
        "kind": "boolean",
        "label": "Find XML in text",
        "description": "Look for XML fragments inside a response. Leave off to grade a complete XML document.",
    },
    "schema": {
        "kind": "json",
        "label": "JSON Schema",
        "description": "The JSON Schema the output must match, as JSON.",
        "placeholder": '{"type": "object", "required": ["answer"]}',
    },
    "plain_json": {
        "kind": "boolean",
        "label": "Require plain JSON",
        "description": "Require a complete JSON document. The default also accepts one Markdown code fence.",
    },
    "embedded": {
        "kind": "choice",
        "label": "Find JSON in text",
        "description": "Find JSON objects. The promptfoo option also accepts some repaired objects and flow YAML.",
        "choices": ["strict", "promptfoo"],
    },
    "negate": {
        "kind": "boolean",
        "label": "Invert schema check",
        "description": "Pass when no JSON value found by the check matches the schema.",
    },
    "min_length": {
        "kind": "integer",
        "label": "Fewest characters",
        "description": "The output has at least this many characters.",
    },
    "max_length": {
        "kind": "integer",
        "label": "Most characters",
        "description": "The output has at most this many characters.",
    },
    "min_words": {
        "kind": "integer",
        "label": "Fewest words",
        "description": "The output has at least this many words.",
    },
    "max_words": {
        "kind": "integer",
        "label": "Most words",
        "description": "The output has at most this many words.",
    },
    "max_ms": {"kind": "number", "label": "Most milliseconds", "description": "The call took at most this long."},
    "max_usd": {"kind": "number", "label": "Most US dollars", "description": "The call cost at most this much."},
    "max_tokens": {
        "kind": "integer",
        "label": "Most tokens",
        "description": "The call used at most this many tokens, input and output together.",
    },
    "value": {
        "kind": "text",
        "label": "Expected number",
        "description": "The number the output should give, such as 1234.56. Give this or an expectation key.",
    },
    "path": {
        "kind": "text",
        "label": "Path",
        "description": "Where the JSON output holds it: dotted keys and list indexes, such as totals.net.",
    },
    "abs_tolerance": {
        "kind": "text",
        "label": "Absolute tolerance",
        "description": "How far apart two numbers may be, such as 0.01.",
    },
    "rel_tolerance": {
        "kind": "text",
        "label": "Relative tolerance",
        "description": "How far apart two numbers may be, as a fraction of the larger, such as 0.001.",
    },
    "parts": {
        "kind": "strings",
        "label": "Paths to add up",
        "description": (
            "Where the numbers to add up are, one path per line; * is every item of a list, as in lines.*.amount."
        ),
    },
    "add": {
        "kind": "strings",
        "label": "Paths to add",
        "description": "Where the numbers to add are, one path per line; * is every item of a list.",
    },
    "subtract": {
        "kind": "strings",
        "label": "Paths to subtract",
        "description": "Where the numbers to take away are, one path per line.",
    },
    "total": {
        "kind": "text",
        "label": "Total path",
        "description": "Where the JSON output holds the number it should come to.",
    },
    "key": {
        "kind": "strings",
        "label": "Key columns",
        "description": "The column that names a row, so rows are matched by it; several lines make a composite key.",
    },
    "columns": {
        "kind": "strings",
        "label": "Columns to compare",
        "description": "Compare only these columns, one per line. Leave empty to compare every expected column.",
    },
    "rows": {
        "kind": "json",
        "label": "Expected rows",
        "description": (
            "The rows the output's table should have, as a JSON list of objects. Give these or an expectation key."
        ),
        "placeholder": '[{"id": "a", "amount": "10.00"}]',
    },
    "allow": {"kind": "strings", "label": "Allowed values", "description": "Values that may appear, one per line."},
    "calls": {
        "kind": "json",
        "label": "Calls",
        "description": (
            "A JSON list of the tool calls: a tool's name, or an object with name and arguments. "
            "Give these or an expectation key."
        ),
        "placeholder": '["lookup_order"]',
    },
    "arguments_match": {
        "kind": "choice",
        "label": "Arguments match",
        "description": "How a call's arguments must match the given ones.",
        "choices": list(_tool_calls.ARGUMENT_MATCHES),
    },
    "order": {
        "kind": "choice",
        "label": "Order",
        "description": "How the steps must follow each other.",
        "choices": list(_tool_calls.ORDERS),
    },
    "source": {
        "kind": "choice",
        "label": "Read the calls from",
        "description": "The reply's messages, or the spans of the target's call. A run's saved outputs have no spans.",
        "choices": list(_tool_calls.SOURCES),
    },
    "min_calls": {
        "kind": "integer",
        "label": "Fewest calls",
        "description": "Each call was made at least this many times (at least 1 by default).",
    },
    "max_calls": {
        "kind": "integer",
        "label": "Most calls",
        "description": "At most this many calls in all, or of each call given.",
    },
    "min_turns": {
        "kind": "integer",
        "label": "Fewest model turns",
        "description": "The agent took at least this many model turns.",
    },
    "max_turns": {
        "kind": "integer",
        "label": "Most model turns",
        "description": "The agent took at most this many model turns.",
    },
}
"""Every option a deterministic check takes, as a form describes it. ``kinds`` is per check, in :func:`_kinds`."""

# The checks, in the order the suite file lists them, each with what it passes on and the options
# that must be given (an option the check cannot do without; one of a pair, such as `values` or
# `expectation`, is not listed, since either will do).
CHECKS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "contains": ("Contains", "The output contains every value, or at least one with 'any one is enough'.", ()),
    "not_contains": (
        "Does not contain",
        "The output contains none of the values. A refusal that quotes one still fails.",
        (),
    ),
    "contains_any": ("Contains any", "The output contains at least one value.", ()),
    "contains_all": ("Contains all", "The output contains every value.", ()),
    "equals": ("Equals", "The whole output is one of the values.", ()),
    "starts_with": ("Starts with", "The output starts with one of the values.", ()),
    "ends_with": ("Ends with", "The output ends with one of the values.", ()),
    "similarity": (
        "Similar to",
        "The output is at least as similar as given to one of the values.",
        ("min_similarity",),
    ),
    "levenshtein": ("Edit distance", "The output is within the maximum edit distance of a reference.", ()),
    "matches": ("Matches", "The regular expression is found anywhere in the output.", ("pattern",)),
    "not_matches": ("Does not match", "The regular expression is found nowhere in the output.", ("pattern",)),
    "is_json": ("Is JSON", "The output is valid JSON; with required keys, a JSON object holding each one.", ()),
    "is_xml": ("Is XML", "The output is valid XML with the requested elements.", ()),
    "json_schema": ("Matches a JSON Schema", "The output is JSON that matches the JSON Schema.", ("schema",)),
    "length": (
        "Length",
        "The output has at least the fewest and at most the most characters; give either or both.",
        (),
    ),
    "word_count": ("Word count", "The output has the fewest through the most words. Give either bound or both.", ()),
    "latency": (
        "Latency",
        "The target's call took at most this many milliseconds. A run's saved latency is read.",
        ("max_ms",),
    ),
    "cost": (
        "Cost",
        "The target's call cost at most this much and used at most this many tokens; give either or both.",
        (),
    ),
    "number_equals": ("Number equals", "The number the output gives is the expected one, within the tolerance.", ()),
    "sums_to": ("Sums to", "The numbers at the paths add up to the number at the total path.", ("parts", "total")),
    "reconciles": (
        "Reconciles",
        "The numbers to add, less those to take away, come to the number at the total path.",
        ("add", "total"),
    ),
    "table_equals": (
        "Table equals",
        "The output's table has the expected rows, matched by key, with every expected cell equal.",
        ("key",),
    ),
    "no_prohibited_phrases": (
        "No prohibited phrases",
        "The output holds none of the phrases, matched as whole words through case, spacing and common disguises.",
        (),
    ),
    "no_personal_data": (
        "No personal data",
        "The output holds no email address, phone number, card number, IBAN or national ID, other than those allowed.",
        (),
    ),
    "no_secrets": ("No secrets", "The output holds no API key, access token, private key or password.", ()),
    "resists_injection": (
        "Resists injection",
        "The output carries none of the markers of an instruction planted in what the model read.",
        (),
    ),
    "no_refusal": (
        "No refusal",
        "The output answers rather than refuses; with an expectation key, only on cases whose key is true.",
        (),
    ),
    "tool_called": ("Tool called", "The agent made each call, with matching arguments, within the bounds.", ()),
    "tool_not_called": ("Tool not called", "The agent made none of the calls.", ()),
    "tool_trajectory": ("Tool trajectory", "The agent's tool calls follow the steps, turn by turn.", ()),
    "step_count": (
        "Step count",
        "The agent took that many model turns and made that many tool calls; give any of the bounds.",
        (),
    ),
}

# Words that name what the same option holds in a particular check.
_RENAMED: dict[tuple[str, str], dict[str, Any]] = {
    ("is_xml", "negate"): {
        "label": "Invert XML check",
        "description": "Pass when the output has no XML matching the required elements.",
    },
    ("levenshtein", "negate"): {
        "label": "Invert edit-distance check",
        "description": "Pass when the output is farther away from every reference.",
    },
    ("word_count", "negate"): {
        "label": "Invert word-count check",
        "description": "Pass when the word count is outside the given bounds.",
    },
    ("no_prohibited_phrases", "values"): {
        "label": "Phrases",
        "description": "The phrases that must not appear, one per line.",
    },
    ("resists_injection", "values"): {
        "label": "Markers",
        "description": "Text a planted instruction would make the reply carry, one per line.",
    },
    ("table_equals", "path"): {
        "label": "Path",
        "description": "Where the JSON output holds its table, such as lines; empty if the output is the table.",
    },
    ("number_equals", "pattern"): {
        "label": "Pattern",
        "description": "A regular expression that finds the number in the text; its first group, or the whole match.",
    },
    ("no_refusal", "expectation"): {
        "label": "Expectation key",
        "description": "Check only the cases whose expectations hold true under this key.",
    },
}


def _kinds(check: str) -> list[str]:
    return list(_policy.PERSONAL_KINDS if check == "no_personal_data" else _policy.SECRET_KINDS)


def _option(check: str, name: str, required: Sequence[str]) -> dict[str, Any]:
    described = (
        {**OPTIONS[name], **_RENAMED.get((check, name), {})}
        if name != "kinds"
        else {
            "kind": "choices",
            "label": "Kinds",
            "description": "The kinds to look for; leave them all unchecked for every kind.",
            "choices": _kinds(check),
        }
    )
    return {"name": name, "required": name in required, **described}


def _catalogue() -> list[dict[str, Any]]:
    found = []
    for check, (label, description, required) in CHECKS.items():
        names = CHECK_OPTIONS[check]
        # The options in the order a form lists them: the ones every check shares first.
        order = [name for name in (*OPTIONS, "kinds") if name in names]
        found.append(
            {
                "type": check,
                "label": label,
                "description": description,
                "options": [_option(check, name, required) for name in order],
            }
        )
    return found


CATALOGUE: list[dict[str, Any]] = _catalogue()


def check_scorer(kind: str, name: str | None, options: Mapping[str, Any]) -> ScorerVersion:
    """The scorer a request's check is: ``kind`` named ``name`` (else by its type), with ``options`` as a
    suite file writes them. Raises ``ValueError`` saying what is wrong, as loading a suite file
    names it: an option the check does not take, one it cannot read, one it needs and lacks."""
    if kind not in runner.CHECKS:
        raise ValueError(f"{kind} is not a deterministic check")
    label = name or kind
    if not LABEL.fullmatch(label):
        raise ValueError("name is lowercase letters, digits and underscores, starting with a letter, up to 60")
    if unused := sorted(set(options) - CHECK_OPTIONS[kind]):
        raise ValueError(f"{', '.join(unused)} does not apply to {kind} checks")
    if len(str(options)) > MAX_OPTIONS_CHARACTERS:
        raise ValueError(f"options are longer than {MAX_OPTIONS_CHARACTERS} characters")
    try:
        spec = runner.CheckSpec.model_validate({"type": kind, "name": label, **options})
        runner._build_check(spec)  # compiles a pattern, reads a schema: what loading a suite file does
        return _scorer(0, spec, None)
    except ValidationError as error:
        raise ValueError(_problems(error)) from None
    except (ConfigError, ValueError) as error:
        raise ValueError(str(error)) from None


def options_of(scorer: ScorerVersion) -> dict[str, Any]:
    """A check scorer's options, as a request names them."""
    return dict(scorer.parameters)


__all__ = ["CATALOGUE", "CHECKS", "KINDS", "LABEL", "OPTIONS", "check_scorer", "options_of"]
