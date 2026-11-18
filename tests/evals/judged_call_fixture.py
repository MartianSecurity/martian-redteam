"""A judged call at a loopback endpoint, for ``tests/evals/test_no_outbound.py``.

Run as ``python -m tests.evals.judged_call_fixture MODE``, it does what the worker does for a
scoring request that names an endpoint: it serves a loopback OpenAI-compatible endpoint that grades
every rubric as a pass and reports its token usage, as a provider does, and asks the SDK judge
(:func:`martian.evals.worker.sdk_judge`) for one verdict at that endpoint. ``MODE`` is ``open``
for a judge with no address policy, as a local run has, or ``pinned`` for one that connects only
to the loopback address, as the Martian server's judge does for an organisation's endpoint. It
prints one JSON line: the verdict, the tokens the judge counted, and how many requests the
endpoint received.

This module imports no MLflow before the core, as a server must not: MLflow decides when it is
imported whether its telemetry runs.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from martian.evals.server import SINGLE_USER
from martian.evals.worker import JudgeRequest, sdk_judge
from martian.evals.worker.scorers import builtin_scorer

KEY = "fixture-judge-key"


def answer(body: dict[str, Any]) -> dict[str, Any]:
    """A chat completion that passes every rubric the request names, with usage."""
    system = body["messages"][0]["content"]
    rubrics = json.loads(system.split("\nRubrics:\n", 1)[1])
    verdicts = [
        {"rubric_index": number, "reason": "Checked.", "verdict": "pass", "evidence_ids": []}
        for number, _ in enumerate(rubrics, 1)
    ]
    return {
        "id": "chatcmpl-fixture",
        "object": "chat.completion",
        "created": 0,
        "model": "your-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": json.dumps({"result": verdicts})},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def serve() -> tuple[ThreadingHTTPServer, list[str]]:
    """The endpoint, listening on a free loopback port, and the paths it was asked for."""
    received: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append(self.path)
            data = json.dumps(answer(body)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, received


def judged(mode: str) -> dict[str, Any]:
    """One judged call at a new loopback endpoint, and what came of it."""
    import os

    os.environ["OPENAI_API_KEY"] = KEY
    server, received = serve()
    try:
        policy = (lambda host, address: address == "127.0.0.1") if mode == "pinned" else None
        judge = sdk_judge(model="openai:/your-model", max_retries=0, address_policy=policy)
        request = JudgeRequest(
            principal=SINGLE_USER,
            prompt="How do I reset my password?",
            expected="Use the reset link.",
            output="Use the reset link.",
            scorers=(builtin_scorer("correctness"),),
            endpoint=f"http://127.0.0.1:{server.server_port}/v1",
        )
        result = asyncio.run(judge(request))
    finally:
        server.shutdown()
        server.server_close()
    return {"passed": result.verdicts["correctness"].value, "tokens": result.tokens, "received": received}


if __name__ == "__main__":
    print(json.dumps(judged(sys.argv[1])))
