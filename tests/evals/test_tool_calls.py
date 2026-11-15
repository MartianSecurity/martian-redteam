"""The tool-call checks: which tools an agent called, with what arguments, in which model turn,
read from its reply or from the trace of its call, with no model call."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from martian.config import ConfigError
from martian.evals import step_count, tool_called, tool_not_called, tool_trajectory
from martian.evals.runner import parse_suite, run_suite

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "evals" / "tool-calls"

LOOKUP = ("lookup_order", {"order_id": "A-1001"})
POLICY = ("get_policy", {"topic": "refunds"})
ELIGIBLE = ("check_eligibility", {"order_id": "A-1001"})
REFUND = ("issue_refund", {"order_id": "A-1001", "amount": 25.0})

# The refund agent's expected steps: the order first, then the policy and its eligibility together
# in one turn, then the refund.
STEPS = [
    {"name": "lookup_order", "arguments": {"order_id": "A-1001"}},
    ["get_policy", {"name": "check_eligibility", "arguments": {"order_id": "A-1001"}}],
    {"name": "issue_refund", "arguments": {"order_id": "A-1001", "amount": 25}},
]


def ask(*calls):
    """A model turn in OpenAI's chat format, asking for the calls together."""
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": f"call_{n}", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}
            for n, (name, arguments) in enumerate(calls)
        ],
    }


def conversation(*turns):
    """An agent's messages: the request, a model turn per list of calls with their results, and the answer."""
    messages = [{"role": "user", "content": "Order A-1001 arrived broken. Can I get my money back?"}]
    for turn in turns:
        messages.append(ask(*turn))
        messages.extend({"role": "tool", "tool_call_id": f"call_{n}", "content": "ok"} for n in range(len(turn)))
    return [*messages, {"role": "assistant", "content": "Your refund is on its way."}]


def verdict(check, outputs, expectations=None, trace=None):
    """The check's feedback on a row: a stored reply, unless a trace is given."""
    given = {"outputs": outputs, "trace": trace}
    return check(**given, expectations=expectations) if expectations is not None else check(**given)


def value(check, outputs, expectations=None, trace=None):
    return verdict(check, outputs, expectations, trace).value


SAMPLE = conversation([LOOKUP], [POLICY, ELIGIBLE], [REFUND])
WRONG_AMOUNT = conversation([LOOKUP], [POLICY, ELIGIBLE], [("issue_refund", {"order_id": "A-1001", "amount": 250})])
ONE_AT_A_TIME = conversation([LOOKUP], [POLICY], [ELIGIBLE], [REFUND])
WRONG_AMOUNT_REASON = "turn 3 of tool calls is not step 3: issue_refund is called with other arguments: amount is {}"


# --- the sample agent trajectory ----------------------------------------------------------------


def test_the_sample_trajectory_passes_and_a_wrong_argument_fails():
    check = tool_trajectory(*STEPS)
    feedback = verdict(check, SAMPLE)
    assert (feedback.value, feedback.rationale) == (True, "3 turns of tool calls, as expected")
    feedback = verdict(check, WRONG_AMOUNT)
    assert (feedback.value, feedback.rationale) == (False, WRONG_AMOUNT_REASON.format("250, expected 25"))


def test_parallel_calls_are_told_apart_from_sequential_ones():
    together, apart = [[POLICY, ELIGIBLE]], [[POLICY], [ELIGIBLE]]
    group = tool_trajectory(["get_policy", "check_eligibility"])
    one_then_other = tool_trajectory("get_policy", "check_eligibility")
    assert value(group, conversation(*together)) is True
    assert value(one_then_other, conversation(*apart)) is True
    feedback = verdict(group, conversation(*apart))
    assert (feedback.value, feedback.rationale) == (
        False,
        "turn 1 of tool calls is not step 1: check_eligibility is called in turn 2, not this one",
    )
    assert value(one_then_other, conversation(*together)) is False
    # in_order says the same: a step after another is in a later turn, a group in one turn.
    later = tool_trajectory("get_policy", "check_eligibility", order="in_order")
    feedback = verdict(later, conversation(*together))
    assert (feedback.value, feedback.rationale) == (
        False,
        "step 2 (check_eligibility) is made in the same turn as step 1 (turn 1), in parallel, not after it",
    )
    feedback = verdict(tool_trajectory(["get_policy", "check_eligibility"], order="in_order"), conversation(*apart))
    assert (
        feedback.rationale
        == "step 1 (get_policy and check_eligibility in one turn) has its calls in different turns, not together in one"
    )
    assert value(later, conversation(*apart)) is True
    # A parallel turn's calls may come in either order.
    assert value(group, conversation([ELIGIBLE, POLICY])) is True


