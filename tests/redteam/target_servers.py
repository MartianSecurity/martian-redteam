"""Loopback servers the red team target tests attack. Nothing here reaches another machine."""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

Handler = Callable[[dict[str, Any], dict[str, str]], tuple[int, Any]]


class Recorded:
    """What a server received: each request's body and headers, in order."""

    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        self.lock = threading.Lock()


@contextmanager
def json_server(handle: Handler) -> Iterator[tuple[str, Recorded]]:
    """A JSON endpoint on 127.0.0.1 that answers each POST and PUT with ``handle(body, headers)``. The
    headers hold the request's path too, under ``:path``."""
    recorded = Recorded()

    class Endpoint(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - the standard library's name
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            headers = {name.lower(): value for name, value in self.headers.items()} | {":path": self.path}
            with recorded.lock:
                recorded.bodies.append(body)
                recorded.headers.append(headers)
            status, answer = handle(body, headers)
            data = answer.encode() if isinstance(answer, str) else json.dumps(answer).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_PUT = do_POST  # noqa: N815 - the standard library's name

        def log_message(self, *_: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Endpoint)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/chat", recorded
    finally:
        server.shutdown()
        server.server_close()


class StatefulChat:
    """A chat server that keeps a turn count per conversation id. The id is the body's ``session``,
    else the ``x-session-id`` header; with ``issues_ids`` an empty id gets a new one, returned in the
    reply's ``session``."""

    def __init__(self, *, issues_ids: bool = False) -> None:
        self.issues_ids = issues_ids
        self.turns: dict[str, int] = {}
        self.lock = threading.Lock()

    def __call__(self, body: dict[str, Any], headers: dict[str, str]) -> tuple[int, Any]:
        session = str(body.get("session") or headers.get("x-session-id") or "")
        if not session:
            if not self.issues_ids:
                return 400, {"error": "no session"}
            session = f"srv-{uuid.uuid4().hex[:8]}"
        with self.lock:
            self.turns[session] = self.turns.get(session, 0) + 1
            turn = self.turns[session]
        messages = body.get("messages")
        return 200, {
            "reply": f"turn {turn}",
            "session": session,
            "seen": len(messages) if isinstance(messages, list) else None,
        }
