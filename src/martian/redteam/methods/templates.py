"""Fixed templates: the control arm every adaptive result is compared with.

``prompts/templates-v1.json`` is a versioned set of attack templates (a direct request, instruction
overrides, "important instructions", a behaviour update in the Skeleton Key style, a mode switch, a
closed delimiter, an answer prefix, a task wrapper and urgency). Each is filled with the probe's
request: the run file's ``goal``, or the vulnerability's own request from the same file. The method
needs no attacker model and draws nothing at random, so its attacks are identical on every run. It
sends the templates in order, each in a conversation of its own, and stops at the first one a check
finds succeeded.
"""

from __future__ import annotations

import json
from functools import cache
from typing import Any

from martian.redteam.methods.base import Arm, AttackContext, AttackMethod, Exchange, MethodKind
from martian.redteam.methods.library import FOLDER
from martian.redteam.model import Probe

TEMPLATES_VERSION = "templates-v1"


@cache
def template_set(version: str = TEMPLATES_VERSION) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((FOLDER / f"{version}.json").read_text(encoding="utf-8"))
    return loaded


def request_of(probe: Probe, version: str = TEMPLATES_VERSION) -> str:
    """What the templates ask the target to do: the probe's goal, or its vulnerability's request."""
    found = template_set(version)
    return probe.goal or found["requests"].get(probe.vulnerability, found["generic_request"])


def filled(probe: Probe, version: str = TEMPLATES_VERSION, *, stated: str = "") -> list[tuple[str, str]]:
    """Every template of ``version`` filled for ``probe``, as ``(template id, attack)``, in order.
    ``stated`` is the goal the run states for the attempt, when it adds to the probe's own (the
    address of a receipt listener)."""
    request = stated or request_of(probe, version)
    return [(item["id"], item["text"].replace("{request}", request)) for item in template_set(version)["templates"]]


class Templates(AttackMethod):
    """The fixed templates, in order, up to ``max_queries`` of them, stopping at the first hit."""

    kind: MethodKind = "rewrite"
    arm: Arm = "control"
    needs_attacker = False
    settings = {"max_queries": len(template_set()["templates"])}

    def describe(self) -> dict[str, Any]:
        return {**super().describe(), "templates": TEMPLATES_VERSION}

    async def run(self, context: AttackContext) -> Exchange:
        exchange: Exchange | None = None
        sent: list[str] = []
        for template, attack in filled(context.probe, stated=context.stated_goal)[: self.query_cap]:
            sent.append(template)
            context.record(templates_sent=list(sent))
            exchange = await context.send(attack, session=context.conversation())
            if (await context.check(exchange)).conclusive:
                break
        assert exchange is not None
        return exchange


__all__ = ["TEMPLATES_VERSION", "Templates", "filled", "request_of", "template_set"]
