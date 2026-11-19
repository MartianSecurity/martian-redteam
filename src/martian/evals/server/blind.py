"""Blind double review: review queues in which each reviewer answers an item without reading the
other reviewers' answers to it until they have given their own (MartianSecurity/martian-notes#357).

When people see each other's answers their agreement goes up for the wrong reason, and the
reviewers' agreement (:mod:`martian.evals.agreement`), which bounds what a judge can be expected to
reach, would overstate how well people agree. So:

- **A queue is blind from the start.** ``POST /v1/evals/blind-review-queues``, the core's own
  route, creates a queue as ``POST /v1/evals/review-queues`` does and records it in the record
  store (:class:`~martian.evals.records.BlindQueue`) before the queue exists, so no answer is ever
  given in it unblinded. The record store keeps it, not the queue in the store, so every store
  binding has it.
- **Hidden until answered.** An item of a blind queue is a case result and one of its questions.
  A reader who has no complete answer of their own to it reads none of the other reviewers'
  reviews of it, only its chain's first review, the pending one the queue added, and their own
  (:func:`hidden_items`, :func:`visible_reviews`). That holds on every route that returns
  reviews or the human scores they leave: the reviews list, a run's workbench (its review history,
  current reviews and assessments), a conversation's turns, agreement reports, and a judge
  calibration's labelled items. Once they have answered, they read the others' answers too.
- **One chain still.** An item keeps one chain of reviews, so the current review is still its
  newest answer. A reader answering after the review they were shown has their answer added
  after the chain's newest review, which they may not read; the request is checked against what
  they were shown.
- **Not on the trace.** A blind queue's answers stay in the record store: they are not copied to
  the case's trace as feedback, where every trace read and search, an export of traces and the
  MLflow UI would show them to anyone.
- **Given blind.** A reviewer's first complete answer to an item of a blind queue is recorded
  with ``Review.blind``: given before they could read anyone else's. Their later answers are not.
  The reviewers' agreement of a blind queue compares the answers given blind.

A queue's progress still counts who has answered how many items. Agreement reports use only
answers the reader can see.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from martian.evals.model import Review, ReviewQueue, Score
from martian.evals.records import RecordStore
from martian.evals.server.principal import Principal
from martian.evals.server.projection import queue_out

Item = tuple[str, str]
"""An item of a review queue: a case result's case id and a question's name (a review's criterion)."""


async def blind_queue_ids(
    records: RecordStore, principal: Principal, project_id: str, run_id: str | None = None
) -> set[str]:
    """The ids of the project's review queues with blind double review, or of one run's."""
    return {queue.id for queue in await records.list_blind_queues(principal, project_id, run_id=run_id)}


def hidden_items(reviews: Iterable[Review], blind: set[str], user_id: str) -> set[Item]:
    """The items of blind queues, among the reviews of one run, that ``user_id`` has given no
    complete answer to: those whose other reviews they may not read."""
    return {(case, criterion) for _, case, criterion in hidden_project_items(reviews, blind, user_id)}


def hidden_project_items(reviews: Iterable[Review], blind: set[str], user_id: str) -> set[tuple[str, str, str]]:
    """Unreadable answers for a batch of review chains, qualified by run, case and question."""
    reviews = list(reviews)
    asked = {(r.run_id, r.case_id, r.criterion) for r in reviews if r.queue_id in blind}
    answered = {(r.run_id, r.case_id, r.criterion) for r in reviews if r.reviewer == user_id and r.state == "complete"}
    return asked - answered


def visible_reviews(reviews: Iterable[Review], hidden: set[Item], user_id: str) -> list[Review]:
    """The reviews of one run that ``user_id`` may read: all of an item they may read, and of a
    hidden item only its chain's first review and their own."""
    return [
        r for r in reviews if (r.case_id, r.criterion) not in hidden or r.reviewer == user_id or r.previous_id is None
    ]


def visible_scores(scores: Iterable[Score], hidden: set[Item]) -> list[Score]:
    """The scores of one run without the human scores of hidden items, which are their answers."""
    return [s for s in scores if not (s.source == "human" and (s.case_id or "", s.scorer) in hidden)]


def queue_with(queue: ReviewQueue, blind: set[str]) -> dict[str, Any]:
    """A review queue as the routes answer it: the contract's ``public_queue`` and whether it has
    blind double review, given the ids of the blind queues."""
    return {**queue_out(queue), "blind": queue.id in blind}


async def hidden_of_run(
    records: RecordStore, principal: Principal, project_id: str, run_id: str, blind: set[str] | None = None
) -> set[Item]:
    """The items of one run's blind queues that the principal has not answered. ``blind`` is the
    ids of the blind queues, read when it is not given."""
    if blind is None:
        blind = await blind_queue_ids(records, principal, project_id, run_id)
    if not blind:
        return set()
    reviews = await records.list_reviews(principal, project_id, run_id, current_only=False)
    return hidden_items(reviews, blind, principal.user_id)


async def hidden_of_runs(
    records: RecordStore, principal: Principal, project_id: str, run_ids: Iterable[str] | None = None
) -> set[tuple[str, str, str]]:
    """The items of the given runs' blind queues that the principal has not answered, each as its
    run id, case id and question."""
    by_run: dict[str, set[str]] = {}
    for queue in await records.list_blind_queues(principal, project_id):
        by_run.setdefault(queue.run_id, set()).add(queue.id)
    found: set[tuple[str, str, str]] = set()
    relevant = set(by_run) if run_ids is None else set(by_run) & set(run_ids)
    for run_id in sorted(relevant):
        hidden = await hidden_of_run(records, principal, project_id, run_id, by_run[run_id])
        found |= {(run_id, case, question) for case, question in hidden}
    return found


__all__ = [
    "Item",
    "blind_queue_ids",
    "hidden_items",
    "hidden_project_items",
    "hidden_of_run",
    "hidden_of_runs",
    "queue_with",
    "visible_reviews",
    "visible_scores",
]
