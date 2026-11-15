"""Conversation judges (MorningHQ/martian-notes#176): task completion, policy adherence across turns
and knowledge retention, each reading a whole conversation through the shared judge engine with a
flat verdict whose turns and quotes the SDK checks; in the suite file at scope: conversation, on
scripted and simulated conversations, and in every place a run's results show. A fake provider
stands in for the model; nothing here calls one."""

import asyncio
import importlib.util
import json
import shutil
import sys
from pathlib import Path

import mlflow
import pytest
import yaml
from fastapi.testclient import TestClient
from mlflow.entities import Feedback

from martian.config import ConfigError
from martian.evals import knowledge_retention, policy_adherence, runner, task_completion
from martian.evals._mlflow import ChatCompletionError
from martian.evals.conversation_judges import (
    KNOWLEDGE_RETENTION_VERSION,
    POLICY_ADHERENCE_VERSION,
    TASK_COMPLETION_VERSION,
    _AdherenceJudgment,
    _CompletionJudgment,
    _RetentionJudgment,
    conversation_turns,
)
from martian.evals.local import create_local_app, open_stores
from martian.evals.runner import run_suite
from martian.evals.server.principal import SINGLE_USER
from martian.evals.simulation import _Step
from martian.evals.state import STATE
from martian.evals.suite_file import load_suite_file, suite_from_yaml, suite_to_yaml
from tests.evals.evals_contract import validate_response

REPOSITORY = Path(__file__).resolve().parents[2]
AGENT = REPOSITORY / "examples" / "evals" / "conversation" / "agent.py"

ASKED_AGAIN = "I can help with that. What is your order number?"
MESSAGES = [
    {"role": "system", "content": "You are a shop assistant."},
    {"role": "user", "content": "My order number is A-1002."},
    {"role": "assistant", "content": "Order A-1002 is waiting for stock."},
    {"role": "user", "content": "Then please cancel it."},
    {"role": "assistant", "content": ASKED_AGAIN},
]
INPUTS = {"messages": MESSAGES, "case_id": "hidden-case-id"}
GOAL = "Cancel order A-1002."
GUIDELINES = ["Never reveal another customer's order.", "Never ask again for something the customer already gave."]


def completed(verdict="pass", turn=2, quote="What is your order number?", reason="The order was not cancelled."):
    return {"reason": reason, "turn": turn, "quote": quote, "verdict": verdict}


def adhered(verdict="fail", turn=2, guideline=2, quote="What is your order number?", reason="It asked again."):
    return {"reason": reason, "turn": turn, "guideline": guideline, "quote": quote, "verdict": verdict}


def retained(
    verdict="fail", fact_turn=1, fact="My order number is A-1002.", turn=2, quote="What is your order number?"
):
    return {
        "reason": "It forgot the order number.",
        "fact_turn": fact_turn,
        "fact": fact,
        "turn": turn,
        "quote": quote,
        "verdict": verdict,
    }


@pytest.fixture
def fake_model(monkeypatch):
    """Replace the shared transport with replies the test supplies, and keep each request."""
    calls = []

    def install(*values):
        replies = list(values)

        def provider(**kwargs):
            calls.append(kwargs)
            return Feedback(name=kwargs["assessment_name"], value=replies.pop(0), metadata={"fixture": "1"})

        monkeypatch.setattr("martian.evals.conversation_judges.invoke_response_judge", provider)
        return calls

    return install


def payload_of(call):
    return json.loads(call["prompt"][1].content)


def evidence_of(result):
    return json.loads(result.metadata["martian.judge.evidence"])


# ---------- each judge reads the whole conversation and names its turn ----------


