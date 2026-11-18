"""Simulator fidelity: how close a simulator's users come to real ones.

    martian evals fidelity transcripts.jsonl --results results --output fidelity.json
    martian evals fidelity --session <id> --session <id> --results results --output fidelity.json

A simulator flatters the agent when its users are more patient, shorter-spoken or more easily
satisfied than real ones. The fidelity report compares the simulated conversations of one or more
``martian evals run`` output folders with real conversations, the transcripts the persona builder
reads (:mod:`martian.evals.personas`), on three measures:

- **Turns**: the messages the user sent in each conversation.
- **Corrections**: user messages that correct or repeat a request, found by the deterministic
  rules below, and the share of conversations with at least one.
- **Goal reached**: for a simulated conversation, its ``goal_reached`` verdict, which the
  simulator gives; for a real one, its ``goal_reached`` label when the transcript has one, else
  the outcome rule below, which can leave it unknown.

**One report per simulator setup.** A setup is what ``summary.json`` names under ``simulator``:
the model, its inference parameters, the simulator's instruction version, and the persona source
(the data file the simulated users came from and the dataset's digest). Output folders of the same
setup are read as one; each other setup has its own report. A conversation that ended on an error
(a failed target or simulator call) is left out and counted.

**Intervals** are :mod:`martian.evals.stats`'s: the Student t interval for a mean and the Wilson
interval for a share, at 95%. With repeats a case's attempts are conversations of one persona, so
the simulated side groups them by case (the design effect); real conversations are independent. A
difference, simulated minus real, has Newcombe's hybrid score interval for two shares and the same
construction (MOVER, from the two sides' own intervals) for two means; it reads ``higher`` or
``lower`` when its interval leaves out 0, else ``within_noise``.

**Corrections** (``corrections-v1``). The first message is never a correction. A later user message
is one when, compared case-insensitively with curly apostrophes made straight:

- it holds one of :data:`CORRECTION_PHRASES` as whole words, such as ``i meant`` or ``not what i``;
  or
- it repeats an earlier user message of the conversation: each has at least two content words (the
  runs of letters or digits, less single letters and :data:`STOPWORDS`), and the words they share
  are at least 60% of the words either has (a Jaccard similarity of at least
  :data:`REPEAT_OVERLAP`).

**The outcome rule** (``outcome-v1``), for a real conversation with no label, reads its last user
message: the goal was not reached when that message is a correction or holds one of
:data:`UNRESOLVED_PHRASES`; it was reached when it holds one of :data:`CLOSING_PHRASES`; otherwise
the outcome is unknown and the conversation is left out of the goal's share.

The phrases are English. The report holds counts, case and transcript ids, with the personal data
``no_personal_data`` finds replaced by its kind, and turn numbers, never a message.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from statistics import fmean, median
from typing import Any, Literal

from martian.config import ConfigError
from martian.evals._policy import redact_personal_data
from martian.evals.personas import Transcript
from martian.evals.simulation import GOAL
from martian.evals.stats import mean_interval, proportion_interval

VERSION = "fidelity-v1"
"""The version of the report's shape and rules: the correction rules ``corrections-v1`` and the
outcome rule ``outcome-v1``. A change to either is a new version."""

CORRECTION_PHRASES = (
    "i meant",
    "i said",
    "i asked",
    "i already",
    "i told you",
    "as i said",
    "like i said",
    "not what i",
    "that's not right",
    "that is not right",
    "that's not correct",
    "that is not correct",
    "that's wrong",
    "that is wrong",
    "that's incorrect",
    "that is incorrect",
    "you misunderstood",
    "you got it wrong",
    "let me rephrase",
    "to clarify",
    "to be clear",
    "doesn't answer",
    "does not answer",
    "didn't answer",
    "did not answer",
)
"""Phrases that make a later user message a correction, matched as whole words."""

UNRESOLVED_PHRASES = (
    "didn't help",
    "did not help",
    "doesn't help",
    "does not help",
    "not helpful",
    "didn't work",
    "did not work",
    "doesn't work",
    "does not work",
    "still not",
    "never mind",
    "nevermind",
    "forget it",
    "give up",
    "useless",
)
"""Phrases in a real conversation's last user message that say its goal was not reached."""

