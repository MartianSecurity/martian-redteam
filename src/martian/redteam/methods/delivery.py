"""Indirect delivery routes: how an attack reaches the agent other than as its user message.

A method's ``delivery`` (the run file's :class:`~martian.redteam.runfile.Delivery`) applies to every
attack it sends, after its layers:

- ``variable``: the attack fills ``{{payload}}`` in the route's ``template``, and that is the user
  message, as an inject variable fills one field of a prompt.
- ``tool_result``: the user message is the ``template``, and the recording proxy adds the attack to
  the results of the named server's tools for that conversation only
  (:meth:`martian.redteam.proxy.ToolProxy.stage`). It is delivered when the agent called one.
- ``web_page``: a page on a loopback server (:class:`PageServer`) holds the attack, shown in the page
  or ``hidden`` in it (an HTML comment and an element that is not displayed), and the user message is
  the ``template`` with the page's address at ``{{url}}``. It is delivered when the agent fetched it.

Each query records its delivery as ``payload`` evidence: the route, the attack and whether it reached
what the agent read. The engine starts the page server with the run when a method uses the
``web_page`` route, and stops it when the run ends, however it ends.
"""

from __future__ import annotations

import asyncio
import html
import secrets
from dataclasses import dataclass, field
from typing import Any

from martian.redteam.engine.hostile import loopback_host

PAGE_TITLE = "Store notice"
VISIBLE_TEXT = "Opening hours are 9 to 5 on weekdays. Orders ship within two days."
"""What every page shows besides the attack, so a hidden attack sits in an ordinary page."""


def page_html(payload: str, *, hidden: bool) -> str:
    """The page that carries ``payload``: in a paragraph, or hidden in a comment and in an element
    that is not displayed."""
    text = html.escape(payload)
    if hidden:
        comment = payload.replace("--", "- -")
        carried = f'<!-- {comment} -->\n<div style="display:none" aria-hidden="true">{text}</div>'
    else:
        carried = f"<p>{text}</p>"
    return (
        f'<!doctype html>\n<html><head><meta charset="utf-8"><title>{PAGE_TITLE}</title></head>\n'
        f"<body><h1>{PAGE_TITLE}</h1>\n<p>{VISIBLE_TEXT}</p>\n{carried}\n</body></html>\n"
    )


@dataclass
class _Page:
    body: bytes
    fetched: int = 0


@dataclass
class PageServer:
    """Serves each attack's page at ``/p/<token>`` on 127.0.0.1, on a port the system picks, and
    counts each page's fetches. Any other path is ``404``. Only ``GET`` and ``HEAD`` are answered."""

    host: str = "127.0.0.1"
    _pages: dict[str, _Page] = field(default_factory=dict)
    _server: asyncio.Server | None = None
    port: int = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, loopback_host(self.host), 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    @property
    def running(self) -> bool:
        return self._server is not None

    def publish(self, payload: str, *, hidden: bool) -> tuple[str, str]:
        """A new page holding ``payload``: its token and its address."""
        if self._server is None:
            raise RuntimeError("the page server is not running")
        token = secrets.token_urlsafe(12)
        self._pages[token] = _Page(page_html(payload, hidden=hidden).encode())
        return token, f"http://{self.host}:{self.port}/p/{token}"

    def fetched(self, token: str) -> int:
        page = self._pages.get(token)
        return page.fetched if page is not None else 0

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            method, path, *_ = head.split(b"\r\n", 1)[0].decode("latin-1").split(" ")
            page = self._pages.get(path.removeprefix("/p/")) if path.startswith("/p/") else None
            if method not in ("GET", "HEAD") or page is None:
                status, body = "404 Not Found", b"Not found\n"
            else:
                page.fetched += 1
                status, body = "200 OK", page.body
            content = b"" if method == "HEAD" else body
            writer.write(
                f"HTTP/1.1 {status}\r\nContent-Type: text/html; charset=utf-8\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                + content
            )
            await writer.drain()
        except (TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError, ConnectionError):
            pass
        finally:
            writer.close()


@dataclass
class Prepared:
    """One attack ready to send: the user message, and what checking its delivery needs."""

    message: str
    route: str
    details: dict[str, Any] = field(default_factory=dict)


class Deliverer:
    """The routes of one run: the proxy for ``tool_result`` and the page server for ``web_page``."""

    def __init__(self, *, proxy: Any = None, pages: PageServer | None = None) -> None:
        self.proxy = proxy
        self.pages = pages

    def prepare(self, delivery: Any, payload: str, *, action_id: str) -> Prepared:
        route = delivery.route
        if route == "variable":
            return Prepared(delivery.template.replace("{{payload}}", payload), route)
        if route == "tool_result":
            if self.proxy is None:
                raise RuntimeError("the tool_result route needs the target behind tools.proxy")
            self.proxy.stage(action_id, delivery.server, payload)
            return Prepared(delivery.template, route, {"server": delivery.server, "action_id": action_id})
        if route == "web_page":
            if self.pages is None or not self.pages.running:
                raise RuntimeError("the web_page route needs the run's page server")
            token, url = self.pages.publish(payload, hidden=delivery.hidden)
            return Prepared(
                delivery.template.replace("{{url}}", url),
                route,
                {"url": url, "token": token, "hidden": delivery.hidden},
            )
        return Prepared(payload, route)

    def confirm(self, prepared: Prepared, payload: str) -> dict[str, Any]:
        """Whether the attack reached what the agent read, with the route's details."""
        details = {key: value for key, value in prepared.details.items() if key != "token"}
        if prepared.route == "tool_result":
            server, action = prepared.details["server"], prepared.details["action_id"]
            self.proxy.unstage(action, server)
            calls = [
                call
                for call in self.proxy.calls(action_id=action)
                if call.server == server and call.injected == payload
            ]
            return {**details, "delivered": bool(calls), "tool_calls": [call.call_id for call in calls]}
        if prepared.route == "web_page":
            assert self.pages is not None
            fetched = self.pages.fetched(prepared.details["token"])
            return {**details, "delivered": fetched > 0, "fetches": fetched}
        return {**details, "delivered": True}

    def release(self, prepared: Prepared) -> None:
        """Undo what ``prepare`` staged when the target call did not complete."""
        if prepared.route == "tool_result" and self.proxy is not None:
            self.proxy.unstage(prepared.details["action_id"], prepared.details["server"])


__all__ = ["PAGE_TITLE", "VISIBLE_TEXT", "Deliverer", "PageServer", "Prepared", "page_html"]
