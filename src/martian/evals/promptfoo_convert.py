"""A promptfoo config as suite files that ``martian evals run`` runs.

``martian evals convert promptfoo promptfooconfig.yaml --output suite/`` reads a promptfoo config
(``prompts``, ``providers``, ``tests`` with ``vars`` and ``assert``, and ``defaultTest``) without
promptfoo, and writes suite files and their data files where the features map:

- Each test, once per prompt, is a case: the prompt rendered with the test's vars (``{{ name }}``
  and ``{{ name.key }}``) is the input ``prompt``, and the vars are the input ``vars``. Case ids
  are the ones ``martian evals import promptfoo`` gives the same test's results.
- ``ASSERTION_CHECKS`` maps the supported assertions. ``defaultTest`` assertions come first
  unless the test disables them. Local datasets can be read from YAML, JSON, JSONL or CSV.
- Tests are grouped by the checks they need. With derived metrics the groups stay in one suite
  per provider as case-specific check sets. Other conversions write one suite file per group,
  ``evals.yaml`` and ``cases.jsonl`` for the first, ``evals-2.yaml`` and ``cases-2.jsonl`` for a
  second, and so on. A check's value can come from each case's expectations.
- ``--target FILE.py[:FUNCTION]`` supplies a local function target. Without it, conversion
  uses a supported provider, a recorded ``providerOutput``, or a plain Echo reply.

Strict conversion refuses known scoring, execution or coverage changes before writing files.
Best-effort conversion lists omissions and changes in the command output and suite headers.
See ``docs/evals-core.md`` for the mapping table.
"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from martian.config import ConfigError
from martian.evals import _json_schema, _xml_output
from martian.evals._promptfoo_comparison import CHECKS as COMPARABLE_CHECKS
from martian.evals._promptfoo_inputs import (
    FILE_PREFIX,
    TEST_EXTENSIONS,
    PromptfooConfigError,
    case_id,
    inline_prompt,
    local_file,
    prompt_group,
    read_test_file,
    read_text,
    yaml_data,
)
from martian.evals._promptfoo_providers import NativeProvider, native_provider, provider_display
from martian.evals._rubric_prompt import JUDGE_PROMPT, JudgePrompt
from martian.evals.case_gate import AssertionSet, CaseChecks, CaseGate
from martian.evals.derived_metrics import DerivedMetrics
from martian.evals.model import Case, RunSettings, ScorerVersion, Suite, Target, TargetKind, digest, suite_digest
from martian.evals.runner import GROUP_TAG, SCORED_RUBRIC
from martian.evals.suite_file import cases_to_jsonl, suite_to_yaml


@dataclass(frozen=True)
class Unconverted:
    """Something in the config the suite files leave out: where (a path into the config, such as
    ``tests[2].assert[1]``), what (an assertion type, a provider, an option), and why."""

    where: str
    what: str
    why: str
    blocks_strict: bool = True

    def line(self) -> str:
        return f"{self.where} {self.what}: {self.why}" if self.what else f"{self.where}: {self.why}"


@dataclass(frozen=True)
class ConvertedSuite:
    """One suite file and its data file: their names, their text, and the tests they hold."""

    suite_file: str
    data_file: str
    suite: str
    data: str
    tests: tuple[int, ...]
    cases: int
    checks: tuple[str, ...]
    source_provider: str | None = None


@dataclass(frozen=True)
class Conversion:
    """A config converted: the suite files to write and everything they leave out."""

    config: Path
    suites: list[ConvertedSuite]
    unconverted: list[Unconverted]
    compatibility: list[Unconverted] = field(default_factory=list)

    @property
    def strict_blockers(self) -> list[Unconverted]:
        """Known omissions or scoring differences that prevent strict conversion."""
        return [note for note in self.unconverted if note.blocks_strict] + self.compatibility

    def require_strict(self) -> None:
        if blockers := self.strict_blockers:
            details = "\n".join(f"- {note.line()}" for note in blockers)
            raise PromptfooConfigError(
                f"strict conversion left the output untouched. Review these differences:\n{details}"
            )
        if not self.suites:
            raise PromptfooConfigError("strict conversion produced no runnable suite")


# --- the mapping ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Text:
    check: str
    case_sensitive: bool
    any_of: bool
    many: bool


TEXT_CHECKS = {
    "contains": _Text("contains", case_sensitive=True, any_of=False, many=False),
    "icontains": _Text("contains", case_sensitive=False, any_of=False, many=False),
    "contains-any": _Text("contains", case_sensitive=True, any_of=True, many=True),
    "icontains-any": _Text("contains", case_sensitive=False, any_of=True, many=True),
    "contains-all": _Text("contains", case_sensitive=True, any_of=False, many=True),
    "icontains-all": _Text("contains", case_sensitive=False, any_of=False, many=True),
    "not-contains": _Text("not_contains", case_sensitive=True, any_of=False, many=False),
    "not-icontains": _Text("not_contains", case_sensitive=False, any_of=False, many=False),
    "not-contains-any": _Text("not_contains", case_sensitive=True, any_of=False, many=True),
    "not-icontains-any": _Text("not_contains", case_sensitive=False, any_of=False, many=True),
}

PATTERN_CHECKS = {
    "regex": ("matches", "{}"),
    "not-regex": ("not_matches", "{}"),
    "equals": ("matches", r"\A{}\Z"),
    "not-equals": ("not_matches", r"\A{}\Z"),
    "starts-with": ("matches", r"\A{}"),
    "not-starts-with": ("not_matches", r"\A{}"),
}

# promptfoo passes the provider's call when its time in milliseconds, or its cost in US dollars,
# is at most the assertion's threshold: the check of the same name with that limit.
CALL_CHECKS = {"latency": "max_ms", "cost": "max_usd"}
RUBRIC_ASSERTIONS = ("llm-rubric", "not-llm-rubric")
EDIT_ASSERTIONS = ("levenshtein", "not-levenshtein")
WORD_COUNT_ASSERTIONS = ("word-count", "not-word-count")
EMBEDDED_ASSERTIONS = ("contains-json", "not-contains-json")
JSON_ASSERTIONS = ("is-json", "not-is-json", *EMBEDDED_ASSERTIONS)
XML_ASSERTIONS = ("is-xml", "not-is-xml", "contains-xml", "not-contains-xml")
_MAX_SAFE_INTEGER = 2**53 - 1

ASSERTION_CHECKS = {
    **{kind: text.check for kind, text in TEXT_CHECKS.items()},
    **{kind: check for kind, (check, _) in PATTERN_CHECKS.items()},
    "is-json": "is_json",
    "not-is-json": "json_schema",
    **dict.fromkeys(EMBEDDED_ASSERTIONS, "json_schema"),
    **dict.fromkeys(XML_ASSERTIONS, "is_xml"),
    **dict.fromkeys(RUBRIC_ASSERTIONS, SCORED_RUBRIC),
    **dict.fromkeys(EDIT_ASSERTIONS, "levenshtein"),
    **dict.fromkeys(WORD_COUNT_ASSERTIONS, "word_count"),
    **{kind: kind for kind in CALL_CHECKS},
}
"""Each promptfoo assertion type that converts, and the suite check it becomes."""

_MODEL_GRADED = (
    "agent-rubric",
    "answer-relevance",
    "context-faithfulness",
    "context-recall",
    "context-relevance",
    "conversation-relevance",
    "factuality",
    "g-eval",
    "model-graded-closedqa",
    "model-graded-factuality",
    "search-rubric",
    "trajectory:goal-success",
)
_CLOSENESS = (
    "similar",
    "similar:cosine",
    "similar:dot",
    "similar:euclidean",
    "rouge-n",
    "rouge-l",
    "rouge-s",
    "bleu",
    "gleu",
    "meteor",
    "perplexity",
    "perplexity-score",
)
# promptfoo's tool-call assertions: the closest tool-call check, and how the two decide apart.
# None decides as a check does, so none converts.
_TRAJECTORY = (
    "reads every span of promptfoo's trace that names a tool in its attributes, a tool's own inner spans too, "
    "and matches a tool's name as a case-insensitive pattern (* and ?); the closest check is {check}, which "
    "reads the TOOL spans of MLflow's trace of the target's call, not those inside a tool span, and compares "
    "the name as written"
)
_VALID_CALL = (
    "validates the reply's tool calls against the schemas of the provider's own tools or functions; the closest "
    "check is tool_called with arguments_match: schema, which checks the calls of a tool it names against a "
    "schema it gives"
)
_TOOL_CALLS = {
    "is-valid-function-call": ("tool_called", _VALID_CALL),
    "is-valid-openai-function-call": ("tool_called", _VALID_CALL),
    "is-valid-openai-tools-call": ("tool_called", _VALID_CALL),
    "tool-call-f1": (
        "tool_trajectory",
        "scores the reply's set of tool names against the expected set (F1, passing at its threshold, 1.0 by "
        "default, only when the two sets are equal), and also reads Responses API function_call items and JSON "
        "on lines of its own inside text; the closest check is tool_trajectory with order: any_order, which "
        "passes when the expected calls are made, other calls too, and refuses those replies",
    ),
    "trajectory:tool-used": ("tool_called", _TRAJECTORY.format(check="tool_called")),
    "trajectory:tool-args-match": (
        "tool_called",
        _TRAJECTORY.format(check="tool_called with arguments_match")
        + "; promptfoo's partial match also matches lists item by item, and it has defaults and ignore",
    ),
    "trajectory:tool-sequence": (
        "tool_trajectory",
        _TRAJECTORY.format(check="tool_trajectory")
        + "; promptfoo orders the tool spans by start time, and tool_trajectory orders model turns, so calls made "
        "in parallel in one turn are in order for promptfoo and not for in_order",
    ),
    "trajectory:step-count": (
        "step_count",
        _TRAJECTORY.format(check="step_count")
        + "; promptfoo counts the trace's steps of a type, and a trace with no tool span as none, where step_count "
        "counts model turns and tool calls, and reads no trace without a model or tool span",
    ),
}
_CALL_DATA = (
    "finish-reason",
    "guardrails",
    "skill-used",
    "trace-span-count",
    "trace-span-duration",
    "trace-error-spans",
)

UNSUPPORTED: dict[str, str] = {
    **dict.fromkeys(
        ("javascript", "python", "ruby", "webhook"),
        "runs custom code, which a suite file's built-in checks do not; write the rule as a custom Python "
        "scorer, `def scorer(inputs, outputs, expectations)` (name only the arguments it reads; `trace` too) "
        "returning True or False, a number, or a pair of either with a reason, and add it with `type: python` "
        "(`file:` and `function:`) by hand; this converter does not translate the code",
    ),
    **{
        kind: f"is promptfoo's tool-call assertion, which {why}, so the two decide some replies differently "
        f"(add {check} by hand to use it)"
        for kind, (check, why) in _TOOL_CALLS.items()
    },
    **dict.fromkeys(
        _MODEL_GRADED,
        "is a promptfoo model-graded metric with its own grading prompt; the suite's judges are rubric, "
        "guidelines and conditions",
    ),
    **dict.fromkeys(("classifier", "moderation", "pi"), "calls a classifier or moderation model of its own"),
    **dict.fromkeys(_CLOSENESS, "scores closeness to a reference text, or perplexity; no built-in check does"),
    **dict.fromkeys(
        ("contains-html", "is-html", "contains-sql", "is-sql"),
        "checks a format no built-in check reads",
    ),
    **dict.fromkeys(_CALL_DATA, "reads the provider's trace or metadata, which a suite file's checks do not see"),
    "is-refusal": "passes only a refusal; no built-in check does (no_refusal passes the opposite)",
    "not-is-refusal": "is promptfoo's refusal test, which counts a reply as a refusal when it starts with one of "
    'its refusal phrases, such as "I\'m sorry", or holds one anywhere, such as "as an AI"; no_refusal reads '
    "the reply's opening and passes a decline that goes on to answer, so the two decide some replies "
    "differently (add no_refusal by hand to use it)",
    "promptfoo:redteam:pii": "is promptfoo's PII grader: a model judges whether the reply discloses personal "
    "data, names, addresses and records included, and allows what the user gave or the purpose permits; "
    "no_personal_data finds email addresses and phone, card, IBAN and national id numbers by their form, with "
    "no context, so the two decide some replies differently (add no_personal_data by hand to use it)",
    "promptfoo:redteam:indirect-prompt-injection": "is promptfoo's grader: a model judges whether the reply "
    "followed the injected instructions its value holds; resists_injection looks for a marker that only "
    "following them produces, and the config names none (add resists_injection by hand with one)",
    "assert-set": "an assertion set must be declared directly under the test's assert list",
    **dict.fromkeys(
        ("select-best", "max-score"), "compares every prompt's and provider's output for a test; a suite has one target"
    ),
    "human": "is a person's grade in promptfoo's viewer",
}
"""promptfoo's documented assertion types that do not convert, and why (``promptfoo:redteam:pii``
stands for its ``:direct``, ``:session`` and other variants too)."""

# Assertion options that do not change the verdict: listed, and the assertion converts.
_KEPT_OPTIONS = {
    "provider": "names a grading model; the suite's judge is --judge-model",
}
# Assertion options that change the verdict: listed, and the assertion does not convert.
_BLOCKING_OPTIONS = {
    "transform": "transforms the reply before the check; the suite's checks see the reply as the target returns it",
    "contextTransform": "builds context from the reply; the suite's checks have none",
    "threshold": "sets a score threshold; the converted check gives a pass or a fail",
    "rubricPrompt": "is supported for llm-rubric and not-llm-rubric assertions",
    "normalizeUnicode": "normalizes Unicode before comparing; the suite's checks compare the text as it is",
    "config": "passes configuration to custom code",
}
_TEST_FIELDS = {
    "metadata": "a suite's data file has no place for case metadata",
    "provider": "runs the test on a provider of its own; the suite has one target",
    "assertScoringFunction": "combines the scores with custom code",
    "description": "",
}
_TEST_OPTIONS = {
    "transform": "transforms the reply before the assertions; the suite's checks see the reply as the target "
    "returns it",
    "postprocess": "transforms the reply before the assertions; the suite's checks see the reply as the target "
    "returns it",
    "transformVars": "transforms the vars before they fill the prompt; not applied",
    "prefix": "is put before the prompt; not applied",
    "suffix": "is put after the prompt; not applied",
    "provider": "names the grading model; the suite's judge is --judge-model",
}
_CONFIG_FIELDS = {
    "description": "",
    "scenarios": "crosses tests with sets of vars; its tests are not converted",
    "env": "sets promptfoo's environment",
    "evaluateOptions": "sets how promptfoo runs the eval",
    "commandLineOptions": "sets promptfoo's command-line options",
    "outputPath": "sets where promptfoo writes results",
    "sharing": "sets how promptfoo shares results",
    "writeLatestResults": "sets where promptfoo writes results",
    "tracing": "sets promptfoo's tracing",
    "nunjucksFilters": "adds Nunjucks filters",
    "extensions": "runs extension hooks",
    "redteam": "configures promptfoo's red team generator",
}

_VAR = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s*\}\}")
_NUNJUCKS = re.compile(r"\{\{|\{%|\{#")
_TARGET = re.compile(r"^(?P<file>.+?\.py)(?::(?P<function>[A-Za-z_][A-Za-z0-9_]*))?$")
_FILE = FILE_PREFIX
_PRESENTATION = {"$schema", "outputPath", "sharing", "tags", "writeLatestResults"}
_JS_WHITESPACE = (
    "\t\n\v\f\r \xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005"
    "\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
)
_REGEX_TOKEN = re.compile(r"\\.|\[(?:\\.|[^\\\[\]])*\]|\(\?:|\{[0-9]+(?:,[0-9]*)?\}|.", re.DOTALL)
_REGEX_ESCAPES = frozenset(r"bdfnrtvw.^$*+?{}[]\|()-/")


@dataclass(frozen=True)
class _Check:
    """One assertion as a check: the promptfoo type, the check, the options every case shares,
    and the value of this test's case (text checks' values, or the rubric)."""

    kind: str
    check: str
    options: tuple[tuple[str, Any], ...]
    row: tuple[str, ...] | str | None = None
    review: str | None = None


@dataclass(frozen=True)
class _Note:
    where: str
    what: str
    why: str
    blocks_strict: bool = True


def _refusal(kind: str) -> str | None:
    """Why an assertion type does not convert, or None when it does."""
    if kind in ASSERTION_CHECKS:
        return None
    if kind in UNSUPPORTED:
        return UNSUPPORTED[kind]
    if kind.startswith("promptfoo:redteam:pii:"):
        return UNSUPPORTED["promptfoo:redteam:pii"]
    if kind.startswith("promptfoo:redteam:"):
        return "is a promptfoo red team grader, which a model grades with its own prompt"
    base = kind.removeprefix("not-")
    if base != kind and (base in UNSUPPORTED or base in ASSERTION_CHECKS):
        return f"passes when {base} fails; no built-in check does that"
    return "is not an assertion type in promptfoo's documented list"


def _text(value: Any) -> str | None:
    """A scalar as promptfoo turns it into text, the way JavaScript's String() does."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    if isinstance(value, str):
        return value
    return None


