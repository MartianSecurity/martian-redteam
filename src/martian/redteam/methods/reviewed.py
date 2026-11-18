"""Reviewed attack cases as an attack source: the hosted engine writes each attack from them.

``ReviewedCases`` asks a connected Martian server for one attack per query with ``source:
reviewed_cases``: the server's engine picks one published reviewed case and one attack skill for the
probe's vulnerability and type, writes the attack from them on the server, and answers the attack
alone. The cases and skills never reach this package. It needs a Martian connection and an edition
that includes reviewed cases; a Community organisation is refused, and the attempt is an error.
"""

from __future__ import annotations

from martian.redteam.methods.base import Arm, AttackContext, AttackMethod, Exchange, MethodKind

REVIEWED_CASES = "reviewed_cases"
STYLE = "prompt_injection"
"""The catalogue style the server writes in."""


class ReviewedCases(AttackMethod):
    """One attack written by the Martian server from a reviewed case, sent once."""

    kind: MethodKind = "rewrite"
    arm: Arm = "static"
    needs_attacker = True
    hosted_only = True

    async def run(self, context: AttackContext) -> Exchange:
        payload = await context.generate(method=STYLE, source=REVIEWED_CASES)
        context.record(source=REVIEWED_CASES)
        return await context.send(payload, session=context.conversation())


__all__ = ["REVIEWED_CASES", "ReviewedCases"]
