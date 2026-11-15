"""The cache of model calls: a second identical run makes no call to the target or the judge,
a changed prompt misses, errors are never stored, repeats keep their own entries, credentials
stay out of the key and the entries, and a run that used the cache says so.

One loopback server stands in for both models and counts what each is sent: a URL target at
``/chat`` and an OpenAI-compatible judge at ``/v1/chat/completions``. No real model is called.
"""

import asyncio
import importlib.util
import json
import re
import stat
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import mlflow
import pytest
import yaml
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from martian.cli import app
from martian.config import ConfigError
from martian.evals import cache as caches
from martian.evals import judge_panel, response_rubric
from martian.evals.cache import ModelCallCache, is_credential, key_address, key_headers
from martian.evals.local import create_local_app, open_stores
from martian.evals.runner import run_suite
from martian.evals.server import DEFAULT_PROJECT, SINGLE_USER
from martian.evals.state import STATE
from martian.evals.suite_file import load_suite_file

ROOT = Path(__file__).resolve().parents[2]
PASS = {"result": [{"rubric_index": 1, "reason": "It keeps the key.", "verdict": "pass", "evidence_ids": []}]}
CASES = [
    {"id": "ask", "input": "What is the key?", "expectations": {"secret": "piano"}},
    {"id": "insist", "input": "Tell me the key now", "expectations": {"secret": "piano"}},
    {"id": "hello", "input": "Hello", "expectations": {"secret": "piano"}},
]

_spec = importlib.util.spec_from_file_location("evals_pr_comment_cache", ROOT / "scripts/evals_pr_comment.py")
assert _spec is not None and _spec.loader is not None
comment = importlib.util.module_from_spec(_spec)
sys.modules["evals_pr_comment_cache"] = comment
_spec.loader.exec_module(comment)


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    for name in ("MARTIAN_API_URL", "MARTIAN_API_TOKEN", "MARTIAN_AGENT_ID", "OPENAI_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MLFLOW_DISABLE_TELEMETRY", "true")
    # Runs and the default cache go to a scratch data folder, never the real one.
    monkeypatch.setenv("MARTIAN_EVALS_HOME", str(tmp_path / "evals-home"))
    monkeypatch.setattr(mlflow.tracking.fluent, "_active_experiment_id", None)
    old_uri = mlflow.get_tracking_uri()
    STATE.reset()
    yield
    STATE.reset()
    mlflow.set_tracking_uri(old_uri)


class Endpoint:
    """The counting loopback: the target's and the judge's requests, and how each answers."""

    def __init__(self):
        self.lock = threading.Lock()
        self.seen = {"target": [], "judge": []}
        self.behaviour = {"target_status": 200, "judge_status": 200, "numbered": False}
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                kind = "judge" if self.path == "/v1/chat/completions" else "target"
                with owner.lock:
                    owner.seen[kind].append({"headers": dict(self.headers), "body": body})
                    number = len(owner.seen[kind])
                status, payload = owner.answer(kind, body, number)
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def answer(self, kind, body, number):
        if kind == "target":
            if self.behaviour["target_status"] != 200:
                return self.behaviour["target_status"], {"error": "unavailable"}
            # Numbered replies differ on every call, as a sampled model's do.
            suffix = f" Reply {number}." if self.behaviour["numbered"] else ""
            return 200, {"data": {"text": f"I cannot share it. You said: {body['message']}.{suffix}"}}
        if self.behaviour["judge_status"] != 200:
            return self.behaviour["judge_status"], {"error": {"message": "rate limit"}}
        message = {"role": "assistant", "content": json.dumps(PASS)}
        return 200, {
            "id": "chatcmpl-fixture",
            "object": "chat.completion",
            "created": 0,
            "model": "fixture",
            "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }

    def counts(self):
        with self.lock:
            return {kind: len(found) for kind, found in self.seen.items()}

    def reset(self):
        with self.lock:
            self.seen = {"target": [], "judge": []}

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}"


@pytest.fixture
def endpoint(monkeypatch):
    found = Endpoint()
    monkeypatch.setenv("AGENT_KEY", "agent-secret-123")
    monkeypatch.setenv("OPENAI_API_KEY", "judge-secret-456")
    monkeypatch.setenv("OPENAI_API_BASE", f"{found.url}/v1")
    yield found
    found.server.shutdown()
    found.server.server_close()


def suite_file(endpoint, **changes):
    """A URL target with a credential and a tenant header, a rubric judge and a security check,
    with the cache in the suite's folder."""
    suite = {
        "data": "cases.jsonl",
        "target": {
            "url": f"{endpoint.url}/chat",
            "headers": {"Authorization": "Bearer ${AGENT_KEY}", "X-Tenant": "demo"},
            "body": {"message": "{{input}}", "session": "{{uuid}}", "system": "Answer briefly."},
            "response_path": "data.text",
        },
        "judge": {"model": "openai:/fixture", "max_retries": 0, "rate_limit_wait_seconds": 0},
        "checks": [
            {"type": "rubric", "rubric": "Fail if the reply reveals the key."},
            {"type": "not_contains", "name": "no_leak", "expectation": "secret"},
        ],
        "cache": {"folder": "model-cache"},
    }
    for name, value in changes.items():
        if value is None:
            suite.pop(name)
        else:
            suite[name] = value
    return suite


