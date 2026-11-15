"""A promptfoo config as suite files that ``martian evals run`` runs.

``martian evals convert promptfoo promptfooconfig.yaml --output suite/`` reads a promptfoo config
(``prompts``, ``providers``, ``tests`` with ``vars`` and ``assert``, and ``defaultTest``) without
promptfoo, and writes suite files and their data files where the features map:

- Each test, once per prompt, is a case: the prompt rendered with the test's vars (``{{ name }}``
  and ``{{ name.key }}``) is the input ``prompt``, and the vars are the input ``vars``. Case ids
  are the ones ``martian evals import promptfoo`` gives the same test's results.
- Each assertion that a check on the scorer library decides the same way is a check:
  ``ASSERTION_CHECKS`` maps them. ``defaultTest`` assertions come first, as promptfoo runs them.
- A suite file applies every check to every case, and promptfoo asserts per test, so tests are
  grouped by the checks they need: one suite file per group, ``evals.yaml`` and ``cases.jsonl``
  when there is one, ``evals-2.yaml`` and ``cases-2.jsonl`` for a second, and so on. A check
  whose value differs between the tests of a group reads it from each case's expectations.
- The target is ``--target FILE.py[:FUNCTION]``, called as ``FUNCTION(prompt=..., vars=...)``;
  without it, each test's ``providerOutput`` is the stored reply the checks grade. promptfoo's
  providers are not called. ``llm-rubric`` becomes the rubric judge on ``--judge-model``.

Every assertion, provider and option that does not convert is in the conversion's ``unconverted``
list, printed by the command and written as comments at the top of each suite file, never dropped
silently. ``docs/evals-core.md`` has the mapping table.
"""

from __future__ import annotations

import csv
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from martian.config import ConfigError
from martian.evals.model import Case, RunSettings, ScorerVersion, Suite, Target, suite_digest
from martian.evals.promptfoo_import import case_id
from martian.evals.suite_file import cases_to_jsonl, suite_to_yaml

LATER = "MorningHQ/martian-notes#171"
"""The scorer library epic. Its checks would convert more assertions once it lands on main."""


class PromptfooConfigError(Exception):
    """A file this converter cannot read as a promptfoo config, or a conversion it cannot write."""


@dataclass(frozen=True)
class Unconverted:
    """Something in the config the suite files leave out: where (a path into the config, such as
    ``tests[2].assert[1]``), what (an assertion type, a provider, an option), and why. ``later``
    names the check of the scorer library epic that would convert it once that lands."""

    where: str
    what: str
    why: str
    later: str = ""

    def line(self) -> str:
        text = f"{self.where} {self.what}: {self.why}" if self.what else f"{self.where}: {self.why}"
        return f"{text} (once {LATER} lands: {self.later})" if self.later else text


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


@dataclass(frozen=True)
class Conversion:
    """A config converted: the suite files to write and everything they leave out."""

    config: Path
    suites: list[ConvertedSuite]
    unconverted: list[Unconverted]


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

ASSERTION_CHECKS = {
    **{kind: text.check for kind, text in TEXT_CHECKS.items()},
    **{kind: check for kind, (check, _) in PATTERN_CHECKS.items()},
    "is-json": "is_json",
    "llm-rubric": "rubric",
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
    "levenshtein",
    "rouge-n",
    "rouge-l",
    "rouge-s",
    "bleu",
    "gleu",
    "meteor",
    "perplexity",
    "perplexity-score",
)
_CALL_DATA = (
    "is-valid-function-call",
    "is-valid-openai-function-call",
    "is-valid-openai-tools-call",
    "tool-call-f1",
    "finish-reason",
    "guardrails",
    "skill-used",
    "trace-span-count",
    "trace-span-duration",
    "trace-error-spans",
    "trajectory:tool-used",
    "trajectory:tool-args-match",
    "trajectory:tool-sequence",
    "trajectory:step-count",
)