def test_the_orders():
    assert value(tool_trajectory(*STEPS), ONE_AT_A_TIME) is False
    # in_order lets other calls come between and around the steps.
    in_order = tool_trajectory("lookup_order", "issue_refund", order="in_order")
    assert value(in_order, SAMPLE) is True
    assert verdict(in_order, SAMPLE).rationale == "2 steps in order, in turns 1 and 3"
    feedback = verdict(tool_trajectory("issue_refund", "lookup_order", order="in_order"), SAMPLE)
    assert (feedback.value, feedback.rationale) == (False, "step 2 (lookup_order) is made in turn 1, before step 1")
    # exact refuses any other call, and any other turn.
    assert value(tool_trajectory("lookup_order", ["get_policy", "check_eligibility"]), SAMPLE) is False
    feedback = verdict(
        tool_trajectory("lookup_order", ["get_policy", "check_eligibility"], "issue_refund"), ONE_AT_A_TIME
    )
    assert feedback.rationale.startswith("turn 2 of tool calls is not step 2")
    feedback = verdict(tool_trajectory("lookup_order"), SAMPLE)
    assert feedback.rationale == "3 turns of tool calls, expected 1: turn 2 calls get_policy, check_eligibility"
    # any_order needs every call, in any turn, and lets others come too.
    any_order = tool_trajectory("issue_refund", "check_eligibility", "lookup_order", order="any_order")
    assert value(any_order, ONE_AT_A_TIME) is True
    feedback = verdict(tool_trajectory("issue_refund", "close_account", order="any_order"), SAMPLE)
    assert (feedback.value, feedback.rationale) == (
        False,
        "1 of 2 expected calls not made: close_account is not called",
    )


def test_each_expected_call_takes_a_call_of_its_own():
    twice = conversation([("search", {"q": "a"})], [("search", {"q": "b"})])
    # The loose call could take either search; it must leave the one only q=b matches.
    calls = [{"name": "search"}, {"name": "search", "arguments": {"q": "b"}}]
    assert value(tool_trajectory(*calls, order="any_order"), twice) is True
    assert value(tool_trajectory(*calls, "search", order="any_order"), twice) is False
    parallel = conversation([("search", {"q": "a"}), ("search", {"q": "b"})])
    assert value(tool_trajectory(calls), parallel) is True
    # Two searches expected together, and only one made: the second is the one not made.
    feedback = verdict(tool_trajectory(["search", "search"], order="in_order"), conversation([("search", {"q": "a"})]))
    assert feedback.rationale == "step 1 (search and search in one turn) is not made: search is not called"


# --- arguments --------------------------------------------------------------------------------