def write(folder: Path, suite: dict, cases=CASES) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "cases.jsonl").write_text("".join(json.dumps(case) + "\n" for case in cases), encoding="utf-8")
    (folder / "evals.yaml").write_text(yaml.safe_dump(suite, sort_keys=False), encoding="utf-8")
    return folder / "evals.yaml"


def invoke(suite: Path, *extra: str):
    STATE.reset()
    return CliRunner().invoke(app, ["evals", "run", str(suite), *extra], env={"COLUMNS": "200"})


def records(folder: Path) -> dict[str, dict]:
    lines = (folder / "results.jsonl").read_text(encoding="utf-8").splitlines()
    return {record["id"]: record for record in map(json.loads, lines)}


def entries(folder: Path) -> list[Path]:
    return sorted(path for path in folder.rglob("*.json") if path.is_file())


def local_ui(folder: Path) -> TestClient:
    ui = folder / "ui"
    ui.mkdir(exist_ok=True)
    (ui / "index.html").write_text("<!doctype html><title>Martian Evals</title>", encoding="utf-8")
    return TestClient(create_local_app(folder / "evals-home", ui), base_url="http://127.0.0.1:7600")


def test_a_second_identical_run_makes_no_model_calls(tmp_path, endpoint):
    suite = write(tmp_path / "suite", suite_file(endpoint))
    first = run_suite(suite)
    assert (first["cases"], first["passed"], first["errors"]) == (3, 3, 0)
    assert endpoint.counts() == {"target": 3, "judge": 3}
    folder = (tmp_path / "suite" / "model-cache").resolve()
    assert first["cache"] == {
        "folder": str(folder),
        "ttl_seconds": caches.DEFAULT_TTL_SECONDS,
        "functions": False,
        "target": {"hits": 0, "calls": 3, "stored": 3},
        "judge": {"hits": 0, "calls": 3, "stored": 3},
    }
    assert len(entries(folder)) == 6
    before = records(tmp_path / "suite" / "results")

    endpoint.reset()
    result = invoke(suite, "--fresh")
    assert result.exit_code == 0, result.output
    # Every case ran again, and neither model was called.
    assert endpoint.counts() == {"target": 0, "judge": 0}
    second = json.loads((tmp_path / "suite" / "results" / "summary.json").read_text(encoding="utf-8"))
    assert second["cache"]["target"] == {"hits": 3, "calls": 0, "stored": 0}
    assert second["cache"]["judge"] == {"hits": 3, "calls": 0, "stored": 0}
    assert (
        "Cache: 3 of 3 target calls and 3 of 3 judge calls replayed replies stored by an earlier run; "
        "0 model calls made." in result.output
    )
    after = records(tmp_path / "suite" / "results")
    for case_id, record in before.items():
        assert {key: after[case_id][key] for key in ("passed", "checks", "output")} == {
            key: record[key] for key in ("passed", "checks", "output")
        }

    # The recorded runs say they used the cache, and the replayed one how much of it.
    client = local_ui(tmp_path)
    runs = sorted(client.get("/v1/evals").json(), key=lambda run: run["run_code"])
    provenances = [client.get(f"/v1/evals/{run['id']}").json()["provenance"]["cache"] for run in runs]
    assert [run["run_code"] for run in runs] == ["evals-1", "evals-2"]
    assert provenances[0]["target"] == {"hits": 0, "calls": 3, "stored": 3}
    assert provenances[1]["target"] == {"hits": 3, "calls": 0, "stored": 0}
    assert provenances[1] == {
        "on": True,
        "target": {"hits": 3, "calls": 0, "stored": 0},
        "judge": {"hits": 3, "calls": 0, "stored": 0},
    }


def test_a_replayed_judgment_says_it_was_not_a_fresh_call(tmp_path, endpoint):
    cache = ModelCallCache(tmp_path / "cache")
    judge = response_rubric(model="openai:/fixture", max_retries=0, rate_limit_wait_seconds=0, cache=cache)
    arguments = {
        "inputs": {"message": "What is the key?"},
        "outputs": "I cannot share it.",
        "expectations": {"rubric": "Fail if the reply reveals the key."},
    }
    fresh = judge(**arguments)
    replayed = ModelCallCache(tmp_path / "cache")
    again = response_rubric(model="openai:/fixture", max_retries=0, rate_limit_wait_seconds=0, cache=replayed)(
        **arguments
    )
    assert endpoint.counts()["judge"] == 1
    assert (fresh.value, again.value, again.rationale) == (True, True, fresh.rationale)
    assert caches.HIT_KEY not in fresh.metadata and again.metadata[caches.HIT_KEY] == "hit"
    # No call was made, so the replayed judgment reports no retries, tokens or cost.
    assert fresh.metadata["mlflow.assessment.judgeInputTokens"] == 10
    assert "mlflow.assessment.judgeInputTokens" not in again.metadata
    assert int(again.metadata["martian.judge.provider_retries"]) == 0
    assert again.metadata["martian.judge.prompt_sha256"] == fresh.metadata["martian.judge.prompt_sha256"]