def test_task_completion_names_the_turn_that_fell_short_and_quotes_its_reply(fake_model):
    calls = fake_model(completed("fail"), completed("fail", turn=0, quote=""))
    judge = task_completion(model="openai:/stub", include_judge_prompt=True)
    result = judge(inputs=INPUTS, outputs="ignored", expectations={"goal": GOAL, "label": "fail"})
    assert result.value is False
    assert result.rationale == (
        "Goal not reached; fell short on turn 2. The order was not cancelled. "
        'Turn 2 reply: "What is your order number?"'
    )
    start = ASKED_AGAIN.index("What")
    assert evidence_of(result) == [
        {
            "field": "quote",
            "turn": 2,
            "role": "assistant",
            "start": start,
            "end": len(ASKED_AGAIN),
            "text": "What is your order number?",
        }
    ]
    # The payload: the goal and the conversation turn by turn, without the system message, the
    # case's labels or its id.
    assert payload_of(calls[0]) == {
        "goal": GOAL,
        "conversation": [
            {"turn": 1, "user": "My order number is A-1002.", "assistant": "Order A-1002 is waiting for stock."},
            {"turn": 2, "user": "Then please cancel it.", "assistant": ASKED_AGAIN},
        ],
    }
    assert "hidden-case-id" not in result.metadata["martian.judge.prompt"]
    assert "shop assistant" not in result.metadata["martian.judge.prompt"]
    assert "untrusted" in calls[0]["prompt"][0].content
    assert calls[0]["response_format"] is _CompletionJudgment and calls[0]["stage"] == "draft"
    metadata = result.metadata
    assert (metadata["martian.judge.version"], metadata["martian.judge.turn"], metadata["martian.judge.turns"]) == (
        TASK_COMPLETION_VERSION,
        "2",
        "2",
    )
    assert metadata["martian.judge.model_calls"] == "1" and metadata["fixture"] == "1"
    # A fail need not name a turn: no single reply fell short.
    assert judge(inputs=INPUTS, outputs=None, expectations={"goal": GOAL}).rationale == (
        "Goal not reached. The order was not cancelled."
    )


def test_task_completion_passes_on_the_turn_that_completed_the_goal(fake_model):
    fake_model(completed("pass", turn=1, quote="waiting for stock", reason="It told the status."))
    result = task_completion(model="openai:/stub")(
        inputs=INPUTS, outputs=None, expectations={"goal": "Know the status."}
    )
    assert result.value is True
    assert result.rationale == 'Goal reached on turn 1. It told the status. Turn 1 reply: "waiting for stock"'


def test_policy_adherence_names_the_turn_and_the_guideline_it_broke(fake_model):
    calls = fake_model(adhered(), adhered(quote=""), adhered("pass", turn=0, guideline=0, quote="", reason="Fine."))
    judge = policy_adherence(model="openai:/stub")
    result = judge(inputs=INPUTS, outputs=None, expectations={"guidelines": GUIDELINES})
    assert result.value is False
    assert result.rationale == (
        'Turn 2 broke guideline 2: "Never ask again for something the customer already gave.". It asked again. '
        'Turn 2 reply: "What is your order number?"'
    )
    assert payload_of(calls[0])["guidelines"] == [
        {"guideline": 1, "text": GUIDELINES[0]},
        {"guideline": 2, "text": GUIDELINES[1]},
    ]
    assert calls[0]["response_format"] is _AdherenceJudgment
    assert (result.metadata["martian.judge.turn"], result.metadata["martian.judge.guideline"]) == ("2", "2")
    assert result.metadata["martian.judge.version"] == POLICY_ADHERENCE_VERSION
    # A reply that breaks a guideline by leaving something out has no words to quote.
    omitted = judge(inputs=INPUTS, outputs=None, expectations={"guidelines": GUIDELINES})
    assert omitted.rationale.endswith("It asked again.") and evidence_of(omitted) == []
    passed = judge(inputs=INPUTS, outputs=None, expectations={"guidelines": GUIDELINES})
    assert (passed.value, passed.rationale) == (True, "No reply broke a guideline. Fine.")


def test_knowledge_retention_names_the_turn_and_the_fact_it_forgot(fake_model):
    calls = fake_model(retained())
    result = knowledge_retention(model="openai:/stub")(inputs=INPUTS, outputs=None, expectations={})
    assert result.value is False
    assert result.rationale == (
        'Turn 2 contradicts or forgets what the user said on turn 1: "My order number is A-1002.". '
        'It forgot the order number. Turn 2 reply: "What is your order number?"'
    )
    assert [(place["field"], place["turn"], place["role"]) for place in evidence_of(result)] == [
        ("fact", 1, "user"),
        ("quote", 2, "assistant"),
    ]
    assert set(payload_of(calls[0])) == {"conversation"}  # it takes no criteria
    assert calls[0]["response_format"] is _RetentionJudgment
    assert result.metadata["martian.judge.version"] == KNOWLEDGE_RETENTION_VERSION
    assert (result.metadata["martian.judge.fact_turn"], result.metadata["martian.judge.turn"]) == ("1", "2")


