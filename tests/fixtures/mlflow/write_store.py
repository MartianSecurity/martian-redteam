"""Write a small MLflow tracking store with MLflow itself: the source that
tests/evals/test_mlflow_import.py copies.

    python tests/fixtures/mlflow/write_store.py <folder> [<database URL>]

It writes a SQLite tracking store, ``<folder>/mlflow.db``, or the database the URL names, with its
artifacts in ``<folder>/mlruns``, using whichever MLflow the interpreter has. The test runs it with
the mlflow-skinny the SDK pins, in a process of its own, so that MLflow's tracking URI and active
experiment stay out of the test's process. Nothing in it calls a model: the agent answers from a
table, and the scorers are code.

The store holds what a team that used MLflow for GenAI evaluation would have:

- the experiment ``support-bot``, with five traces of an agent: two sessions of two turns by two
  users, each trace a ``support_turn`` span with the agent's ``AGENT``, ``TOOL`` and
  ``CHAT_MODEL`` spans under it, and a fifth turn without a session, which fails;
- on those traces, a person's feedback, a person's expectation, an LLM judge's feedback, and a
  person's feedback that a later correction overrides;
- the evaluation dataset ``support-questions``, three records with expectations;
- two evaluation runs over that dataset (``mlflow.genai.evaluate`` with two code scorers), the
  second with a better agent;
- a run with a parameter and a metric and no traces;
- the experiment ``scratch``, deleted, with one trace.
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path

import mlflow
from mlflow.entities import AssessmentSource, AssessmentSourceType
from mlflow.genai.datasets import create_dataset
from mlflow.genai.scorers import scorer

ANSWERS = {
    "How do I reset my password?": "Open Settings, choose Security, then Reset password.",
    "Can I get a refund after 30 days?": "Refunds are available within 30 days of purchase.",
    "Where is my order?": "Your order ships in two days.",
}
BETTER = {**ANSWERS, "Where is my order?": "Track your order under Orders in your account."}
EXPECTED = {
    "How do I reset my password?": "Open Settings, choose Security, then Reset password.",
    "Can I get a refund after 30 days?": "No. Refunds are available within 30 days of purchase.",
    "Where is my order?": "Track your order under Orders in your account.",
}
FAILING = "Why was I charged twice?"


def make_agent(table: dict[str, str]):  # noqa: ANN201 - MLflow's decorator keeps the signature
    @mlflow.trace(name="answer", span_type="CHAT_MODEL")
    def answer(question: str) -> str:
        return table.get(question, "I don't know.")

    @mlflow.trace(name="support_agent", span_type="AGENT")
    def support_agent(question: str) -> str:
        with mlflow.start_span("search_help_center", span_type="TOOL") as span:
            span.set_inputs({"query": question})
            span.set_outputs({"articles": 1 if question in table else 0})
        if question == FAILING:
            raise RuntimeError("the billing service did not answer")
        return answer(question)

    return support_agent


def write(folder: Path, url: str | None = None) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    mlflow.set_tracking_uri(url or f"sqlite:///{folder / 'mlflow.db'}")
    artifacts = (folder / "mlruns").resolve().as_uri()
    experiment_id = mlflow.create_experiment("support-bot", artifact_location=artifacts, tags={"team": "support"})
    mlflow.set_experiment(experiment_id=experiment_id)
    agent = make_agent(ANSWERS)

    def turn(question: str, session: str | None, user: str | None) -> str:
        @mlflow.trace(name="support_turn", span_type="CHAIN")
        def support_turn(question: str) -> str:
            if session is not None:
                mlflow.update_current_trace(session_id=session, user=user, tags={"channel": "chat"})
            return agent(question)

        with contextlib.suppress(RuntimeError):
            support_turn(question)
        trace_id = mlflow.get_last_active_trace_id()
        assert trace_id is not None
        return trace_id

    questions = list(ANSWERS)
    traces = [
        turn(questions[0], "conversation-1", "alice"),
        turn(questions[1], "conversation-1", "alice"),
        turn(questions[2], "conversation-2", "bob"),
        turn(questions[0], "conversation-2", "bob"),
        turn(FAILING, None, None),
    ]
    mlflow.flush_trace_async_logging()

    person = AssessmentSource(source_type=AssessmentSourceType.HUMAN, source_id="alice@example.com")
    judge = AssessmentSource(source_type=AssessmentSourceType.LLM_JUDGE, source_id="openai:/gpt-4o-mini")
    mlflow.log_feedback(trace_id=traces[0], name="helpful", value=True, source=person, rationale="It answers.")
    mlflow.log_expectation(trace_id=traces[1], name="expected_response", value=EXPECTED[questions[1]], source=person)
    mlflow.log_feedback(trace_id=traces[1], name="correct", value="no", source=judge, rationale="It does not say no.")
    wrong = mlflow.log_feedback(trace_id=traces[2], name="helpful", value=False, source=person)
    mlflow.override_feedback(
        trace_id=traces[2], assessment_id=wrong.assessment_id, value=True, rationale="It is fine.", source=person
    )

    dataset = create_dataset(name="support-questions", experiment_id=experiment_id, tags={"owner": "support"})
    dataset.merge_records(
        [{"inputs": {"question": q}, "expectations": {"expected_response": EXPECTED[q]}} for q in EXPECTED]
    )

    @scorer
    def exact_match(outputs: str, expectations: dict[str, str]) -> bool:
        return outputs == expectations["expected_response"]

    @scorer
    def answer_length(outputs: str) -> int:
        return len(outputs)

    for name, table in (("baseline", ANSWERS), ("candidate", BETTER)):
        with mlflow.start_run(run_name=name):
            mlflow.genai.evaluate(data=dataset, predict_fn=make_agent(table), scorers=[exact_match, answer_length])
    mlflow.flush_trace_async_logging()

    with mlflow.start_run(run_name="prompt-tuning"):
        mlflow.log_param("temperature", 0.2)
        mlflow.log_metric("loss", 0.31)

    scratch = mlflow.create_experiment("scratch", artifact_location=(folder / "scratch").resolve().as_uri())
    mlflow.set_experiment(experiment_id=scratch)
    turn("Is anyone there?", None, None)
    mlflow.flush_trace_async_logging()
    mlflow.delete_experiment(scratch)


if __name__ == "__main__":
    write(Path(sys.argv[1]).resolve(), sys.argv[2] if len(sys.argv) > 2 else None)