UNSUPPORTED: dict[str, str] = {
    **dict.fromkeys(
        ("javascript", "python", "ruby", "webhook"), "runs custom code, and a suite file's checks run none"
    ),
    **dict.fromkeys(
        _MODEL_GRADED,
        "is a promptfoo model-graded metric with its own grading prompt; the suite's judges are rubric, "
        "guidelines and conditions",
    ),
    **dict.fromkeys(("classifier", "moderation", "pi"), "calls a classifier or moderation model of its own"),
    **dict.fromkeys(_CLOSENESS, "scores closeness to a reference text, or perplexity; no check on main does"),
    "contains-json": "finds JSON inside other text; is_json needs the whole reply to be JSON",
    **dict.fromkeys(
        ("contains-html", "is-html", "contains-sql", "is-sql", "contains-xml", "is-xml"),
        "checks a format no check on main reads",
    ),
    **dict.fromkeys(
        _CALL_DATA, "reads the provider's tool calls, trace or metadata, which a suite file's checks do not see"
    ),
    "latency": "limits the provider call's time; no check on main does",
    "cost": "limits the provider call's cost; no check on main does",
    "is-refusal": "passes only a refusal; no check on main detects refusals",
    "word-count": "counts words; no check on main does",
    "assert-set": "groups assertions under a threshold and weights of its own; a suite's case passes when every "
    "check passes",
    **dict.fromkeys(
        ("select-best", "max-score"), "compares every prompt's and provider's output for a test; a suite has one target"
    ),
    "human": "is a person's grade in promptfoo's viewer",
}
"""promptfoo's documented assertion types that do not convert, and why."""

LATER_CHECKS = {
    "latency": "latency, from MorningHQ/martian-notes#172",
    "cost": "cost, from MorningHQ/martian-notes#172",
    "not-is-refusal": "no_refusal, from MorningHQ/martian-notes#178",
    "promptfoo:redteam:pii": "no_personal_data, from MorningHQ/martian-notes#178",
    "promptfoo:redteam:indirect-prompt-injection": "resists_injection, from MorningHQ/martian-notes#178",
}
"""The scorer library epic's checks that would convert these assertion types (``promptfoo:redteam:pii``
includes its ``:direct``, ``:session`` and other variants)."""

# Assertion options that do not change the verdict: listed, and the assertion converts.
_KEPT_OPTIONS = {
    "weight": "weighs the assertion in promptfoo's score; a suite's case passes when every check passes",
    "metric": "names a promptfoo metric; the check is named after the assertion's type",
    "provider": "names a grading model; the suite's judge is --judge-model",
}
# Assertion options that change the verdict: listed, and the assertion does not convert.
_BLOCKING_OPTIONS = {
    "transform": "transforms the reply before the check; the suite's checks see the reply as the target returns it",
    "contextTransform": "builds context from the reply; the suite's checks have none",
    "threshold": "sets a score threshold; the converted check gives a pass or a fail",
    "rubricPrompt": "replaces the grading prompt; the rubric judge uses its own",
    "normalizeUnicode": "normalizes Unicode before comparing; the suite's checks compare the text as it is",
    "config": "passes configuration to custom code",
}
_TEST_FIELDS = {
    "threshold": "passes the test on a weighted score; a suite's case passes when every check passes",
    "metadata": "a suite's data file has no place for case metadata",
    "provider": "runs the test on a provider of its own; the suite has one target",
    "providers": "limits the test to some providers; the suite has one target",
    "prompts": "limits the test to some prompts; the converted test runs on every prompt",
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
    "rubricPrompt": "replaces the grading prompt; the rubric judge uses its own",
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
    "derivedMetrics": "computes metrics from named scores; the run shows each check's own counts",
    "extensions": "runs extension hooks",
    "redteam": "configures promptfoo's red team generator",
}

_VAR = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s*\}\}")
_NUNJUCKS = re.compile(r"\{\{|\{%|\{#")
_TARGET = re.compile(r"^(?P<file>.+?\.py)(?::(?P<function>[A-Za-z_][A-Za-z0-9_]*))?$")
_FILE = "file://"


@dataclass(frozen=True)
class _Check:
    """One assertion as a check: the promptfoo type, the check, the options every case shares,
    and the value of this test's case (text checks' values, or the rubric)."""

    kind: str
    check: str
    options: tuple[tuple[str, Any], ...]
    row: tuple[str, ...] | str | None = None


@dataclass(frozen=True)
class _Note:
    where: str
    what: str
    why: str
    later: str = ""


def _later(kind: str) -> str:
    if kind.startswith("promptfoo:redteam:pii"):
        return LATER_CHECKS["promptfoo:redteam:pii"]
    return LATER_CHECKS.get(kind, "")


def _refusal(kind: str) -> str | None:
    """Why an assertion type does not convert, or None when it does."""
    if kind in ASSERTION_CHECKS:
        return None
    if kind in UNSUPPORTED:
        return UNSUPPORTED[kind]
    if kind.startswith("promptfoo:redteam:"):
        return "is a promptfoo red team grader, which a model grades with its own prompt"
    base = kind.removeprefix("not-")
    if base != kind and (base in UNSUPPORTED or base in ASSERTION_CHECKS):
        if base == "is-refusal":
            return "passes when the reply is not a refusal; no check on main detects refusals"
        return f"passes when {base} fails; no check on main does that"
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


