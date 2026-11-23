"""The version 0.2 callback, and a function in a Python file, as a :class:`~martian.redteam.targets.Target`.

:class:`CallbackTarget` adapts a callback as ``red_team()`` takes it (:mod:`martian_redteam.callback`):
sync or async, returning ``str`` or a ``Turn``. A ``Turn`` reports the tools the target called: with
their arguments and whether they ran when it has ``tool_calls``, else by name. So its reply's
``tool_calls_observed`` is true; a ``str`` reports none, so it is false and a tool-call check cannot
be resisted on it. :func:`function_target` loads the run file's ``file`` and ``function``.

A callback may also return the reply contract's object, ``{"text": ..., "tool_calls": [...]}`` and no
other key (:func:`~martian.redteam.targets.reply.reply_from`), as a suite file's function target does:
its calls are read with their arguments and whether they ran.

Each ``send`` is one call. The target keeps each session's conversation
(:class:`~martian.redteam.targets.conversations.Conversations`), as the other targets do, and names it
on the reply. A callback that takes a ``session_id`` parameter is given the conversation's id
(:func:`~martian.redteam.targets.conversations.conversation_id` of the session), the id it binds its
tools with as the action id, so the recording proxy or an environment's gateway attributes its calls
to the attempt. One that takes ``history`` is given the conversation's earlier turns, as ``user`` and
``assistant`` messages.
"""

from __future__ import annotations

import inspect
import time
from collections.abc import Callable, Mapping
from concurrent.futures import Executor
from functools import partial
from pathlib import Path
from typing import Any

from martian.evals.conversations import HISTORY, SESSION_ID
from martian.redteam.targets import TargetReply
from martian.redteam.targets.conversations import Conversations
from martian.redteam.targets.reply import REPLY_KEYS, reply_from
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
        self._callback, self._executor = callback, executor
        self._invoke = invoke_callback(callback, _executor=executor)
        try:
            parameters = inspect.signature(callback).parameters
        except (TypeError, ValueError):
            parameters = {}  # type: ignore[assignment]
        self._takes = tuple(name for name in (SESSION_ID, HISTORY) if name in parameters)
        self._conversations = Conversations()

    @property
    def kind(self) -> str:
        return self._kind

    async def send(self, message: str, *, session: str | None = None) -> TargetReply:
        async with self._conversations.turn(session) as conversation:
            invoke = self._invoke
            if self._takes:
                given = {SESSION_ID: conversation.id, HISTORY: list(conversation.history)}
                named = partial(self._callback, **{name: given[name] for name in self._takes})
                invoke = invoke_callback(named, _executor=self._executor)
            started = time.perf_counter()
            result: Any = await invoke(message, None)
            latency = (time.perf_counter() - started) * 1000
            if not (
                isinstance(result, str | Turn)
                or (isinstance(result, Mapping) and isinstance(result.get("text"), str) and set(result) <= REPLY_KEYS)
            ):
                raise TypeError(
                    'target callback must return str, Turn or {"text": ..., "tool_calls": [...]}, '
                    f"got {type(result).__name__}"
                )
            reply = reply_from(result, session=conversation.session, latency_ms=latency)
            conversation.answered(message, reply.text)
            return reply

    def end(self, session: str) -> None:
        """Forget ``session``'s conversation. Sending with it again starts a new one."""
        self._conversations.end(session)

    async def close(self) -> None:
        self._conversations.clear()


def function_target(file: str | Path, function: str = "target", *, executor: Executor | None = None) -> CallbackTarget:
    """The function ``function`` of the Python file ``file``, loaded once, as a target."""
    from martian_redteam.runner import _load_callback

    callback: Callable[..., Any] = _load_callback(str(file), function)
    return CallbackTarget(callback, kind="function", name=f"{Path(file).name}:{function}", executor=executor)


__all__ = ["CallbackTarget", "function_target"]
