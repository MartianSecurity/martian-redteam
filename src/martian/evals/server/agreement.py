"""The judges' agreement with people: ``GET /v1/evals/judge-agreement``, the core's own route, which
the server's contract does not have.

For each judge version of a project it reports how the judge's verdicts agree with the labels
people gave the same items (:func:`martian.evals.agreement.judge_agreement`):

- **Verdicts** are the judge scores of every run of the project in the record store (a scoring
  job's, and a suite-file run's own), and the judge scores on the project's traces that are on no
  case result's trace and name no case result, such as an application's judges on live traffic.
  A case result's judge verdict is its score in the record store, so the copy each scored value
  also leaves on the case's trace is not read again.
- **Labels** are the complete current reviews of every run (a review's criterion is what it
  labels; ``human_review`` labels the case as a whole, so it pairs with no judge), and the human
  feedback on the project's traces: trace annotations, and feedback an application recorded. The
  copy of a review's answer on the case's trace is the review's, so it is not read again, and an
  expectation is what an output should hold, not a verdict on it, so it is no label.
- **Judge versions** are each scorer name and version among the verdicts, and every version of each
  judge registered in the project, so a registered version that never scored anything is reported
  with no items. A registered version's kind is its definition's.

Traces are read newest first, at most ``TRACE_LIMIT`` of them (``traces_examined``);
``traces_truncated`` says there were more, whose labels and verdicts are left out. Runs, their
scores and their reviews are read in full.

Route functions have no docstrings: FastAPI would publish one as the operation's description.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query

from martian.evals.agreement import KINDS, MINIMUM_LABELLED, JudgeAgreement, Kind, judge_agreement
from martian.evals.model import Review, Score, TraceQuery
from martian.evals.records import RecordStore
from martian.evals.server.dependencies import PrincipalDep, RecordsDep, StoreDep
from martian.evals.server.principal import Principal
from martian.evals.server.projection import DEFAULT_PROJECT, project_key, project_out
from martian.evals.server.runs import project_runs, run_results
from martian.evals.server.suites import is_judge
from martian.evals.server.traces import READ_LIMIT
from martian.evals.store import EvalStore

explorer = APIRouter(prefix="/v1/evals", tags=["evaluation explorer"])

TRACE_LIMIT = READ_LIMIT
"""The most traces one report reads, newest first."""

_TRACE_PAGE = 500
REVIEW_ECHO = "martian.review_criterion"
"""The metadata key of the human score a complete review leaves on its case's trace."""
ANNOTATION_KIND = "martian.annotation"


def is_label(score: Score) -> bool:
    """Whether a score on a trace is a person's verdict on it that no review already holds."""
    return (
        score.source == "human"
        and REVIEW_ECHO not in score.metadata
        and score.metadata.get(ANNOTATION_KIND) != "expectation"
    )


async def _from_records(
    records: RecordStore, principal: Principal, key: str
) -> tuple[list[Score], list[Review], dict[str, tuple[str, str]]]:
    """Every run's judge scores and complete current reviews, and each case result's trace."""
    verdicts: list[Score] = []
    reviews: list[Review] = []
    case_traces: dict[str, tuple[str, str]] = {}
    for run in await project_runs(records, principal, key):
        for result in await run_results(records, principal, run):
            if result.store.trace_id:
                case_traces[result.store.trace_id] = (run.id, result.case_id)
        verdicts += [s for s in await records.list_scores(principal, key, run.id) if s.source == "llm_judge"]
        reviews += [r for r in await records.list_reviews(principal, key, run.id) if r.state == "complete"]
    return verdicts, reviews, case_traces


async def _from_traces(
    store: EvalStore, principal: Principal, key: str, case_traces: dict[str, tuple[str, str]]
) -> tuple[list[Score], list[Score], int, bool]:
    """The newest traces' judge scores outside case results and people's labels, how many traces
    were read and whether there were more."""
    verdicts: list[Score] = []
    labels: list[Score] = []
    examined, cursor = 0, None
    while examined < TRACE_LIMIT:
        query = TraceQuery(project_id=key, limit=min(_TRACE_PAGE, TRACE_LIMIT - examined), cursor=cursor)
        page = await store.search_traces(principal, query)
        examined += len(page.items)
        for trace in page.items:
            for score in trace.scores:
                if is_label(score):
                    labels.append(score)
                elif (
                    score.source == "llm_judge" and trace.id not in case_traces and not (score.run_id and score.case_id)
                ):
                    verdicts.append(score)
        if (cursor := page.next_cursor) is None:
            break
    return verdicts, labels, examined, cursor is not None


async def _registered(
    store: EvalStore, principal: Principal, key: str, name: str | None
) -> dict[tuple[str, str], Kind]:
    """Every version of each judge registered in the project, with its kind."""
    latest = await store.list_scorers(principal, key)
    names = sorted({s.name for s in latest if is_judge(s) and (name is None or s.name == name)})
    found: dict[tuple[str, str], Kind] = {}
    for each in names:
        for version in await store.scorer_versions(principal, key, each):
            kind = version.criteria.get("kind")
            if is_judge(version) and kind in KINDS:
                found[(version.name, version.version)] = kind
    return found


async def agreement_reports(
    store: EvalStore, records: RecordStore, principal: Principal, key: str, name: str | None = None
) -> tuple[list[JudgeAgreement], int, bool]:
    """Each judge version's report of a project that exists, by its store id, or of one judge's
    versions, with the items each compared, and how many traces were read and whether there were
    more."""
    verdicts, reviews, case_traces = await _from_records(records, principal, key)
    traced, annotations, examined, truncated = await _from_traces(store, principal, key, case_traces)
    kinds = await _registered(store, principal, key, name)
    found = [v for v in [*verdicts, *traced] if name is None or v.scorer == name]
    reports = judge_agreement(found, [*reviews, *annotations], case_traces=case_traces, kinds=kinds, versions=kinds)
    return reports, examined, truncated


async def project_agreement(
    store: EvalStore, records: RecordStore, principal: Principal, key: str, name: str | None = None
) -> dict[str, Any]:
    """The agreement report of a project that exists, by its store id, or of one judge's versions,
    as the route answers it. A judge version's calibration is its item of this report
    (``martian.evals.server.calibration``)."""
    reports, examined, truncated = await agreement_reports(store, records, principal, key, name)
    return {
        "project_id": project_out(key),
        "minimum": MINIMUM_LABELLED,
        "traces_examined": examined,
        "traces_truncated": truncated,
        "items": [r.as_dict() for r in reports],
    }


@explorer.get("/judge-agreement", response_model=None)
async def judge_agreement_report(
    principal: PrincipalDep,
    store: StoreDep,
    records: RecordsDep,
    project_id: uuid.UUID | None = None,
    name: Annotated[str | None, Query(pattern=r"^[a-z][a-z0-9_]{0,59}$")] = None,
) -> dict[str, Any]:
    key = project_key(project_id)
    if await store.get_project(principal, key) is None:
        if key != DEFAULT_PROJECT:
            raise HTTPException(404, "Not found")
        return {
            "project_id": project_out(key),
            "minimum": MINIMUM_LABELLED,
            "traces_examined": 0,
            "traces_truncated": False,
            "items": [],
        }
    return await project_agreement(store, records, principal, key, name)


__all__ = ["TRACE_LIMIT", "agreement_reports", "explorer", "is_label", "project_agreement"]