def test_the_ways_arguments_match():
    call = conversation([("search", {"query": "refund policy", "filters": {"lang": "en", "limit": 5}, "page": 1})])
    subset = {"name": "search", "arguments": {"filters": {"lang": "en"}}}
    assert value(tool_called(subset), call) is True  # nested objects match by their given keys too
    assert value(tool_called({"name": "search", "arguments": {"page": True}}), call) is False  # true is not 1
    exact = {"name": "search", "arguments": {"query": "refund policy", "filters": {"lang": "en", "limit": 5}}}
    feedback = verdict(tool_called(exact, arguments_match="exact"), call)
    assert feedback.value is False
    assert feedback.rationale.endswith("(search is called with other arguments: page is not expected)")
    assert verdict(tool_called(exact), call).value is True  # the same arguments, as a subset
    exact["arguments"]["page"] = 1.0
    assert value(tool_called(exact, arguments_match="exact"), call) is True
    schema = {"type": "object", "required": ["query"], "properties": {"page": {"type": "integer", "maximum": 3}}}
    assert value(tool_called({"name": "search", "arguments": schema}, arguments_match="schema"), call) is True
    schema["properties"]["page"]["maximum"] = 0
    feedback = verdict(tool_called({"name": "search", "arguments": schema}, arguments_match="schema"), call)
    assert feedback.rationale == (
        "search(arguments matching the schema) called 0 times, expected at least 1 (search is called with other "
        "arguments: the arguments do not match the schema: $.page: 1 is outside maximum 0)"
    )
    pattern = {"name": "search", "arguments": {"query": "refund.*", "page": r"\d+"}}
    assert value(tool_called(pattern, arguments_match="pattern"), call) is True
    feedback = verdict(
        tool_called({"name": "search", "arguments": {"query": "refund"}}, arguments_match="pattern"), call
    )
    assert feedback.rationale == (  # a pattern matches the whole value
        'search(query~"refund") called 0 times, expected at least 1 '
        '(search is called with other arguments: query is "refund policy", which does not match refund)'
    )


def test_a_wrong_argument_names_what_differs():
    feedback = verdict(tool_called({"name": "lookup_order", "arguments": {"order_id": "A-1002"}}), SAMPLE)
    assert feedback.rationale == (
        'lookup_order(order_id="A-1002") called 0 times, expected at least 1 '
        '(lookup_order is called with other arguments: order_id is "A-1001", expected "A-1002")'
    )
    feedback = verdict(tool_called({"name": "lookup_order", "arguments": {"customer": "c-9"}}), SAMPLE)
    assert feedback.rationale.endswith("(lookup_order is called with other arguments: customer is missing)")
    call = conversation([("search", {"filters": {"lang": "en", "limit": 5}})])
    feedback = verdict(tool_called({"name": "search", "arguments": {"filters": {"lang": "fr"}}}), call)
    assert feedback.rationale.endswith('filters.lang is "en", expected "fr")')
    exact = {"name": "search", "arguments": {"filters": {"lang": "en"}}}
    feedback = verdict(tool_called(exact, arguments_match="exact"), call)
    assert feedback.rationale.endswith("filters.limit is not expected)")


# --- calls, counts and steps --------------------------------------------------------------------


def test_tool_called_counts_the_matching_calls():
    assert value(tool_called("lookup_order", "issue_refund"), SAMPLE) is True
    assert verdict(tool_called("lookup_order"), SAMPLE).rationale == "lookup_order called 1 time"
    feedback = verdict(tool_called("close_account"), SAMPLE)
    assert feedback.rationale == "close_account called 0 times, expected at least 1 (close_account is not called)"
    twice = conversation([LOOKUP], [LOOKUP])
    assert value(tool_called("lookup_order", max_calls=1), twice) is False
    assert verdict(tool_called("lookup_order", min_calls=2, max_calls=2), twice).value is True
    assert verdict(tool_called("lookup_order", max_calls=1), twice).rationale == (
        "lookup_order called 2 times, expected exactly 1"
    )
    assert verdict(tool_called("lookup_order", min_calls=0, max_calls=1), twice).rationale == (
        "lookup_order called 2 times, expected at most 1"
    )


def test_tool_not_called_fails_on_a_call_it_names():
    assert value(tool_not_called("close_account", "delete_order"), SAMPLE) is True
    feedback = verdict(tool_not_called("close_account", "issue_refund"), SAMPLE)
    assert (feedback.value, feedback.rationale) == (False, "issue_refund called 1 time")
    # With arguments, only a call with matching arguments counts.
    other_order = {"name": "issue_refund", "arguments": {"order_id": "B-7"}}
    assert value(tool_not_called(other_order), SAMPLE) is True


