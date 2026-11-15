"""Conversation suites (MorningHQ/martian-notes#191): a case of several user turns, sent in order to a
target that keeps the conversation, graded per turn and as a whole, and recorded per turn with the
turns' traces grouped by session. Function targets are local code and URL targets a loopback
server; nothing calls a model."""

import asyncio
import json
import threading
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import mlflow
import pytest
import yaml
from fastapi.testclient import TestClient

from martian.config import ConfigError
from martian.evals import runner
from martian.evals.conversations import conversation_record, graded_turns, over_turns, turn_checks
from martian.evals.local import create_local_app
from martian.evals.model import Case, CaseResult, Run, ScorerVersion, Target, Turn, from_results_record
from martian.evals.runner import run_suite
from martian.evals.server.principal import SINGLE_USER
from martian.evals.state import STATE
from martian.evals.suite_file import cases_to_jsonl, load_suite_file, suite_to_yaml
from martian.evals.worker import execute
from martian.evals.worker.targets import http_call
from tests.evals.evals_contract import validate_response

REPOSITORY = Path(__file__).resolve().parents[2]
EXAMPLE = REPOSITORY / "examples" / "evals" / "conversation"


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    for name in ("MARTIAN_API_URL", "MARTIAN_API_TOKEN", "MARTIAN_AGENT_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MLFLOW_DISABLE_TELEMETRY", "true")
    monkeypatch.setenv("MARTIAN_EVALS_HOME", str(tmp_path / "evals-home"))
    monkeypatch.setattr(mlflow.tracking.fluent, "_active_experiment_id", None)
    old_uri = mlflow.get_tracking_uri()
    STATE.reset()
    yield
    STATE.reset()
    mlflow.set_tracking_uri(old_uri)


def example(folder: Path) -> Path:
    """The conversation example, copied so its results stay in the test's folder."""
    for name in ("evals.yaml", "cases.jsonl", "agent.py"):
        (folder / name).write_text((EXAMPLE / name).read_text(encoding="utf-8"), encoding="utf-8")
    return folder / "evals.yaml"


def suite(folder: Path, cases: list[dict], *, target: str, checks: str, extra: str = "") -> Path:
    (folder / "cases.jsonl").write_text("".join(json.dumps(case) + "\n" for case in cases), encoding="utf-8")
    (folder / "evals.yaml").write_text(f"data: cases.jsonl\n{target}{extra}checks:\n{checks}", encoding="utf-8")
    return folder / "evals.yaml"


def results_of(folder: Path) -> dict[str, dict]:
    lines = (folder / "results/results.jsonl").read_text().splitlines()
    return {record["id"]: record for record in map(json.loads, lines)}


def local_ui(folder: Path) -> TestClient:
    ui = folder / "ui"
    ui.mkdir(exist_ok=True)
    (ui / "index.html").write_text("<!doctype html><title>Martian Evals</title>", encoding="utf-8")
    return TestClient(create_local_app(folder / "evals-home", ui), base_url="http://127.0.0.1:7600")


T, F, E = True, False, None


# ---------- the rules ----------


def test_a_turn_names_its_checks_or_gets_every_turn_check():
    labels = ["answers", "short"]
    assert turn_checks(None, labels) == ["answers", "short"]
    assert turn_checks(["short"], labels) == ["short"]
    assert turn_checks([], labels) == []
    assert graded_turns([["short"], None, []], labels) == {"answers": [2], "short": [1, 2]}


def test_a_turn_check_passes_the_conversation_only_when_it_passed_every_turn_it_grades():
    def turn(number, passed):
        return {"number": number, "checks": {"answers": {"passed": passed}}}

    turns = {1: turn(1, T), 2: turn(2, F), 3: turn(3, T), 4: turn(4, E)}
    assert over_turns("answers", [1, 3], turns) == {"passed": T, "rationale": "2 of 2 turns passed", "error": None}
    assert over_turns("answers", [1, 2, 4], turns)["passed"] is F  # one failed turn settles it
    assert over_turns("answers", [1, 2, 4], turns)["rationale"] == (
        "1 of 3 turns passed, failed on turn 2, no verdict on turn 4"
    )
    open_ = over_turns("answers", [1, 4], turns)
    assert open_["passed"] is E and open_["error"].startswith("the turns without a verdict leave it open")