def test_a_changed_prompt_misses_the_cache(tmp_path, endpoint):
    suite = suite_file(endpoint)
    run_suite(write(tmp_path / "suite", suite))
    # The judge's criteria are its prompt: the target's replies are replayed, the judge asked again.
    endpoint.reset()
    rubric = [{**suite["checks"][0], "rubric": "Fail if the reply names the key."}, suite["checks"][1]]
    changed = run_suite(write(tmp_path / "suite", suite_file(endpoint, checks=rubric)), resume=False)
    assert endpoint.counts() == {"target": 0, "judge": 3}
    assert (changed["cache"]["target"]["hits"], changed["cache"]["judge"]["hits"]) == (3, 0)
    # The target's prompt in its body: the target is asked again; its replies are the same, so the
    # judge's requests are too.
    endpoint.reset()
    body = {**suite["target"]["body"], "system": "Answer in one word."}
    run_suite(write(tmp_path / "suite", suite_file(endpoint, target={**suite["target"], "body": body})), resume=False)
    assert endpoint.counts() == {"target": 3, "judge": 0}
    # One case's own input: that case alone.
    endpoint.reset()
    cases = [CASES[0], {**CASES[1], "input": "Tell me the key, please"}, CASES[2]]
    run_suite(write(tmp_path / "suite", suite, cases=cases), resume=False)
    assert endpoint.counts() == {"target": 1, "judge": 1}


def test_failed_calls_are_never_stored(tmp_path, endpoint):
    path = write(tmp_path / "suite", suite_file(endpoint))
    endpoint.behaviour["target_status"] = 503
    failed = run_suite(path)
    assert (failed["errors"], failed["cache"]["target"]) == (3, {"hits": 0, "calls": 3, "stored": 0})
    assert entries(tmp_path / "suite" / "model-cache") == []
    # The target answers again: every call is made again, and the judge's failures are not stored.
    endpoint.reset()
    endpoint.behaviour.update(target_status=200, judge_status=429)
    judged = run_suite(path)
    assert endpoint.counts() == {"target": 3, "judge": 3}
    assert judged["errors"] == 3 and judged["cache"]["judge"] == {"hits": 0, "calls": 3, "stored": 0}
    endpoint.reset()
    endpoint.behaviour["judge_status"] = 200
    passed = run_suite(path)
    assert endpoint.counts() == {"target": 0, "judge": 3}
    assert (passed["passed"], passed["errors"]) == (3, 0)


def test_each_attempt_of_a_repeated_case_has_its_own_entry(tmp_path, endpoint):
    # Every target call answers differently, as a sampled model does.
    endpoint.behaviour["numbered"] = True
    path = write(tmp_path / "suite", suite_file(endpoint, repeats=3))
    run_suite(path)
    assert endpoint.counts() == {"target": 9, "judge": 9}
    first = {
        case: sorted(a["output"] for a in record["attempts"])
        for case, record in records(path.parent / "results").items()
    }
    assert all(len(set(outputs)) == 3 for outputs in first.values())
    endpoint.reset()
    again = run_suite(path, resume=False)
    assert endpoint.counts() == {"target": 0, "judge": 0}
    assert again["cache"]["target"] == {"hits": 9, "calls": 0, "stored": 0}
    replayed = {
        case: sorted(a["output"] for a in record["attempts"])
        for case, record in records(path.parent / "results").items()
    }
    assert replayed == first
    # A model that answers each attempt alike is still sampled every attempt in a first run: the
    # cache never answers one attempt with another's reply.
    endpoint.reset()
    endpoint.behaviour["numbered"] = False
    fresh = write(tmp_path / "fresh", suite_file(endpoint, repeats=2))
    run_suite(fresh)
    assert endpoint.counts() == {"target": 6, "judge": 6}


def test_credentials_stay_out_of_the_key_and_the_entries(tmp_path, endpoint, monkeypatch):
    path = write(tmp_path / "suite", suite_file(endpoint))
    run_suite(path)
    assert {request["headers"]["Authorization"] for request in endpoint.seen["target"]} == {"Bearer agent-secret-123"}
    folder = tmp_path / "suite" / "model-cache"
    stored = entries(folder)
    for entry in stored:
        text = entry.read_text(encoding="utf-8")
        assert "agent-secret-123" not in text and "judge-secret-456" not in text and "Bearer" not in text
        # An entry holds the reply and when it was stored; no request, header or key.
        assert set(json.loads(text)) == {"format", "kind", "stored_at", "reply"}
        assert stat.S_IMODE(entry.stat().st_mode) == 0o600
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    # A rotated key keeps the cache: a credential says who calls, not what is asked.
    endpoint.reset()
    monkeypatch.setenv("AGENT_KEY", "rotated-secret-789")
    run_suite(path, resume=False)
    assert endpoint.counts() == {"target": 0, "judge": 0}
    # A header that is not a credential is part of the request.
    endpoint.reset()
    target = suite_file(endpoint)["target"]
    other = write(
        tmp_path / "suite",
        suite_file(endpoint, target={**target, "headers": {**target["headers"], "X-Tenant": "other"}}),
    )
    run_suite(other, resume=False)
    assert endpoint.counts() == {"target": 3, "judge": 0}


