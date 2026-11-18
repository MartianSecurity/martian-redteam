"""Grounding and retrieval judges: prompts, flat verdicts, quotes checked against their source,
and the suite file. A fake provider stands in for the model; nothing here calls one."""

import json
from pathlib import Path

import mlflow
import pytest
from mlflow.entities import Feedback

from martian.evals import context_relevance, context_sufficiency, groundedness, runner, suite_file
from martian.evals.grounding import (
    CONTEXT_RELEVANCE_VERSION,
    CONTEXT_SUFFICIENCY_VERSION,
    GROUNDEDNESS_VERSION,
    _GroundednessJudgment,
    _Judgment,
    _SufficiencyJudgment,
    find_quote,
    locate_quote,
)
from martian.evals.state import STATE

DOCUMENTS = [
    "Store hours: the shop opens at 9 am\nand closes at 6 pm on weekdays.",
    "Returns are accepted within 30 days with a receipt.",
]
REQUEST = "When does the shop close on weekdays, and can I return a gift without a receipt?"
INPUTS = {"message": REQUEST, "context": DOCUMENTS, "case_id": "hidden-case-id", "gold": "fail"}
REASON = "The documents give the closing time."


def grounded(verdict="pass", claim="", passage="closes at 6 pm on weekdays", reason=REASON):
    return {"reason": reason, "claim": claim, "passage": passage, "verdict": verdict}


def relevant(verdict="pass", quote="closes at 6 pm on weekdays", reason=REASON):
    return {"reason": reason, "quote": quote, "verdict": verdict}


def sufficient(verdict="pass", passage="closes at 6 pm on weekdays", missing="", reason=REASON):
    return {"reason": reason, "passage": passage, "missing": missing, "verdict": verdict}


@pytest.fixture
def fake_model(monkeypatch):
    """Replace the shared transport with replies the test supplies, and keep each request."""
    calls = []

    def install(*values):
        replies = list(values)

        def provider(**kwargs):
            calls.append(kwargs)
            return Feedback(name=kwargs["assessment_name"], value=replies.pop(0), metadata={"fixture": "1"})

        monkeypatch.setattr("martian.evals.grounding.invoke_response_judge", provider)
        return calls

    return install


def payload_of(call):
    return json.loads(call["prompt"][1].content)


def evidence_of(result):
    return json.loads(result.metadata["martian.judge.evidence"])


def ground(outputs="It closes at 6 pm.", **options):
    return groundedness(model="openai:/stub", **options)(inputs=INPUTS, outputs=outputs, expectations={})


def test_a_grounded_response_passes_and_the_rationale_quotes_the_document_as_written(fake_model):
    # The model folds the document's line break into a space; the SDK quotes the source itself.
    calls = fake_model(grounded(claim="It closes at 6 pm.", passage="the shop opens at 9 am and closes at 6 pm"))
    result = ground(include_judge_prompt=True)
    assert result.value is True
    assert result.rationale == (
        "Grounded. The documents give the closing time. "
        'Claim (the response): "It closes at 6 pm." '
        'Supporting passage (document 1): "the shop opens at 9 am\nand closes at 6 pm"'
    )
    start = DOCUMENTS[0].index("the shop")
    assert evidence_of(result) == [
        {"field": "claim", "source": "response", "start": 0, "end": 18, "text": "It closes at 6 pm."},
        {
            "field": "passage",
            "source": "documents",
            "document": 1,
            "start": start,
            "end": start + len("the shop opens at 9 am\nand closes at 6 pm"),
            "text": "the shop opens at 9 am\nand closes at 6 pm",
        },
    ]
    assert result.metadata["martian.judge.version"] == GROUNDEDNESS_VERSION
    assert result.metadata["martian.judge.verdict"] == "pass"
    assert result.metadata["martian.judge.model_calls"] == "1"
    assert result.metadata["fixture"] == "1"  # the transport's own metadata is kept
    assert len(calls) == 1 and calls[0]["stage"] == "draft"
    assert calls[0]["response_format"] is _GroundednessJudgment


