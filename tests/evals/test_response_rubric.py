"""Direct rubric/policy pass/fail judgment, context separation, and citation resolution tests."""

import json

import pytest
from mlflow.entities import Feedback

from martian.evals import response_rubric
from martian.evals.judges import RESPONSE_RUBRICS_VERSION


def rubric_check(verdict="pass", index=1, ids=None):
    return {
        "rubric_index": index,
        "reason": "Evaluated against the stated rubric criteria.",
        "verdict": verdict,
        "evidence_ids": [1] if ids is None else ids,
    }


def invoke(monkeypatch, values, rubrics="Must be grounded in context.", **options):
    requests = []

    def provider(**kwargs):
        requests.append(kwargs)
        return Feedback(name="rubric", value=values)

    monkeypatch.setattr("martian.evals.judges.invoke_judge_model", provider)
    judge = response_rubric(model="openai:/stub", include_judge_prompt=True, verify=False, **options)
    result = judge(
        inputs={
            "message": "The original request.",
            "context": "Trusted source context.",
            "expected": False,
            "case_id": "hidden-case-id",
            "gold_pass": True,
        },
        outputs="Original response.\nIncluding a qualification.",
        expectations={"rubric": rubrics},
    )
    return result, requests


@pytest.mark.parametrize("verdict, expected", [("pass", True), ("fail", False)])
def test_direct_verdict_maps_to_boolean_and_preserves_metadata(monkeypatch, verdict, expected):
    proposed = rubric_check(verdict)
    result, requests = invoke(monkeypatch, [proposed])
    assert result.value is expected
    assert len(requests) == 1 and requests[0]["num_retries"] == 0
    assert result.metadata["martian.judge.version"] == RESPONSE_RUBRICS_VERSION
    assert json.loads(result.metadata["martian.judge.rubric_verdicts"]) == [proposed]
    check = json.loads(result.metadata["martian.judge.checks"])[0]
    assert check["evidence"] == "Original response.\n"
    assert check["status"] == ("complied" if expected else "violated")


def test_context_and_request_are_separated_and_reference_labels_excluded(monkeypatch):
    result, _ = invoke(monkeypatch, [rubric_check("pass")])
    prompt_str = result.metadata["martian.judge.prompt"]
    assert "hidden-case-id" not in prompt_str
    assert "gold_pass" not in prompt_str
    messages = json.loads(prompt_str)
    payload = json.loads(messages[1]["content"])
    assert payload["context"] == "Trusted source context."
    assert payload["request"] == "The original request."
    assert payload["max_response_segment_id"] == 2
    rubrics_section = json.loads(messages[0]["content"].split("\nRubrics:\n")[1])
    assert rubrics_section == [{"rubric_index": 1, "rubric": "Must be grounded in context."}]


def test_duplicate_valid_segment_ids_are_deduplicated_and_out_of_range_fails_closed(monkeypatch):
    # Duplicate valid IDs within a single rubric check should deduplicate cleanly
    result, _ = invoke(monkeypatch, [rubric_check("pass", ids=[1, 1, 2])])
    assert result.value is True
    citations = json.loads(result.metadata["martian.judge.citations"])
    assert citations[0]["segment_ids"] == [1, 2]

    # Out-of-range segment IDs must still fail closed
    with pytest.raises(ValueError, match="Judge cited an invalid response segment"):
        invoke(monkeypatch, [rubric_check("pass", ids=[1, 99])])


def test_multiple_rubrics_all_must_pass(monkeypatch):
    result, _ = invoke(
        monkeypatch,
        [rubric_check("fail", index=2, ids=[]), rubric_check("pass", index=1)],
        rubrics=["Rubric 1", "Rubric 2"],
    )
    assert result.value is False
    verdicts = json.loads(result.metadata["martian.judge.rubric_verdicts"])
    assert [v["rubric_index"] for v in verdicts] == [1, 2]


@pytest.mark.parametrize(
    "rubrics",
    [
        None,
        "",
        "   ",
        [],
        [""],
        [123],
    ],
)
def test_invalid_rubrics_do_not_call_provider(monkeypatch, rubrics):
    def forbidden(**kwargs):
        pytest.fail("Invalid rubrics reached the provider")

    monkeypatch.setattr("martian.evals.judges.invoke_judge_model", forbidden)
    with pytest.raises(ValueError):
        response_rubric(model="openai:/stub", verify=False)(
            inputs="Request", outputs="Response", expectations={"rubric": rubrics}
        )


@pytest.mark.parametrize(
    "values",
    [
        [],
        [rubric_check("PASS")],
        [rubric_check("uncertain", ids=[])],
        [rubric_check("yes")],
        [rubric_check(index=True)],
        [rubric_check(index=2)],
        [rubric_check(), rubric_check()],
        [rubric_check(ids=[0])],
        [rubric_check(ids=[99])],
        [rubric_check(ids=[True])],
        [{**rubric_check(), "reason": " "}],
    ],
)
def test_unknown_verdicts_and_invalid_checks_fail_closed(monkeypatch, values):
    with pytest.raises(ValueError):
        invoke(monkeypatch, values)


def test_fixed_review_runs_for_rubrics(monkeypatch):
    calls = []

    def provider(**kwargs):
        calls.append(kwargs)
        return Feedback(
            name="rubric",
            value=[rubric_check("fail" if len(calls) == 1 else "pass")],
            metadata={"mlflow.assessment.judgeInputTokens": "12", "mlflow.assessment.judgeOutputTokens": "6"},
        )

    monkeypatch.setattr("martian.evals.judges.invoke_judge_model", provider)
    result = response_rubric(model="openai:/stub", include_judge_prompt=True, verify=True)(
        inputs={"message": "Original request", "context": "Some context"},
        outputs="Original reply",
        expectations={"rubric": "Pass if safe."},
    )
    assert len(calls) == 2
    assert result.value is True
    assert result.metadata["martian.judge.verification"] == "fixed-review"
    assert result.metadata["martian.judge.model_calls"] == "2"
    assert int(result.metadata["mlflow.assessment.judgeInputTokens"]) == 24
    assert int(result.metadata["mlflow.assessment.judgeOutputTokens"]) == 12
