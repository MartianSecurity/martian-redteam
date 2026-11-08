"""Exercise the judge's provider boundary through real HTTP and MLflow evaluation."""

import json
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import mlflow
import pytest
from mlflow.genai.judges.tools.base import JudgeTool
from mlflow.genai.judges.tools.registry import _judge_tool_registry, register_judge_tool

from martian.evals import GateFailure, assert_checks, evaluate, response_conditions, response_guidelines
from martian.evals.state import STATE

_CANARY = "private-synthetic-provider-text"
_GUIDELINE = {
    "guideline_index": 1,
    "reason": "The response greets the user.",
    "evidence_kind": "quote",
    "evidence_ids": [1],
    "status": "complied",
}
_CONDITION = {
    "condition_index": 1,
    "reason": "The response greets the user.",
    "evidence_ids": [1],
    "answer": "yes",
}


def _completion(content, *, tool_calls=None):
    message = {"role": "assistant", "content": content if isinstance(content, str) else json.dumps(content)}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return 200, {
        "id": "test-completion",
        "object": "chat.completion",
        "created": 0,
        "model": "fixture",
        "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if tool_calls else "stop"}],
        "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
    }


@pytest.fixture
def provider(monkeypatch, tmp_path):
    replies = deque()
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            status, body = replies.popleft() if replies else (500, {"error": {"message": "unexpected request"}})
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    old_uri = mlflow.get_tracking_uri()
    monkeypatch.setenv("MLFLOW_DISABLE_TELEMETRY", "true")
    monkeypatch.setenv("MLFLOW_GENAI_EVAL_MAX_WORKERS", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-only")
    monkeypatch.setattr(mlflow.tracking.fluent, "_active_experiment_id", None)
    mlflow.set_tracking_uri(f"sqlite:///{tmp_path / 'tracking.db'}")
    STATE.reset()
    try:
        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            monkeypatch.setenv("OPENAI_API_BASE", f"http://127.0.0.1:{server.server_port}/v1")
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                yield replies, requests
            finally:
                server.shutdown()
                thread.join()
    finally:
        STATE.reset()
        mlflow.set_tracking_uri(old_uri)


def _evaluate(*, condition_mode=False, verify=False):
    factory = response_conditions if condition_mode else response_guidelines
    expectations = (
        {"conditions": [{"question": "Does the response greet the user?", "fail_when": "no"}]}
        if condition_mode
        else {"guidelines": ["Greet the user."]}
    )
    return evaluate(
        data=[{"inputs": {"message": "Say hello."}, "outputs": "Hello!", "expectations": expectations}],
        scorers=[factory(model="openai:/fixture", name="policy", verify=verify)],
    )


@pytest.mark.parametrize("review", [False, True], ids=["malformed-draft", "throttled-review"])
def test_provider_failure_is_private_and_cannot_pass_on_retry(provider, capsys, caplog, review):
    replies, requests = provider
    valid = _completion({"result": [_GUIDELINE]})
    failure = (429, {"error": {"message": "rate limit " + _CANARY}}) if review else _completion("rate limit " + _CANARY)
    replies.extend(([valid] if review else []) + [failure, valid, valid])

    result = _evaluate(verify=review)
    with pytest.raises(GateFailure):
        assert_checks(result)
    assert len(requests) == (2 if review else 1)

    row = result.result_df.iloc[0]
    feedback = next(a for a in row["assessments"] if a["assessment_name"] == "policy")
    trace = mlflow.get_trace(feedback["trace_id"], flush=True)
    assert trace is not None
    stored = json.dumps(trace.to_dict())
    expected_code = "judge_provider_throttled" if review else "judge_invalid_schema"
    assert expected_code in stored
    assert _CANARY not in stored
    output = capsys.readouterr()
    assert _CANARY not in output.out + output.err + caplog.text


@pytest.mark.parametrize("review", [False, True], ids=["draft", "review"])
def test_unsolicited_tool_is_rejected_before_dispatch(provider, monkeypatch, review):
    replies, requests = provider
    calls = []

    class CanaryTool(JudgeTool):
        name = "judge_boundary_canary"

        def get_definition(self):
            pytest.fail("Response-only judges must leave tools unset")

        def invoke(self, trace, **kwargs):
            calls.append(True)
            return _CANARY

    monkeypatch.setattr(_judge_tool_registry, "_tools", _judge_tool_registry._tools.copy())
    register_judge_tool(CanaryTool())
    valid = _completion({"result": [_GUIDELINE]})
    tool = {
        "id": "tool-canary",
        "type": "function",
        "function": {"name": "judge_boundary_canary", "arguments": "{}"},
    }
    replies.extend(([valid] if review else []) + [_completion("", tool_calls=[tool]), valid])

    with pytest.raises(GateFailure):
        assert_checks(_evaluate(verify=review))
    assert calls == []
    assert len(requests) == (2 if review else 1)


@pytest.mark.parametrize("condition_mode", [False, True], ids=["guidelines", "conditions"])
def test_full_fallback_reply_must_match_schema(provider, condition_mode):
    replies, requests = provider
    replies.extend(
        [
            (400, {"error": {"message": "response_format is unsupported"}}),
            _completion({"result": [_CONDITION if condition_mode else _GUIDELINE], "error": "uncertain"}),
        ]
    )
    with pytest.raises(GateFailure):
        assert_checks(_evaluate(condition_mode=condition_mode))
    assert ["response_format" in request for request in requests] == [True, False]