def test_the_payload_numbers_documents_and_leaves_out_labels_and_case_ids(fake_model):
    calls = fake_model(grounded())
    result = groundedness(model="openai:/stub", include_judge_prompt=True)(
        inputs=INPUTS, outputs="It closes at 6 pm.", expectations={"expected_response": "unused"}
    )
    assert payload_of(calls[0]) == {
        "request": REQUEST,
        "documents": [{"document_index": 1, "text": DOCUMENTS[0]}, {"document_index": 2, "text": DOCUMENTS[1]}],
        "response": "It closes at 6 pm.",
    }
    assert "hidden-case-id" not in result.metadata["martian.judge.prompt"]
    assert "untrusted" in calls[0]["prompt"][0].content


def test_an_unsupported_claim_fails_quoted_from_the_response_with_what_the_documents_say(fake_model):
    fake_model(
        grounded(
            "fail",
            claim="Gifts can be returned without a receipt.",
            passage="Returns are accepted within 30 days with a receipt.",
            reason="The documents require a receipt.",
        ),
        grounded("fail", claim="Gifts can be returned without a receipt.", passage=""),
    )
    outputs = "It closes at 6 pm. Gifts can be returned without a receipt."
    result = ground(outputs)
    assert result.value is False
    assert result.rationale == (
        "Not grounded. The documents require a receipt. "
        'Unsupported claim (the response): "Gifts can be returned without a receipt." '
        'The documents say (document 2): "Returns are accepted within 30 days with a receipt."'
    )
    assert [place["source"] for place in evidence_of(result)] == ["response", "documents"]
    # The documents may say nothing about the claim: then the claim is the only quote.
    silent = ground(outputs)
    assert [place["field"] for place in evidence_of(silent)] == ["claim"]


@pytest.mark.parametrize(
    "reply",
    [
        grounded(passage="the shop closes at 7 pm"),  # not in any document
        grounded(passage="It closes at 6 pm."),  # the passage is from the documents, not the response
        grounded(passage="Closes at 6 pm on weekdays"),  # case differs: no case folding
        grounded(passage="opens at 9 am ... 6 pm"),  # an ellipsis is not a passage
        grounded(claim="It closes at 7 pm."),  # an optional quote is checked too
        grounded("fail", claim="Returns are accepted within 30 days"),  # the claim is from the response
        grounded(passage="Store hours: the shop opens at 9 am. Returns are free."),  # one sentence is invented
    ],
)
def test_a_quote_that_is_not_a_passage_of_its_source_is_an_error(fake_model, reply):
    fake_model(reply)
    with pytest.raises(ValueError, match=r"quote is not a passage of the (documents|response) \[judge_invalid_quote\]"):
        ground()


def test_a_quote_may_join_sentences_from_different_documents(fake_model):
    fake_model(grounded(passage="The shop closes at 6 pm. Returns are accepted within 30 days with a receipt."))
    with pytest.raises(ValueError, match="judge_invalid_quote"):
        ground()  # "The shop closes" is not a sentence of document 1
    fake_model(grounded(passage="and closes at 6 pm on weekdays. Returns are accepted within 30 days with a receipt."))
    result = ground()
    assert [(place["document"], place["text"]) for place in evidence_of(result)] == [
        (1, "and closes at 6 pm on weekdays."),
        (2, "Returns are accepted within 30 days with a receipt."),
    ]
    assert result.rationale.endswith(
        'Supporting passage (document 1): "and closes at 6 pm on weekdays."; '
        '(document 2): "Returns are accepted within 30 days with a receipt."'
    )


def test_locate_quote_prefers_one_passage_and_needs_every_sentence():
    texts = {"documents": ["A b. C d.", "E f."]}
    assert locate_quote("A b. C d.", ("documents",), texts) == [
        {"source": "documents", "document": 1, "start": 0, "end": 9, "text": "A b. C d."}
    ]
    assert len(locate_quote("C d. E f.", ("documents",), texts)) == 2
    assert locate_quote("C d. G h.", ("documents",), texts) is None
    assert locate_quote("G h.", ("documents",), texts) is None


def test_the_quote_error_never_repeats_the_model_text(fake_model):
    fake_model(grounded(passage="private-synthetic-canary"))
    with pytest.raises(ValueError) as raised:
        ground()
    assert "private-synthetic-canary" not in str(raised.value)