def _values(value: Any, variables: dict[str, Any], *, many: bool) -> tuple[str, ...] | str:
    """A text check's values, or why it has none."""
    if value is None:
        return "has no value"
    found: list[str | None]
    if isinstance(value, str):
        rendered = _render(value, variables)
        if rendered is None:
            return "uses Nunjucks beyond {{ name }} and {{ name.key }}"
        # promptfoo reads the text value of a -any or -all type as comma-separated values.
        found = [part.strip() for part in next(csv.reader([rendered], skipinitialspace=True))] if many else [rendered]
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


def _assertion(assertion: Any, variables: dict[str, Any], judge_model: str | None) -> tuple[_Check | None, list[_Note]]:
    """One assertion of one test as a check, and the notes on what of it does not convert."""
    if not isinstance(assertion, dict) or not isinstance(assertion.get("type"), str):
        return None, [_Note("", "", "is not an assertion with a type")]
    kind: str = assertion["type"]
    if (refused := _refusal(kind)) is not None:
        return None, [_Note("", kind, refused, _later(kind))]
    notes = [_Note(f".{key}", "", reason) for key, reason in _KEPT_OPTIONS.items() if key in assertion]
    blocking = [
        _Note(
            f".{key}",
            "",
            f"{_BLOCKING_OPTIONS.get(key, 'is not an assertion option this converter knows')}; "
            "the assertion is not converted",
        )
        for key in assertion
        if key not in {"type", "value", *_KEPT_OPTIONS} and not (key == "normalizeUnicode" and assertion[key] is False)
    ]
    if blocking:
        return None, notes + blocking
    value = assertion.get("value")
    if isinstance(value, str) and value.startswith(_FILE):
        return None, [*notes, _Note("", kind, "reads its value from a file, which this converter does not read")]
    found = _check(kind, value, variables, judge_model)
    if isinstance(found, str):
        return None, [*notes, _Note("", kind, found, _later(kind))]
    return found, notes


def _check(kind: str, value: Any, variables: dict[str, Any], judge_model: str | None) -> _Check | str:
    if kind in TEXT_CHECKS:
        text = TEXT_CHECKS[kind]
        values = _values(value, variables, many=text.many)
        if isinstance(values, str):
            return values
        options: list[tuple[str, Any]] = [("any_of", True)] if text.any_of else []
        if text.case_sensitive:
            options.append(("case_sensitive", True))
        return _Check(kind, text.check, tuple(options), values)
    if kind in PATTERN_CHECKS:
        check, shape = PATTERN_CHECKS[kind]
        exact = kind in ("equals", "not-equals")
        if exact and isinstance(value, dict | list):
            return "compares JSON values; no check on main does"
        if not exact and not isinstance(value, str):
            return "needs a text value"
        given = _text(value)
        if given is None:
            return "has no value"
        rendered = _render(given, variables)
        if rendered is None:
            return "uses Nunjucks beyond {{ name }} and {{ name.key }}"
        if not rendered:
            return "has an empty value"
        pattern = shape.format(rendered if kind.endswith("regex") else _escape(rendered))
        try:
            re.compile(pattern)
        except re.error as error:
            return f"is not a regular expression Python reads ({error}); promptfoo reads it as JavaScript's"
        return _Check(kind, check, (("pattern", pattern),))
    if kind == "is-json":
        if value is None:
            return _Check(kind, "is_json", ())
        required = value.get("required", []) if isinstance(value, dict) else None
        if (
            isinstance(value, dict)
            and set(value) <= {"type", "required"}
            and value.get("type", "object") == "object"
            and isinstance(required, list)
            and all(isinstance(key, str) and key for key in required)
        ):
            return _Check(kind, "is_json", (("required_keys", tuple(required)),) if required else ())
        return "validates a JSON schema; is_json checks only that the reply is JSON with the required top-level keys"
    # llm-rubric
    if judge_model is None:
        return "needs --judge-model, the model the suite's rubric judge calls"
    if not isinstance(value, str) or not value.strip():
        return "needs its rubric as text"
    rubric = _render(value, variables)
    if rubric is None:
        return "uses Nunjucks beyond {{ name }} and {{ name.key }}"
    return _Check(kind, "rubric", (), rubric)


# --- reading the config ----------------------------------------------------------------------------


