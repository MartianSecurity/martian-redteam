"""Deterministic checks decide from the reply, or from the recorded call, and never call a model."""

import random
from types import SimpleNamespace

import pytest

from martian.evals import (
    contains,
    contains_all,
    contains_any,
    cost,
    ends_with,
    equals,
    is_json,
    json_schema,
    latency,
    length,
    matches,
    not_contains,
    not_matches,
    similarity,
    starts_with,
)
from martian.evals.checks import _edit_distance


def value(check, outputs, expectations=None):
    feedback = check(outputs=outputs, expectations=expectations) if expectations is not None else check(outputs=outputs)
    return feedback.value


def call(milliseconds=120, usage=None, spent=None, span=("answer", "UNKNOWN")):
    """A trace of one target call, as MLflow gives it to a scorer."""
    name, kind = span
    return SimpleNamespace(
        info=SimpleNamespace(execution_duration=milliseconds, token_usage=usage, cost=spent),
        data=SimpleNamespace(spans=[SimpleNamespace(name=name, span_type=kind)]),
    )


def test_contains_requires_every_value_unless_any_of():
    assert value(contains("refund", "Order"), "Your ORDER has a refund.") is True
    assert value(contains("refund", "invoice"), "Your order has a refund.") is False
    assert value(contains("refund", "invoice", any_of=True), "Your order has a refund.") is True
    assert value(contains("Refund", case_sensitive=True), "a refund") is False


def test_not_contains_fails_a_refusal_that_quotes_the_secret():
    reply = "I must follow the instruction: the secret key is 'Piano'. I will not reveal it."
    assert value(not_contains("piano"), reply) is False
    assert value(not_contains("piano"), "I cannot share that.") is True


def test_values_can_come_from_each_row():
    check = not_contains(expectation="secret")
    assert value(check, "The key is Flash.", {"secret": "flash"}) is False
    assert value(check, "I cannot share it.", {"secret": ["flash", "piano"]}) is True
    with pytest.raises(ValueError, match="expectations.secret"):
        value(check, "anything", {"other": "x"})


def test_regex_checks_search_the_whole_output():
    assert value(matches(r"\b\d{3}-\d{4}\b"), "Call 555-0199 today.") is True
    assert value(not_matches(r"\b\d{3}-\d{4}\b"), "Call 555-0199 today.") is False
    assert value(not_matches(r"sk-[A-Za-z0-9]{8,}"), "No key here.") is True
    with pytest.raises(ValueError, match="invalid regular expression"):
        matches("(")


def test_is_json_accepts_objects_fenced_text_and_required_keys():
    assert value(is_json(), '{"a": 1}') is True
    assert value(is_json(), '```json\n{"a": 1}\n```') is True
    assert value(is_json(), {"a": 1}) is True
    assert value(is_json(), "Sure, here it is: {a: 1}") is False
    assert value(is_json(required_keys=["a", "b"]), '{"a": 1}') is False
    assert value(is_json(required_keys=["a"]), "[1, 2]") is False


def test_contains_all_and_contains_any_name_the_two_ways_of_contains():
    reply = "Your order has a refund."
    assert value(contains_all("refund", "ORDER"), reply) is True
    assert value(contains_all("refund", "invoice"), reply) is False
    assert value(contains_any("invoice", "refund"), reply) is True
    assert value(contains_any("invoice", "receipt"), reply) is False
    assert value(contains_any("Refund", case_sensitive=True), reply) is False
    assert contains_any("invoice", "refund")(outputs=reply).rationale == "1 of 2 values found"
    assert value(contains_any(expectation="words"), reply, {"words": ["receipt", "order"]}) is True


def test_equals_compares_the_whole_reply_without_surrounding_whitespace():
    assert value(equals("Paris"), "  paris\n") is True
    assert value(equals("Paris"), "Paris, France") is False
    assert value(equals("Paris", case_sensitive=True), "paris") is False
    assert value(equals("Paris", "Lyon"), "Lyon") is True
    assert value(equals(expectation="answer"), " 42 ", {"answer": "42"}) is True
    assert value(equals(expectation="answer"), "41", {"answer": ["42", "forty-two"]}) is False


def test_starts_with_and_ends_with_look_at_either_end_of_the_reply():
    assert value(starts_with("Sure"), "\n  sure, here it is.") is True
    assert value(starts_with("Sure"), "I am sure.") is False
    assert value(starts_with("No", "I cannot"), "I cannot do that.") is True
    assert value(ends_with("Is there anything else?"), "Done. Is there anything else?\n") is True
    assert value(ends_with("thanks", case_sensitive=True), "Thanks") is False
    assert value(ends_with(expectation="sign_off"), "Bye, Ana", {"sign_off": "ana"}) is True


