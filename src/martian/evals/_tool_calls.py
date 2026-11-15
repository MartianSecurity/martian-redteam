"""Tool calls and trajectories: which tools an agent called, with what arguments, in which turn.

An agent's trajectory is its model turns in order, each holding the tool calls that turn made.
Calls made in one turn are parallel (the model asked for them together); calls in successive
turns are sequential. The tool-call checks (:mod:`martian.evals.checks`) read a trajectory from
one of two places:

- The reply (``source="reply"``): the messages a target returns or a case stores, as a value or
  as JSON text. A list of messages, an object with ``messages``, a chat completion (its
  ``choices``) or one message. Each model message (``role`` ``assistant`` or ``model``, or
  LangChain's ``type`` ``ai``) is a turn; its calls are its ``tool_calls`` (OpenAI, LangChain),
  its ``content`` blocks of type ``tool_use`` (Anthropic), its ``parts`` of type ``tool_call``
  (OpenTelemetry GenAI) or with a ``functionCall`` (Gemini), and OpenAI's older
  ``function_call``.
- The trace (``source="trace"``): the spans MLflow records for the target's call. Each model
  span (``LLM`` or ``CHAT_MODEL``) is a turn, and each tool span (``TOOL``) is a call, as
  MLflow's own tool-call scorers read them. A span's kind and its tool's name and arguments are
  read through the trace mapping (:func:`martian.evals.trace_conventions.map_span`): the kind is
  MLflow's span type, else, for a span MLflow left untyped, ``gen_ai.operation.name`` or
  ``openinference.span.kind``; the tool is the one ``gen_ai.tool.name`` (or OpenInference's
  ``tool.name``) names, else the span's name, with the span's inputs as its arguments, else the
  arguments its attributes record (``gen_ai.tool.call.arguments``). A tool span is in the turn of
  the model span that started last before it. Spans inside a tool span are that tool's own work,
  not the agent's, and are not read.

What cannot be read is a scorer error, never an empty trajectory: a reply that is not messages or
holds an item that is not a chat message (such as a Responses API ``function_call`` item), a
tool call without a name, a trace with no model or tool span, and a trace whose model spans ask
for tools while no tool span records one.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from martian.evals import _json_schema
from martian.evals.model import Span
from martian.evals.trace_conventions import SpanFields, map_span

TOOL_CHECKS = ("tool_called", "tool_not_called", "tool_trajectory", "step_count")
SOURCES = ("reply", "trace")
ARGUMENT_MATCHES = ("exact", "subset", "schema", "pattern")
ORDERS = ("exact", "in_order", "any_order")

_MODEL_KINDS = frozenset({"LLM", "CHAT_MODEL"})
# Google ADK adds a tool span that merges the results of the calls one model turn made; it is not
# a call itself, and MLflow's own reader leaves it out too.
_MERGED_TOOLS = "(merged tools)"
# A chat message has one of these roles, or, as LangChain writes it, one of these types. A reply
# item with another role or type, such as a Responses API function_call item, is not read.
_ROLES = frozenset({"system", "developer", "user", "assistant", "model", "tool", "function"})
_TYPES = frozenset({"system", "human", "ai", "tool", "function", "chat"})
_SHORT = 60


@dataclass(frozen=True)
class Call:
    """One tool call the agent made: the tool's name and the arguments it was given."""

    name: str
    arguments: Any


@dataclass(frozen=True)
class Trajectory:
    """The agent's model turns in order, each the calls it made. ``before`` holds the calls a trace
    records before its first model span, which belong to no turn."""

    source: str
    turns: tuple[tuple[Call, ...], ...]
    before: tuple[Call, ...] = ()

    @property
    def calls(self) -> tuple[Call, ...]:
        return self.before + tuple(call for turn in self.turns for call in turn)

    def tool_turns(self, check: str) -> list[tuple[Call, ...]]:
        """The turns that made a tool call, for a check that compares turns."""
        if self.before:
            raise ValueError(
                f"{check} compares model turns, and the trace records {_count(len(self.before), 'tool call')} "
                "before any model call, which belong to no turn; trace the target's model calls too"
            )
        return [turn for turn in self.turns if turn]