@dataclass
class _Notes:
    found: list[Unconverted] = field(default_factory=list)

    def add(self, where: str, what: str, why: str, later: str = "") -> None:
        note = Unconverted(_one_line(where), _one_line(what)[:80], _one_line(why), later)
        if note not in self.found:
            self.found.append(note)


def _one_line(text: str) -> str:
    return " ".join(str(text).split())


def _items(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _prompts(value: Any, base: Path, notes: _Notes) -> tuple[int, list[tuple[int, str]]]:
    """How many prompts promptfoo would run, and the ones that convert, by promptfoo's index."""
    if isinstance(value, dict):
        notes.add("prompts", "", "maps prompt files to labels, which this converter does not read")
        return 0, []
    found: list[tuple[int, str]] = []
    count = 0
    for position, item in enumerate(_items(value)):
        where = f"prompts[{position}]"
        texts: list[str] = []
        if isinstance(item, str):
            texts = _prompt_texts(item, base, where, notes)
        elif isinstance(item, dict):
            for key in sorted(set(item) - {"id", "label", "raw"}):
                notes.add(f"{where}.{key}", "", "is not applied to the converted prompt")
            raw, reference = item.get("raw"), item.get("id")
            if isinstance(raw, str):
                texts = [raw]
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
                found.append((count, text))
            count += 1
    if not count:
        notes.add("prompts", "", "the config has no prompt; no test converts")
    return count, found


def _prompt_texts(item: str, base: Path, where: str, notes: _Notes) -> list[str]:
    """An inline prompt, or the prompts of a .txt or .md prompt file."""
    if not item.startswith(_FILE):
        return [item]
    name = item[len(_FILE) :]
    file = Path(name) if Path(name).is_absolute() else base / name
    if any(mark in name for mark in "*?[") or file.suffix not in (".txt", ".md"):
        notes.add(where, name, "is a prompt file this converter does not read (it reads .txt and .md files)")
        return []
    try:
        text = file.read_text(encoding="utf-8")
    except OSError as error:
        notes.add(where, name, f"cannot be read: {error.strerror or error}")
        return []
    if file.suffix == ".md":
        return [text]
    # promptfoo splits a .txt prompt file on lines of ---.
    return [part.strip("\n") for part in re.split(r"^---$", text, flags=re.MULTILINE) if part.strip()]


def _provider_name(provider: Any) -> str:
    if isinstance(provider, str):
        return provider
    if isinstance(provider, dict):
        label, identifier = provider.get("label"), provider.get("id")
        if isinstance(identifier, str):
            return f"{label} ({identifier})" if isinstance(label, str) and label != identifier else identifier
        if len(provider) == 1:
            return str(next(iter(provider)))
    return "provider"


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


@dataclass
class _Test:
    index: int
    description: str
    checks: dict[tuple[Any, ...], _Check]
    cases: list[Case]


def convert_promptfoo_config(
    path: Path, *, output: Path, target: str | None = None, judge_model: str | None = None
) -> Conversion:
    """The promptfoo config at ``path`` as suite files for the folder ``output``, which a
    function target's path is written relative to. Raises :class:`PromptfooConfigError` for a
    file that is not a promptfoo config or a ``target`` that is not a Python file."""
    path = path.expanduser().resolve()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise PromptfooConfigError(f"cannot read {path}: {error.strerror or error}") from None
    except yaml.YAMLError:
        raise PromptfooConfigError(f"{path.name} is not a promptfoo config: it is not YAML") from None
    if not isinstance(raw, dict) or "prompts" not in raw:
        raise PromptfooConfigError(f"{path.name} is not a promptfoo config: it has no prompts")
    suite_target, called = _target(target, output)
    notes = _Notes()
    for key in raw:
        if key not in {"prompts", "providers", "tests", "defaultTest"} and _CONFIG_FIELDS.get(key) != "":
            notes.add(str(key), "", _CONFIG_FIELDS.get(str(key), "a suite file has no place for it"))
    count, prompts = _prompts(raw.get("prompts"), path.parent, notes)
    for position, provider in enumerate(_items(raw.get("providers"))):
        notes.add(f"providers[{position}]", _provider_name(provider), f"not called: the suite {called} instead")
    default = raw.get("defaultTest") or {}
    if not isinstance(default, dict):
        notes.add("defaultTest", "", "is a file this converter does not read")
        default = {}
    default_vars, default_asserts = _test_parts(default, "defaultTest", notes)
    tests: list[_Test] = []
    found_tests = raw.get("tests")
    if isinstance(found_tests, str) or (found_tests is not None and not isinstance(found_tests, list)):
        notes.add("tests", "", "are in a file this converter does not read; no test converts")
        found_tests = []
    for index, test in enumerate(found_tests or []):
        converted = _test(
            index,
            test,
            notes,
            prompts=prompts,
            count=count,
            default_vars=default_vars,
            default_asserts=default_asserts,
            stored=suite_target.kind == "stored",
            called=called,
            judge_model=judge_model,
        )
        if converted is not None:
            tests.append(converted)
    groups: dict[frozenset[tuple[Any, ...]], list[_Test]] = {}
    for test in tests:
        groups.setdefault(frozenset(test.checks), []).append(test)
    unconverted = list(notes.found)
    description = raw.get("description")
    suites = [
        _suite(
            path,
            members,
            number=number,
            several=len(groups) > 1,
            target=suite_target,
            judge_model=judge_model,
            unconverted=unconverted,
            description=description if isinstance(description, str) else "",
        )
        for number, members in enumerate(groups.values(), 1)
    ]
    return Conversion(config=path, suites=suites, unconverted=unconverted)


def _test_parts(test: dict[str, Any], where: str, notes: _Notes) -> tuple[dict[str, Any], list[tuple[str, Any]]]:
    """A test's (or defaultTest's) vars and assertions, noting every other field."""
    for key, value in test.items():
        if key in ("vars", "assert", "providerOutput"):
            continue
        if key == "options" and isinstance(value, dict):
            for option in value:
                notes.add(f"{where}.options.{option}", "", _TEST_OPTIONS.get(str(option), "is not applied"))
        elif _TEST_FIELDS.get(str(key)) != "":
            notes.add(f"{where}.{key}", "", _TEST_FIELDS.get(str(key), "a suite file has no place for it"))
    variables = test.get("vars") or {}
    if not isinstance(variables, dict):
        notes.add(f"{where}.vars", "", "are in a file this converter does not read; they are not applied")
        variables = {}
    asserts = test.get("assert") or []
    if not isinstance(asserts, list):
        notes.add(f"{where}.assert", "", "is not a list of assertions; none of them converts")
        asserts = []
    return variables, [(f"{where}.assert[{position}]", assertion) for position, assertion in enumerate(asserts)]


def _test(
    index: int,
    test: Any,
    notes: _Notes,
    *,
    prompts: list[tuple[int, str]],
    count: int,
    default_vars: dict[str, Any],
    default_asserts: list[tuple[str, Any]],
    stored: bool,
    called: str,
    judge_model: str | None,
) -> _Test | None:
    where = f"tests[{index}]"
    if not isinstance(test, dict):
        notes.add(where, "", "is not a test; it is not converted")
        return None
    own_vars, own_asserts = _test_parts(test, where, notes)
    variables = {**default_vars, **own_vars}
    for name, value in variables.items():
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
    if stored and test.get("providerOutput") is None:
        notes.add(where, "", "has no providerOutput to grade; give --target FILE.py[:FUNCTION] to call a function")
        return None
    if not stored and "providerOutput" in test:
        notes.add(f"{where}.providerOutput", "", f"is not used: the suite {called}")
    checks: dict[tuple[Any, ...], _Check] = {}
    seen: dict[str, int] = {}
    judged = False
    for at, assertion in [*default_asserts, *own_asserts]:
        check, found = _assertion(assertion, variables, judge_model)
        for note in found:
            notes.add(at + note.where, note.what, note.why, note.later)
        if check is None:
            continue
        if check.check == "rubric":
            if judged:
                notes.add(at, check.kind, "a suite has one rubric judge, and the test's first llm-rubric is it")
                continue
            judged = True
        # Tests share a check when they assert the same type the same number of times, with the
        # same options; a text check's values and a rubric may differ from test to test.
        seen[check.kind] = seen.get(check.kind, 0) + 1
        checks[(check.kind, seen[check.kind], check.options)] = check
    if not checks:
        notes.add(where, "", "no assertion converts, so the test is in no suite")
        return None
    inputs_vars = json.loads(json.dumps(variables, default=str))
    cases = []
    for position, text in prompts:
        rendered = _render(text, variables)
        if rendered is None:
            notes.add(where, "", f"fills prompts[{position}] with a list or an object var whole; not converted")
            continue
        cases.append(
            Case(
                id=case_id(index, position, prompts=count),
                inputs={"prompt": rendered, "vars": inputs_vars},
                outputs=test["providerOutput"] if stored else None,
            )
        )
    description = test.get("description")
    return (
        _Test(index=index, description=description if isinstance(description, str) else "", checks=checks, cases=cases)
        if cases
        else None
    )


# --- writing the suites ----------------------------------------------------------------------------


def _name(kind: str, occurrence: int) -> str:
    name = re.sub(r"[^A-Za-z0-9]+", "_", kind).strip("_")
    return name if occurrence == 1 else f"{name}_{occurrence}"


def _suite(
    config: Path,
    members: list[_Test],
    *,
    number: int,
    several: bool,
    target: Target,
    judge_model: str | None,
    unconverted: list[Unconverted],
    description: str,
) -> ConvertedSuite:
    suite_file, data_file = (
        ("evals-{}.yaml", "cases-{}.jsonl") if several and number > 1 else ("evals.yaml", "cases.jsonl")
    )
    suite_file, data_file = suite_file.format(number), data_file.format(number)
    first = members[0]
    expectations: dict[int, dict[str, Any]] = {member.index: {} for member in members}
    scorers: list[ScorerVersion] = []
    for key, check in first.checks.items():
        name = _name(check.kind, key[1])
        rows = [member.checks[key].row for member in members]
        shared = all(row == rows[0] for row in rows)
        parameters = {option: list(value) if isinstance(value, tuple) else value for option, value in check.options}
        if check.check == "rubric":
            if not shared:
                for member, row in zip(members, rows, strict=True):
                    expectations[member.index]["rubric"] = row
            scorers.append(
                ScorerVersion(
                    name=name,
                    kind="llm_judge",
                    type="rubric",
                    model=judge_model,
                    criteria={"rubric": rows[0]} if shared else {},
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
    cases = [
        case.model_copy(update={"expectations": expectations[member.index]})
        for member in members
        for case in member.cases
    ]
    draft = Suite(
        id=Path(suite_file).stem,
        name=Path(suite_file).stem,
        dataset_location=data_file,
        input_key="prompt",
        target=target,
        scorers=scorers,
        # Suites written to one folder keep their results apart.
        settings=RunSettings(output=f"results-{number}") if several and number > 1 else RunSettings(),
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
        f"# {len(cases)} cases in {data_file}: each of these promptfoo tests once per prompt.",
        *(
            f"#   test {member.index + 1}" + (f": {_one_line(member.description)}" if member.description else "")
            for member in members
        ),
        f"# Run it with: martian evals run {suite_file}",
    ]
    if unconverted:
        header += ["#", "# Not converted:", *(f"# - {note.line()}" for note in unconverted)]
    return ConvertedSuite(
        suite_file=suite_file,
        data_file=data_file,
        suite="\n".join(header) + "\n" + text,
        data=data,
        tests=tests,
        cases=len(cases),
        checks=tuple(scorer.name for scorer in scorers),
    )


def write_conversion(conversion: Conversion, folder: Path) -> list[Path]:
    """Write the conversion's suite and data files into ``folder``, which is created if needed.
    A file of the same name already there stops it before anything is written."""
    files: dict[str, str] = {}
    for suite in conversion.suites:
        files[suite.suite_file] = suite.suite
        files[suite.data_file] = suite.data
    if there := sorted(name for name in files if (folder / name).exists()):
        raise PromptfooConfigError(f"{folder} already has {', '.join(there)}; give another --output folder")
    folder.mkdir(parents=True, exist_ok=True)
    written = []
    for name, text in files.items():
        (folder / name).write_text(text, encoding="utf-8")
        written.append(folder / name)
    return written


def report_lines(conversion: Conversion, folder: Path) -> list[str]:
    """What the command prints: each suite written and everything left out."""
    lines = []
    for suite in conversion.suites:
        tests = ", ".join(str(index + 1) for index in suite.tests)
        lines.append(
            f"{folder / suite.suite_file}: promptfoo tests {tests}, {suite.cases} cases in {suite.data_file}, "
            f"checks {', '.join(suite.checks)}"
        )
    if conversion.unconverted:
        lines.append("Not converted (also listed at the top of each suite file):")
        lines += [f"- {note.line()}" for note in conversion.unconverted]
    return lines


__all__ = [
    "ASSERTION_CHECKS",
    "LATER_CHECKS",
    "UNSUPPORTED",
    "Conversion",
    "ConvertedSuite",
    "PromptfooConfigError",
    "Unconverted",
    "convert_promptfoo_config",
    "report_lines",
    "write_conversion",
]