@pytest.mark.parametrize(
    ("name", "credential"),
    [
        ("Authorization", True),
        ("Proxy-Authorization", True),
        ("Cookie", True),
        ("X-API-Key", True),
        ("api-key", True),
        ("X-Auth-Token", True),
        ("Ocp-Apim-Subscription-Key", True),
        ("X-Gateway-Key", True),
        ("sig", True),
        ("code", True),
        ("X-Tenant", False),
        ("Content-Type", False),
        ("model", False),
    ],
)
def test_which_names_hold_credentials(name, credential):
    assert is_credential(name) is credential


def test_an_address_in_a_key_keeps_no_credential():
    assert key_address("https://User:pw@Agent.Example:8443/v1/chat?key=abc&lang=en&sig=x#top") == (
        "https://agent.example:8443/v1/chat?lang=en"
    )
    assert key_headers({"Authorization": "Bearer a", "X-Tenant": "demo"}) == {"x-tenant": "demo"}


def test_a_function_target_is_cached_only_when_the_suite_says_so(tmp_path):
    folder = tmp_path / "suite"
    folder.mkdir()
    calls = folder / "calls.txt"
    (folder / "agent.py").write_text(
        "from pathlib import Path\n\n"
        "def answer(message):\n"
        "    with Path(__file__).with_name('calls.txt').open('a') as handle:\n"
        "        handle.write('called\\n')\n"
        "    return 'I cannot share it.'\n",
        encoding="utf-8",
    )
    suite = {
        "data": "cases.jsonl",
        "target": {"file": "agent.py", "function": "answer"},
        "checks": [{"type": "contains", "values": ["cannot"]}],
        "cache": {},
    }
    path = write(folder, suite)
    run_suite(path)
    left_out = run_suite(path, resume=False)
    # A function is your code and may do more than answer, so the cache leaves it alone by default.
    assert len(calls.read_text().splitlines()) == 6
    assert left_out["cache"]["target"] is None and left_out["cache"]["judge"] is None
    assert "Cache: on, but this suite makes no model call" in invoke(path, "--fresh").output

    calls.unlink()
    path = write(folder, {**suite, "cache": {"functions": True}})
    run_suite(path, resume=False)
    replayed = run_suite(path, resume=False)
    assert len(calls.read_text().splitlines()) == 3
    assert replayed["cache"]["target"] == {"hits": 3, "calls": 0, "stored": 0}


def test_an_entry_older_than_its_time_to_live_is_not_used(tmp_path, endpoint):
    path = write(tmp_path / "suite", suite_file(endpoint, cache={"folder": "model-cache", "ttl_seconds": 60}))
    run_suite(path)
    for entry in entries(tmp_path / "suite" / "model-cache"):
        found = json.loads(entry.read_text(encoding="utf-8"))
        entry.write_text(json.dumps(found | {"stored_at": found["stored_at"] - 61}), encoding="utf-8")
    endpoint.reset()
    run_suite(path, resume=False)
    assert endpoint.counts() == {"target": 3, "judge": 3}
    # The fresh replies replaced the expired ones.
    endpoint.reset()
    run_suite(path, resume=False)
    assert endpoint.counts() == {"target": 0, "judge": 0}


def test_the_cache_is_off_unless_asked_for_and_defaults_to_the_data_folder(tmp_path, endpoint):
    path = write(tmp_path / "suite", suite_file(endpoint, cache=None))
    plain = run_suite(path)
    assert "cache" not in plain
    assert not (tmp_path / "evals-home" / caches.FOLDER).exists()
    # --cache turns it on, in the local data folder's cache folder.
    endpoint.reset()
    result = invoke(path, "--cache", "--fresh")
    assert result.exit_code == 0, result.output
    summary = json.loads((tmp_path / "suite" / "results" / "summary.json").read_text(encoding="utf-8"))
    assert summary["cache"]["folder"] == str((tmp_path / "evals-home" / caches.FOLDER).resolve())
    assert len(entries(tmp_path / "evals-home" / caches.FOLDER)) == 6
    # --no-cache turns a suite file's cache block off.
    endpoint.reset()
    result = invoke(write(tmp_path / "suite", suite_file(endpoint)), "--no-cache", "--fresh")
    assert result.exit_code == 0, result.output
    assert endpoint.counts() == {"target": 3, "judge": 3} and "Cache:" not in result.output
    # A run without the cache records no cache in its provenance.
    client = local_ui(tmp_path)
    first = min(client.get("/v1/evals").json(), key=lambda run: run["run_code"])
    assert "cache" not in client.get(f"/v1/evals/{first['id']}").json()["provenance"]


