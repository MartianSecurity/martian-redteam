"""The conversations a target keeps: one per session, each with its id and its history.

A red team attempt is one conversation. The engine starts it by sending with ``session=None`` (or a
session name the target has not seen) and continues it by sending with the session the first reply
named. A new session is the reset signal: it has a new id and an empty history, so a server that
keys its state on the id starts fresh. Every target kind keeps its conversations here:

- ``id`` is what the target sends as the conversation's id: the session name itself, or, for a
  server that issues its own ids, empty until its first reply names one.
- ``history`` holds the earlier turns as ``user`` and ``assistant`` messages, the shape the suite
  file's ``{{history}}`` and ``{{messages}}`` placeholders and a function target's ``history``
  parameter take. Whether the target resends it or sends only the newest turn is the target's
  setting: an HTTP body with ``{{messages}}`` or ``{{history}}`` resends it, one with only
  ``{{input}}`` does not.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from martian.evals.conversations import assistant, user


@dataclass
class Conversation:
    """One session's state. ``turns`` counts the messages the target answered."""

    session: str
    id: str
    history: list[dict[str, str]] = field(default_factory=list)
    turns: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @property
    def new(self) -> bool:
        return self.turns == 0

    def answered(self, message: str, reply: str) -> None:
        self.history += [user(message), assistant(reply)]
        self.turns += 1


class Conversations:
    """A target's sessions by name. ``issued_by_server``: the server issues each conversation's id,
    so a new one starts with an empty id and takes the one the first reply names."""

    def __init__(self, *, issued_by_server: bool = False) -> None:
        self._issued_by_server = issued_by_server
        self._open: dict[str, Conversation] = {}

    @asynccontextmanager
    async def turn(self, session: str | None) -> AsyncIterator[Conversation]:
        """The conversation of ``session``, new when it is ``None`` or not open, held for one turn so
        that two turns of one conversation never interleave."""
        name = session or str(uuid.uuid4())
        conversation = self._open.get(name)
        if conversation is None:
            conversation = Conversation(session=name, id="" if self._issued_by_server else name)
            self._open[name] = conversation
        async with conversation.lock:
            yield conversation

    def end(self, session: str) -> None:
        """Forget ``session``. Sending with it again starts a new conversation."""
        self._open.pop(session, None)

    def clear(self) -> None:
        self._open.clear()


__all__ = ["Conversation", "Conversations"]
