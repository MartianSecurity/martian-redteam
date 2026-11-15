"""Simulated users (MartianSecurity/martian-notes#192): a conversation case that names a goal, a persona, a
turn budget and a stop condition instead of scripted turns, played by a model on the judges'
provider path, recorded turn by turn as a scripted conversation is, with its goal verdict beside
the conversation's checks; and personas built from real transcripts with personal data removed.

A loopback OpenAI-compatible server is the fake model: it plays the simulated user by a fixed script
chosen from the persona it is given, and writes a persona when asked to read a transcript. Nothing
calls a real model."""

import asyncio
import json
import shutil
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
from martian.evals import runner
from martian.evals._policy import personal_data, redact_personal_data
from martian.evals.local import create_local_app, open_stores
from martian.evals.model import Case, Run, SimulatedUser, from_results_record
from martian.evals.personas import Transcript, build_personas, read_transcripts, transcripts_from_store
from martian.evals.runner import run_suite
from martian.evals.server.principal import SINGLE_USER
from martian.evals.simulation import GOAL
from martian.evals.state import STATE
from martian.evals.suite_file import cases_to_jsonl, load_suite_file, suite_to_yaml
from martian.evals.worker import execute
from tests.evals.evals_contract import validate_response

REPOSITORY = Path(__file__).resolve().parents[2]
EXAMPLE = REPOSITORY / "examples" / "evals" / "simulated-users"
CONVERSATION = REPOSITORY / "examples" / "evals" / "conversation"
KEY = "fixture-simulator-key"
MODEL = "your-model"  # the example's simulator.model is openai:/your-model

# What the fake model sends as each persona: the example's clear customer asks straight away; its
# chatty one gives the number first and asks to cancel two turns later, when the assistant has
# forgotten it.
SCRIPTS = {
    "polite": ["Please cancel my order A-1002."],
    "chatty": [
        "Hi, my order A-1002 was due last week.",
        "Is it going to arrive soon?",
        "Then please cancel it.",
        "I already gave you the number. Cancel it.",
    ],
}


