"""A local edition with a dataset to run and an HTTP target to call, for the local run tests.

:class:`FakeTarget` is the target: an HTTP endpoint on loopback, started by the test, that answers
each JSON ``POST`` as the write route tests' agent does (``write_fixture.agent``) and refuses a
request without the key it was given. It keeps every request it got. Nothing calls a model.
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from martian.evals.local import open_stores
from martian.evals.model import Case
from martian.evals.server import DEFAULT_PROJECT, SINGLE_USER
from tests.evals.write_fixture import ANSWERS, CASES

DATASET = "support-faq"
SUITE_SCORERS = ["contains_expected", "response_length"]


class FakeTarget:
    """An HTTP target on 127.0.0.1 that wants ``key`` as ``Authorization: Bearer <key>``."""

    def __init__(self, key: str) -> None:
        self.key = key
        self.requests: list[dict[str, Any]] = []
        target = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - the name http.server calls
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                target.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                if self.headers.get("Authorization") != f"Bearer {target.key}":
                    self.send_response(401)
                    self.end_headers()
                    return
                answer = ANSWERS.get(body.get("message", ""), "I am not sure.")
                data = json.dumps({"reply": {"text": answer}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - http.server's name
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}/chat"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> FakeTarget:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(10)

    def definition(self, name: str = "Support agent") -> dict[str, Any]:
        """The target as the UI defines it, with its key."""
        return {
            "name": name,
            "kind": "http",
            "url": self.url,
            "body": {"message": "{{input}}"},
            "response_path": "reply.text",
            "key": self.key,
        }


def publish(home: Path, cases: list[Case] = CASES) -> str:
    """Publish ``cases`` as a version of the dataset in the data folder ``home``; its id."""
    store, records = open_stores(home)
    try:
        version = asyncio.run(store.create_dataset(SINGLE_USER, DEFAULT_PROJECT, DATASET, cases))
    finally:
        records.close()
    return version.id


def save_suite(client: TestClient, request_id: str, scorers: list[str] = SUITE_SCORERS, **changes: Any) -> Any:
    """Save a suite of built-in scorers in the UI's request; the saved version."""
    body = {"request_id": request_id, "name": "Deterministic checks", "scorers": scorers, **changes}
    response = client.post("/v1/evals/suites", json=body)
    assert response.status_code == 201, response.text
    return response.json()