@pytest.mark.parametrize(
    ("judge", "expectations", "reply", "message"),
    [
        (
            task_completion,
            {"goal": GOAL},
            completed(turn=3),
            r"turn 3 is not a turn it may name, 1 to 2 \[judge_invalid_turn\]",
        ),
        (task_completion, {"goal": GOAL}, completed("pass", turn=0), r"gave no turn \[judge_missing_turn\]"),
        (task_completion, {"goal": GOAL}, completed("pass", quote=" "), r"gave no quote .* \[judge_missing_quote\]"),
        # The quote is from the reply in turn 1, not the turn the verdict names.
        (task_completion, {"goal": GOAL}, completed(quote="waiting for stock"), r"in turn 2 \[judge_invalid_quote\]"),
        (task_completion, {"goal": GOAL}, completed("uncertain", turn=0, quote=""), r"\[judge_uncertain\]"),
        (task_completion, {"goal": GOAL}, {**completed(), "turn": "2"}, "invalid task completion schema"),
        (task_completion, {"goal": GOAL}, {**completed(), "turns": [2]}, "invalid task completion schema"),
        (policy_adherence, {"guidelines": GUIDELINES}, adhered(guideline=3), r"\[judge_invalid_guideline\]"),
        (policy_adherence, {"guidelines": GUIDELINES}, adhered(turn=0), r"\[judge_missing_turn\]"),
        (policy_adherence, {"guidelines": GUIDELINES}, adhered(quote="Never ask"), r"\[judge_invalid_quote\]"),
        # The fact is the user's words, not the assistant's.
        (knowledge_retention, {}, retained(fact="Order A-1002 is waiting for stock."), r"user's message in turn 1"),
        (knowledge_retention, {}, retained(fact=" "), r"gave no fact quote .* \[judge_missing_quote\]"),
        (knowledge_retention, {}, retained(fact_turn=2, turn=1), r"turn 1 is not a turn it may name, 2 to 2"),
        (knowledge_retention, {}, retained(fact_turn=0), r"gave no fact_turn \[judge_missing_turn\]"),
    ],
)
def test_evidence_that_is_not_in_the_conversation_is_an_error(fake_model, judge, expectations, reply, message):
    calls = fake_model(reply)
    with pytest.raises(ValueError, match=message):
        judge(model="openai:/stub")(inputs=INPUTS, outputs=None, expectations=expectations)
    assert len(calls) == 1  # never asked again


def test_the_quote_error_never_repeats_the_model_text(fake_model):
    fake_model(retained(quote="private-synthetic-canary"))
    with pytest.raises(ValueError) as raised:
        knowledge_retention(model="openai:/stub")(inputs=INPUTS, outputs=None, expectations={})
    assert "private-synthetic-canary" not in str(raised.value)


@pytest.mark.parametrize(
    ("judge", "inputs", "expectations", "message"),
    [
        (knowledge_retention, {"message": "hi"}, {}, "needs the conversation at inputs.messages"),
        (knowledge_retention, {"messages": []}, {}, "a list of user and assistant messages"),
        (knowledge_retention, {"messages": MESSAGES[:4]}, {}, "followed by the assistant's reply"),
        (knowledge_retention, {"messages": MESSAGES[2:]}, {}, "starting with the user's"),
        (knowledge_retention, {"messages": [{"role": "tool", "content": "x"}]}, {}, "role is user or assistant"),
        (knowledge_retention, {"messages": [{"role": "user", "content": 3}]}, {}, "its text as content"),
        (task_completion, INPUTS, {}, "expectations.goal must be the user's goal"),
        (task_completion, INPUTS, {"goal": " "}, "expectations.goal must be the user's goal"),
        (policy_adherence, INPUTS, {"guidelines": "one string"}, "expectations.guidelines must be a nonempty list"),
        (policy_adherence, INPUTS, {"guidelines": ["ok", ""]}, "expectations.guidelines must be a nonempty list"),
    ],
)
def test_a_conversation_or_criteria_it_cannot_read_fail_before_any_model_call(
    fake_model, judge, inputs, expectations, message
):
    calls = fake_model()
    with pytest.raises(ValueError, match=message):
        judge(model="openai:/stub")(inputs=inputs, outputs=None, expectations=expectations)
    assert calls == []


def test_conversation_turns_pairs_each_user_message_with_its_reply():
    assert conversation_turns(MESSAGES) == [
        ("My order number is A-1002.", "Order A-1002 is waiting for stock."),
        ("Then please cancel it.", ASKED_AGAIN),
    ]


@pytest.mark.parametrize("schema", [_CompletionJudgment, _AdherenceJudgment, _RetentionJudgment])
def test_each_wire_schema_is_one_flat_object(schema):
    """GLM 5 breaks nested JSON in structured output: the verdict holds only strings and numbers."""
    definitions = schema.model_json_schema()["$defs"]
    assert len(definitions) == 1
    (verdict,) = definitions.values()
    assert list(verdict["properties"])[0] == "reason" and list(verdict["properties"])[-1] == "verdict"
    assert all(field.get("type") in ("string", "integer") for field in verdict["properties"].values())


