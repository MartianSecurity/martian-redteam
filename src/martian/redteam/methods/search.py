"""Adaptive search: refinement driven by the check result, and tree search on the same engine.

:class:`Search` writes attacks in levels. The first level has ``branching`` attacks written fresh
toward the goal; each later level has ``branching`` refinements of each of the ``width`` best attacks
of the level before. Every attack is sent in a conversation of its own and read with the checks'
feedback (:meth:`~martian.redteam.methods.base.AttackContext.check`): the deterministic checks when the
probe has any, graded for a tool goal as not called, wrong tool, wrong arguments or matched; the judge
only when the probe has no other check. The search stops at the first hit, after ``depth`` levels, or
at ``max_queries`` target queries, whichever comes first, and the attempt is decided on the hit or on
the attack whose reply came closest.

- :class:`Refine` is the search with one line of attack (width 1, branching 1): PAIR-style
  refinement, about ten target queries.
- :class:`TreeSearch` branches and prunes (TAP-style), with width, branching and depth capped.

A refinement's request carries the last attack, the target's reply and its tool calls as data fields
the attacker is told never to follow (:data:`~martian.redteam.engine.hostile.ATTACKER_DATA_RULE`),
and the checks' finding in the harness's own words. The text is in ``prompts/search/``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from martian.redteam.methods.base import Arm, AttackContext, AttackMethod, Exchange, MethodKind
from martian.redteam.methods.library import prompt

BASE_STYLE = "prompt_injection"
"""The attack method whose style the attacker writes in: the catalogue's prompt injection."""


def start_request(goal: str) -> str:
    return prompt("search/start.md").replace("{goal}", goal)


def refine_request(parent: Exchange, *, goal: str, queries: int) -> str:
    """The attacker's request for a refinement of ``parent``: its attack and the target's reply as
    data fields, and the checks' finding in the harness's words."""
    from martian.redteam.engine.hostile import ATTACKER_DATA_RULE, FIELD_LIMIT, data_block

    feedback = parent.feedback
    calls = [{"name": call.name, "arguments": call.arguments} for call in parent.reply.tool_calls]
    fields = data_block(
        ATTACKER_DATA_RULE,
        [
            ("LAST_ATTACK", parent.payload),
            ("TARGET_REPLY", parent.reply.text),
            ("TOOLS_CALLED", json.dumps(calls, ensure_ascii=False, default=str)),
        ],
        limit=FIELD_LIMIT,
    )
    signal = feedback.summary if feedback is not None else "No check could decide this reply."
    text = (
        prompt("search/refine.md")
        .replace("{goal}", goal)
        .replace("{signal}", signal)
        .replace("{query}", str(parent.number))
        .replace("{queries}", str(queries))
    )
    return f"{fields}\n\n{text}"


@dataclass
class _Node:
    exchange: Exchange
    level: int
    parent: int | None

    def record(self) -> dict[str, Any]:
        feedback = self.exchange.feedback
        return {
            "query": self.exchange.number,
            "level": self.level,
            "parent": self.parent,
            "rung": feedback.rung if feedback is not None else None,
            "hit": bool(feedback and feedback.hit),
        }


class Search(AttackMethod):
    """The shared engine: ``width`` attacks kept per level, ``branching`` children each, ``depth``
    levels, at most ``max_queries`` target queries."""

    kind: MethodKind = "search"
    arm: Arm = "adaptive"
    needs_attacker = True

    @property
    def shape(self) -> tuple[int, int, int]:
        """Width, branching and depth."""
        return int(self.values["width"]), int(self.values["branching"]), int(self.values["depth"])

    @property
    def query_cap(self) -> int:
        """``max_queries``, or fewer when the tree is smaller: ``branching`` attacks at the first
        level, then ``branching`` for each of at most ``width`` kept from the level before."""
        width, branching, depth = self.shape
        level, total = branching, branching
        for _ in range(depth - 1):
            level = branching * min(width, level)
            total += level
        return min(int(self.values["max_queries"]), total)

    async def run(self, context: AttackContext) -> Exchange:
        width, branching, depth = self.shape
        goal = context.goal
        nodes: list[_Node] = []
        leaves: list[_Node] = []
        found: Exchange | None = None

        def keep() -> None:
            context.record(tree=[node.record() for node in nodes], search_shape=[width, branching, depth])

        for level in range(depth):
            parents: list[_Node | None] = [None] if level == 0 else list(leaves)
            children: list[_Node] = []
            for parent in parents:
                for _ in range(branching):
                    if context.queries >= self.query_cap:
                        break
                    if parent is None:
                        payload = await context.generate(request=start_request(goal), method=BASE_STYLE)
                    else:
                        request = refine_request(parent.exchange, goal=goal, queries=self.query_cap)
                        payload = await context.generate(request=request, method=BASE_STYLE, mode="enhance")
                    exchange = await context.send(payload, session=context.conversation())
                    feedback = await context.check(exchange)
                    node = _Node(exchange, level, parent.exchange.number if parent is not None else None)
                    nodes.append(node)
                    children.append(node)
                    keep()
                    if feedback.conclusive:
                        return exchange
            if not children:
                break
            ranked = sorted(children, key=lambda node: (_score(node), node.exchange.number), reverse=True)
            leaves = ranked[:width]
            if found is None or _score(leaves[0]) >= _score_of(found):
                found = leaves[0].exchange
        assert found is not None
        context.record(ended="cap" if context.queries >= self.query_cap else "depth")
        return found


def _score(node: _Node) -> int:
    return _score_of(node.exchange)


def _score_of(exchange: Exchange) -> int:
    return exchange.feedback.score if exchange.feedback is not None else 0


class Refine(Search):
    """PAIR-style refinement: one line of attack, each attack refined from the last one's feedback."""

    settings = {"max_queries": 10}

    @property
    def shape(self) -> tuple[int, int, int]:
        return 1, 1, int(self.values["max_queries"])


class TreeSearch(Search):
    """TAP-style tree search: branching and pruning on the refinement engine."""

    settings = {"width": 3, "branching": 2, "depth": 3, "max_queries": 20}


__all__ = ["BASE_STYLE", "Refine", "Search", "TreeSearch", "refine_request", "start_request"]