CLOSING_PHRASES = (
    "thank",
    "thanks",
    "thx",
    "cheers",
    "perfect",
    "great",
    "brilliant",
    "awesome",
    "that works",
    "that worked",
    "that helps",
    "that helped",
    "got it",
    "all set",
    "all good",
    "sorted",
    "solved",
    "resolved",
    "appreciate it",
)
"""Phrases in a real conversation's last user message that say its goal was reached."""

STOPWORDS = frozenset(
    [
        "about",
        "am",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "being",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "hello",
        "hey",
        "hi",
        "how",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "just",
        "may",
        "me",
        "might",
        "mine",
        "must",
        "my",
        "of",
        "ok",
        "okay",
        "on",
        "or",
        "our",
        "please",
        "shall",
        "should",
        "so",
        "thank",
        "thanks",
        "that",
        "the",
        "there",
        "these",
        "this",
        "those",
        "to",
        "us",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "would",
        "yes",
        "you",
        "your",
        "yours",
    ]
)
"""Words a repeated request does not need to share: pleasantries and grammar. ``no`` and ``not``
are content words."""

REPEAT_OVERLAP = 0.6
"""The Jaccard similarity of two user messages' content words at which the later repeats the earlier."""

REPEAT_WORDS = 2
"""The fewest content words each message needs for the repeat rule to compare them."""

Outcome = Literal["simulator", "label", "rule"]
Reading = Literal["higher", "lower", "within_noise"]


def _normal(text: str) -> str:
    return " ".join(text.casefold().replace("’", "'").replace("‘", "'").split())


def _pattern(phrases: Sequence[str]) -> re.Pattern[str]:
    alternatives = sorted((r"\s+".join(map(re.escape, phrase.split())) for phrase in phrases), key=len, reverse=True)
    return re.compile(r"(?<!\w)(?:" + "|".join(alternatives) + r")(?!\w)")


_CORRECTS = _pattern(CORRECTION_PHRASES)
_UNRESOLVED = _pattern(UNRESOLVED_PHRASES)
_CLOSING = _pattern(CLOSING_PHRASES)


def _words(text: str) -> set[str]:
    """A message's content words: runs of letters or digits, less single letters and stopwords."""
    return {
        word
        for word in re.findall(r"[^\W_]+", _normal(text))
        if word not in STOPWORDS and (len(word) > 1 or word.isdigit())
    }


def _repeats(message: str, earlier: Sequence[str]) -> bool:
    words = _words(message)
    if len(words) < REPEAT_WORDS:
        return False
    for before in earlier:
        other = _words(before)
        if len(other) >= REPEAT_WORDS and len(words & other) / len(words | other) >= REPEAT_OVERLAP:
            return True
    return False


def corrections(messages: Sequence[str]) -> list[int]:
    """The numbers, from 1, of a conversation's user ``messages`` that correct or repeat a request
    (``corrections-v1``, in this module's docstring). The first message is never one."""
    return [
        number
        for number, message in enumerate(messages, 1)
        if number > 1 and (_CORRECTS.search(_normal(message)) or _repeats(message, messages[: number - 1]))
    ]


def outcome_by_rule(messages: Sequence[str]) -> bool | None:
    """Whether a real conversation reached its user's goal, from its user ``messages`` by the
    outcome rule (``outcome-v1``): False when the last message is a correction or says the help did
    not work, True when it thanks or confirms, else None."""
    if not messages:
        return None
    last = _normal(messages[-1])
    if corrections(messages)[-1:] == [len(messages)] or _UNRESOLVED.search(last):
        return False
    if _CLOSING.search(last):
        return True
    return None


@dataclass(frozen=True)
class Conversation:
    """One conversation as the report reads it: its id, the group it belongs to (a simulated
    conversation's case, a real one's own id), its user messages, and whether the goal was reached
    with where that came from (``outcome``, None when unknown)."""

    id: str
    group: str
    messages: tuple[str, ...]
    goal_reached: bool | None
    outcome: Outcome | None


@dataclass(frozen=True)
class Simulated:
    """The simulated conversations of one ``martian evals run`` output folder and its setup."""

    folder: Path
    setup: dict[str, Any]
    cases: int
    conversations: list[Conversation]
    left_out: int


def _clean(identifier: str) -> str:
    return redact_personal_data(identifier)[0]