def test_the_options_are_checked_by_the_shared_engine_when_the_judge_is_made():
    with pytest.raises(ValueError, match="explicit judge model"):
        task_completion(model=" ")
    with pytest.raises(ValueError, match="Unsupported judge inference parameter"):
        policy_adherence(model="openai:/stub", inference_params={"top_k": 3})
    with pytest.raises(ValueError, match="line break"):
        knowledge_retention(model="openai:/stub", headers={"X-Key": "a\r\nb"})


def chat_reply(content, model="fixture-model-1"):
    return {
        "id": "fixture",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150},
    }


def test_a_judgment_through_the_shared_transport_asks_again_only_without_structured_output(monkeypatch):
    """The engine's one re-ask: an endpoint that declines structured output is asked once without
    it. A reply that is not the flat verdict is final: it is an error, never asked again."""
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-only")
    monkeypatch.setenv("OPENAI_API_BASE", "http://127.0.0.1:9/v1")
    sent = []
    replies = [
        ChatCompletionError(status_code=400, message="response_format json_schema is not supported"),
        chat_reply(json.dumps({"result": retained()})),
        chat_reply(json.dumps({"result": {"verdict": {"value": "fail"}}})),
    ]

    def post(endpoint, headers, payload):
        sent.append((headers, payload))
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr("martian.evals._judge_transport._post_chat", post)
    judge = knowledge_retention(model="openai:/fixture", inference_params={"temperature": 0}, headers={"X-Team": "a"})
    result = judge(inputs=INPUTS, outputs=None, expectations={})
    assert result.value is False
    assert [("response_format" in payload) for _, payload in sent] == [True, False]
    assert sent[0][1]["response_format"]["type"] == "json_schema" and sent[0][0]["X-Team"] == "a"
    assert result.metadata["martian.judge.response_format"] == "none"
    assert result.metadata["martian.judge.model_version"] == "fixture-model-1"
    assert result.metadata["mlflow.assessment.judgeInputTokens"] == 120
    sent.clear()
    with pytest.raises(ValueError, match=r"\[judge_invalid_schema\]"):
        judge(inputs=INPUTS, outputs=None, expectations={})
    assert len(sent) == 1


# ---------- the suite file ----------

SUITE = """data: cases.jsonl
target:
  file: agent.py
  function: reply
judge:
  model: openai:/stub
  inference_params: {temperature: 0}
checks:
  - type: length
    name: short
    max_length: 200
  - type: task_completion
    scope: conversation
  - type: policy_adherence
    scope: conversation
    guidelines: ["Never ask again for something the customer already gave."]
  - type: knowledge_retention
    name: remembers
    scope: conversation
"""

CASES = [
    {
        "id": "shipped-order",
        "turns": [
            {"input": "Hi, I ordered a lamp last week."},
            {"input": "It's order A-1001. Where is it?"},
            {"input": "Can I still cancel it?"},
        ],
        "expectations": {
            "goal": "Find out whether order A-1001 can still be cancelled.",
            # The case's own guidelines replace the check's.
            "guidelines": ["Never reveal another customer's order.", "Say when an order cannot be cancelled."],
        },
    },
    {
        "id": "cancel-later",
        "turns": [
            {"input": "My order number is A-1002."},
            {"input": "Is it in stock yet?"},
            {"input": "Then please cancel it."},
        ],
        "expectations": {"goal": "Cancel order A-1002."},
    },
]


def write(folder, suite=SUITE, cases=CASES):
    shutil.copy(AGENT, folder / "agent.py")
    (folder / "cases.jsonl").write_text("".join(json.dumps(case) + "\n" for case in cases), encoding="utf-8")
    (folder / "evals.yaml").write_text(suite, encoding="utf-8")
    return folder / "evals.yaml"


def test_the_suite_file_holds_the_conversation_judges_and_writes_them_back(tmp_path):
    suite, cases = load_suite_file(write(tmp_path))
    judges = [scorer for scorer in suite.scorers if scorer.kind == "llm_judge"]
    assert [(s.name, s.type, s.scope, s.criteria, s.model) for s in judges] == [
        ("task_completion_judge", "task_completion", "conversation", {}, "openai:/stub"),
        (
            "policy_adherence_judge",
            "policy_adherence",
            "conversation",
            {"guidelines": ["Never ask again for something the customer already gave."]},
            "openai:/stub",
        ),
        ("remembers", "knowledge_retention", "conversation", {}, "openai:/stub"),
    ]
    text = suite_to_yaml(suite)
    assert yaml.safe_load(text)["checks"][2]["guidelines"] == [
        "Never ask again for something the customer already gave."
    ]
    assert suite_from_yaml(text, name=suite.name, cases=cases) == suite
    bad = suite.model_copy(
        update={"scorers": [*suite.scorers[:3], suite.scorers[3].model_copy(update={"criteria": {"rubric": "x"}})]}
    )
    with pytest.raises(ConfigError, match="knowledge_retention judges have no criteria"):
        suite_to_yaml(bad)


