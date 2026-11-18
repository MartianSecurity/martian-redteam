"""Blind double review: a review queue in which each reviewer answers an item without seeing the
other reviewers' answers to it until they have given their own, the answers given blind, and the
reviewers' agreement on them (martian-notes#357). Two people answer through the routes; no model is
called."""

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from martian.evals.agreement import question_agreement, queue_agreement
from martian.evals.model import Review, ReviewQuestion, ReviewQueue
from martian.evals.records import BlindQueue, LabelledItem
from martian.evals.records.sql import SqlRecordStore
from martian.evals.server import DEFAULT_PROJECT, ENTERPRISE, organisation_principal
from martian.evals.server.blind import hidden_items, visible_reviews
from martian.evals.store import MemoryStore
from tests.evals.write_fixture import Served, request_id, serve

ADMIN = organisation_principal("admin-1", "org-a", frozenset({"admin"}))
MEMBER = organisation_principal(str(uuid.UUID(int=21)), "org-a", frozenset({"member"}))
QUESTION = ReviewQuestion(name="polite_tone", kind="boolean", instruction="Is the reply polite?")
T0 = datetime(2026, 11, 15, 9, 0, tzinfo=UTC)


@pytest.fixture
def served(tmp_path):
    records = SqlRecordStore(f"sqlite:///{tmp_path / 'martian.db'}")
    client, found = serve(MemoryStore(), records, entitlements=lambda _p: ENTERPRISE, principal=ADMIN)
    with client:
        yield found
    records.close()


class Queue:
    """A review queue of a completed run's first two results, and its answers through the routes."""

    def __init__(self, served: Served, *, blind: bool) -> None:
        self.served = served
        self.run = served.completed_run()
        items = {item["dataset_item_id"]: item["id"] for item in self.run["items"]}
        self.items = [items["faq-1"], items["faq-2"]]
        name = "Tone, blind" if blind else "Tone"
        self.body = {"request_id": request_id(), "run_id": self.run["id"], "name": name,
                     "item_ids": self.items, "question": QUESTION.model_dump()}  # fmt: skip
        route = "/v1/evals/blind-review-queues" if blind else "/v1/evals/review-queues"
        self.queue = served.call("POST", route, 201, json=self.body)
        self.id = self.queue["id"]

    def head(self, item: str) -> dict:
        """The review the review drawer shows for an item: the current one it may read."""
        rows = self.served.call("GET", "/v1/evals/reviews", 200, params={"queue_id": self.id})["items"]
        return next(row for row in rows if row["item_id"] == item)

    def give(self, item: str, value: bool, comment: str, *, request: str | None = None) -> dict:
        """Answer after the review the drawer shows, as it sends the answer."""
        answer = {"request_id": request or request_id(), "state": "complete", "queue_id": self.id,
                  "previous_id": self.head(item)["id"], "comment": comment, "value": value}  # fmt: skip
        return self.served.call("POST", f"/v1/evals/{self.run['id']}/items/{item}/reviews", 201, json=answer)

    def readable(self, item: str) -> str:
        """Everything a person can read about the run and the item, as one text."""
        run = self.run["id"]
        reads = [
            ("/v1/evals/reviews", {}),
            ("/v1/evals/reviews", {"queue_id": self.id}),
            ("/v1/evals/reviews", {"queue_id": self.id, "filter": "complete"}),
            (f"/v1/evals/{run}/workbench", {}),
            (f"/v1/evals/{run}/workbench", {"item_id": item}),
            (f"/v1/evals/{run}/items/{item}/trace", {}),
            (f"/v1/evals/{run}", {}),
        ]
        return json.dumps([self.served.call("GET", path, 200, params=params) for path, params in reads])