# --- reading the reply ------------------------------------------------------------------------


def _decoded(arguments: Any) -> Any:
    """A call's arguments: JSON text decoded (OpenAI sends them as text), none as no arguments."""
    if arguments is None or arguments == "":
        return {}
    if isinstance(arguments, str):
        try:
            return json.loads(arguments)
        except ValueError:
            return arguments
    return arguments


def _listed(message: Mapping[str, Any], key: str) -> list[Any]:
    found = message.get(key)
    return found if isinstance(found, list) else []


def _is_message(item: Any) -> bool:
    if not isinstance(item, Mapping):
        return False
    if "role" in item:
        return item["role"] in _ROLES
    if "type" in item:
        return item["type"] in _TYPES
    return "tool_calls" in item


def _messages(value: Any) -> list[Mapping[str, Any]] | None:
    """The messages a reply holds, or ``None`` when it is not messages, or holds an item that is
    not a chat message."""
    items: list[Any]
    if isinstance(value, Mapping):
        if isinstance(value.get("choices"), list):  # a chat completion
            items = [choice.get("message") if isinstance(choice, Mapping) else choice for choice in value["choices"]]
        elif isinstance(value.get("messages"), list):
            items = value["messages"]
        else:
            items = [value]
    elif isinstance(value, list):
        items = value
    else:
        return None
    return items if items and all(_is_message(item) for item in items) else None


def _from_model(message: Mapping[str, Any]) -> bool:
    if message.get("role") in ("assistant", "model") or message.get("type") == "ai":
        return True
    return "role" not in message and "tool_calls" in message


def _call_items(message: Mapping[str, Any]) -> list[Any]:
    """A model message's tool calls: OpenAI's and LangChain's ``tool_calls`` (and OpenAI's older
    ``function_call``), Anthropic's ``tool_use`` and OpenTelemetry GenAI's ``tool_call`` blocks,
    and Gemini's ``functionCall`` parts."""
    found = list(_listed(message, "tool_calls"))
    if isinstance(message.get("function_call"), Mapping):
        found.append(message["function_call"])
    for block in _listed(message, "content") + _listed(message, "parts"):
        if not isinstance(block, Mapping):
            continue
        if block.get("type") in ("tool_use", "tool_call"):
            found.append(block)
        for key in ("functionCall", "function_call"):
            if isinstance(block.get(key), Mapping):
                found.append(block[key])
    return found


def _call(item: Any) -> Call:
    if not isinstance(item, Mapping):
        raise ValueError("a tool call in the reply is not an object")
    if isinstance(item.get("function"), Mapping):  # OpenAI's form
        item = {**item, **item["function"]}
    name = item.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("a tool call in the reply has no name")
    arguments = next((item[key] for key in ("arguments", "args", "input") if key in item), None)
    return Call(name=name, arguments=_decoded(arguments))


def from_reply(parsed: bool, value: Any) -> Trajectory:
    """The trajectory in a reply, read as JSON (``parsed`` and ``value``, as ``is_json`` reads it)."""
    if not parsed or not isinstance(value, Mapping | list):
        raise ValueError(
            "the reply is text, not messages, so it holds no tool calls to read; "
            "have the target return its messages, or read the trace of its call (source: trace)"
        )
    messages = _messages(value)
    if messages is None:
        raise ValueError(
            "the reply holds no messages to read tool calls from: give a list of chat messages (each with a role, "
            "or LangChain's type), an object with messages, a chat completion or one message"
        )
    turns = tuple(tuple(_call(item) for item in _call_items(m)) for m in messages if _from_model(m))
    return Trajectory(source="reply", turns=turns)


# --- reading the trace ------------------------------------------------------------------------


def _mapped(span: Any) -> SpanFields:
    """An MLflow span's trace fields through the trace mapping, read as the store reads its spans."""
    return map_span(
        Span(
            id=str(span.span_id),
            name=span.name or "",
            kind=str(span.span_type or ""),
            inputs=span.inputs,
            outputs=span.outputs,
            attributes=dict(span.attributes or {}),
        )
    )