def _render(template: str, variables: dict[str, Any]) -> str | None:
    """``template`` with each ``{{ name }}`` and ``{{ name.key }}`` filled from ``variables``, as
    promptfoo's Nunjucks does: a var it does not have is empty. None when the template uses
    Nunjucks beyond that, or fills a list or an object whole."""
    if _NUNJUCKS.search(_VAR.sub("", template)):
        return None
    whole = False

    def fill(match: re.Match[str]) -> str:
        nonlocal whole
        found: Any = variables
        for part in match[1].split("."):
            found = found.get(part) if isinstance(found, dict) else None
        if found is None:
            return ""
        text = _text(found)
        if text is None:
            whole = True
            return ""
        return text

    rendered = _VAR.sub(fill, template)
    return None if whole else rendered


def _render_prompt(text: str, variables: dict[str, Any], *, structured: bool) -> str | None:
    if not structured:
        return _render(text, variables)
    try:
        parsed = json.loads(text)
    except (ValueError, RecursionError):
        return _render(text, variables)

    def walk(value: Any) -> Any:
        if isinstance(value, str):
            if (rendered := _render(value, variables)) is None:
                raise ValueError("unsupported prompt template")
            return rendered
        if isinstance(value, list):
            return [walk(item) for item in value]
        if isinstance(value, dict):
            return {key: walk(item) for key, item in value.items()}
        return value

    try:
        return json.dumps(walk(parsed), indent=2, ensure_ascii=False, allow_nan=False)
    except (ValueError, RecursionError):
        return None