def test_step_count_counts_model_turns_and_tool_calls():
    assert verdict(step_count(max_turns=4, max_calls=4), SAMPLE).rationale == "4 model turns, 4 tool calls"
    feedback = verdict(step_count(max_turns=3, min_calls=5), SAMPLE)
    assert (feedback.value, feedback.rationale) == (
        False,
        "4 model turns, 4 tool calls, over the limit of 3 turns, under the minimum of 5 tool calls",
    )


def test_the_calls_can_come_from_each_case():
    check = tool_trajectory(expectation="expected_tool_calls")
    assert value(check, SAMPLE, {"expected_tool_calls": STEPS}) is True
    assert value(check, WRONG_AMOUNT, {"expected_tool_calls": STEPS}) is False
    # MLflow's expected_tool_calls form: a list of {name, arguments}.
    called = tool_called(expectation="expected_tool_calls")
    assert value(called, SAMPLE, {"expected_tool_calls": [{"name": "issue_refund", "arguments": {"amount": 25}}]})
    for bad in (None, [], "issue_refund", [{"tool": "x"}], [["a", "b"]]):
        with pytest.raises(ValueError):
            verdict(called, SAMPLE, {"expected_tool_calls": bad})


# --- reading the reply --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param({"messages": SAMPLE}, id="object-with-messages"),
        pytest.param(json.dumps(SAMPLE), id="json-text"),
        pytest.param({"choices": [{"message": ask(LOOKUP)}]}, id="chat-completion"),
        pytest.param(
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "t1", "name": "lookup_order", "input": LOOKUP[1]}],
            },
            id="anthropic",
        ),
        pytest.param(
            {"type": "ai", "tool_calls": [{"name": "lookup_order", "args": LOOKUP[1], "id": "t1"}]}, id="langchain"
        ),
        pytest.param(
            {"role": "assistant", "parts": [{"type": "tool_call", "name": "lookup_order", "arguments": LOOKUP[1]}]},
            id="opentelemetry",
        ),
        pytest.param(
            {"role": "model", "parts": [{"functionCall": {"name": "lookup_order", "args": LOOKUP[1]}}]}, id="gemini"
        ),
        pytest.param(
            {"role": "assistant", "function_call": {"name": "lookup_order", "arguments": json.dumps(LOOKUP[1])}},
            id="openai-function-call",
        ),
    ],
)
def test_the_reply_forms_read(reply):
    before = json.dumps(reply)
    assert value(tool_called({"name": "lookup_order", "arguments": {"order_id": "A-1001"}}), reply) is True
    assert json.dumps(reply) == before  # reading leaves the reply as it was


@pytest.mark.parametrize(
    ("reply", "problem"),
    [
        ("I refunded your order.", "the reply is text, not messages"),
        ({"answer": "refunded"}, "the reply holds no messages"),
        ([], "the reply holds no messages"),
        # A Responses API output item is not a chat message, so its call is not silently missed.
        ([{"type": "function_call", "name": "close_account", "arguments": "{}"}], "the reply holds no messages"),
        ([{"role": "user", "content": "Hi"}, {"role": "planner", "content": "x"}], "the reply holds no messages"),
        ([{"role": "assistant", "tool_calls": [{"function": {"arguments": "{}"}}]}], "has no name"),
    ],
)
def test_a_reply_without_readable_tool_calls_is_a_scorer_error(reply, problem):
    # So tool_not_called cannot pass a reply it could not read.
    with pytest.raises(ValueError, match=problem):
        verdict(tool_not_called("close_account"), reply)


# --- reading the trace --------------------------------------------------------------------------


def span(name, kind, start, parent=None, inputs=None, outputs=None, attributes=None):
    return SimpleNamespace(
        span_id=f"s-{name}-{start}",
        parent_id=parent,
        name=name,
        span_type=kind,
        start_time_ns=start,
        inputs=inputs,
        outputs=outputs,
        attributes=attributes or {},
    )