def _asks_for_tools(outputs: Any) -> bool:
    """Whether a model span's output asks for a tool, in the forms a reply is read in."""
    messages = _messages(outputs) or []
    return any(_from_model(message) and _call_items(message) for message in messages)


def from_trace(trace: Any) -> Trajectory:
    """The trajectory the spans of the target's call record."""
    spans = list(trace.data.spans)
    by_id = {span.span_id: span for span in spans}
    mapped = {id(span): _mapped(span) for span in spans}

    def kind_of(span: Any) -> str:
        return mapped[id(span)].kind or ""

    def in_tool(span: Any) -> bool:
        seen, parent = set(), span.parent_id
        while parent in by_id and parent not in seen:
            seen.add(parent)
            if kind_of(by_id[parent]) == "TOOL":
                return True
            parent = by_id[parent].parent_id
        return False

    turns: list[list[Call]] = []
    before: list[Call] = []
    asked = traced_tools = False
    for _, span in sorted(enumerate(spans), key=lambda pair: (pair[1].start_time_ns or 0, pair[0])):
        kind = kind_of(span)
        if (kind not in _MODEL_KINDS and kind != "TOOL") or in_tool(span):
            continue
        if kind in _MODEL_KINDS:
            turns.append([])
            asked = asked or _asks_for_tools(span.outputs)
            continue
        traced_tools = True
        tool = mapped[id(span)].tool
        name, arguments = (tool.name, tool.arguments) if tool is not None else (str(span.name or ""), span.inputs)
        if name == _MERGED_TOOLS:
            continue
        (turns[-1] if turns else before).append(Call(name=name, arguments=_decoded(arguments)))
    if not turns and not traced_tools:
        raise ValueError(
            "the trace records no model or tool call, so it shows none of the agent's tool use; trace the "
            "target's model calls (for example with mlflow.openai.autolog()) and its tools (spans of type TOOL), "
            "or read the tool calls in its reply (source: reply)"
        )
    if asked and not traced_tools:
        raise ValueError(
            "the trace's model calls ask for tools, but it records no tool span, so the calls made cannot be "
            "read; trace each tool as a span of type TOOL, or read the tool calls in the reply (source: reply)"
        )
    return Trajectory(source="trace", turns=tuple(tuple(turn) for turn in turns), before=tuple(before))


# --- the calls a check expects ----------------------------------------------------------------


@dataclass(frozen=True)
class Expected:
    """A call a check looks for: a tool's name and, unless ``arguments`` is ``None``, what its
    arguments must match, as ``match`` says (``patterns`` compiled for ``pattern``)."""

    name: str
    arguments: Any = None
    match: str = "subset"
    patterns: dict[str, re.Pattern[str]] = field(default_factory=dict, compare=False)

    def __str__(self) -> str:
        if self.arguments is None:
            return self.name
        if self.match == "schema":
            return f"{self.name}(arguments matching the schema)"
        if isinstance(self.arguments, Mapping):
            sign = "~" if self.match == "pattern" else "="
            given = ", ".join(f"{key}{sign}{_short(value)}" for key, value in self.arguments.items())
            return f"{self.name}({given})"
        return f"{self.name}({_short(self.arguments)})"


# A step of a trajectory: the calls one model turn makes, one call unless they are parallel.
Step = tuple[Expected, ...]


def _short(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return text if len(text) <= _SHORT else text[: _SHORT - 3] + "..."


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}{'' if number == 1 else 's'}"