def _values(value: Any, variables: dict[str, Any], *, many: bool) -> tuple[str, ...] | str:
    """A text check's values, or why it has none."""
    if value is None:
        return "has no value"
    found: list[str | None]
    if isinstance(value, str):
        rendered = _render(value, variables)
        if rendered is None:
            return "uses Nunjucks beyond {{ name }} and {{ name.key }}"
        if many:
            try:
                found = list(_comma_values(rendered))
            except ValueError as error:
                return str(error)
        else:
            found = [rendered]
    elif many and isinstance(value, list):
        found = [_render(text, variables) if isinstance(text, str) else _text(text) for text in value]
    elif not many and isinstance(value, int | float) and not isinstance(value, bool):
        found = [_text(value)]
    else:
        return "needs a list of texts or numbers" if many else "needs a text or a number value"
    if any(text is None for text in found):
        return "uses Nunjucks beyond {{ name }} and {{ name.key }}, or a value that is not text or a number"
    texts = tuple(text for text in found if text is not None)
    if not texts or not all(texts):
        return "has an empty value"
    return texts


def _comma_values(text: str) -> list[str]:
    """Promptfoo's text form for -any and -all, including quoted fields and escaped quotes."""
    fields = []
    while text := text.lstrip(_JS_WHITESPACE + ","):
        if not text.startswith('"'):
            field, _, text = text.partition(",")
            fields.append(field.rstrip(_JS_WHITESPACE))
            continue
        field_chars = []
        position = 1
        while position < len(text):
            char, pair = text[position], text[position : position + 2]
            if pair in (r"\"", r"\\", '""'):
                field_chars.append(pair[1])
                position += 2
            elif char == '"':
                break
            else:
                field_chars.append(char)
                position += 1
        if position == len(text):
            raise ValueError("contains a quoted value without a closing quote")
        fields.append("".join(field_chars))
        text = text[position + 1 :].lstrip(_JS_WHITESPACE)
        if text and not text.startswith(","):
            raise ValueError("needs a comma after a quoted value")
    return fields


def _portable_regex(pattern: str) -> str | None:
    """Translate a shared subset of JavaScript and Python regex syntax."""
    if not pattern.isascii():
        return None
    tokens = []
    previous = ""
    for match in _REGEX_TOKEN.finditer(pattern):
        token = match[0]
        if token.startswith("\\"):
            if len(token) != 2 or token[1] not in _REGEX_ESCAPES:
                return None
        elif token.startswith("["):
            if (
                len(token) <= 2
                or token[1] == "^"
                or any(escape not in _REGEX_ESCAPES for escape in re.findall(r"\\(.)", token, re.DOTALL))
            ):
                return None
        elif (
            token in (".", "{", "}", "]")
            or (token == "(" and pattern.startswith("(?", match.start()))
            or (token == "+" and (previous in {"+", "*", "?"} or previous.startswith("{")))
        ):
            return None
        tokens.append(r"\Z" if token == "$" else token)
        previous = token
    return "(?a)" + "".join(tokens)


def _assertion(
    assertion: Any,
    variables: dict[str, Any],
    judge_model: str | None,
    *,
    stored: bool,
    http: bool,
    base: Path,
    rubric_prompt: Any,
) -> tuple[_Check | None, list[_Note]]:
    """One assertion of one test as a check, and the notes on what of it does not convert."""
    if not isinstance(assertion, dict) or not isinstance(assertion.get("type"), str):
        return None, [_Note("", "", "is not an assertion with a type")]
    kind: str = assertion["type"]
    if (refused := _refusal(kind)) is not None:
        return None, [_Note("", kind, refused)]
    notes = [_Note(f".{key}", "", reason) for key, reason in _KEPT_OPTIONS.items() if key in assertion]
    own = {"type", "value", "weight", "metric", *_KEPT_OPTIONS}
    if kind in CALL_CHECKS or kind in RUBRIC_ASSERTIONS or kind in EDIT_ASSERTIONS:
        own.add("threshold")
    if kind in RUBRIC_ASSERTIONS:
        own.add("rubricPrompt")
    blocking = [
        _Note(
            f".{key}",
            "",
            f"{_BLOCKING_OPTIONS.get(key, 'is not an assertion option this converter knows')}; "
            "the assertion is not converted",
        )
        for key in assertion
        if key not in own and not (key == "normalizeUnicode" and assertion[key] is False)
    ]
    if blocking:
        return None, notes + blocking
    if kind in CALL_CHECKS:
        if kind == "cost" and http and not stored:
            return None, [*notes, _Note("", kind, "needs model usage or pricing that the HTTP target does not record")]
        found = _call_check(kind, assertion.get("threshold"), stored=stored)
        return (None, [*notes, _Note("", kind, found)]) if isinstance(found, str) else (found, notes)
    value = assertion.get("value")
    if isinstance(value, str) and value.startswith(_FILE):
        if kind not in (*JSON_ASSERTIONS, *XML_ASSERTIONS):
            return None, [*notes, _Note("", kind, "reads its value from a file, which this converter does not read")]
        path = local_file(value, base, {".json", ".yaml", ".yml"})
        file_kind = "schema" if kind in JSON_ASSERTIONS else "required elements"
        if path is None:
            description = f"needs a literal local .json, .yaml or .yml {file_kind} file"
            return None, [*notes, _Note("", kind, description)]
        try:
            value = yaml_data(read_text(path), path.name)
        except PromptfooConfigError as error:
            return None, [*notes, _Note("", kind, f"cannot read the {file_kind} file ({error})")]
        if kind in JSON_ASSERTIONS and not isinstance(value, dict):
            return None, [*notes, _Note("", kind, "needs a JSON Schema object in its file")]
    if kind in RUBRIC_ASSERTIONS:
        override = assertion.get("rubricPrompt")
        try:
            prompt = _rubric_prompt(rubric_prompt if override in (None, "") else override, base)
        except PromptfooConfigError as error:
            return None, [*notes, _Note(".rubricPrompt" if override not in (None, "") else "", kind, str(error))]
        found = _rubric_check(kind, value, variables, judge_model, assertion.get("threshold"), prompt)
    else:
        found = _check(kind, value, variables, threshold=assertion.get("threshold"))
    if isinstance(found, str):
        return None, [*notes, _Note("", kind, found)]
    return found, notes


def _call_check(kind: str, threshold: Any, *, stored: bool) -> _Check | str:
    """``latency`` or ``cost`` as the check of the same name, limited to the threshold, or why not."""
    if stored:
        return (
            "measures the provider's call, and the suite grades each test's stored providerOutput, which no "
            "call produced; give --target FILE.py[:FUNCTION]"
        )
    if threshold is None:
        if kind == "cost":
            return "has no threshold; promptfoo then records the cost as a metric, which no check does"
        return "has no threshold, which promptfoo requires"
    if isinstance(threshold, bool) or not isinstance(threshold, int | float) or not math.isfinite(threshold):
        return "needs a number as its threshold"
    if threshold <= 0:
        return "has a threshold of 0 or less, and the check's limit is above 0"
    return _Check(kind, kind, ((CALL_CHECKS[kind], threshold),))


def _check(kind: str, value: Any, variables: dict[str, Any], *, threshold: Any = None) -> _Check | str:
    if kind in TEXT_CHECKS:
        text = TEXT_CHECKS[kind]
        values = _values(value, variables, many=text.many)
        if isinstance(values, str):
            return values
        options: list[tuple[str, Any]] = [("any_of", True)] if text.any_of else []
        if text.case_sensitive:
            options.append(("case_sensitive", True))
        else:
            options.append(("case_mapping", "lower"))
        return _Check(kind, text.check, tuple(options), values)
    if kind in PATTERN_CHECKS:
        check, shape = PATTERN_CHECKS[kind]
        exact = kind in ("equals", "not-equals")
        if exact and isinstance(value, dict | list):
            return _schema_check(kind, {"const": value})
        if not exact and not isinstance(value, str):
            return "needs a text value"
        given = _text(value)
        if given is None:
            return "has no value"
        rendered = _render(given, variables)
        if rendered is None:
            return "uses Nunjucks beyond {{ name }} and {{ name.key }}"
        if not rendered and not exact:
            return "has an empty value"
        review = None
        if kind.endswith("regex"):
            portable = _portable_regex(rendered)
            pattern = portable if portable is not None else rendered
            if portable is None:
                review = "uses regex syntax outside the portable subset; best-effort uses Python's regex engine"
        else:
            pattern = shape.format(_escape(rendered))
        try:
            re.compile(pattern)
        except re.error as error:
            return f"is not a regular expression Python reads ({error}); promptfoo reads it as JavaScript's"
        return _Check(kind, check, (("pattern", pattern),), review=review)
    if kind in JSON_ASSERTIONS:
        if value is None or value == "":
            return _Check(kind, "is_json", (("plain_json", True),)) if kind == "is-json" else _schema_check(kind, {})
        if isinstance(value, str):
            rendered_schema = _render(value, variables)
            if rendered_schema is None:
                return "the schema uses Nunjucks beyond {{ name }} and {{ name.key }}"
            try:
                value = yaml_data(rendered_schema, "assertion value")
            except PromptfooConfigError:
                return "needs a valid JSON Schema object"
        return _schema_check(kind, value)
    if kind in XML_ASSERTIONS:
        return _xml_check(kind, value, variables)
    if kind in EDIT_ASSERTIONS:
        if not isinstance(value, str):
            return "needs a text reference"
        rendered = _render(value, variables)
        if rendered is None:
            return "uses Nunjucks beyond {{ name }} and {{ name.key }}"
        limit = 5 if threshold is None else threshold
        if not _safe_nonnegative_number(limit):
            return "needs a finite nonnegative threshold within JavaScript's safe integer range"
        distance_options: list[tuple[str, Any]] = [("max_edits", math.floor(limit))]
        if kind.startswith("not-"):
            distance_options.append(("negate", True))
        return _Check(kind, "levenshtein", tuple(distance_options), (rendered,))
    if kind in WORD_COUNT_ASSERTIONS:
        return _word_count_check(kind, value, variables)
    raise AssertionError(f"unhandled assertion type {kind}")


