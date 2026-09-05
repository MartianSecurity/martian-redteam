"""Wrap customer target callbacks into a uniform async Turn producer."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from martian_redteam.types import Turn

TargetCallback = Callable[..., str | Turn | Awaitable[str | Turn]]
AsyncTarget = Callable[[str, Sequence[Turn] | None], Awaitable[Turn]]


def wrap_callback(callback: TargetCallback) -> AsyncTarget:
    """Accept str or Turn, sync or async, with optional turns."""

    async def wrapped(prompt: str, turns: Sequence[Turn] | None = None) -> Turn:
        result: Any
        try:
            signature = inspect.signature(callback)
            result = callback(prompt, turns) if "turns" in signature.parameters else callback(prompt)
        except TypeError:
            result = callback(prompt)
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, Turn):
            return result
        if isinstance(result, str):
            return Turn(role="ai", content=result)
        raise TypeError(f"target callback must return str or Turn, got {type(result).__name__}")

    return wrapped
