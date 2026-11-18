"""Wrap customer target callbacks into a uniform async Turn producer."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Sequence
from concurrent.futures import Executor
from contextvars import copy_context
from functools import partial
from typing import Any

from martian_redteam.types import Turn

TargetCallback = Callable[..., str | Turn | Awaitable[str | Turn]]
AsyncTarget = Callable[[str, Sequence[Turn] | None], Awaitable[Turn]]
RawTarget = Callable[[str, Sequence[Turn] | None], Awaitable[str | Turn]]


def invoke_callback(callback: TargetCallback, *, _executor: Executor | None = None) -> RawTarget:
    """Call a sync or async callback, with ``turns`` when it declares that parameter, and return
    what it returned (``str`` or ``Turn``) without converting it."""

    try:
        parameters = inspect.signature(callback).parameters
        accepts_turns = "turns" in parameters
    except (TypeError, ValueError):
        accepts_turns = False

    async_callable = inspect.iscoroutinefunction(callback) or inspect.iscoroutinefunction(type(callback).__call__)

    async def invoked(prompt: str, turns: Sequence[Turn] | None = None) -> str | Turn:
        args = (prompt, turns) if accepts_turns else (prompt,)
        # Blocking callbacks must yield the loop so its target deadline can actually fire.
        result: Any
        if async_callable:
            result = callback(*args)
        elif _executor is None:
            result = await asyncio.to_thread(callback, *args)
        else:
            call = partial(copy_context().run, callback, *args)
            result = await asyncio.get_running_loop().run_in_executor(_executor, call)
        if inspect.isawaitable(result):
            result = await result
        return result  # type: ignore[no-any-return]

    return invoked


def wrap_callback(callback: TargetCallback, *, _executor: Executor | None = None) -> AsyncTarget:
    """Accept str or Turn, sync or async, with optional turns.

    Phase 1 probes pass ``turns=None``. Callables that declare a ``turns``
    parameter receive it; everyone else is called with the prompt only.
    """
    invoked = invoke_callback(callback, _executor=_executor)

    async def wrapped(prompt: str, turns: Sequence[Turn] | None = None) -> Turn:
        result: Any = await invoked(prompt, turns)
        if isinstance(result, Turn):
            return result
        if isinstance(result, str):
            return Turn(role="ai", content=result)
        raise TypeError(f"target callback must return str or Turn, got {type(result).__name__}")

    return wrapped