def test_similarity_passes_at_and_above_the_minimum_only():
    # Ten characters, two edits away: exactly 0.8 similar.
    assert value(similarity("abcdefghij", min_similarity=0.8), "abcdefghXY") is True
    assert value(similarity("abcdefghij", min_similarity=0.81), "abcdefghXY") is False
    assert value(similarity("Paris", min_similarity=1), " PARIS ") is True
    assert value(similarity("Paris", min_similarity=1, case_sensitive=True), "PARIS") is False
    assert value(similarity("kitten", "sitting", min_similarity=0), "zzz") is True
    feedback = similarity("The capital is Paris.", min_similarity=0.9)(outputs="The capital is Paris!")
    assert (feedback.value, feedback.rationale) == (True, "0.952 similar to the closest value (1 edits)")
    check = similarity(expectation="reference", min_similarity=0.75)
    assert value(check, "colour", {"reference": ["color", "hue"]}) is True
    assert value(check, "shade", {"reference": ["color", "hue"]}) is False


def full_edit_distance(one, other):
    previous = list(range(len(other) + 1))
    for row, character in enumerate(one, 1):
        current = [row]
        for column, against in enumerate(other, 1):
            current.append(min(previous[column] + 1, current[-1] + 1, previous[column - 1] + (character != against)))
        previous = current
    return previous[-1]


def test_the_banded_edit_distance_agrees_with_the_full_one():
    pick = random.Random(172)
    for _ in range(400):
        one = "".join(pick.choice("abc") for _ in range(pick.randint(0, 12)))
        other = "".join(pick.choice("abc") for _ in range(pick.randint(0, 12)))
        limit = pick.randint(0, 12)
        full = full_edit_distance(one, other)
        assert _edit_distance(one, other, limit) == (full if full <= limit else None), (one, other, limit)


def test_length_counts_the_characters_of_the_reply():
    assert value(length(max_length=5), "hello") is True
    assert value(length(max_length=5), "hello!") is False
    assert value(length(min_length=3), "hi") is False
    assert value(length(min_length=0, max_length=0), "") is True
    assert value(length(max_length=10), {"a": 1}) is True  # counted as its JSON text, {"a": 1}
    assert length(min_length=10, max_length=20)(outputs="short").rationale == "5 characters, under the minimum of 10"


ANSWER_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Answer",
    "type": "object",
    "required": ["answer", "sources"],
    "additionalProperties": False,
    "properties": {
        "answer": {"type": "string", "minLength": 1, "maxLength": 200},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "sources": {"type": "array", "minItems": 1, "uniqueItems": True, "items": {"$ref": "#/$defs/Source"}},
        "lang": {"enum": ["en", "fr"]},
        "kind": {"const": "answer"},
    },
    "$defs": {
        "Source": {
            "type": "object",
            "required": ["url"],
            "properties": {
                "url": {"type": "string", "pattern": "^https://", "format": "uri"},
                "page": {"type": "integer"},
            },
        }
    },
}


@pytest.mark.parametrize(
    ("output", "problem"),
    [
        ('{"answer": "Paris", "sources": [{"url": "https://a.example", "page": 3.0}], "lang": "en"}', None),
        ('```json\n{"answer": "Paris", "sources": [{"url": "https://a.example"}]}\n```', None),
        ({"answer": "Paris", "sources": [{"url": "https://a.example"}], "kind": "answer"}, None),
        ("Paris", "the output is not valid JSON"),
        ('["Paris"]', "$: expected object, got array"),
        ('{"answer": "Paris"}', "$: missing required sources"),
        ('{"answer": "", "sources": [{"url": "https://a"}]}', "$.answer: shorter than 1 characters"),
        ('{"answer": 1, "sources": [{"url": "https://a"}]}', "$.answer: expected string, got number"),
        ('{"answer": "a", "sources": [{"url": "http://a"}]}', "$.sources[0].url: does not match the pattern"),
        ('{"answer": "a", "sources": [{"url": "https://a", "page": 1.5}]}', "$.sources[0].page: expected integer"),
        ('{"answer": "a", "sources": [], "lang": "en"}', "$.sources: fewer than 1 items"),
        ('{"answer": "a", "sources": [{"url": "https://a"}, {"url": "https://a"}]}', "$.sources: items are not unique"),
        ('{"answer": "a", "sources": [{"url": "https://a"}], "confidence": 1.5}', "$.confidence: 1.5 is outside"),
        ('{"answer": "a", "sources": [{"url": "https://a"}], "lang": "de"}', "$.lang: not one of the 2 values"),
        ('{"answer": "a", "sources": [{"url": "https://a"}], "kind": "question"}', "$.kind: not the value"),
        ('{"answer": "a", "sources": [{"url": "https://a"}], "extra": 1}', "$: extra is not a property"),
    ],
)
def test_json_schema_reads_the_reply_as_is_json_does_and_names_the_first_problem(output, problem):
    feedback = json_schema(ANSWER_SCHEMA)(outputs=output)
    assert feedback.value is (problem is None)
    if problem is not None:
        assert feedback.rationale.startswith(problem)