def test_a_second_reviewer_cannot_read_the_first_answer_in_a_blind_queue_before_giving_their_own(served: Served):
    queue = Queue(served, blind=True)
    assert queue.queue["blind"] is True
    # The same request is the same queue; the contract's route takes it for another request.
    assert served.call("POST", "/v1/evals/blind-review-queues", 201, json=queue.body)["id"] == queue.id
    served.call("POST", "/v1/evals/review-queues", 409, json=queue.body)
    listed = served.call("GET", "/v1/evals/review-queues", 200)["items"]
    assert [(q["id"], q["blind"]) for q in listed] == [(queue.id, True)]
    workbench = served.call("GET", f"/v1/evals/{queue.run['id']}/workbench", 200)
    assert [(q["id"], q["blind"]) for q in workbench["queues"]] == [(queue.id, True)]

    first, other = queue.items
    given = queue.give(first, True, "Polite enough, says the admin.")
    assert given["blind"] is True
    served.act_as(MEMBER)
    # Nothing the member can read holds the admin's answer: the item shows its queue's pending
    # review, with no value, and neither the reviews, the workbench's history and assessments, the
    # item's trace nor the run holds the answer, its comment or its id.
    shown = queue.head(first)
    assert (shown["state"], shown["value"], shown["comment"], shown["previous_id"]) == ("pending", None, "", None)
    text = queue.readable(first)
    assert "Polite enough" not in text and given["id"] not in text
    assessments = served.call("GET", f"/v1/evals/{queue.run['id']}/workbench", 200, params={"item_id": first})
    assert [a for a in assessments["assessments"] if a["source"] == "HUMAN"] == []
    # The member answers after the review they were shown; the answer follows the admin's in the
    # item's one chain, and was given blind.
    request = request_id()
    answered = queue.give(first, False, "Too curt.", request=request)
    assert (answered["blind"], answered["previous_id"], answered["value"]) == (True, given["id"], False)
    # The same request again is the same answer, though the review it named is not the one it follows.
    again = {"request_id": request, "state": "complete", "queue_id": queue.id, "previous_id": shown["id"],
             "comment": "Too curt.", "value": False}  # fmt: skip
    assert served.call("POST", f"/v1/evals/{queue.run['id']}/items/{first}/reviews", 201, json=again) == answered
    # Having answered, the member reads the admin's answer too, but not on the other item.
    history = served.call("GET", f"/v1/evals/{queue.run['id']}/workbench", 200, params={"item_id": first})["reviews"]
    assert {(r["id"], r["blind"]) for r in history if r["state"] == "complete"} == {
        (given["id"], True),
        (answered["id"], True),
    }
    served.act_as(ADMIN)
    queue.give(other, True, "The second, says the admin.")
    served.act_as(MEMBER)
    assert "The second, says the admin" not in queue.readable(other)

    # The admin answered first and reads the member's answer. A second answer of their own, given
    # after reading it, is not blind.
    served.act_as(ADMIN)
    assert queue.head(first)["id"] == answered["id"]
    revised = queue.give(first, False, "On reflection, curt.")
    assert (revised["blind"], revised["previous_id"]) == (False, answered["id"])
    # No answer of a blind queue is copied to its case's trace, where any trace read would show it.
    trace = served.call("GET", f"/v1/evals/{queue.run['id']}/items/{first}/trace", 200)
    assert not any(word in json.dumps(trace) for word in ("Polite enough", "Too curt", "On reflection"))


def test_an_answer_in_a_queue_that_is_not_blind_is_read_by_the_next_reviewer_and_not_given_blind(served: Served):
    queue = Queue(served, blind=False)
    assert queue.queue["blind"] is False
    first = queue.items[0]
    given = queue.give(first, True, "Polite enough, says the admin.")
    assert given["blind"] is False
    served.act_as(MEMBER)
    assert queue.head(first)["id"] == given["id"]
    assert queue.give(first, False, "Too curt.")["blind"] is False
    # Its answers are copied to the case's trace, as before.
    trace = served.call("GET", f"/v1/evals/{queue.run['id']}/items/{first}/trace", 200)
    assert "Too curt" in json.dumps(trace)
    served.call("POST", "/v1/evals/blind-review-queues", 409, json=queue.body)


def test_reviewer_agreement_rests_on_the_answers_given_blind_and_says_so(served: Served):
    blind, open_ = Queue(served, blind=True), Queue(served, blind=False)
    for queue, (admin, member) in ((blind, ((True, True), (True, False))), (open_, ((False, True), (False, True)))):
        for item, value in zip(queue.items, admin, strict=True):
            queue.give(item, value, "Admin.")
        served.act_as(MEMBER)
        for item, value in zip(queue.items, member, strict=True):
            queue.give(item, value, "Member.")
        served.act_as(ADMIN)
    # The admin changes their answer on the second item after reading the member's: the blind
    # report keeps the answer they gave blind, so the two still disagree there.
    blind.give(blind.items[1], False, "Admin, again.")
    report = served.call("GET", "/v1/evals/reviewer-agreement", 200)
    found = {q["queue_id"]: q["questions"][0] for q in report["queues"]}
    assert (found[blind.id]["blind"], found[blind.id]["shared"], found[blind.id]["agreed"]) == (True, 2, 1)
    assert (found[open_.id]["blind"], found[open_.id]["shared"], found[open_.id]["agreed"]) == (False, 2, 2)
    # Across queues, a question a blind queue asks rests on the blind queues' blind answers only.
    (tone,) = report["questions"]
    assert (tone["question"], tone["blind"], tone["shared"], tone["agreed"]) == ("polite_tone", True, 2, 1)