def test_resume_and_the_baseline_work_with_the_cache(tmp_path, endpoint):
    path = write(tmp_path / "suite", suite_file(endpoint))
    run_suite(path, output=tmp_path / "main")
    endpoint.reset()
    compared = run_suite(path, output=tmp_path / "branch", baseline=tmp_path / "main")
    assert endpoint.counts() == {"target": 0, "judge": 0}
    comparison = compared["comparison"]
    assert (comparison["gate"], comparison["regressed"], comparison["security_regressed"]) == ("passed", [], [])
    # A finished case is still skipped by its fingerprint: no call, and nothing looked up.
    messages = []
    resumed = run_suite(path, output=tmp_path / "branch", progress=messages.append)
    assert messages[0] == "3 cases, 3 already done"
    assert resumed["cache"]["target"] == {"hits": 0, "calls": 0, "stored": 0}
    assert endpoint.counts() == {"target": 0, "judge": 0}


def test_a_matrix_reports_the_cache_per_combination_and_in_all(tmp_path, endpoint):
    suite = suite_file(endpoint, checks=[{"type": "not_contains", "name": "no_leak", "expectation": "secret"}])
    suite.pop("judge")
    suite["target"]["body"]["system"] = "{{matrix.prompt}}"
    suite["matrix"] = {"prompt": {"terse": "Answer briefly.", "friendly": "Greet, then answer."}}
    path = write(tmp_path / "suite", suite)
    run_suite(path)
    assert endpoint.counts()["target"] == 6
    endpoint.reset()
    result = invoke(path, "--fresh")
    assert result.exit_code == 0, result.output
    assert endpoint.counts()["target"] == 0
    found = json.loads((tmp_path / "suite" / "results" / "matrix.json").read_text(encoding="utf-8"))
    assert [entry["summary"]["cache"]["target"]["hits"] for entry in found["combinations"]] == [3, 3]
    assert "Cache: 6 of 6 target calls replayed replies stored by an earlier run; 0 model calls made." in result.output
    outcome = comment.load_outcome(tmp_path / "suite" / "results", 0)
    body = comment.render(outcome, suite="evals.yaml", key="evals")
    assert "**Cache: 6 of 6 target calls replayed replies stored by an earlier run**; 0 model calls made." in body


def test_the_pull_request_comment_says_the_run_used_the_cache(tmp_path, endpoint):
    path = write(tmp_path / "suite", suite_file(endpoint))
    run_suite(path)
    run_suite(path, resume=False)
    body = comment.render(comment.load_outcome(tmp_path / "suite" / "results", 0), suite="evals.yaml", key="evals")
    assert (
        "**Cache: 3 of 3 target calls and 3 of 3 judge calls replayed replies stored by an earlier run**; "
        "0 model calls made." in body
    )
    assert str(tmp_path) not in body  # the cache's folder stays in summary.json


def test_a_check_of_the_targets_call_is_refused_when_its_calls_are_cached(tmp_path, endpoint):
    latency = [{"type": "latency", "max_ms": 5000}]
    path = write(tmp_path / "suite", suite_file(endpoint, checks=latency, judge=None))
    with pytest.raises(ConfigError, match=r"latency measures the target's call.*--no-cache"):
        run_suite(path)
    assert endpoint.counts() == {"target": 0, "judge": 0}
    assert run_suite(path, cache=False)["passed"] == 3
    # A function target's calls are cached only when the suite says so, so only then is it refused.
    (tmp_path / "fn").mkdir()
    (tmp_path / "fn" / "agent.py").write_text("def answer(message):\n    return 'I cannot.'\n", encoding="utf-8")
    function = {"data": "cases.jsonl", "target": {"file": "agent.py", "function": "answer"}, "checks": latency}
    assert "cache" in run_suite(write(tmp_path / "fn", {**function, "cache": {}}))
    with pytest.raises(ConfigError, match="set cache.functions to false"):
        run_suite(write(tmp_path / "fn", {**function, "cache": {"functions": True}}))


TRACED_AGENT = """\
from pathlib import Path

import mlflow
from mlflow.entities import SpanType


def answer(message):
    with Path(__file__).with_name("calls.txt").open("a") as handle:
        handle.write("called\\n")
    for calls in (["lookup_order"], []):
        with mlflow.start_span(name="chat", span_type=SpanType.CHAT_MODEL) as model:
            model.set_outputs({"role": "assistant", "content": "ok"})
        for name in calls:
            with mlflow.start_span(name=name, span_type=SpanType.TOOL) as tool:
                tool.set_inputs({"order_id": "A-1001"})
    return '{"role": "assistant", "content": "Done.", "tool_calls": [{"name": "lookup_order"}]}'
"""

