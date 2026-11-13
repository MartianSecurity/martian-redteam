"""Explicit condition polarity, evidence and provider-boundary regressions."""

import json

import pytest
from mlflow.entities import Feedback

from martian.evals import response_conditions
from martian.evals.judges import RESPONSE_CONDITIONS_VERSION


def answer(value="yes", index=1, kind="quote", ids=None):
    return {
        "condition_index": index,
        "reason": "Observed content supports this answer.",
        "evidence_ids": [1] if ids is None else ids,
        "answer": value,
    }


def invoke(monkeypatch, values, conditions, **options):
    requests = []

    def provider(**kwargs):
        requests.append(kwargs)
        return Feedback(name="conditions", value=values)

    monkeypatch.setattr("martian.evals.judges.invoke_response_judge", provider)
    judge = response_conditions(model="openai:/stub", include_judge_prompt=True, verify=False, **options)
    result = judge(
        inputs={"message": "The original request.", "expected": False, "case_id": "hidden-case-id"},
        outputs="Original response.\nIncluding a qualification.",
        expectations={"conditions": conditions},
    )
    return result, requests


@pytest.mark.parametrize(
    "value, fail_when, expected",
    [("yes", "yes", False), ("no", "yes", True), ("yes", "no", True), ("no", "no", False)],
)
def test_sdk_controls_polarity_not_the_model(monkeypatch, value, fail_when, expected):
    # Rationale is deliberately not usable for deciding the verdict.
    proposed = {**answer(value), "reason": "The response passes. The response fails."}
    result, requests = invoke(monkeypatch, [proposed], [{"question": "Is the condition met?", "fail_when": fail_when}])
    assert result.value is expected
    assert len(requests) == 1
    assert result.metadata["martian.judge.version"] == RESPONSE_CONDITIONS_VERSION
    assert json.loads(result.metadata["martian.judge.condition_answers"]) == [proposed]
    assert json.loads(result.metadata["martian.judge.condition_polarities"]) == [fail_when]
    check = json.loads(result.metadata["martian.judge.checks"])[0]
    assert check["evidence"] == "Original response.\n"
    assert check["status"] == ("complied" if expected else "violated")


def test_polarity_and_reference_labels_never_enter_model_prompt(monkeypatch):
    first, _ = invoke(monkeypatch, [answer()], [{"question": "Is the condition met?", "fail_when": "yes"}])
    second, _ = invoke(monkeypatch, [answer()], [{"question": "Is the condition met?", "fail_when": "no"}])
    assert first.value is False and second.value is True
    assert first.metadata["martian.judge.prompt"] == second.metadata["martian.judge.prompt"]
    assert first.metadata["martian.judge.prompt_sha256"] == second.metadata["martian.judge.prompt_sha256"]
    assert first.metadata["martian.judge.conditions_sha256"] != second.metadata["martian.judge.conditions_sha256"]
    assert "hidden-case-id" not in first.metadata["martian.judge.prompt"]
    messages = json.loads(first.metadata["martian.judge.prompt"])
    questions = json.loads(messages[0]["content"].split("\nConditions:\n")[1].split("\nOutput schema:\n")[0])
    assert questions == [{"condition_index": 1, "question": "Is the condition met?"}]


def test_mixed_polarities_match_indices_even_if_answers_are_reordered(monkeypatch):
    result, _ = invoke(
        monkeypatch,
        [answer("yes", index=2), answer("no", index=1, kind="absence", ids=[])],
        [{"question": "Is A present?", "fail_when": "yes"}, {"question": "Is B present?", "fail_when": "no"}],
    )
    assert result.value is True
    assert [a["condition_index"] for a in json.loads(result.metadata["martian.judge.condition_answers"])] == [1, 2]


@pytest.mark.parametrize(
    "conditions",
    [
        None,
        [],
        "question",
        [{}],
        [{"question": "Q?"}],
        [{"question": " ", "fail_when": "yes"}],
        [{"question": "Q?", "fail_when": True}],
        [{"question": "Q?", "fail_when": "YES"}],
        [{"question": 4, "fail_when": "no"}],
        [{"question": "Q?", "fail_when": "yes", "expected": "yes"}],
    ],
)
def test_invalid_conditions_do_not_call_provider(monkeypatch, conditions):
    def forbidden(**kwargs):
        pytest.fail("Invalid conditions reached the provider")

    monkeypatch.setattr("martian.evals.judges.invoke_response_judge", forbidden)
    with pytest.raises(ValueError):
        response_conditions(model="openai:/stub", verify=False)(
            inputs="Request", outputs="Response", expectations={"conditions": conditions}
        )