@pytest.mark.parametrize(
    ("reply", "message"),
    [
        (grounded("uncertain", passage=""), r"could not decide groundedness \[judge_uncertain\]"),
        (grounded(passage="  "), r"gave no passage quote from the documents \[judge_missing_quote\]"),
        (grounded("fail", passage="closes at 6 pm"), r"gave no claim quote from the response \[judge_missing_quote\]"),
        ({"reason": "x", "passage": "a", "verdict": "pass"}, "invalid groundedness schema"),
        ({**grounded(), "document": 1}, "invalid groundedness schema"),
        (relevant(), "invalid groundedness schema"),
    ],
)
def test_uncertain_empty_and_malformed_verdicts_are_errors(fake_model, reply, message):
    fake_model(reply)
    with pytest.raises(ValueError, match=message):
        ground()


def test_relevance_reads_no_response_and_a_fail_quotes_the_request(fake_model):
    calls = fake_model(relevant("fail", quote="can I return a gift without a receipt?", reason="Off topic."))
    result = context_relevance(model="openai:/stub")(inputs=INPUTS, outputs="anything", expectations={})
    assert "response" not in payload_of(calls[0])
    assert calls[0]["response_format"] is _Judgment
    assert result.value is False
    assert result.metadata["martian.judge.version"] == CONTEXT_RELEVANCE_VERSION
    assert result.rationale == (
        'Not relevant. Off topic. Not addressed by any document (the request): "can I return a gift without a receipt?"'
    )


def test_sufficiency_quotes_what_is_missing_from_the_expected_response_or_the_request(fake_model):
    expected = "It closes at 6 pm. Gifts cannot be returned without a receipt."
    calls = fake_model(
        sufficient("fail", passage="", missing="Gifts cannot be returned without a receipt."),
        sufficient("fail", passage="Returns are accepted within 30 days", missing="return a gift without a receipt?"),
        sufficient("pass", passage="Returns are accepted within 30 days with a receipt.", missing="ignored on a pass"),
        sufficient("fail", passage="Returns are accepted within 30 days", missing=""),
    )
    judge = context_sufficiency(model="openai:/stub")
    first = judge(inputs=INPUTS, outputs=None, expectations={"expected_response": expected})
    assert first.value is False
    assert payload_of(calls[0])["expected_response"] == expected
    assert calls[0]["response_format"] is _SufficiencyJudgment
    assert evidence_of(first)[0]["source"] == "expected_response"
    # Without an expected response the missing information is quoted from the request.
    second = judge(inputs=INPUTS, outputs=None, expectations=None)
    assert "expected_response" not in payload_of(calls[1])
    assert [place["source"] for place in evidence_of(second)] == ["documents", "request"]
    assert second.rationale.endswith(
        'Closest passage (document 2): "Returns are accepted within 30 days" '
        'Missing from the documents (the request): "return a gift without a receipt?"'
    )
    third = judge(inputs=INPUTS, outputs=None, expectations={})
    assert third.value is True
    assert [(place["field"], place["document"]) for place in evidence_of(third)] == [("passage", 2)]
    assert third.metadata["martian.judge.version"] == CONTEXT_SUFFICIENCY_VERSION
    with pytest.raises(ValueError, match=r"no missing quote from the request \[judge_missing_quote\]"):
        judge(inputs=INPUTS, outputs=None, expectations={})


@pytest.mark.parametrize(
    ("inputs", "outputs", "message"),
    [
        ({"message": REQUEST}, "reply", "needs the documents at inputs.context"),
        ({"message": REQUEST, "context": []}, "reply", "needs the documents at inputs.context"),
        ({"message": REQUEST, "context": ["ok", " "]}, "reply", "must be a nonempty string"),
        ({"message": REQUEST, "context": [{"text": "a"}]}, "reply", "must be a nonempty string"),
        ({"message": 3, "context": "doc"}, "reply", "string request"),
        ({"message": REQUEST, "context": "doc"}, "  ", "nonempty assistant response"),
        ("just a string", "reply", "needs inputs with the request"),
    ],
)
def test_missing_documents_request_or_response_fail_before_any_model_call(fake_model, inputs, outputs, message):
    calls = fake_model()
    with pytest.raises(ValueError, match=message):
        groundedness(model="openai:/stub")(inputs=inputs, outputs=outputs, expectations={})
    assert calls == []