class FakeModel:
    """A fake OpenAI-compatible endpoint. ``behaviour`` changes how it answers."""

    def __init__(self):
        self.requests = []
        self.behaviour = {"fail": None, "persona": None, "silent": False, "usage": True}
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
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
    def base(self):
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    def answer(self, headers, body):
        if self.behaviour["fail"]:
            return self.behaviour["fail"], {"error": {"message": "private provider text"}}
        if headers.get("Authorization") != f"Bearer {KEY}":
            return 401, {"error": {"message": "Invalid API key"}}
        system, user = (message["content"] for message in body["messages"])
        reply = self.persona(user) if system.startswith("You read a real conversation") else self.step(system, user)
        message = {"role": "assistant", "content": json.dumps(reply)}
        completion = {
            "id": "chatcmpl-fake",
            "object": "chat.completion",
            "created": 0,
            "model": body["model"],
            "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        }
        if self.behaviour["usage"]:
            completion["usage"] = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        return 200, completion

    def step(self, system, user):
        played = json.loads(system.split("The user you play:\n", 1)[1].split("\nOutput schema:", 1)[0])
        asked = json.loads(user)
        replies = [turn["text"] for turn in asked["conversation"] if turn["from"] == "assistant"]
        reached = any("cancelled" in text for text in replies)
        if self.behaviour["silent"]:
            return {"message": "", "done": False, "goal_reached": False, "reason": "Nothing to say."}
        if asked["budget_spent"] or (replies and reached):
            why = "The assistant cancelled the order." if reached else "The assistant kept asking for the number."
            return {"message": "", "done": True, "goal_reached": reached, "reason": why}
        script = SCRIPTS["chatty" if "chatty" in played["persona"] else "polite"]
        return {"message": script[asked["messages_sent"]], "done": False, "goal_reached": False, "reason": "Go on."}

    def persona(self, user):
        conversation = json.loads(user)["conversation"]
        first = next(message["content"] for message in conversation if message["role"] == "user")
        return self.behaviour["persona"] or {
            "goal": f"Get help with: {first[:40]}",
            # A model can write a value that identifies someone even when told not to.
            "persona": "A terse customer who signs off as jane.doe@example.com.",
            "stop_when": "The assistant answers the question.",
        }

    def simulated(self):
        return [r for r in self.requests if r["body"]["messages"][0]["content"].startswith("You play a user")]


@pytest.fixture
def fake(monkeypatch, tmp_path):
    for name in ("MARTIAN_API_URL", "MARTIAN_API_TOKEN", "MARTIAN_AGENT_ID", "OPENAI_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MLFLOW_DISABLE_TELEMETRY", "true")
    monkeypatch.setenv("MARTIAN_EVALS_HOME", str(tmp_path / "evals-home"))
    monkeypatch.setattr(mlflow.tracking.fluent, "_active_experiment_id", None)
    old_uri = mlflow.get_tracking_uri()
    STATE.reset()
    model = FakeModel()
    monkeypatch.setenv("OPENAI_API_BASE", model.base)
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    try:
        yield model
    finally:
        model.server.shutdown()
        model.server.server_close()
        STATE.reset()
        mlflow.set_tracking_uri(old_uri)


def example(folder: Path) -> Path:
    """The documented example, copied unchanged."""
    for name in ("evals.yaml", "cases.jsonl", "agent.py"):
        shutil.copy(EXAMPLE / name, folder / name)
    return folder / "evals.yaml"


def results_of(folder: Path) -> dict[str, dict]:
    lines = (folder / "results/results.jsonl").read_text().splitlines()
    return {record["id"]: record for record in map(json.loads, lines)}


def local_ui(folder: Path) -> TestClient:
    ui = folder / "ui"
    ui.mkdir(exist_ok=True)
    (ui / "index.html").write_text("<!doctype html><title>Martian Evals</title>", encoding="utf-8")
    return TestClient(create_local_app(folder / "evals-home", ui), base_url="http://127.0.0.1:7600")


# ---------- a simulated conversation reaches or fails its goal ----------


def test_a_simulated_user_reaches_or_fails_its_goal_and_is_recorded_turn_by_turn(tmp_path, fake):
    summary = run_suite(example(tmp_path))
    found = results_of(tmp_path)
    clearly, chatty = found["cancel-clearly"], found["cancel-after-chat"]

    # The clear customer is done after one turn; the chatty one spends its budget of four, and
    # the assistant has forgotten the number by the time it asks to cancel.
    assert [turn["inputs"] for turn in clearly["turns"]] == [{"message": "Please cancel my order A-1002."}]
    assert [turn["inputs"]["message"] for turn in chatty["turns"]] == SCRIPTS["chatty"]
    assert chatty["turns"][2]["output"] == "I can help with that. What is your order number?"
    assert clearly["checks"][GOAL] == {
        "passed": True,
        "rationale": "The simulated user stopped after 1 message and reached its goal: The assistant cancelled "
        "the order.",
        "error": None,
    }
    assert chatty["checks"][GOAL]["passed"] is False
    assert chatty["checks"][GOAL]["rationale"].startswith("With its budget of 4 messages spent, the simulated user")
    # The goal is a conversation-level result: it decides the case beside the checks, which passed.
    assert (clearly["passed"], chatty["passed"]) == (True, False)
    assert chatty["checks"]["short"]["passed"] is True and chatty["checks"]["no_other_orders"]["passed"] is True
    assert clearly["simulated"] is True and chatty["simulated"] is True
    assert chatty["output"][0] == {"role": "user", "content": SCRIPTS["chatty"][0]}

    # One simulator call per message, and one more when the budget is spent or it stops, each
    # asking for the flat reply on the provider path with the key from the environment.
    asked = fake.simulated()
    assert len(asked) == (1 + 1) + (4 + 1)
    assert {r["body"]["response_format"]["type"] for r in asked} == {"json_schema"}
    schema = asked[0]["body"]["response_format"]["json_schema"]["schema"]
    assert set(schema["properties"]) == {"message", "done", "goal_reached", "reason"}
    assert all(field.get("type") in ("string", "boolean") for field in schema["properties"].values())

    # The run says which simulator played the users, and the goal is counted as a conversation check.
    assert summary["simulator"] | {"confidence_95": None} == {
        "model": f"openai:/{MODEL}",
        "parameters": {"temperature": 0.7},
        "version": "simulated-user-v1",
        "personas": {"data": "cases.jsonl", "digest": load_suite_file(tmp_path / "evals.yaml")[0].dataset_digest},
        "cases": 2,
        "passed": 1,
        "failed": 1,
        "errors": 0,
        "pass_rate": 0.5,
        "confidence_95": None,
    }
    assert summary["checks"][GOAL] | {"pass_rate": None, "confidence_95": None} == {
        "passed": 1,
        "failed": 1,
        "errors": 0,
        "pass_rate": None,
        "confidence_95": None,
        "scope": "conversation",
    }
    assert summary["conversations"]["turns"]["total"] == 5

    # In the local Evals UI: the turns route, a trace per turn under the conversation's session,
    # tagged as simulated, the provenance and the goal score naming the simulator model.
    client = local_ui(tmp_path)
    run_id = summary["run"]["id"]
    detail = client.get(f"/v1/evals/{run_id}").json()
    validate_response("get", "/v1/evals/{run_id}", detail)
    # Simulated conversations count among the run's conversations, so the UI asks for their turns.
    assert detail["provenance"]["conversations"] == 2
    assert {item["dataset_item_id"]: item["status"] for item in detail["items"]} == {
        "cancel-clearly": "passed",
        "cancel-after-chat": "failed",
    }
    talks = {item["dataset_item_id"]: item for item in client.get(f"/v1/evals/{run_id}/turns").json()["items"]}
    shown = talks["cancel-after-chat"]
    assert (shown["simulated"], shown["session_id"], len(shown["turns"])) == (True, chatty["session_id"], 4)
    assert shown["checks"][GOAL]["passed"] is False
    assert [turn["prompt"] for turn in shown["turns"]] == SCRIPTS["chatty"]
    traces = client.post("/v1/evals/traces/search", json={"limit": 50}).json()["items"]
    assert sorted(t["name"] for t in traces) == sorted(
        ["evals-1 cancel-clearly turn 1", *(f"evals-1 cancel-after-chat turn {n}" for n in range(1, 5))]
    )
    _, records = open_stores(tmp_path / "evals-home")
    try:
        run = asyncio.run(records.get_run(SINGLE_USER, "default", run_id))
        assert isinstance(run, Run)
        assert run.provenance["simulator"] == {
            "model": f"openai:/{MODEL}",
            "parameters": {"temperature": 0.7},
            "version": "simulated-user-v1",
        }
        goal = next(scorer for scorer in run.scorers if scorer.name == GOAL)
        assert (goal.kind, goal.model, goal.scope) == ("llm_judge", f"openai:/{MODEL}", "conversation")
        scores = asyncio.run(records.list_scores(SINGLE_USER, "default", run_id))
        assert {score.source_id for score in scores if score.scorer == GOAL} == {f"openai:/{MODEL}"}
        results = {r.case_id: r for r in asyncio.run(records.list_results(SINGLE_USER, "default", run_id)).items}
        assert results["cancel-after-chat"].simulated is True
    finally:
        records.close()
    store, _ = open_stores(tmp_path / "evals-home")
    turn = asyncio.run(store.get_trace(SINGLE_USER, "default", shown["turns"][0]["trace_id"]))
    assert turn is not None and turn.tags["martian.simulated"] == "true" and turn.session_id == chatty["session_id"]
    assert KEY not in (tmp_path / "results/results.jsonl").read_text()


def test_the_command_and_the_pull_request_comment_name_the_simulator(tmp_path, fake):
    import importlib.util
    import sys

    result = CliRunner().invoke(app, ["evals", "run", str(example(tmp_path))])
    assert result.exit_code == 1  # cancel-after-chat did not reach its goal
    printed = " ".join(result.output.split())
    assert GOAL in printed and "2 conversations have a simulated user, played by openai:/your-model" in printed
    spec = importlib.util.spec_from_file_location("evals_pr_comment", REPOSITORY / "scripts/evals_pr_comment.py")
    assert spec is not None and spec.loader is not None
    comment = importlib.util.module_from_spec(spec)
    sys.modules["evals_pr_comment"] = comment
    spec.loader.exec_module(comment)
    body = comment.render(comment.load_outcome(tmp_path / "results", 1), suite="evals.yaml", key="evals.yaml")
    assert "| `goal_reached` | 1 | 1 | 0 |" in body
    assert "2 conversations have a simulated user, played by `openai:/your-model`" in body


def test_a_simulator_that_fails_ends_the_conversation_without_a_verdict(tmp_path, fake):
    fake.behaviour["fail"] = 401
    summary = run_suite(example(tmp_path))
    record = results_of(tmp_path)["cancel-clearly"]
    # No turn was written, so none was sent, and nothing has a verdict.
    assert (record["passed"], record["turns"]) == (None, [])
    assert record["error"] == (
        "Simulated user failed [judge_provider_auth]. The endpoint refused the credentials. "
        "Check OPENAI_API_KEY and simulator.headers."
    )
    assert record["checks"][GOAL]["error"] == "turn 1 did not complete"
    assert summary["errors"] == 2 and "private provider text" not in (tmp_path / "results/results.jsonl").read_text()


def test_a_simulated_user_that_neither_writes_nor_stops_is_an_error(tmp_path, fake):
    fake.behaviour["silent"] = True
    run_suite(example(tmp_path))
    assert results_of(tmp_path)["cancel-clearly"]["error"].startswith(
        "Simulated user wrote no message and did not end the conversation [simulator_no_message]"
    )


def test_with_repeats_each_attempt_is_a_simulated_conversation_of_its_own(tmp_path, fake):
    path = example(tmp_path)
    # A case of one input beside them: only conversations have a goal verdict to combine.
    agent = tmp_path / "agent.py"
    agent.write_text(agent.read_text().replace("def reply(message, history):", "def reply(message, history=()):"))
    with (tmp_path / "cases.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps({"id": "one", "input": "Where is A-1001?", "expectations": {"other_orders": ["A-1002"]}}) + "\n"
        )
    summary = run_suite(path, repeats=2)
    found = results_of(tmp_path)
    assert found["one"]["passed"] is True and GOAL not in found["one"]["checks"]
    attempts = found["cancel-after-chat"]["attempts"]
    assert len({attempt["session_id"] for attempt in attempts}) == 2
    assert [attempt["checks"][GOAL]["passed"] for attempt in attempts] == [False, False]
    assert found["cancel-after-chat"]["checks"][GOAL]["rationale"].startswith("0 of 2 attempts passed")
    assert summary["repeats"]["checks"][GOAL] == {"combine": "all", "from": "suite"}
    assert summary["simulator"]["cases"] == 2 and summary["conversations"]["turns"]["total"] == 10


def test_changing_the_simulator_grades_only_the_simulated_cases_again(tmp_path, fake):
    path = example(tmp_path)
    scripted = json.loads((CONVERSATION / "cases.jsonl").read_text().splitlines()[0])
    with (tmp_path / "cases.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(scripted) + "\n")
    assert run_suite(path)["conversations"]["cases"] == 3
    path.write_text(path.read_text().replace("temperature: 0.7", "temperature: 0.2"), encoding="utf-8")
    messages: list[str] = []
    again = run_suite(path, progress=messages.append)
    assert again["invalidated"] == 2 and messages[0].startswith("3 cases, 1 already done")
    assert "2 with a simulated user played by openai:/your-model" in messages[0]


# ---------- the run's token limit counts the simulator (MartianSecurity/martian-notes#359) ----------


def test_a_run_with_simulated_users_stops_at_its_token_limit_counting_the_simulator(tmp_path, fake):
    path = example(tmp_path)
    # Each simulator reply reports 10 + 5 tokens. Both users write their first message (30 tokens);
    # then the clear customer ends its conversation and the chatty one writes its second (60), past
    # the limit of 45, so that turn is not sent and the run stops. The example has no LLM judge.
    summary = run_suite(path, max_judge_tokens=45)
    assert len(fake.simulated()) == 4
    assert (summary["cases"], summary["passed"], summary["not_run"]) == (1, 1, 1)
    assert set(results_of(tmp_path)) == {"cancel-clearly"}
    assert summary["stopped"] == {
        "limit": "max_judge_tokens",
        "value": 45,
        "used": 60,
        "message": "Stopped at its judge token limit (max_judge_tokens 45) after 60 tokens "
        "(0 by its judges, 60 by its simulated users)",
    }
    assert summary["tokens_this_run"] == {"judges": 0, "simulator": 60}
    (run,) = local_ui(tmp_path).get("/v1/evals").json()
    assert (run["state"], run["error"]) == ("stopped", summary["stopped"]["message"])

    # Run again without the limit: the finished conversation is kept, the other talks from the start.
    messages: list[str] = []
    again = run_suite(path, progress=messages.append)
    assert messages[0].startswith("2 cases, 1 already done")
    assert (again["cases"], again["not_run"], again["stopped"]) == (2, 0, None)
    # Four messages and the goal verdict, each run counting its own.
    assert again["tokens_this_run"] == {"judges": 0, "simulator": 75}


def test_a_simulator_that_reports_no_usage_stops_a_run_with_a_token_limit(tmp_path, fake):
    fake.behaviour["usage"] = False
    path = example(tmp_path)
    # A suite with simulated users and no LLM judge can set the limit in its file.
    path.write_text(path.read_text() + "limits:\n  max_judge_tokens: 1000\n", encoding="utf-8")
    summary = run_suite(path)
    assert len(fake.simulated()) == 2 and summary["not_run"] == 2
    assert summary["stopped"]["message"] == (
        "Stopped: the simulated user reported no token usage, so its token limit (max_judge_tokens) cannot be kept"
    )
    assert summary["tokens_this_run"] == {"judges": 0, "simulator": None}


# ---------- the suite file and the model ----------


def test_a_simulated_suite_is_the_model_and_round_trips(tmp_path):
    loaded, cases = load_suite_file(example(tmp_path))
    assert loaded.simulator is not None and loaded.simulator.model == f"openai:/{MODEL}"
    assert cases[0].simulated_user == SimulatedUser(
        goal="Cancel order A-1002 and be told you will be refunded.",
        persona=cases[0].simulated_user.persona,
        max_turns=4,
        stop_when="The assistant confirms the cancellation, or says it cannot cancel the order.",
    )
    (tmp_path / "again.jsonl").write_text(cases_to_jsonl(cases), encoding="utf-8")
    text = suite_to_yaml(loaded.model_copy(update={"dataset_location": "again.jsonl"}))
    assert yaml.safe_load(text)["simulator"] == {"model": f"openai:/{MODEL}", "inference_params": {"temperature": 0.7}}
    (tmp_path / "again.yaml").write_text(text, encoding="utf-8")
    reloaded, again = load_suite_file(tmp_path / "again.yaml")
    assert again == cases and reloaded.simulator == loaded.simulator
    # A suite and a case without a simulator keep the digests they had.
    assert "simulator" not in loaded.model_copy(update={"simulator": None}).model_dump()
    assert "simulated_user" not in Case(id="a", inputs={"message": "hi"}).model_dump()
    assert "simulator_headers" not in loaded.settings.model_dump()
    # A results record of a simulated conversation maps onto the model.
    record = {"id": "a", "passed": True, "checks": {}, "session_id": "s", "turns": [], "simulated": True}
    assert from_results_record(record, run_id="r", position=0)[0].simulated is True


SIMULATED = {"goal": "g", "persona": "p", "max_turns": 2, "stop_when": "s"}


@pytest.mark.parametrize(
    ("case", "suite", "problem"),
    [
        ({"id": "a", "simulated_user": SIMULATED}, "", "has a simulated user, which a model plays"),
        (
            {"id": "a", "simulated_user": SIMULATED},
            "  - type: length\n    name: goal_reached\n    max_length: 9\n",
            "goal",
        ),
        ({"id": "a", "simulated_user": SIMULATED, "turns": [{"input": "hi"}]}, "", "remove turns"),
        ({"id": "a", "simulated_user": SIMULATED, "input": "hi"}, "", "remove input"),
        ({"id": "a", "simulated_user": {**SIMULATED, "mood": "x"}}, "", "has no field mood"),
        ({"id": "a", "simulated_user": {**SIMULATED, "max_turns": 0}}, "", "from 1 to 100"),
        ({"id": "a", "simulated_user": {**SIMULATED, "goal": " "}}, "", "goal must be a nonempty string"),
    ],
)
def test_a_simulated_user_the_suite_cannot_run_is_refused_before_any_turn(tmp_path, case, suite, problem):
    (tmp_path / "cases.jsonl").write_text(json.dumps(case) + "\n", encoding="utf-8")
    (tmp_path / "agent.py").write_text("def target(message, history):\n    return message\n", encoding="utf-8")
    simulator = "" if problem.startswith("has a simulated user") else "simulator:\n  model: openai:/m\n"
    checks = suite or "  - type: length\n    max_length: 9\n"
    (tmp_path / "evals.yaml").write_text(
        f"data: cases.jsonl\ntarget:\n  file: agent.py\n{simulator}checks:\n{checks}", encoding="utf-8"
    )
    with pytest.raises(ConfigError, match=problem):
        run_suite(tmp_path / "evals.yaml")
    assert not (tmp_path / "results").exists()


def test_the_worker_skips_a_simulated_case_with_its_reason():
    async def never(case):
        raise AssertionError("a simulated conversation is not sent")

    run = Run(id="r", project_id="default", source="console", state="running")
    case = Case(id="c", simulated_user=SimulatedUser(**SIMULATED))
    result, score, model = asyncio.run(execute._run_case(None, SINGLE_USER, run, 0, case, never, 5))  # type: ignore[arg-type]
    assert (result.status, result.error, score, model) == ("skipped", execute.CONVERSATION, None, None)


# ---------- personas from real transcripts ----------


def test_personal_data_is_replaced_by_its_kind():
    text = "Mail jane.doe@example.com or call +44 20 7946 0958; card 4111 1111 1111 1111. I am Jane."
    cleaned, counts = redact_personal_data(text)
    assert cleaned == "Mail [email] or call [phone]; card [card]. I am Jane."
    assert counts == {"email": 1, "phone": 1, "card": 1}
    assert not list(personal_data(cleaned, ("email", "phone", "card", "iban", "national_id")))


def test_personas_from_a_transcripts_file_have_no_personal_data_the_check_finds(tmp_path, fake):
    transcripts = tmp_path / "transcripts.jsonl"
    lines = [
        {
            "id": "chat:jane.doe@example.com",
            "messages": [
                {"role": "system", "content": "You are a support assistant."},
                {"role": "user", "content": "My email is jane.doe@example.com and my order is late."},
                {"role": "assistant", "content": "Sorry to hear that. Which order?"},
                {"role": "user", "content": "A-1002. Call me on +44 20 7946 0958."},
            ],
        },
        {"messages": [{"role": "user", "content": "Where is my parcel?"}]},
    ]
    transcripts.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    output = tmp_path / "personas.jsonl"
    result = CliRunner().invoke(
        app, ["evals", "personas", str(transcripts), "--model", f"openai:/{MODEL}", "-o", str(output)]
    )
    assert result.exit_code == 0, result.output
    assert "Wrote 2 personas" in result.output
    assert "Personal data removed: 4 email addresses and 1 phone number" in result.output
    assert "Names and street addresses are not found" in result.output

    # The model never read the values, and nothing written holds one the check finds.
    sent = json.dumps([r["body"] for r in fake.requests])
    assert "jane.doe@example.com" not in sent and "7946" not in sent and "[email]" in sent
    assert "support assistant" not in sent  # the system message is the assistant's, not the user's
    written = output.read_text()
    assert not list(personal_data(written, ("email", "phone", "card", "iban", "national_id")))
    cases = runner.load_cases(output)
    assert [case["id"] for case in cases] == ["chat:[email]", "transcript-2"]
    first = cases[0]["simulated_user"]
    assert first["persona"] == "A terse customer who signs off as [email]."
    assert (first["max_turns"], cases[1]["simulated_user"]["max_turns"]) == (2, 1)
    assert first["goal"] == "Get help with: My email is [email] and my order is late"


def test_personas_from_sessions_in_the_data_folder_read_their_turns_in_order(tmp_path, fake):
    for name in ("evals.yaml", "cases.jsonl", "agent.py"):
        shutil.copy(CONVERSATION / name, tmp_path / name)
    run_suite(tmp_path / "evals.yaml")
    session = results_of(tmp_path)["cancel-later"]["session_id"]
    (found,) = transcripts_from_store(tmp_path / "evals-home", [session])
    assert found.id == session
    assert [m["content"] for m in found.messages if m["role"] == "user"] == [
        "My order number is A-1002.",
        "Is it in stock yet?",
        "Then please cancel it.",
    ]
    assert found.messages[1] == {"role": "assistant", "content": "Order A-1002 is waiting for stock."}
    built = build_personas([found], model=f"openai:/{MODEL}", max_turns=5)
    assert built.cases[0].simulated_user is not None and built.cases[0].simulated_user.max_turns == 5
    with pytest.raises(ConfigError, match="no traces in the data folder have the session id 'missing'"):
        transcripts_from_store(tmp_path / "evals-home", ["missing"])


def test_transcripts_that_cannot_be_read_are_refused(tmp_path, fake):
    bad = tmp_path / "bad.jsonl"
    bad.write_text(json.dumps({"messages": [{"role": "tool", "content": "x"}]}) + "\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="a message has a role"):
        read_transcripts(bad)
    bad.write_text(json.dumps({"messages": [{"role": "assistant", "content": "Hello"}]}) + "\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="has no user message"):
        read_transcripts(bad)
    with pytest.raises(ConfigError, match="the id 'same'"):
        two = [Transcript("same", [{"role": "user", "content": "hi"}])] * 2
        build_personas(two, model=f"openai:/{MODEL}")
    assert fake.requests == []  # refused before the model reads anything