@pytest.mark.parametrize(
    ("suite", "cases", "problem"),
    [
        # A conversation judge on a case of one input is refused when the suite loads.
        (
            SUITE,
            [*CASES, {"id": "one", "input": "Where is my order?"}],
            "case one has one input, and task_completion_judge",
        ),
        (
            SUITE.replace(
                "  - type: knowledge_retention\n    name: remembers\n    scope: conversation\n",
                "  - type: knowledge_retention\n",
            ),
            CASES,
            "reads a whole conversation; give it scope: conversation",
        ),
        (
            SUITE,
            [CASES[0], {**CASES[1], "expectations": {}}],
            "case cancel-later: task_completion_judge .* needs the user's goal",
        ),
        (SUITE, [CASES[0], {**CASES[1], "expectations": {"goal": ""}}], "expectations.goal must be a nonempty string"),
        (
            SUITE.replace('    guidelines: ["Never ask again for something the customer already gave."]\n', ""),
            CASES,
            "case cancel-later: policy_adherence_judge .* needs guidelines",
        ),
        (
            SUITE,
            [CASES[0], {**CASES[1], "expectations": {"goal": "x", "guidelines": "one"}}],
            "guidelines must be a nonempty list",
        ),
        (SUITE.replace('guidelines: ["Never', 'guidelines: ["", "Never'), CASES, "guidelines must be a nonempty list"),
        (
            SUITE.replace("  - type: knowledge_retention\n", "  - type: knowledge_retention\n    rubric: x\n"),
            CASES,
            "rubric does not apply to knowledge_retention checks",
        ),
        # The other judges still grade each turn's reply.
        (
            SUITE.replace(
                "  - type: length\n",
                '  - type: rubric\n    rubric: "Pass."\n    scope: conversation\n  - type: length\n',
            ),
            CASES,
            "scope: conversation is for deterministic checks, python checks and the conversation judges",
        ),
    ],
    ids=[
        "single-turn-case",
        "no-scope",
        "no-goal",
        "empty-goal",
        "no-guidelines",
        "case-guidelines-not-a-list",
        "check-guidelines-empty",
        "option-of-another-type",
        "rubric-at-conversation-scope",
    ],
)
def test_a_conversation_judge_the_data_cannot_feed_is_refused_at_load(tmp_path, suite, cases, problem):
    with pytest.raises(ConfigError, match=problem):
        load_suite_file(write(tmp_path, suite, cases))


# ---------- a run: results, summary, comment, turns route and read routes ----------


@pytest.fixture
def isolated_run(monkeypatch, tmp_path):
    for name in ("MARTIAN_API_URL", "MARTIAN_API_TOKEN", "MARTIAN_AGENT_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MLFLOW_DISABLE_TELEMETRY", "true")
    monkeypatch.setenv("MARTIAN_EVALS_HOME", str(tmp_path / "evals-home"))
    monkeypatch.setattr(mlflow.tracking.fluent, "_active_experiment_id", None)
    previous = mlflow.get_tracking_uri()
    STATE.reset()
    yield
    STATE.reset()
    mlflow.set_tracking_uri(previous)


TOKENS = {"mlflow.assessment.judgeInputTokens": 40, "mlflow.assessment.judgeOutputTokens": 10}


def asked_again(conversation):
    """The first turn whose reply asks for the order number after the user gave it, and that turn."""
    given = None
    for turn in conversation:
        if given is None and "A-" in turn["user"]:
            given = turn
        if given is not None and "What is your order number?" in turn["assistant"]:
            return turn, given
    return None, None


