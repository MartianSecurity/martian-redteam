"""Deterministic checks decide from the text alone and never call a model."""

import pytest

from martian.evals import contains, is_json, matches, not_contains, not_matches


def value(check, outputs, expectations=None):
    feedback = check(outputs=outputs, expectations=expectations) if expectations is not None else check(outputs=outputs)
    return feedback.value


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


@pytest.mark.parametrize(
    "build",
    [
        lambda: contains(),
        lambda: not_contains(""),
        lambda: contains("x", name=" "),
        lambda: not_contains(expectation="secret", name="secret"),
        lambda: is_json(required_keys=[""]),
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