def read_simulated(folder: str | Path) -> Simulated:
    """The simulated conversations in an output folder of ``martian evals run``: each attempt of
    each case with a simulated user in the folder's last run (the cases ``junit.xml`` lists, by
    their latest ``results.jsonl`` record), its user messages and its ``goal_reached`` verdict,
    and the run's simulator setup from ``summary.json``."""
    where = Path(folder)
    try:
        summary = json.loads((where / "summary.json").read_text(encoding="utf-8"))
        lines = (where / "results.jsonl").read_text(encoding="utf-8").splitlines()
        order = [str(case.get("name")) for case in ET.parse(where / "junit.xml").getroot().iter("testcase")]
    except (OSError, ValueError, ET.ParseError) as error:
        raise ConfigError(
            f"{where} is not the output folder of a finished martian evals run: {type(error).__name__}"
        ) from None
    simulator = summary.get("simulator")
    if not isinstance(simulator, dict):
        raise ConfigError(f"{where}: the run has no simulated users")
    setup = {name: simulator.get(name) for name in ("model", "parameters", "version", "personas")}
    latest: dict[str, dict[str, Any]] = {}
    for line in lines:
        if line.strip():
            record = json.loads(line)
            latest[str(record["id"])] = record
    records = [latest[name] for name in order if name in latest and latest[name].get("simulated") is True]
    found: list[Conversation] = []
    left_out = 0
    for record in records:
        case = str(record["id"])
        units = record.get("attempts") or [record]
        for unit in units:
            if unit.get("error"):
                left_out += 1
                continue
            messages = tuple(
                str(message["content"]) for message in unit.get("output") or [] if message.get("role") == "user"
            )
            goal = (unit.get("checks") or {}).get(GOAL) or {}
            reached = goal.get("passed")
            identifier = f"{case} attempt {unit['number']}" if "attempts" in record else case
            found.append(
                Conversation(_clean(identifier), case, messages, reached, None if reached is None else "simulator")
            )
    return Simulated(where, setup, len(records), found, left_out)


def real_conversations(transcripts: Sequence[Transcript]) -> list[Conversation]:
    """Real conversations from transcripts: each one's user messages, and its goal as labelled, else
    by the outcome rule."""
    found = []
    for transcript in transcripts:
        messages = tuple(message["content"] for message in transcript.messages if message["role"] == "user")
        if transcript.goal_reached is not None:
            reached: bool | None = transcript.goal_reached
            outcome: Outcome | None = "label"
        else:
            reached = outcome_by_rule(messages)
            outcome = None if reached is None else "rule"
        identifier = _clean(transcript.id)
        found.append(Conversation(identifier, identifier, messages, reached, outcome))
    return found


def transcripts_source(path: str | Path) -> dict[str, Any]:
    """The report's name for a transcripts file: as given, with the SHA-256 of its contents."""
    file = Path(path)
    return {"transcripts": str(path), "digest": hashlib.sha256(file.read_bytes()).hexdigest()}


def sessions_source(session_ids: Sequence[str]) -> dict[str, Any]:
    """The report's name for sessions read from the local Evals UI's data folder."""
    return {"sessions": [_clean(session_id) for session_id in session_ids]}


def _interval(found: tuple[float, float] | None) -> list[float] | None:
    return None if found is None else [found[0], found[1]]


def difference(
    simulated: float | None,
    simulated_95: Sequence[float] | None,
    real: float | None,
    real_95: Sequence[float] | None,
) -> dict[str, Any]:
    """Simulated minus real, with its 95% interval from the two sides' own (MOVER: Newcombe's hybrid
    score interval when they are Wilson intervals of two shares), and how it reads: ``higher`` or
    ``lower`` when the interval leaves out 0, else ``within_noise``; no interval and no reading
    when a side has none."""
    if simulated is None or real is None:
        return {"value": None, "confidence_95": None, "reading": None}
    value = simulated - real
    if simulated_95 is None or real_95 is None:
        return {"value": value, "confidence_95": None, "reading": None}
    low = value - math.hypot(simulated - simulated_95[0], real_95[1] - real)
    high = value + math.hypot(simulated_95[1] - simulated, real - real_95[0])
    reading: Reading = "higher" if low > 0 else "lower" if high < 0 else "within_noise"
    return {"value": value, "confidence_95": [low, high], "reading": reading}


def _turns(conversations: Sequence[Conversation]) -> dict[str, Any]:
    lengths = [len(c.messages) for c in conversations]
    if not lengths:
        return {"conversations": 0, "turns": 0, "mean": None, "confidence_95": None, "median": None, "max": None}
    return {
        "conversations": len(lengths),
        "turns": sum(lengths),
        "mean": fmean(lengths),
        "confidence_95": _interval(mean_interval(lengths, [c.group for c in conversations])),
        "median": median(lengths),
        "max": max(lengths),
    }