@pytest.fixture
def fake_judge(monkeypatch):
    """A judge that reads the conversation it is sent: a reply asking for the order number after the
    user gave it breaks the guideline and forgets the fact, and a cancellation completes the goal."""
    calls = []

    def provider(**kwargs):
        calls.append(kwargs)
        payload = payload_of(kwargs)
        conversation = payload["conversation"]
        forgot, given = asked_again(conversation)
        if kwargs["response_format"] is _CompletionJudgment:
            done = next((turn for turn in conversation if "cancel" in turn["assistant"]), None)
            value = (
                completed("pass", turn=done["turn"], quote="cancel", reason="It answered about cancelling.")
                if done
                else completed(
                    "fail", turn=forgot["turn"] if forgot else 0, quote="What is your order number?" if forgot else ""
                )
            )
        elif kwargs["response_format"] is _AdherenceJudgment:
            value = (
                adhered(turn=forgot["turn"], guideline=1)
                if forgot
                else adhered("pass", turn=0, guideline=0, quote="", reason="Fine.")
            )
        else:
            value = (
                retained(fact_turn=given["turn"], fact=given["user"], turn=forgot["turn"])
                if forgot
                else retained("pass", fact_turn=0, fact="", turn=0, quote="")
            )
        return Feedback(name=kwargs["assessment_name"], value=value, metadata=dict(TOKENS))

    monkeypatch.setattr("martian.evals.conversation_judges.invoke_response_judge", provider)
    return calls


def results_of(folder):
    return {
        record["id"]: record for record in map(json.loads, (folder / "results/results.jsonl").read_text().splitlines())
    }


def local_ui(folder: Path) -> TestClient:
    ui = folder / "ui"
    ui.mkdir(exist_ok=True)
    (ui / "index.html").write_text("<!doctype html><title>Martian Evals</title>", encoding="utf-8")
    return TestClient(create_local_app(folder / "evals-home", ui), base_url="http://127.0.0.1:7600")


def pr_comment(folder, exit_code):
    spec = importlib.util.spec_from_file_location("evals_pr_comment", REPOSITORY / "scripts/evals_pr_comment.py")
    assert spec is not None and spec.loader is not None
    comment = importlib.util.module_from_spec(spec)
    sys.modules["evals_pr_comment"] = comment
    spec.loader.exec_module(comment)
    return comment.render(comment.load_outcome(folder / "results", exit_code), suite="evals.yaml", key="evals.yaml")


def test_a_scripted_conversation_is_judged_whole_and_shows_wherever_results_do(tmp_path, isolated_run, fake_judge):
    summary = run_suite(write(tmp_path))
    found = results_of(tmp_path)
    passing, failing = found["shipped-order"], found["cancel-later"]

    # results.jsonl: each judge's verdict on the whole conversation, its rationale naming the turn.
    assert failing["checks"]["remembers"] == {
        "passed": False,
        "rationale": 'Turn 3 contradicts or forgets what the user said on turn 1: "My order number is A-1002.". '
        'It forgot the order number. Turn 3 reply: "What is your order number?"',
        "error": None,
    }
    assert failing["checks"]["policy_adherence_judge"]["rationale"].startswith(
        'Turn 3 broke guideline 1: "Never ask again for something the customer already gave.".'
    )
    assert failing["checks"]["task_completion_judge"]["rationale"].startswith("Goal not reached; fell short on turn 3.")
    assert failing["checks"]["short"]["passed"] is True and failing["passed"] is False
    assert passing["passed"] is True
    assert passing["checks"]["task_completion_judge"]["rationale"].startswith("Goal reached on turn 3.")
    # A conversation judge grades the conversation, not a turn.
    assert all(set(turn["checks"]) == {"short"} for turn in failing["turns"])

    # What each judge was sent: the conversation as the target answered it, the case's goal, and
    # the case's guidelines or else the check's.
    sent = {
        (call["assessment_name"], payload_of(call)["conversation"][0]["user"]): payload_of(call) for call in fake_judge
    }
    assert len(fake_judge) == 6
    shipped = sent[("task_completion_judge", "Hi, I ordered a lamp last week.")]
    assert shipped["goal"] == "Find out whether order A-1001 can still be cancelled."
    assert [turn["assistant"] for turn in shipped["conversation"]] == [
        ASKED_AGAIN,
        "Order A-1001 is shipped.",
        "Order A-1001 has already shipped, so it cannot be cancelled. You can return it within 30 days.",
    ]
    assert [g["text"] for g in sent[("policy_adherence_judge", "Hi, I ordered a lamp last week.")]["guidelines"]] == [
        "Never reveal another customer's order.",
        "Say when an order cannot be cancelled.",
    ]
    assert [g["text"] for g in sent[("policy_adherence_judge", "My order number is A-1002.")]["guidelines"]] == [
        "Never ask again for something the customer already gave."
    ]

    # The summary counts each judge as a conversation check.
    counted = ("passed", "failed", "errors", "scope")
    assert {
        label: {k: summary["checks"][label][k] for k in counted}
        for label in ("task_completion_judge", "policy_adherence_judge", "remembers")
    } == {
        label: {"passed": 1, "failed": 1, "errors": 0, "scope": "conversation"}
        for label in ("task_completion_judge", "policy_adherence_judge", "remembers")
    }
    assert "turns" not in summary["checks"]["remembers"]

    # The pull-request comment: a row per judge, and the judges among the failed case's checks.
    body = pr_comment(tmp_path, 1)
    assert "| `task_completion_judge` | 1 | 1 | 0 |" in body and "| `remembers` | 1 | 1 | 0 |" in body
    assert "| `cancel-later` | `task_completion_judge`, `policy_adherence_judge`, `remembers` |" in body
    assert "What is your order number?" not in body  # never a reply or a judge's reason

    # The core's turns route and read routes.
    client = local_ui(tmp_path)
    run_id = summary["run"]["id"]
    detail = client.get(f"/v1/evals/{run_id}").json()
    validate_response("get", "/v1/evals/{run_id}", detail)
    assert {item["dataset_item_id"]: item["status"] for item in detail["items"]} == {
        "shipped-order": "passed",
        "cancel-later": "failed",
    }
    talks = {item["dataset_item_id"]: item for item in client.get(f"/v1/evals/{run_id}/turns").json()["items"]}
    assert talks["cancel-later"]["checks"]["remembers"] == {**failing["checks"]["remembers"], "error": ""}
    assert talks["shipped-order"]["checks"]["task_completion_judge"]["passed"] is True
    _, records = open_stores(tmp_path / "evals-home")
    try:
        run = asyncio.run(records.get_run(SINGLE_USER, "default", run_id))
        assert run is not None
        judged = {s.name: (s.kind, s.type, s.scope, s.model) for s in run.scorers if s.kind == "llm_judge"}
        assert judged["remembers"] == ("llm_judge", "knowledge_retention", "conversation", "openai:/stub")
        scores = asyncio.run(records.list_scores(SINGLE_USER, "default", run_id))
        mine = {(s.case_id, s.scorer): s for s in scores}
        assert mine[("cancel-later", "remembers")].rationale == failing["checks"]["remembers"]["rationale"]
        assert {s.source for s in scores if s.scorer != "short"} == {"llm_judge"}
        assert {s.source_id for s in scores if s.scorer != "short"} == {"openai:/stub"}
    finally:
        records.close()


