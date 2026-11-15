"""Audit a suite: grade replies that did no work, or that game the checks, instead of the target's.

A suite can hold cases that pass with no work or by a shortcut, and a pass rate does not show it.
An audit runs the suite's checks against built-in adversarial targets instead of its real target
(:data:`TARGETS`): do-nothing targets (an empty reply, a refusal, an echo of the input) and
cheating targets that try known shortcuts (repeat the question, return every option, claim a pass,
give the check's own values or the form it asks for, guess a common answer, send JSON with nothing
in it). No target is called and no model either, unless the caller runs the LLM judges.

Every case a target passes is a finding, with the target, its reply and the checks that let it
through. A check that *asks* for something (an answer, a value, a format, a tool call) and lets a
target through is a weak check, and a suite with a weak check is flagged. Checks that only forbid
something or bound it (a leaked secret, a forbidden call, a maximum length) pass any reply that
holds nothing forbidden, by design: a case guarded only by them is reported, not flagged. Two
findings are not flags either: a constant guess that passes no more than half of the cases it was
graded on is a chance pass, and a decline on a case the suite marks for declining (not benign, by
its ``no_refusal`` check's expectation) is the answer the case wants.

Checks that cannot be audited this way are listed with the reason: an LLM judge unless its model
is called (:func:`audit_suite` with ``run_judges``), checks of the target's call (latency, cost,
tool calls read from its trace), installed checks, which only a worker that installs them runs, and
the evals core's judges and worker scorers (:func:`audit_saved_suite`).
"""

from __future__ import annotations

import importlib
import json
import re
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from martian._version import __version__

if TYPE_CHECKING:
    from martian.evals import runner
    from martian.evals.model import Case, Suite

AUDIT_VERSION = "martian-suite-audit-v1"
"""Names the targets and the rules an audit result was made with."""

DO_NOTHING = "do_nothing"
CHEATING = "cheating"
TARGETS: dict[str, tuple[str, str]] = {
    "empty": (DO_NOTHING, "An empty reply"),
    "refusal": (DO_NOTHING, "A refusal: I'm sorry, but I can't help with that."),
    "echo": (DO_NOTHING, "The case's input sent back as it came"),
    "repeat_question": (CHEATING, "Every text in the case's inputs, the question and any context or options"),
    "every_option": (CHEATING, "Every option the case offers, A) to H) or an options list, in one reply"),
    "claim_pass": (CHEATING, "A claim of success: PASS, Correct"),
    "check_keywords": (
        CHEATING,
        "The checks' own values: the words they look for, the number or rows they expect, the tool calls they name",
    ),
    "answer_format": (CHEATING, "The form a check asks for with nothing in it: a sample of its pattern, filler"),
    "common_guesses": (CHEATING, "One common answer to every case: yes, no, true, false, 0, 1, A to D, none"),
    "hollow_json": (CHEATING, "JSON with nothing in it that keys, a schema or totals accept: {}, [], blanks, zeros"),
}
"""Each built-in adversarial target: its kind and what it replies."""

WHY = {
    "empty": "an empty reply passes it",
    "refusal": "a refusal passes it",
    "echo": "the case's input, sent back unchanged, passes it",
    "repeat_question": "the case's inputs, repeated, pass it",
    "every_option": "a list of every option passes it",
    "claim_pass": "a bare claim of success passes it",
    "check_keywords": "the checks' own values, given back, pass it",
    "answer_format": "the form it asks for, with nothing in it, passes it",
    "common_guesses": "one constant guess passes more than half of the cases it was graded on",
    "hollow_json": "JSON with nothing in it passes it",
}
"""Why a check is weak, by the target it let through."""

DECLINES = ("empty", "refusal")
REFUSAL = "I'm sorry, but I can't help with that."
CLAIMS = ("PASS", "Correct", "PASS. The answer is correct and the task is complete.")
GUESSES = ("yes", "no", "true", "false", "0", "1", "A", "B", "C", "D", "none")
GUESS_SHARE = 0.5
"""A constant guess flags a check when it passes more than this share of the cases it was graded on."""
FILLER = "lorem ipsum dolor sit amet "
PREVIEW = 200
SHOWN = 20
"""The most cases a weak check names; the findings name every one."""

Verdict = tuple[bool | None, str]


