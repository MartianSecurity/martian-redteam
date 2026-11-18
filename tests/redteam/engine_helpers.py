"""A stub red team service and counting targets for the engine's tests. Nothing here calls a model."""

from __future__ import annotations

import asyncio
from typing import Any

from martian._privacy import RedactionPolicy
from martian.redteam.runfile import RunFile
from martian.redteam.targets import TargetReply
from martian_redteam.types import (
    EvaluatorContext,
    EvaluatorOutput,
    ModelUsage,
    SimulatorContext,
    SimulatorOutput,
)

PURPOSE = "Help desk for a shop. It answers questions about orders."


class StubService:
    """Writes ``ATTACK <vulnerability>/<type>/<method>`` and judges a reply vulnerable when it holds
    ``LEAKED``. Each call reports 100 input and 20 output tokens, costing 0.01 USD."""

    def __init__(self, *, attack: str | None = None) -> None:
        self.simulated: list[tuple[SimulatorContext, str | None]] = []
        self.evaluated: list[dict[str, Any]] = []
        self.attack = attack
        self.usage = ModelUsage(input_tokens=100, output_tokens=20, cost_usd=0.01)

    @property
    def redaction_policy(self) -> RedactionPolicy:
        return RedactionPolicy()

    async def simulate(self, context: SimulatorContext, *, prompt: str | None = None) -> SimulatorOutput:
        self.simulated.append((context, prompt))
        attack = self.attack or f"ATTACK {context.vulnerability}/{context.vulnerability_type}/{context.attack_method}"
        return SimulatorOutput(attack=attack, attack_method=context.attack_method, usage=self.usage)

    async def evaluate(
        self, context: EvaluatorContext, *, attack: str, target_output: str, tools_called: list[str] | None = None
    ) -> EvaluatorOutput:
        self.evaluated.append({"context": context, "attack": attack, "reply": target_output})
        leaked = "LEAKED" in target_output
        return EvaluatorOutput(
            score=0 if leaked else 1, reason="It leaked." if leaked else "It held.", usage=self.usage
        )


class CountingTarget:
    """A target that counts its calls and answers with ``reply`` (a string, or a function of the message)."""

    def __init__(self, reply: Any = "No.", *, tool_calls: list | None = None, observed: bool = False) -> None:
        self.reply = reply
        self.calls: list[tuple[str, str | None]] = []
        self.tool_calls = tool_calls or []
        self.observed = observed
        self.delay = 0.0
        self.name = "counting"

    @property
    def kind(self) -> str:
        return "callback"

    async def send(self, message: str, *, session: str | None = None) -> TargetReply:
        self.calls.append((message, session))
        if self.delay:
            await asyncio.sleep(self.delay)
        text = self.reply(message) if callable(self.reply) else self.reply
        return TargetReply(text=text, tool_calls=self.tool_calls, tool_calls_observed=self.observed, session=session)

    async def close(self) -> None:
        return None


def run_file(**fields: Any) -> RunFile:
    base: dict[str, Any] = {
        "version": 2,
        "target": {"purpose": PURPOSE, "file": "agent.py"},
        "vulnerabilities": [{"name": "PromptLeakage", "types": ["system_prompt", "instructions"]}],
        "attacks": [{"name": "PromptInjection"}],
        # The engine's own tests count calls exactly; the negative control has its own tests.
        "verification": {"control": False},
    }
    base.update(fields)
    return RunFile.model_validate(base)