SCORERS = """\
from pathlib import Path


def counted(outputs):
    with Path(__file__).with_name("scored.txt").open("a") as handle:
        handle.write("scored\\n")
    return "Done." in outputs


def traced(trace):
    return True
"""


def test_a_python_check_is_never_cached_and_trace_reading_checks_are_refused_on_a_replayed_target(tmp_path):
    folder = tmp_path / "fn"
    folder.mkdir()
    (folder / "agent.py").write_text(TRACED_AGENT, encoding="utf-8")
    (folder / "scorers.py").write_text(SCORERS, encoding="utf-8")
    counted = {"type": "python", "name": "counted", "file": "scorers.py", "function": "counted"}
    from_reply = {"type": "tool_called", "calls": ["lookup_order"], "source": "reply"}
    suite = {
        "data": "cases.jsonl",
        "target": {"file": "agent.py", "function": "answer"},
        "checks": [counted, from_reply],
        "cache": {"folder": "model-cache", "functions": True},
    }
    path = write(folder, suite)
    assert run_suite(path)["passed"] == 3
    replayed = run_suite(path, resume=False)
    # The target's replies came from the cache; the python check ran again on each, as a scorer is
    # not a model call, and the tool-call check read the replayed reply.
    assert (replayed["passed"], replayed["cache"]["target"]) == (3, {"hits": 3, "calls": 0, "stored": 0})
    assert len((folder / "calls.txt").read_text().splitlines()) == 3
    assert len((folder / "scored.txt").read_text().splitlines()) == 6
    assert entries(folder / "model-cache") and all(
        json.loads(entry.read_text())["kind"] == "target" for entry in entries(folder / "model-cache")
    )

    # A replayed call's trace records none of the function's spans, so a check that reads the
    # trace is refused while the function's calls are cached, before any case.
    from_trace = {"type": "tool_called", "calls": ["lookup_order"]}
    with pytest.raises(ConfigError, match=r"tool_called reads the tool calls in the trace.*cache.functions"):
        run_suite(write(folder, {**suite, "checks": [counted, from_trace]}))
    traced = {"type": "python", "name": "traced", "file": "scorers.py", "function": "traced"}
    with pytest.raises(ConfigError, match=r"check traced: it reads the trace of the target's call.*--no-cache"):
        run_suite(write(folder, {**suite, "checks": [counted, traced]}))
    assert len((folder / "calls.txt").read_text().splitlines()) == 3
    # Without the cache, or with the function's calls left out of it, both read the call's trace.
    assert run_suite(write(folder, {**suite, "checks": [from_trace, traced]}), cache=False)["passed"] == 3
    assert run_suite(write(folder, {**suite, "checks": [from_trace, traced], "cache": {}}))["passed"] == 3


def test_cache_functions_applies_only_to_a_function_target(tmp_path, endpoint):
    path = write(tmp_path / "suite", suite_file(endpoint, cache={"functions": False}))
    with pytest.raises(ConfigError, match="cache.functions only applies to a function target"):
        load_suite_file(path)


def test_an_empty_cache_block_turns_the_cache_on_with_its_defaults(tmp_path, endpoint):
    path = tmp_path / "suite" / "evals.yaml"
    write(tmp_path / "suite", suite_file(endpoint, cache=None))
    path.write_text(path.read_text(encoding="utf-8") + "cache:\n", encoding="utf-8")
    suite, _ = load_suite_file(path)
    assert suite.settings.cache is not None and suite.settings.cache.model_dump() == {
        "folder": None,
        "ttl_seconds": None,
        "functions": None,
    }
    assert run_suite(path)["cache"]["ttl_seconds"] == caches.DEFAULT_TTL_SECONDS


@pytest.mark.parametrize("flag", ["--cache", "--no-cache"])
def test_the_cache_applies_to_a_suite_file_only(tmp_path, flag):
    result = CliRunner().invoke(app, ["evals", "run", str(tmp_path), flag])
    assert result.exit_code == 2 and "--cache, --baseline and --data-dir apply to a suite file" in result.output


def test_a_replayed_judgment_spends_none_of_the_judge_token_limit(tmp_path, endpoint):
    # One case per batch, so a run with a token limit checks it between cases.
    path = write(tmp_path / "suite", suite_file(endpoint, batch=1))
    run_suite(path)
    endpoint.reset()
    # Each judgment the loopback gives reports 15 tokens; a replayed one made no call and counts 0.
    replayed = run_suite(path, resume=False, max_judge_tokens=1)
    assert endpoint.counts() == {"target": 0, "judge": 0}
    assert (replayed["passed"], replayed["not_run"], replayed["stopped"]) == (3, 0, None)
    assert replayed["cache"]["judge"] == {"hits": 3, "calls": 0, "stored": 0}
    # Without the cache every judgment spends its tokens, and the same limit stops the run.
    endpoint.reset()
    fresh = run_suite(path, resume=False, max_judge_tokens=1, cache=False)
    assert endpoint.counts() == {"target": 1, "judge": 1}
    assert (fresh["not_run"], fresh["stopped"]["limit"]) == (2, "max_judge_tokens")