def test_an_expected_response_must_be_text(fake_model):
    calls = fake_model()
    with pytest.raises(ValueError, match="expected_response must be a nonempty string"):
        context_sufficiency(model="openai:/stub")(inputs=INPUTS, outputs=None, expectations={"expected_response": 3})
    assert calls == []


def test_the_options_are_checked_by_the_shared_engine_when_the_judge_is_made():
    with pytest.raises(ValueError, match="explicit judge model"):
        groundedness(model=" ")
    with pytest.raises(ValueError, match="Unsupported judge inference parameter"):
        context_relevance(model="openai:/stub", inference_params={"top_k": 3})
    with pytest.raises(ValueError, match="line break"):
        context_sufficiency(model="openai:/stub", headers={"X-Key": "a\r\nb"})


@pytest.mark.parametrize("schema", [_GroundednessJudgment, _Judgment, _SufficiencyJudgment])
def test_each_wire_schema_is_one_object_of_strings(schema):
    definitions = schema.model_json_schema()["$defs"]
    assert len(definitions) == 1
    (verdict_schema,) = definitions.values()
    assert list(verdict_schema["properties"])[0] == "reason" and list(verdict_schema["properties"])[-1] == "verdict"
    assert all(field.get("type") == "string" for field in verdict_schema["properties"].values())


def test_find_quote_lets_only_whitespace_differ():
    text = "Line one\n\nline  two."
    assert find_quote("one line two.", text) == (5, len(text))
    assert find_quote("One line two.", text) is None
    assert find_quote("", text) is None


def test_a_judgment_through_the_shared_transport(monkeypatch):
    """The provider's JSON reply is read by the shared transport and its schema check."""
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-only")
    monkeypatch.setenv("OPENAI_API_BASE", "http://127.0.0.1:9/v1")
    sent = []

    def post(endpoint, headers, payload):
        sent.append((headers, payload))
        content = json.dumps({"result": sufficient(passage="Returns are accepted within 30 days")})
        return {
            "id": "fixture",
            "object": "chat.completion",
            "created": 0,
            "model": "fixture-model-1",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
        }

    monkeypatch.setattr("martian.evals._judge_transport._post_chat", post)
    judge = context_sufficiency(model="openai:/fixture", inference_params={"temperature": 0}, headers={"X-Team": "a"})
    result = judge(inputs=INPUTS, outputs=None, expectations={})
    assert result.value is True
    assert result.metadata["martian.judge.model_version"] == "fixture-model-1"
    assert sent[0][0]["X-Team"] == "a"
    assert sent[0][1]["response_format"]["type"] == "json_schema"
    assert sent[0][1]["temperature"] == 0


# --- suite file -------------------------------------------------------------------------------

SUITE = """data: cases.jsonl
judge:
  model: openai:/stub
  inference_params: {temperature: 0}
checks:
  - type: groundedness
  - type: context_relevance
  - type: context_sufficiency
    name: enough
"""