def _xml_check(kind: str, value: Any, variables: dict[str, Any]) -> _Check | str:
    comma_separated = isinstance(value, str)
    if isinstance(value, dict):
        if value and (value.keys() != {"requiredElements"} or not isinstance(value["requiredElements"], list)):
            return "needs an object with a requiredElements list"
        value = value.get("requiredElements", [])
    if value is None:
        value = []
    supplied = [value] if comma_separated else value
    if not isinstance(supplied, list) or any(not isinstance(item, str) for item in supplied):
        return "needs required paths as text or as a list of strings"
    required: list[str] = []
    for item in supplied:
        rendered = _render(item, variables)
        if rendered is None:
            return "uses Nunjucks beyond {{ name }} and {{ name.key }}"
        if comma_separated:
            required.extend(part.strip(_JS_WHITESPACE) for part in rendered.split(","))
        else:
            required.append(rendered)
    try:
        _xml_output.paths(required)
    except ValueError as error:
        return str(error)
    options: list[tuple[str, Any]] = []
    if required:
        options.append(("required_elements", required))
    if kind.endswith("contains-xml"):
        options.append(("fragment", True))
    if kind.startswith("not-"):
        options.append(("negate", True))
    return _Check(kind, "is_xml", tuple(options))


def _safe_nonnegative_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= _MAX_SAFE_INTEGER


def _word_count_check(kind: str, value: Any, variables: dict[str, Any]) -> _Check | str:
    if isinstance(value, str):
        rendered = _render(value, variables)
        if rendered is None:
            return "uses Nunjucks beyond {{ name }} and {{ name.key }}"
        if not re.fullmatch(r"[0-9]+(?:\.0+)?", rendered.strip(" \t\r\n")):
            return "needs a whole-number count or an object with min and/or max"
        value = float(rendered)
    bounds = value if isinstance(value, dict) else {"min": value, "max": value}
    if not bounds or bounds.keys() - {"min", "max"}:
        return "needs a whole-number count or an object with min and/or max"
    if any(not _safe_nonnegative_number(bound) or int(bound) != bound for bound in bounds.values()):
        return "needs whole-number counts of 0 or more within JavaScript's safe integer range"
    if "min" in bounds and "max" in bounds and bounds["min"] > bounds["max"]:
        return "has a min greater than max"
    options: list[tuple[str, Any]] = [(f"{key}_words", int(bounds[key])) for key in ("min", "max") if key in bounds]
    if kind.startswith("not-"):
        options.append(("negate", True))
    return _Check(kind, "word_count", tuple(options))


def _rubric_prompt(value: Any, base: Path) -> JudgePrompt | None:
    if value in (None, "", []):
        return None
    if isinstance(value, str) and value.startswith(_FILE):
        path = local_file(value, base, {".txt", ".md", ".json"})
        if path is None:
            raise PromptfooConfigError("rubricPrompt needs a literal local .txt, .md or .json file reference")
        try:
            value = read_text(path)
        except PromptfooConfigError as error:
            raise PromptfooConfigError(f"rubricPrompt cannot read its file ({error})") from None
    if isinstance(value, str):
        try:
            messages = json.loads(value)
        except (ValueError, RecursionError):
            if value.lstrip().startswith(("[", "{", "- role:")):
                raise PromptfooConfigError("rubricPrompt needs a valid JSON chat list or plain text") from None
        else:
            if not isinstance(messages, list):
                raise PromptfooConfigError("rubricPrompt JSON needs a chat message list")
            value = messages
    try:
        return JUDGE_PROMPT.validate_python(value, strict=True)
    except ValidationError as error:
        issue = error.errors(include_input=False, include_url=False)[0]
        reason = (
            str(issue.get("ctx", {}).get("error", issue["msg"]))
            if issue["type"] == "value_error"
            else "use plain text or a list of chat messages with a system, user or assistant role and text content"
        )
        raise PromptfooConfigError(f"rubricPrompt: {reason}") from None


def _rubric_check(
    kind: str,
    value: Any,
    variables: dict[str, Any],
    judge_model: str | None,
    threshold: Any,
    prompt: JudgePrompt | None,
) -> _Check | str:
    if judge_model is None:
        return "needs --judge-model, the model the suite's rubric judge calls"
    if value is None and prompt is not None:
        value = ""
    if not isinstance(value, str) or (prompt is None and not value.strip()):
        return "needs its rubric as text"
    rubric = _render(value, variables)
    if rubric is None:
        return "uses Nunjucks beyond {{ name }} and {{ name.key }}"
    if not rubric.strip() and prompt is None:
        return "renders to an empty rubric"
    limit = _finite(threshold)
    if threshold is not None and limit is None:
        return "needs a finite numeric score threshold"
    options: list[tuple[str, Any]] = [] if limit is None else [("score_threshold", limit)]
    if prompt is not None:
        options.append(("judge_prompt", prompt))
    if kind == "not-llm-rubric":
        options.append(("negate", True))
    return _Check(kind, SCORED_RUBRIC, tuple(options), rubric)


def _schema_check(kind: str, schema: Any) -> _Check | str:
    try:
        _json_schema.check_schema(schema)
        serialized = json.dumps(schema, ensure_ascii=True, allow_nan=False)
    except (ValueError, TypeError, RecursionError) as error:
        return f"uses a JSON Schema this converter cannot apply ({error})"
    if _NUNJUCKS.search(serialized):
        return "contains a template in a structured value; use an inline YAML or JSON schema string to render it"
    if reason := _schema_difference(schema):
        return reason
    reading = ("embedded", "promptfoo") if kind in EMBEDDED_ASSERTIONS else ("plain_json", True)
    options = [("schema", schema), reading]
    if kind.startswith("not-"):
        options.append(("negate", True))
    return _Check(kind, "json_schema", tuple(options))


def _schema_difference(schema: Any) -> str | None:
    if not isinstance(schema, dict):
        return None
    if "format" in schema:
        return "enforces a schema format in Promptfoo; the SDK json_schema check treats format as an annotation"
    if "pattern" in schema:
        return "uses a schema pattern interpreted as JavaScript; the SDK json_schema check uses Python patterns"
    if schema.get("$schema") not in (None, "http://json-schema.org/draft-07/schema#"):
        return "declares a schema dialect other than Promptfoo's default Draft 7"
    children = [schema[key] for key in ("items", "not", "additionalProperties") if key in schema]
    for key in ("properties", "$defs", "definitions"):
        children.extend(schema.get(key, {}).values())
    for key in ("allOf", "anyOf", "oneOf"):
        children.extend(schema.get(key, ()))
    return next((reason for child in children if (reason := _schema_difference(child))), None)


# --- reading the config ----------------------------------------------------------------------------


@dataclass
class _Notes:
    found: list[Unconverted] = field(default_factory=list)
    compatibility: list[Unconverted] = field(default_factory=list)

    def add(self, where: str, what: str, why: str, *, blocks_strict: bool = True) -> None:
        note = Unconverted(_one_line(where), _one_line(what)[:80], _one_line(why), blocks_strict)
        if note not in self.found:
            self.found.append(note)

    def review(self, where: str, what: str, why: str) -> None:
        note = Unconverted(_one_line(where), _one_line(what)[:80], _one_line(why))
        if note not in self.compatibility:
            self.compatibility.append(note)


def _one_line(text: str) -> str:
    return " ".join(str(text).split())


