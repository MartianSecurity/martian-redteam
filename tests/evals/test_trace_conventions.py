"""The mapping of the three trace attribute styles onto the shared model's trace fields, on the
hand-written samples in ``trace_samples``. No store, network or model is involved."""

import json

import pytest

from martian.evals.model import Span, Trace
from martian.evals.trace_conventions import (
    MAPPING_KEY,
    MAPPING_VERSION,
    STYLES,
    TABLES,
    V1,
    ToolCall,
    map_attributes,
    map_span,
    map_trace,
    mapping_table,
)
from tests.evals.trace_samples import ANSWER, ARGUMENTS, MODEL, QUESTION, RESULT, UNKNOWN, sample_spans, sample_trace

KINDS = {"otel_genai": "CHAT_MODEL", "openinference": "LLM", "mlflow": "CHAT_MODEL"}


@pytest.mark.parametrize("style", STYLES)
def test_a_model_call_in_each_style_gives_its_model_tokens_inputs_and_tool_calls(style):
    _, model_call, _ = sample_spans(style)
    mapped = map_attributes(model_call.attributes)
    assert (mapped.kind, mapped.model, mapped.provider) == (KINDS[style], MODEL, "openai")
    assert mapped.token_usage() == {"input_tokens": 12, "output_tokens": 8, "total_tokens": 20}
    assert QUESTION in json.dumps(mapped.inputs)
    assert mapped.tool_calls == [ToolCall(id="call_1", name="order_status", arguments=ARGUMENTS)]
    assert mapped.tool is None


@pytest.mark.parametrize("style", STYLES)
def test_a_tool_call_in_each_style_gives_the_tool_its_arguments_and_result(style):
    _, _, tool = sample_spans(style)
    mapped = map_span(tool)
    # MLflow's span attributes have no tool call id; the span's name is the tool's.
    call_id = None if style == "mlflow" else "call_1"
    assert mapped.kind == "TOOL" and mapped.tool == ToolCall(id=call_id, name="order_status", arguments=ARGUMENTS)
    assert (mapped.inputs, mapped.outputs, mapped.tool_calls) == (ARGUMENTS, RESULT, [])


@pytest.mark.parametrize("style", STYLES)
def test_a_trace_is_filled_from_its_spans_and_names_the_mapping_version(style):
    sample = sample_trace(style, "tr-" + "1" * 32)
    mapped = map_trace(sample)
    assert mapped.metadata == {MAPPING_KEY: MAPPING_VERSION} and MAPPING_VERSION == "martian-trace-mapping-v1"
    assert (mapped.model, mapped.tokens, mapped.session_id, mapped.user_id) == (MODEL, 20, "conversation-1", "alice")
    assert QUESTION in mapped.request and ANSWER in mapped.response
    assert [s.kind for s in mapped.spans] == ["AGENT", KINDS[style], "TOOL"]
    assert mapped.spans[2].inputs == ARGUMENTS and mapped.spans[2].outputs == RESULT
    # No attribute is removed or rewritten, so the ones no convention defines are kept.
    assert [s.attributes for s in mapped.spans] == [s.attributes for s in sample.spans]
    assert mapped.spans[0].attributes.items() >= UNKNOWN.items()
    # A mapped trace keeps the version and fields it was mapped with.
    assert map_trace(mapped) is mapped


def test_what_a_trace_says_itself_wins_over_its_spans():
    sample = sample_trace("otel_genai", "tr-" + "2" * 32)
    spans = [sample.spans[0].model_copy(update={"kind": "CHAIN", "inputs": {"asked": "directly"}}), *sample.spans[1:]]
    given = sample.model_copy(
        update={"model": "set-model", "tokens": 3, "session_id": "s", "user_id": "u", "request": "hi", "spans": spans}
    )
    mapped = map_trace(given)
    assert (mapped.model, mapped.tokens, mapped.session_id, mapped.user_id, mapped.request) == (
        "set-model",
        3,
        "s",
        "u",
        "hi",
    )
    assert (mapped.spans[0].kind, mapped.spans[0].inputs) == ("CHAIN", {"asked": "directly"})
    assert map_span(Span(id="s", kind="UNKNOWN", attributes={"gen_ai.operation.name": "chat"})).kind == "CHAT_MODEL"


def test_a_parent_that_reports_usage_is_not_added_to_its_childrens():
    usage = lambda total: {"mlflow.chat.tokenUsage": json.dumps({"total_tokens": total})}  # noqa: E731
    spans = [
        Span(id="agent", start_ms=0, attributes=usage(30)),
        Span(id="call-1", parent_id="agent", start_ms=1, attributes=usage(20)),
        Span(id="call-2", parent_id="agent", start_ms=2, attributes=usage(10)),
        Span(id="step", start_ms=3, parent_id="agent"),
        Span(id="other", start_ms=4, attributes={"gen_ai.usage.input_tokens": 5}),
    ]
    assert map_trace(Trace(id="t", project_id="p", spans=spans)).tokens == 35


def test_earlier_and_alternative_keys_and_their_order():
    # Before OpenTelemetry 1.37.0 the provider was gen_ai.system; before 1.27.0 tokens were prompt and completion.
    older = map_attributes(
        {"gen_ai.system": "openai", "gen_ai.usage.prompt_tokens": "7", "gen_ai.usage.completion_tokens": 2}
    )
    assert (older.provider, older.input_tokens, older.output_tokens, older.total_tokens) == ("openai", 7, 2, 9)
    # The model that answered wins over the model asked for; structured values need no JSON text.
    newer = map_attributes(
        {
            "gen_ai.request.model": "asked",
            "gen_ai.response.model": "answered",
            "gen_ai.input.messages": [{"role": "user", "parts": [{"type": "text", "content": "hi"}]}],
            "gen_ai.output.messages": [{"role": "assistant", "parts": [{"type": "tool_call", "name": "look"}]}],
        }
    )
    assert newer.model == "answered" and newer.inputs[0]["role"] == "user"
    assert newer.tool_calls == [ToolCall(name="look")]
    # A kind the table does not list, an empty or negative value and a non-JSON input.value are not mapped.
    unlisted = map_attributes(
        {"gen_ai.operation.name": "dance", "gen_ai.usage.input_tokens": -1, "llm.model_name": " ", "input.value": "{"}
    )
    assert (unlisted.kind, unlisted.input_tokens, unlisted.model, unlisted.inputs) == (None, None, None, "{")
    assert map_attributes({}) == map_attributes({"acme.region": "eu-west"})


def test_the_table_is_versioned_and_covers_every_style():
    assert {V1.version: V1} == TABLES and mapping_table(MAPPING_VERSION) is V1
    with pytest.raises(ValueError, match="not one this release knows"):
        mapping_table("martian-trace-mapping-v0")
    with pytest.raises(ValueError):
        map_attributes({}, version="martian-trace-mapping-v0")
    covered = {(rule.field, rule.style) for rule in V1.rules}
    for field in ("kind", "model", "input_tokens", "output_tokens", "inputs", "outputs", "tool_calls"):
        assert {style for f, style in covered if f == field} == set(STYLES), field
    assert set(V1.conventions) == set(V1.kinds) == set(STYLES)