def _expected(item: Any, where: str, match: str) -> Expected:
    if isinstance(item, str):
        if not item.strip():
            raise ValueError(f"{where} is an empty tool name")
        return Expected(name=item)
    if not isinstance(item, Mapping):
        raise ValueError(f"{where} must be a tool name or an object with name and arguments")
    if unknown := sorted(set(item) - {"name", "arguments"}):
        raise ValueError(f"{where} has no field {', '.join(map(str, unknown))}; a call has name and arguments")
    name = item.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"{where} needs a tool name")
    arguments = copy.deepcopy(item.get("arguments"))
    if arguments is None:
        return Expected(name=name, match=match)
    patterns: dict[str, re.Pattern[str]] = {}
    if match == "subset" and not isinstance(arguments, Mapping):
        raise ValueError(f"{where}: with arguments_match subset, the arguments must be an object")
    if match == "schema":
        _json_schema.check_schema(arguments)
    if match == "pattern":
        if not isinstance(arguments, Mapping) or not all(isinstance(value, str) for value in arguments.values()):
            raise ValueError(f"{where}: with arguments_match pattern, the arguments must map names to patterns")
        for key, pattern in arguments.items():
            try:
                patterns[str(key)] = re.compile(pattern)
            except re.error as error:
                raise ValueError(f"{where}: arguments.{key} is not a valid regular expression: {error}") from None
    return Expected(name=name, arguments=arguments, match=match, patterns=patterns)


def steps(value: Any, where: str, *, match: str, groups: bool, check: str) -> tuple[Step, ...]:
    """The calls a check looks for, in order. A step is one call or, when ``groups``, a list of
    the calls one model turn makes together."""
    if not isinstance(value, list | tuple) or not value:
        raise ValueError(f"{where} must be a nonempty list of tool calls")
    found: list[Step] = []
    for position, item in enumerate(value, 1):
        here = f"{where} item {position}"
        if isinstance(item, list | tuple):
            if not groups:
                why = "any_order ignores turns" if check == "tool_trajectory" else f"{check} counts calls"
                raise ValueError(f"{here} is a group of parallel calls, which {why}; list the calls one by one")
            if not item:
                raise ValueError(f"{here} is an empty group of parallel calls")
            found.append(tuple(_expected(one, f"{here}.{at}", match) for at, one in enumerate(item, 1)))
        else:
            found.append((_expected(item, here, match),))
    return tuple(found)


# --- matching ---------------------------------------------------------------------------------


def _differs(expected: Any, actual: Any, at: str, *, exact: bool) -> str | None:
    """The first argument that differs, named by its path: in an object, ``exact`` refuses keys
    the expected one does not have, and without it only the expected keys are compared, nested
    objects too. Other values must be equal as JSON is, so true is not 1 but 1 is 1.0."""
    if isinstance(expected, Mapping) and isinstance(actual, Mapping):
        for key, want in expected.items():
            where = f"{at}.{key}" if at else str(key)
            if key not in actual:
                return f"{where} is missing"
            if found := _differs(want, actual[key], where, exact=exact):
                return found
        if exact and (extra := [str(key) for key in actual if key not in expected]):
            return f"{', '.join(f'{at}.{key}' if at else key for key in extra)} is not expected"
        return None
    if isinstance(expected, Mapping) and not at:
        return f"the arguments are {_short(actual)}, not an object"
    if _json_schema._same(actual, expected):
        return None
    return f"{at or 'the arguments'} is {_short(actual)}, expected {_short(expected)}"


def problem(expected: Expected, call: Call) -> str | None:
    """How the call's arguments fail to match the expected ones, or ``None`` when they match."""
    actual = call.arguments
    if expected.arguments is None:
        return None
    if expected.match in ("exact", "subset"):
        return _differs(expected.arguments, actual, "", exact=expected.match == "exact")
    if expected.match == "schema":
        found = _json_schema.problem(actual, expected.arguments)
        return None if found is None else f"the arguments do not match the schema: {found}"
    if not isinstance(actual, Mapping):
        return f"the arguments are {_short(actual)}, not an object"
    for key, pattern in expected.patterns.items():
        if key not in actual:
            return f"{key} is missing"
        text = actual[key] if isinstance(actual[key], str) else json.dumps(actual[key], ensure_ascii=False)
        if pattern.fullmatch(text) is None:
            return f"{key} is {_short(actual[key])}, which does not match {pattern.pattern}"
    return None


def fits(expected: Expected, call: Call) -> bool:
    return call.name == expected.name and problem(expected, call) is None


