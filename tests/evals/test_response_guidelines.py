"""Judge contract regressions; no live model or external network is used."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pandas as pd
import pytest
from mlflow.entities import Feedback

from martian.evals import GateFailure, assert_checks, response_guidelines
from martian.evals.judges import RESPONSE_GUIDELINES_VERSION, _resolve_citations, _response_segments, _validate_checks
from martian.evals.state import STATE


def check(status="complied", index=1, kind="none", evidence=""):
    value = {
        "guideline_index": index,
        "reason": {
            "complied": "The response satisfies this applicable guideline.",
            "violated": "The response violates the specified guideline.",
            "not_applicable": "The condition for applying this guideline is absent.",
            "uncertain": "The available evidence is insufficient.",
        }.get(status, "Invalid model status."),
        "evidence_kind": kind,
        "status": status,
    }
    if kind == "absence" and evidence:
        value["reason"] = evidence
    elif evidence:
        value["evidence"] = evidence  # Forbidden wire field, used by negative tests.
    return value


def call(monkeypatch, checks, **options):
    requests = []

    def invoke(**kwargs):
        requests.append(kwargs)
        return Feedback(name="policy", value=[{**c, "evidence_ids": c.get("evidence_ids", [])} for c in checks])

    monkeypatch.setattr("martian.evals.judges.invoke_judge_model", invoke)
    judge = response_guidelines(model="openai:/stub", name="policy", **options)
    result = judge(
        inputs={"message": "How to greet?", "case_id": "do-not-send", "label": "do-not-send"},
        outputs="Hello!",
        expectations={"guidelines": ["Include a greeting."] * len(checks)},
    )
    return result, requests


@pytest.mark.parametrize(
    ("checks", "expected"),
    [
        ([check()], True),  # Applicable AND satisfied: regression for the original ambiguity.
        ([check("not_applicable")], True),
        ([check(), check("not_applicable", 2)], True),
        ([{**check("violated", kind="quote"), "evidence_ids": [1]}], False),
        ([check(), check("violated", 2, "absence", "No closing farewell")], False),
    ],
)
def test_truth_table_and_gate(monkeypatch, checks, expected):
    feedback, requests = call(monkeypatch, checks)
    assert feedback.value is expected
    assert len(requests) == 1
    assert requests[0]["num_retries"] == 0
    parsed = json.loads(feedback.metadata["martian.judge.checks"])
    assert [c["status"] for c in parsed] == [c["status"] for c in checks]
    if checks[0].get("evidence_ids"):
        assert parsed[0]["evidence"] == "Hello!"
    assert feedback.metadata["martian.judge.version"] == RESPONSE_GUIDELINES_VERSION
    assert "martian.judge.prompt" not in feedback.metadata
    STATE.reset()
    result = SimpleNamespace(result_df=pd.DataFrame({"response": ["Hello!"], "policy/value": [feedback.value]}))
    try:
        if expected:
            assert_checks(result, scorers=["policy"])
        else:
            with pytest.raises(GateFailure):
                assert_checks(result, scorers=["policy"])
    finally:
        STATE.reset()


@pytest.mark.parametrize(
    "checks",
    [
        [check("no")],
        [check("uncertain")],
        [check("violated")],
        [check("violated", kind="quote", evidence="invented quote")],
        [check("violated", kind="absence", evidence=" ")],
        [check(index=True)],
        [check(index=2)],
        [check(), check()],
        [{**check(), "reason": " "}],
        [{**check(), "unexpected": True}],
    ],
)
def test_bad_judgment_is_error_without_retry_or_repair(monkeypatch, checks):
    with pytest.raises(ValueError):
        call(monkeypatch, checks)


def test_scope_roles_and_parameters(monkeypatch):
    params = {"temperature": 0}
    feedback, requests = call(monkeypatch, [check()], inference_params=params, include_judge_prompt=True)
    messages = requests[0]["prompt"]
    assert [m.role for m in messages] == ["system", "user"]
    assert "All applicable guidelines satisfied" in messages[0].content
    assert json.loads(messages[1].content) == {
        "request": "How to greet?",
        "response_segments": [{"id": 1, "text": "Hello!"}],
    }
    assert "do-not-send" not in feedback.metadata["martian.judge.prompt"]
    assert requests[0]["inference_params"] == params
    schema = requests[0]["response_format"].model_json_schema()
    assert schema["$defs"]["_CitationCheck"]["properties"]["status"]["enum"] == [
        "complied",
        "violated",
        "not_applicable",
        "uncertain",
    ]


@pytest.mark.parametrize("params", [{"messages": []}, {"temperature": float("nan")}, {"seed": True}, {"top_p": 0}])
def test_parameters_cannot_override_prompt_or_schema(params):
    with pytest.raises(ValueError):
        response_guidelines(model="openai:/stub", inference_params=params)


def test_quote_whitespace_is_not_an_invalid_judgment():
    value = [check("violated", kind="quote", evidence="Hello! We open at nine.")]
    assert _validate_checks(value, 1, "Hello!\n\nWe open at nine.")[0].status == "violated"
    # Whitespace tolerance cannot turn an invented word or case change into evidence.
    for response in ("Hello!\n\nWe close at nine.", "HELLO!\n\nWe open at nine."):
        with pytest.raises(ValueError, match="quote"):
            _validate_checks(value, 1, response)


@pytest.mark.parametrize(
    "response",
    [
        "\nHello!\r\n\r\nGoodbye.\n",
        "A single line",
        "\nজুব্রোকার রাজধানী হলো **‘Zubrowkaburg’**\n",
        "1. Antik Mısır\n  Details with İzmir and İstanbul.\n\n2. Antik Roma\n",
    ],
)
def test_segments_preserve_all_original_text_and_offsets(response):
    segments = _response_segments(response)
    assert "".join(s["text"] for s in segments) == response
    assert [s["id"] for s in segments] == list(range(1, len(segments) + 1))
    assert all(s["text"] == response[s["start"] : s["end"]] for s in segments)


def test_discontinuous_citations_keep_intervening_context():
    response = "First item.\nThis middle sentence negates the claim.\nThird item."
    values = [{**check("violated", kind="quote"), "evidence_ids": [3, 1]}]
    resolved, citations = _resolve_citations(values, 1, response)
    assert resolved[0].evidence == response
    assert citations == [{"guideline_index": 1, "segment_ids": [1, 3], "start": 0, "end": len(response)}]


def test_unicode_is_copied_from_source_not_normalized():
    response = "The name is **‘Zubrowkaburg’**. Bengali: য়. Turkish: ı.\n"
    resolved, _ = _resolve_citations([{**check("violated", kind="quote"), "evidence_ids": [1]}], 1, response)
    assert resolved[0].evidence == response
    assert "‘" in resolved[0].evidence and "ı" in resolved[0].evidence


@pytest.mark.parametrize(
    "value",
    [
        {**check("violated", kind="quote"), "evidence_ids": [0]},
        {**check("violated", kind="quote"), "evidence_ids": [2]},
        {**check("violated", kind="quote"), "evidence_ids": [True]},
        {**check("violated", kind="quote"), "evidence_ids": ["1"]},
        {**check("violated", kind="quote"), "evidence_ids": [1, 1]},
        {**check("violated", kind="quote"), "evidence_ids": []},
        {**check("violated", kind="quote", evidence="invented ... quote"), "evidence_ids": [1]},
        {**check("violated", kind="absence", evidence="Missing greeting"), "evidence_ids": [1]},
        {**check(), "evidence_ids": [1]},
    ],
)
def test_citation_errors_fail_closed_without_model_retry(monkeypatch, value):
    calls = []

    def invoke(**kwargs):
        calls.append(kwargs)
        return Feedback(name="policy", value=[value])

    monkeypatch.setattr("martian.evals.judges.invoke_judge_model", invoke)
    with pytest.raises(ValueError):
        response_guidelines(model="openai:/stub")(
            inputs="A request", outputs="Text with a fake segment ID 99.", expectations={"guidelines": ["A rule"]}
        )
    assert len(calls) == 1 and calls[0]["num_retries"] == 0


def test_omission_and_compliance_do_not_need_source_quotes():
    resolved, citations = _resolve_citations(
        [
            {**check("violated", kind="absence", evidence="The required greeting is missing."), "evidence_ids": []},
            {**check("complied", index=2), "evidence_ids": []},
        ],
        2,
        "We close at nine.",
    )
    assert [c.status for c in resolved] == ["violated", "complied"]
    assert citations == []


@pytest.mark.parametrize("status", ["complied", "not_applicable"])
@pytest.mark.parametrize("kind, ids", [("quote", [1]), ("absence", []), ("none", [])])
def test_supporting_evidence_does_not_change_nonviolation_status(status, kind, ids):
    values = [{**check(status, kind=kind), "evidence_ids": ids}]
    resolved, citations = _resolve_citations(values, 1, "The actual source text.")
    assert resolved[0].status == status
    if kind == "quote":
        assert resolved[0].evidence == "The actual source text."
        assert citations[0]["segment_ids"] == [1]
    elif kind == "absence":
        assert resolved[0].evidence == values[0]["reason"]
    else:
        assert resolved[0].evidence == ""


def test_wire_schema_cannot_request_model_transcribed_quotes(monkeypatch):
    _, requests = call(monkeypatch, [check()])
    properties = requests[0]["response_format"].model_json_schema()["$defs"]["_CitationCheck"]["properties"]
    assert "evidence" not in properties
    assert properties["evidence_ids"]["items"]["type"] == "integer"


@pytest.mark.parametrize("guidelines", [None, [], "a rule", [""], [3]])
def test_missing_criteria_do_not_call_model(monkeypatch, guidelines):
    def unexpected(**kwargs):
        pytest.fail("Invalid input invoked the model")

    monkeypatch.setattr("martian.evals.judges.invoke_judge_model", unexpected)
    with pytest.raises(ValueError):
        response_guidelines(model="openai:/stub")(
            inputs="Request", outputs="Reply", expectations={"guidelines": guidelines}
        )


def test_actual_mlflow_adapter_validates_fallback_output(monkeypatch):
    """Exercise MLflow's real request/schema/Feedback adapter, replacing only HTTP."""
    from mlflow.genai.judges.adapters import gateway_adapter

    monkeypatch.setenv("OPENAI_API_KEY", "stub-key")
    monkeypatch.setenv("OPENAI_API_BASE", "https://judge.test.example/v1")
    monkeypatch.setenv("MLFLOW_DISABLE_TELEMETRY", "true")
    requests = []
    content = {"result": [{**check(), "evidence_ids": []}]}

    def send(**kwargs):
        requests.append(deepcopy(kwargs["payload"]))
        return {
            "id": "stub-request",
            "object": "chat.completion",
            "created": 0,
            "model": "stub",
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": json.dumps(content)}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70},
        }

    monkeypatch.setattr(gateway_adapter, "send_chat_request", send)
    judge = response_guidelines(model="openai:/stub")
    kwargs = {"inputs": "Say hello", "outputs": "Hello!", "expectations": {"guidelines": ["Greet"]}}
    feedback = judge(**kwargs)
    assert feedback.value is True
    assert len(requests) == 1
    assert requests[0]["response_format"]["json_schema"]["schema"]["properties"]["result"]["type"] == "array"
    assert int(feedback.metadata["mlflow.assessment.judgeInputTokens"]) == 50
    # A provider can ignore JSON Schema: enforce it again after the adapter.
    content["result"] = "yes"
    with pytest.raises(ValueError, match="schema"):
        judge(**kwargs)
    assert len(requests) == 2