@dataclass(frozen=True)
class AuditCase:
    """One case as the audit grades it: ``question`` is what the echo sends back."""

    id: str
    inputs: Mapping[str, Any]
    expectations: Mapping[str, Any]
    question: str


@dataclass(frozen=True)
class AuditCheck:
    """One check of the suite as the audit runs it.

    ``decide`` grades a reply for a case; ``None`` means the check is not audited, for the reason in
    ``not_audited``. ``asks`` is whether it asks for something the reply must hold, rather than
    only forbidding or bounding something. ``judge`` marks a check that calls a model: it is asked
    only about replies every other check passed. ``checks_case`` is false for a case the check
    does not grade (``no_refusal`` on a case that is not benign), ``declines_case`` true for a case
    the suite marks for declining. ``keywords``, ``formats`` and ``skeletons`` are what its
    definition gives a cheat: replies of its own values, text in its form, and JSON values."""

    label: str
    type: str
    decide: Callable[[AuditCase, str], Verdict] | None = None
    not_audited: str = ""
    asks: bool = True
    judge: bool = False
    checks_case: Callable[[AuditCase], bool] | None = None
    declines_case: Callable[[AuditCase], bool] | None = None
    keywords: tuple[str, ...] = ()
    formats: tuple[str, ...] = ()
    skeletons: tuple[Any, ...] = ()


def _preview(text: str) -> str:
    return text if len(text) <= PREVIEW else text[: PREVIEW - 3] + "..."


def _texts(value: Any, depth: int = 0) -> list[str]:
    """Every non-blank text in a case's inputs, in order."""
    if depth > 6:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, Mapping):
        return [text for item in value.values() for text in _texts(item, depth + 1)]
    if isinstance(value, (list, tuple)):
        return [text for item in value for text in _texts(item, depth + 1)]
    return []


_OPTION_LINE = re.compile(r"^\s*\(?([A-Ha-h]|[1-9])[).:]\s+(\S.*?)\s*$", re.MULTILINE)
_OPTION_INLINE = re.compile(r"(?:^|\s)\(?([A-H])\)\s+(.+?)(?=\s+\(?[A-H]\)\s|\s*$)", re.DOTALL)
OPTION_KEYS = ("options", "choices", "answers", "candidates")


def _options(case: AuditCase) -> list[str]:
    """The options a case offers: an options list among its inputs, or lettered options in its question."""
    for key in OPTION_KEYS:
        given = case.inputs.get(key)
        if isinstance(given, Mapping):
            return [f"{letter}) {text}" for letter, text in given.items()]
        if isinstance(given, (list, tuple)):
            return [f"{chr(65 + n)}) {_text(item)}" for n, item in enumerate(given) if n < 26]
    found = _OPTION_LINE.findall(case.question)
    if len(found) < 2:
        found = _OPTION_INLINE.findall(case.question)
    return [f"{letter}) {text}" for letter, text in found]


def _text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)


def _merge(left: Any, right: Any) -> Any:
    """Two JSON skeletons as one: objects merged key by key, else the first."""
    if isinstance(left, dict) and isinstance(right, dict):
        merged = dict(left)
        for key, value in right.items():
            merged[key] = _merge(merged[key], value) if key in merged else value
        return merged
    return left


def _constant_replies(checks: Sequence[AuditCheck]) -> list[tuple[str, str]]:
    """The replies that are the same for every case."""
    replies = [("empty", ""), ("refusal", REFUSAL)]
    replies += [("claim_pass", claim) for claim in CLAIMS]
    for target, field in (("check_keywords", "keywords"), ("answer_format", "formats")):
        given = [list(getattr(check, field)) for check in checks if getattr(check, field)]
        replies += [(target, text) for texts in given for text in texts]
        if len(given) > 1:  # one reply that meets every check at once
            replies.append((target, "\n".join(texts[0] for texts in given)))
    skeletons = [skeleton for check in checks for skeleton in check.skeletons]
    replies += [("hollow_json", "{}"), ("hollow_json", "[]")]
    replies += [("hollow_json", json.dumps(skeleton, ensure_ascii=False)) for skeleton in skeletons]
    objects = [skeleton for skeleton in skeletons if isinstance(skeleton, dict)]
    if len(objects) > 1:
        merged: dict[str, Any] = {}
        for skeleton in objects:
            merged = _merge(merged, skeleton)
        replies.append(("hollow_json", json.dumps(merged, ensure_ascii=False)))
    replies += [("common_guesses", guess) for guess in GUESSES]
    return replies


