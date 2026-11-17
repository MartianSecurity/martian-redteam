"""The attack method interface: what a method is, what it may do in an attempt, and what it gets back.

A method drives one attempt of a probe. It writes attacks (with the attacker model, from fixed
templates, or by changing a seed), sends them to the target through :class:`AttackContext`, reads
:class:`Feedback` from the probe's checks, and returns the :class:`Exchange` the attempt is decided
on. The engine (:mod:`martian.redteam.engine.run`) implements the context: it reserves every call on
the budget ledger, handles attacker refusals, applies the method's transform layers and delivery
route, keeps every turn and the evidence, and verifies the final exchange.

Every method has one of four kinds:

- ``transform``: code changes the attack's text, with no model call (Base64, Morse). Used as an
  attack, a transform rewrites a generated prompt injection; used as a layer, it changes whatever
  another method sends.
- ``rewrite``: one attack per query, written without feedback: by the attacker model in a stated
  style, or from a fixed template.
- ``conversation``: several turns in one conversation, each written after the target's last reply.
- ``search``: several queries, each chosen from the checks' feedback on the ones before.

And one arm: ``control`` (fixed templates, the same every run), ``static`` (one written attack) or
``adaptive`` (it reads the target's replies or the checks' feedback).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Literal, Protocol

from martian.redteam.model import Probe
from martian.redteam.targets import TargetReply

MethodKind = Literal["transform", "rewrite", "conversation", "search"]
Arm = Literal["control", "static", "adaptive"]
GenerationMode = Literal["baseline", "enhance"]

Rung = Literal["matched", "wrong_arguments", "wrong_tool", "not_called", "no_match", "undetermined"]
"""How close a reply came to the goal, from the checks decided by code. For a tool goal: the goal
tool was not called, another tool was, the goal tool was called with other arguments, or the call
matched. For a text or canary check: no match, or matched. ``undetermined`` when no check decided."""

SCORES: dict[str, int] = {
    "undetermined": 0,
    "no_match": 0,
    "not_called": 0,
    "wrong_tool": 1,
    "wrong_arguments": 2,
    "matched": 3,
}
"""Each rung's rank: a search keeps the attacks whose replies came closest."""


@dataclass(frozen=True)
class Feedback:
    """What the probe's checks say about one reply. ``hit``: a check found the attack succeeded.
    ``conclusive``: the method stops here, on a hit or when the checks could not go on (the judge
    failed, the budget ran out). ``refused``: the target's reply opens with a refusal. ``summary``
    is written by the harness, never copied from the target."""

    hit: bool
    rung: Rung
    refused: bool = False
    conclusive: bool = False
    summary: str = ""
    source: Literal["code", "judge", "none"] = "none"

    @property
    def score(self) -> int:
        return 4 if self.hit else SCORES[self.rung]


@dataclass
class Exchange:
    """One query of the target in an attempt: the attack as the method wrote it (``payload``), the
    message the target was sent after the layers and the delivery route (``message``), its reply,
    the conversation it was sent in, and the checks' feedback once asked for. ``verified`` holds the
    full verification (a :class:`~martian.redteam.verify.verifiers.Decided`) when the feedback already
    ran every check, so it is not run twice."""

    number: int
    payload: str
    message: str
    reply: TargetReply
    session: str
    feedback: Feedback | None = None
    verified: Any = None
    attack: str = ""
    """The payload after the method's layers: what the attempt keeps as its ``input``."""
    sent: str = ""
    """What the target received: ``message`` with what the probe plants and its benign task."""


