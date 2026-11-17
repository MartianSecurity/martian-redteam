"""Single-turn rewrites: the attacker model writes one attack in a stated style, and it is sent once.

The three catalogue methods (prompt injection, roleplay, prompt probing) take their style from the
catalogue's generator prompt (:data:`martian.redteam.catalog.prompts.ATTACK_METHODS`). A transform used
as an attack is a prompt-injection rewrite with that transform as its layer.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from martian.redteam.methods.base import Arm, AttackContext, AttackMethod, Exchange, MethodKind, Transform


class Rewrite(AttackMethod):
    """One attack written by the attacker in the style of ``style`` (an attack method id), sent once.
    ``request`` is the attacker's instruction, when the style is not a catalogue method."""

    kind: MethodKind = "rewrite"
    arm: Arm = "static"

    def __init__(
        self,
        *,
        id: str,
        label: str,
        style: str,
        request: str | None = None,
        layers: Sequence[Transform] = (),
        values: dict[str, Any] | None = None,
        kind: MethodKind = "rewrite",
    ) -> None:
        super().__init__(id=id, label=label, layers=layers, values=values)
        self.style = style
        self.request = request
        self.kind = kind  # a transform used as an attack is a rewrite under that transform

    def describe(self) -> dict[str, Any]:
        return {**super().describe(), "style": self.style}

    async def run(self, context: AttackContext) -> Exchange:
        payload = await context.generate(request=self.request, method=self.style)
        return await context.send(payload, session=context.conversation())


__all__ = ["Rewrite"]
