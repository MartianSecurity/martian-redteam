"""Change events: how a live page learns that a run, result or review changed.

An event names what changed, never its content; a listener reads the record again. The server
delivers events between processes (it uses PostgreSQL NOTIFY today); ``InProcessEvents`` delivers
them inside one process, which is all the local edition needs. A listener only hears its own
organisation's events.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from martian.evals.server.principal import Principal

EventKind = Literal["run", "case_results", "scores", "scoring_job", "review", "suite", "draft", "view", "monitor"]


class Event(BaseModel):
    """``id`` names the record: a run's id for ``run``, ``case_results`` and ``scores``, and the
    record's own id for the other kinds. A ``scoring_job`` event also names its ``run_id``, which
    reading the job takes, so a listener follows a scoring job's progress as it follows a run's."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: EventKind
    project_id: str
    id: str
    run_id: str | None = None


class EventsLost(Exception):
    """The listener fell too far behind and events were dropped; read everything again."""


def space_of(principal: Principal) -> str:
    """The key that separates organisations; the single local user has a space of its own."""
    return principal.organisation_id or "~local"


@runtime_checkable
class EventBus(Protocol):
    def publish(self, space: str, event: Event) -> None: ...

    def subscribe(self, principal: Principal) -> Subscription: ...


class Subscription:
    """Events for one organisation, from the moment of subscribing. Iterate, and close when done."""

    def __init__(self, bus: InProcessEvents, space: str, size: int) -> None:
        self._bus = bus
        self._space = space
        self._loop = asyncio.get_running_loop()
        self._queue: asyncio.Queue[Event | None] = asyncio.Queue(maxsize=size)
        self._lost = False

    def _deliver(self, event: Event) -> None:
        if self._lost:
            return
        if self._queue.full():
            self._lost = True
            self._queue.get_nowait()
            self._queue.put_nowait(None)
            return
        self._queue.put_nowait(event)

    def offer(self, event: Event) -> None:
        self._loop.call_soon_threadsafe(self._deliver, event)

    async def next(self, timeout: float | None = None) -> Event:
        item = await asyncio.wait_for(self._queue.get(), timeout)
        if item is None:
            raise EventsLost("events were dropped; read everything again")
        return item

    def __aiter__(self) -> AsyncIterator[Event]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[Event]:
        while True:
            yield await self.next()

    def close(self) -> None:
        self._bus._remove(self._space, self)


class InProcessEvents:
    """Events within one process. A listener that falls ``queue_size`` events behind gets
    ``EventsLost`` instead of an unbounded backlog."""

    def __init__(self, queue_size: int = 1000) -> None:
        self._queue_size = queue_size
        self._subscriptions: dict[str, set[Subscription]] = {}

    def publish(self, space: str, event: Event) -> None:
        for subscription in list(self._subscriptions.get(space, ())):
            subscription.offer(event)

    def subscribe(self, principal: Principal) -> Subscription:
        space = space_of(principal)
        subscription = Subscription(self, space, self._queue_size)
        self._subscriptions.setdefault(space, set()).add(subscription)
        return subscription

    def _remove(self, space: str, subscription: Subscription) -> None:
        self._subscriptions.get(space, set()).discard(subscription)
