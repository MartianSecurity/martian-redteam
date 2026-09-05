"""Built-in attack ids. Encodings run locally; LLM attacks use the simulator."""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, ConfigDict


class Attack(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    label: str
    kind: Literal["encoding", "llm"]
    weight: float = 1.0


def PromptInjection(*, weight: float = 1.0) -> Attack:
    return Attack(id="prompt_injection", label="Prompt injection", kind="llm", weight=weight)


def ROT13(*, weight: float = 1.0) -> Attack:
    return Attack(id="rot13", label="ROT13", kind="encoding", weight=weight)


def Base64(*, weight: float = 1.0) -> Attack:
    return Attack(id="base64", label="Base64", kind="encoding", weight=weight)


def Leetspeak(*, weight: float = 1.0) -> Attack:
    return Attack(id="leetspeak", label="Leetspeak", kind="encoding", weight=weight)


ATTACKS: dict[str, Callable[..., Attack]] = {
    "PromptInjection": PromptInjection,
    "ROT13": ROT13,
    "Base64": Base64,
    "Leetspeak": Leetspeak,
}


def get_attack(name: str, *, weight: float | None = None) -> Attack:
    factory = ATTACKS.get(name)
    if factory is None:
        raise KeyError(f"unknown attack: {name}")
    if weight is None:
        return factory()
    return factory(weight=weight)
