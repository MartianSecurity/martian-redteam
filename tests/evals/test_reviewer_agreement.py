"""People's agreement with each other on the items two or more of them answered
(martian.evals.agreement), and the evals core's own route that reports it per review queue and
question, beside each judge's agreement with people. No model is called."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from martian.evals.agreement import (
    MINIMUM_LABELLED,
    cohen_kappa,
    compare_reviewers,
    concordance,
    fleiss_kappa,
    intraclass_correlation,
    question_agreement,
    queue_agreement,
)
from martian.evals.model import (
    CaseResult,
    Project,
    Review,
    ReviewQuestion,
    ReviewQueue,
    Run,
    Score,
    ScorerVersion,
    StoreRef,
    Trace,
)
from martian.evals.records.sql import SqlRecordStore
from martian.evals.server import DEFAULT_PROJECT, ENTERPRISE, SINGLE_USER, create_app, organisation_principal
from martian.evals.stats import mean_interval, wilson
from martian.evals.store import MemoryStore
from martian.evals.store.mlflow_local import MlflowStore
from tests.evals.write_fixture import Served, request_id, serve

T0 = datetime(2026, 11, 15, 9, 0, tzinfo=UTC)


def close(found, expected, tolerance=1e-12):
    assert found is not None
    assert found == pytest.approx(expected, abs=tolerance)


def two(a: list, b: list) -> list[dict]:
    """Items answered by reviewers a and b; None is no answer."""
    return [{k: v for k, v in (("a", x), ("b", y)) if v is not None} for x, y in zip(a, b, strict=True)]


# --- the statistics ---------------------------------------------------------------------------


def test_fleiss_kappa_matches_fleiss_published_example_and_allows_items_their_own_reviewers():
    # Fleiss (1971), as Wikipedia gives it: 10 items, 14 answers each over 5 categories, kappa 0.210.
    table = [
        [0, 0, 0, 0, 14], [0, 2, 6, 4, 2], [0, 0, 3, 5, 6], [0, 3, 9, 2, 0], [2, 2, 8, 1, 1],
        [7, 7, 0, 0, 0], [3, 2, 6, 3, 0], [2, 5, 3, 2, 2], [6, 5, 2, 1, 0], [0, 2, 2, 3, 7],
    ]  # fmt: skip
    items = [[f"c{k}" for k, n in enumerate(row) for _ in range(n)] for row in table]
    close(fleiss_kappa(items), 0.20993, 1e-5)
    # Each item may have its own number of answers; an item with one answer has no pair to compare.
    found = fleiss_kappa([["x", "x", "x"], ["x", "y"], ["y", "y"], ["y"]])
    # P̄ = (1 + 0 + 1) / 3, and 7 answers, 4 of x and 3 of y: P_e = 25/49.
    close(found, (2 / 3 - 25 / 49) / (1 - 25 / 49))
    assert fleiss_kappa([[True, True], [True, True]]) is None and fleiss_kappa([["x"]]) is None
    assert fleiss_kappa([[True, False], [False, True]]) == -1.0


def test_the_intraclass_correlation_matches_shrout_and_fleiss_and_allows_unequal_items():
    # Shrout and Fleiss (1979): 6 items, 4 answers each; ICC(1,1) is 0.166.
    items = [[9, 2, 5, 8], [6, 1, 3, 2], [8, 4, 6, 8], [7, 1, 2, 6], [10, 5, 6, 9], [6, 2, 4, 7]]
    close(intraclass_correlation(items), 0.16574, 1e-5)
    # Equal answers on each item agree fully, whatever the spread between items.
    assert intraclass_correlation([[1, 1], [4, 4, 4], [2, 2]]) == 1.0
    assert intraclass_correlation([[3, 3], [3, 3]]) == 1.0
    assert intraclass_correlation([[1, 2]]) is None
    # Unequal items: a = 2, N = 5, n0 = (5 − 13/5) / 1 = 2.4.
    between, within = (2 * (1.5 - 2.6) ** 2 + 3 * (3.33333333333 - 2.6) ** 2) / 1, (0.5 + 2 / 3) / 3
    close(intraclass_correlation([[1, 2], [3, 3, 4]]), (between - within) / (between + 1.4 * within), 1e-9)


def test_two_reviewers_are_compared_with_cohens_kappa_and_their_shared_items_counted():
    # The textbook pairs: 20 both pass, 5 and 10 split, 15 both fail. p_o = 0.7 and kappa 0.4.
    a = [True] * 25 + [False] * 25 + [True, None]
    b = [True] * 20 + [False] * 5 + [True] * 10 + [False] * 15 + [None, None]
    found = compare_reviewers(two(a, b), "boolean", question="polite_tone")
    assert (found.question, found.kind, found.status, found.method) == ("polite_tone", "boolean", "measured", "cohen")
    # 51 items were answered, 50 by both, and the 52nd by nobody.
    assert (found.answered, found.shared, found.reviewers, found.incomparable) == (51, 50, 2, 0)
    assert (found.agreed, found.minimum) == (35, MINIMUM_LABELLED)
    close(found.agreement, 0.7)
    assert found.agreement_confidence_95 == wilson(35, 50)
    close(found.kappa, 0.4)
    assert found.kappa == cohen_kappa([(x, y) for x, y in zip(a[:50], b[:50], strict=True)])
    assert found.mean_absolute_difference is None and found.concordance is None


def test_more_than_two_reviewers_are_compared_with_fleiss_kappa():
    # Three people; each item has two or three of them.
    items = [{"a": True, "b": True, "c": True}] * 12 + [{"a": True, "c": False}] * 8 + [{"b": False, "c": False}] * 10
    found = compare_reviewers(items, "boolean")
    assert (found.status, found.method, found.shared, found.reviewers, found.agreed) == (
        "measured",
        "fleiss",
        30,
        3,
        22,
    )
    close(found.agreement, 22 / 30)
    assert found.kappa == fleiss_kappa([list(item.values()) for item in items])
    categories = [{"a": "warm", "b": "warm"}] * 20 + [{"a": "warm", "b": "cold"}] * 6 + [{"a": "cold", "b": "cold"}] * 4
    found = compare_reviewers(categories, "categorical")
    assert (found.method, found.agreed) == ("cohen", 24)
    assert found.kappa == cohen_kappa([(item["a"], item["b"]) for item in categories])


def test_too_few_shared_items_give_the_counts_and_no_number():
    few = compare_reviewers(two([True] * 29 + [False], [True] * 28 + [False, None]), "boolean")
    assert (few.status, few.answered, few.shared, few.reviewers, few.agreed) == ("too_few", 30, 29, 2, 28)
    assert few.method is None and few.agreement is None and few.agreement_confidence_95 is None and few.kappa is None
    nobody = compare_reviewers([{"a": True}, {}], "boolean")
    assert (nobody.status, nobody.answered, nobody.shared, nobody.reviewers, nobody.agreed) == ("too_few", 1, 0, 0, 0)
    with pytest.raises(ValueError):
        compare_reviewers([], "boolean", minimum=0)
    with pytest.raises(ValueError):
        compare_reviewers([], "scale")  # type: ignore[arg-type]


def test_numeric_answers_report_how_far_apart_they_are_and_their_concordance():
    # Reviewer b always answers one more than a: one point apart on every item.
    a = [float(i % 5) for i in range(30)]
    found = compare_reviewers(two(a, [x + 1 for x in a]), "numeric")
    assert (found.status, found.method, found.agreed, found.agreement, found.kappa) == (
        "measured",
        "lin",
        None,
        None,
        None,
    )
    close(found.mean_absolute_difference, 1.0)
    assert found.mean_absolute_difference_confidence_95 == mean_interval([1.0] * 30)
    assert found.concordance == concordance([(x, x + 1) for x in a])
    # Three reviewers: each item's pairs of answers are averaged, and the coefficient is ICC(1).
    items = [{"a": x, "b": x + 1, "c": x + 2} for x in a]
    found = compare_reviewers(items, "numeric")
    assert (found.method, found.reviewers) == ("icc", 3)
    close(found.mean_absolute_difference, 4 / 3)
    assert found.concordance == intraclass_correlation([[x, x + 1, x + 2] for x in a])
    assert compare_reviewers(items[:29], "numeric").mean_absolute_difference is None


def test_free_text_is_not_compared_and_answers_of_another_kind_are_left_out():
    text = compare_reviewers([{"a": "Fine.", "b": "Fine."}] * 40, "text")
    assert (text.status, text.shared, text.agreed, text.method) == ("not_comparable", 40, None, None)
    # A 1 is no pass/fail answer, and True no number.
    mixed = compare_reviewers([{"a": True, "b": 1}, {"a": True, "b": True, "c": 1}], "boolean")
    assert (mixed.answered, mixed.shared, mixed.incomparable, mixed.reviewers) == (2, 1, 2, 2)


# --- answers from reviews and queues -----------------------------------------------------------

QUESTION = ReviewQuestion(name="polite_tone", kind="boolean", instruction="Is the reply polite?")
SCORE = ReviewQuestion(name="helpful", kind="numeric", instruction="How helpful, 1 to 5?", minimum=1, maximum=5)


def queue(queue_id: str, run: str, cases: list[str], *questions: ReviewQuestion, minutes: int = 0) -> ReviewQueue:
    return ReviewQueue(
        id=queue_id,
        run_id=run,
        name=f"Queue {queue_id}",
        questions=list(questions or [QUESTION]),
        case_ids=cases,
        created_by="creator",
        created_at=T0 + timedelta(minutes=minutes),
    )


def answer(queue_id: str | None, case: str, reviewer: str | None, value, *, question: str = "polite_tone",
           run: str = "run-1", minutes: int = 5) -> Review:  # fmt: skip
    return Review(
        id=f"{queue_id}-{run}-{case}-{question}-{reviewer}-{minutes}",
        run_id=run,
        case_id=case,
        criterion=question,
        state="complete" if value is not None else "pending",
        value=value,
        reviewer=reviewer,
        queue_id=queue_id,
        created_at=T0 + timedelta(minutes=minutes),
    )


def test_each_reviewer_counts_once_per_item_with_their_newest_review():
    reviews = [
        answer("q1", "c1", "creator", None, minutes=0),  # the queue's pending review: no answer
        answer("q1", "c1", "a", True),
        answer("q1", "c1", "b", False, minutes=6),  # replaces a's in the chain, and both count
        answer("q1", "c1", "a", False, minutes=7),  # a answers again: their newer answer counts
        answer("q1", "c2", "a", True),
        answer("q1", "c2", "b", True, minutes=6),
        answer("q1", "c2", "b", None, minutes=8),  # b withdraws theirs
        answer("q1", "c3", "a", True),
        answer("q1", "c3", None, True, minutes=6),  # names no reviewer: counts for nobody
        answer(None, "c4", "a", True, question="human_review"),  # in no queue
        answer("q9", "c4", "a", True),  # a queue not reported
        answer("q9", "c4", "b", True),
    ]
    (found,) = queue_agreement(reviews, [queue("q1", "run-1", ["c1", "c2", "c3"])])
    (tone,) = found.questions
    assert (found.queue_id, found.name, found.run_id) == ("q1", "Queue q1", "run-1")
    # c1 is shared and agreed, False and False; c2 and c3 have one answer each.
    assert (tone.answered, tone.shared, tone.reviewers, tone.agreed, tone.status) == (3, 1, 2, 1, "too_few")
    served = found.as_dict()
    assert served["queue_id"] == "q1" and served["questions"][0]["shared"] == 1
    assert set(served["questions"][0]) == {
        "question", "kind", "answered", "shared", "reviewers", "incomparable", "minimum", "status", "method",
        "agreed", "agreement", "agreement_confidence_95", "kappa", "mean_absolute_difference",
        "mean_absolute_difference_confidence_95", "concordance", "blind",
    }  # fmt: skip
    # No queue here has blind double review, so no report rests on blind answers.
    assert served["questions"][0]["blind"] is False


def test_queues_report_each_question_and_questions_are_also_reported_across_queues():
    cases = [f"c{i}" for i in range(20)]
    first = queue("q1", "run-1", cases, QUESTION, SCORE)
    second = queue("q2", "run-2", cases, ReviewQuestion(name="polite_tone", kind="numeric", instruction="How polite?"),
                   minutes=10)  # fmt: skip
    reviews = [answer("q1", c, r, True) for c in cases for r in ("a", "b")]
    reviews += [answer("q1", c, r, 3 + (r == "b"), question="helpful") for c in cases[:12] for r in ("a", "b")]
    reviews += [answer("q2", c, r, 1, run="run-2") for c in cases for r in ("a", "c")]
    reviews += [answer("q2", c, "b", True, run="run-2") for c in cases[:3]]  # of another kind there
    one, other = queue_agreement(reviews, [first, second])
    assert [(q.question, q.kind, q.shared, q.reviewers) for q in one.questions] == [
        ("polite_tone", "boolean", 20, 2),
        ("helpful", "numeric", 12, 2),
    ]
    assert [(q.question, q.kind, q.shared, q.incomparable) for q in other.questions] == [
        ("polite_tone", "numeric", 20, 3)
    ]
    # Across queues each item is its run and case; the newer queue's question gives the kind, so
    # the older queue's pass/fail answers are of another kind.
    tone, helpful = sorted(question_agreement(reviews, [first, second]), key=lambda q: q.question != "polite_tone")
    assert (tone.question, tone.kind, tone.answered, tone.shared, tone.incomparable) == (
        "polite_tone",
        "numeric",
        20,
        20,
        43,
    )
    assert (tone.reviewers, tone.status, tone.method) == (2, "too_few", None)
    assert (helpful.question, helpful.shared) == ("helpful", 12)
    assert [q.question for q in question_agreement(reviews, [])] == []


# --- the core's route ---------------------------------------------------------------------------

RUN = str(uuid.UUID(int=89))
QUEUE = str(uuid.UUID(int=189))
JUDGE = ScorerVersion(name="polite_tone", kind="llm_judge", type="instructions", version="", model="openai:/judge")
DEFINITION = {
    "name": "polite_tone",
    "description": "",
    "instructions": "Is {{ outputs }} polite? Answer pass or fail.",
    "kind": "boolean",
    "options": [],
    "minimum": 0,
    "maximum": 1,
    "operation_id": "",
}


def tid(n: int) -> str:
    return "tr-" + f"{n:032x}"


async def seed(store, records, *, first: list[bool], second: list[bool | None]) -> None:
    """A registered judge that judged every case of a run, and a review queue of the run's cases
    asking the question named like it: reviewer a answered every case, and b, after a in each
    chain, those ``second`` gives an answer for."""
    principal = SINGLE_USER
    cases = len(first)
    await store.create_project(principal, Project(id=DEFAULT_PROJECT, name="Default", created_at=T0))
    await store.register_scorer(principal, DEFAULT_PROJECT, JUDGE.model_copy(update={"criteria": DEFINITION}))
    run = Run(id=RUN, project_id=DEFAULT_PROJECT, code="EV-1", source="console", state="completed", created_at=T0)
    await records.create_run(principal, run)
    await store.start_run(principal, run)
    results = []
    for i in range(cases):
        await store.record_trace(
            principal,
            Trace(id=tid(i), project_id=DEFAULT_PROJECT, name=f"c{i}", state="ok", request_time=T0, run_id=RUN),
        )
        results.append(
            CaseResult(run_id=RUN, case_id=f"c{i}", position=i, status="passed", store=StoreRef(trace_id=tid(i)))
        )
    await records.put_results(principal, DEFAULT_PROJECT, results)
    verdicts = [
        Score(run_id=RUN, case_id=f"c{i}", trace_id=tid(i), scorer="polite_tone", scorer_version="1",
              source="llm_judge", source_id="openai:/judge", value=True, passed=True, created_at=T0)
        for i in range(cases)
    ]  # fmt: skip
    await records.put_scores(principal, DEFAULT_PROJECT, verdicts)
    asked = ReviewQueue(id=QUEUE, run_id=RUN, name="Tone, twice", questions=[QUESTION],
                        case_ids=[f"c{i}" for i in range(cases)], created_by="creator", created_at=T0)  # fmt: skip
    await store.create_queue(principal, DEFAULT_PROJECT, asked, [tid(i) for i in range(cases)])
    for i in range(cases):
        chain = [Review(id=f"pending-{i}", run_id=RUN, case_id=f"c{i}", criterion="polite_tone", state="pending",
                        reviewer="creator", queue_id=QUEUE, created_at=T0)]  # fmt: skip
        for minutes, (reviewer, value) in enumerate((("a", first[i]), ("b", second[i])), start=1):
            if value is not None:
                chain.append(Review(id=f"{reviewer}-{i}", run_id=RUN, case_id=f"c{i}", criterion="polite_tone",
                                    state="complete", value=value, reviewer=reviewer, queue_id=QUEUE,
                                    previous_id=chain[-1].id, created_at=T0 + timedelta(minutes=minutes)))  # fmt: skip
        for review in chain:
            await records.add_review(principal, DEFAULT_PROJECT, review)


@pytest.fixture(params=["memory", "mlflow"])
def stores(request, tmp_path):
    store = MemoryStore() if request.param == "memory" else MlflowStore(f"sqlite:///{tmp_path / 'mlflow.db'}", tmp_path)
    records = SqlRecordStore(f"sqlite:///{tmp_path / 'martian.db'}")
    yield store, records
    records.close()


def client_for(store, records, entitlements=ENTERPRISE) -> TestClient:
    return TestClient(
        create_app(store=store, records=records, principal=lambda: SINGLE_USER, entitlements=lambda _p: entitlements)
    )


def test_the_route_reports_each_queue_and_question_and_the_judge_report_shows_it_as_the_ceiling(stores):
    store, records = stores
    # 32 cases both answered, 26 alike; b did not answer the last two.
    first = [i % 4 != 0 for i in range(34)]
    second: list[bool | None] = [value if i % 8 else not value for i, value in enumerate(first)]
    second[1] = not second[1]
    second[32:] = [None, None]
    asyncio.run(seed(store, records, first=first, second=second))
    client = client_for(store, records)
    found = client.get("/v1/evals/reviewer-agreement").json()
    expected = compare_reviewers(two(first, second), "boolean", question="polite_tone").as_dict()
    assert (expected["shared"], expected["answered"], expected["status"], expected["method"]) == (
        32,
        34,
        "measured",
        "cohen",
    )
    assert (found["project_id"], found["minimum"]) == (None, MINIMUM_LABELLED)
    assert found["queues"] == [{"queue_id": QUEUE, "name": "Tone, twice", "run_id": RUN, "questions": [expected]}]
    assert found["questions"] == [expected]
    assert client.get("/v1/evals/reviewer-agreement", params={"queue_id": QUEUE}).json() == found
    assert client.get("/v1/evals/reviewer-agreement", params={"queue_id": str(uuid.uuid4())}).status_code == 404
    assert client.get("/v1/evals/reviewer-agreement", params={"project_id": str(uuid.uuid4())}).status_code == 404
    # The judge's labels are each chain's current review: b's answer, else a's. Beside them, the
    # reviewers' agreement on the question named like the judge.
    judges = client.get("/v1/evals/judge-agreement").json()
    (item,) = judges["items"]
    assert (item["judge"], item["version"], item["labelled"]) == ("polite_tone", "1", 34)
    assert item["agreed"] == sum(1 for a, b in zip(first, second, strict=True) if (a if b is None else b))
    assert judges["reviewer_agreement"] == [expected]
    assert client.get("/v1/evals/judge-agreement", params={"name": "concise"}).json()["reviewer_agreement"] == []
    # The reviewer route reports on review queues, so it needs that feature.
    community = create_app(store=store, records=records, principal=lambda: SINGLE_USER)
    assert TestClient(community).get("/v1/evals/reviewer-agreement").status_code == 403


def test_a_project_with_nothing_yet_reports_nothing(tmp_path):
    records = SqlRecordStore(f"sqlite:///{tmp_path / 'martian.db'}")
    client = client_for(MemoryStore(), records)
    assert client.get("/v1/evals/reviewer-agreement").json() == {
        "project_id": None,
        "minimum": MINIMUM_LABELLED,
        "queues": [],
        "questions": [],
    }
    assert client.get("/v1/evals/judge-agreement").json()["reviewer_agreement"] == []
    assert client.get("/v1/evals/reviewer-agreement", params={"queue_id": QUEUE}).status_code == 404
    records.close()


ADMIN = organisation_principal("admin-1", "org-a", frozenset({"admin"}))
MEMBER = organisation_principal(str(uuid.UUID(int=21)), "org-a", frozenset({"member"}))


@pytest.fixture
def memory(tmp_path):
    records = SqlRecordStore(f"sqlite:///{tmp_path / 'martian.db'}")
    client, served = serve(MemoryStore(), records, entitlements=lambda _p: ENTERPRISE, principal=ADMIN)
    with client:
        yield served
    records.close()


def test_two_people_answering_a_queue_through_the_review_route_are_compared(memory: Served):
    run = memory.completed_run()
    items = {item["dataset_item_id"]: item["id"] for item in run["items"]}
    chosen = [items["faq-1"], items["faq-2"]]
    question = QUESTION.model_dump()
    body = {"request_id": request_id(), "run_id": run["id"], "name": "Twice", "item_ids": chosen, "question": question}
    queue_id = memory.call("POST", "/v1/evals/review-queues", 201, json=body)["id"]
    path = f"/v1/evals/{run['id']}/items/{{}}/reviews"

    def give(item: str, value: bool) -> None:
        # Each answer follows the chain's current review, as the review drawer sends it.
        listed = memory.call("GET", "/v1/evals/reviews", 200, params={"queue_id": queue_id})["items"]
        head = next(r for r in listed if r["item_id"] == item)
        answer = {"request_id": request_id(), "state": "complete", "queue_id": queue_id, "previous_id": head["id"],
                  "comment": "Checked.", "value": value}  # fmt: skip
        memory.call("POST", path.format(item), 201, json=answer)

    give(chosen[0], True)
    give(chosen[1], True)
    memory.act_as(MEMBER)
    give(chosen[0], True)
    give(chosen[1], False)
    # The second answer replaced the first in each chain, and both are compared.
    current = memory.call("GET", "/v1/evals/reviews", 200, params={"queue_id": queue_id, "filter": "complete"})
    assert {r["value"] for r in current["items"]} == {True, False} and current["total"] == 2
    found = memory.call("GET", "/v1/evals/reviewer-agreement", 200, params={"queue_id": queue_id})
    (tone,) = found["queues"][0]["questions"]
    assert (tone["answered"], tone["shared"], tone["reviewers"], tone["agreed"]) == (2, 2, 2, 1)
    assert (tone["status"], tone["method"], tone["agreement"]) == ("too_few", None, None)