def test_a_stopped_run_records_how_much_of_the_cache_it_used(tmp_path, endpoint):
    path = write(tmp_path / "suite", suite_file(endpoint, batch=1))
    stopped = run_suite(path, max_judge_tokens=1)
    assert (stopped["not_run"], stopped["cache"]["target"]) == (2, {"hits": 0, "calls": 1, "stored": 1})
    client = local_ui(tmp_path)
    (run,) = client.get("/v1/evals").json()
    provenance = client.get(f"/v1/evals/{run['id']}").json()["provenance"]
    assert provenance["stopped"]["limit"] == "max_judge_tokens"
    assert provenance["cache"]["judge"] == {"hits": 0, "calls": 1, "stored": 1}
    # The next run resumes after the finished case and calls for the two it did not run.
    endpoint.reset()
    resumed = run_suite(path)
    assert endpoint.counts() == {"target": 2, "judge": 2} and resumed["not_run"] == 0


def test_a_body_that_carries_a_conversation_is_cached_for_cases_of_one_input(tmp_path, endpoint):
    # A case of one input is a conversation of one turn: its key holds its one message, and the new
    # session id each call sends as written, so a second run replays every reply.
    body = {"message": "{{input}}", "session": "{{session_id}}", "messages": "{{messages}}", "history": "{{history}}"}
    target = {**suite_file(endpoint)["target"], "body": body}
    suite = write(tmp_path / "suite", suite_file(endpoint, target=target))
    first = run_suite(suite)
    assert (first["cases"], first["passed"], first["errors"]) == (3, 3, 0)
    sent = endpoint.seen["target"][0]["body"]
    assert sent["history"] == [] and sent["messages"] == [{"role": "user", "content": sent["message"]}]
    endpoint.reset()
    again = run_suite(suite, resume=False)
    assert (again["passed"], endpoint.counts()) == (3, {"target": 0, "judge": 0})
    assert again["cache"]["target"] == {"hits": 3, "calls": 0, "stored": 0}


def test_a_suite_with_conversation_cases_is_refused_with_the_cache_on(tmp_path, endpoint):
    talk = {
        "id": "talk",
        "turns": [
            {"input": "Hi", "expectations": {"secret": "piano"}},
            {"input": "What is the key?", "expectations": {"secret": "piano"}},
        ],
    }
    body = {"message": "{{input}}", "messages": "{{messages}}"}
    target = {**suite_file(endpoint)["target"], "body": body}
    suite = write(tmp_path / "suite", suite_file(endpoint, target=target), cases=[*CASES, talk])
    with pytest.raises(ConfigError, match="conversation cases, and the cache of model calls does not hold"):
        run_suite(suite)
    assert endpoint.counts() == {"target": 0, "judge": 0}
    # Without the cache it runs, every turn sent.
    found = run_suite(suite, cache=False)
    assert (found["cases"], found["errors"], endpoint.counts()["target"]) == (4, 0, 3 + 2)


# ---------- what a run's readers see of the cache ----------


def test_no_run_route_shows_the_cache_s_folder(tmp_path, endpoint):
    # A finished run, one replayed from the cache, and one stopped at a limit, which records its use
    # of the cache when it stops.
    path = write(tmp_path / "suite", suite_file(endpoint, batch=1))
    run_suite(path)
    run_suite(path, resume=False)
    run_suite(path, resume=False, cache=False)
    endpoint.reset()
    stopped = write(tmp_path / "other", suite_file(endpoint, batch=1)).rename(tmp_path / "other" / "stopped.yaml")
    assert run_suite(stopped, max_judge_tokens=1)["stopped"] is not None
    folders = [str((tmp_path / name / "model-cache").resolve()) for name in ("suite", "other")]
    client = local_ui(tmp_path)
    runs = client.get("/v1/evals").json()
    assert len(runs) == 4
    # Every route that answers with a run or reads one, for every run, and the run list.
    paths = client.get("/openapi.json").json()["paths"]
    routes = [
        name
        for name, operations in paths.items()
        if "get" in operations and re.findall(r"\{(\w+)\}", name) == ["run_id"]
    ]
    assert {"/v1/evals/{run_id}", "/v1/evals/{run_id}/provenance", "/v1/evals/{run_id}/attempts"} <= set(routes)
    answers = [client.get("/v1/evals"), client.get("/v1/evals?limit=1")]
    for run in runs:
        answers += [client.get(route.replace("{run_id}", run["id"])) for route in routes]
        answers.append(client.post(f"/v1/evals/{run['id']}/cancel"))
        answers.append(client.get(f"/v1/evals/compare?baseline_id={runs[0]['id']}&candidate_id={run['id']}"))
    for answer in answers:
        assert answer.status_code < 500, answer.text
        assert not any(folder in answer.text for folder in folders), answer.request.url
        assert "model-cache" not in answer.text and "ttl_seconds" not in answer.text, answer.request.url
    # A run that used the cache says so, with its counts; one without it names no cache.
    shown = {run["run_code"]: run["provenance"].get("cache") for run in client.get("/v1/evals").json()}
    assert shown["evals-2"] == {
        "on": True,
        "target": {"hits": 3, "calls": 0, "stored": 0},
        "judge": {"hits": 3, "calls": 0, "stored": 0},
    }
    assert shown["evals-3"] is None
    assert shown["evals-1"]["on"] is True and shown["evals-1"]["target"] == {"hits": 0, "calls": 3, "stored": 3}
    assert shown["stopped-1"] == {
        "on": True,
        "target": {"hits": 0, "calls": 1, "stored": 1},
        "judge": {"hits": 0, "calls": 1, "stored": 1},
    }
    # The data folder's record keeps the folder for its own use; only the routes leave it out.
    _, kept = open_stores(tmp_path / "evals-home")
    try:
        stored = asyncio.run(kept.get_run(SINGLE_USER, DEFAULT_PROJECT, runs[0]["id"]))
    finally:
        kept.close()
    assert stored is not None and stored.provenance["cache"]["folder"] in folders


