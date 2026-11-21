"""Run converted provider declarations through the SDK's native HTTP target."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
import yaml

from martian.evals.promptfoo_convert import PromptfooConfigError, convert_promptfoo_config, write_conversion
from martian.evals.runner import run_suite
from martian.evals.suite_file import load_suite_file


@pytest.fixture
def endpoint():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            authorized = self.headers.get("Authorization") == "Bearer fixture-key"
            calls.append((self.path, authorized, body))
            if not authorized:
                self.send_response(401)
                self.end_headers()
                return
            if self.path == "/v1/chat/completions":
                data = {"choices": [{"message": {"content": "Ottawa"}}]}
            else:
                data = {"items": [{"reply-text": "Ottawa"}]}
            payload = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", calls
        finally:
            server.shutdown()
            thread.join()


def config_file(folder, declaration, *, prompts=None, tests=None, **extra):
    path = folder / "promptfooconfig.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "providers": [declaration],
                "prompts": prompts or ["Capital of {{place}}?"],
                "tests": tests or [{"vars": {"place": "Canada"}, "assert": [{"type": "equals", "value": "Ottawa"}]}],
                **extra,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_one_converted_run_scores_live_and_stored_outputs(tmp_path, endpoint, monkeypatch):
    base, calls = endpoint
    monkeypatch.setenv("MIGRATION_FIXTURE_KEY", "fixture-key")
    config = config_file(
        tmp_path,
        {
            "id": "http",
            "config": {
                "url": f"{base}/answer",
                "method": "POST",
                "headers": {"Authorization": "Bearer {{env.MIGRATION_FIXTURE_KEY}}"},
                "body": {"question": "{{prompt}}"},
                "transformResponse": "(payload) => payload.items[0]['reply-text']",
            },
        },
        tests=[
            {"vars": {"place": "Canada"}, "assert": [{"type": "equals", "value": "Ottawa", "metric": "correct"}]},
            {
                "vars": {"place": "France"},
                "providerOutput": "Paris",
                "assert": [{"type": "contains", "value": "Paris", "metric": "correct"}],
            },
        ],
        derivedMetrics=[{"name": "accuracy", "value": "correct / __count"}],
    )
    out = tmp_path / "out"
    conversion = convert_promptfoo_config(config, output=out)
    write_conversion(conversion, out, strict=True)
    (one,) = conversion.suites
    result = run_suite(out / one.suite_file, home=tmp_path / "store")
    assert (result["passed"], result["errors"], result["derived_metrics"][0]["score"]) == (2, 0, 1)
    assert calls == [("/answer", True, {"question": "Capital of Canada?"})]


def test_http_provider_preserves_request_shape_and_replays_recorded_cases(tmp_path, endpoint, monkeypatch):
    base, calls = endpoint
    monkeypatch.setenv("MIGRATION_FIXTURE_URL", base)
    monkeypatch.setenv("MIGRATION_FIXTURE_KEY", "fixture-key")
    case = {
        "vars": {"place": "Canada", "region": {"name": "North America"}, "limit": 2},
        "assert": [{"type": "equals", "value": "Ottawa"}],
    }
    config = config_file(
        tmp_path,
        {
            "id": "https",
            "config": {
                "url": "{{ env.MIGRATION_FIXTURE_URL }}/answer",
                "method": "POST",
                "headers": {"Authorization": "Bearer {{env.MIGRATION_FIXTURE_KEY}}"},
                "body": [
                    {"text": "{{prompt}}", "region": "{{region.name}}", "limit": "{{limit}}", "roles": '["user"]'}
                ],
                "transformResponse": "(payload) => payload.items[0]['reply-text']",
            },
        },
        tests=[case, {**case, "providerOutput": "Ottawa"}],
    )
    out = tmp_path / "out"
    conversion = convert_promptfoo_config(config, output=out)
    write_conversion(conversion, out, strict=True)
    assert calls == []
    live, replay = conversion.suites
    suite, _ = load_suite_file(out / live.suite_file)
    assert suite.target is not None and suite.target.kind == "http"
    assert suite.target.url == "${MIGRATION_FIXTURE_URL}/answer"
    assert suite.target.headers["authorization"] == "Bearer ${MIGRATION_FIXTURE_KEY}"
    assert base not in live.suite and "fixture-key" not in live.suite
    assert run_suite(out / live.suite_file, home=tmp_path / "store")["passed"] == 1
    assert run_suite(out / replay.suite_file, home=tmp_path / "store")["passed"] == 1
    assert calls == [
        ("/answer", True, [{"text": "Capital of Canada?", "region": "North America", "limit": "2", "roles": ["user"]}])
    ]
    monkeypatch.setenv("MIGRATION_FIXTURE_URL", base + "/second")
    rerun = run_suite(out / live.suite_file, home=tmp_path / "store")
    assert rerun["passed"] == rerun["invalidated"] == 1
    assert calls[-1] == ("/second/answer", *calls[0][1:])


def test_chat_provider_preserves_system_messages_and_generation_options(tmp_path, endpoint, monkeypatch):
    base, calls = endpoint
    monkeypatch.setenv("MIGRATION_FIXTURE_KEY", "fixture-key")
    config = config_file(
        tmp_path,
        {
            "id": "openai:chat:local-fixture",
            "config": {
                "apiBaseUrl": f"{base}/v1/",
                "apiKeyEnvar": "MIGRATION_FIXTURE_KEY",
                "temperature": 0.25,
                "max_tokens": 45,
                "response_format": {"type": "json_object"},
            },
        },
        prompts=['[{"role":"system","content":"Return a city."},{"role":"user","content":"Capital of {{place}}?"}]'],
    )
    out = tmp_path / "out"
    write_conversion(convert_promptfoo_config(config, output=out), out, strict=True)
    assert run_suite(out / "evals.yaml", home=tmp_path / "store")["passed"] == 1
    assert calls == [
        (
            "/v1/chat/completions",
            True,
            {
                "model": "local-fixture",
                "messages": [
                    {"role": "system", "content": "Return a city."},
                    {"role": "user", "content": "Capital of Canada?"},
                ],
                "temperature": 0.25,
                "max_tokens": 45,
                "response_format": {"type": "json_object"},
            },
        )
    ]
    monkeypatch.setenv("MIGRATION_FIXTURE_KEY", "wrong-key")
    rejected = run_suite(out / "evals.yaml", home=tmp_path / "store", resume=False)
    assert rejected["errors"] == 1
    assert any("HTTP 401" in error for error in rejected["target_errors"])


@pytest.mark.parametrize(
    ("patch", "diagnostic"),
    [
        ({"headers": {"Authorization": "Bearer inline-example"}}, "environment references for credential headers"),
        ({"transformResponse": "json.items.map(item => item.reply)"}, "response selector must be a JSON property path"),
        ({"method": "GET"}, "requires method: POST"),
    ],
)
def test_unrepresentable_http_config_leaves_no_files(tmp_path, patch, diagnostic):
    config = config_file(
        tmp_path,
        {
            "id": "http",
            "config": {
                "url": "http://127.0.0.1:1/answer",
                "method": "POST",
                "body": {"message": "{{prompt}}"},
                **patch,
            },
        },
    )
    out = tmp_path / "out"
    conversion = convert_promptfoo_config(config, output=out)
    with pytest.raises(PromptfooConfigError, match=diagnostic) as error:
        write_conversion(conversion, out, strict=True)
    assert "inline-example" not in str(error.value)
    assert not out.exists()
