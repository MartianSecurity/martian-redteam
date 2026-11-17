"""A Python function in a file as a red team target: the suite file's function target.

The function runs in a process of its own (:class:`martian.evals.isolation.TargetProcess`), as a
suite file's function target and the evals worker's do, so it cannot reach what the run keeps
private (``private``, such as the run's output folder) on Linux, and a function that hangs cannot hold
up the run. The process starts with the first message and ends with :meth:`FunctionTarget.close`.

Each message is sent as the keyword argument ``message``, the suite file's default input. A function
that takes ``session_id`` or ``history`` is also given the conversation's id and its earlier turns
(:mod:`martian.redteam.targets.conversations`), as a suite file's conversation cases give them. Its
return value must be JSON: text, or ``{"text": ..., "tool_calls": [...]}``, the reply contract of
:mod:`martian.redteam.targets.reply`. A function that
raises is a :class:`~martian.evals.runner.TargetCallError` naming the exception's type only; its
text may hold anything and is not kept.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Iterable
from pathlib import Path

from martian.evals.conversations import CONVERSATION_INPUTS, HISTORY, SESSION_ID
from martian.evals.isolation import TargetProcess, TargetRaised
from martian.evals.runner import TargetCallError, TargetRef
from martian.redteam.targets import TargetReply
from martian.redteam.targets.conversations import Conversations
from martian.redteam.targets.reply import reply_from

MESSAGE = "message"


class FunctionTarget:
    """``reference`` is the suite file's target block with a ``file``; a relative ``file`` is read
    from ``base``."""

    kind = "function"

    def __init__(self, reference: TargetRef, *, base: str | Path = ".", private: Iterable[str | Path] = ()) -> None:
        if reference.file is None:
            raise ValueError("a function target needs a file")
        file = Path(reference.file)
        self._file = file if file.is_absolute() else Path(base) / file
        self._function = reference.function
        self._timeout = reference.timeout_seconds
        self._private = list(private)
        self._process: TargetProcess | None = None
        self._takes: tuple[str, ...] = ()
        self._starting = asyncio.Lock()
        self._conversations = Conversations()

    async def _ready(self) -> TargetProcess:
        async with self._starting:
            if self._process is None:
                process = TargetProcess(self._file, self._function, private=self._private, trace=False)
                try:
                    await asyncio.to_thread(process.ready)
                    signature = await asyncio.to_thread(process.signature)
                except BaseException:
                    process.close()
                    raise
                self._takes = _conversation_parameters(signature)
                self._process = process
            return self._process

    async def send(self, message: str, *, session: str | None = None) -> TargetReply:
        process = await self._ready()
        async with self._conversations.turn(session) as conversation:
            given = {SESSION_ID: conversation.id, HISTORY: list(conversation.history)}
            inputs = {MESSAGE: message, **{name: given[name] for name in self._takes}}
            started = time.perf_counter()
            try:
                value, _ = await asyncio.to_thread(process.call, inputs, self._timeout)
            except TargetRaised as raised:
                raise TargetCallError(f"the target raised {raised}") from None
            latency = (time.perf_counter() - started) * 1000
            reply = reply_from(value, session=conversation.session, latency_ms=latency)
            conversation.answered(message, reply.text)
            return reply

    def end(self, session: str) -> None:
        self._conversations.end(session)

    async def close(self) -> None:
        self._conversations.clear()
        if self._process is not None:
            process, self._process = self._process, None
            await asyncio.to_thread(process.close)


def _conversation_parameters(signature: inspect.Signature) -> tuple[str, ...]:
    """Which of ``session_id`` and ``history`` the function takes. One with ``**kwargs`` takes both."""
    parameters = signature.parameters.values()
    if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters):
        return CONVERSATION_INPUTS
    named = {
        parameter.name
        for parameter in parameters
        if parameter.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    }
    return tuple(name for name in CONVERSATION_INPUTS if name in named)


__all__ = ["MESSAGE", "FunctionTarget"]