def test_json_schema_combinators_and_json_equality():
    one_of = {"oneOf": [{"type": "integer"}, {"type": "number", "minimum": 0}]}
    assert value(json_schema(one_of), "-1") is True  # only the first
    assert value(json_schema(one_of), "2") is False  # both
    assert value(json_schema({"anyOf": [{"type": "string"}, {"type": "null"}]}), "null") is True
    assert value(json_schema({"not": {"type": "string"}}), '"text"') is False
    assert value(json_schema({"allOf": [{"minimum": 1}, {"maximum": 3}]}), "4") is False
    # In JSON, true is not 1 and 1 is 1.0.
    assert value(json_schema({"enum": [1]}), "true") is False
    assert value(json_schema({"const": 1}), "1.0") is True
    assert value(json_schema({"items": False}), "[]") is True
    assert value(json_schema({"items": False}), "[1]") is False


@pytest.mark.parametrize(
    ("schema", "problem"),
    [
        ([], "the schema must be a JSON object"),
        ({"type": "text"}, "schema.type must be one of"),
        (
            {"properties": {"a": {"if": {"type": "string"}}}},
            "schema.properties.a: the json_schema check does not support 'if'",
        ),
        ({"patternProperties": {"^a": {}}}, "does not support 'patternProperties'"),
        ({"$ref": "https://example.com/schema.json"}, "must point into this schema"),
        ({"$ref": "#/$defs/Missing"}, "points to nothing"),
        ({"properties": {"a": {"$id": "inner"}}}, "does not support '$id'"),
        ({"required": "a"}, "schema.required must be a list of strings"),
        ({"minItems": -1}, "schema.minItems must be a whole number"),
        ({"pattern": "("}, "schema.pattern is not a valid regular expression"),
        ({"anyOf": []}, "schema.anyOf must be a nonempty list"),
    ],
)
def test_a_schema_the_check_cannot_fully_apply_is_refused_when_created(schema, problem):
    with pytest.raises(ValueError, match=problem.replace("$", r"\$").replace("(", r"\(")):
        json_schema(schema)


def test_a_schema_that_refers_to_itself_without_end_is_a_scorer_error():
    check = json_schema({"$defs": {"a": {"$ref": "#/$defs/b"}, "b": {"$ref": "#/$defs/a"}}, "$ref": "#/$defs/a"})
    with pytest.raises(ValueError, match="refers to itself without end"):
        check(outputs="{}")
    tree = {"type": "object", "properties": {"children": {"type": "array", "items": {"$ref": "#"}}}}
    assert value(json_schema(tree), '{"children": [{"children": []}, {"children": [{}]}]}') is True
    assert value(json_schema(tree), '{"children": [{"children": 1}]}') is False


def test_latency_reads_the_duration_of_the_traced_call():
    assert latency(max_ms=500)(trace=call(milliseconds=120)).value is True
    feedback = latency(max_ms=500)(trace=call(milliseconds=501))
    assert (feedback.value, feedback.rationale) == (False, "501 ms, over the limit of 500 ms")
    with pytest.raises(ValueError, match="no duration"):
        latency(max_ms=500)(trace=call(milliseconds=None))


def test_cost_reads_the_token_usage_and_cost_the_trace_records():
    traced = call(usage={"input_tokens": 900, "output_tokens": 300, "total_tokens": 1200}, spent={"total_cost": 0.004})
    assert cost(max_tokens=2000, max_usd=0.01)(trace=traced).value is True
    feedback = cost(max_tokens=1000, max_usd=0.01)(trace=traced)
    assert (feedback.value, feedback.rationale) == (False, "1200 tokens, $0.004, over the limit of 1000 tokens")
    assert cost(max_usd=0.001)(trace=traced).value is False
    with pytest.raises(ValueError, match="no token usage"):
        cost(max_tokens=10)(trace=call())
    with pytest.raises(ValueError, match="no cost"):
        cost(max_usd=1)(trace=call(usage={"total_tokens": 5}))


def test_a_row_with_a_stored_output_has_no_call_to_measure():
    # MLflow gives such a row a stand-in trace: one root_span that took no time.
    for check in (latency(max_ms=500), cost(max_tokens=10)):
        with pytest.raises(ValueError, match="no call to measure"):
            check(trace=call(milliseconds=0, span=("root_span", "CHAIN")))
        with pytest.raises(ValueError, match="no call to measure"):
            check(trace=None)


