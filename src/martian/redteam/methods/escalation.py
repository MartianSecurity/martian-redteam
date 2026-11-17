"""Multi-turn escalation with backtracking (Crescendo-style).

:class:`Escalation` attacks in one conversation, through the target's own sessions (the conversation
contract the engine shares with suite-file conversation cases: one session id, the history kept and
resent as the target is set up to). Before each turn the attacker reads the conversation so far, as
data fields it is told never to follow, and the checks' finding, and writes the next message, a
small step closer to the goal. The checks read every reply; the attempt stops at the first hit.

When the target refuses a turn (its reply opens with a refusal, :func:`martian.redteam.engine.refusal.classify`)
and backtracks remain, the method goes back a turn: it starts a new conversation, sends again the
turns the target had accepted before the refusal (each a target query), and asks the attacker for a
different step, showing it the refused message. Every turn, the refused and the replayed ones too,
is kept in the attempt's turns, with a harness note at each backtrack.

Caps: ``max_turns`` turns in one conversation (replayed turns count, since the target sees them),
``max_backtracks`` backtracks, ``max_queries`` target queries in all.
"""

from __future__ import annotations

import json

from martian.redteam.methods.base import Arm, AttackContext, AttackMethod, Exchange, Feedback, MethodKind
from martian.redteam.methods.library import prompt

STYLE = "escalation"


def turn_request(
    accepted: list[Exchange], *, goal: str, turn: int, turns: int, feedback: Feedback | None, refused: str | None
) -> str:
    """The attacker's request for turn ``turn``: the conversation so far and any refused message as
    data fields, and what the checks found in the harness's words."""
    from martian.redteam.engine.hostile import ATTACKER_DATA_RULE, FIELD_LIMIT, data_block

    fields: list[tuple[str, str]] = []
    if accepted:
        conversation = [
            {"turn": number, "you": exchange.payload, "target": exchange.reply.text}
            for number, exchange in enumerate(accepted, start=1)
        ]
        fields.append(("CONVERSATION", json.dumps(conversation, ensure_ascii=False)))
    if refused is not None:
        fields.append(("REFUSED_MESSAGE", refused))
    situation = []
    if refused is not None:
        situation.append(prompt("escalation/backtrack.md"))
    if feedback is not None and not feedback.hit:
        situation.append(f"What the test's checks found on the last reply: {feedback.summary}")
    text = (
        prompt("escalation/turn.md")
        .replace("{goal}", goal)
        .replace("{turn}", str(turn))
        .replace("{turns}", str(turns))
        .replace("{situation}", " ".join(situation))
    )
    if not fields:
        return text
    return data_block(ATTACKER_DATA_RULE, fields, limit=FIELD_LIMIT * 3) + "\n\n" + text


class Escalation(AttackMethod):
    """Gradual escalation in one conversation, going back a turn when the target refuses."""

    kind: MethodKind = "conversation"
    arm: Arm = "adaptive"
    needs_attacker = True
    settings = {"max_turns": 6, "max_backtracks": 2, "max_queries": 20}

    async def run(self, context: AttackContext) -> Exchange:
        turns, backtracks_allowed = int(self.values["max_turns"]), int(self.values["max_backtracks"])
        session = context.conversation()
        accepted: list[Exchange] = []
        backtracks = 0
        refused: str | None = None
        feedback: Feedback | None = None
        last: Exchange | None = None

        def keep(ended: str) -> None:
            context.record(backtracks=backtracks, turns_in_conversation=len(accepted), ended=ended)

        while context.queries < self.query_cap and len(accepted) < turns:
            request = turn_request(
                accepted, goal=context.goal, turn=len(accepted) + 1, turns=turns, feedback=feedback, refused=refused
            )
            message = await context.generate(request=request, method=STYLE)
            last = await context.send(message, session=session)
            feedback = await context.check(last)
            if feedback.conclusive:
                accepted.append(last)
                keep("hit" if feedback.hit else "checks")
                return last
            if feedback.refused and backtracks < backtracks_allowed:
                backtracks += 1
                refused = message
                context.note(
                    f"Backtrack {backtracks}: the target refused turn {len(accepted) + 1}. A new conversation "
                    f"sends again the {len(accepted)} turn(s) before it."
                )
                session = context.conversation()
                replayed: list[Exchange] = []
                for earlier in accepted:
                    if context.queries >= self.query_cap:
                        break
                    last = await context.send(earlier.payload, session=session)
                    feedback = await context.check(last)
                    replayed.append(last)
                    if feedback.conclusive:
                        accepted = replayed
                        keep("hit" if feedback.hit else "checks")
                        return last
                accepted = replayed
                continue
            accepted.append(last)
            refused = None
        assert last is not None
        keep("max_turns" if len(accepted) >= turns else "max_queries")
        return last


__all__ = ["STYLE", "Escalation", "turn_request"]