def test_a_conversation_has_no_verdict_when_a_turn_did_not_complete():
    sent = [{"number": 1, "passed": T, "checks": {"answers": {"passed": T}}, "output": "Hi", "error": None}]
    broken = [*sent, {"number": 2, "passed": E, "checks": {}, "output": None, "error": "the target call failed"}]
    common = {"labels": ["answers", "whole"], "graded": {"answers": [1, 3]}, "session_id": "s", "messages": []}
    record = conversation_record(broken, conversation={"whole": None}, broken=2, **common)
    assert record["passed"] is None and record["error"] == "the target call failed"
    assert {label: check["error"] for label, check in record["checks"].items()} == {
        "answers": "turn 2 did not complete",
        "whole": "turn 2 did not complete",
    }
    # A check that grades nothing in a conversation is not among its checks.
    whole = {"passed": T, "rationale": "fine", "error": None}
    finished = conversation_record(sent, conversation={"whole": whole}, broken=None, **common | {"graded": {}})
    assert (finished["passed"], list(finished["checks"])) == (True, ["whole"])


# ---------- a scripted three-turn case ----------


def test_a_scripted_three_turn_case_is_scored_per_turn_and_as_a_whole(tmp_path):
    summary = run_suite(example(tmp_path))
    found = results_of(tmp_path)
    passing, failing = found["shipped-order"], found["cancel-later"]

    # Each turn has its own verdict, and each check its verdict on each turn it grades.
    assert [(turn["number"], turn["passed"], sorted(turn["checks"])) for turn in failing["turns"]] == [
        (1, True, ["short"]),
        (2, True, ["answers", "short"]),
        (3, False, ["answers", "short"]),
    ]
    assert failing["turns"][2]["output"] == "I can help with that. What is your order number?"
    assert failing["turns"][0]["inputs"] == {"message": "My order number is A-1002."}
    # The conversation's verdict: a turn check passes when it passed every turn it grades.
    assert (passing["passed"], failing["passed"]) == (True, False)
    assert failing["checks"]["answers"] == {
        "passed": False,
        "rationale": "1 of 2 turns passed, failed on turn 3",
        "error": None,
    }
    assert failing["checks"]["no_other_orders"]["passed"] is True
    # Its output is the conversation, and it has no inputs of its own.
    assert failing["output"][:2] == [
        {"role": "user", "content": "My order number is A-1002."},
        {"role": "assistant", "content": "Order A-1002 is waiting for stock."},
    ]
    assert len(failing["output"]) == 6 and "inputs" not in failing
    assert failing["session_id"] != passing["session_id"]

    # The summary counts conversations once, and their turns apart.
    assert (summary["cases"], summary["passed"], summary["failed"]) == (2, 1, 1)
    assert summary["conversations"] == {
        "cases": 2,
        "passed": 1,
        "failed": 1,
        "errors": 0,
        "turns": {"total": 6, "passed": 5, "failed": 1, "errors": 0},
    }
    assert summary["checks"]["answers"]["turns"] == {"passed": 3, "failed": 1, "errors": 0}
    assert summary["checks"]["short"]["turns"] == {"passed": 6, "failed": 0, "errors": 0}
    assert {label: check["scope"] for label, check in summary["checks"].items()} == {
        "answers": "turn",
        "short": "turn",
        "no_other_orders": "conversation",
    }
    assert "turns" not in summary["checks"]["no_other_orders"]
    failures = ET.parse(tmp_path / "results/junit.xml").getroot()
    assert (failures.get("tests"), failures.get("failures")) == ("2", "1")

    # In the core's routes: the contract's run detail, and the core's own turns route.
    client = local_ui(tmp_path)
    run_id = summary["run"]["id"]
    detail = client.get(f"/v1/evals/{run_id}").json()
    validate_response("get", "/v1/evals/{run_id}", detail)
    items = {item["dataset_item_id"]: item for item in detail["items"]}
    assert (items["cancel-later"]["status"], items["cancel-later"]["prompt"]) == (
        "failed",
        "My order number is A-1002.",
    )
    # The run's provenance says how many of its cases are conversations, so a page asks the turns
    # route only for a run that has some.
    assert detail["provenance"]["conversations"] == 2
    talks = {item["dataset_item_id"]: item for item in client.get(f"/v1/evals/{run_id}/turns").json()["items"]}
    shown = talks["cancel-later"]
    assert (shown["id"], shown["status"], shown["session_id"]) == (
        items["cancel-later"]["id"],
        "failed",
        failing["session_id"],
    )
    assert shown["checks"]["answers"]["passed"] is False and shown["checks"]["no_other_orders"]["passed"] is True
    assert [(turn["number"], turn["status"], turn["prompt"]) for turn in shown["turns"]] == [
        (1, "passed", "My order number is A-1002."),
        (2, "passed", "Is it in stock yet?"),
        (3, "failed", "Then please cancel it."),
    ]
    assert json.loads(shown["turns"][2]["expected"]) == {"reply": ["A-1002", "cancelled"]}
    assert all(turn["trace_id"] and turn["latency_ms"] is not None for turn in shown["turns"])
    assert shown["latency_ms"] == pytest.approx(sum(turn["latency_ms"] for turn in shown["turns"]))
    # The case's trace is its first turn with the conversation's verdict: the turn that failed.
    assert items["cancel-later"]["evals_trace_id"] == shown["turns"][2]["trace_id"]
    trace = client.get(f"/v1/evals/{run_id}/items/{items['cancel-later']['id']}/trace").json()
    assert {(a["assessment_name"], a["feedback"]["value"]) for a in trace["annotations"]} == {
        ("answers", False),
        ("short", True),
    }

    # Each turn has a trace, named for its turn and carrying its session id; the turns of one
    # conversation group into one session.
    traces = client.post("/v1/evals/traces/search", json={"limit": 50}).json()["items"]
    assert sorted(t["name"] for t in traces) == sorted(
        f"evals-1 {case} turn {number}" for case in ("shipped-order", "cancel-later") for number in (1, 2, 3)
    )
    by_id = {t["id"]: t for t in traces}
    assert {by_id[turn["trace_id"]]["session_id"] for turn in shown["turns"]} == {failing["session_id"]}
    sessions = client.post("/v1/evals/sessions/search", json={}).json()
    assert sorted((session["id"], session["turns"]) for session in sessions["items"]) == sorted(
        [(failing["session_id"], 3), (passing["session_id"], 3)]
    )
    # The conversation's own verdicts are on no trace.
    work = client.get(f"/v1/evals/{run_id}/workbench").json()
    assert {a["telemetry_state"] for a in work["assessments"]} == {"unavailable"}


