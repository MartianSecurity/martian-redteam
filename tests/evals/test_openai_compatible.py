"""Judges and model targets against an OpenAI-compatible endpoint, end to end.

A loopback server stands in for a gateway or a self-hosted model: it serves one model at
``/v1/chat/completions``, checks the key and a gateway header, and answers judge requests with a
judgment and target requests with a reply. Each failure test breaks one setting and checks that
the message names it, and that the key and the server's own error text are written nowhere.
"""

import json
import shutil
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import mlflow
import pytest
from typer.testing import CliRunner

from martian.cli import app
from martian.config import ConfigError
from martian.evals import assert_checks, evaluate, response_rubric
from martian.evals.runner import run_suite
from martian.evals.state import STATE

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "evals" / "openai-compatible"
KEY = "fixture-endpoint-key"
SERVER_TEXT = "private-server-error-text"  # in every error body; must never be repeated
PASS = {"result": [{"rubric_index": 1, "reason": "It keeps the word secret.", "verdict": "pass", "evidence_ids": []}]}


def is_judge_request(body):
    """The judge sends the reply under test as numbered response_segments; a target call does not."""
    return any("response_segments" in str(message.get("content")) for message in body.get("messages") or [])


class Endpoint:
    """A fake OpenAI-compatible server. ``behaviour`` changes how it answers."""

    def __init__(self):
        self.requests = []
        self.behaviour = {
            "model": "your-model",
            "unknown_model": (404, "The model `{model}` does not exist or you do not have access to it."),
            "required_header": None,  # (name, value) a gateway requires, else 403
            "structured": "supported",  # supported, rejected (400), refused (422) or ignored
            "judge_reply": PASS,  # what a judge request gets as message content
            "html": False,  # answer 200 with a web page, as a server that is not a chat endpoint
            "fail": None,  # (status, message) for every request
            "delay": 0.0,
        }
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                headers = {name.lower(): value for name, value in self.headers.items()}
                owner.requests.append({"path": self.path, "headers": headers, "body": body})
                time.sleep(owner.behaviour["delay"])
                if owner.behaviour["html"]:
                    return self.reply(200, "<html>not an API</html>", "text/html")
                self.reply(*owner.answer(self.path, headers, body))

            def reply(self, status, payload, kind="application/json"):
                data = (payload if isinstance(payload, str) else json.dumps(payload)).encode()
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def answer(self, path, headers, body):
        behaviour = self.behaviour

        def error(status, message):
            return status, {"error": {"message": f"{message} ({SERVER_TEXT})", "type": "invalid_request_error"}}

        if behaviour["fail"]:
            return error(*behaviour["fail"])
        if path != "/v1/chat/completions":
            return 404, {"detail": "Not Found"}
        if headers.get("authorization") != f"Bearer {KEY}":
            return error(401, "Authentication Error, Invalid API key provided.")
        required = behaviour["required_header"]
        if required and headers.get(required[0].lower()) != required[1]:
            return error(403, "Access denied for this tenant.")
        if body.get("model") != behaviour["model"]:
            status, message = behaviour["unknown_model"]
            return error(status, message.format(model=body.get("model")))
        if not body.get("messages"):
            return error(400, "messages is a required property")
        judge = is_judge_request(body)
        if "response_format" in body:
            if behaviour["structured"] == "rejected":
                return error(400, "response_format is not supported by this endpoint")
            if behaviour["structured"] == "refused":
                return error(422, "unknown variant `json_schema` for response_format")
        content = behaviour["judge_reply"] if judge else "I cannot share the code word, sorry."
        message = {"role": "assistant", "content": content if isinstance(content, str) else json.dumps(content)}
        return 200, {
            "id": "chatcmpl-fixture",
            "object": "chat.completion",
            "created": 0,
            "model": behaviour["model"],
            "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }

    @property
    def base(self):
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    def judged(self):
        return [r for r in self.requests if is_judge_request(r["body"])]

    def targeted(self):
        return [r for r in self.requests if r not in self.judged()]


@pytest.fixture
def endpoint(monkeypatch):
    for name in ("MARTIAN_API_URL", "MARTIAN_API_TOKEN", "MARTIAN_AGENT_ID", "OPENAI_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MLFLOW_DISABLE_TELEMETRY", "true")
    monkeypatch.setattr(mlflow.tracking.fluent, "_active_experiment_id", None)
    old_uri = mlflow.get_tracking_uri()
    STATE.reset()
    server = Endpoint()
    server.thread.start()
    monkeypatch.setenv("OPENAI_API_BASE", server.base)
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    try:
        yield server
    finally:
        server.server.shutdown()
        server.server.server_close()
        STATE.reset()
        mlflow.set_tracking_uri(old_uri)


def example(folder: Path) -> Path:
    """The documented example, copied unchanged."""
    for name in ("evals.yaml", "cases.jsonl"):
        shutil.copy(EXAMPLE / name, folder / name)
    return folder / "evals.yaml"


def judge_suite(folder: Path, judge: str = "") -> Path:
    """A rubric judge over stored replies, so every request is a judge request. No retries."""
    (folder / "cases.jsonl").write_text(
        "".join(json.dumps({"id": str(i), "input": "Hi", "output": "Hello."}) + "\n" for i in range(2))
    )
    judge = judge or "judge:\n  model: openai:/your-model\n"
    (folder / "evals.yaml").write_text(
        f"data: cases.jsonl\n{judge}  max_retries: 0\n  rate_limit_wait_seconds: 0\n"
        "checks:\n  - type: rubric\n    rubric: Pass if polite.\n",
        encoding="utf-8",
    )
    return folder / "evals.yaml"


def written(folder: Path) -> str:
    """Everything the run wrote, the local MLflow store included."""
    return "\n".join(
        file.read_bytes().decode("utf-8", "replace") for file in (folder / "results").iterdir() if file.is_file()
    )


def check_errors(summary):
    return [reason for reasons in summary["check_errors"].values() for reason in reasons]


def test_the_documented_example_runs_against_an_openai_compatible_endpoint(tmp_path, endpoint):
    summary = run_suite(example(tmp_path))
    assert (summary["cases"], summary["passed"], summary["errors"]) == (3, 3, 0)
    # One target call and one judge call per case, all at the base address with the key.
    assert len(endpoint.requests) == 6
    assert {r["path"] for r in endpoint.requests} == {"/v1/chat/completions"}
    assert {r["headers"]["authorization"] for r in endpoint.requests} == {f"Bearer {KEY}"}
    targeted, judged = endpoint.targeted(), endpoint.judged()
    assert sorted(r["body"]["messages"][-1]["content"] for r in targeted) == sorted(
        json.loads(line)["input"] for line in (EXAMPLE / "cases.jsonl").read_text().splitlines()
    )
    assert all("response_format" not in r["body"] and "x-client" not in r["headers"] for r in targeted)
    # The judge sends its own headers and asks for structured output.
    assert {r["headers"]["x-client"] for r in judged} == {"martian-evals"}
    assert {r["body"]["response_format"]["type"] for r in judged} == {"json_schema"}
    assert {r["body"]["model"] for r in endpoint.requests} == {"your-model"}
    assert KEY not in written(tmp_path)


def test_an_endpoint_that_declines_structured_output_is_asked_again_without_it(tmp_path, endpoint):
    endpoint.behaviour["structured"] = "rejected"
    summary = run_suite(example(tmp_path))
    assert (summary["passed"], summary["errors"]) == (3, 0)
    # Each judge stage: the structured request, declined, then the same request without it.
    assert sorted("response_format" in r["body"] for r in endpoint.judged()) == [False] * 3 + [True] * 3


def test_the_python_judge_sends_its_headers_on_every_stage_and_records_the_format(tmp_path, endpoint):
    mlflow.set_tracking_uri(f"sqlite:///{tmp_path / 'mlflow.db'}")
    endpoint.behaviour["structured"] = "rejected"
    judge = response_rubric(model="openai:/your-model", headers={"X-Gateway-Tenant": "acme"}, verify=True)
    result = evaluate(
        data=[{"inputs": {"message": "Hi"}, "outputs": "Hello.", "expectations": {"rubric": "Pass if polite."}}],
        scorers=[judge],
    )
    assert_checks(result)
    assert len(endpoint.requests) == 4  # draft and review, each declined once
    assert {r["headers"]["x-gateway-tenant"] for r in endpoint.requests} == {"acme"}
    assessment = next(a for a in result.result_df["assessments"].iloc[0] if "feedback" in a)
    assert assessment["metadata"]["martian.judge.response_format"] == "none"
    assert "acme" not in json.dumps(assessment, default=str)


def test_judge_headers_come_from_the_environment_and_are_written_nowhere(tmp_path, endpoint, monkeypatch):
    endpoint.behaviour["required_header"] = ("X-Gateway-Key", "gateway-secret-123")
    suite = judge_suite(tmp_path, 'judge:\n  model: openai:/your-model\n  headers: {X-Gateway-Key: "${GATEWAY_KEY}"}\n')
    monkeypatch.delenv("GATEWAY_KEY", raising=False)
    with pytest.raises(ConfigError, match=r"judge.headers.X-Gateway-Key needs the environment variable GATEWAY_KEY"):
        run_suite(suite)
    assert endpoint.requests == []  # stopped before any case
    monkeypatch.setenv("GATEWAY_KEY", "gateway-secret-123")
    summary = run_suite(suite)
    assert (summary["passed"], summary["errors"]) == (2, 0)
    assert {r["headers"]["x-gateway-key"] for r in endpoint.requests} == {"gateway-secret-123"}
    assert "gateway-secret-123" not in written(tmp_path)


def _free_port():
    with socket.socket() as closed:
        closed.bind(("127.0.0.1", 0))
        return closed.getsockname()[1]


@pytest.mark.parametrize(
    "fault, code, setting",
    [
        ("unreachable address", "judge_provider_unreachable", "Check OPENAI_API_BASE."),
        ("address without /v1", "judge_provider_address", "Check OPENAI_API_BASE: the endpoint's base address"),
        ("address without a scheme", "judge_provider_address", "Check OPENAI_API_BASE: it must be an http"),
        (
            "address of a web page",
            "judge_provider_address",
            "did not answer as a chat endpoint. Check OPENAI_API_BASE.",
        ),
        ("address in OPENAI_BASE_URL", "judge_provider_unreachable", "Check OPENAI_BASE_URL."),
        ("wrong key", "judge_provider_auth", "Check OPENAI_API_KEY and judge.headers."),
        ("missing key", "judge_provider_key", "Set OPENAI_API_KEY."),
        ("missing gateway header", "judge_provider_auth", "Check OPENAI_API_KEY and judge.headers."),
        ("unknown model, 404", "judge_provider_model", "Check judge.model."),
        ("unknown model, 400", "judge_provider_model", "Check judge.model."),
        ("model not allowed for the key, 403", "judge_provider_model", "OPENAI_API_KEY may not use it"),
        ("unsupported parameter", "judge_provider_rejected", "Check judge.inference_params."),
        ("structured output refused with 422", "judge_provider_structured_output", "judge.model"),
        ("structured output ignored", "judge_invalid_schema", "may ignore structured output (response_format)"),
        ("declined, then not JSON", "judge_invalid_schema", "declined structured output"),
    ],
)
def test_a_wrong_judge_setting_is_named_in_the_error(tmp_path, endpoint, monkeypatch, fault, code, setting):
    judge = "judge:\n  model: openai:/your-model\n"
    if fault == "unreachable address":
        monkeypatch.setenv("OPENAI_API_BASE", f"http://127.0.0.1:{_free_port()}/v1")
    elif fault == "address without /v1":
        monkeypatch.setenv("OPENAI_API_BASE", endpoint.base.removesuffix("/v1"))
    elif fault == "address without a scheme":
        monkeypatch.setenv("OPENAI_API_BASE", endpoint.base.removeprefix("http://"))
    elif fault == "address of a web page":
        endpoint.behaviour["html"] = True
    elif fault == "address in OPENAI_BASE_URL":
        monkeypatch.delenv("OPENAI_API_BASE")
        monkeypatch.setenv("OPENAI_BASE_URL", f"http://127.0.0.1:{_free_port()}/v1")
    elif fault == "wrong key":
        monkeypatch.setenv("OPENAI_API_KEY", "sk-wrong-key")
    elif fault == "missing key":
        monkeypatch.delenv("OPENAI_API_KEY")
    elif fault == "missing gateway header":
        endpoint.behaviour["required_header"] = ("X-Gateway-Tenant", "acme")
    elif fault.startswith("unknown model"):
        judge = judge.replace("your-model", "no-such-model")
        if fault.endswith("400"):
            endpoint.behaviour["unknown_model"] = (400, "Invalid model name passed in model={model}.")
    elif fault.startswith("model not allowed"):
        judge = judge.replace("your-model", "other-model")
        endpoint.behaviour["unknown_model"] = (403, "The requested model '{model}' is not available for this API key.")
    elif fault == "unsupported parameter":
        endpoint.behaviour["fail"] = (400, "top_p is not supported")
    elif fault == "structured output refused with 422":
        endpoint.behaviour["structured"] = "refused"
    elif fault == "structured output ignored":
        endpoint.behaviour.update(structured="ignored", judge_reply="The reply is polite, so it passes.")
    else:
        endpoint.behaviour.update(structured="rejected", judge_reply="The reply is polite, so it passes.")

    summary = run_suite(judge_suite(tmp_path, judge))
    assert (summary["cases"], summary["errors"]) == (2, 2)
    reasons = check_errors(summary)
    assert len(reasons) == 1 and f"[{code}]" in reasons[0] and setting in reasons[0], reasons
    text = written(tmp_path)
    assert SERVER_TEXT not in text and "sk-wrong-key" not in text and KEY not in text


@pytest.mark.parametrize(
    "fault, reason",
    [
        ("unreachable address", "the target could not be reached: ConnectError; check target.url"),
        ("wrong path", "the target returned HTTP 404; check target.url"),
        ("wrong key", "the target returned HTTP 401; check the credentials in target.headers"),
        ("unknown model, 404", "the target returned HTTP 404; check the model named in target.body"),
        ("unknown model, 400", "the target returned HTTP 400; check the model named in target.body"),
        ("model not allowed for the key, 403", "the target returned HTTP 403; check the model named in target.body"),
        ("bad body", "the target returned HTTP 400; check target.body"),
        ("slow", "the target did not answer within target.timeout_seconds"),
    ],
)
def test_a_wrong_target_setting_is_named_in_the_error(tmp_path, endpoint, monkeypatch, fault, reason):
    suite = example(tmp_path)
    text = suite.read_text()
    text = text[: text.index("# The judge")] + "checks:\n  - type: not_contains\n    values: [Piano]\n"
    if fault == "unreachable address":
        monkeypatch.setenv("OPENAI_API_BASE", f"http://127.0.0.1:{_free_port()}/v1")
    elif fault == "wrong path":
        text = text.replace("/chat/completions", "/chat/complete")
    elif fault == "wrong key":
        monkeypatch.setenv("OPENAI_API_KEY", "sk-wrong-key")
    elif fault.startswith("unknown model") or fault.startswith("model not allowed"):
        text = text.replace("model: your-model", "model: other-model")
        status = 400 if fault.endswith("400") else 403 if fault.endswith("403") else 404
        endpoint.behaviour["unknown_model"] = (status, "The model `{model}` does not exist.")
    elif fault == "bad body":
        text = text.replace("    messages:", "    prompt: x\n    unused:")
    else:
        endpoint.behaviour["delay"] = 1.5
        text = text.replace("  response_path:", "  timeout_seconds: 1\n  response_path:")
    suite.write_text(text)

    summary = run_suite(suite)
    assert (summary["cases"], summary["errors"]) == (3, 3)
    assert summary["target_errors"] == {reason: 3}
    written_text = written(tmp_path)
    assert SERVER_TEXT not in written_text and "sk-wrong-key" not in written_text and KEY not in written_text


def test_the_command_prints_the_judge_setting_to_fix(tmp_path, endpoint, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-wrong-key")
    result = CliRunner().invoke(app, ["evals", "run", str(judge_suite(tmp_path))])
    assert result.exit_code == 2
    assert "2 case(s), rubric_judge: Judge draft failed [judge_provider_auth]" in result.output
    assert "Check OPENAI_API_KEY and judge.headers." in result.output


@pytest.mark.parametrize(
    "text, unknown",
    [
        # Wordings real servers use for a model they do not serve, or one the key may not use.
        ("The model `gpt-x` does not exist or you do not have access to it.", True),  # OpenAI, 404
        ("The model `x` does not exist.", True),  # vLLM, 404
        ('model "x" not found, try pulling it first', True),  # Ollama, 404
        ("/chat/completions: Invalid model name passed in model=x.", True),  # LiteLLM, 400
        ("The requested model 'x' is not available for this API key, or the model name is invalid.", True),  # 403
        ("The provided model identifier is invalid.", True),  # Bedrock's OpenAI-compatible endpoint, 400
        # Errors that are not about the model.
        ('{"detail":"Not Found"}', False),
        ("top_p is not supported", False),
        ("response_format is not supported by this model", False),
        ("Authentication Error, Invalid API key provided.", False),
    ],
)
def test_an_unknown_model_is_told_apart_from_other_errors(text, unknown):
    from martian.evals._endpoint_errors import names_unknown_model

    assert names_unknown_model(text) is unknown


@pytest.mark.parametrize(
    "headers, problem",
    [({"": "x"}, "nonempty"), ({"X-A": 1}, "string values"), ({"X-A": "a\r\nB: c"}, "line break"), (["X-A"], "map")],
)
def test_judge_headers_are_validated(headers, problem):
    with pytest.raises(ValueError, match=problem):
        response_rubric(model="openai:/your-model", headers=headers)
