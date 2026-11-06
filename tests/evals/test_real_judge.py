"""MLflow's real built-in LLM judge (Guidelines) through a real pytest session and the plugin.

The model endpoint is a local OpenAI-compatible server, so MLflow's actual judge prompt
construction, request, response parsing and Feedback type are exercised; only the model's answer
is canned. No provider key or network is involved.
"""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .stub_backend import StubBackend
from .test_plugin import run_pytest

SAMPLE = (Path(__file__).parent / "sample_judge_test.py").read_text()


class JudgeServer:
    def __init__(self):
        self.requests = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.requests.append(body)
                english = "Bonjour" not in json.dumps(body["messages"])
                content = json.dumps(
                    {
                        "result": "yes" if english else "no",
                        "rationale": "The answer is in English." if english else "The answer is in French.",
                    }
                )
                data = json.dumps(
                    {
                        "id": "chatcmpl-1",
                        "object": "chat.completion",
                        "created": 0,
                        "model": body.get("model", "stub"),
                        "choices": [
                            {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
                        ],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    @property
    def base(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"


class RealJudge(unittest.TestCase):
    def run_with(self, answer, backend):
        with JudgeServer() as judge:
            result = run_pytest(
                "unused",
                backend,
                sample=SAMPLE,
                ANSWER=answer,
                OPENAI_API_KEY="stub-key",
                OPENAI_API_BASE=judge.base,
            )
            return result, judge.requests

    def test_a_passing_judge_is_recorded_with_its_rationale(self):
        with StubBackend() as backend:
            (code, out), requests = self.run_with("Hello, we open at nine.", backend)
            self.assertEqual(code, 0, out)
            self.assertEqual(len(requests), 1, "the real judge made one model call")
            score = backend.sessions()[0][3]["tests"][0]["rows"][0]["scores"]["is_english"]
            self.assertEqual(score, {"value": True, "rationale": "The answer is in English."})

    def test_a_failing_judge_fails_the_job_and_keeps_its_rationale(self):
        with StubBackend() as backend:
            (code, out), _ = self.run_with("Bonjour, nous ouvrons a neuf heures.", backend)
            self.assertEqual(code, 1, out)
            score = backend.sessions()[0][3]["tests"][0]["rows"][0]["scores"]["is_english"]
            self.assertEqual(score, {"value": False, "rationale": "The answer is in French."})
            self.assertIn("failing check", out)


if __name__ == "__main__":
    unittest.main()
