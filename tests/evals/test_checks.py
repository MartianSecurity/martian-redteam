"""Deterministic checks decide from the reply, or from the recorded call, and never call a model."""

import json
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
    is_xml,
    json_schema,
    latency,
    length,
    matches,
    not_contains,
    not_matches,
    number_equals,
    reconciles,
    similarity,
    starts_with,
    sums_to,
    table_equals,
    word_count,
)
from martian.evals._numeric import read_text
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


def test_exact_word_count_reports_one_word_with_a_singular_reason():
    check = word_count(min_words=1, max_words=1)
    assert check(outputs="Library").rationale == "1 word"
    assert check(outputs="Public library").value is False


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


def test_embedded_json_uses_strict_objects_or_promptfoo_repair():
    schema = {"type": "object", "required": ["city"]}
    strict = json_schema(schema, embedded="strict")
    compatible = json_schema(schema, embedded="promptfoo")
    assert value(strict, 'Map: {"note":"closed } gate"} {"city":"Paris"}') is True
    assert value(strict, "Map: {city: 'Paris'") is False
    assert value(compatible, "Map: {city: 'Paris'") is True
    with pytest.raises(ValueError, match="use either plain_json or embedded"):
        json_schema(schema, plain_json=True, embedded="strict")


def test_xml_document_and_fragment_checks_follow_paths_from_each_root():
    document = '<lib:catalog xmlns:lib="urn:library"><book/><book><title>Atlas</title></book></lib:catalog>'
    required = ["lib:catalog.book.1.title"]
    assert value(is_xml(required_elements=required), document) is True
    assert value(is_xml(required_elements=["lib:catalog.book.title"]), document) is False
    assert value(is_xml(), f"Read this {document} next.") is False
    assert value(is_xml(fragment=True, required_elements=required), f"Read this {document} next.") is True
    assert value(is_xml(fragment=True, required_elements=["book"]), f"Read this {document} next.") is False
    assert (
        value(
            is_xml(fragment=True, required_elements=["hours.open", "location"]),
            "Note <hours><open/></hours><location/>",
        )
        is True
    )


def test_xml_rejects_malformed_fragments_and_dtds_with_internal_subsets():
    assert value(is_xml(fragment=True), 'Text <hours open="nine" open="ten"/>') is False
    assert value(is_xml(fragment=True), "Text <!-- <hours/> -->") is False
    assert value(is_xml(fragment=True), "Text <![CDATA[<hours/>]]>") is False
    declaration = "<!DOCTYPE hours [<!ELEMENT hours EMPTY>]><hours/>"
    assert value(is_xml(), declaration) is False
    assert value(is_xml(fragment=True), f"Public schedule: {declaration}") is False
    assert value(is_xml(negate=True), "<hours><close/></open>") is True
    with pytest.raises(ValueError, match="must name XML elements"):
        is_xml(required_elements=["hours..open"])


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
        lambda: number_equals(),
        lambda: number_equals(1, expectation="total"),
        lambda: number_equals(True),
        lambda: number_equals("about ten"),
        lambda: number_equals(1, path="a", pattern="(.+)"),
        lambda: number_equals(1, path="lines.*.amount"),
        lambda: number_equals(1, path="a..b"),
        lambda: number_equals(1, pattern="("),
        lambda: number_equals(1, abs_tolerance=-0.01),
        lambda: number_equals(1, rel_tolerance=5),  # 5 meaning 5% would accept almost anything
        lambda: number_equals(1, rel_tolerance=float("nan")),
        lambda: number_equals(expectation="total", name="total"),
        lambda: sums_to(parts="lines.*.amount", total="total"),
        lambda: sums_to(parts=[], total="total"),
        lambda: sums_to(parts=["lines.*.amount"], total="lines.*.amount"),
        lambda: reconciles(add=["opening"], subtract="fees", total="closing"),
        lambda: table_equals(key="id"),
        lambda: table_equals([{"id": "a"}], key="id", expectation="rows"),
        lambda: table_equals([], key="id"),
        lambda: table_equals([{"id": "a"}, {"id": "A"}], key="id"),
        lambda: table_equals([{"name": "a"}], key="id"),
        lambda: table_equals([{"id": "a", "tags": ["x"]}], key="id"),
        lambda: table_equals([{"id": "a"}], key="id", columns=["amount"]),
        lambda: table_equals([{"id": "a"}], key="id", columns=["id"]),
        lambda: table_equals([{"id": "a"}], key=[]),
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