def test_the_judges_are_scorers_for_evaluate_as_the_guide_shows(tmp_path, isolated_run, fake_judge, monkeypatch):
    from martian.evals import GateFailure, assert_checks, evaluate

    monkeypatch.setenv("MLFLOW_GENAI_EVAL_SKIP_TRACE_VALIDATION", "true")
    mlflow.set_tracking_uri(f"sqlite:///{tmp_path / 'tracking.db'}")
    messages = [message for message in MESSAGES if message["role"] != "system"]
    data = [{"inputs": {"messages": messages}, "outputs": ASKED_AGAIN, "expectations": {"goal": GOAL}}]
    result = evaluate(
        data=data,
        scorers=[task_completion(model="openai:/stub"), knowledge_retention(model="openai:/stub")],
    )
    frame = result.result_df
    assert (frame["task_completion/value"].iloc[0], frame["knowledge_retention/value"].iloc[0]) == (False, False)
    assert frame["knowledge_retention/rationale"].iloc[0].startswith("Turn 2 contradicts or forgets")
    with pytest.raises(GateFailure, match="knowledge_retention"):
        assert_checks(result)


def test_the_printed_table_lists_the_conversation_judges(tmp_path, isolated_run, fake_judge):
    from typer.testing import CliRunner

    from martian.cli import app

    result = CliRunner().invoke(app, ["evals", "run", str(write(tmp_path))])
    assert result.exit_code == 1  # cancel-later failed
    for label in ("task_completion_judge", "policy_adherence_judge", "remembers"):
        assert label in result.output


def test_the_conversation_judges_tokens_count_towards_the_run_limit(tmp_path, isolated_run, fake_judge):
    # The turn checks are deterministic, so only the conversation judges spend judge tokens: after
    # the first conversation they have spent 150 of 100, and the run stops before the second.
    path = write(tmp_path, SUITE + "batch: 1\nlimits:\n  max_judge_tokens: 100\n")
    summary = run_suite(path)
    assert (summary["cases"], summary["not_run"]) == (1, 1)
    assert summary["stopped"]["limit"] == "max_judge_tokens" and summary["stopped"]["used"] == 150