def test_a_calibration_shows_no_label_of_a_blind_item_its_reader_has_not_answered(served: Served):
    queue = Queue(served, blind=True)
    first = queue.items[0]
    queue.give(first, False, "Admin.")
    case = next(item["dataset_item_id"] for item in queue.run["items"] if item["id"] == first)
    from martian.evals.records import JudgeCalibration

    calibration = JudgeCalibration(
        id=str(uuid.uuid4()), project_id=DEFAULT_PROJECT, judge="polite_tone", version="1", labelled=2,
        items=[LabelledItem(run_id=queue.run["id"], case_id=case, label=False, verdict=True),
               LabelledItem(trace_id="tr-" + "1" * 32, label=True, verdict=True)],
        report={"status": "too_few"}, created_at=T0,
    )  # fmt: skip
    asyncio.run(served.records.save_calibration(ADMIN, DEFAULT_PROJECT, calibration))
    path = f"/v1/evals/judge-calibrations/{calibration.id}"
    assert [i["label"] for i in served.call("GET", path, 200)["items"]] == [False, True]
    served.act_as(MEMBER)
    assert [i["label"] for i in served.call("GET", path, 200)["items"]] == [None, True]
    queue.give(first, True, "Member.")
    assert [i["label"] for i in served.call("GET", path, 200)["items"]] == [False, True]


# --- the rules, without the routes -------------------------------------------------------------

RUN, BLIND, OPEN = "run-1", "queue-blind", "queue-open"


def link(id: str, case: str, reviewer: str, value, minutes: int, *, queue: str = BLIND, previous: str | None = None,
         blind: bool = False) -> Review:  # fmt: skip
    return Review(id=id, run_id=RUN, case_id=case, criterion="polite_tone", queue_id=queue, reviewer=reviewer,
                  state="pending" if value is None else "complete", value=value, previous_id=previous, blind=blind,
                  created_at=T0 + timedelta(minutes=minutes))  # fmt: skip


CHAIN = [
    link("root", "c1", "creator", None, 0),
    link("a1", "c1", "a", True, 1, previous="root", blind=True),
    link("b1", "c1", "b", False, 2, previous="a1", blind=True),
    link("a2", "c1", "a", False, 3, previous="b1"),
]


def test_a_reviewer_who_has_not_answered_an_item_of_a_blind_queue_reads_only_its_pending_review_and_their_own():
    hidden = hidden_items(CHAIN, {BLIND}, "c")
    assert hidden == {("c1", "polite_tone")}
    assert [r.id for r in visible_reviews(CHAIN, hidden, "c")] == ["root"]
    assert hidden_items(CHAIN, {BLIND}, "a") == set() and hidden_items(CHAIN, set(), "c") == set()
    # A pending review of their own does not answer the item.
    mine = [*CHAIN, link("c0", "c1", "c", None, 4, previous="a2")]
    assert [r.id for r in visible_reviews(mine, hidden_items(mine, {BLIND}, "c"), "c")] == ["root", "c0"]


def test_the_agreement_of_a_blind_queue_counts_each_reviewers_blind_answer():
    asked = [
        ReviewQueue(id=BLIND, run_id=RUN, name="Blind", questions=[QUESTION], case_ids=["c1"], created_at=T0),
        ReviewQueue(id=OPEN, run_id=RUN, name="Open", questions=[QUESTION], case_ids=["c2"], created_at=T0),
    ]
    opened = [link("o1", "c2", "a", True, 1, queue=OPEN), link("o2", "c2", "b", True, 2, queue=OPEN, previous="o1")]
    reviews = [*CHAIN, *opened]
    by_queue = {q.queue_id: q.questions[0] for q in queue_agreement(reviews, asked, blind={BLIND})}
    # a's blind answer is True, though their newest is False: they disagree with b.
    assert (by_queue[BLIND].blind, by_queue[BLIND].shared, by_queue[BLIND].agreed) == (True, 1, 0)
    assert (by_queue[OPEN].blind, by_queue[OPEN].shared, by_queue[OPEN].agreed) == (False, 1, 1)
    (across,) = question_agreement(reviews, asked, blind={BLIND})
    assert (across.blind, across.shared, across.agreed) == (True, 1, 0)
    (unblinded,) = question_agreement(reviews, asked)
    assert (unblinded.blind, unblinded.shared, unblinded.agreed) == (False, 2, 2)
    assert unblinded.as_dict()["blind"] is False


def test_a_blind_queue_is_a_record_of_its_own():
    marked = BlindQueue(id=BLIND, run_id=RUN, created_by="a", created_at=T0)
    assert marked.model_dump()["id"] == BLIND
    with pytest.raises(ValueError):
        BlindQueue(id=BLIND, run_id=RUN, extra=True)  # type: ignore[call-arg]