# --- numbers and tables -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "number"),
    [
        ("1234.56", "1234.56"),
        ("  1,234.56 ", "1234.56"),
        ("$1,234.56", "1234.56"),
        ("-$1,234.56", "-1234.56"),
        ("$-1,234.56", "-1234.56"),
        ("\u22121,234.56", "-1234.56"),
        ("(1,234.56)", "-1234.56"),
        ("USD 1,234.56", "1234.56"),
        ("1,234.56 EUR", "1234.56"),
        ("12.5%", "12.5"),
        ("1234567", "1234567"),
        ("0.10", "0.10"),
    ],
)
def test_a_number_written_as_text_is_read_exactly(text, number):
    assert str(read_text(text)) == number


@pytest.mark.parametrize(
    "text",
    ["", "ten", "1,23", "1.234,56", "1 234", ".5", "1.", "1e5", "--5", "(-5)", "$5%", "12,34.5", "5 apples", "\u0665"],
)
def test_text_that_is_not_one_number_is_not_read(text):
    assert read_text(text) is None


def test_number_equals_reads_the_one_number_a_pattern_or_a_path():
    assert value(number_equals(1234.56), "The total is $1,234.56.") is True
    assert value(number_equals("1234.560"), "1234.56") is True  # the same value, written longer
    two = number_equals(30)(outputs="3 items cost $30.")
    assert (two.value, two.rationale) == (
        False,
        "the output holds 2 numbers; a pattern or a path picks the one to compare",
    )
    assert value(number_equals(30, pattern=r"cost (\S+)\."), "3 items cost $30.") is True
    assert value(number_equals(30, pattern=r"\$\d+"), "3 items cost $30.") is True  # the whole match
    assert value(number_equals(30, pattern=r"total (\d+)"), "3 items cost $30.") is False
    assert value(number_equals(10.1, path="totals.net"), '{"totals": {"net": "10.10"}}') is True
    assert value(number_equals(7, path="lines.1"), {"lines": [5, 7]}) is True
    missing = number_equals(10, path="totals.gross")(outputs='{"totals": {"net": 10}}')
    assert (missing.value, missing.rationale) == (False, "the output has no value at totals.gross")
    assert number_equals(10, path="totals")(outputs="10").rationale == "the output has no value at totals"
    assert number_equals(10, path="a")(outputs="ten").rationale == "the output is not JSON, so path cannot be read"
    assert number_equals(10, path="a")(outputs='{"a": true}').rationale == "the value at a is not a number"


def test_number_equals_reads_the_expected_number_from_each_row():
    check = number_equals(expectation="total", path="total")
    # A CSV data file gives every expectation as text.
    assert value(check, '{"total": 1234.56}', {"total": "1,234.56"}) is True
    assert value(check, '{"total": 1234.56}', {"total": 1234.57}) is False
    with pytest.raises(ValueError, match="expectations.total must be a number"):
        value(check, '{"total": 1}', {"other": 1})


def test_a_one_cent_error_fails_and_figures_are_exact_decimals():
    exact = number_equals("1234.56")
    feedback = exact(outputs="1234.57")
    assert (feedback.value, feedback.rationale) == (
        False,
        "1234.57, but the expected number is 1234.56: off by 0.01",
    )
    assert value(exact, "1234.55") is False
    # 0.1 + 0.2 is 0.30000000000000004 in binary floats, and exactly 0.3 here.
    assert value(sums_to(parts=["a", "b"], total="total"), '{"a": 0.1, "b": 0.2, "total": 0.3}') is True
    assert value(sums_to(parts=["a", "b"], total="total"), {"a": 0.1, "b": 0.2, "total": 0.3}) is True
    # A float's shortest text is its value: 1e16 + 1 cannot be held in a float, but is read here.
    assert value(number_equals("10000000000000001"), "10,000,000,000,000,001") is True
    assert value(number_equals("10000000000000001"), "10,000,000,000,000,000") is False
    with pytest.raises(ValueError, match="more than 100 digits"):
        number_equals("1" * 120)(outputs="2" * 120)