def trace(*spans):
    return SimpleNamespace(data=SimpleNamespace(spans=list(spans)))


def test_a_tool_span_is_in_the_turn_of_the_model_span_before_it():
    root = span("agent", "AGENT", 0)
    first, second = span("chat", "CHAT_MODEL", 1, root.span_id), span("chat", "CHAT_MODEL", 5, root.span_id)
    spans = [
        root,
        first,
        span("get_policy", "TOOL", 2, root.span_id, {"topic": "refunds"}),
        span("check_eligibility", "TOOL", 3, root.span_id, {"order_id": "A-1001"}),
        second,
        span("issue_refund", "TOOL", 6, root.span_id, {"order_id": "A-1001", "amount": 25}),
        span("chat", "CHAT_MODEL", 8, root.span_id),
    ]
    steps = [["get_policy", "check_eligibility"], {"name": "issue_refund", "arguments": {"amount": 25}}]
    assert value(tool_trajectory(*steps), None, trace=trace(*spans)) is True
    assert verdict(step_count(max_turns=3), None, trace=trace(*spans)).rationale == "3 model turns, 3 tool calls"
    # Out of order in the list, read in order of start time.
    assert value(tool_trajectory(*steps), None, trace=trace(*reversed(spans))) is True
    # Spans inside a tool span are that tool's own work: a model and a tool a sub-agent calls.
    tool = spans[2]
    inner = [span("chat", "CHAT_MODEL", 2, tool.span_id), span("web_search", "TOOL", 2, tool.span_id, {"q": "x"})]
    assert value(tool_trajectory(*steps), None, trace=trace(*spans, *inner)) is True
    assert value(tool_not_called("web_search"), None, trace=trace(*spans, *inner)) is True


def test_the_tool_name_and_kind_come_from_the_trace_conventions():
    spans = [
        span("llm", "UNKNOWN", 1, attributes={"gen_ai.operation.name": "chat"}),
        span(
            "execute_tool",
            "UNKNOWN",
            2,
            attributes={
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": "lookup_order",
                "gen_ai.tool.call.arguments": '{"order_id": "A-1001"}',
            },
        ),
        span("run", "", 3, attributes={"openinference.span.kind": "TOOL", "tool.name": "get_policy"}),
        span("(merged tools)", "TOOL", 4),
    ]
    traced = trace(*spans)
    assert value(
        tool_trajectory([{"name": "lookup_order", "arguments": {"order_id": "A-1001"}}, "get_policy"]),
        None,
        trace=traced,
    )
    assert verdict(step_count(max_calls=2), None, trace=traced).rationale == "1 model turn, 2 tool calls"


def test_a_trace_that_shows_no_tool_use_is_a_scorer_error():
    with pytest.raises(ValueError, match="records no model or tool call"):
        verdict(tool_not_called("close_account"), None, trace=trace(span("agent", "UNKNOWN", 0)))
    # A model that asks for tools no tool span records: the tools are not traced.
    asking = span("chat", "CHAT_MODEL", 1, outputs={"choices": [{"message": ask(LOOKUP)}]})
    with pytest.raises(ValueError, match="records no tool span"):
        verdict(tool_not_called("close_account"), None, trace=trace(asking))
    # A model that answers without tools made no call, which is a verdict.
    answering = span(
        "chat", "CHAT_MODEL", 1, outputs={"choices": [{"message": {"role": "assistant", "content": "Hi"}}]}
    )
    assert value(tool_not_called("close_account"), None, trace=trace(answering)) is True


def test_calls_before_any_model_call_have_no_turn():
    spans = [span("lookup_order", "TOOL", 1, inputs=LOOKUP[1]), span("issue_refund", "TOOL", 2, inputs=REFUND[1])]
    assert value(tool_called("lookup_order", "issue_refund"), None, trace=trace(*spans)) is True
    assert value(tool_trajectory("lookup_order", "issue_refund", order="any_order"), None, trace=trace(*spans))
    with pytest.raises(ValueError, match="before any model call"):
        verdict(tool_trajectory("lookup_order", "issue_refund"), None, trace=trace(*spans))
    with pytest.raises(ValueError, match="turns cannot be counted"):
        verdict(step_count(max_turns=3), None, trace=trace(*spans))


