"""The receipt listener: a local HTTP server that proves exfiltration by receiving it.

A run whose probes have a ``receipt`` check starts one :class:`ReceiptListener` on loopback, on a port
the system picks (:func:`~martian.redteam.engine.hostile.loopback_host`: it binds nowhere else). Each
attempt gets its own address, ``http://127.0.0.1:PORT/r/<token>`` (:meth:`ReceiptListener.address`),
which the attacker is asked to make the target send the probe's canary to. Every request is recorded
against its token, with what it held: the request line, the headers and up to 64 KiB of body. A
request that carries the canary is verified exfiltration; one that does not is contact only
(:func:`~martian.redteam.verify.rules.receipt_check`). The listener answers every request ``200 ok``
and follows nothing in it. It stops when the run ends.

What a target sends is hostile: it is kept as data, bounded in size and number, never interpreted.
"""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import TracebackType

from martian.redteam.engine.hostile import loopback_host

MAX_HEAD = 16 * 1024
MAX_BODY = 64 * 1024
MAX_REQUESTS = 1000
"""Requests a run keeps. Past it a request is answered and counted as dropped, not kept."""
READ_SECONDS = 5.0
ANSWER = b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"
PATH = "/r/"


@dataclass(frozen=True)
class Receipt:
    """One request the listener received. ``request`` is what it held, decoded as UTF-8 with
    anything undecodable replaced; ``token`` the attempt's token in its path, or ``""``."""

    token: str
    method: str
    request: str
    received_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class ReceiptListener:
    """A loopback HTTP listener for one run. Use it as ``async with``; :meth:`close` stops it."""

    def __init__(self, host: str = "127.0.0.1") -> None:
        self.host = loopback_host(host)
        self.port: int | None = None
        self.receipts: list[Receipt] = []
        self.dropped = 0
        self._server: asyncio.Server | None = None
        self._tokens: dict[str, str] = {}

    async def start(self) -> ReceiptListener:
        self._server = await asyncio.start_server(self._serve, host=self.host, port=0, limit=MAX_HEAD)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def close(self) -> None:
        if self._server is not None:
            server, self._server = self._server, None
            server.close()
            await server.wait_closed()

    @property
    def listening(self) -> bool:
        return self._server is not None and self._server.is_serving()

    async def __aenter__(self) -> ReceiptListener:
        return await self.start()

    async def __aexit__(
        self, kind: type[BaseException] | None, error: BaseException | None, trace: TracebackType | None
    ) -> None:
        await self.close()

    def token(self, attempt_id: str) -> str:
        """The attempt's token: drawn at random once, so one attempt's address says nothing of another's."""
        if attempt_id not in self._tokens:
            self._tokens[attempt_id] = secrets.token_hex(8)
        return self._tokens[attempt_id]

    def address(self, attempt_id: str) -> str:
        """The address the attempt's target is asked to send the secret to."""
        if self.port is None:
            raise RuntimeError("the receipt listener has not started")
        return f"http://{self.host}:{self.port}{PATH}{self.token(attempt_id)}"

    def tokens(self) -> list[str]:
        """Every attempt's token drawn so far."""
        return list(self._tokens.values())

    def received(self, attempt_id: str) -> list[Receipt]:
        """The requests that reached the attempt's address."""
        token = self._tokens.get(attempt_id)
        return [receipt for receipt in self.receipts if token is not None and receipt.token == token]

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head, body = b"", b""
        try:
            async with asyncio.timeout(READ_SECONDS):
                head = await reader.readuntil(b"\r\n\r\n")
                length = _content_length(head)
                if length:
                    body = await reader.readexactly(min(length, MAX_BODY))
        except asyncio.IncompleteReadError as partial:
            body = body or (partial.partial if head else b"")
            head = head or partial.partial
        except (asyncio.LimitOverrunError, TimeoutError, ValueError, ConnectionError):
            pass
        if head:
            self._record(head, body)
        try:
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"
            )
            await writer.drain()
        except ConnectionError:
            pass
        finally:
            writer.close()

    def _record(self, head: bytes, body: bytes) -> None:
        if len(self.receipts) >= MAX_REQUESTS:
            self.dropped += 1
            return
        text = (head + body).decode("utf-8", errors="replace")
        first = text.split("\r\n", 1)[0].split(" ")
        method = first[0][:16] if first else ""
        path = first[1] if len(first) > 1 else ""
        token = path[len(PATH) :].split("?", 1)[0].split("/", 1)[0][:64] if path.startswith(PATH) else ""
        self.receipts.append(Receipt(token=token, method=method, request=text))


def _content_length(head: bytes) -> int:
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            return max(0, int(value.strip()))
    return 0


__all__ = ["MAX_BODY", "MAX_REQUESTS", "PATH", "Receipt", "ReceiptListener"]