def test_tolerance_is_the_larger_of_the_absolute_and_the_relative_and_includes_its_bound():
    absolute = number_equals(100, abs_tolerance=0.01)
    assert value(absolute, "100.01") is True and value(absolute, "99.99") is True  # the bound itself
    assert absolute(outputs="100.011").rationale == (
        "100.011, but the expected number is 100: off by 0.011, more than the tolerance of 0.01"
    )
    relative = number_equals(-2000, rel_tolerance="0.001")  # 0.1% of the expected number's size: 2
    assert value(relative, "-2002") is True and value(relative, "-2002.01") is False
    assert relative(outputs="-1999").rationale == "-1999, within 2.000 of the expected number -2000"
    both = number_equals(1000, abs_tolerance=5, rel_tolerance=0.001)  # 5, and 1: 5 applies
    assert value(both, "1004.5") is True and value(both, "1005.5") is False
    small = number_equals("0.50", abs_tolerance=0.01, rel_tolerance=0.1)  # 0.01, and 0.05: 0.05 applies
    assert value(small, "0.55") is True and value(small, "0.56") is False
    # Near zero a relative tolerance allows nothing; an absolute one is needed.
    assert value(number_equals(0, rel_tolerance=0.5), "0.0001") is False
    assert value(number_equals(0, abs_tolerance=0.001, rel_tolerance=0.5), "0.0001") is True


STATEMENT = {
    "opening_balance": "12,500.00",
    "deposits": [{"ref": "D-101", "amount": 3200.00}, {"ref": "D-102", "amount": "845.10"}],
    "withdrawals": [{"ref": "W-201", "amount": 1520.00}, {"ref": "W-202", "amount": 230.00}],
    "total_deposits": 4045.10,
    "closing_balance": "$14,795.10",
}
BALANCE = {
    "add": ["opening_balance", "deposits.*.amount"],
    "subtract": ["withdrawals.*.amount"],
    "total": "closing_balance",
}


def statement(**changes):
    return json.dumps({**STATEMENT, **changes})


def test_a_reconciliation_passes_and_a_one_cent_error_fails():
    check = reconciles(**BALANCE)
    feedback = check(outputs=statement())
    assert (feedback.value, feedback.rationale) == (
        True,
        "16545.10 added (3 values) less 1750.0 subtracted (2 values) is 14795.10, equal to the total 14795.10",
    )
    one_cent = check(outputs=statement(closing_balance="14,795.11"))
    assert (one_cent.value, one_cent.rationale) == (
        False,
        "16545.10 added (3 values) less 1750.0 subtracted (2 values) is 14795.10, "
        "but the total is 14795.11: off by 0.01",
    )
    assert value(reconciles(**BALANCE, abs_tolerance=0.01), statement(closing_balance="14,795.11")) is True
    deposits = statement(deposits=[{"ref": "D-101", "amount": 3200}, {"ref": "D-102", "amount": 845.11}])
    assert value(check, deposits) is False
    assert value(sums_to(parts=["deposits.*.amount"], total="total_deposits"), statement()) is True
    assert value(sums_to(parts=["deposits.*.amount"], total="total_deposits"), deposits) is False


def test_totals_and_reconciliation_fail_on_a_figure_they_cannot_read():
    check = reconciles(**BALANCE)
    assert check(outputs="The books balance.").rationale == "the output is not JSON, so its figures cannot be read"
    broken = statement(withdrawals=[{"ref": "W-201", "amount": "n/a"}])
    assert check(outputs=broken).rationale == "the value at withdrawals.0.amount is not a number"
    assert check(outputs=statement(withdrawals={})).rationale == (
        "the value at withdrawals is not a list, so * has no items"
    )
    assert check(outputs=json.dumps({"opening_balance": 1, "deposits": []})).rationale == (
        "the output has no value at withdrawals"
    )
    # No items is a sum of zero.
    nothing = json.dumps({"opening_balance": 5, "deposits": [], "withdrawals": [], "closing_balance": 5})
    assert value(check, nothing) is True


INVOICES = [
    {"invoice": "INV-1", "amount": "1,200.00", "status": "paid"},
    {"invoice": "INV-2", "amount": 50, "status": "open"},
    {"invoice": "INV-3", "amount": 75.5, "status": None},
]
MARKDOWN = """Here are the invoices:

| Invoice | Amount | Status |
| :--- | ---: | --- |
| INV-2 | **$50.00** | Open |
| `INV-1` | 1,200.00 | paid |
| INV-3 | 75.50 | |

Let me know if you need more.
"""