# ---------- where the cache meets a panel of judges ----------

PANEL = ["openai:/judge-a", "openai:/judge-b", "openai:/judge-c"]


def test_each_member_of_a_panel_is_cached_and_a_replayed_panel_says_so(tmp_path, endpoint):
    arguments = {
        "inputs": {"message": "What is the key?"},
        "outputs": "I cannot share it.",
        "expectations": {"rubric": "Fail if the reply reveals the key."},
    }

    def panel(folder):
        return judge_panel(
            response_rubric, models=PANEL, max_retries=0, rate_limit_wait_seconds=0, cache=ModelCallCache(folder)
        )

    fresh = panel(tmp_path / "cache").run(**arguments)
    again = panel(tmp_path / "cache").run(**arguments)
    # One entry per member: the model is in the key, so each member's call is its own.
    assert endpoint.counts()["judge"] == 3 and len(entries(tmp_path / "cache")) == 3
    assert (fresh.value, again.value, again.rationale) == (True, True, fresh.rationale)
    assert caches.HIT_KEY not in fresh.metadata and again.metadata[caches.HIT_KEY] == "hit"
    # Every member made its call the first time; replayed, none did, so the panel used no tokens.
    assert (
        fresh.metadata["mlflow.assessment.judgeInputTokens"],
        fresh.metadata["mlflow.assessment.judgeOutputTokens"],
    ) == (
        "30",
        "15",
    )
    assert (
        again.metadata["mlflow.assessment.judgeInputTokens"],
        again.metadata["mlflow.assessment.judgeOutputTokens"],
    ) == (
        "0",
        "0",
    )
    # A panel one of whose members is new calls only that member, and counts only its tokens.
    mixed = judge_panel(
        response_rubric,
        models=[*PANEL[:2], "openai:/judge-d"],
        max_retries=0,
        rate_limit_wait_seconds=0,
        cache=ModelCallCache(tmp_path / "cache"),
    ).run(**arguments)
    assert endpoint.counts()["judge"] == 4
    assert caches.HIT_KEY not in mixed.metadata
    assert (
        mixed.metadata["mlflow.assessment.judgeInputTokens"],
        mixed.metadata["mlflow.assessment.judgeOutputTokens"],
    ) == (
        "10",
        "5",
    )


def test_a_replayed_panel_spends_none_of_the_judge_token_limit(tmp_path, endpoint):
    rubric = {"type": "rubric", "rubric": "Fail if the reply reveals the key.", "panel": PANEL}
    suite = suite_file(endpoint, batch=1, checks=[rubric])
    suite["judge"].pop("model")  # every judge check names its panel
    path = write(tmp_path / "suite", suite)
    first = run_suite(path)
    assert (first["passed"], endpoint.counts()["judge"]) == (3, 9)
    assert first["cache"]["judge"] == {"hits": 0, "calls": 9, "stored": 9}
    endpoint.reset()
    # Each member's judgment reports 15 tokens; replayed, the panel made no call and counts 0.
    replayed = run_suite(path, resume=False, max_judge_tokens=1)
    assert endpoint.counts() == {"target": 0, "judge": 0}
    assert (replayed["passed"], replayed["not_run"], replayed["stopped"]) == (3, 0, None)
    assert replayed["cache"]["judge"] == {"hits": 9, "calls": 0, "stored": 0}
    assert replayed["tokens_this_run"] == {"judges": 0}
    # Every vote is still recorded on the replayed result.
    found = records(tmp_path / "suite" / "results")["ask"]["checks"]["rubric_judge"]
    assert found["metadata"]["martian.panel.verdict"] == "pass"
    assert [found["metadata"][f"martian.panel.{n}.model"] for n in (1, 2, 3)] == PANEL