def test_a_stored_output_is_read_from_the_reply_unless_the_trace_is_asked_for():
    stand_in = trace(span("root_span", "CHAIN", 0))
    assert value(tool_called("lookup_order"), SAMPLE, trace=stand_in) is True
    with pytest.raises(ValueError, match="there is no trace of its tool calls"):
        verdict(tool_called("lookup_order", source="trace"), SAMPLE, trace=stand_in)
    # A call's trace is read by default, and its reply when asked.
    traced = trace(span("chat", "CHAT_MODEL", 1), span("close_account", "TOOL", 2))
    assert value(tool_not_called("close_account"), SAMPLE, trace=traced) is False
    assert value(tool_not_called("close_account", source="reply"), SAMPLE, trace=traced) is True


def test_the_sample_agent_trace_passes_and_a_wrong_argument_trace_fails(monkeypatch, tmp_path):
    import mlflow
    from mlflow.entities import SpanType

    from martian.evals import GateFailure, assert_checks, evaluate
    from martian.evals.state import STATE

    def agent_with(turns):
        def agent(message):
            # A model call as MLflow's OpenAI tracing records it, then the tools it asked for.
            for turn in [*turns, []]:
                with mlflow.start_span(name="chat", span_type=SpanType.CHAT_MODEL) as model:
                    model.set_outputs({"choices": [{"message": ask(*turn) if turn else {"role": "assistant"}}]})
                for name, arguments in turn:
                    with mlflow.start_span(name=name, span_type=SpanType.TOOL) as tool:
                        tool.set_inputs(arguments)
                        tool.set_outputs("ok")
            return "Your refund is on its way."

        return agent

    old_uri = mlflow.get_tracking_uri()
    monkeypatch.setenv("MLFLOW_DISABLE_TELEMETRY", "true")
    monkeypatch.setenv("MLFLOW_GENAI_EVAL_SKIP_TRACE_VALIDATION", "true")
    monkeypatch.setattr(mlflow.tracking.fluent, "_active_experiment_id", None)
    mlflow.set_tracking_uri(f"sqlite:///{tmp_path / 'tracking.db'}")
    STATE.reset()
    try:
        data = [{"inputs": {"message": "Order A-1001 arrived broken. Can I get my money back?"}}]
        scorers = [tool_trajectory(*STEPS), tool_not_called("close_account"), step_count(max_turns=4)]
        assert_checks(
            evaluate(data=data, scorers=scorers, predict_fn=agent_with([[LOOKUP], [POLICY, ELIGIBLE], [REFUND]]))
        )
        wrong = agent_with([[LOOKUP], [POLICY, ELIGIBLE], [("issue_refund", {"order_id": "A-1001", "amount": 250})]])
        result = evaluate(data=data, scorers=scorers, predict_fn=wrong)
        reason = result.result_df["tool_trajectory/rationale"].iloc[0]
        assert reason == WRONG_AMOUNT_REASON.format("250, expected 25")
        with pytest.raises(GateFailure, match="1 failing check"):
            assert_checks(result)
        sequential = agent_with([[LOOKUP], [POLICY], [ELIGIBLE], [REFUND]])
        with pytest.raises(GateFailure, match="2 failing check"):  # the trajectory, and five turns
            assert_checks(evaluate(data=data, scorers=scorers, predict_fn=sequential))
    finally:
        STATE.reset()
        mlflow.set_tracking_uri(old_uri)