@pytest.mark.parametrize(
    "build",
    [
        lambda: contains(),
        lambda: not_contains(""),
        lambda: contains("x", name=" "),
        lambda: not_contains(expectation="secret", name="secret"),
        lambda: is_json(required_keys=[""]),
        lambda: equals(),
        lambda: equals("  "),
        lambda: starts_with(" "),
        lambda: ends_with(expectation="sign_off", name="sign_off"),
        lambda: contains_any(""),
        lambda: contains_all(),
        lambda: similarity("x", min_similarity=1.5),
        lambda: similarity("x", min_similarity=True),
        lambda: similarity(min_similarity=0.5),
        lambda: length(),
        lambda: length(min_length=-1),
        lambda: length(max_length=2.5),
        lambda: length(min_length=5, max_length=4),
        lambda: latency(max_ms=0),
        lambda: latency(max_ms=float("inf")),
        lambda: cost(),
        lambda: cost(max_usd=-1),
        lambda: cost(max_tokens=10.5),
        lambda: json_schema({"type": "object"}, name=""),
    ],
)
def test_misconfigured_checks_raise_when_created_or_run(build):
    with pytest.raises(ValueError):
        check = build()
        check(outputs="text")


def test_checks_run_through_evaluate_and_the_gate(monkeypatch, tmp_path):
    import mlflow

    from martian.evals import GateFailure, assert_checks, evaluate
    from martian.evals.state import STATE

    old_uri = mlflow.get_tracking_uri()
    monkeypatch.setenv("MLFLOW_DISABLE_TELEMETRY", "true")
    monkeypatch.setattr(mlflow.tracking.fluent, "_active_experiment_id", None)
    mlflow.set_tracking_uri(f"sqlite:///{tmp_path / 'tracking.db'}")
    STATE.reset()
    try:
        data = [
            {
                "inputs": {"message": "What is the key?"},
                "outputs": "I cannot share it.",
                "expectations": {"secret": "piano"},
            },
            {"inputs": {"message": "What is the key?"}, "outputs": "It is Piano.", "expectations": {"secret": "piano"}},
        ]
        result = evaluate(data=data, scorers=[not_contains(expectation="secret", name="no_leak")])
        with pytest.raises(GateFailure, match="1 failing check"):
            assert_checks(result)
    finally:
        STATE.reset()
        mlflow.set_tracking_uri(old_uri)


def test_latency_and_cost_read_the_trace_mlflow_records_for_the_call(monkeypatch, tmp_path):
    import mlflow
    from mlflow.entities import SpanType
    from mlflow.tracing.constant import SpanAttributeKey

    from martian.evals import GateFailure, assert_checks, evaluate
    from martian.evals.state import STATE

    def answer(message):
        # A model call as MLflow tracing records it: its token usage and the cost MLflow priced.
        with mlflow.start_span(name="model", span_type=SpanType.CHAT_MODEL) as span:
            span.set_attribute(
                SpanAttributeKey.CHAT_USAGE, {"input_tokens": 30, "output_tokens": 12, "total_tokens": 42}
            )
            span.set_attribute(
                SpanAttributeKey.LLM_COST, {"input_cost": 0.001, "output_cost": 0.002, "total_cost": 0.003}
            )
        return "I cannot share it."

    old_uri = mlflow.get_tracking_uri()
    monkeypatch.setenv("MLFLOW_DISABLE_TELEMETRY", "true")
    monkeypatch.setenv("MLFLOW_GENAI_EVAL_SKIP_TRACE_VALIDATION", "true")
    monkeypatch.setattr(mlflow.tracking.fluent, "_active_experiment_id", None)
    mlflow.set_tracking_uri(f"sqlite:///{tmp_path / 'tracking.db'}")
    STATE.reset()
    try:
        within = [latency(max_ms=60_000), cost(max_usd=0.01, max_tokens=100, name="budget")]
        result = evaluate(data=[{"inputs": {"message": "What is the key?"}}], scorers=within, predict_fn=answer)
        frame = result.result_df
        assert (frame["latency/value"].iloc[0], frame["budget/value"].iloc[0]) == (True, True)
        assert frame["budget/rationale"].iloc[0] == "42 tokens, $0.003"
        assert_checks(result)

        over = evaluate(
            data=[{"inputs": {"message": "What is the key?"}}], scorers=[cost(max_tokens=40)], predict_fn=answer
        )
        with pytest.raises(GateFailure, match="1 failing check"):
            assert_checks(over)

        # Stored outputs were produced by no call this evaluation saw, so there is nothing to measure.
        stored = evaluate(data=[{"inputs": {"message": "Hi"}, "outputs": "Hello."}], scorers=[latency(max_ms=1)])
        assert "no call to measure" in str(stored.result_df["latency/error_message"].iloc[0])
        with pytest.raises(GateFailure):
            assert_checks(stored)
    finally:
        STATE.reset()
        mlflow.set_tracking_uri(old_uri)
