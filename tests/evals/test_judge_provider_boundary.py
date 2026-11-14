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
            # A reply is (status, body) or (status, body, headers).
            status, body, *extra = replies.popleft() if replies else (500, {"error": {"message": "unexpected request"}})
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            for name, value in (extra[0] if extra else {}).items():
                self.send_header(name, value)
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


def _evaluate(*, condition_mode=False, verify=False, max_retries=0, **budget):
    factory = response_conditions if condition_mode else response_guidelines
    expectations = (
        {"conditions": [{"question": "Does the response greet the user?", "fail_when": "no"}]}
        if condition_mode
        else {"guidelines": ["Greet the user."]}
    )
    return evaluate(
        data=[{"inputs": {"message": "Say hello."}, "outputs": "Hello!", "expectations": expectations}],
        scorers=[factory(model="openai:/fixture", name="policy", verify=verify, max_retries=max_retries, **budget)],
    )


@pytest.mark.parametrize("review", [False, True], ids=["malformed-draft", "throttled-review"])
def test_provider_failure_is_private_and_cannot_pass_on_retry(provider, capsys, caplog, review):
    replies, requests = provider
    valid = _completion({"result": [_GUIDELINE]})
    failure = (429, {"error": {"message": "rate limit " + _CANARY}}) if review else _completion("rate limit " + _CANARY)
    replies.extend(([valid] if review else []) + [failure, valid, valid])

    # No rate-limit budget, so the throttled review stays a failure.
    result = _evaluate(verify=review, rate_limit_wait_seconds=0)
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


@pytest.fixture
def no_backoff(monkeypatch):
    """Record each wait instead of sleeping, and let the judge's clock advance by it."""
    waits = []
    now = [0.0]

    def sleep(seconds):
        waits.append(seconds)
        now[0] += seconds

    monkeypatch.setattr("martian.evals._judge_transport._sleep", sleep)
    monkeypatch.setattr("martian.evals._judge_transport._clock", lambda: now[0], raising=False)
    return waits


_THROTTLED = {"error": {"message": "rate limit"}}


def test_throttling_longer_than_the_old_retry_window_is_outlasted(provider, no_backoff):
    # The reported defect: five retries over about half a minute, then a judge error.
    replies, requests = provider
    replies.extend([(429, _THROTTLED, {"Retry-After": "2"})] * 20 + [_completion({"result": [_GUIDELINE]})])

    result = _evaluate(max_retries=5)
    assert_checks(result)
    assert len(requests) == 21
    assert no_backoff == [2.0] * 20  # the provider's Retry-After, 40 seconds in all
    assert _feedback_metadata(result)["martian.judge.provider_retries"] == "20"


def test_retry_after_in_milliseconds_is_honoured(provider, no_backoff):
    replies, _ = provider
    replies.extend([(429, _THROTTLED, {"retry-after-ms": "250"}), _completion({"result": [_GUIDELINE]})])

    assert_checks(_evaluate())
    assert no_backoff == [0.25]


def test_rate_limits_stop_when_the_time_budget_is_spent(provider, no_backoff):
    replies, requests = provider
    replies.extend([(429, _THROTTLED, {"Retry-After": "100"})] * 10)

    result = _evaluate(rate_limit_wait_seconds=250)
    with pytest.raises(GateFailure):
        assert_checks(result)
    # Waited 100 + 100; the third reply asks for 100 more with only 50 left, so the judge stops.
    assert len(requests) == 3 and no_backoff == [100.0, 100.0]
    assert "judge_provider_throttled" in json.dumps(result.result_df["assessments"].iloc[0], default=str)


def test_a_retry_after_beyond_the_budget_is_not_waited_for(provider, no_backoff):
    replies, requests = provider
    replies.extend([(429, _THROTTLED, {"Retry-After": "1000"}), _completion({"result": [_GUIDELINE]})])

    with pytest.raises(GateFailure):
        assert_checks(_evaluate())
    assert len(requests) == 1 and no_backoff == []


def test_rate_limits_without_retry_after_back_off_within_the_budget(provider, no_backoff):
    replies, requests = provider
    replies.extend([(429, _THROTTLED)] * 8 + [_completion({"result": [_GUIDELINE]})])

    assert_checks(_evaluate(max_retries=0))  # the retry count does not limit rate-limit retries
    assert len(requests) == 9
    assert no_backoff == sorted(no_backoff) and no_backoff[-1] <= 30.5


def test_connection_failures_are_retried(provider, no_backoff, monkeypatch):
    import socket

    with socket.socket() as closed:
        closed.bind(("127.0.0.1", 0))
        port = closed.getsockname()[1]
    monkeypatch.setenv("OPENAI_API_BASE", f"http://127.0.0.1:{port}/v1")

    with pytest.raises(GateFailure):
        assert_checks(_evaluate(max_retries=2))
    assert len(no_backoff) == 2


def test_the_rate_limit_budget_is_validated():
    for bad in (-1, 3601, "60", True, float("nan")):
        with pytest.raises(ValueError, match="rate_limit_wait_seconds"):
            response_guidelines(model="openai:/fixture", rate_limit_wait_seconds=bad)


def test_retry_after_dates_are_read_as_seconds():
    from datetime import UTC, datetime, timedelta
    from email.utils import format_datetime

    from martian.evals._judge_transport import _retry_after

    later = format_datetime(datetime.now(UTC) + timedelta(seconds=30), usegmt=True)
    assert 25 <= _retry_after({"Retry-After": later}) <= 30
    assert _retry_after({"Retry-After": "7"}) == 7.0
    assert _retry_after({"retry-after-ms": "1500"}) == 1.5
    assert _retry_after({"Retry-After": "soon"}) is None
    assert _retry_after({}) is None


def test_rate_limit_is_retried_with_backoff_and_then_judged(provider, no_backoff):
    replies, requests = provider
    throttled = (429, {"error": {"message": "rate limit"}})
    replies.extend([throttled, (503, {"error": {"message": "overloaded"}}), _completion({"result": [_GUIDELINE]})])

    result = _evaluate(max_retries=5)
    assert_checks(result)
    assert len(requests) == 3
    assert len(no_backoff) == 2 and no_backoff[0] < no_backoff[1] <= 30.5
    assert _feedback_metadata(result)["martian.judge.provider_retries"] == "2"


def test_a_received_reply_is_never_retried(provider, no_backoff):
    replies, requests = provider
    replies.extend([_completion("not a judgment"), _completion({"result": [_GUIDELINE]})])

    result = _evaluate(max_retries=5)
    with pytest.raises(GateFailure):
        assert_checks(result)
    assert len(requests) == 1 and no_backoff == []


def test_server_error_retries_are_bounded(provider, no_backoff):
    replies, requests = provider
    replies.extend([(503, {"error": {"message": "overloaded"}})] * 10)

    result = _evaluate(max_retries=2)
    with pytest.raises(GateFailure):
        assert_checks(result)
    assert len(requests) == 3 and len(no_backoff) == 2


def test_request_errors_are_not_retried(provider, no_backoff):
    replies, requests = provider
    replies.extend([(401, {"error": {"message": "bad key"}}), _completion({"result": [_GUIDELINE]})])

    result = _evaluate(max_retries=5)
    with pytest.raises(GateFailure):
        assert_checks(result)
    assert len(requests) == 1 and no_backoff == []


def _feedback_metadata(result):
    assessments = result.result_df["assessments"].iloc[0]
    return next(a for a in assessments if "feedback" in a)["metadata"]