def _replies(case: AuditCase, constant: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Every adversarial reply for one case, each once per target, in the order of ``TARGETS``."""
    replies = [*constant, ("echo", case.question)]
    everything = "\n".join(dict.fromkeys(_texts(dict(case.inputs))))
    if everything and everything != case.question:
        replies.append(("repeat_question", everything))
    if len(options := _options(case)) >= 2:
        replies.append(("every_option", "\n".join(options)))
    order = list(TARGETS)
    unique = list(dict.fromkeys(replies))
    return sorted(unique, key=lambda reply: order.index(reply[0]))


def _graded(check: AuditCheck, case: AuditCase) -> bool:
    return check.checks_case is None or check.checks_case(case)


def _declined(checks: Sequence[AuditCheck], case: AuditCase) -> bool:
    return any(check.declines_case is not None and check.declines_case(case) for check in checks)


def _decide(check: AuditCheck, case: AuditCase, reply: str) -> Verdict:
    assert check.decide is not None
    try:
        return check.decide(case, reply)
    except Exception as error:  # noqa: BLE001 - a check that raises gives no verdict, as in a run
        return None, f"the check raised {type(error).__name__}: {error}"[:300]


def run_audit(
    cases: Sequence[AuditCase], checks: Sequence[AuditCheck], *, progress: Callable[[str], None] | None = None
) -> dict[str, Any]:
    """Grade every adversarial reply to every case with the audited checks, and report."""
    audited = [check for check in checks if check.decide is not None]
    plain = [check for check in audited if not check.judge]
    judges = [check for check in audited if check.judge]
    constant = _constant_replies(audited)
    passes: dict[str, dict[str, set[str]]] = {check.label: {} for check in audited}
    errors: dict[str, Counter[str]] = {check.label: Counter() for check in audited}
    won: dict[str, set[str]] = {target: set() for target in TARGETS}
    tried: Counter[str] = Counter()
    guesses: dict[str, list[int]] = {guess: [0, 0] for guess in GUESSES}  # passed, graded
    passing: list[tuple[AuditCase, str, str, dict[str, Verdict]]] = []
    judge_calls = 0
    for number, case in enumerate(cases, 1):
        for target, reply in _replies(case, constant):
            tried[target] += 1
            verdicts = {check.label: _decide(check, case, reply) for check in plain}
            # A judge is asked only when its answer can decide the case: every other check passed.
            if judges and all(passed is True for passed, _ in verdicts.values()):
                for judge in judges:
                    judge_calls += 1
                    verdicts[judge.label] = _decide(judge, case, reply)
            for check in audited:
                if check.label not in verdicts:
                    continue
                passed, reason = verdicts[check.label]
                if passed is None:
                    errors[check.label][reason] += 1
                elif passed and _graded(check, case):
                    passes[check.label].setdefault(target, set()).add(case.id)
            found = [passed for passed, _ in verdicts.values()]
            failed = False in found
            passed_case = bool(audited) and len(found) == len(audited) and all(found)
            if target == "common_guesses" and (failed or passed_case):
                guesses[reply][1] += 1
                guesses[reply][0] += passed_case
            if passed_case:
                won[target].add(case.id)
                passing.append((case, target, reply, verdicts))
        if progress and number % 100 == 0:
            progress(f"{number}/{len(cases)} cases audited")
    unaudited = [check.label for check in checks if check.decide is None]
    findings = [{**finding, "not_audited": unaudited} for finding in _findings(passing, audited, guesses)]
    weak = _weak_checks(findings, audited)
    return {
        "audit": AUDIT_VERSION,
        "flagged": bool(weak),
        "audited": bool(audited),
        "cases": len(cases),
        "replies": sum(tried.values()),
        "judge_calls": judge_calls,
        "targets": [
            {
                "name": name,
                "kind": kind,
                "description": description,
                "replies": tried[name],
                "cases_passed": len(won[name]),
            }
            for name, (kind, description) in TARGETS.items()
        ],
        "checks": [
            {
                "label": check.label,
                "type": check.type,
                "audited": check.decide is not None,
                "asks": check.asks,
                "not_audited": check.not_audited or None,
                "passed": {target: len(ids) for target, ids in passes.get(check.label, {}).items()},
                "errors": dict(errors[check.label].most_common(3)) if check.label in errors else {},
            }
            for check in checks
        ],
        "weak_checks": weak,
        "not_audited": [
            {"label": check.label, "type": check.type, "reason": check.not_audited}
            for check in checks
            if check.decide is None
        ],
        "findings": findings,
    }


def _findings(
    passing: list[tuple[AuditCase, str, str, dict[str, Verdict]]],
    audited: Sequence[AuditCheck],
    guesses: dict[str, list[int]],
) -> list[dict[str, Any]]:
    """One finding per case and target: the first reply of the target that passed the case, and
    whether it flags the suite. Of several passing replies, one that flags is kept over one that
    does not, so a lopsided guess is not hidden behind a chance one."""
    by_label = {check.label: check for check in audited}
    found: dict[tuple[str, str], dict[str, Any]] = {}
    for case, target, reply, verdicts in passing:
        asking = [label for label in verdicts if by_label[label].asks and _graded(by_label[label], case)]
        share = 0.0
        if target == "common_guesses":
            passed, graded = guesses[reply]
            share = passed / graded if graded else 0.0
        if target in DECLINES and _declined(audited, case):
            flags, note = False, "the suite marks this case for declining (not benign), and the reply declines"
        elif not asking:
            flags, note = False, "every check of the case only forbids or bounds something, so doing nothing passes it"
        elif target == "common_guesses" and share <= GUESS_SHARE:
            passed, graded = guesses[reply]
            flags, note = False, f"the guess passes {passed} of {graded} cases, no more than half: a chance pass"
        else:
            flags, note = True, WHY[target]
        key = (case.id, target)
        if key in found and (found[key]["flags"] or not flags):
            continue
        found[key] = {
            "case": case.id,
            "target": target,
            "kind": TARGETS[target][0],
            "reply": _preview(reply),
            "checks": [{"label": label, "reason": reason} for label, (_, reason) in verdicts.items()],
            "weak_checks": asking if flags else [],
            "flags": flags,
            "note": note,
        }
    order = list(TARGETS)
    return sorted(found.values(), key=lambda finding: (order.index(finding["target"]),))


def _weak_checks(findings: list[dict[str, Any]], audited: Sequence[AuditCheck]) -> list[dict[str, Any]]:
    weak: dict[str, dict[str, Any]] = {}
    for finding in findings:
        for label in finding["weak_checks"]:
            entry = weak.setdefault(label, {"targets": [], "cases": [], "why": []})
            if finding["target"] not in entry["targets"]:
                entry["targets"].append(finding["target"])
                entry["why"].append(WHY[finding["target"]])
            if finding["case"] not in entry["cases"]:
                entry["cases"].append(finding["case"])
    return [
        {
            "label": check.label,
            "type": check.type,
            "targets": weak[check.label]["targets"],
            "cases": len(weak[check.label]["cases"]),
            "examples": weak[check.label]["cases"][:SHOWN],
            "why": weak[check.label]["why"],
        }
        for check in audited
        if check.label in weak
    ]


# --- what a check's definition gives away ---------------------------------------------------------


def pattern_sample(pattern: str) -> str | None:
    """A short text the regular expression finds, with nothing else in it: the form a ``matches``
    check asks for. ``None`` for a pattern this cannot write a sample of (a back reference, a
    look-around it cannot meet)."""
    try:
        parser: Any = importlib.import_module("re._parser")
        sample = _emit(parser.parse(pattern), 0)
        return sample if re.search(pattern, sample) else None
    except Exception:  # noqa: BLE001 - any pattern this cannot sample has no sample
        return None


def _emit(items: Any, depth: int) -> str:
    if depth > 30:
        raise ValueError("too deep")
    out = []
    for op, value in items:
        name = str(op)
        if name == "LITERAL":
            out.append(chr(value))
        elif name == "NOT_LITERAL":
            out.append("a" if value != ord("a") else "b")
        elif name == "ANY":
            out.append("a")
        elif name == "IN":
            out.append(_member(value))
        elif name in ("MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT"):
            low, _high, inner = value
            out.append(_emit(inner, depth + 1) * low)
        elif name == "SUBPATTERN":
            out.append(_emit(value[-1], depth + 1))
        elif name == "ATOMIC_GROUP":
            out.append(_emit(value, depth + 1))
        elif name == "BRANCH":
            out.append(_emit(value[1][0], depth + 1))
        elif name in ("AT", "ASSERT", "ASSERT_NOT"):
            continue
        else:
            raise ValueError(f"cannot sample {name}")
    return "".join(out)


_CATEGORIES: dict[str, Callable[[str], bool]] = {
    "CATEGORY_DIGIT": str.isdigit,
    "CATEGORY_NOT_DIGIT": lambda c: not c.isdigit(),
    "CATEGORY_WORD": lambda c: c.isalnum() or c == "_",
    "CATEGORY_NOT_WORD": lambda c: not (c.isalnum() or c == "_"),
    "CATEGORY_SPACE": str.isspace,
    "CATEGORY_NOT_SPACE": lambda c: not c.isspace(),
}


def _member(items: Any) -> str:
    """A character of a ``[...]`` class: its first member, or for ``[^...]`` one it leaves out."""

    def holds(char: str) -> bool:
        for op, value in items:
            name = str(op)
            if (name == "LITERAL" and char == chr(value)) or (name == "RANGE" and value[0] <= ord(char) <= value[1]):
                return True
            if name == "CATEGORY" and _CATEGORIES[str(value)](char):
                return True
        return False

    negated = bool(items) and str(items[0][0]) == "NEGATE"
    if negated:
        items = items[1:]
    for char in "a0 xZ_-.":
        if holds(char) != negated:
            return char
    raise ValueError("no member")


def hollow_instance(schema: Any, root: Any = None, depth: int = 0) -> Any:
    """The emptiest JSON value a schema accepts: required keys only, blank text, zeros, the fewest
    items. Not checked here: the check itself grades it."""
    root = schema if root is None else root
    if depth > 20 or not isinstance(schema, Mapping):
        return {}
    if isinstance(reference := schema.get("$ref"), str) and reference.startswith("#/"):
        target: Any = root
        for step in reference[2:].split("/"):
            target = target.get(step, {}) if isinstance(target, Mapping) else {}
        return hollow_instance(target, root, depth + 1)
    if "const" in schema:
        return schema["const"]
    if isinstance(schema.get("enum"), list) and schema["enum"]:
        return schema["enum"][0]
    for key in ("anyOf", "oneOf"):
        if isinstance(schema.get(key), list) and schema[key]:
            return hollow_instance(schema[key][0], root, depth + 1)
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), "null")
    if kind is None:
        kind = (
            "object"
            if "properties" in schema or "required" in schema
            else "array"
            if "items" in schema
            else "string"
            if "pattern" in schema or "minLength" in schema
            else None
        )
    value: Any = {}
    if kind == "object":
        properties = schema.get("properties") or {}
        value = {key: hollow_instance(properties.get(key, {}), root, depth + 1) for key in schema.get("required") or []}
    elif kind == "array":
        value = [hollow_instance(schema.get("items") or {}, root, depth + 1) for _ in range(schema.get("minItems", 0))]
    elif kind == "string":
        sample = pattern_sample(schema["pattern"]) if isinstance(schema.get("pattern"), str) else ""
        value = (sample or "").ljust(int(schema.get("minLength", 0)), "x")
    elif kind in ("number", "integer"):
        value = _least(schema, kind)
    elif kind == "boolean":
        value = False
    elif kind == "null":
        value = None
    for part in schema.get("allOf") or []:
        value = _merge(value, hollow_instance(part, root, depth + 1))
    return value


def _least(schema: Mapping[str, Any], kind: str) -> int | float:
    low, high = schema.get("minimum"), schema.get("maximum")
    if isinstance(above := schema.get("exclusiveMinimum"), (int, float)):
        low = above + 1 if kind == "integer" else above + 0.5
    if isinstance(below := schema.get("exclusiveMaximum"), (int, float)):
        high = below - 1 if kind == "integer" else below - 0.5
    if isinstance(low, (int, float)) and low > 0:
        return low
    if isinstance(high, (int, float)) and high < 0:
        return high
    return 0


def zeros_at(paths: Sequence[str]) -> Any:
    """A JSON object with 0 at every dotted path, and an empty list where a path steps into every
    item (``*``): totals and parts with nothing in them, which add up."""
    built: dict[str, Any] = {}
    for path in paths:
        steps = [step for step in path.split(".") if step]
        if not steps or steps[0] == "*" or steps[0].isdigit():
            continue
        node: Any = built
        for position, step in enumerate(steps):
            if not isinstance(node, dict):
                break
            following = steps[position + 1] if position + 1 < len(steps) else None
            if following is None:
                node.setdefault(step, 0)
            elif following == "*" or following.isdigit():
                node.setdefault(step, [])
                break
            else:
                node = node.setdefault(step, {})
    return built


def value_at(path: str | None, value: Any) -> Any:
    """``value`` placed at a dotted path, or itself without one."""
    for step in reversed([step for step in (path or "").split(".") if step]):
        value = [None] * int(step) + [value] if step.isdigit() else {step: value}
    return value


def claimed_calls(steps: Sequence[Sequence[Any]]) -> str:
    """Chat messages that claim the agent made these calls, one model turn per step: what a check
    that reads the calls in the reply cannot tell from calls made."""
    messages: list[dict[str, Any]] = []
    for number, step in enumerate(steps, 1):
        calls = []
        for position, expected in enumerate(step):
            arguments = expected.arguments if expected.match in ("subset", "exact") and expected.arguments else {}
            calls.append(
                {
                    "id": f"call_{number}_{position}",
                    "type": "function",
                    "function": {"name": expected.name, "arguments": json.dumps(arguments)},
                }
            )
        messages.append({"role": "assistant", "content": None, "tool_calls": calls})
    messages.append({"role": "assistant", "content": "Done."})
    return json.dumps(messages)


# --- a suite file ---------------------------------------------------------------------------------

ASKS_NOTHING = "it only forbids or bounds something"
NOT_A_CALL = "it measures the target's call, which the audit's replies do not make"
TRACED = "it reads the tool calls the trace of the target's call records, which a reply cannot claim"
JUDGE_COST = "an LLM judge: auditing it calls its model once per case and reply; run the audit with --run-judges to"
JUDGE_COST += " do so at your provider's cost"
INSTALLED = "an installed check: only a worker that installs a check under its name runs it"


def _question(inputs: Mapping[str, Any], input_key: str) -> str:
    from martian.evals.server.projection import prompt_text

    given = inputs.get(input_key)
    return given if isinstance(given, str) else prompt_text(dict(inputs))


def _feedback(feedback: Any) -> Verdict:
    """A check's MLflow feedback as a verdict, read as a suite run reads it."""
    from martian.evals.gate import _interpret

    if isinstance(feedback, list):
        feedback = feedback[0] if feedback else None
    error = getattr(feedback, "error", None)
    if error is not None:
        message = getattr(error, "error_message", None) or str(error)
        return None, f"no verdict: {message}"[:300]
    passed = _interpret(getattr(feedback, "value", feedback), None)
    return passed, str(getattr(feedback, "rationale", "") or "")


def _file_check(
    spec: runner.CheckSpec, suite: runner.Suite, scorer: Any, expectations: Callable[[AuditCase], dict[str, Any]]
) -> AuditCheck:
    """An audit check of one check of a suite file, built with the scorer a run would use."""
    from martian.evals import _policy
    from martian.evals.runner import CALL_CHECKS, INSTALLED_CHECKS, JUDGES, NUMERIC_CHECKS, TEXT_CHECKS, TOOL_CHECKS

    label, kind = spec.label, spec.type
    if spec.type in CALL_CHECKS:
        return AuditCheck(label, kind, not_audited=NOT_A_CALL, asks=False)
    if spec.type in INSTALLED_CHECKS:
        return AuditCheck(label, kind, not_audited=INSTALLED)
    tools = spec.type in TOOL_CHECKS
    if tools and (spec.source or ("trace" if suite.target is not None else "reply")) == "trace":
        return AuditCheck(label, kind, not_audited=TRACED)
    if spec.type in JUDGES and scorer is None:
        return AuditCheck(label, kind, not_audited=JUDGE_COST)

    def decide(case: AuditCase, reply: str) -> Verdict:
        return _feedback(scorer.run(inputs=dict(case.inputs), outputs=reply, expectations=expectations(case)))

    asks = not spec.security_check
    if spec.type == "length":
        asks = bool(spec.min_length)
    elif spec.type == "step_count":
        asks = bool(spec.min_turns or spec.min_calls)
    checks_case: Callable[[AuditCase], bool] | None = None
    declines_case: Callable[[AuditCase], bool] | None = None
    if spec.type == "no_refusal" and spec.expectation is not None:
        key = spec.expectation

        def benign(case: AuditCase) -> bool | None:
            """The case's benign flag; None when it has none, which the check itself refuses."""
            try:
                return _policy.benign_flag(case.expectations.get(key), key)
            except ValueError:
                return None

        checks_case = lambda case: benign(case) is not False  # noqa: E731 - it grades only benign cases
        declines_case = lambda case: benign(case) is False  # noqa: E731 - the others are to be declined

    keywords: tuple[str, ...] = ()
    formats: tuple[str, ...] = ()
    skeletons: tuple[Any, ...] = ()
    if spec.type in TEXT_CHECKS and asks and spec.values:
        together = spec.type in ("contains", "contains_all", "contains_any")
        keywords = (" ".join(spec.values),) if together else tuple(spec.values)
    elif spec.type == "number_equals" and spec.value is not None:
        keywords = (json.dumps(value_at(spec.path, spec.value)) if spec.path else str(spec.value),)
    elif spec.type == "table_equals" and spec.rows:
        keywords = (json.dumps(value_at(spec.path, spec.rows)),)
    elif tools and spec.type != "tool_not_called" and spec.calls:
        keywords = (claimed_calls(spec.tool_options().calls or ()),)
    if spec.type == "matches" and spec.pattern and (sample := pattern_sample(spec.pattern)) is not None:
        formats = (sample,)
    elif spec.type == "length" and spec.min_length:
        formats = ((FILLER * (spec.min_length // len(FILLER) + 1))[: spec.min_length],)
    if spec.type == "is_json":
        skeletons = ({key: "" for key in spec.required_keys},) if spec.required_keys else ()
    elif spec.type == "json_schema" and spec.json_schema is not None:
        skeletons = (hollow_instance(spec.json_schema),)
    elif spec.type in NUMERIC_CHECKS and spec.type != "table_equals":
        paths = [*spec.parts, *spec.add, *spec.subtract, *([spec.total] if spec.total else [])]
        if spec.type == "number_equals" and spec.path and spec.value is None:
            paths = [spec.path]
        skeletons = (zeros_at(paths),) if paths else ()
    return AuditCheck(
        label,
        kind,
        decide=decide,
        asks=asks,
        judge=spec.type in JUDGES,
        checks_case=checks_case,
        declines_case=declines_case,
        keywords=keywords,
        formats=formats,
        skeletons=skeletons,
    )


def audit_suite(
    path: str | Path,
    *,
    run_judges: bool = False,
    limit: int | None = None,
    output: str | Path | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Audit a suite file: grade the built-in adversarial targets' replies to its cases with its
    checks, in place of its target or its stored outputs, and write ``audit.json`` in the output
    folder (``output``, else the suite's ``output``). Neither the target nor a model is called;
    with ``run_judges`` the LLM judges are, on the replies every other check passed. A conversation
    case, scripted or simulated, is not audited: an audit grades one reply per case, and
    ``skipped`` counts them. Returns the audit, with the folder it was written to under
    ``output``."""
    from martian.evals import runner
    from martian.evals.suite_file import load_suite_file

    model, model_cases = load_suite_file(path)
    suite, base = runner.load_suite(path)
    if limit is not None:
        model_cases = model_cases[:limit]
    # An audit grades one reply per case; a conversation's turns are not audited.
    talks = sum(1 for case in model_cases if case.turns or case.simulated_user is not None)
    model_cases = [case for case in model_cases if not (case.turns or case.simulated_user is not None)]
    if run_judges:
        runs = suite.model_copy(update={"checks": [c for c in suite.checks if c.type not in runner.INSTALLED_CHECKS]})
        built = dict(zip([spec.label for spec in runs.checks], runner._build_scorers(runs, base), strict=True))
    else:
        modules: dict[Path, Any] = {}
        built = {
            spec.label: runner._python_check(spec, base, modules)
            if spec.type in runner.CODE_CHECKS
            else runner._build_check(spec)
            for spec in suite.checks
            if spec.type not in runner.JUDGES + runner.INSTALLED_CHECKS
        }

    def expectations(case: AuditCase) -> dict[str, Any]:
        return runner._with_suite_criteria({"expectations": dict(case.expectations)}, suite)

    checks = [_file_check(spec, suite, built.get(spec.label), expectations) for spec in suite.checks]
    cases = [
        AuditCase(
            id=case.id,
            inputs=case.inputs,
            expectations=case.expectations,
            question=_question(case.inputs, suite.input_key),
        )
        for case in model_cases
    ]
    result = {
        "suite": {"name": model.name, "digest": model.digest},
        "dataset": {"name": model.dataset_name, "digest": model.dataset_digest},
        "sdk_version": __version__,
        "judges_run": run_judges,
        **run_audit(cases, checks, progress=progress),
        "skipped": talks,
    }
    folder = Path(output) if output is not None else base / suite.output
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "audit.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return {**result, "output": str(folder)}


# --- a saved suite in the evals core ------------------------------------------------------------

CORE_JUDGE = "an LLM judge: the evals core makes no model call in a request, so the audit does not ask it"
WORKER_SCORER = "a custom scorer: it runs in the worker, not in the request"
NO_THRESHOLD = "it gives a number, and the suite's policy sets no threshold for one case to pass by"


def _builtin_check(name: str, scorer: Any, suite: Suite) -> AuditCheck:
    from martian.evals.server.projection import expected_text
    from martian.evals.worker.hooks import is_installed
    from martian.evals.worker.scorers import CODE_SCORERS, code_score, is_builtin

    label, kind = name, scorer.type
    if scorer.kind == "llm_judge":
        return AuditCheck(label, kind, not_audited=CORE_JUDGE)
    if is_installed(scorer):
        return AuditCheck(label, kind, not_audited=INSTALLED)
    if not is_builtin(scorer) or name not in CODE_SCORERS:
        return AuditCheck(label, kind, not_audited=WORKER_SCORER)
    policy = next((p for p in suite.policies if p.scorer == name), None)
    if name == "response_length":
        if policy is None or policy.threshold is None:
            return AuditCheck(label, kind, not_audited=NO_THRESHOLD)
        threshold, higher = policy.threshold, policy.direction == "higher"

        def length(case: AuditCase, reply: str) -> Verdict:
            value, _ = code_score(name, reply, expected_text(dict(case.expectations)))
            assert isinstance(value, int)
            passed = value >= threshold if higher else value <= threshold
            bound = "at least" if higher else "at most"
            return passed, f"{value} characters; the policy asks for {bound} {threshold:g}"

        return AuditCheck(label, kind, decide=length, asks=higher)

    def decide(case: AuditCase, reply: str) -> Verdict:
        value, rationale = code_score(name, reply, expected_text(dict(case.expectations)))
        return value is True, rationale

    return AuditCheck(label, kind, decide=decide)


def audit_saved_suite(suite: Suite, cases: Sequence[Case]) -> dict[str, Any]:
    """Audit a saved suite version of the evals core over a dataset's cases: its built-in code
    scorers grade the adversarial replies, a case being read as the worker reads it (its prompt and
    expected text; a case without both is skipped, as a run skips it). Its judges and custom
    scorers are not audited."""
    from martian.evals.server.projection import expected_text, prompt_text

    usable = [case for case in cases if prompt_text(case.inputs) and expected_text(case.expectations)]
    checks = [_builtin_check(scorer.name, scorer, suite) for scorer in suite.scorers]
    audited = [
        AuditCase(id=case.id, inputs=case.inputs, expectations=case.expectations, question=prompt_text(case.inputs))
        for case in usable
    ]
    return {**run_audit(audited, checks), "skipped": len(cases) - len(usable)}


__all__ = [
    "AUDIT_VERSION",
    "TARGETS",
    "AuditCase",
    "AuditCheck",
    "audit_saved_suite",
    "audit_suite",
    "hollow_instance",
    "pattern_sample",
    "run_audit",
]