def _matching(expected: Sequence[Expected], calls: Sequence[Call]) -> list[int | None]:
    """The call each expected call is matched to, one to one and as many as can be (Kuhn's
    augmenting paths), so a loosely matching expected call cannot take another one's call."""
    fit = [[fits(want, call) for call in calls] for want in expected]
    owner: dict[int, int] = {}

    def assign(index: int, seen: set[int]) -> bool:
        for position, ok in enumerate(fit[index]):
            if ok and position not in seen:
                seen.add(position)
                if position not in owner or assign(owner[position], seen):
                    owner[position] = index
                    return True
        return False

    for index in range(len(expected)):
        assign(index, set())
    found: list[int | None] = [None] * len(expected)
    for position, index in owner.items():
        found[index] = position
    return found


def _made(step: Step, calls: Sequence[Call]) -> bool:
    """Whether the calls hold every call of the step, each a different one."""
    return None not in _matching(step, calls)


def _unmatched(calls: Sequence[Call], matched: list[int | None]) -> list[Call]:
    taken = {position for position in matched if position is not None}
    return [call for position, call in enumerate(calls) if position not in taken]


def _why_missing(expected: Expected, calls: Sequence[Call]) -> str:
    """Why none of the calls is the expected one: a call of its tool with other arguments, or none."""
    same = [call for call in calls if call.name == expected.name]
    if not same:
        return f"{expected.name} is not called"
    return f"{expected.name} is called with other arguments: {problem(expected, same[0])}"


def _describe(step: Step) -> str:
    return str(step[0]) if len(step) == 1 else " and ".join(map(str, step)) + " in one turn"


def _bounds(low: int, high: int | None) -> str:
    if high is None:
        return f"at least {low}"
    if low == high:
        return f"exactly {low}"
    return f"at most {high}" if low == 0 else f"{low} to {high}"


def _listing(numbers: list[str]) -> str:
    return numbers[0] if len(numbers) == 1 else ", ".join(numbers[:-1]) + " and " + numbers[-1]


# --- the checks' decisions --------------------------------------------------------------------