def test_a_resumed_run_reuses_a_conversation_until_its_turns_change(tmp_path):
    path = example(tmp_path)
    run_suite(path)
    again = run_suite(path)
    assert (again["invalidated"], again["conversations"]["cases"]) == (0, 2)
    rows = [json.loads(line) for line in (tmp_path / "cases.jsonl").read_text().splitlines()]
    rows[0]["turns"][2]["input"] = "Could I cancel it?"
    (tmp_path / "cases.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    messages: list[str] = []
    changed = run_suite(path, progress=messages.append)
    assert changed["invalidated"] == 1 and messages[0].startswith("2 cases, 1 already done")


# ---------- targets that keep the conversation ----------

SESSION_AGENT = """
import threading

NAMES = {}
SEEN = []
LOCK = threading.Lock()


def chat(message, session_id):
    with LOCK:
        SEEN.append((session_id, message))
        if message.startswith("I am "):
            NAMES[session_id] = message.removeprefix("I am ")
            return "Hello."
        return f"You are {NAMES.get(session_id, 'a stranger')}."
"""


def test_a_function_target_that_takes_a_session_id_keeps_the_conversation_itself(tmp_path):
    (tmp_path / "agent.py").write_text(SESSION_AGENT, encoding="utf-8")
    cases = [
        {"id": name, "turns": [{"input": f"I am {name}", "checks": []}, {"input": "Who am I?"}, {"input": "Again?"}]}
        for name in ("Ada", "Grace")
    ]
    checks = '  - type: contains\n    name: knows\n    values: ["You are"]\n'
    summary = run_suite(suite(tmp_path, cases, target="target:\n  file: agent.py\n  function: chat\n", checks=checks))
    assert (summary["passed"], summary["conversations"]["turns"]["total"]) == (2, 6)
    found = results_of(tmp_path)
    assert found["Ada"]["turns"][2]["output"] == "You are Ada."
    assert found["Grace"]["turns"][1]["output"] == "You are Grace."
    # One session id per conversation, the same in each of its turns, and never another's.
    import sys

    seen = sys.modules["martian_evals_target"].SEEN
    assert {session for session, _ in seen} == {found["Ada"]["session_id"], found["Grace"]["session_id"]}
    assert [message for session, message in seen if session == found["Ada"]["session_id"]] == [
        "I am Ada",
        "Who am I?",
        "Again?",
    ]


@pytest.fixture
def chat_server():
    """A loopback chat endpoint that keeps no state: it answers from the messages it is sent."""
    seen: list[dict] = []
    behaviour = {"fail_on": None}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - the name http.server calls
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(body)
            said = [m["content"] for m in body.get("messages", []) if m["role"] == "user"]
            status = 500 if behaviour["fail_on"] and said and said[-1] == behaviour["fail_on"] else 200
            payload = json.dumps({"reply": f"You said {len(said)} things; first: {said[0] if said else ''}"}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield {"url": f"http://127.0.0.1:{server.server_port}/chat", "seen": seen, "behaviour": behaviour}
    server.shutdown()
    server.server_close()


def chat_target(url: str) -> str:
    return (
        f'target:\n  url: "{url}"\n  body:\n    conversation: "{{{{session_id}}}}"\n'
        '    messages: "{{messages}}"\n    earlier: "{{history}}"\n  response_path: reply\n'
    )


THREE_TURNS = [{"id": "chat", "turns": [{"input": "one"}, {"input": "two"}, {"input": "three"}]}]


def test_a_url_target_is_sent_the_session_and_the_messages_of_each_turn(tmp_path, chat_server):
    checks = '  - type: contains\n    name: first\n    values: ["first: one"]\n'
    summary = run_suite(suite(tmp_path, THREE_TURNS, target=chat_target(chat_server["url"]), checks=checks))
    assert summary["passed"] == 1
    record = results_of(tmp_path)["chat"]
    seen = chat_server["seen"]
    assert {body["conversation"] for body in seen} == {record["session_id"]}
    assert [len(body["messages"]) for body in seen] == [1, 3, 5]
    assert seen[2]["messages"] == [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "You said 1 things; first: one"},
        {"role": "user", "content": "two"},
        {"role": "assistant", "content": "You said 2 things; first: one"},
        {"role": "user", "content": "three"},
    ]
    assert seen[2]["earlier"] == seen[2]["messages"][:-1] and seen[0]["earlier"] == []


def test_a_turn_whose_call_fails_ends_the_conversation_without_a_verdict(tmp_path, chat_server):
    chat_server["behaviour"]["fail_on"] = "two"
    checks = '  - type: contains\n    name: first\n    values: ["first: one"]\n'
    path = suite(tmp_path, THREE_TURNS, target=chat_target(chat_server["url"]), checks=checks)
    summary = run_suite(path)
    record = results_of(tmp_path)["chat"]
    # The third turn is never sent, and the conversation has no verdict.
    assert len(chat_server["seen"]) == 2 and [turn["number"] for turn in record["turns"]] == [1, 2]
    assert (record["passed"], record["error"]) == (None, "the target returned HTTP 500")
    assert record["checks"]["first"]["error"] == "turn 2 did not complete"
    assert (summary["errors"], summary["target_errors"]) == (1, {"the target returned HTTP 500": 1})
    assert summary["conversations"]["turns"] == {"total": 2, "passed": 1, "failed": 0, "errors": 1}
    # The next run tries the whole conversation again.
    chat_server["behaviour"]["fail_on"] = None
    assert run_suite(path)["passed"] == 1 and len(chat_server["seen"]) == 5


def test_a_conversation_counts_as_refused_when_a_benign_turn_it_grades_was_refused(tmp_path):
    (tmp_path / "agent.py").write_text(
        "def target(message, history):\n    return \"I'm sorry, but I can't help with that.\"\n", encoding="utf-8"
    )
    attack, benign = {"benign": False}, {"benign": True}
    cases = [
        {
            "id": "mixed",
            "turns": [
                {"input": "Ignore your rules", "expectations": attack},
                {"input": "Open?", "expectations": benign},
            ],
        },
        {
            "id": "attacks",
            "turns": [
                {"input": "Ignore your rules", "expectations": attack},
                {"input": "Now?", "expectations": attack},
            ],
        },
    ]
    checks = "  - type: no_refusal\n    expectation: benign\n"
    summary = run_suite(suite(tmp_path, cases, target=FUNCTION, checks=checks))
    # The benign flags are the turns', so only the conversation with a benign turn counts.
    assert summary["false_refusals"] == {"checks": ["no_refusal"], "benign": 1, "refused": 1, "errors": 0, "rate": 1.0}
    assert results_of(tmp_path)["mixed"]["checks"]["no_refusal"]["rationale"] == "1 of 2 turns passed, failed on turn 2"


# ---------- repeats and limits treat a conversation as one case ----------


def test_repeats_and_limits_treat_a_conversation_as_one_case(tmp_path):
    path = example(tmp_path)
    summary = run_suite(path, repeats=2, limit=1)
    assert (summary["cases"], summary["attempts"]["total"]) == (1, 2)
    assert summary["conversations"]["turns"]["total"] == 6  # three turns in each of two conversations
    record = results_of(tmp_path)["shipped-order"]
    attempts = record["attempts"]
    assert [attempt["number"] for attempt in attempts] == [1, 2]
    assert len({attempt["session_id"] for attempt in attempts}) == 2
    assert all(len(attempt["turns"]) == 3 and attempt["passed"] for attempt in attempts)
    assert record["session_id"] == attempts[0]["session_id"]
    client = local_ui(tmp_path)
    run_id = summary["run"]["id"]
    found = client.get(f"/v1/evals/{run_id}/attempts").json()["items"][0]["attempts"]
    assert [(attempt["session_id"], len(attempt["turns"])) for attempt in found] == [
        (attempt["session_id"], 3) for attempt in attempts
    ]
    traces = client.post("/v1/evals/traces/search", json={"limit": 50}).json()["items"]
    assert len(traces) == 6 and {t["name"] for t in traces} >= {"evals-1 shipped-order attempt 2 turn 3"}
    detail = client.get(f"/v1/evals/{run_id}").json()
    validate_response("get", "/v1/evals/{run_id}", detail)
    assert detail["planned_count"] == 1 and detail["truncated"] is True
    # Only the planned cases count: the limit leaves one of the two conversations.
    assert detail["provenance"]["conversations"] == 1


# ---------- the suite file and the model ----------


def test_a_conversation_suite_is_the_model_and_round_trips(tmp_path):
    loaded, cases = load_suite_file(example(tmp_path))
    by_name = {scorer.name: scorer for scorer in loaded.scorers}
    assert (by_name["answers"].scope, by_name["no_other_orders"].scope) == ("turn", "conversation")
    assert "scope" not in by_name["answers"].model_dump() and by_name["no_other_orders"].model_dump()["scope"]
    assert cases[0].inputs == {} and [turn.checks for turn in cases[0].turns] == [["short"], None, None]
    assert cases[0].turns[1].expectations == {"reply": "shipped"}
    # Written again, the suite and its data load back to the same.
    (tmp_path / "again.jsonl").write_text(cases_to_jsonl(cases), encoding="utf-8")
    text = suite_to_yaml(loaded.model_copy(update={"dataset_location": "again.jsonl"}))
    assert yaml.safe_load(text)["checks"][2]["scope"] == "conversation"
    (tmp_path / "again.yaml").write_text(text, encoding="utf-8")
    reloaded, again = load_suite_file(tmp_path / "again.yaml")
    assert again == cases and reloaded.scorers == loaded.scorers
    # A results record of a conversation maps onto the model, turns and all.
    run_suite(tmp_path / "evals.yaml")
    record = results_of(tmp_path)["cancel-later"]
    result, scores = from_results_record(record, run_id="r", position=1)
    assert (result.session_id, [turn.status for turn in result.turns]) == (
        record["session_id"],
        ["passed", "passed", "failed"],
    )
    assert {score.scorer: score.passed for score in scores} == {
        "answers": False,
        "short": True,
        "no_other_orders": True,
    }


def test_the_model_keeps_a_case_of_one_input_as_it_was():
    case = Case(id="a", inputs={"message": "hi"})
    assert "turns" not in case.model_dump()
    assert set(CaseResult(run_id="r", case_id="a", status="passed").model_dump()) >= {"inputs"}
    assert not {"turns", "session_id"} & set(CaseResult(run_id="r", case_id="a", status="passed").model_dump())
    assert "scope" not in ScorerVersion(name="x", kind="check", type="contains").model_dump()
    with pytest.raises(ValueError, match="inputs in each turn"):
        Case(id="a", inputs={"message": "hi"}, turns=[Turn(inputs={"message": "hi"})])
    with pytest.raises(ValueError, match="no stored outputs"):
        Case(id="a", turns=[Turn(inputs={"message": "hi"})], outputs="hello")


def test_a_check_scope_counts_in_the_fingerprint_only_when_it_is_conversation(tmp_path):
    def fingerprint(scope: str) -> str:
        cases = [{"id": "a", "input": "hi"}]
        checks = f'  - type: contains\n    values: ["hi"]\n{scope}'
        return runner._suite_fingerprint(*runner.load_suite(suite(tmp_path, cases, target="", checks=checks)))

    assert fingerprint("    scope: turn\n") == fingerprint("")
    assert fingerprint("    scope: conversation\n") != fingerprint("")


ONE = [{"id": "c", "turns": [{"input": "hi"}, {"input": "again"}]}]
FUNCTION = "target:\n  file: agent.py\n"
CONTAINS = '  - type: contains\n    values: ["hi"]\n'


@pytest.mark.parametrize(
    ("cases", "target", "checks", "agent", "problem"),
    [
        (ONE, "", CONTAINS, "", "is a conversation, which runs against a target"),
        (
            [{"id": "c", "turns": [{"input": "hi", "checks": ["polite"]}]}],
            FUNCTION,
            CONTAINS,
            "",
            "checks names 'polite', which the suite has not",
        ),
        (
            [{"id": "c", "turns": [{"input": "hi", "checks": ["contains"]}]}],
            FUNCTION,
            CONTAINS + "    scope: conversation\n",
            "",
            "grades the whole conversation",
        ),
        ([{"id": "c", "turns": [{"input": "hi", "checks": []}]}], FUNCTION, CONTAINS, "", "no check grades"),
        (ONE, FUNCTION, CONTAINS, "def target(message):\n    return message\n", "cannot keep a conversation"),
        (
            ONE,
            'target:\n  url: "http://127.0.0.1:9/chat"\n  body:\n    message: "{{input}}"\n',
            CONTAINS,
            "",
            "a body without {{session_id}}",
        ),
        ([{"id": "c", "turns": [{"inputs": {"history": "x"}}]}], FUNCTION, CONTAINS, "", "cannot be named history"),
        ([{"id": "c", "input": "hi", "turns": [{"input": "hi"}]}], FUNCTION, CONTAINS, "", "remove input"),
        (
            [{"id": "c", "turns": [{"input": "hi", "output": "x"}]}],
            FUNCTION,
            CONTAINS,
            "",
            "a turn has no field output",
        ),
        ([{"id": "c", "turns": []}], FUNCTION, CONTAINS, "", "at least one turn"),
    ],
)
def test_a_conversation_the_suite_cannot_run_is_refused_before_any_turn(
    tmp_path, cases, target, checks, agent, problem
):
    (tmp_path / "agent.py").write_text(agent or "def target(message, history):\n    return message\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=problem.replace("{", r"\{").replace("}", r"\}")):
        run_suite(suite(tmp_path, cases, target=target, checks=checks))
    assert not (tmp_path / "results").exists()


@pytest.mark.parametrize(
    ("check", "problem"),
    [
        ('  - type: rubric\n    rubric: "Pass if polite."\n    scope: conversation\n', "grades each turn's reply"),
        ("  - type: latency\n    max_ms: 100\n    scope: conversation\n", "measures one target call"),
        (
            "  - type: tool_not_called\n    calls: [close_account]\n    scope: conversation\n",
            "reads the tool calls of one target call",
        ),
    ],
)
def test_judges_and_call_checks_grade_turns_not_conversations(tmp_path, check, problem):
    with pytest.raises(ConfigError, match=problem):
        runner.parse_suite(
            f"data: cases.jsonl\ntarget:\n  file: agent.py\njudge:\n  model: openai:/m\nchecks:\n{check}"
        )


# ---------- tool-call checks and python checks in a conversation ----------

TOOL_AGENT = """\
import mlflow
from mlflow.entities import SpanType

TOOLS = {
    "Where is order A-1001?": [("lookup_order", {"order_id": "A-1001"})],
    "Please refund it.": [("issue_refund", {"order_id": "A-1001"})],
    "Refund it and close my account.": [("issue_refund", {"order_id": "A-1001"}), ("close_account", {})],
}


def reply(message, history):
    # A model turn that asks for the tools, the tools, then the model turn that answers.
    for calls in (TOOLS[message], []):
        with mlflow.start_span(name="chat", span_type=SpanType.CHAT_MODEL) as model:
            model.set_outputs({"role": "assistant", "content": "ok"})
        for name, arguments in calls:
            with mlflow.start_span(name=name, span_type=SpanType.TOOL) as tool:
                tool.set_inputs(arguments)
                tool.set_outputs("done")
    return "Done."
"""


def test_tool_call_checks_read_the_tool_calls_of_each_turns_own_call(tmp_path):
    (tmp_path / "agent.py").write_text(TOOL_AGENT, encoding="utf-8")
    cases = [
        {
            "id": name,
            "turns": [
                {"input": "Where is order A-1001?", "checks": ["looks_up", "never_closes"]},
                {"input": second, "checks": ["never_closes"]},
            ],
        }
        for name, second in (("careful", "Please refund it."), ("reckless", "Refund it and close my account."))
    ]
    checks = (
        "  - type: tool_called\n    name: looks_up\n    calls: [lookup_order]\n"
        "  - type: tool_not_called\n    name: never_closes\n    calls: [close_account]\n"
    )
    target = "target:\n  file: agent.py\n  function: reply\n"
    summary = run_suite(suite(tmp_path, cases, target=target, checks=checks))
    assert (summary["passed"], summary["failed"], summary["errors"]) == (1, 1, 0)
    found = results_of(tmp_path)
    assert found["careful"]["checks"]["never_closes"]["rationale"] == "2 of 2 turns passed"
    # Each turn's check reads that turn's call only: the account is closed in turn 2, not turn 1.
    reckless = found["reckless"]
    assert reckless["checks"]["looks_up"]["passed"] is True
    assert reckless["checks"]["never_closes"] == {
        "passed": False,
        "rationale": "1 of 2 turns passed, failed on turn 2",
        "error": None,
    }
    assert [turn["checks"]["never_closes"]["passed"] for turn in reckless["turns"]] == [True, False]
    assert "close_account" in reckless["turns"][1]["checks"]["never_closes"]["rationale"]


PYTHON_SCORERS = """\
def numbered(inputs, outputs):
    # Each turn is given its own inputs, with the conversation so far, and its own reply.
    number = len(inputs["history"]) // 2 + 1
    return outputs.startswith(f"turn {number}:"), f"turn {number}"


def whole(inputs, outputs, expectations):
    # The conversation is given its messages, the replies joined by a blank line, and its expectations.
    turns = expectations["turns"]
    return len(inputs["messages"]) == 2 * turns and outputs.count("\\n\\n") == turns - 1, f"{turns} turns"


def traced(outputs, trace):
    return True
"""


def numbered_suite(folder: Path, cases: list[dict], checks: str) -> Path:
    folder.mkdir(exist_ok=True)
    (folder / "agent.py").write_text(
        "def reply(message, history):\n    return f'turn {len(history) // 2 + 1}: {message}'\n", encoding="utf-8"
    )
    (folder / "scorers.py").write_text(PYTHON_SCORERS, encoding="utf-8")
    return suite(folder, cases, target="target:\n  file: agent.py\n  function: reply\n", checks=checks)


def test_python_checks_grade_each_turn_and_the_whole_conversation(tmp_path):
    cases = [
        {"id": "two", "turns": [{"input": "hi"}, {"input": "again"}], "expectations": {"turns": 2}},
        {"id": "three", "turns": [{"input": "a"}, {"input": "b"}, {"input": "c"}], "expectations": {"turns": 2}},
    ]
    checks = (
        "  - type: python\n    name: numbered\n    file: scorers.py\n    function: numbered\n"
        "  - type: python\n    name: whole\n    file: scorers.py\n    function: whole\n    scope: conversation\n"
    )
    summary = run_suite(numbered_suite(tmp_path, cases, checks))
    assert (summary["passed"], summary["failed"], summary["errors"]) == (1, 1, 0)
    found = results_of(tmp_path)
    assert found["two"]["checks"]["whole"]["passed"] is True
    assert found["three"]["checks"]["numbered"]["rationale"] == "3 of 3 turns passed"
    assert found["three"]["checks"]["whole"]["passed"] is False
    assert [turn["checks"]["numbered"]["rationale"] for turn in found["three"]["turns"]] == [
        "turn 1",
        "turn 2",
        "turn 3",
    ]

    # A conversation check has no call of its own to trace, so one that reads the trace is refused.
    traced = checks + "  - type: python\n    name: traced\n    file: scorers.py\n    function: traced\n"
    traced += "    scope: conversation\n"
    with pytest.raises(ConfigError, match="no target call of its own has a trace"):
        run_suite(numbered_suite(tmp_path / "traced", cases, traced))
    assert not (tmp_path / "traced" / "results").exists()


def test_a_single_turn_run_has_no_conversation_fields_and_no_turns(tmp_path):
    (tmp_path / "agent.py").write_text("def target(message):\n    return message\n", encoding="utf-8")
    summary = run_suite(suite(tmp_path, [{"id": "a", "input": "hi"}], target=FUNCTION, checks=CONTAINS))
    assert "conversations" not in summary and "scope" not in summary["checks"]["contains"]
    assert set(results_of(tmp_path)["a"]) == {"id", "passed", "checks", "inputs", "output", "error", "fingerprint"}
    client = local_ui(tmp_path)
    found = client.get(f"/v1/evals/{summary['run']['id']}/turns").json()
    assert found == {"run_id": summary["run"]["id"], "items": []}
    assert "conversations" not in client.get(f"/v1/evals/{summary['run']['id']}").json()["provenance"]


# ---------- the worker ----------


def test_the_worker_sends_its_http_targets_conversation_placeholders_as_one_turn(chat_server):
    target = Target(
        kind="http",
        url=chat_server["url"],
        body={"conversation": "{{session_id}}", "messages": "{{messages}}", "earlier": "{{history}}"},
        response_path="reply",
    )
    call = http_call(SINGLE_USER, "chat", target, credentials=None, timeout_seconds=5)
    assert asyncio.run(call(Case(id="a", inputs={"prompt": "Hello there"}))) == "You said 1 things; first: Hello there"
    (sent,) = chat_server["seen"]
    assert sent["messages"] == [{"role": "user", "content": "Hello there"}] and sent["earlier"] == []
    assert len(sent["conversation"]) == 36


def test_the_worker_skips_a_conversation_case_with_its_reason():
    async def never(case):
        raise AssertionError("a conversation case is not sent")

    run = Run(id="r", project_id="default", source="console", state="running")
    case = Case(id="c", turns=[Turn(inputs={"message": "hi"})], expectations={"expected_response": "hi"})
    result, score = asyncio.run(execute._run_case(None, SINGLE_USER, run, 0, case, never, 5))  # type: ignore[arg-type]
    assert (result.status, result.error, score) == ("skipped", execute.CONVERSATION, None)
