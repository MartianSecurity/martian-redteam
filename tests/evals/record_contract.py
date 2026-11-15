"""The behaviour every ``RecordStore`` must have. A test module subclasses ``RecordContract`` and
provides a ``records`` fixture."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from martian.evals.model import (
    Case,
    CaseResult,
    Counts,
    DatasetDraft,
    Review,
    Run,
    Score,
    ScorerVersion,
    ScoringJob,
    Suite,
    digest,
)
from martian.evals.records import EventsLost, Monitor, MonitoredTrace, MonitorProgress, RecordStore, SavedView
from martian.evals.server.principal import SINGLE_USER, organisation_principal
from martian.evals.store import StoreError

ORG_A = organisation_principal("user-a", "org-a", frozenset({"admin"}))
ORG_B = organisation_principal("user-b", "org-b", frozenset({"admin"}))
T0 = datetime(2026, 11, 15, 9, 0, tzinfo=UTC)
JUDGE = ScorerVersion(name="rubric_judge", kind="llm_judge", type="rubric", model="openai:/judge")


def monitor(monitor_id: str, *, name: str = "Safety") -> Monitor:
    return Monitor(id=monitor_id, project_id="p1", name=name, scorers=[JUDGE], sampling_rate=0.5)


def run(run_id: str, *, minutes: int = 0, state: str = "running") -> Run:
    return Run.model_validate(
        {
            "id": run_id,
            "project_id": "p1",
            "source": "local",
            "state": state,
            "created_at": T0 + timedelta(minutes=minutes),
        }
    )


async def refused(kind: str, awaitable: object) -> None:
    with pytest.raises(StoreError) as caught:
        await awaitable  # type: ignore[misc]
    assert caught.value.kind == kind


class RecordContract:
    async def test_runs_are_created_once_listed_newest_first_and_paged(self, records: RecordStore) -> None:
        first = await records.create_run(ORG_A, run("r1", minutes=0))
        assert await records.create_run(ORG_A, run("r1", minutes=99)) == first
        await records.create_run(ORG_A, run("r2", minutes=1, state="completed"))
        await records.create_run(ORG_A, run("r3", minutes=2))
        page = await records.list_runs(ORG_A, "p1", limit=2)
        assert [r.id for r in page.items] == ["r3", "r2"] and page.next_cursor
        rest = await records.list_runs(ORG_A, "p1", limit=2, cursor=page.next_cursor)
        assert [r.id for r in rest.items] == ["r1"] and rest.next_cursor is None
        assert [r.id for r in (await records.list_runs(ORG_A, "p1", states=["running"])).items] == ["r3", "r1"]
        assert await records.get_run(ORG_A, "p1", "r9") is None

    async def test_a_run_moves_on_only_from_the_state_it_was_expected_in(self, records: RecordStore) -> None:
        started = await records.create_run(ORG_A, run("r1"))
        done = started.model_copy(update={"state": "completed", "counts": Counts(cases=2, passed=2)})
        saved = await records.update_run(ORG_A, done, expected_state="running")
        assert saved.state == "completed" and saved.created_at == started.created_at
        await refused(
            "conflict", records.update_run(ORG_A, done.model_copy(update={"state": "failed"}), expected_state="running")
        )
        assert (await records.get_run(ORG_A, "p1", "r1")).state == "completed"
        await refused("not_found", records.update_run(ORG_A, run("r9")))

    async def test_case_results_are_kept_in_case_order_and_replaced_per_case(self, records: RecordStore) -> None:
        await records.create_run(ORG_A, run("r1"))
        results = [
            CaseResult(run_id="r1", case_id=f"c{i}", position=i, status="passed", inputs={"m": str(i)}, output="ok")
            for i in (2, 0, 1)
        ]
        await records.put_results(ORG_A, "p1", results)
        await records.put_results(ORG_A, "p1", [results[0].model_copy(update={"status": "failed", "error": "no"})])
        page = await records.list_results(ORG_A, "p1", "r1", limit=2)
        assert [r.case_id for r in page.items] == ["c0", "c1"] and page.next_cursor == "2"
        (last,) = (await records.list_results(ORG_A, "p1", "r1", offset=2)).items
        assert (last.case_id, last.status, last.error) == ("c2", "failed", "no")
        await refused(
            "not_found", records.put_results(ORG_A, "p1", [CaseResult(run_id="r9", case_id="c", status="passed")])
        )
        await refused("not_found", records.list_results(ORG_A, "p1", "r9"))

    async def test_a_case_result_and_its_scores_are_stored_together_or_not_at_all(self, records: RecordStore) -> None:
        await records.create_run(ORG_A, run("r1"))
        result = CaseResult(run_id="r1", case_id="c1", status="passed", output="ok")
        own = Score(run_id="r1", case_id="c1", scorer="exact_match", source="code", value=True, passed=True)
        listener = records.events.subscribe(ORG_A)
        try:
            await records.put_results(ORG_A, "p1", [result], scores=[own])
            got = [await listener.next(timeout=5), await listener.next(timeout=5)]
            assert [(e.kind, e.id) for e in got] == [("case_results", "r1"), ("scores", "r1")]
        finally:
            listener.close()
        assert [r.case_id for r in (await records.list_results(ORG_A, "p1", "r1")).items] == ["c1"]
        assert [s.scorer for s in await records.list_scores(ORG_A, "p1", "r1", case_id="c1")] == ["exact_match"]
        # A score the store refuses leaves the case without its result too.
        later = CaseResult(run_id="r1", case_id="c2", position=1, status="passed")
        await refused(
            "invalid",
            records.put_results(ORG_A, "p1", [later], scores=[Score(scorer="s", source="code", trace_id="t")]),
        )
        assert [r.case_id for r in (await records.list_results(ORG_A, "p1", "r1")).items] == ["c1"]

    async def test_active_runs_and_scoring_jobs_are_counted_across_projects_and_only_for_the_organisation(
        self, records: RecordStore
    ) -> None:
        for run_id, state in [("r1", "queued"), ("r2", "running"), ("r3", "completed"), ("r4", "failed")]:
            await records.create_run(ORG_A, run(run_id, state=state))
        other_project = run("r5", state="running").model_copy(update={"project_id": "p2"})
        await records.create_run(ORG_A, other_project)
        await records.create_run(ORG_B, run("b1", state="running"))
        assert await records.count_active_runs(ORG_A) == 3
        assert await records.count_active_runs(ORG_A, up_to=2) == 2
        assert await records.count_active_runs(ORG_B) == 1
        for job_id, state in [("j1", "queued"), ("j2", "running"), ("j3", "completed"), ("j4", "failed")]:
            await records.create_scoring_job(
                ORG_A, "p1", ScoringJob(id=job_id, run_id="r3", scorers=[JUDGE], state=state)
            )
        # A job of a run that has not completed is not one a worker can take.
        await records.create_scoring_job(ORG_A, "p1", ScoringJob(id="j5", run_id="r1", scorers=[JUDGE], state="queued"))
        await records.create_scoring_job(ORG_B, "p1", ScoringJob(id="bj", run_id="b1", scorers=[JUDGE], state="queued"))
        assert await records.count_active_scoring_jobs(ORG_A) == 2
        assert await records.count_active_scoring_jobs(ORG_A, up_to=1) == 1
        assert await records.count_active_scoring_jobs(ORG_B) == 0

    async def test_a_scoring_job_adds_scores_beside_the_runs_own(self, records: RecordStore) -> None:
        await records.create_run(ORG_A, run("r1"))
        own = Score(run_id="r1", case_id="c1", scorer="exact_match", source="code", value=True, passed=True)
        await records.put_scores(ORG_A, "p1", [own])
        job = await records.create_scoring_job(
            ORG_A, "p1", ScoringJob(id="j1", run_id="r1", scorers=[JUDGE], state="queued")
        )
        later = Score(
            run_id="r1",
            case_id="c1",
            scorer="exact_match",
            source="llm_judge",
            value=False,
            passed=False,
            scoring_job_id="j1",
        )
        await records.put_scores(ORG_A, "p1", [later])
        found = await records.list_scores(ORG_A, "p1", "r1", case_id="c1")
        assert [(s.scoring_job_id, s.passed) for s in found] == [(None, True), ("j1", False)]
        await records.put_scores(ORG_A, "p1", [own.model_copy(update={"passed": False, "value": False})])
        assert [s.passed for s in await records.list_scores(ORG_A, "p1", "r1")] == [False, False]
        await refused("invalid", records.put_scores(ORG_A, "p1", [Score(scorer="s", source="code", trace_id="t1")]))
        done = await records.update_scoring_job(
            ORG_A, "p1", job.model_copy(update={"state": "completed", "succeeded": 1}), expected_state="queued"
        )
        assert done.state == "completed" and done.created_at == job.created_at
        await refused("conflict", records.update_scoring_job(ORG_A, "p1", done, expected_state="queued"))
        assert [j.id for j in await records.list_scoring_jobs(ORG_A, "p1", "r1")] == ["j1"]
        await refused(
            "not_found",
            records.create_scoring_job(ORG_A, "p1", ScoringJob(id="j2", run_id="r9", scorers=[JUDGE], state="queued")),
        )

    async def test_review_chains_never_fork(self, records: RecordStore) -> None:
        await records.create_run(ORG_A, run("r1"))
        first = await records.add_review(
            ORG_A, "p1", Review(id="v1", run_id="r1", case_id="c1", state="complete", value=True, comment="ok")
        )
        second = await records.add_review(
            ORG_A,
            "p1",
            Review(id="v2", run_id="r1", case_id="c1", state="complete", value=False, comment="no", previous_id="v1"),
        )
        assert (
            await records.add_review(ORG_A, "p1", Review(id="v2", run_id="r1", case_id="c1", state="pending")) == second
        )
        fork = Review(id="v3", run_id="r1", case_id="c1", state="complete", value=True, comment="?", previous_id="v1")
        await refused("conflict", records.add_review(ORG_A, "p1", fork))
        other_case = Review(id="v4", run_id="r1", case_id="c2", state="pending", previous_id="v2")
        await refused("invalid", records.add_review(ORG_A, "p1", other_case))
        await refused(
            "not_found", records.add_review(ORG_A, "p1", fork.model_copy(update={"id": "v5", "previous_id": "v9"}))
        )
        assert [r.id for r in await records.list_reviews(ORG_A, "p1", "r1")] == ["v2"]
        assert [r.id for r in await records.list_reviews(ORG_A, "p1", "r1", current_only=False)] == ["v1", "v2"]
        assert first.created_at is not None

    async def test_every_suite_change_is_a_new_version(self, records: RecordStore) -> None:
        def suite(rubric: str, name: str = "Support") -> Suite:
            scorer = JUDGE.model_copy(update={"criteria": {"rubric": rubric}})
            return Suite(id="s1", name=name, scorers=[scorer], digest=digest({"rubric": rubric}))

        first = await records.save_suite(ORG_A, "p1", suite("Be kind."))
        assert first.version == 1
        assert first.created_by == "user-a" and first.created_at is not None
        assert await records.save_suite(ORG_A, "p1", suite("Be kind.")) == first
        second = await records.save_suite(ORG_A, "p1", suite("Be kind and brief."))
        assert second.version == 2
        assert await records.get_suite(ORG_A, "p1", "s1") == second
        assert await records.get_suite(ORG_A, "p1", "s1", version=1) == first
        assert await records.get_suite(ORG_A, "p1", "s9") is None
        await records.save_suite(ORG_A, "p1", suite("x", name="Another").model_copy(update={"id": "s0"}))
        assert [(s.id, s.version) for s in await records.list_suites(ORG_A, "p1")] == [("s0", 1), ("s1", 2)]

    async def test_a_draft_takes_edits_in_turn_and_none_after_publishing(self, records: RecordStore) -> None:
        draft = await records.create_draft(ORG_A, "p1", DatasetDraft(id="d1", project_id="p1", name="Refunds"))
        assert (draft.revision, draft.case_count, draft.created_by) == (1, 0, "user-a")
        cases = [Case(id=f"c{i}", inputs={"m": str(i)}) for i in range(3)]
        draft = await records.put_draft_cases(ORG_A, "p1", "d1", cases, expected_revision=1)
        assert (draft.revision, draft.case_count) == (2, 3)
        await refused("conflict", records.put_draft_cases(ORG_A, "p1", "d1", cases, expected_revision=1))
        draft = await records.put_draft_cases(
            ORG_A,
            "p1",
            "d1",
            [Case(id="c1", inputs={"m": "edited"}), Case(id="c9", inputs={"m": "new"})],
            expected_revision=2,
        )
        page = await records.draft_cases(ORG_A, "p1", "d1")
        assert [(c.id, c.inputs["m"]) for c in page.items] == [
            ("c0", "0"),
            ("c1", "edited"),
            ("c2", "2"),
            ("c9", "new"),
        ]
        draft = await records.delete_draft_cases(ORG_A, "p1", "d1", ["c0", "c2"], expected_revision=3)
        assert (draft.revision, draft.case_count) == (4, 2)
        published = await records.publish_draft(ORG_A, "p1", "d1", expected_revision=4, digest="a" * 64)
        assert (published.state, published.published_digest) == ("published", "a" * 64)
        await refused(
            "conflict", records.put_draft_cases(ORG_A, "p1", "d1", cases, expected_revision=published.revision)
        )
        await refused("not_found", records.put_draft_cases(ORG_A, "p1", "d9", cases, expected_revision=1))
        assert [d.id for d in await records.list_drafts(ORG_A, "p1")] == ["d1"]
        assert await records.get_draft(ORG_A, "p1", "d1") == published

    async def test_a_draft_is_renamed_in_turn_and_removed_with_its_cases(self, records: RecordStore) -> None:
        await records.create_draft(ORG_A, "p1", DatasetDraft(id="d1", project_id="p1", name="Refunds"))
        await records.put_draft_cases(ORG_A, "p1", "d1", [Case(id="c1", inputs={"m": "1"})], expected_revision=1)
        renamed = await records.edit_draft(ORG_A, "p1", "d1", expected_revision=2, name="Returns")
        assert (renamed.name, renamed.description, renamed.revision) == ("Returns", "", 3)
        described = await records.edit_draft(ORG_A, "p1", "d1", expected_revision=3, description="Every refund")
        assert (described.name, described.description, described.revision) == ("Returns", "Every refund", 4)
        await refused("conflict", records.edit_draft(ORG_A, "p1", "d1", expected_revision=3, name="Stale"))
        await refused("not_found", records.edit_draft(ORG_B, "p1", "d1", expected_revision=4, name="Theirs"))
        await records.delete_draft(ORG_A, "p1", "d1")
        assert await records.get_draft(ORG_A, "p1", "d1") is None
        await refused("not_found", records.draft_cases(ORG_A, "p1", "d1"))
        await refused("not_found", records.delete_draft(ORG_A, "p1", "d1"))

    async def test_a_saved_view_changes_in_turn(self, records: RecordStore) -> None:
        view = SavedView(id="v1", project_id="p1", owner="user-a", name="Slow", definition={"query": {"limit": 5}})
        saved = await records.save_view(ORG_A, "p1", view)
        assert saved.revision == 1 and saved.created_at is not None
        assert await records.save_view(ORG_A, "p1", view.model_copy(update={"name": "Other"})) == saved
        changed = await records.save_view(ORG_A, "p1", view.model_copy(update={"name": "Slowest"}), expected_revision=1)
        assert (changed.name, changed.revision, changed.created_at) == ("Slowest", 2, saved.created_at)
        await refused("conflict", records.save_view(ORG_A, "p1", view, expected_revision=1))
        await refused(
            "not_found", records.save_view(ORG_A, "p1", view.model_copy(update={"id": "v9"}), expected_revision=1)
        )
        await records.save_view(ORG_A, "p1", view.model_copy(update={"id": "v0", "name": "Errors"}))
        assert [v.name for v in await records.list_views(ORG_A, "p1")] == ["Errors", "Slowest"]
        assert await records.list_views(ORG_B, "p1") == [] and await records.get_view(ORG_B, "p1", "v1") is None
        await refused("conflict", records.delete_view(ORG_A, "p1", "v1", expected_revision=1))
        await records.delete_view(ORG_A, "p1", "v1", expected_revision=2)
        assert await records.get_view(ORG_A, "p1", "v1") is None
        await refused("not_found", records.delete_view(ORG_A, "p1", "v1", expected_revision=2))

    async def test_organisations_and_the_local_user_never_see_each_other(self, records: RecordStore) -> None:
        await records.create_run(ORG_A, run("r1"))
        await records.put_results(ORG_A, "p1", [CaseResult(run_id="r1", case_id="c1", status="passed")])
        await records.create_draft(ORG_A, "p1", DatasetDraft(id="d1", project_id="p1", name="Refunds"))
        await records.save_suite(ORG_A, "p1", Suite(id="s1", name="S", scorers=[JUDGE], digest="b" * 64))
        for other in (ORG_B, SINGLE_USER):
            assert await records.get_run(other, "p1", "r1") is None
            assert (await records.list_runs(other, "p1")).items == []
            await refused("not_found", records.list_results(other, "p1", "r1"))
            await refused(
                "not_found", records.put_results(other, "p1", [CaseResult(run_id="r1", case_id="c1", status="failed")])
            )
            await refused("not_found", records.update_run(other, run("r1")))
            assert await records.get_draft(other, "p1", "d1") is None
            await refused("not_found", records.put_draft_cases(other, "p1", "d1", [], expected_revision=1))
            assert await records.get_suite(other, "p1", "s1") is None
        # The same ids are separate records in another space.
        await records.create_run(ORG_B, run("r1", state="queued"))
        assert (await records.get_run(ORG_A, "p1", "r1")).state == "running"
        assert (await records.list_results(ORG_A, "p1", "r1")).items[0].status == "passed"

    async def test_a_monitor_changes_in_turn_and_keeps_its_progress_apart(self, records: RecordStore) -> None:
        made = await records.create_monitor(ORG_A, "p1", monitor("m1"))
        assert (made.revision, made.created_by, made.active_since) == (1, "user-a", made.created_at)
        assert await records.create_monitor(ORG_A, "p1", monitor("m1", name="Other")) == made
        await records.create_monitor(ORG_A, "p1", monitor("m0", name="A first"))
        assert [m.id for m in await records.list_monitors(ORG_A, "p1")] == ["m0", "m1"]
        assert await records.monitor_progress(ORG_A, "p1", "m1") == MonitorProgress()
        progress = MonitorProgress(watermark=T0, day=T0.date(), judge_tokens=12, scored=3)
        await records.set_monitor_progress(ORG_A, "p1", "m1", progress)
        paused = await records.update_monitor(
            ORG_A, "p1", made.model_copy(update={"state": "paused"}), expected_revision=1
        )
        assert (paused.state, paused.revision, paused.created_at) == ("paused", 2, made.created_at)
        await refused("conflict", records.update_monitor(ORG_A, "p1", made, expected_revision=1))
        # The routes' changes and the worker's progress never overwrite each other.
        assert await records.monitor_progress(ORG_A, "p1", "m1") == progress
        assert await records.get_monitor(ORG_A, "p1", "m1") == paused
        await records.delete_monitor(ORG_A, "p1", "m1")
        assert await records.get_monitor(ORG_A, "p1", "m1") is None
        assert await records.monitor_progress(ORG_A, "p1", "m1") is None
        await records.set_monitor_progress(ORG_A, "p1", "m1", progress)  # gone: nothing happens
        await refused("not_found", records.delete_monitor(ORG_A, "p1", "m1"))
        await refused("not_found", records.update_monitor(ORG_A, "p1", made, expected_revision=2))

    async def test_a_monitor_takes_each_trace_once_and_only_while_active(self, records: RecordStore) -> None:
        await records.create_monitor(ORG_A, "p1", monitor("m1"))
        assert await records.claim_monitored_trace(ORG_A, "p1", "m1", "tr-1", pass_id="first")
        assert await records.claim_monitored_trace(ORG_A, "p1", "m1", "tr-1", pass_id="first")  # its own
        # A pass that stopped before storing its scores is taken over by the next.
        assert await records.claim_monitored_trace(ORG_A, "p1", "m1", "tr-1", pass_id="second")
        score = Score(trace_id="tr-1", scorer="safety", source="llm_judge", value=True, passed=True)
        done = MonitoredTrace(monitor_id="m1", trace_id="tr-1", state="scored", pass_id="second", scores=[score])
        await records.finish_monitored_trace(ORG_A, "p1", done)
        assert not await records.claim_monitored_trace(ORG_A, "p1", "m1", "tr-1", pass_id="third")
        assert await records.claim_monitored_trace(ORG_A, "p1", "m1", "tr-2", pass_id="third")
        page = await records.list_monitored_traces(ORG_A, "p1", "m1", limit=1)
        assert [t.trace_id for t in page.items] == ["tr-2"] and page.next_cursor
        rest = await records.list_monitored_traces(ORG_A, "p1", "m1", limit=1, cursor=page.next_cursor)
        [scored] = rest.items
        assert (scored.state, scored.scores, scored.created_at is not None) == ("scored", [score], True)
        # Paused or gone, it takes nothing; another organisation's monitor of the same id is its own.
        stored = await records.get_monitor(ORG_A, "p1", "m1")
        assert stored is not None
        await records.update_monitor(ORG_A, "p1", stored.model_copy(update={"state": "paused"}), expected_revision=1)
        assert not await records.claim_monitored_trace(ORG_A, "p1", "m1", "tr-3", pass_id="third")
        assert not await records.claim_monitored_trace(ORG_B, "p1", "m1", "tr-3", pass_id="third")
        assert await records.get_monitor(ORG_B, "p1", "m1") is None
        assert (await records.list_monitored_traces(ORG_B, "p1", "m1")).items == []
        await records.delete_monitor(ORG_A, "p1", "m1")
        assert (await records.list_monitored_traces(ORG_A, "p1", "m1")).items == []

    async def test_changes_reach_listeners_of_the_same_organisation_only(self, records: RecordStore) -> None:
        mine, theirs = records.events.subscribe(ORG_A), records.events.subscribe(ORG_B)
        try:
            await records.create_run(ORG_A, run("r1"))
            await records.put_results(ORG_A, "p1", [CaseResult(run_id="r1", case_id="c1", status="passed")])
            got = [await mine.next(timeout=5), await mine.next(timeout=5)]
            assert [(e.kind, e.id) for e in got] == [("run", "r1"), ("case_results", "r1")]
            with pytest.raises(asyncio.TimeoutError):
                await theirs.next(timeout=0.2)
        finally:
            mine.close()
            theirs.close()


async def test_a_listener_that_falls_behind_is_told_to_read_again() -> None:
    from martian.evals.records import Event, InProcessEvents

    bus = InProcessEvents(queue_size=2)
    listener = bus.subscribe(ORG_A)
    for number in range(3):
        bus.publish("org-a", Event(kind="run", project_id="p1", id=str(number)))
    await asyncio.sleep(0)
    with pytest.raises(EventsLost):
        for _ in range(3):
            await listener.next(timeout=1)
    listener.close()