def test_table_equals_matches_rows_by_key_in_any_order_from_markdown_or_json():
    check = table_equals(INVOICES, key="invoice")
    feedback = check(outputs=MARKDOWN)
    assert (feedback.value, feedback.rationale) == (True, "3 rows match, 6 cells compared")
    rows = [
        {"invoice": "INV-3", "amount": 75.50, "status": "", "note": "an extra column is not compared"},
        {"invoice": "INV-1", "amount": 1200, "status": "PAID"},
        {"invoice": "INV-2", "amount": "50", "status": "open"},
    ]
    assert value(check, rows) is True
    assert value(check, "```json\n" + json.dumps(rows) + "\n```") is True
    assert value(table_equals(INVOICES, key="invoice", path="data.rows"), {"data": {"rows": rows}}) is True
    assert value(table_equals(INVOICES, key="invoice", case_sensitive=True), rows) is False


def test_table_equals_names_the_rows_and_cells_that_differ():
    rows = [
        {"invoice": "INV-1", "amount": "1,200.01", "status": "unpaid"},
        {"invoice": "INV-3", "amount": 75.5},
        {"invoice": "INV-9", "amount": 1, "status": "open"},
    ]
    feedback = table_equals(INVOICES, key="invoice")(outputs=rows)
    assert feedback.value is False
    assert feedback.rationale == (
        '2 of 3 rows differ: INV-1: amount is 1200.01, expected 1200.00, off by 0.01, status is "unpaid", '
        'expected "paid"; INV-2 is missing. 1 unexpected row: INV-9'
    )
    within = table_equals(INVOICES, key="invoice", columns=["amount"], abs_tolerance=0.01)(outputs=rows)
    assert within.rationale == "1 of 3 rows differ: INV-2 is missing. 1 unexpected row: INV-9"
    nan = table_equals([{"id": "a", "n": 1}], key="id")(outputs=[{"id": "a", "n": "one"}])
    assert nan.rationale == '1 of 1 row differ: a: n is "one", not a number; expected 1'
    blank = table_equals([{"id": "a", "n": ""}], key="id")(outputs=[{"id": "a", "n": 0}])
    assert blank.rationale == "1 of 1 row differ: a: n is 0, expected blank"
    absent = table_equals([{"id": "a", "n": 1}], key="id")(outputs=[{"id": "a"}])
    assert absent.rationale == "1 of 1 row differ: a: no n"
    many = table_equals([{"id": str(n)} for n in range(12)], key="id")(outputs=[])
    assert many.rationale.endswith("9 is missing; and 2 more")


def test_table_equals_takes_a_composite_key_and_rows_from_each_case():
    check = table_equals(expectation="lines", key=["invoice", "line"])
    expected = [{"invoice": "INV-1", "line": 1, "amount": 10}, {"invoice": "INV-1", "line": 2, "amount": 5}]
    table = "| invoice | line | amount |\n|---|---|---|\n| INV-1 | 2 | 5 |\n| INV-1 | 1 | 10.00 |"
    assert value(check, table, {"lines": expected}) is True
    assert value(check, table, {"lines": json.dumps(expected)}) is True  # as a CSV data file gives it
    wrong = check(outputs=table.replace("| 5 |", "| 6 |"), expectations={"lines": expected})
    assert wrong.rationale == "1 of 2 rows differ: INV-1, 2: amount is 6, expected 5, off by 1"
    with pytest.raises(ValueError, match="expectations.lines must be a nonempty list of row objects"):
        value(check, table, {"lines": {"invoice": "INV-1"}})


@pytest.mark.parametrize(
    ("output", "problem"),
    [
        ("No table here.", "the output is not JSON and holds no Markdown table; the check reads one"),
        (MARKDOWN + "\n" + MARKDOWN, "the output is not JSON and holds 2 Markdown tables; the check reads one"),
        ('{"rows": []}', "the JSON output is not a list of row objects"),
        ("| invoice |\n|---|\n| INV-1 | extra |", "row 1 of the table has 2 cells and its header 1"),
        ("| id |\n|---|\n| INV-1 |", "row 1 of the output has no invoice, a key column"),
        ('[{"invoice": "INV-1"}, {"invoice": "inv-1"}]', "the output has two rows with the key inv-1"),
        ("| Invoice | invoice |\n|---|---|\n| a | b |", "row 1 of the output has two columns named invoice"),
    ],
)
def test_table_equals_fails_on_an_output_it_cannot_read_as_one_table(output, problem):
    feedback = table_equals(INVOICES, key="invoice")(outputs=output)
    assert (feedback.value, feedback.rationale) == (False, problem)


def test_table_equals_reads_markdown_without_outer_pipes_and_with_escaped_ones():
    rows = [{"name": "a|b", "n": 1}]
    assert value(table_equals(rows, key="name"), "name | n\n--- | ---\na\\|b | 1") is True