@dataclass(frozen=True)
class ToolOptions:
    """A tool-call check's options as the check reads them, so a bad one is refused when the
    check is created or the suite loads. ``calls`` are fixed; else each case gives them under
    ``expectation``."""

    check: str
    match: str
    order: str
    calls: tuple[Step, ...] | None = None
    expectation: str | None = None
    source: str | None = None
    min_calls: int | None = None
    max_calls: int | None = None
    min_turns: int | None = None
    max_turns: int | None = None

    def expected(self, expectations: Mapping[str, Any] | None) -> tuple[Step, ...]:
        if self.calls is not None:
            return self.calls
        given = (expectations or {}).get(self.expectation or "")
        return self.read(given, f"expectations.{self.expectation}")

    def read(self, value: Any, where: str) -> tuple[Step, ...]:
        groups = self.check == "tool_trajectory" and self.order != "any_order"
        return steps(value, where, match=self.match, groups=groups, check=self.check)

    def decide(self, trajectory: Trajectory, expectations: Mapping[str, Any] | None) -> tuple[bool, str]:
        """Whether the trajectory passes the check, and why."""
        if self.check == "step_count":
            return self._count(trajectory)
        wanted = self.expected(expectations)
        calls = [step[0] for step in wanted]
        if self.check == "tool_called":
            return self._called(trajectory, calls)
        if self.check == "tool_not_called":
            return self._not_called(trajectory, calls)
        if self.order == "any_order":
            return self._any_order(trajectory, calls)
        turns = trajectory.tool_turns(self.check)
        return self._exact(wanted, turns) if self.order == "exact" else self._in_order(wanted, turns)

    def _called(self, trajectory: Trajectory, wanted: list[Expected]) -> tuple[bool, str]:
        low = 1 if self.min_calls is None else self.min_calls
        counts, problems = [], []
        for want in wanted:
            made = sum(fits(want, call) for call in trajectory.calls)
            counts.append(f"{want} called {_count(made, 'time')}")
            if made < low or (self.max_calls is not None and made > self.max_calls):
                found = f"{want} called {_count(made, 'time')}, expected {_bounds(low, self.max_calls)}"
                problems.append(found + (f" ({_why_missing(want, trajectory.calls)})" if not made else ""))
        return (False, "; ".join(problems)) if problems else (True, "; ".join(counts))

    def _not_called(self, trajectory: Trajectory, wanted: list[Expected]) -> tuple[bool, str]:
        found = []
        for want in wanted:
            if made := sum(fits(want, call) for call in trajectory.calls):
                found.append(f"{want} called {_count(made, 'time')}")
        return (False, "; ".join(found)) if found else (True, f"not called: {', '.join(map(str, wanted))}")

    def _any_order(self, trajectory: Trajectory, wanted: list[Expected]) -> tuple[bool, str]:
        calls = trajectory.calls
        matched = _matching(wanted, calls)
        missing = [want for want, position in zip(wanted, matched, strict=True) if position is None]
        if missing:
            free = _unmatched(calls, matched)
            reasons = "; ".join(_why_missing(want, free) for want in missing)
            return False, f"{len(missing)} of {_count(len(wanted), 'expected call')} not made: {reasons}"
        return True, f"all {_count(len(wanted), 'expected call')} made, among {_count(len(calls), 'call')}"

    def _exact(self, wanted: tuple[Step, ...], turns: list[tuple[Call, ...]]) -> tuple[bool, str]:
        for number, (step, turn) in enumerate(zip(wanted, turns, strict=False), 1):
            matched = _matching(step, turn)
            if None not in matched and len(turn) == len(step):
                continue
            free = _unmatched(turn, matched)
            missing = [want for want, position in zip(step, matched, strict=True) if position is None]
            reasons = []
            for want in missing:
                other = next((n for n, t in enumerate(turns, 1) if n != number and _made((want,), t)), None)
                elsewhere = other is not None and not any(call.name == want.name for call in free)
                reasons.append(
                    f"{want} is called in turn {other}, not this one" if elsewhere else _why_missing(want, free)
                )
            # A call of a missing tool with other arguments is explained above, not listed again.
            explained = {want.name for want in missing}
            if extra := [call.name for call in free if call.name not in explained]:
                reasons.append(f"it also calls {', '.join(extra)}")
            return False, f"turn {number} of tool calls is not step {number}: {'; '.join(reasons)}"
        made, expected = _count(len(turns), "turn"), len(wanted)
        if len(turns) > expected:
            more = ", ".join(call.name for call in turns[expected])
            return False, f"{made} of tool calls, expected {expected}: turn {expected + 1} calls {more}"
        if len(turns) < expected:
            absent = _describe(wanted[len(turns)])
            return False, f"{made} of tool calls, expected {expected}: step {len(turns) + 1} ({absent}) is not made"
        return True, f"{made} of tool calls, as expected"

    def _in_order(self, wanted: tuple[Step, ...], turns: list[tuple[Call, ...]]) -> tuple[bool, str]:
        start, placed = 0, []
        for number, step in enumerate(wanted, 1):
            at = next((t for t in range(start, len(turns)) if _made(step, turns[t])), None)
            if at is None:
                return False, f"step {number} ({_describe(step)}) {self._not_after(step, turns, start, number)}"
            placed.append(str(at + 1))
            start = at + 1
        return (
            True,
            f"{_count(len(wanted), 'step')} in order, in turn{'s' if len(placed) > 1 else ''} {_listing(placed)}",
        )

    @staticmethod
    def _not_after(step: Step, turns: list[tuple[Call, ...]], start: int, number: int) -> str:
        """Why a step is not made after the step before it: it is made in the same turn as that
        one or before it, its calls are split across turns, or it is not made."""
        earlier = next((t for t in range(start) if _made(step, turns[t])), None)
        if earlier is not None and earlier == start - 1:
            return f"is made in the same turn as step {number - 1} (turn {start}), in parallel, not after it"
        if earlier is not None:
            return f"is made in turn {earlier + 1}, before step {number - 1}"
        later = [call for turn in turns[start:] for call in turn]
        if len(step) > 1 and _made(step, later):
            return "has its calls in different turns, not together in one"
        matched = _matching(step, later)
        missing = next(want for want, position in zip(step, matched, strict=True) if position is None)
        return (
            f"is not made{f' after turn {start}' if start else ''}: {_why_missing(missing, _unmatched(later, matched))}"
        )

    def _count(self, trajectory: Trajectory) -> tuple[bool, str]:
        if (self.min_turns, self.max_turns) != (None, None) and trajectory.source == "trace" and not trajectory.turns:
            raise ValueError(
                "the trace records no model call, so its turns cannot be counted; trace the target's model calls"
            )
        turns, calls = len(trajectory.turns), len(trajectory.calls)
        found = [_count(turns, "model turn"), _count(calls, "tool call")]
        for number, low, high, noun in (
            (turns, self.min_turns, self.max_turns, "turn"),
            (calls, self.min_calls, self.max_calls, "tool call"),
        ):
            if low is not None and number < low:
                found.append(f"under the minimum of {_count(low, noun)}")
            if high is not None and number > high:
                found.append(f"over the limit of {_count(high, noun)}")
        return len(found) == 2, ", ".join(found)


