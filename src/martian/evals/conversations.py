"""Conversation cases: several user turns sent in order, in one conversation, to a target that keeps
its state, and graded per turn and as a whole.

A conversation case (``Case.turns``) runs its turns one after another. Each turn's request carries
the conversation's state, so the target can keep it:

- ``session_id``: one id for the whole conversation, new for every conversation the runner starts
  (each attempt of a repeated case is its own conversation). It is also the ``session_id`` of each
  turn's trace, so the turns group into one session.
- ``history``: the earlier turns, as ``role`` and ``content`` messages (``user``, then
  ``assistant``), empty on the first turn.
- ``messages``: the history and then this turn's user message, the shape of a chat-completions
  request.

A function target is called with a turn's inputs and whichever of ``history`` and ``session_id`` it
takes. A URL target's body names them as placeholders: ``{{session_id}}``, ``{{history}}`` and
``{{messages}}``. The evals core's worker fills the same placeholders through the same code
(:func:`martian.evals.runner.request_body`), its cases being one-turn conversations.

A check grades either each turn's reply (scope ``turn``, the default) or the conversation once
(scope ``conversation``). A turn names the turn checks that grade it (``Turn.checks``); without a
list every turn check does. A conversation check grades the target's replies, joined by a blank
line, against the case's own expectations; a python one is given the conversation as ``messages``
in its inputs, and no trace. A tool-call check reads one call's tool calls, so it grades turns only.

The conversation's verdict on a turn check passes when the check passed every turn it grades
(:func:`over_turns`); the case passes when every check, so combined, passes it. A target call
that fails ends the conversation: the later turns are not sent, and the case has no verdict.

A conversation's user can be simulated instead of scripted (:mod:`martian.evals.simulation`): its
turns are then written as it goes, and its goal verdict is one more conversation-level result.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from martian.evals.repeats import combine

SESSION_ID = "session_id"
HISTORY = "history"
MESSAGES = "messages"
CONVERSATION_INPUTS = (SESSION_ID, HISTORY)
"""The names a conversation turn's request gives the target besides the turn's own inputs, which
therefore cannot use them."""
PLACEHOLDERS = (SESSION_ID, HISTORY, MESSAGES)
"""The body placeholders of a URL target that carry the conversation."""


def user_text(inputs: Mapping[str, Any], input_key: str) -> str:
    """A turn's user message: its input under ``input_key`` when that is text, else its inputs as JSON."""
    found = inputs.get(input_key)
    if isinstance(found, str):
        return found
    return json.dumps(dict(inputs), ensure_ascii=False, sort_keys=True, default=str)


def user(text: str) -> dict[str, str]:
    return {"role": "user", "content": text}


def assistant(text: str) -> dict[str, str]:
    return {"role": "assistant", "content": text}


def turn_checks(listed: Sequence[str] | None, turn_labels: Sequence[str]) -> list[str]:
    """The turn checks that grade a turn, in the suite's order: those the turn lists, or every turn
    check when it lists none."""
    if listed is None:
        return list(turn_labels)
    return [label for label in turn_labels if label in listed]


def graded_turns(listed: Sequence[Sequence[str] | None], turn_labels: Sequence[str]) -> dict[str, list[int]]:
    """Each turn check and the numbers of the turns of one case it grades, from each turn's list."""
    found: dict[str, list[int]] = {label: [] for label in turn_labels}
    for number, checks in enumerate(listed, 1):
        for label in turn_checks(checks, turn_labels):
            found[label].append(number)
    return found


def replies(turns: Sequence[Mapping[str, Any]]) -> str:
    """What a conversation check grades: the target's replies, in order, joined by a blank line."""
    return "\n\n".join(str(turn["output"]) for turn in turns if turn.get("output") is not None)


def _numbers(numbers: Sequence[int]) -> str:
    return ("turn " if len(numbers) == 1 else "turns ") + ", ".join(str(number) for number in numbers)


def over_turns(label: str, numbers: Sequence[int], turns: Mapping[int, Mapping[str, Any]]) -> dict[str, Any]:
    """A turn check's verdict on the conversation from its verdicts on the turns it grades: it passes
    when every one passed, fails when one failed, and is open otherwise (``all``, as for repeats)."""
    verdicts = [turns[number]["checks"][label]["passed"] for number in numbers]
    passed = combine(verdicts, "all")
    text = f"{sum(verdict is True for verdict in verdicts)} of {len(verdicts)} turns passed"
    if failed := [number for number, verdict in zip(numbers, verdicts, strict=True) if verdict is False]:
        text += f", failed on {_numbers(failed)}"
    if open_ := [number for number, verdict in zip(numbers, verdicts, strict=True) if verdict is None]:
        text += f", no verdict on {_numbers(open_)}"
    return {
        "passed": passed,
        "rationale": text if passed is not None else None,
        "error": None if passed is not None else f"the turns without a verdict leave it open: {text}",
    }


def conversation_record(
    turns: Sequence[Mapping[str, Any]],
    *,
    labels: Sequence[str],
    graded: Mapping[str, Sequence[int]],
    conversation: Mapping[str, Mapping[str, Any] | None],
    session_id: str,
    messages: list[dict[str, str]],
    broken: int | None,
    error: str | None = None,
) -> dict[str, Any]:
    """One conversation's record: its verdict, each check's verdict on it, the conversation as
    messages, the error of the turn that failed, its session id and its turns.

    ``turns`` are the turns sent, in order; ``labels`` every check in the suite's order; ``graded``
    each turn check and the numbers of the turns it grades; ``conversation`` each conversation
    check and its verdict, None when it has none; ``broken`` the number of the turn that did not
    complete, if one did not, and ``error`` why, when no turn sent says so (a simulated user that
    could not write it). A turn check that grades no turn of this conversation is not among its
    checks. When a turn did not complete, no check has a verdict: the conversation did not finish.
    """
    by_number = {turn["number"]: turn for turn in turns}
    unfinished = {"passed": None, "rationale": None, "error": f"turn {broken} did not complete"}
    checks: dict[str, dict[str, Any]] = {}
    for label in labels:
        if graded.get(label):
            checks[label] = dict(unfinished) if broken is not None else over_turns(label, graded[label], by_number)
        elif label in conversation:
            found = conversation[label]
            if broken is not None:
                checks[label] = dict(unfinished)
            elif found is None:
                checks[label] = {"passed": None, "rationale": None, "error": "no result was recorded for this check"}
            else:
                checks[label] = dict(found)
    values = [check["passed"] for check in checks.values()]
    error = next((turn["error"] for turn in turns if turn.get("error")), error)
    return {
        "passed": None if broken is not None or any(value is None for value in values) else all(values),
        "checks": checks,
        "output": messages,
        "error": error,
        "session_id": session_id,
        "turns": [dict(turn) for turn in turns],
    }


__all__ = [
    "CONVERSATION_INPUTS",
    "HISTORY",
    "MESSAGES",
    "PLACEHOLDERS",
    "SESSION_ID",
    "assistant",
    "conversation_record",
    "graded_turns",
    "over_turns",
    "replies",
    "turn_checks",
    "user",
    "user_text",
]