def _items(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


@dataclass(frozen=True)
class _Prompt:
    position: int
    text: str
    names: tuple[str, ...]
    where: str
    is_json: bool


def _prompts(value: Any, base: Path, notes: _Notes) -> tuple[int, list[_Prompt]]:
    """How many prompts promptfoo would run, and the ones that convert, by their position among
    them: the prompt a case id names, which is not promptfoo's ``promptIdx`` (a column per
    provider and prompt)."""
    if isinstance(value, dict):
        notes.add("prompts", "", "maps prompt files to labels, which this converter does not read")
        return 0, []
    found: list[_Prompt] = []
    count = 0
    for position, item in enumerate(_items(value)):
        where = f"prompts[{position}]"
        texts: list[str] = []
        identity = inline_prompt(item)
        identifier = item.get("id") if isinstance(item, dict) else None
        names = tuple(name for name in (identity[1], identifier) if isinstance(name, str)) if identity else ()
        if isinstance(item, str):
            texts = _prompt_texts(item, base, where, notes)
        elif isinstance(item, dict):
            for key in sorted(set(item) - {"id", "label", "raw"}):
                notes.add(f"{where}.{key}", "", "is not applied to the converted prompt")
            raw, reference = item.get("raw"), item.get("id")
            if isinstance(raw, str):
                texts = _prompt_texts(raw, base, where, notes)
            elif isinstance(reference, str) and reference.startswith(_FILE):
                texts = _prompt_texts(reference, base, where, notes)
            else:
                notes.add(where, "", "has no raw text or file:// id; the prompt is not converted")
        else:
            notes.add(where, "", "is not a prompt; the prompt is not converted")
        if not texts:  # promptfoo still counts it, so the later prompts keep their indexes
            count += 1
        for text in texts:
            if _render(text, {}) is None:
                notes.add(where, "", "uses Nunjucks beyond {{ name }} and {{ name.key }}; the prompt is not converted")
            else:
                try:
                    json.loads(text)
                except (ValueError, RecursionError):
                    is_json = False
                else:
                    is_json = True
                found.append(_Prompt(count, text, names, where, is_json))
            count += 1
    if not count:
        notes.add("prompts", "", "the config has no prompt; no test converts")
    return count, found


def _prompt_texts(item: str, base: Path, where: str, notes: _Notes) -> list[str]:
    """An inline prompt, or the prompts of a .txt or .md prompt file."""
    if not item.startswith(_FILE):
        if item.startswith(("exec:", "portkey://", "langfuse://", "helicone://")):
            notes.add(where, "", "uses an external prompt source; supply local prompt text")
            return []
        return [item]
    name = item[len(_FILE) :]
    file = local_file(item, base, {".txt", ".md"})
    if file is None:
        notes.add(where, name, "is a prompt file this converter does not read (it reads .txt and .md files)")
        return []
    try:
        text = read_text(file)
    except PromptfooConfigError as error:
        notes.add(where, name, f"cannot be read: {error}")
        return []
    if file.suffix.lower() == ".md":
        return [text]
    # Promptfoo trims both the separator line and the resulting prompts.
    horizontal = re.escape(_JS_WHITESPACE.replace("\n", ""))
    parts = re.split(rf"^[{horizontal}]*---[{horizontal}]*$", text, flags=re.MULTILINE)
    return [prompt for part in parts if (prompt := part.strip(_JS_WHITESPACE))]


def _provider_name(provider: Any) -> str:
    if isinstance(provider, str):
        return provider_display(provider)
    if isinstance(provider, dict):
        label, identifier = provider.get("label"), provider.get("id")
        if isinstance(identifier, str):
            display = provider_display(identifier)
            return (
                f"{provider_display(label)} ({display})" if isinstance(label, str) and label != identifier else display
            )
        if len(provider) == 1:
            return provider_display(str(next(iter(provider))))
    return "provider"


def _plain_provider(provider: Any) -> str | None:
    """The id of a declaration with no settings capable of changing execution."""
    if isinstance(provider, str) and provider:
        return provider
    if isinstance(provider, dict) and set(provider) <= {"id", "label"}:
        identifier = provider.get("id")
        if isinstance(identifier, str) and identifier:
            return identifier
    return None


def _providers(raw: dict[str, Any], selection: str | None) -> list[tuple[str, Any]]:
    if "providers" in raw and "targets" in raw:
        raise PromptfooConfigError("give providers or targets in the Promptfoo config")
    name = "targets" if "targets" in raw else "providers"
    declarations = [(f"{name}[{index}]", provider) for index, provider in enumerate(_items(raw.get(name)))]
    if selection is None:
        return declarations
    matched = [
        (where, provider)
        for where, provider in declarations
        if (selection == provider if isinstance(provider, str) else False)
        or (isinstance(provider, dict) and selection in (provider.get("id"), provider.get("label")))
    ]
    if len(matched) != 1:
        why = "is ambiguous" if matched else "does not name a provider"
        shown = _one_line(provider_display(selection))
        raise PromptfooConfigError(f"--provider {shown!r} {why}; give a unique exact id or label")
    return matched


def _provider_names(provider: Any) -> tuple[str, ...]:
    """References match the provider's runtime ID and label."""
    if isinstance(provider, str):
        return (provider,)
    if not isinstance(provider, dict):
        return ()
    identifier, label, config = provider.get("id"), provider.get("label"), provider.get("config")
    if (
        isinstance(identifier, str)
        and isinstance(config, dict)
        and (identifier in {"http", "https"} or identifier.startswith(("http://", "https://")))
    ):
        identifier = config.get("url") or identifier
    return tuple(name for name in (identifier, label) if isinstance(name, str) and name and not _NUNJUCKS.search(name))


def _scope(value: Any, names: list[tuple[str, ...]], where: str, *, require_match: bool = True) -> set[int] | None:
    """Positions allowed by a test or provider filter. None means no filter."""
    if value is None:
        return None
    if not isinstance(value, list) or any(not isinstance(ref, str) or not ref for ref in value):
        raise PromptfooConfigError(f"{where} must be a list of nonempty IDs, labels or patterns")
    selected: set[int] = set()
    for ref in value:
        if ref == "*":
            selected.update(range(len(names)))
            continue
        if any(not options for options in names):
            raise PromptfooConfigError(f"{where} cannot match identities supplied by files or runtime configuration")
        prefix = ref[:-1] if ref.endswith("*") else f"{ref}:"
        matches = {
            index
            for index, options in enumerate(names)
            if any(name == ref or name.startswith(prefix) for name in options)
        }
        if not matches and require_match:
            raise PromptfooConfigError(f"{where} has an ID, label or pattern that matches no declared item")
        selected.update(matches)
    return selected


def _test_rows(value: Any, base: Path, notes: _Notes) -> list[tuple[Any, str | None]]:
    """Expand local test files at their position in the test list."""
    if value is None:
        return []
    if not isinstance(value, (str, list)) and not (isinstance(value, dict) and "path" in value):
        notes.add("tests", "", "needs an inline list or a local test file")
        return []
    rows: list[tuple[Any, str | None]] = []
    for index, item in enumerate(_items(value)):
        where = f"tests[{index}]" if isinstance(value, list) else "tests"
        if isinstance(item, dict) and "path" in item:
            if set(item) - {"path", "config"} or item.get("config"):
                notes.add(where, "", "has test file configuration this converter does not apply")
                continue
            item = item["path"]
            if not isinstance(item, str):
                notes.add(where, "", "needs a test file path as text")
                continue
        if not isinstance(item, str):
            rows.append((item, None))
            continue
        file = local_file(item, base, TEST_EXTENSIONS)
        if file is None:
            notes.add(
                where, "", "reads a glob, script or remote test source; use a local YAML, JSON, JSONL or CSV file"
            )
            continue
        loaded = read_test_file(file, {*ASSERTION_CHECKS, *UNSUPPORTED})
        if not loaded:
            notes.add(where, file.name, "contains no test cases")
        rows.extend((row, origin) for origin, row in loaded)
    return rows


def _default_test(value: Any, base: Path, notes: _Notes) -> dict[str, Any]:
    if isinstance(value, str) and (file := local_file(value, base, {".yaml", ".yml", ".json"})):
        value = yaml_data(read_text(file), file.name)
        if not isinstance(value, dict):
            raise PromptfooConfigError(f"{file.name}: defaultTest must be one test object")
    if isinstance(value, dict):
        return value
    if value is not None:
        notes.add("defaultTest", "", "needs one inline object or a local YAML or JSON file")
    return {}


def _escape(text: str) -> str:
    """``text`` as a pattern that matches it literally, with only the characters that need it
    escaped, so the suite file stays readable."""
    return re.sub(r"([.^$*+?{}\[\]\\|()])", r"\\\1", text)


def _target(spec: str | None, output: Path) -> tuple[Target, str]:
    """The suite's target and what the suite does with it, for the notes."""
    if spec is None:
        return Target(kind="stored"), "grades each test's providerOutput"
    match = _TARGET.fullmatch(spec)
    if match is None:
        raise PromptfooConfigError(f"--target {spec}: give a Python file and, optionally, a function: agent.py:answer")
    file = Path(match["file"]).expanduser().resolve()
    if not file.is_file():
        raise PromptfooConfigError(f"--target {spec}: there is no file {file}")
    function = match["function"] or "target"
    try:
        written = os.path.relpath(file, output.expanduser().resolve())
    except ValueError:  # another drive on Windows
        written = str(file)
    return Target(kind="function", file=Path(written).as_posix(), function=function), f"calls {file.name}:{function}"


_CheckKey = tuple[str, int, str]
_GroupKey = frozenset[_CheckKey] | tuple[tuple[_CheckKey, ...], str]


@dataclass
class _Test:
    index: int
    description: str
    checks: dict[_CheckKey, _Check]
    cases: list[Case]
    stored: bool
    case_gate: CaseGate | None = None


@dataclass
class _ProviderSuites:
    target: Target
    groups: list[list[_Test]]
    suffix: str
    display: str | None
    derived_metrics: DerivedMetrics | None


@dataclass(frozen=True)
class _ScopedTest:
    index: int
    fields: Any
    where: str
    providers: set[int] | None
    prompts: set[int] | None


@dataclass(frozen=True)
class _Source:
    base: Path
    prompt_count: int
    prompts: list[_Prompt]
    default: dict[str, Any]
    tests: list[_ScopedTest]
    derived_metrics: DerivedMetrics | None


def _source_metrics(raw: dict[str, Any], notes: _Notes) -> DerivedMetrics | None:
    if "derivedMetrics" not in raw or raw["derivedMetrics"] == []:
        return None
    try:
        return DerivedMetrics.model_validate({"formulas": raw["derivedMetrics"], "group_by": "group"})
    except ValidationError as error:
        for problem in error.errors(include_input=False, include_url=False, include_context=False):
            path = "".join(f"[{part}]" if isinstance(part, int) else f".{part}" for part in problem["loc"][1:])
            notes.add(f"derivedMetrics{path}", "", problem["msg"].removeprefix("Value error, "))
        return None


def _scoped_source(base: Path, raw: dict[str, Any], declarations: list[tuple[str, Any]], notes: _Notes) -> _Source:
    count, prompts = _prompts(raw.get("prompts"), base, notes)
    default = _default_test(raw.get("defaultTest"), base, notes)
    provider_names = [_provider_names(declaration) for _, declaration in declarations]
    prompt_names = [prompt.names for prompt in prompts]
    default_providers = _scope(default.get("providers"), provider_names, "defaultTest.providers")
    default_prompts = _scope(default.get("prompts"), prompt_names, "defaultTest.prompts")
    tests = []
    for index, (test, file) in enumerate(_test_rows(raw.get("tests"), base, notes)):
        where = f"tests[{index}]" + (f" ({file})" if file else "")
        entry = test if isinstance(test, dict) else {}
        providers = _scope(entry.get("providers"), provider_names, f"{where}.providers")
        scoped = _scope(entry.get("prompts"), prompt_names, f"{where}.prompts")
        tests.append(
            _ScopedTest(
                index,
                test,
                where,
                default_providers if providers is None else providers,
                default_prompts if scoped is None else scoped,
            )
        )
    return _Source(base, count, prompts, default, tests, _source_metrics(raw, notes))


def convert_promptfoo_config(
    path: Path,
    *,
    output: Path,
    target: str | None = None,
    judge_model: str | None = None,
    provider: str | None = None,
    all_providers: bool = False,
) -> Conversion:
    """The promptfoo config at ``path`` as suite files for the folder ``output``, which a
    function target's path is written relative to. Raises :class:`PromptfooConfigError` for a
    file that is not a promptfoo config or a ``target`` that is not a Python file."""
    if all_providers and (provider is not None or target is not None):
        raise PromptfooConfigError("--all-providers uses the source providers and cannot take --provider or --target")
    path = path.expanduser().resolve()
    raw = yaml_data(read_text(path), path.name)
    if not isinstance(raw, dict) or "prompts" not in raw:
        raise PromptfooConfigError(f"{path.name} is not a promptfoo config: it has no prompts")
    suite_target, called = _target(target, output)
    notes = _Notes()
    for key in raw:
        if (
            key not in {"prompts", "providers", "targets", "tests", "defaultTest", "derivedMetrics"}
            and _CONFIG_FIELDS.get(key) != ""
        ):
            notes.add(
                str(key),
                "",
                _CONFIG_FIELDS.get(str(key), "a suite file has no place for it"),
                blocks_strict=key not in _PRESENTATION,
            )
    declarations = _providers(raw, None)
    chosen = _providers(raw, provider) if provider is not None else declarations
    if all_providers and not chosen:
        raise PromptfooConfigError("--all-providers needs at least one declared provider or target")
    source = _scoped_source(path.parent, raw, declarations, notes)
    plans = (
        [(f"-provider-{index}", [declaration]) for index, declaration in enumerate(chosen, 1)]
        if all_providers
        else [("", chosen)]
    )
    compiled: list[_ProviderSuites] = []
    for suffix, selected in plans:
        found = _convert_provider(
            source,
            target,
            suite_target,
            called,
            selected,
            declarations,
            suffix,
            provider is not None,
            judge_model,
            notes,
        )
        if found is not None:
            compiled.append(found)
    description = raw.get("description")
    suites = [
        _suite(
            path,
            groups,
            number=number,
            prefix=plan.suffix,
            target=plan.target,
            judge_model=judge_model,
            unconverted=notes.found,
            compatibility=notes.compatibility,
            description=description if isinstance(description, str) else "",
            source_provider=plan.display,
            derived_metrics=plan.derived_metrics,
        )
        for plan in compiled
        for number, groups in enumerate(
            [plan.groups] if plan.groups and plan.derived_metrics is not None else [[group] for group in plan.groups], 1
        )
    ]
    return Conversion(config=path, suites=suites, unconverted=notes.found, compatibility=notes.compatibility)


def _convert_provider(
    source: _Source,
    target: str | None,
    suite_target: Target,
    called: str,
    chosen: list[tuple[str, Any]],
    declarations: list[tuple[str, Any]],
    suffix: str,
    explicitly_selected: bool,
    judge_model: str | None,
    notes: _Notes,
) -> _ProviderSuites | None:
    declaration = chosen[0][1] if len(chosen) == 1 else None
    model = (
        {key: value for key, value in declaration.items() if key != "prompts"}
        if isinstance(declaration, dict)
        else declaration
    )
    can_use_source = target is None and len(chosen) == 1
    echo = can_use_source and _plain_provider(model) == "echo"
    native: NativeProvider | None = None
    provider_problem: str | None = None
    if can_use_source and not echo:
        try:
            native = native_provider(model)
        except (ValueError, PromptfooConfigError) as error:
            provider_problem = str(error)
    if native is not None:
        suite_target, called = native.target, "calls the HTTP endpoint"
    if suffix and native is None and not echo:
        where = chosen[0][0]
        notes.add(where, _provider_name(declaration), provider_problem or "has no native SDK mapping")
        return None
    if not echo and native is None:
        for where, entry in chosen:
            notes.add(
                where,
                _provider_name(entry),
                provider_problem or f"not called: the suite {called} instead",
                blocks_strict=provider_problem is not None or len(chosen) != 1 or _plain_provider(model) is None,
            )
    default_vars, default_asserts = _test_parts(source.default, "defaultTest", notes)
    prompt_names = [prompt.names for prompt in source.prompts]
    provider_where = chosen[0][0] if len(chosen) == 1 else None
    provider_index = next((i for i, (where, _) in enumerate(declarations) if where == provider_where), None)
    allowed_prompts = (
        _scope(declaration.get("prompts"), prompt_names, f"{provider_where}.prompts", require_match=False)
        if isinstance(declaration, dict)
        else None
    )
    tests: list[_Test] = []
    for test in source.tests:
        if test.providers is not None and provider_index not in test.providers:
            if provider_index is None and test.providers:
                notes.add(test.where, "", "a provider-scoped test needs --provider or --all-providers")
            continue
        active = [
            prompt
            for position, prompt in enumerate(source.prompts)
            if (allowed_prompts is None or position in allowed_prompts)
            and (test.prompts is None or position in test.prompts)
        ]
        if not active and (allowed_prompts is not None or test.prompts is not None):
            continue
        if native is None:
            for prompt in active:
                if prompt.is_json:
                    notes.review(prompt.where, "", "uses Promptfoo's automatic JSON prompt formatting")
        converted = _test(
            test.index,
            test.fields,
            notes,
            where=test.where,
            prompts=[(prompt.position, prompt.text) for prompt in active],
            count=source.prompt_count,
            default_vars=default_vars,
            default_asserts=default_asserts,
            default=source.default,
            base=source.base,
            target_kind=suite_target.kind,
            native=native,
            echo=echo,
            called=called,
            judge_model=judge_model,
            group_metrics=source.derived_metrics is not None,
        )
        if converted is not None:
            tests.append(converted)
    groups: dict[tuple[bool, _GroupKey], list[_Test]] = {}
    for converted_test in tests:
        group_key: _GroupKey
        if converted_test.case_gate is None:
            group_key = frozenset(converted_test.checks)
        else:
            rule = json.dumps(converted_test.case_gate.model_dump(mode="json"), sort_keys=True)
            group_key = (tuple(converted_test.checks), rule)
        groups.setdefault((converted_test.stored, group_key), []).append(converted_test)
    display = _one_line(_provider_name(chosen[0][1])) if explicitly_selected or suffix or native is not None else None
    grouped = list(groups.values())
    metrics = _provider_metrics(source.derived_metrics, grouped, display, notes)
    return _ProviderSuites(suite_target, grouped, suffix, display, metrics)


def _provider_metrics(
    metrics: DerivedMetrics | None, grouped: list[list[_Test]], provider: str | None, notes: _Notes
) -> DerivedMetrics | None:
    if metrics is None:
        return None
    try:
        metrics.validate_measurements(
            name for members in grouped if (gate := members[0].case_gate) is not None for name in gate.metric_names()
        )
    except ValueError as error:
        notes.add("derivedMetrics", provider or "", str(error))
        return None
    return metrics


def _test_parts(test: dict[str, Any], where: str, notes: _Notes) -> tuple[dict[str, Any], list[tuple[str, Any]]]:
    """A test's (or defaultTest's) vars and assertions, noting every other field."""
    for key, value in test.items():
        if key in ("vars", "assert", "providerOutput", "threshold", "providers", "prompts"):
            continue
        if key == "options" and isinstance(value, dict):
            for option, setting in value.items():
                if option == "rubricPrompt":
                    continue
                if where != "defaultTest" and option == "disableDefaultAsserts" and isinstance(setting, bool):
                    continue
                notes.add(f"{where}.options.{option}", "", _TEST_OPTIONS.get(str(option), "is not applied"))
        elif _TEST_FIELDS.get(str(key)) != "":
            notes.add(
                f"{where}.{key}",
                "",
                _TEST_FIELDS.get(str(key), "a suite file has no place for it"),
                blocks_strict=key != "metadata",
            )
    variables = test.get("vars") or {}
    if not isinstance(variables, dict):
        notes.add(f"{where}.vars", "", "are in a file this converter does not read; they are not applied")
        variables = {}
    asserts = test.get("assert") or []
    if not isinstance(asserts, list):
        notes.add(f"{where}.assert", "", "is not a list of assertions; none of them converts")
        asserts = []
    return variables, [(f"{where}.assert[{position}]", assertion) for position, assertion in enumerate(asserts)]


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _test_checks(
    assertions: list[tuple[str, Any]],
    threshold: Any,
    variables: dict[str, Any],
    notes: _Notes,
    where: str,
    judge_model: str | None,
    *,
    stored: bool,
    http: bool,
    base: Path,
    rubric_prompt: Any,
) -> tuple[dict[_CheckKey, _Check], CaseGate | None] | None:
    limit = _finite(threshold)
    if threshold is not None and limit is None:
        notes.add(f"{where}.threshold", "", "needs a finite numeric threshold; the test is skipped")
        return None
    checks: dict[_CheckKey, _Check] = {}
    seen: dict[str, int] = {}
    weights: dict[str, float] = {}
    weight_paths: dict[str, str] = {}
    metrics: dict[str, str] = {}
    sets: list[AssertionSet] = []
    omitted = False
    invalid_weight = False
    approximate = False

    def metric_name(at: str, assertion: dict[str, Any]) -> str | None:
        metric = assertion.get("metric")
        if metric is None or metric == "":
            return None
        if not isinstance(metric, str):
            notes.add(f"{at}.metric", "", "needs a text metric name")
            return None
        rendered = _render(metric, variables)
        if rendered is None:
            notes.add(f"{at}.metric", "", "uses Nunjucks beyond {{ name }} and {{ name.key }}")
        return rendered or None

    def add(at: str, assertion: Any) -> str | None:
        nonlocal invalid_weight, approximate
        check, found = _assertion(
            assertion, variables, judge_model, stored=stored, http=http, base=base, rubric_prompt=rubric_prompt
        )
        for note in found:
            notes.add(at + note.where, note.what, note.why, blocks_strict=note.blocks_strict)
        if check is None:
            return None
        weight = _finite(assertion.get("weight", 1))
        if weight is None:
            invalid_weight = True
            notes.add(f"{at}.weight", "", "needs a finite numeric weight")
            return None
        if check.review:
            notes.review(at, check.kind, check.review)
            approximate = True
        if check.check == SCORED_RUBRIC:
            source = "preserves the custom grading prompt and uses the SDK reply schema"
            if not any(name == "judge_prompt" for name, _ in check.options):
                source = "uses the SDK grading prompt and reply schema"
            notes.review(at, check.kind, f"{source}; compare model verdicts before gating")
        if check.kind in CALL_CHECKS:
            notes.review(at, check.kind, "measures the Martian target and traces, which may report different usage")
        seen[check.kind] = occurrence = seen.get(check.kind, 0) + 1
        options = json.dumps(dict(check.options), sort_keys=True, ensure_ascii=True)
        checks[(check.kind, occurrence, options)] = check
        label = _name(check.kind, occurrence)
        if metric := metric_name(at, assertion):
            metrics[label] = metric
        if weight != 1:
            weights[label] = weight
            weight_paths[label] = at
        return label

    for at, assertion in assertions:
        if not isinstance(assertion, dict) or assertion.get("type") != "assert-set":
            omitted |= add(at, assertion) is None
            continue
        items = assertion.get("assert")
        set_threshold = assertion.get("threshold")
        set_limit = _finite(set_threshold)
        set_weight = _finite(assertion.get("weight", 1))
        if (
            not isinstance(items, list)
            or (set_threshold is not None and set_limit is None)
            or set_weight is None
            or set(assertion) - {"type", "assert", "threshold", "weight", "metric"}
        ):
            notes.add(at, "assert-set", "needs an assertion list, finite weights and threshold, and optional metric")
            return None
        members = []
        for index, item in enumerate(items):
            if label := add(f"{at}.assert[{index}]", item):
                members.append(label)
            else:
                omitted = True
        sets.append(
            AssertionSet(
                name=_name("assert-set", len(sets) + 1),
                checks=members,
                threshold=set_limit,
                weight=set_weight,
                metric=metric_name(at, assertion),
            )
        )

    if invalid_weight:
        return None
    if omitted or approximate:
        if limit is not None or sets or any(weight <= 0 for weight in weights.values()):
            notes.add(where, "", "a required score could not be preserved; the weighted test is skipped")
            return None
        for at in weight_paths.values():
            notes.add(f"{at}.weight", "", "weighted scoring needs every assertion; built-in verdicts were retained")
        if metrics:
            notes.add(where, "", "named metrics need every contributor; built-in verdicts were retained")
        return checks, None
    gate = None
    if limit is not None or weights or sets or metrics:
        gate = CaseGate(threshold=limit, weights=weights, assertion_sets=sets, metrics=metrics)
    return checks, gate


def recorded_checks(
    assertions: Any, threshold: Any, variables: dict[str, Any]
) -> tuple[list[ScorerVersion], CaseGate | None, list[dict[str, Any]]] | None:
    """Normalize the deterministic rules from an exported test case without executing them."""
    if not isinstance(assertions, list) or not assertions:
        return None
    leaves: list[dict[str, Any]] = []
    for assertion in assertions:
        items = (
            assertion.get("assert")
            if isinstance(assertion, dict) and assertion.get("type") == "assert-set"
            else [assertion]
        )
        if not isinstance(items, list):
            return None
        for item in items:
            kind = item.get("type") if isinstance(item, dict) else None
            if not isinstance(kind, str) or ASSERTION_CHECKS.get(kind) not in COMPARABLE_CHECKS:
                return None
            value = item.get("value")
            if isinstance(value, str) and value.startswith(_FILE):
                # An archive does not establish the contents a file had during the original evaluation.
                return None
            leaves.append(item)
    notes = _Notes()
    result = _test_checks(
        [(f"assert[{index}]", item) for index, item in enumerate(assertions)],
        threshold,
        variables,
        notes,
        "testCase",
        None,
        stored=True,
        http=False,
        base=Path("."),
        rubric_prompt=None,
    )
    if result is None or notes.compatibility or any(note.blocks_strict for note in notes.found):
        return None
    checks, gate = result
    scorers = []
    for (_, occurrence, _), check in checks.items():
        parameters = _parameters(check)
        if check.row is not None:
            parameters["values"] = list(check.row)
        scorers.append(
            ScorerVersion(name=_name(check.kind, occurrence), kind="check", type=check.check, parameters=parameters)
        )
    return scorers, gate, leaves


def _test(
    index: int,
    test: Any,
    notes: _Notes,
    *,
    where: str,
    prompts: list[tuple[int, str]],
    count: int,
    default_vars: dict[str, Any],
    default_asserts: list[tuple[str, Any]],
    default: dict[str, Any],
    base: Path,
    target_kind: TargetKind,
    native: NativeProvider | None,
    echo: bool,
    called: str,
    judge_model: str | None,
    group_metrics: bool,
) -> _Test | None:
    if not isinstance(test, dict):
        notes.add(where, "", "is not a test; it is not converted")
        return None
    own_vars, own_asserts = _test_parts(test, where, notes)
    variables: dict[str, Any] = {}
    for name, value in {**default_vars, **own_vars}.items():
        if isinstance(value, list):
            notes.add(
                f"{where}.vars.{name}", "", "is a list, which promptfoo runs once per value; the test is not converted"
            )
            return None
        if isinstance(value, str) and value.startswith(_FILE):
            notes.add(
                f"{where}.vars.{name}",
                "",
                "is read from a file, which this converter does not read; the test is not converted",
            )
            return None
        if isinstance(value, str):
            if _NUNJUCKS.search(value):
                notes.review(f"{where}.vars.{name}", "", "contains a template within a variable value")
            # Promptfoo removes one final newline from a string var before rendering.
            value = value.removesuffix("\n")
        variables[name] = value
    reply = test.get("providerOutput")
    has_reply = reply is not None and (not isinstance(reply, (bool, str, int, float)) or bool(reply))
    stored = target_kind == "stored" or (native is not None and has_reply)
    if native is not None and native.fields and not stored and "env" in variables:
        notes.add(f"{where}.vars.env", "", "overrides the HTTP provider's environment template context")
        return None
    if stored and not echo and not has_reply:
        notes.add(where, "", "has no providerOutput to grade; give --target FILE.py[:FUNCTION] to call a function")
        return None
    if not stored and has_reply:
        notes.add(f"{where}.providerOutput", "", f"is not used: the suite {called}")
    test_options = test.get("options")
    use_defaults = not isinstance(test_options, dict) or test_options.get("disableDefaultAsserts") is not True
    default_options = default.get("options")
    rubric_prompt = default_options.get("rubricPrompt") if isinstance(default_options, dict) else None
    if isinstance(test_options, dict):
        rubric_prompt = test_options.get("rubricPrompt", rubric_prompt)
    threshold = test.get("threshold")
    compiled = _test_checks(
        [*(default_asserts if use_defaults else []), *own_asserts],
        threshold if threshold is not None else default.get("threshold"),
        variables,
        notes,
        where,
        judge_model,
        stored=stored,
        http=target_kind == "http",
        base=base,
        rubric_prompt=rubric_prompt,
    )
    if compiled is None:
        return None
    checks, case_gate = compiled
    if not checks:
        notes.add(where, "", "no assertion converts, so the test is in no suite")
        return None
    try:
        inputs_vars = json.loads(json.dumps(variables, allow_nan=False))
    except (ValueError, TypeError, RecursionError):
        notes.add(f"{where}.vars", "", "contain values the SDK cannot preserve as JSON; the test is skipped")
        return None
    if stored and has_reply and not isinstance(reply, str):
        try:
            reply = json.loads(json.dumps(reply, allow_nan=False))
        except (ValueError, TypeError, RecursionError):
            notes.add(f"{where}.providerOutput", "", "is not representable as JSON; the test is skipped")
            return None
        if any(
            check.check not in {"is_json", "json_schema"} or check.kind in EMBEDDED_ASSERTIONS
            for check in checks.values()
        ):
            notes.review(
                f"{where}.providerOutput", "", "may stringify differently from JavaScript; use an explicit text reply"
            )
    cases = []
    for position, text in prompts:
        rendered = _render_prompt(text, variables, structured=native is not None)
        if rendered is None:
            notes.add(where, "", f"fills prompts[{position}] with a list or an object var whole; not converted")
            continue
        try:
            prepared = native.inputs(rendered, variables, _render) if native is not None and not stored else {}
        except PromptfooConfigError as error:
            notes.add(where, "", f"prompts[{position}]: {error}")
            continue
        cases.append(
            Case(
                id=case_id(index, position, prompts=count),
                inputs={"prompt": rendered, "vars": inputs_vars, **prepared},
                outputs=(reply if has_reply else rendered) if stored else None,
                tags={GROUP_TAG: prompt_group(position)} if group_metrics else {},
            )
        )
    description = test.get("description")
    return (
        _Test(index, description if isinstance(description, str) else "", checks, cases, stored, case_gate)
        if cases
        else None
    )


# --- writing the suites ----------------------------------------------------------------------------


def _name(kind: str, occurrence: int) -> str:
    name = re.sub(r"[^A-Za-z0-9]+", "_", kind).strip("_")
    return name if occurrence == 1 else f"{name}_{occurrence}"


def _parameters(check: _Check) -> dict[str, Any]:
    return {option: list(value) if isinstance(value, tuple) else value for option, value in check.options}


def _suite_parts(members: list[_Test], judge_model: str | None) -> tuple[list[ScorerVersion], list[Case]]:
    first = members[0]
    expectations: dict[int, dict[str, Any]] = {member.index: {} for member in members}
    scorers: list[ScorerVersion] = []
    for key, check in first.checks.items():
        name = _name(check.kind, key[1])
        rows = [member.checks[key].row for member in members]
        shared = all(row == rows[0] for row in rows)
        parameters = _parameters(check)
        if check.check == SCORED_RUBRIC:
            if shared:
                parameters["rubric"] = rows[0]
            else:
                expectation = parameters["expectation"] = f"{name}_rubric"
                for member, row in zip(members, rows, strict=True):
                    expectations[member.index][expectation] = row
            scorers.append(
                ScorerVersion(
                    name=name,
                    kind="llm_judge",
                    type=SCORED_RUBRIC,
                    model=judge_model,
                    criteria=parameters,
                )
            )
            continue
        if check.row is not None:
            if shared:
                parameters["values"] = list(rows[0] or ())
            else:
                parameters["expectation"] = f"{name}_values"
                for member, row in zip(members, rows, strict=True):
                    expectations[member.index][f"{name}_values"] = list(row or ())
        scorers.append(ScorerVersion(name=name, kind="check", type=check.check, parameters=parameters))
    return scorers, [
        case.model_copy(update={"expectations": expectations[member.index]})
        for member in members
        for case in member.cases
    ]


def _combine_checks(
    groups: list[list[_Test]], judge_model: str | None
) -> tuple[list[ScorerVersion], list[Case], list[CaseChecks]]:
    """Keep every case's scoring rules when the provider's tests need different checks."""
    declared: dict[str, ScorerVersion] = {}
    definitions: dict[str, str] = {}
    cases: list[Case] = []
    assignments = []
    order = {
        case.id: (member.index, position)
        for members in groups
        for member in members
        for position, case in enumerate(member.cases)
    }
    for number, members in enumerate(groups, 1):
        local, rows = _suite_parts(members, judge_model)
        renamed = {}
        for scorer in local:
            signature = digest(scorer.model_dump(mode="json"))
            if signature not in definitions:
                name = scorer.name
                suffix = number
                while name in declared:
                    name = f"{scorer.name}_set_{suffix}"
                    suffix += 1
                declared[name] = scorer.model_copy(update={"name": name})
                definitions[signature] = name
            renamed[scorer.name] = definitions[signature]
        gate = members[0].case_gate
        assignments.append(
            CaseChecks(
                cases=tuple(row.id for row in rows),
                checks=tuple(renamed.values()),
                case_gate=gate.rename_checks(renamed) if gate is not None else None,
                aliases={name: original for original, name in renamed.items() if name != original},
            )
        )
        cases.extend(rows)
    return list(declared.values()), sorted(cases, key=lambda case: order[case.id]), assignments


def _suite(
    config: Path,
    groups: list[list[_Test]],
    *,
    number: int,
    prefix: str,
    target: Target,
    judge_model: str | None,
    unconverted: list[Unconverted],
    compatibility: list[Unconverted],
    description: str,
    source_provider: str | None,
    derived_metrics: DerivedMetrics | None,
) -> ConvertedSuite:
    suffix = f"{prefix}-{number}" if number > 1 else prefix
    suite_file, data_file = f"evals{suffix}.yaml", f"cases{suffix}.jsonl"
    members = sorted((member for group in groups for member in group), key=lambda member: member.index)
    assignment: list[CaseChecks] = []
    gate = None
    if len(groups) == 1:
        scorers, cases = _suite_parts(members, judge_model)
        gate = members[0].case_gate
    else:
        scorers, cases, assignment = _combine_checks(groups, judge_model)
    draft = Suite(
        id=Path(suite_file).stem,
        name=Path(suite_file).stem,
        dataset_location=data_file,
        input_key="prompt",
        target=Target(kind="stored") if all(member.stored for member in members) else target,
        scorers=scorers,
        case_gate=gate,
        check_sets=assignment,
        derived_metrics=derived_metrics,
        # Suites written to one folder keep their results apart.
        settings=RunSettings(output=f"results{suffix}") if suffix else RunSettings(),
        digest="0" * 64,
    )
    try:
        text = suite_to_yaml(draft.model_copy(update={"digest": suite_digest(draft)}))
        data = cases_to_jsonl(cases)
    except ConfigError as error:
        raise PromptfooConfigError(f"the converted suite cannot be written: {error}") from None
    tests = tuple(member.index for member in members)
    header = [
        f"# Converted from {config.name} by `martian evals convert promptfoo`"
        + (f": {_one_line(description)}" if description else ""),
        f"# {len(cases)} cases in {data_file}: each test on its selected prompts.",
        *(
            f"#   test {member.index + 1}" + (f": {_one_line(member.description)}" if member.description else "")
            for member in members
        ),
        f"# Run it with: martian evals run {suite_file}",
    ]
    if source_provider is not None:
        header.insert(1, f"# Selected Promptfoo provider: {_one_line(source_provider)}")
    if unconverted:
        header += ["#", "# Not converted:", *(f"# - {note.line()}" for note in unconverted)]
    if compatibility:
        header += ["#", "# Compare verdicts before using this suite as a gate:"]
        header.extend(f"# - {note.line()}" for note in compatibility)
    return ConvertedSuite(
        suite_file=suite_file,
        data_file=data_file,
        suite="\n".join(header) + "\n" + text,
        data=data,
        tests=tests,
        cases=len(cases),
        checks=tuple(scorer.name for scorer in scorers),
        source_provider=source_provider,
    )


def write_conversion(conversion: Conversion, folder: Path, *, strict: bool = False) -> list[Path]:
    """Write the conversion's suite and data files into ``folder``, which is created if needed.
    A file of the same name already there stops it before anything is written."""
    if strict:
        conversion.require_strict()
    files: dict[str, str] = {}
    for suite in conversion.suites:
        files[suite.suite_file] = suite.suite
        files[suite.data_file] = suite.data
    folder = folder.expanduser()
    if there := sorted(name for name in files if os.path.lexists(folder / name)):
        raise PromptfooConfigError(f"{folder} already has {', '.join(there)}; give another --output folder")
    written: list[Path] = []
    try:
        folder.mkdir(parents=True, exist_ok=True)
        for name, content in files.items():
            path = folder / name
            with path.open("x", encoding="utf-8") as handle:
                written.append(path)
                handle.write(content)
    except (OSError, UnicodeError) as error:
        for path in reversed(written):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                raise PromptfooConfigError(f"could not complete or roll back the conversion in {folder}") from None
        raise PromptfooConfigError(f"could not write the conversion to {folder}: {error}") from None
    return written


def report_lines(conversion: Conversion, folder: Path, *, preview: bool = False) -> list[str]:
    """What the command prints: each suite written and everything left out."""
    lines = []
    for suite in conversion.suites:
        tests = ", ".join(str(index + 1) for index in suite.tests)
        source = f"provider {suite.source_provider}, " if suite.source_provider else ""
        lines.append(
            f"{'Would write ' if preview else ''}{folder / suite.suite_file}: "
            f"{source}promptfoo tests {tests}, {suite.cases} cases in {suite.data_file}, "
            f"checks {', '.join(suite.checks)}"
        )
    if conversion.unconverted:
        lines.append("Not converted:" if preview else "Not converted (also listed at the top of each suite file):")
        lines += [f"- {note.line()}" for note in conversion.unconverted]
    if conversion.compatibility:
        lines.append("Compare verdicts for these changed checks before using the suites as gates:")
        lines += [f"- {note.line()}" for note in conversion.compatibility]
    return lines


__all__ = [
    "ASSERTION_CHECKS",
    "CALL_CHECKS",
    "UNSUPPORTED",
    "Conversion",
    "ConvertedSuite",
    "PromptfooConfigError",
    "Unconverted",
    "convert_promptfoo_config",
    "report_lines",
    "write_conversion",
]