class AttackContext(Protocol):
    """What a method may do in one attempt. Every call is counted on the run's budget first; a
    limit that runs out raises, and the attempt is stopped."""

    @property
    def probe(self) -> Probe: ...

    @property
    def purpose(self) -> str: ...

    @property
    def goal(self) -> str:
        """The probe's goal in words: the run file's ``goal``, or the vulnerability's own."""
        ...

    @property
    def seed(self) -> int:
        """A number fixed for this attempt (its probe and number), for methods that draw at random."""
        ...

    @property
    def queries(self) -> int:
        """Target queries made so far in this attempt."""
        ...

    async def generate(
        self, *, request: str | None = None, method: str | None = None, mode: GenerationMode = "baseline"
    ) -> str:
        """One attack from the attacker model, in the style of attack method ``method`` (the method's
        own id by default). ``request`` is the attacker's instruction; target text in it must be a
        data field (:func:`martian.redteam.engine.hostile.data_field`). A refused or empty generation
        is asked for once more; a second refusal fails the attempt as the attacker's."""
        ...

    async def send(self, payload: str, *, session: str | None = None) -> Exchange:
        """Send ``payload``, after the method's layers and delivery route, in conversation ``session``
        (a new one when ``None``), keep the turn and return the exchange."""
        ...

    async def check(self, exchange: Exchange) -> Feedback:
        """The checks' feedback on ``exchange``: the checks decided by code when the probe has any,
        else the judge. Kept on the exchange."""
        ...

    def conversation(self) -> str:
        """A new conversation's session name. The first is the attempt's own id."""
        ...

    def note(self, text: str) -> None:
        """Add a harness note to the attempt's turns, such as a backtrack."""
        ...

    def record(self, **values: Any) -> None:
        """Add values to the attempt's method record (its ``trace`` evidence)."""
        ...


class Transform:
    """A local change of an attack's text, with no model call and no network. ``apply`` is a pure
    function of the text."""

    def __init__(self, id: str, name: str, label: str, apply: Callable[[str], str], tries: str) -> None:
        self.id = id
        self.name = name
        self.label = label
        self.apply = apply
        self.tries = tries

    def __call__(self, text: str) -> str:
        return self.apply(text)

    def __repr__(self) -> str:
        return f"Transform({self.id!r})"


class AttackMethod(ABC):
    """An attack method, configured for one ``attacks`` entry of a run file.

    A subclass sets ``kind``, ``arm``, ``needs_attacker`` and the run-file ``settings`` it takes
    (with their defaults), and implements :meth:`run`. ``id`` names the method in probe ids and
    reports; ``layers`` are transforms applied to every attack it sends, in order."""

    kind: MethodKind
    arm: Arm
    needs_attacker: bool = True
    settings: ClassVar[dict[str, Any]] = {}
    """The run-file settings this method takes, with their defaults."""
    max_queries: ClassVar[int] = 1
    """Target queries per attempt when the method has no ``max_queries`` setting."""
    legacy: bool = False
    """One of the six version 0.2 methods with no layers and the default route: its attempts and
    their fingerprints are what they were before methods had this interface."""

    def __init__(
        self,
        *,
        id: str,
        label: str,
        layers: Sequence[Transform] = (),
        values: dict[str, Any] | None = None,
    ) -> None:
        self.id = id
        self.label = label
        self.layers = tuple(layers)
        self.values = {**self.settings, **(values or {})}
        self.delivery: Any = None
        """The run file's ``delivery`` when the attacks take another route than the user message."""

    def setting(self, name: str) -> Any:
        return self.values[name]

    @property
    def query_cap(self) -> int:
        """The most target queries one attempt of this method makes."""
        return int(self.values.get("max_queries", self.max_queries))

    def describe(self) -> dict[str, Any]:
        """What the run records about the method; part of each attempt's fingerprint."""
        described: dict[str, Any] = {
            "id": self.id,
            "method": type(self).__name__,
            "kind": self.kind,
            "arm": self.arm,
            "needs_attacker": self.needs_attacker,
            "layers": [layer.id for layer in self.layers],
            "settings": dict(self.values),
        }
        if self.delivery is not None:
            described["delivery"] = self.delivery.model_dump(exclude_none=True)
        return described

    @abstractmethod
    async def run(self, context: AttackContext) -> Exchange:
        """Drive one attempt and return the exchange it is decided on."""


__all__ = [
    "SCORES",
    "Arm",
    "AttackContext",
    "AttackMethod",
    "Exchange",
    "Feedback",
    "GenerationMode",
    "MethodKind",
    "Rung",
    "Transform",
]
