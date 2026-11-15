"""The behaviour every ``EvalStore`` binding must have.

A binding's test module subclasses ``StoreContract`` and provides a ``store`` fixture; the memory
store, the in-process MLflow binding and the server's REST binding all run these same tests.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from martian.evals.model import (
    Case,
    Counts,
    Project,
    ReviewQuestion,
    ReviewQueue,
    Run,
    Score,
    ScorerVersion,
    Session,
    SessionSearch,
    Span,
    Trace,
    TraceQuery,
)
from martian.evals.server.principal import SINGLE_USER, organisation_principal
from martian.evals.store import EvalStore, StoreError
from martian.evals.trace_conventions import MAPPING_KEY, MAPPING_VERSION
from tests.evals.trace_samples import ARGUMENTS, MODEL, QUESTION, RESULT, STYLES, UNKNOWN, sample_trace

ORG_A = organisation_principal("user-a", "org-a", frozenset({"admin"}))
ORG_B = organisation_principal("user-b", "org-b", frozenset({"admin"}))
T0 = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)


def text_of_length(length: int, words: str, end: str) -> str:
    """``words`` repeated to ``length`` characters, ending in ``end``, so that a copy shortened
    anywhere is told apart from the whole."""
    return (words * (length // len(words) + 1))[: length - len(end)] + end


LONG_REQUEST = text_of_length(100_000, "Retrieved context for the question. ", "What is the refund window?")
"""A prompt with retrieved context: 100,000 characters."""
LONG_RESPONSE = text_of_length(100_000, "An answer quoting the policy at length. ", "Thirty days.")


def cases(*texts: str) -> list[Case]:
    return [
        Case(id=f"c{i}", inputs={"message": text}, expectations={"secret": "piano"}) for i, text in enumerate(texts)
    ]


def trace(trace_id: str, *, minutes: int = 0, **fields: object) -> Trace:
    values: dict[str, object] = {
        "id": trace_id,
        "project_id": "p1",
        "name": "chat",
        "state": "ok",
        "request_time": T0 + timedelta(minutes=minutes),
        "request": f"question {trace_id}",
        "response": f"answer {trace_id}",
    }
    values.update(fields)
    return Trace.model_validate(values)


async def refused(kind: str, awaitable: object) -> None:
    with pytest.raises(StoreError) as caught:
        await awaitable  # type: ignore[misc]
    assert caught.value.kind == kind


class StoreContract:
    @pytest.fixture
    async def project(self, store: EvalStore) -> Project:
        return await store.create_project(ORG_A, Project(id="p1", name="Support agent"))

    async def test_status_answers_for_each_principal(self, store: EvalStore) -> None:
        for principal in (SINGLE_USER, ORG_A):
            assert (await store.status(principal)).ready is True

    async def test_projects_are_created_once_and_listed(self, store: EvalStore, project: Project) -> None:
        assert project.store.experiment_id and project.created_at is not None
        again = await store.create_project(ORG_A, Project(id="p1", name="Support agent"))
        assert again == project
        await refused("conflict", store.create_project(ORG_A, Project(id="p1", name="Other")))
        await store.create_project(ORG_A, Project(id="p2", name="Another agent"))
        assert [p.id for p in await store.list_projects(ORG_A)] == ["p2", "p1"]
        assert await store.get_project(ORG_A, "missing") is None

    async def test_an_archived_project_takes_no_new_work(self, store: EvalStore, project: Project) -> None:
        archived = await store.archive_project(ORG_A, "p1")
        assert archived.archived_at is not None
        await refused("conflict", store.create_dataset(ORG_A, "p1", "set", cases("hi")))
        await refused(
            "conflict", store.start_run(ORG_A, Run(id="r1", project_id="p1", source="local", state="running"))
        )
        assert (await store.get_project(ORG_A, "p1")) == archived

    async def test_organisations_and_the_local_user_never_see_each_other(
        self, store: EvalStore, project: Project
    ) -> None:
        dataset = await store.create_dataset(ORG_A, "p1", "set", cases("hi"))
        await store.start_run(ORG_A, Run(id="r1", project_id="p1", source="local", state="running"))
        await store.record_trace(ORG_A, trace("t1", run_id="r1"))
        for other in (ORG_B, SINGLE_USER):
            assert await store.get_project(other, "p1") is None
            assert await store.list_projects(other) == []
            await refused("not_found", store.list_datasets(other, "p1"))
            await refused("not_found", store.get_dataset(other, "p1", dataset.id))
            await refused("not_found", store.read_cases(other, "p1", dataset.id))
            await refused("not_found", store.get_run(other, "p1", "r1"))
            await refused("not_found", store.get_trace(other, "p1", "t1"))
            await refused("not_found", store.search_traces(other, TraceQuery(project_id="p1")))
            await refused("not_found", store.search_sessions(other, TraceQuery(project_id="p1")))
            await refused("not_found", store.set_trace_tag(other, "p1", "t1", "k", "v"))
            await refused(
                "not_found", store.log_score(other, "p1", "t1", Score(scorer="s", source="human", value=True))
            )
            await refused("not_found", store.create_dataset(other, "p1", "set", cases("hi")))
        # The same ids in another space are separate objects.
        mine = await store.create_project(ORG_B, Project(id="p1", name="Someone else's"))
        assert mine.name == "Someone else's" and (await store.get_project(ORG_A, "p1")).name == "Support agent"
        assert await store.list_datasets(ORG_B, "p1") == []

    async def test_datasets_are_immutable_versions_identified_by_content(
        self, store: EvalStore, project: Project
    ) -> None:
        first = await store.create_dataset(ORG_A, "p1", "set", cases("a", "b", "c"))
        assert (first.version, first.case_count, first.created_by) == (1, 3, "user-a")
        assert await store.create_dataset(ORG_A, "p1", "set", cases("a", "b", "c")) == first
        second = await store.create_dataset(ORG_A, "p1", "set", cases("a", "b", "changed"))
        assert second.version == 2 and second.digest != first.digest and second.id != first.id
        assert await store.find_dataset(ORG_A, "p1", "set") == second
        assert await store.find_dataset(ORG_A, "p1", "set", digest=first.digest) == first
        assert await store.find_dataset(ORG_A, "p1", "missing") is None
        assert await store.get_dataset(ORG_A, "p1", first.id) == first
        assert [d.version for d in await store.list_datasets(ORG_A, "p1")] == [1, 2]
        page = await store.read_cases(ORG_A, "p1", first.id, offset=0, limit=2)
        assert [c.id for c in page.items] == ["c0", "c1"] and page.next_cursor is not None
        rest = await store.read_cases(ORG_A, "p1", first.id, offset=2, limit=2)
        assert [c.id for c in rest.items] == ["c2"] and rest.next_cursor is None
        assert rest.items[0].inputs == {"message": "c"}
        await refused("invalid", store.create_dataset(ORG_A, "p1", "empty", []))
        await refused("invalid", store.create_dataset(ORG_A, "p1", "dup", [cases("x")[0], cases("y")[0]]))

    async def test_a_run_is_started_once_and_finished_with_its_counts(self, store: EvalStore, project: Project) -> None:
        run = Run(id="r1", project_id="p1", source="local", state="running", dataset_name="set")
        started = await store.start_run(ORG_A, run)
        assert started.store.run_id and started.started_at is not None
        assert await store.start_run(ORG_A, run) == started
        await refused("invalid", store.finish_run(ORG_A, "p1", "r1", state="running", counts=Counts()))
        done = await store.finish_run(ORG_A, "p1", "r1", state="completed", counts=Counts(cases=3, passed=2, failed=1))
        assert (done.state, done.counts.passed, done.finished_at is not None) == ("completed", 2, True)
        assert await store.get_run(ORG_A, "p1", "r1") == done
        assert await store.get_run(ORG_A, "p1", "missing") is None
        await refused("not_found", store.finish_run(ORG_A, "p1", "missing", state="failed", counts=Counts()))

    async def test_traces_are_searched_without_spans_and_read_with_them(
        self, store: EvalStore, project: Project
    ) -> None:
        await store.start_run(ORG_A, Run(id="r1", project_id="p1", source="local", state="running"))
        span = Span(id="s1", name="model call", kind="LLM", status="ok", inputs={"q": 1}, outputs="a")
        await store.record_trace(ORG_A, trace("t1", minutes=0, run_id="r1", session_id="sess", spans=[span]))
        await store.record_trace(ORG_A, trace("t2", minutes=1, state="error", tags={"team": "a"}))
        await store.record_trace(ORG_A, trace("t3", minutes=2, session_id="sess", tags={"team": "b"}))

        async def ids(**query: object) -> list[str]:
            page = await store.search_traces(ORG_A, TraceQuery(project_id="p1", **query))  # type: ignore[arg-type]
            assert all(t.spans == [] for t in page.items)
            return [t.id for t in page.items]

        assert await ids() == ["t3", "t2", "t1"]
        assert await ids(oldest_first=True) == ["t1", "t2", "t3"]
        assert await ids(run_id="r1") == ["t1"]
        assert await ids(session_id="sess") == ["t3", "t1"]
        assert await ids(states=["error"]) == ["t2"]
        assert await ids(tags={"team": "b"}) == ["t3"]
        assert await ids(text="ANSWER t2") == ["t2"]
        assert await ids(trace_ids=["t1", "t3"]) == ["t3", "t1"]
        assert await ids(since=T0 + timedelta(minutes=1), until=T0 + timedelta(minutes=2)) == ["t2"]
        first = await store.search_traces(ORG_A, TraceQuery(project_id="p1", limit=2))
        assert first.next_cursor is not None
        second = await store.search_traces(ORG_A, TraceQuery(project_id="p1", limit=2, cursor=first.next_cursor))
        assert [t.id for t in second.items] == ["t1"] and second.next_cursor is None
        full = await store.get_trace(ORG_A, "p1", "t1")
        assert full is not None and full.spans == [span] and full.session_id == "sess"
        assert await store.get_trace(ORG_A, "p1", "missing") is None

    async def test_sessions_and_users_come_from_the_traces(self, store: EvalStore, project: Project) -> None:
        # s2 starts first; s1 has the newest turn; t4 has no session.
        await store.record_trace(ORG_A, trace("t1", minutes=0, session_id="s2", user_id="bob", tokens=5))
        await store.record_trace(ORG_A, trace("t2", minutes=1, session_id="s1", user_id="alice", tokens=10))
        await store.record_trace(ORG_A, trace("t3", minutes=2, session_id="s1", user_id="alice", state="error"))
        await store.record_trace(ORG_A, trace("t4", minutes=3, user_id="carol"))
        await store.record_trace(ORG_A, trace("t5", minutes=4, session_id="s1", user_id="alice", tokens=7))

        found = await store.search_sessions(ORG_A, TraceQuery(project_id="p1"))
        assert found == SessionSearch(
            items=[
                Session(
                    id="s1",
                    turns=3,
                    errors=1,
                    started_at=T0 + timedelta(minutes=1),
                    last_at=T0 + timedelta(minutes=4),
                    preview="question t2",
                    tokens=17,
                ),
                Session(id="s2", turns=1, started_at=T0, last_at=T0, preview="question t1", tokens=5),
            ],
            traces_examined=5,
            ungrouped=1,
        )
        # Sessions follow the query's order, and the preview is the last turn read.
        oldest = await store.search_sessions(ORG_A, TraceQuery(project_id="p1", oldest_first=True))
        assert [(s.id, s.preview) for s in oldest.items] == [("s2", "question t1"), ("s1", "question t5")]
        # A session's turns are the traces with its id.
        turns = await store.search_traces(ORG_A, TraceQuery(project_id="p1", session_id="s1", oldest_first=True))
        assert [(t.id, t.user_id) for t in turns.items] == [("t2", "alice"), ("t3", "alice"), ("t5", "alice")]
        # The user filters traces and sessions alike.
        bob = await store.search_traces(ORG_A, TraceQuery(project_id="p1", user_id="bob"))
        assert [t.id for t in bob.items] == ["t1"]
        bob_sessions = await store.search_sessions(ORG_A, TraceQuery(project_id="p1", user_id="bob"))
        assert [s.id for s in bob_sessions.items] == ["s2"]
        carol = await store.search_sessions(ORG_A, TraceQuery(project_id="p1", user_id="carol"))
        assert (carol.items, carol.traces_examined, carol.ungrouped) == ([], 1, 1)
        # Every trace filter applies before grouping.
        errors = await store.search_sessions(ORG_A, TraceQuery(project_id="p1", states=["error"]))
        assert [(s.id, s.turns, s.errors) for s in errors.items] == [("s1", 1, 1)]
        # Only the first traces in the query's order are grouped; has_more says more matched.
        bounded = await store.search_sessions(ORG_A, TraceQuery(project_id="p1"), max_traces=2)
        assert (bounded.has_more, bounded.traces_examined, bounded.ungrouped, bounded.limit) == (True, 2, 1, 2)
        assert [(s.id, s.turns) for s in bounded.items] == [("s1", 1)]
        await refused("invalid", store.search_sessions(ORG_A, TraceQuery(project_id="p1", cursor="2")))
        await refused("invalid", store.search_sessions(ORG_A, TraceQuery(project_id="p1"), max_traces=1001))
        await refused("not_found", store.search_sessions(ORG_A, TraceQuery(project_id="missing")))

    async def test_traces_in_each_convention_are_mapped_and_keep_their_attributes(
        self, store: EvalStore, project: Project
    ) -> None:
        for minutes, style in enumerate(STYLES):
            sample = sample_trace(style, f"tr-{minutes + 1:032x}", project_id="p1")
            saved = await store.record_trace(
                ORG_A, sample.model_copy(update={"request_time": T0 + timedelta(minutes=minutes)})
            )
            assert saved.metadata[MAPPING_KEY] == MAPPING_VERSION, style
            assert (saved.model, saved.tokens, saved.session_id, saved.user_id) == (
                MODEL,
                20,
                "conversation-1",
                "alice",
            )
            assert QUESTION in saved.request and [s.kind for s in saved.spans][::2] == ["AGENT", "TOOL"]
            assert (saved.spans[2].inputs, saved.spans[2].outputs) == (ARGUMENTS, RESULT)
            assert saved.spans[0].attributes.items() >= UNKNOWN.items(), style
        page = await store.search_traces(ORG_A, TraceQuery(project_id="p1", oldest_first=True))
        assert [(t.model, t.tokens, t.metadata.get(MAPPING_KEY)) for t in page.items] == [
            (MODEL, 20, MAPPING_VERSION)
        ] * 3
        sessions = await store.search_sessions(ORG_A, TraceQuery(project_id="p1"))
        assert [(s.id, s.turns, s.tokens) for s in sessions.items] == [("conversation-1", 3, 60)]
        alice = await store.search_traces(ORG_A, TraceQuery(project_id="p1", user_id="alice"))
        assert len(alice.items) == 3

    async def test_a_trace_s_model_is_stored_and_searched_by_name(self, store: EvalStore, project: Project) -> None:
        """A trace's model, given or mapped from its spans' attributes (the response's model before
        the request's), is kept, comes back in a search and is a filter of its own
        (MartianSecurity/martian-notes#200)."""
        span = Span(id="s1", name="chat", kind="CHAT_MODEL", status="ok")

        def attributes(**values: str) -> list[Span]:
            return [span.model_copy(update={"attributes": values})]

        await store.record_trace(ORG_A, trace("t0", minutes=0, model="gpt-4o-2024-05-13"))
        await store.record_trace(
            ORG_A,
            trace(
                "t1",
                minutes=1,
                spans=attributes(**{"gen_ai.request.model": "gpt-4o", "gen_ai.response.model": "gpt-4o-2024-08-06"}),
            ),
        )
        await store.record_trace(ORG_A, trace("t2", minutes=2, spans=attributes(**{"gen_ai.request.model": "gpt-4o"})))
        await store.record_trace(ORG_A, trace("t3", minutes=3, model="it's `quoted`"))
        await store.record_trace(ORG_A, trace("t4", minutes=4))
        found = await store.search_traces(ORG_A, TraceQuery(project_id="p1", oldest_first=True))
        assert [(t.id, t.model) for t in found.items] == [
            ("t0", "gpt-4o-2024-05-13"),
            ("t1", "gpt-4o-2024-08-06"),
            ("t2", "gpt-4o"),
            ("t3", "it's `quoted`"),
            ("t4", None),
        ]
        for model, ids in (
            ("gpt-4o-2024-08-06", ["t1"]),
            ("gpt-4o", ["t2"]),
            ("it's `quoted`", ["t3"]),
            ("gpt-4", []),
        ):
            page = await store.search_traces(ORG_A, TraceQuery(project_id="p1", model=model))
            assert [t.id for t in page.items] == ids, model
        read = await store.get_trace(ORG_A, "p1", "t1")
        assert read is not None and read.model == "gpt-4o-2024-08-06"

    async def test_traces_take_tags_and_join_runs(self, store: EvalStore, project: Project) -> None:
        await store.start_run(ORG_A, Run(id="r1", project_id="p1", source="ci", state="running"))
        await store.record_trace(ORG_A, trace("t1"))
        await store.set_trace_tag(ORG_A, "p1", "t1", "martian.evals.triage", '{"status":"open"}')
        await store.link_traces(ORG_A, "p1", "r1", ["t1"])
        saved = await store.get_trace(ORG_A, "p1", "t1")
        assert saved is not None and saved.run_id == "r1" and saved.tags["martian.evals.triage"] == '{"status":"open"}'
        await refused("not_found", store.link_traces(ORG_A, "p1", "r1", ["missing"]))
        await refused("not_found", store.link_traces(ORG_A, "p1", "missing", ["t1"]))
        await refused("not_found", store.set_trace_tag(ORG_A, "p1", "missing", "k", "v"))

    async def test_a_span_with_no_start_time_leaves_its_trace_at_the_time_it_was_given(
        self, store: EvalStore, project: Project
    ) -> None:
        # As an imported Inspect sample that failed before the model answered: its messages have no times.
        start = T0.timestamp() * 1000
        spans = [
            Span(id="sample", name="task", kind="AGENT", status="error", start_ms=start, end_ms=start + 2000),
            Span(id="unanswered", parent_id="sample", name="messages", kind="UNKNOWN", status="error"),
        ]
        await store.record_trace(ORG_A, trace("t1", state="error", latency_ms=2000.0, spans=spans))
        read = await store.get_trace(ORG_A, "p1", "t1")
        assert read is not None
        assert (read.request_time, read.latency_ms, read.spans) == (T0, 2000.0, spans)
        page = await store.search_traces(ORG_A, TraceQuery(project_id="p1", since=T0))
        assert [t.id for t in page.items] == ["t1"]

    async def test_a_long_request_and_response_are_kept_whole(self, store: EvalStore, project: Project) -> None:
        """Read back, a trace has its request and its response whole. A search may return a
        preview of either instead; the trace's metadata then says that preview was shortened."""
        span = Span(id="s1", name="model call", kind="LLM", status="ok", inputs={"q": 1}, outputs="a")
        await store.record_trace(ORG_A, trace("t1", request=LONG_REQUEST, response=LONG_RESPONSE, spans=[span]))
        read = await store.get_trace(ORG_A, "p1", "t1")
        assert read is not None
        assert (read.request, read.response, read.spans) == (LONG_REQUEST, LONG_RESPONSE, [span])
        (found,) = (await store.search_traces(ORG_A, TraceQuery(project_id="p1"))).items
        for field, whole in (("request", LONG_REQUEST), ("response", LONG_RESPONSE)):
            key = f"martian.{field}_preview_truncated"
            shown: str = getattr(found, field)
            assert found.metadata.get(key) == read.metadata.get(key), key
            if read.metadata.get(key) == "true":
                assert len(shown) < len(whole) and shown.endswith("...") and whole.startswith(shown[:-3])
            else:
                assert key not in read.metadata and shown == whole

    async def test_free_text_looks_in_the_start_of_a_long_request_and_response(
        self, store: EvalStore, project: Project
    ) -> None:
        """Free text matches a trace's name, its request and its response up to 1,000 characters,
        and the first 997 of a longer one, which MLflow's preview holds before its "..."
        (MartianSecurity/martian-notes#348). Read back, the trace has both whole."""
        inside, outside = "|inside-the-reach|", "|outside-the-reach|"
        request = "a" * (997 - len(inside)) + inside + outside + "b" * 2000
        await store.record_trace(ORG_A, trace("long", minutes=2, request=request, response=LONG_RESPONSE))
        at_most = "c" * (1000 - len("|ends-at-1000|")) + "|ends-at-1000|"
        await store.record_trace(ORG_A, trace("whole", minutes=1, request=at_most, response="Thirty days."))
        await store.record_trace(ORG_A, trace("short", minutes=0, response="What is the refund window? Thirty days."))

        async def ids(text: str) -> list[str]:
            pages = [await store.search_traces(ORG_A, TraceQuery(project_id="p1", text=text))]
            while (cursor := pages[-1].next_cursor) is not None:
                pages.append(await store.search_traces(ORG_A, TraceQuery(project_id="p1", text=text, cursor=cursor)))
            return [t.id for page in pages for t in page.items]

        assert await ids("INSIDE-the-reach") == ["long"]
        assert await ids("a|inside-the-reach|") == ["long"]
        assert await ids("outside-the-reach") == []
        assert await ids("inside-the-reach||out") == []
        assert await ids("...") == []
        assert await ids("|ends-at-1000|") == ["whole"]
        assert await ids("An answer quoting the policy") == ["long"]
        assert await ids("Thirty days.") == ["whole", "short"]  # LONG_RESPONSE ends with it, past the reach
        sessions = await store.search_sessions(ORG_A, TraceQuery(project_id="p1", text="outside-the-reach"))
        assert sessions.traces_examined == 0
        read = await store.get_trace(ORG_A, "p1", "long")
        assert read is not None and (read.request, read.response) == (request, LONG_RESPONSE)

    async def test_a_trace_keeps_the_state_it_was_recorded_with(self, store: EvalStore, project: Project) -> None:
        span = Span(id="s1", name="model call", kind="LLM", status="ok")
        states = ("ok", "error", "in_progress", "unknown")
        for state in states:
            await store.record_trace(ORG_A, trace(f"{state}-alone", state=state))
            await store.record_trace(ORG_A, trace(f"{state}-with-spans", state=state, spans=[span]))
        found = {t.id: t.state for t in (await store.search_traces(ORG_A, TraceQuery(project_id="p1"))).items}
        for state in states:
            for trace_id in (f"{state}-alone", f"{state}-with-spans"):
                read = await store.get_trace(ORG_A, "p1", trace_id)
                assert read is not None and (read.state, found[trace_id]) == (state, state), trace_id

    async def test_every_filter_pages_through_exactly_its_traces(self, store: EvalStore, project: Project) -> None:
        # Seven traces a minute apart; t2, t5 and t6 are r1's, t1 and t4 errors, t3 and t6 hold "refund".
        await store.start_run(ORG_A, Run(id="r1", project_id="p1", source="local", state="running"))
        for number in range(7):
            await store.record_trace(
                ORG_A,
                trace(
                    f"t{number}",
                    minutes=number,
                    state="error" if number in (1, 4) else "ok",
                    run_id="r1" if number in (2, 5, 6) else None,
                    session_id=f"s{number % 2}",
                    user_id="alice" if number < 4 else "bob",
                    tags={"route": f"r{number % 3}"},
                    response="a refund" if number in (3, 6) else f"answer t{number}",
                ),
            )

        async def paged(**query: object) -> list[str]:
            """Every page of the query, two traces at a time: a page may hold fewer than its limit,
            and only a missing next_cursor ends the search."""
            ids: list[str] = []
            cursor = None
            for _ in range(20):
                page = await store.search_traces(
                    ORG_A,
                    TraceQuery(project_id="p1", limit=2, cursor=cursor, **query),  # type: ignore[arg-type]
                )
                ids += [t.id for t in page.items]
                if (cursor := page.next_cursor) is None:
                    return ids
            raise AssertionError("the search never ended")

        assert await paged() == ["t6", "t5", "t4", "t3", "t2", "t1", "t0"]
        assert await paged(oldest_first=True) == ["t0", "t1", "t2", "t3", "t4", "t5", "t6"]
        assert await paged(run_id="r1") == ["t6", "t5", "t2"]
        assert await paged(run_id="r1", oldest_first=True, states=["ok"]) == ["t2", "t5", "t6"]
        assert await paged(session_id="s0") == ["t6", "t4", "t2", "t0"]
        assert await paged(user_id="bob", tags={"route": "r1"}) == ["t4"]
        assert await paged(states=["error"]) == ["t4", "t1"]
        assert await paged(trace_ids=["t0", "t3", "t5", "missing"]) == ["t5", "t3", "t0"]
        assert await paged(since=T0 + timedelta(minutes=2), until=T0 + timedelta(minutes=5)) == ["t4", "t3", "t2"]
        assert await paged(text="REFUND") == ["t6", "t3"]
        assert await paged(text="answer", run_id="r1") == ["t5", "t2"]
        # A session search groups the same traces as the first page of the search.
        refunds = await store.search_sessions(ORG_A, TraceQuery(project_id="p1", text="refund"))
        assert [(s.id, s.turns) for s in refunds.items] == [("s0", 1), ("s1", 1)]
        assert (refunds.traces_examined, refunds.has_more) == (2, False)

    async def test_scores_are_kept_with_their_trace(self, store: EvalStore, project: Project) -> None:
        await store.record_trace(ORG_A, trace("t1"))
        judged = await store.log_score(
            ORG_A,
            "p1",
            "t1",
            Score(scorer="rubric_judge", source="llm_judge", value=False, passed=False, rationale="It leaks."),
        )
        assert judged.trace_id == "t1" and judged.store.assessment_id and judged.created_at is not None
        human = await store.log_score(ORG_A, "p1", "t1", Score(scorer="human_review", source="human", value=True))
        assert [s.scorer for s in await store.list_scores(ORG_A, "p1", "t1")] == ["rubric_judge", "human_review"]
        read = await store.get_trace(ORG_A, "p1", "t1")
        assert read is not None and read.scores == [judged, human]
        (found,) = (await store.search_traces(ORG_A, TraceQuery(project_id="p1"))).items
        assert found.scores == [judged, human]
        await refused("not_found", store.log_score(ORG_A, "p1", "missing", Score(scorer="s", source="code")))

    async def test_a_trace_keeps_the_time_it_was_given_even_at_the_epoch(
        self, store: EvalStore, project: Project
    ) -> None:
        epoch = datetime(1970, 1, 1, tzinfo=UTC)
        await store.record_trace(ORG_A, trace("t1", request_time=epoch))
        read = await store.get_trace(ORG_A, "p1", "t1")
        assert read is not None and read.request_time == epoch

    async def test_a_score_keeps_the_time_it_was_given(self, store: EvalStore, project: Project) -> None:
        # As an imported score does: the time it was scored, not the time it was written.
        await store.record_trace(ORG_A, trace("t1"))
        given = await store.log_score(ORG_A, "p1", "t1", Score(scorer="politeness", source="human", created_at=T0))
        assert given.created_at == T0
        assert [s.created_at for s in await store.list_scores(ORG_A, "p1", "t1")] == [T0]

    async def test_registered_scorers_are_versioned_by_name(self, store: EvalStore, project: Project) -> None:
        judge = ScorerVersion(
            name="politeness", kind="llm_judge", type="guidelines", criteria={"guidelines": ["Be kind."]}
        )
        first = await store.register_scorer(ORG_A, "p1", judge)
        second = await store.register_scorer(
            ORG_A, "p1", judge.model_copy(update={"criteria": {"guidelines": ["Kind."]}})
        )
        await store.register_scorer(ORG_A, "p1", ScorerVersion(name="accuracy", kind="llm_judge", type="rubric"))
        assert (first.version, second.version) == ("1", "2")
        assert [(s.name, s.version) for s in await store.list_scorers(ORG_A, "p1")] == [
            ("accuracy", "1"),
            ("politeness", "2"),
        ]
        assert await store.scorer_versions(ORG_A, "p1", "politeness") == [first, second]
        assert await store.scorer_versions(ORG_A, "p1", "missing") == []

    async def test_review_questions_and_queues(self, store: EvalStore, project: Project) -> None:
        question = ReviewQuestion(name="correct", instruction="Is the answer correct?")
        assert await store.save_question(ORG_A, "p1", question) == question
        assert await store.save_question(ORG_A, "p1", question) == question
        await refused("conflict", store.save_question(ORG_A, "p1", question.model_copy(update={"kind": "text"})))
        assert await store.list_questions(ORG_A, "p1") == [question]
        await store.record_trace(ORG_A, trace("t1"))
        await store.record_trace(ORG_A, trace("t2"))
        queue = ReviewQueue(id="q1", run_id="r1", name="Check answers", questions=[question], case_ids=["c1", "c2"])
        await refused("invalid", store.create_queue(ORG_A, "p1", queue, ["t1"]))
        await refused("not_found", store.create_queue(ORG_A, "p1", queue, ["t1", "missing"]))
        created = await store.create_queue(ORG_A, "p1", queue, ["t1", "t2"])
        assert created.created_at is not None and await store.get_queue(ORG_A, "p1", "q1") == created
        items = await store.queue_items(ORG_A, "p1", "q1")
        assert [(i.trace_id, i.case_id, i.status) for i in items] == [("t1", "c1", "pending"), ("t2", "c2", "pending")]
        done = await store.set_queue_item(ORG_A, "p1", "q1", "t2", "completed")
        assert done.status == "completed"
        assert [i.status for i in await store.queue_items(ORG_A, "p1", "q1")] == ["pending", "completed"]
        await refused("not_found", store.set_queue_item(ORG_A, "p1", "q1", "t9", "completed"))
        await refused("not_found", store.queue_items(ORG_A, "p1", "missing"))
        assert await store.get_queue(ORG_A, "p1", "missing") is None

    async def test_run_artifacts(self, store: EvalStore, project: Project) -> None:
        await store.start_run(ORG_A, Run(id="r1", project_id="p1", source="ci", state="running"))
        await store.put_artifact(ORG_A, "p1", "r1", "reports/summary.json", b'{"passed": 2}')
        await store.put_artifact(ORG_A, "p1", "r1", "junit.xml", b"<testsuite/>")
        assert await store.list_artifacts(ORG_A, "p1", "r1") == ["junit.xml", "reports/summary.json"]
        assert await store.get_artifact(ORG_A, "p1", "r1", "reports/summary.json") == b'{"passed": 2}'
        assert await store.get_artifact(ORG_A, "p1", "r1", "missing.txt") is None
        for bad in ("../escape", "/abs", "a//b", "."):
            await refused("invalid", store.put_artifact(ORG_A, "p1", "r1", bad, b"x"))
        await refused("not_found", store.put_artifact(ORG_A, "p1", "missing", "a.txt", b"x"))
        await refused("not_found", store.list_artifacts(ORG_B, "p1", "r1"))
