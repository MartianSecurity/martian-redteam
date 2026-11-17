"""A loopback OpenAI-compatible server that plays the red team attacker and judge.

It tells the two apart by their system prompts (:mod:`martian.redteam.catalog.prompts`). By default
the attacker writes ``ATTACK <vulnerability>/<type> via <method>`` and the judge scores 0 (vulnerable)
when the target's reply holds ``LEAKED`` and 1 otherwise. ``attacker`` and ``judge`` replace either
with a function of the request's messages that returns ``(status, reply)``: a reply that is a dict is
sent as the JSON content, a string as it is. Every request is kept in ``requests``."""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

KEY = "fixture-attacker-key-0123456789"

Answer = Callable[[list[dict[str, Any]]], tuple[int, Any]]


def default_attacker(messages: list[dict[str, Any]]) -> tuple[int, Any]:
    system = messages[0]["content"]
    vulnerability = re.search(r"Vulnerability: (\S+) \((\S+)\)", system)
    method = re.search(r"Attack method: (\S+)", system)
    assert vulnerability and method
    attack = f"ATTACK {vulnerability.group(1)}/{vulnerability.group(2)} via {method.group(1)}"
    return 200, {"attack": attack, "attack_method": method.group(1)}


def field(message: str, name: str) -> str:
    """The data field ``name`` of a judge's message, found by its markers."""
    found = re.search(rf"<<<{name} ([0-9a-f]+)>>>\n(.*)\n<<<END {name} \1>>>", message, re.DOTALL)
    assert found, message
    return found.group(2)


def default_judge(messages: list[dict[str, Any]]) -> tuple[int, Any]:
    leaked = "LEAKED" in field(messages[1]["content"], "TARGET_REPLY")
    return 200, {"score": 0 if leaked else 1, "reason": "It leaked." if leaked else "It held."}


class FakeModels:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.attacker: Answer = default_attacker
        self.judge: Answer = default_judge
        self.usage = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
        self.lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with owner.lock:
                    owner.requests.append({"headers": dict(self.headers), "body": body})
                status, payload = owner.answer(self.headers, body)
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    @property
    def port(self) -> int:
        return self.server.server_port

    def calls(self, role: str) -> list[dict[str, Any]]:
        """The requests the attacker (``attacker``) or the judge (``judge``) received."""
        start = "You generate one red-team" if role == "attacker" else "You judge whether"
        return [item for item in self.requests if item["body"]["messages"][0]["content"].startswith(start)]

    def answer(self, headers: Any, body: dict[str, Any]) -> tuple[int, Any]:
        if headers.get("Authorization") != f"Bearer {KEY}":
            return 401, {"error": {"message": "Invalid API key"}}
        messages = body["messages"]
        role = "attacker" if messages[0]["content"].startswith("You generate one red-team") else "judge"
        status, reply = (self.attacker if role == "attacker" else self.judge)(messages)
        if status != 200:
            return status, {"error": {"message": "private provider text"}}
        content = reply if isinstance(reply, str) else json.dumps(reply)
        return 200, {
            "id": "chatcmpl-fake",
            "object": "chat.completion",
            "created": 0,
            "model": body["model"],
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": self.usage,
        }

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
