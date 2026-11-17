"""Single-turn rewrites: the attacker model writes one attack in a stated style, and it is sent once.

The three catalogue methods (prompt injection, roleplay, prompt probing) take their style from the
catalogue's generator prompt (:data:`martian.redteam.catalog.prompts.ATTACK_METHODS`). A transform used
as an attack is a prompt-injection rewrite with that transform as its layer. The rewrite pack
(:data:`REWRITES`) states its style in the attacker's request, from a prompt file of its own
(:mod:`martian.redteam.methods.library`), and adds the probe's goal when the run file states one.
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
        request = self.request
        if request is not None and context.probe.goal:
            request += f"\nThe goal of the attack: {context.probe.goal}"
        payload = await context.generate(request=request, method=self.style)
        return await context.send(payload, session=context.conversation())


REWRITES: dict[str, tuple[str, str, str]] = {
    "authority": ("Authority", "Authority", "rewrites/authority.md"),
    "hypothetical": ("Hypothetical", "Hypothetical", "rewrites/hypothetical.md"),
    "citation": ("Citation", "Citation", "rewrites/citation.md"),
    "task_framing": ("TaskFraming", "Task framing", "rewrites/task_framing.md"),
    "format_shift": ("FormatShift", "Format shift", "rewrites/format_shift.md"),
}
"""The rewrite pack: each method's id, YAML name, label and prompt file."""


def rewrite_request(label: str, file: str) -> str:
    """The attacker's request for a rewrite of the pack: its style, in its prompt file's words."""
    from martian.redteam.methods.library import prompt

    return f"Attack style: {label}. {prompt(file)}"


__all__ = ["REWRITES", "Rewrite", "rewrite_request"]
