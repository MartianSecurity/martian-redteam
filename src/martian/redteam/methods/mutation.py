"""Mutation: random changes of a seed attack, with no attacker model (Best-of-N style).

:class:`Mutation` takes the probe's direct request from the fixed templates (``Please {request}.``)
as its seed and sends up to ``variants`` changed copies of it, each in a conversation of its own,
stopping at the first one a check finds succeeded. Each copy changes the seed at random: letter case,
spacing, the order of neighbouring words and the order of letters inside words. The draws come from
a generator seeded by the attempt (its probe and number), so a rerun of the attempt sends the same
copies. It calls no model: it runs when the run file names no attacker, and every copy is a target
query on the budget.
"""

from __future__ import annotations

import random

from martian.redteam.methods.base import Arm, AttackContext, AttackMethod, Exchange, MethodKind
from martian.redteam.methods.templates import filled

CASE = 0.4
"""The chance each letter changes case."""
SPACING = 0.15
"""The chance a space is doubled or dropped."""
SWAP = 0.15
"""The chance two neighbouring words change places."""
SCRAMBLE = 0.3
"""The chance a word longer than three letters has its inner letters shuffled."""


def mutate(text: str, draw: random.Random) -> str:
    """One random copy of ``text``."""
    words = text.split(" ")
    for index in range(len(words) - 1):
        if draw.random() < SWAP:
            words[index], words[index + 1] = words[index + 1], words[index]
    changed = []
    for word in words:
        if len(word) > 3 and draw.random() < SCRAMBLE:
            inner = list(word[1:-1])
            draw.shuffle(inner)
            word = word[0] + "".join(inner) + word[-1]
        changed.append(word)
    spaced = ""
    for index, word in enumerate(changed):
        if index:
            roll = draw.random()
            spaced += "" if roll < SPACING / 2 else ("  " if roll < SPACING else " ")
        spaced += word
    return "".join(char.swapcase() if char.isalpha() and draw.random() < CASE else char for char in spaced)


class Mutation(AttackMethod):
    """Up to ``variants`` random copies of the seed, stopping at the first hit."""

    kind: MethodKind = "search"
    arm: Arm = "adaptive"
    needs_attacker = False
    settings = {"variants": 10}

    @property
    def query_cap(self) -> int:
        return int(self.values["variants"])

    async def run(self, context: AttackContext) -> Exchange:
        seed = dict(filled(context.probe, stated=context.stated_goal))["direct"]
        draw = random.Random(context.seed)
        exchange: Exchange | None = None
        for number in range(1, self.query_cap + 1):
            exchange = await context.send(mutate(seed, draw), session=context.conversation())
            context.record(variants_sent=number)
            if (await context.check(exchange)).conclusive:
                break
        assert exchange is not None
        return exchange


__all__ = ["CASE", "SCRAMBLE", "SPACING", "SWAP", "Mutation", "mutate"]