def test_a_judge_error_leaves_the_conversation_without_a_verdict(tmp_path, isolated_run, monkeypatch):
    def provider(**kwargs):
        return Feedback(name=kwargs["assessment_name"], value=completed("pass", turn=9), metadata={})

    monkeypatch.setattr("martian.evals.conversation_judges.invoke_response_judge", provider)
    suite = SUITE.split("  - type: policy_adherence")[0]
    summary = run_suite(write(tmp_path, suite))
    record = results_of(tmp_path)["cancel-later"]
    assert record["passed"] is None and record["checks"]["task_completion_judge"]["passed"] is None
    assert "[judge_invalid_turn]" in record["checks"]["task_completion_judge"]["error"]
    assert summary["errors"] == 2 and list(summary["check_errors"]) == ["task_completion_judge"]


# ---------- simulated conversations ----------

SIMULATED_SUITE = """data: cases.jsonl
target:
  file: agent.py
  function: reply
simulator:
  model: openai:/simulator
judge:
  model: openai:/stub
checks:
  - type: task_completion
    scope: conversation
"""

SIMULATED = [
    {
        "id": "polite",
        "simulated_user": {
            "goal": "Cancel order A-1002 and be told you will be refunded.",
            "persona": "polite",
            "max_turns": 2,
            "stop_when": "The order is cancelled.",
        },
    },
    {
        "id": "terse",
        "simulated_user": {"goal": "Cancel A-1002.", "persona": "terse", "max_turns": 2, "stop_when": "Done."},
        # The case's own goal is the judge's, not its simulated user's.
        "expectations": {"goal": "Be told about the refund."},
    },
]


def test_a_simulated_conversation_is_judged_against_its_users_goal(tmp_path, isolated_run, fake_judge, monkeypatch):
    def simulate(**kwargs):
        played = kwargs["prompt"][0].content.split("The user you play:\n", 1)[1].split("\nOutput schema:", 1)[0]
        asked = json.loads(kwargs["prompt"][1].content)
        message = "Please cancel my order A-1002." if "polite" in played else "Cancel A-1002 now."
        if asked["messages_sent"]:
            return _Step(message="", done=True, goal_reached=True, reason="Cancelled."), {}
        return _Step(message=message, done=False, goal_reached=False, reason="Go on."), {}

    monkeypatch.setattr("martian.evals.simulation.invoke_structured", simulate)
    summary = run_suite(write(tmp_path, SIMULATED_SUITE, SIMULATED))
    goals = {payload_of(call)["conversation"][0]["user"]: payload_of(call)["goal"] for call in fake_judge}
    assert goals == {
        "Please cancel my order A-1002.": "Cancel order A-1002 and be told you will be refunded.",
        "Cancel A-1002 now.": "Be told about the refund.",
    }
    found = results_of(tmp_path)
    assert found["polite"]["simulated"] is True
    assert found["polite"]["checks"]["task_completion_judge"]["rationale"].startswith("Goal reached on turn 1.")
    assert set(found["polite"]["checks"]) == {"task_completion_judge", "goal_reached"}
    assert summary["checks"]["task_completion_judge"]["passed"] == 2


# ---------- the labelled set ----------

BENCHMARK = Path(__file__).parent / "benchmarks" / "conversation_judges.jsonl"


def test_the_labelled_set_has_balanced_well_formed_conversations_for_each_judge():
    """The set scripts/benchmark_conversation_judges.py scores the judges against (docs/evals.md)."""
    cases = [json.loads(line) for line in BENCHMARK.read_text(encoding="utf-8").splitlines()]
    assert len({case["id"] for case in cases}) == len(cases)
    for judge in runner.CONVERSATION_JUDGES:
        labels = [case["label"] for case in cases if case["judge"] == judge]
        assert len(labels) >= 10 and labels.count("pass") == labels.count("fail"), judge
    for case in cases:
        turns = conversation_turns(case["messages"])  # user and assistant messages in turn
        assert case["label"] in ("pass", "fail") and case["why"].strip()
        assert ("goal" in case) == (case["judge"] == "task_completion")
        assert ("guidelines" in case) == (case["judge"] == "policy_adherence")
        named = {"turn", "guideline", "fact_turn"} & set(case)
        if case["label"] == "pass":
            assert not named, case["id"]
            continue
        # A labelled fail names the turn the judge should name, and its guideline or fact's turn.
        assert 1 <= case["turn"] <= len(turns), case["id"]
        if case["judge"] == "policy_adherence":
            assert 1 <= case["guideline"] <= len(case["guidelines"]), case["id"]
        if case["judge"] == "knowledge_retention":
            assert 1 <= case["fact_turn"] <= case["turn"], case["id"]
        assert set(case) <= {"id", "judge", "label", "goal", "guidelines", "messages", "why", *named}