def _share(outcomes: Sequence[bool], groups: Sequence[str]) -> tuple[float | None, list[float] | None]:
    if not outcomes:
        return None, None
    return sum(outcomes) / len(outcomes), _interval(proportion_interval(outcomes, groups))


def _corrections(conversations: Sequence[Conversation]) -> dict[str, Any]:
    found = [len(corrections(c.messages)) for c in conversations]
    rate, interval = _share([count > 0 for count in found], [c.group for c in conversations])
    turns = sum(len(c.messages) for c in conversations)
    return {
        "conversations": len(found),
        "with_correction": sum(count > 0 for count in found),
        "rate": rate,
        "confidence_95": interval,
        "corrections": sum(found),
        "turns": turns,
        "per_turn": sum(found) / turns if turns else None,
    }


def _goal(conversations: Sequence[Conversation]) -> dict[str, Any]:
    known = [c for c in conversations if c.goal_reached is not None]
    rate, interval = _share([bool(c.goal_reached) for c in known], [c.group for c in known])
    sources: dict[str, int] = {}
    for conversation in known:
        assert conversation.outcome is not None  # a known outcome says where it came from
        sources[conversation.outcome] = sources.get(conversation.outcome, 0) + 1
    return {
        "conversations": len(known),
        "reached": sum(bool(c.goal_reached) for c in known),
        "not_reached": sum(c.goal_reached is False for c in known),
        "unknown": len(conversations) - len(known),
        "rate": rate,
        "confidence_95": interval,
        "from": sources,
    }


def _measure(simulated: dict[str, Any], real: dict[str, Any], key: str) -> dict[str, Any]:
    return {
        "simulated": simulated,
        "real": real,
        "difference": difference(simulated[key], simulated["confidence_95"], real[key], real["confidence_95"]),
    }


def _listed(conversations: Sequence[Conversation]) -> list[dict[str, Any]]:
    return [
        {
            "id": c.id,
            "turns": len(c.messages),
            "corrections": corrections(c.messages),
            "goal_reached": c.goal_reached,
            "outcome": c.outcome,
        }
        for c in conversations
    ]


def fidelity_report(
    simulated: Sequence[Simulated], real: Sequence[Conversation], source: dict[str, Any]
) -> dict[str, Any]:
    """The fidelity report: the real conversations (``source`` names them), and one report per
    simulator setup among ``simulated``, in the order the setups first appear, each comparing that
    setup's simulated conversations with the real ones on turns, corrections and goal reached."""
    if not real:
        raise ConfigError("there are no real conversations to compare with")
    setups: dict[str, list[Simulated]] = {}
    for run in simulated:
        setups.setdefault(json.dumps(run.setup, sort_keys=True), []).append(run)
    real_turns, real_corrections, real_goal = _turns(real), _corrections(real), _goal(real)
    reports = []
    for runs in setups.values():
        played = [conversation for run in runs for conversation in run.conversations]
        reports.append(
            {
                "setup": runs[0].setup,
                "results": [str(run.folder) for run in runs],
                "cases": sum(run.cases for run in runs),
                "left_out": sum(run.left_out for run in runs),
                "turns": _measure(_turns(played), real_turns, "mean"),
                "corrections": _measure(_corrections(played), real_corrections, "rate"),
                "goal_reached": _measure(_goal(played), real_goal, "rate"),
                "conversations": _listed(played),
            }
        )
    return {
        "version": VERSION,
        "real": {"source": source, "conversations": _listed(real)},
        "reports": reports,
    }


def write_report(report: dict[str, Any], path: str | Path) -> Path:
    """Write the report as JSON."""
    file = Path(path)
    file.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return file


__all__ = [
    "CLOSING_PHRASES",
    "CORRECTION_PHRASES",
    "REPEAT_OVERLAP",
    "REPEAT_WORDS",
    "STOPWORDS",
    "UNRESOLVED_PHRASES",
    "VERSION",
    "Conversation",
    "Simulated",
    "corrections",
    "difference",
    "fidelity_report",
    "outcome_by_rule",
    "read_simulated",
    "real_conversations",
    "sessions_source",
    "transcripts_source",
    "write_report",
]
