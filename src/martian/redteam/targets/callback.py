"""The version 0.2 callback, and a function in a Python file, as a :class:`~martian.redteam.targets.Target`.

:class:`CallbackTarget` adapts a callback as ``red_team()`` takes it (:mod:`martian_redteam.callback`):
sync or async, returning ``str`` or a ``Turn``. A ``Turn`` reports the tools the target called, by name,
so its reply's ``tool_calls_observed`` is true; a ``str`` reports none, so it is false and a tool-call
check cannot be resisted on it. :func:`function_target` loads the run file's ``file`` and ``function``.

A callback keeps no conversation of its own: each ``send`` is one call, and ``session`` is passed back
on the reply.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import Executor
from pathlib import Path
from typing import Any

from martian.redteam.model import ToolCall
from martian.redteam.targets import TargetReply
from martian_redteam.callback import TargetCallback, invoke_callback
from martian_redteam.types import Turn


class CallbackTarget:
    """A callback under attack. ``kind`` is ``callback``, or ``function`` for one loaded from a file."""

    def __init__(
        self,
        callback: TargetCallback,
        *,
        kind: str = "callback",
        name: str = "",
        executor: Executor | None = None,
    ) -> None:
        self._kind = kind
        self.name = name or getattr(callback, "__name__", "callback")
        self._invoke = invoke_callback(callback, _executor=executor)

    @property
    def kind(self) -> str:
        return self._kind

    async def send(self, message: str, *, session: str | None = None) -> TargetReply:
        started = time.perf_counter()
        result: Any = await self._invoke(message, None)
        latency = (time.perf_counter() - started) * 1000
        if isinstance(result, Turn):
            calls = [ToolCall(name=name) for name in result.tools_called]
            return TargetReply(
                text=result.content, tool_calls=calls, tool_calls_observed=True, session=session, latency_ms=latency
            )
        if isinstance(result, str):
            return TargetReply(text=result, session=session, latency_ms=latency)
        raise TypeError(f"target callback must return str or Turn, got {type(result).__name__}")

    async def close(self) -> None:
        return None


def function_target(file: str | Path, function: str = "target", *, executor: Executor | None = None) -> CallbackTarget:
    """The function ``function`` of the Python file ``file``, loaded once, as a target."""
    from martian_redteam.runner import _load_callback

    callback: Callable[..., Any] = _load_callback(str(file), function)
    return CallbackTarget(callback, kind="function", name=f"{Path(file).name}:{function}", executor=executor)


__all__ = ["CallbackTarget", "function_target"]