def _whole(name: str, value: Any) -> None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
        raise ValueError(f"{name} must be a whole number of 0 or more")


def _bounded(low_name: str, low: Any, high_name: str, high: Any) -> None:
    _whole(low_name, low)
    _whole(high_name, high)
    if low is not None and high is not None and low > high:
        raise ValueError(f"{low_name} is more than {high_name}")


def tool_options(
    check: str,
    *,
    calls: Sequence[Any] = (),
    expectation: str | None = None,
    arguments_match: str = "subset",
    order: str = "exact",
    source: str | None = None,
    min_calls: int | None = None,
    max_calls: int | None = None,
    min_turns: int | None = None,
    max_turns: int | None = None,
) -> ToolOptions:
    """A tool-call check's options, checked: the fixed ``calls`` (or steps), or ``expectation``,
    the expectations key each case gives them under, not both."""
    if check not in TOOL_CHECKS:
        raise ValueError(f"{check} is not a tool-call check ({', '.join(TOOL_CHECKS)})")
    if source is not None and source not in SOURCES:
        raise ValueError(f"source must be one of {', '.join(SOURCES)}")
    if arguments_match not in ARGUMENT_MATCHES:
        raise ValueError(f"arguments_match must be one of {', '.join(ARGUMENT_MATCHES)}")
    if order not in ORDERS:
        raise ValueError(f"order must be one of {', '.join(ORDERS)}")
    options = ToolOptions(
        check=check,
        match=arguments_match,
        order=order,
        expectation=expectation,
        source=source,
        min_calls=min_calls,
        max_calls=max_calls,
        min_turns=min_turns,
        max_turns=max_turns,
    )
    if check == "step_count":
        _bounded("min_turns", min_turns, "max_turns", max_turns)
        _bounded("min_calls", min_calls, "max_calls", max_calls)
        if (min_turns, max_turns, min_calls, max_calls) == (None, None, None, None):
            raise ValueError("a step_count check needs min_turns, max_turns, min_calls or max_calls")
        return options
    if check == "tool_called":
        _bounded("min_calls", 1 if min_calls is None else min_calls, "max_calls", max_calls)
        if min_calls == 0 and max_calls is None:
            raise ValueError("min_calls 0 with no max_calls passes every reply; give max_calls, or use tool_not_called")
    if calls and expectation is not None:
        raise ValueError("give the calls, or expectation='<key>' to read them from each case, not both")
    if not calls and expectation is None:
        raise ValueError("give the calls to look for, or expectation='<key>' to read them from each case")
    return replace(options, calls=options.read(list(calls), "calls")) if calls else options


__all__ = [
    "ARGUMENT_MATCHES",
    "ORDERS",
    "SOURCES",
    "TOOL_CHECKS",
    "Call",
    "Expected",
    "ToolOptions",
    "Trajectory",
    "fits",
    "from_reply",
    "from_trace",
    "problem",
    "steps",
    "tool_options",
]