@pytest.mark.parametrize(
    "values",
    [
        [],
        [answer("YES")],
        [answer("uncertain", kind="none", ids=[])],
        [answer("not_applicable")],
        [answer(index=True)],
        [answer(index=2)],
        [answer(), answer()],
        [answer(ids=[0])],
        [answer(ids=[99])],
        [answer(ids=[True])],
        [answer(ids=[1, 1])],
        [{**answer(), "evidence_kind": "none"}],
        [{**answer(), "evidence_kind": "absence"}],
        [{**answer(), "evidence": "An invented quote"}],
        [{**answer(), "reason": " "}],
    ],
)
def test_unknown_answers_and_bad_evidence_fail_closed_without_retry(monkeypatch, values):
    calls = []

    def provider(**kwargs):
        calls.append(kwargs)
        return Feedback(name="condition", value=values)

    monkeypatch.setattr("martian.evals.judges.invoke_response_judge", provider)
    with pytest.raises(ValueError):
        response_conditions(model="openai:/stub", verify=False)(
            inputs="Request",
            outputs="Original text.",
            expectations={"conditions": [{"question": "Is A present?", "fail_when": "yes"}]},
        )
    assert len(calls) == 1


def test_gateway_transport_returns_condition_answers(monkeypatch):
    from martian.evals import _judge_transport

    monkeypatch.setenv("OPENAI_API_KEY", "fixture-only")
    monkeypatch.setenv("OPENAI_API_BASE", "https://judge.test.example/v1")
    requests = []

    def send(**kwargs):
        requests.append(kwargs["payload"])
        return {
            "id": "fixture",
            "object": "chat.completion",
            "created": 0,
            "model": "stub",
            "usage": {"prompt_tokens": 30, "completion_tokens": 20, "total_tokens": 50},
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": json.dumps({"result": [answer()]})},
                }
            ],
        }

    monkeypatch.setattr(_judge_transport, "send_chat_request", send)
    result = response_conditions(model="openai:/stub", verify=False)(
        inputs="Request",
        outputs="Actual source.",
        expectations={"conditions": [{"question": "Does the condition hold?", "fail_when": "yes"}]},
    )
    assert result.value is False
    assert len(requests) == 1
    schema = requests[0]["response_format"]["json_schema"]["schema"]
    assert schema["$defs"]["_ConditionCheck"]["properties"]["answer"]["enum"] == ["yes", "no", "uncertain"]


@pytest.mark.parametrize("first, second", [("yes", "no"), ("no", "yes"), ("yes", "yes"), ("no", "no")])
def test_fixed_review_runs_for_both_outcomes_and_retains_both_stages(monkeypatch, first, second):
    calls = []

    def provider(**kwargs):
        calls.append(kwargs)
        return Feedback(
            name="policy",
            value=[answer(first if len(calls) == 1 else second)],
            metadata={"mlflow.assessment.judgeInputTokens": "10", "mlflow.assessment.judgeOutputTokens": "5"},
        )

    monkeypatch.setattr("martian.evals.judges.invoke_response_judge", provider)
    result = response_conditions(model="openai:/stub", include_judge_prompt=True, verify=True)(
        inputs="Original request",
        outputs="Original reply",
        expectations={"conditions": [{"question": "Does the condition hold?", "fail_when": "yes"}]},
    )
    assert len(calls) == 2
    assert result.value is (second == "no")
    assert calls[0]["prompt"][1] == calls[1]["prompt"][1]
    assert len(calls[1]["prompt"]) == 3
    assert json.loads(result.metadata["martian.judge.draft"])["value"][0]["answer"] == first
    assert json.loads(result.metadata["martian.judge.condition_answers"])[0]["answer"] == second
    assert result.metadata["martian.judge.verification"] == "fixed-review"
    assert result.metadata["martian.judge.model_calls"] == "2"
    assert int(result.metadata["mlflow.assessment.judgeInputTokens"]) == 20
    assert int(result.metadata["mlflow.assessment.judgeOutputTokens"]) == 10
    assert "martian.judge.review_prompt" in result.metadata


@pytest.mark.parametrize("bad_stage", [1, 2])
@pytest.mark.parametrize("bad", [answer("uncertain", ids=[]), answer(ids=[99]), {**answer(), "answer": True}])
def test_fixed_review_does_not_repair_errors_or_uncertainty(monkeypatch, bad_stage, bad):
    calls = []

    def provider(**kwargs):
        calls.append(kwargs)
        return Feedback(name="policy", value=[bad if len(calls) == bad_stage else answer()])

    monkeypatch.setattr("martian.evals.judges.invoke_response_judge", provider)
    with pytest.raises(ValueError):
        response_conditions(model="openai:/stub", verify=True)(
            inputs="Request",
            outputs="Reply",
            expectations={"conditions": [{"question": "Condition?", "fail_when": "yes"}]},
        )
    assert len(calls) == bad_stage


def test_verify_option_requires_a_boolean():
    with pytest.raises(ValueError, match="verify"):
        response_conditions(model="openai:/stub", verify="yes")


def test_default_uses_one_model_call(monkeypatch):
    calls = []

    def provider(**kwargs):
        calls.append(kwargs)
        return Feedback(name="policy", value=[answer()])

    monkeypatch.setattr("martian.evals.judges.invoke_response_judge", provider)
    result = response_conditions(model="openai:/stub")(
        inputs="Request",
        outputs="Reply",
        expectations={"conditions": [{"question": "Condition?", "fail_when": "yes"}]},
    )
    assert len(calls) == 1
    assert result.metadata["martian.judge.verification"] == "none"
    assert result.metadata["martian.judge.model_calls"] == "1"