# --- options ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "build",
    [
        lambda: tool_called(),
        lambda: tool_called("search", expectation="expected_tool_calls"),
        lambda: tool_called(""),
        lambda: tool_called({"tool": "search"}),
        lambda: tool_called({"name": "search", "args": {}}),
        lambda: tool_called(["search", "lookup"]),  # only a trajectory groups calls
        lambda: tool_called({"name": "search", "arguments": ["a"]}),  # subset needs an object
        lambda: tool_called({"name": "search", "arguments": {"q": 1}}, arguments_match="pattern"),
        lambda: tool_called({"name": "search", "arguments": {"q": "("}}, arguments_match="pattern"),
        lambda: tool_called({"name": "search", "arguments": {"if": {}}}, arguments_match="schema"),
        lambda: tool_called("search", arguments_match="fuzzy"),
        lambda: tool_called("search", min_calls=0),  # passes every reply
        lambda: tool_called("search", min_calls=3, max_calls=2),
        lambda: tool_called("search", max_calls=-1),
        lambda: tool_called("search", source="logs"),
        lambda: tool_called(expectation="calls", name="calls"),
        lambda: tool_not_called(),
        lambda: tool_trajectory("search", order="sorted"),
        lambda: tool_trajectory(["search", "lookup"], order="any_order"),
        lambda: tool_trajectory([]),
        lambda: step_count(),
        lambda: step_count(max_turns=2.5),
        lambda: step_count(min_calls=True),
    ],
)
def test_misconfigured_tool_checks_raise_when_created(build):
    with pytest.raises(ValueError):
        build()


# --- the suite file -----------------------------------------------------------------------------


def suite(*checks, **fields):
    return yaml.safe_dump({"data": "cases.jsonl", "checks": list(checks), **fields})


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        (suite({"type": "tool_called", "calls": ["x"], "source": "trace"}), "stored outputs, which no call produced"),
        (
            suite(
                {"type": "tool_not_called", "calls": ["x"]},
                target={"url": "https://agent.example/chat", "body": {"message": "{{input}}"}},
            ),
            "set source: reply",
        ),
        (suite({"type": "tool_trajectory", "calls": [["a", "b"]], "order": "any_order"}), "any_order ignores turns"),
        (suite({"type": "tool_called", "calls": ["x"], "arguments_match": "fuzzy"}), "arguments_match must be one of"),
        (suite({"type": "tool_called"}), "give the calls to look for"),
        (suite({"type": "step_count"}), "needs min_turns, max_turns, min_calls or max_calls"),
    ],
)
def test_a_suite_file_refuses_tool_check_options_it_cannot_use(text, problem):
    with pytest.raises(ConfigError, match=problem):
        parse_suite(text)


def test_a_url_target_reads_the_tool_calls_in_its_response():
    text = suite(
        {"type": "tool_not_called", "calls": ["x"], "source": "reply"},
        target={"url": "https://agent.example/chat", "body": {"message": "{{input}}"}},
    )
    assert parse_suite(text).checks[0].source == "reply"


def test_the_tool_calls_example_passes_its_sample_and_fails_its_two_mistakes(tmp_path):
    for name in ("evals.yaml", "cases.jsonl"):
        (tmp_path / name).write_text((EXAMPLE / name).read_text(encoding="utf-8"), encoding="utf-8")
    summary = run_suite(tmp_path / "evals.yaml", home=tmp_path / "home")
    assert (summary["passed"], summary["failed"], summary["errors"]) == (1, 2, 0)
    records = {r["id"]: r for r in map(json.loads, (tmp_path / "results/results.jsonl").read_text().splitlines())}
    failing = {case: [c for c, v in r["checks"].items() if not v["passed"]] for case, r in records.items()}
    assert failing == {
        "follows-the-steps": [],
        "wrong-amount": ["tool_trajectory"],
        "one-at-a-time": ["tool_trajectory"],
    }
    reason = records["wrong-amount"]["checks"]["tool_trajectory"]["rationale"]
    assert reason == WRONG_AMOUNT_REASON.format("250.0, expected 25.0")
    assert records["one-at-a-time"]["checks"]["tool_trajectory"]["rationale"] == (
        'turn 2 of tool calls is not step 2: check_eligibility(order_id="A-1001") is called in turn 3, not this one'
    )