def write(tmp_path, suite=SUITE):
    rows = [
        {
            "id": "hours",
            "inputs": {"message": REQUEST, "context": DOCUMENTS},
            "output": "It closes at 6 pm.",
            "expectations": {"expected_response": "It closes at 6 pm."},
        }
    ]
    (tmp_path / "cases.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    (tmp_path / "evals.yaml").write_text(suite, encoding="utf-8")
    return tmp_path / "evals.yaml"


def test_the_suite_file_holds_the_grounding_judges_and_writes_them_back(tmp_path):
    suite, _ = suite_file.load_suite_file(write(tmp_path))
    assert [(s.name, s.kind, s.type, s.criteria, s.model) for s in suite.scorers] == [
        ("groundedness_judge", "llm_judge", "groundedness", {}, "openai:/stub"),
        ("context_relevance_judge", "llm_judge", "context_relevance", {}, "openai:/stub"),
        ("enough", "llm_judge", "context_sufficiency", {}, "openai:/stub"),
    ]
    text = suite_file.suite_to_yaml(suite)
    again = suite_file.suite_from_yaml(
        text, name=suite.name, cases=suite_file.load_suite_file(tmp_path / "evals.yaml")[1]
    )
    assert again == suite


def test_a_grounding_judge_takes_no_criteria_or_options(tmp_path):
    with pytest.raises(Exception, match="rubric does not apply to groundedness checks"):
        suite_file.load_suite_file(
            write(tmp_path, SUITE.replace("  - type: groundedness\n", "  - type: groundedness\n    rubric: x\n"))
        )
    suite, _ = suite_file.load_suite_file(write(tmp_path))
    bad = suite.model_copy(
        update={
            "scorers": [suite.scorers[0].model_copy(update={"criteria": {"groundedness": "x"}}), *suite.scorers[1:]]
        }
    )
    with pytest.raises(Exception, match="groundedness judges have no criteria"):
        suite_file.suite_to_yaml(bad)


@pytest.fixture
def isolated_run(monkeypatch, tmp_path):
    """A suite run that records into this test's folders only, as in test_runner."""
    monkeypatch.setenv("MLFLOW_DISABLE_TELEMETRY", "true")
    monkeypatch.setenv("MARTIAN_EVALS_HOME", str(tmp_path / "evals-home"))
    monkeypatch.setattr(mlflow.tracking.fluent, "_active_experiment_id", None)
    previous = mlflow.get_tracking_uri()
    STATE.reset()
    yield
    STATE.reset()
    mlflow.set_tracking_uri(previous)


def test_a_suite_runs_the_grounding_judges_on_the_case_documents(tmp_path, monkeypatch, isolated_run):
    # MLflow may score in any order: the fake answers by the judge each request is for.
    replies = {
        "groundedness_judge": grounded(),
        "context_relevance_judge": relevant(quote="Returns are accepted within 30 days"),
        "enough": sufficient("fail", missing="can I return a gift without a receipt?"),
    }
    calls = []

    def provider(**kwargs):
        calls.append(kwargs)
        return Feedback(name=kwargs["assessment_name"], value=replies[kwargs["assessment_name"]])

    monkeypatch.setattr("martian.evals.grounding.invoke_response_judge", provider)
    summary = runner.run_suite(write(tmp_path))
    counted = ("passed", "failed", "errors", "pass_rate")
    assert {label: {key: found[key] for key in counted} for label, found in summary["checks"].items()} == {
        "groundedness_judge": {"passed": 1, "failed": 0, "errors": 0, "pass_rate": 1.0},
        "context_relevance_judge": {"passed": 1, "failed": 0, "errors": 0, "pass_rate": 1.0},
        "enough": {"passed": 0, "failed": 1, "errors": 0, "pass_rate": 0.0},
    }
    record = json.loads((tmp_path / "results/results.jsonl").read_text().splitlines()[0])
    assert record["checks"]["enough"]["rationale"].startswith("Not sufficient.")
    assert {payload_of(c)["request"] for c in calls} == {REQUEST}


# --- the labelled set ---------------------------------------------------------------------------

BENCHMARK = Path(__file__).parent / "benchmarks" / "grounding_judges.jsonl"


def test_the_labelled_set_has_balanced_well_formed_cases_for_each_judge():
    """The set scripts/benchmark_grounding_judges.py scores the judges against (docs/evals.md)."""
    cases = [json.loads(line) for line in BENCHMARK.read_text(encoding="utf-8").splitlines()]
    assert len({case["id"] for case in cases}) == len(cases)
    for judge in runner.GROUNDING_JUDGES:
        mine = [case for case in cases if case["judge"] == judge]
        labels = [case["label"] for case in mine]
        assert 20 <= len(mine) <= 30 and abs(labels.count("pass") - labels.count("fail")) <= 1, judge
    for case in cases:
        assert set(case) <= {"id", "judge", "label", "request", "documents", "response", "expected_response", "why"}
        assert case["label"] in ("pass", "fail") and case["why"].strip() and case["request"].strip()
        assert case["documents"] and all(isinstance(document, str) and document for document in case["documents"])
        assert ("response" in case) == (case["judge"] == "groundedness")
        assert "expected_response" not in case or case["judge"] == "context_sufficiency"
