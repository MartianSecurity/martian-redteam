"""The in-process mlflow-skinny binding passes the store contract on SQLite, and on Postgres when
MARTIAN_TEST_POSTGRES_URL names a database it may create schemas in."""

import contextlib
import json
import os
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from martian.evals.model import Counts, Project, Run, Span, Trace, TraceQuery
from martian.evals.server.principal import SINGLE_USER, organisation_principal
from martian.evals.store import REQUEST_PREVIEW_TRUNCATED, RESPONSE_PREVIEW_TRUNCATED, EvalStore, StoreError
from martian.evals.trace_conventions import MAPPING_KEY, MAPPING_VERSION
from tests.evals.store_contract import LONG_REQUEST, LONG_RESPONSE, ORG_A, T0, StoreContract, trace
from tests.evals.trace_samples import ANSWER, MODEL, QUESTION, sample_spans

POSTGRES_URL = os.environ.get("MARTIAN_TEST_POSTGRES_URL")
LONGEST_ARTIFACT_ROOT = 157
"""MLflow keeps a run's artifact location in ``runs.artifact_uri``, 200 characters, and a run's
folder adds ``/<32-digit run id>/artifacts``, 43, to the artifact root."""


@pytest.fixture(autouse=True)
def no_telemetry(monkeypatch):
    monkeypatch.setenv("MLFLOW_DISABLE_TELEMETRY", "true")
    monkeypatch.delenv("MLFLOW_WORKSPACE", raising=False)


@pytest.fixture
def unpooled(monkeypatch):
    """MLflow keeps each database URL's engine, and its open connections, for the life of the
    process, and every Postgres test's schema is a new URL. Without a pool a test's connections
    close as it finishes, so a full run, or several on one server, stay within its connection limit."""
    monkeypatch.setenv("MLFLOW_SQLALCHEMYSTORE_POOLCLASS", "NullPool")


@contextlib.contextmanager
def application_tracing(monkeypatch, tracking_uri: str, project: Project) -> Iterator[None]:
    """MLflow tracing in this process recording into the local user's workspace and the project's
    experiment, as an application does; the traces are sent before it ends."""
    import mlflow

    monkeypatch.setenv("MLFLOW_ENABLE_WORKSPACES", "true")
    monkeypatch.setenv("MLFLOW_WORKSPACE", "local")
    monkeypatch.setattr(mlflow.tracking.fluent, "_active_experiment_id", None)
    old_uri = mlflow.get_tracking_uri()
    # MLflow also writes these two to the environment; monkeypatch puts them back afterwards.
    monkeypatch.setenv("MLFLOW_TRACKING_URI", tracking_uri)
    monkeypatch.setenv("MLFLOW_EXPERIMENT_ID", str(project.store.experiment_id))
    mlflow.set_tracking_uri(tracking_uri)
    try:
        mlflow.set_experiment(experiment_id=project.store.experiment_id)
        yield
        mlflow.flush_trace_async_logging()
    finally:
        mlflow.set_tracking_uri(old_uri)
    monkeypatch.delenv("MLFLOW_ENABLE_WORKSPACES")
    monkeypatch.delenv("MLFLOW_WORKSPACE")


class MlflowTracingContract:
    """Traces an application records with MLflow tracing itself, read through the binding. A
    binding's class provides ``tracking_uri``: the database the store keeps MLflow's tables in."""

    async def test_traces_recorded_with_mlflow_tracing_form_sessions_and_turns(
        self, store: EvalStore, tracking_uri: str, monkeypatch
    ) -> None:
        import mlflow

        project = await store.create_project(SINGLE_USER, Project(id="p1", name="Support agent"))
        recorded: dict[str, list[str]] = {}
        with application_tracing(monkeypatch, tracking_uri, project):

            def turn(message: str, **trace_fields: object) -> None:
                with mlflow.start_span(name="chat") as span:
                    span.set_inputs({"message": message})
                    mlflow.update_current_trace(**trace_fields)  # type: ignore[arg-type]
                    span.set_outputs(f"echo {message}")
                recorded.setdefault(message.split()[0], []).append(span.trace_id)
                time.sleep(0.01)  # a distinct request time for each turn

            turn("alice hello", session_id="conversation-1", user="alice")
            turn("alice again", session_id="conversation-1", user="alice")
            turn("nobody here")
            # The metadata keys MLflow's documentation gives for users and sessions.
            turn("bob hi", metadata={"mlflow.trace.session": "conversation-2", "mlflow.trace.user": "bob"})

        found = await store.search_sessions(SINGLE_USER, TraceQuery(project_id="p1"))
        assert (found.traces_examined, found.ungrouped, found.has_more) == (4, 1, False)
        newest, older = found.items
        assert (newest.id, newest.turns, newest.preview) == ("conversation-2", 1, '{"message": "bob hi"}')
        assert (older.id, older.turns, older.errors) == ("conversation-1", 2, 0)
        assert older.preview == '{"message": "alice hello"}'
        assert older.started_at is not None and older.last_at is not None and older.started_at < older.last_at

        turns = await store.search_traces(
            SINGLE_USER, TraceQuery(project_id="p1", session_id="conversation-1", oldest_first=True)
        )
        assert [t.id for t in turns.items] == recorded["alice"]
        assert {(t.user_id, t.name, t.state) for t in turns.items} == {("alice", "chat", "ok")}
        by_user = await store.search_traces(SINGLE_USER, TraceQuery(project_id="p1", user_id="bob"))
        assert [t.id for t in by_user.items] == recorded["bob"]
        bob_sessions = await store.search_sessions(SINGLE_USER, TraceQuery(project_id="p1", user_id="bob"))
        assert [s.id for s in bob_sessions.items] == ["conversation-2"]

    async def test_a_trace_mlflow_tracing_recorded_is_mapped_as_it_is_read(
        self, store: EvalStore, tracking_uri: str, monkeypatch
    ) -> None:
        import mlflow

        project = await store.create_project(SINGLE_USER, Project(id="p1", name="Support agent"))
        model_call = sample_spans("openinference")[1]
        with application_tracing(monkeypatch, tracking_uri, project), mlflow.start_span(name="support agent") as root:
            root.set_inputs({"message": QUESTION})
            with mlflow.start_span(name=model_call.name) as span:
                span.set_attributes(model_call.attributes)
        # It never went through record_trace, so the store maps it as it reads it.
        (found,) = (await store.search_traces(SINGLE_USER, TraceQuery(project_id="p1"))).items
        assert found.metadata[MAPPING_KEY] == MAPPING_VERSION and found.tokens == 20
        read = await store.get_trace(SINGLE_USER, "p1", found.id)
        assert read is not None and read.metadata[MAPPING_KEY] == MAPPING_VERSION
        assert (read.model, [s.kind for s in read.spans]) == (MODEL, ["UNKNOWN", "LLM"])
        assert read.spans[1].attributes["llm.model_name"] == MODEL

    async def test_a_trace_recorded_inside_a_runs_mlflow_run_belongs_to_the_run(
        self, store: EvalStore, tracking_uri: str, monkeypatch
    ) -> None:
        import mlflow

        project = await store.create_project(SINGLE_USER, Project(id="p1", name="Support agent"))
        run = await store.start_run(SINGLE_USER, Run(id="r1", project_id="p1", source="local", state="running"))
        tagged = await store.record_trace(
            SINGLE_USER, Trace(id="case-1", project_id="p1", name="case 1", run_id="r1", request="hi")
        )
        recorded: dict[str, str] = {}
        with application_tracing(monkeypatch, tracking_uri, project):

            def call(name: str) -> None:
                with mlflow.start_span(name=name) as span:
                    span.set_inputs({"message": name})
                recorded[name] = span.trace_id

            # MLflow tracing names the active MLflow run in the trace's `mlflow.sourceRun`.
            with mlflow.start_run(run_id=run.store.run_id):
                call("in the run")
            with mlflow.start_run():  # the application's own MLflow run, which is no run of ours
                call("in its own run")
            call("in no run")

        found = await store.search_traces(SINGLE_USER, TraceQuery(project_id="p1"))
        assert {t.id: t.run_id for t in found.items} == {
            tagged.id: "r1",
            recorded["in the run"]: "r1",
            recorded["in its own run"]: None,
            recorded["in no run"]: None,
        }
        in_run = await store.search_traces(SINGLE_USER, TraceQuery(project_id="p1", run_id="r1", oldest_first=True))
        assert [t.id for t in in_run.items] == [tagged.id, recorded["in the run"]]
        read = await store.get_trace(SINGLE_USER, "p1", recorded["in the run"])
        assert read is not None and read.run_id == "r1" and [s.name for s in read.spans] == ["in the run"]
        own = await store.get_trace(SINGLE_USER, "p1", recorded["in its own run"])
        assert own is not None and own.run_id is None
        assert (await store.search_traces(SINGLE_USER, TraceQuery(project_id="p1", run_id="r2"))).items == []
        # The run's pages merge its two searches, on the tag and on the MLflow run, in order.
        by_one = TraceQuery(project_id="p1", run_id="r1", oldest_first=True, limit=1)
        first = await store.search_traces(SINGLE_USER, by_one)
        rest = await store.search_traces(SINGLE_USER, by_one.model_copy(update={"cursor": first.next_cursor}))
        assert [t.id for t in first.items + rest.items] == [tagged.id, recorded["in the run"]]
        assert rest.next_cursor is None
        # A trace linked to another run is that run's alone, wherever MLflow tracing recorded it.
        await store.start_run(SINGLE_USER, Run(id="r2", project_id="p1", source="local", state="running"))
        await store.link_traces(SINGLE_USER, "p1", "r2", [recorded["in the run"]])
        r1 = await store.search_traces(SINGLE_USER, TraceQuery(project_id="p1", run_id="r1"))
        r2 = await store.search_traces(SINGLE_USER, TraceQuery(project_id="p1", run_id="r2"))
        assert ([t.id for t in r1.items], [t.id for t in r2.items]) == ([tagged.id], [recorded["in the run"]])


class MlflowPagingContract:
    """How the binding reads traces: a page or a session search reads only its own traces, through
    MLflow's paging with every filter MLflow has, and free text, which only this process can
    filter, reads a bounded number of traces a call."""

    @pytest.fixture
    def reads(self, store: EvalStore, monkeypatch) -> list[int]:
        """How many traces each of the store's MLflow trace searches returned."""
        counts: list[int] = []
        mlflow_store = store._store  # type: ignore[attr-defined]
        search = mlflow_store.search_traces

        def counted(*args: object, **kwargs: object) -> object:
            page, token = search(*args, **kwargs)
            counts.append(len(page))
            return page, token

        monkeypatch.setattr(mlflow_store, "search_traces", counted)
        return counts

    async def test_a_page_reads_only_its_own_traces(self, store: EvalStore, reads: list[int]) -> None:
        await store.create_project(ORG_A, Project(id="p1", name="Support agent"))
        await store.start_run(ORG_A, Run(id="r1", project_id="p1", source="local", state="running"))
        for number in range(12):
            await store.record_trace(
                ORG_A,
                trace(
                    f"t{number}",
                    minutes=number,
                    state="error" if number % 4 == 0 else "ok",
                    session_id=f"s{number % 3}",
                    user_id=f"u{number % 2}",
                    tags={"route": f"r{number % 2}"},
                    run_id="r1" if number < 6 else None,
                    latency_ms=100.0 * (11 - number),  # the newest the fastest
                ),
            )
        # Each query's page of three (or fewer when fewer match) is all MLflow returns.
        for fields, expected in (
            ({}, 3),
            ({"oldest_first": True}, 3),
            ({"session_id": "s1"}, 3),
            ({"user_id": "u0"}, 3),
            ({"tags": {"route": "r1"}}, 3),
            ({"states": ["error"]}, 3),
            ({"trace_ids": ["t1", "t2"]}, 2),
            ({"since": T0 + timedelta(minutes=10)}, 2),
            ({"until": T0 + timedelta(minutes=1)}, 1),
            ({"run_id": "r1"}, 3),
            ({"min_latency_ms": 850}, 3),
            ({"min_latency_ms": 999.5}, 2),
            ({"slowest_first": True}, 3),
            ({"slowest_first": False, "min_latency_ms": 650}, 3),
        ):
            reads.clear()
            page = await store.search_traces(ORG_A, TraceQuery(project_id="p1", limit=3, **fields))  # type: ignore[arg-type]
            assert (len(page.items), sum(reads)) == (expected, expected), fields
        reads.clear()
        sessions = await store.search_sessions(ORG_A, TraceQuery(project_id="p1"), max_traces=5)
        assert (sessions.traces_examined, sessions.has_more, sum(reads)) == (5, True, 5)

    async def test_free_text_reads_a_bounded_number_of_traces_and_pages_on(
        self, store: EvalStore, reads: list[int], monkeypatch
    ) -> None:
        from martian.evals.store import mlflow_local

        monkeypatch.setattr(mlflow_local, "TRACE_SCAN_LIMIT", 4)
        monkeypatch.setattr(mlflow_local, "_SCAN_CHUNK", 3)
        await store.create_project(ORG_A, Project(id="p1", name="Support agent"))
        for number in range(10):
            response = "a needle" if number in (0, 1, 6) else "hay"
            await store.record_trace(ORG_A, trace(f"t{number}", minutes=number, response=response, session_id="s"))

        async def pages(limit: int) -> list[tuple[list[str], int]]:
            """Each call's traces, and how many traces it read, until no cursor is left."""
            found, cursor = [], None
            while True:
                reads.clear()
                query = TraceQuery(project_id="p1", text="needle", limit=limit, cursor=cursor)
                page = await store.search_traces(ORG_A, query)
                found.append(([t.id for t in page.items], sum(reads)))
                if (cursor := page.next_cursor) is None:
                    return found

        assert await pages(50) == [(["t6"], 4), ([], 4), (["t1", "t0"], 2)]
        assert await pages(1) == [(["t6"], 4), ([], 4), (["t1"], 2), (["t0"], 1)]
        reads.clear()
        sessions = await store.search_sessions(ORG_A, TraceQuery(project_id="p1", text="needle"))
        assert (sessions.traces_examined, sessions.has_more, sum(reads)) == (1, True, 4)

    @pytest.mark.parametrize(
        ("cursor", "run_id"),
        [
            *((cursor, None) for cursor in ("²", "١", "page-2", "9" * 19, "-1", "+1", " 1", "1\n", "1e3", "1.2")),
            ("1", "r1"),  # a run's search is two MLflow searches, so its cursor holds two offsets
            ("1.2.3", "r1"),
        ],
    )
    async def test_a_cursor_this_store_did_not_return_is_refused(
        self, store: EvalStore, cursor: str, run_id: str | None
    ) -> None:
        await store.create_project(ORG_A, Project(id="p1", name="Support agent"))
        await store.start_run(ORG_A, Run(id="r1", project_id="p1", source="local", state="running"))
        await store.record_trace(ORG_A, trace("t1", run_id="r1"))
        with pytest.raises(StoreError) as caught:
            await store.search_traces(ORG_A, TraceQuery(project_id="p1", run_id=run_id, cursor=cursor))
        assert caught.value.kind == "invalid"
        assert caught.value.detail == "cursor is not a next_cursor this store returned"
        # The largest offset a cursor holds reads past the end.
        page = await store.search_traces(ORG_A, TraceQuery(project_id="p1", cursor="9" * 18))
        assert (page.items, page.next_cursor) == ([], None)


class TraceLayoutContract:
    """Where the binding keeps a trace's request and response in MLflow, as MLflow tracing keeps
    them: whole as the inputs and outputs of the trace's root span, and shortened to MLflow's
    previews of 1,000 characters in ``trace_info``, whose columns Postgres limits to that."""

    async def test_the_request_and_response_are_whole_in_the_root_span_and_previewed_in_the_trace(
        self, store: EvalStore
    ) -> None:
        from martian.evals.store.mlflow_local import MlflowStore, _mlflow_trace_id

        assert isinstance(store, MlflowStore)
        await store.create_project(ORG_A, Project(id="p1", name="Support agent"))
        span = Span(id="s1", name="model call", kind="LLM", status="ok", inputs={"q": 1}, outputs="a")
        for trace_id, request, response, spans in (
            ("long", LONG_REQUEST, LONG_RESPONSE, [span]),
            ("short", "hi", "hello", []),
        ):
            fields = {"request": request, "response": response, "spans": spans}
            await store.record_trace(ORG_A, Trace(id=trace_id, project_id="p1", name="chat", state="ok", **fields))

        def native(trace_id: str) -> Any:
            """The trace as MLflow itself reads it."""
            return store._store.get_trace(_mlflow_trace_id(trace_id), allow_partial=True)

        long = await store._call(ORG_A, lambda: native("long"))
        # MLflow's previews, shortened as MLflow tracing shortens its own: 997 characters and "...".
        assert long.info.request_preview == LONG_REQUEST[:997] + "..."
        assert long.info.response_preview == LONG_RESPONSE[:997] + "..."
        # Whole as the root span's inputs and outputs, where MLflow's own reader finds a trace's request.
        assert json.loads(long.data.request) == LONG_REQUEST
        assert json.loads(long.data.response) == LONG_RESPONSE
        (root,) = [s for s in long.data.spans if s.parent_id is None]
        assert [(s.name, s.parent_id) for s in long.data.spans if s is not root] == [("model call", root.span_id)]
        # Not a second time in trace metadata, whose values Postgres limits to 8,000 characters.
        assert all(len(value) <= 1000 for value in long.info.trace_metadata.values())
        short = await store._call(ORG_A, lambda: native("short"))
        assert (short.info.request_preview, short.info.response_preview) == ("hi", "hello")
        assert (json.loads(short.data.request), json.loads(short.data.response)) == ("hi", "hello")

        # Through the binding: whole when read, MLflow's previews in a search, and the trace's
        # metadata says which previews were shortened.
        read = await store.get_trace(ORG_A, "p1", "long")
        assert read is not None
        assert (read.request, read.response, read.spans) == (LONG_REQUEST, LONG_RESPONSE, [span])
        # Every trace the binding records also names its trace conventions mapping version.
        mapped = {MAPPING_KEY: MAPPING_VERSION}
        shortened = {"martian.request_preview_truncated": "true", "martian.response_preview_truncated": "true"}
        assert read.metadata == {**shortened, **mapped}
        found = {t.id: t for t in (await store.search_traces(ORG_A, TraceQuery(project_id="p1"))).items}
        previews = (long.info.request_preview, long.info.response_preview)
        assert (found["long"].request, found["long"].response) == previews
        assert found["long"].metadata == {**shortened, **mapped}
        assert (found["short"].request, found["short"].response, found["short"].metadata) == ("hi", "hello", mapped)
        assert (await store.get_trace(ORG_A, "p1", "short")).spans == []  # type: ignore[union-attr]

    async def test_the_request_a_traces_own_root_span_gives_is_kept_whole_in_one_root_span(
        self, store: EvalStore
    ) -> None:
        """A trace that says nothing but its spans, as the OpenTelemetry endpoint records one: the
        mapping gives its request and response from its own root span, never the binding's, and
        the binding's root span keeps them whole, once, when the trace is recorded again."""
        from martian.evals.store.mlflow_local import TRACE_SPAN_KEY, MlflowStore, _mlflow_trace_id

        assert isinstance(store, MlflowStore)
        await store.create_project(ORG_A, Project(id="p1", name="Support agent"))
        agent, model_call, tool = sample_spans("openinference")
        whole = {"input.value": LONG_REQUEST, "output.value": LONG_RESPONSE}
        agent = agent.model_copy(update={"attributes": {**agent.attributes, **whole}})

        def roots() -> list[Any]:
            """The trace's spans without a parent, as MLflow itself reads them."""
            native = store._store.get_trace(_mlflow_trace_id("t1"), allow_partial=True)
            return [s for s in native.data.spans if s.parent_id is None]

        for spans in ([agent, model_call], [agent, model_call, tool]):
            # Recorded again with one more span, as the OpenTelemetry endpoint merges a later batch.
            await store.record_trace(ORG_A, Trace(id="t1", project_id="p1", name="turn", state="ok", spans=spans))
            read = await store.get_trace(ORG_A, "p1", "t1")
            assert read is not None
            assert (read.request, read.response, read.model) == (LONG_REQUEST, LONG_RESPONSE, MODEL)
            assert [(s.id, s.parent_id) for s in read.spans] == [(agent.id, None)] + [
                (s.id, agent.id) for s in spans[1:]
            ]
            assert [s.kind for s in read.spans] == ["AGENT", "LLM", "TOOL"][: len(spans)]
            (root,) = await store._call(ORG_A, roots)
            assert root.get_attribute(TRACE_SPAN_KEY) is True
            assert (root.inputs, root.outputs) == (LONG_REQUEST, LONG_RESPONSE)
            (found,) = (await store.search_traces(ORG_A, TraceQuery(project_id="p1"))).items
            assert (found.request, found.response) == (LONG_REQUEST[:997] + "...", LONG_RESPONSE[:997] + "...")
            assert (found.metadata[REQUEST_PREVIEW_TRUNCATED], found.metadata[RESPONSE_PREVIEW_TRUNCATED]) == (
                "true",
                "true",
            )

    async def test_a_trace_recorded_without_a_mapping_version_is_mapped_from_its_own_spans(
        self, store: EvalStore, monkeypatch
    ) -> None:
        """A trace the binding wrote, with its root span, before it mapped traces is mapped as it is
        read: from the trace's own spans, with the request its own root span gives where the
        trace's was empty, as if it had been mapped when it was recorded."""
        from martian.evals.store import mlflow_local

        await store.create_project(ORG_A, Project(id="p1", name="Support agent"))
        agent, model_call, _ = sample_spans("openinference")
        with monkeypatch.context() as unmapped:
            unmapped.setattr(mlflow_local, "map_trace", lambda trace: trace)
            await store.record_trace(
                ORG_A, Trace(id="t1", project_id="p1", name="turn", state="ok", spans=[agent, model_call])
            )
        (found,) = (await store.search_traces(ORG_A, TraceQuery(project_id="p1"))).items
        assert (found.metadata[MAPPING_KEY], found.request, found.model) == (MAPPING_VERSION, "", None)
        read = await store.get_trace(ORG_A, "p1", "t1")
        assert read is not None and read.metadata[MAPPING_KEY] == MAPPING_VERSION
        assert (read.request, read.response, read.model, read.tokens) == (QUESTION, ANSWER, MODEL, 20)
        assert [(s.id, s.parent_id, s.kind) for s in read.spans] == [
            (agent.id, None, "AGENT"),
            (model_call.id, agent.id, "LLM"),
        ]


class TraceDeletionContract:
    """A deleted trace is gone from MLflow's own tables, not marked deleted: its row, its tags and
    metadata, its spans, its assessments and its place in a review queue. MLflow keeps one row: a
    linked trace's association with its MLflow run, which holds the two ids and nothing else."""

    async def test_a_deleted_trace_leaves_no_row_in_mlflows_tables_but_its_run_link(self, store: EvalStore) -> None:
        from sqlalchemy import MetaData, String, Table, func, inspect, or_, select

        from martian.evals.model import ReviewQuestion, ReviewQueue, Score
        from martian.evals.store.mlflow_local import MlflowStore, _mlflow_trace_id

        assert isinstance(store, MlflowStore)
        await store.create_project(ORG_A, Project(id="p1", name="Support agent"))
        await store.start_run(ORG_A, Run(id="r1", project_id="p1", source="local", state="running"))
        span = Span(id="s1", name="model call", kind="LLM", status="ok", inputs={"q": 1}, outputs="a")
        for trace_id in ("gone", "kept"):
            await store.record_trace(ORG_A, trace(trace_id, spans=[span], session_id="s", tags={"team": "a"}))
            await store.log_score(ORG_A, "p1", trace_id, Score(scorer="rubric_judge", source="llm_judge", value=True))
        await store.link_traces(ORG_A, "p1", "r1", ["gone", "kept"])
        question = ReviewQuestion(name="correct", instruction="Is the answer correct?")
        await store.save_question(ORG_A, "p1", question)
        queue = ReviewQueue(id="q1", run_id="r1", name="Check", questions=[question], case_ids=["c1", "c2"])
        await store.create_queue(ORG_A, "p1", queue, ["gone", "kept"])

        def rows_naming(native_id: str) -> dict[str, int]:
            """How many rows of each of MLflow's tables hold the trace's MLflow id in a text column."""
            engine = store._store.engine
            found = {}
            with engine.connect() as connection:
                for name in inspect(engine).get_table_names():
                    table = Table(name, MetaData(), autoload_with=engine)
                    texts = [column for column in table.columns if isinstance(column.type, String)]
                    if texts:
                        holding = or_(*(column == native_id for column in texts))
                        found[name] = connection.execute(
                            select(func.count()).select_from(table).where(holding)
                        ).scalar()
            return {name: count for name, count in found.items() if count}

        gone, kept = _mlflow_trace_id("gone"), _mlflow_trace_id("kept")
        before = rows_naming(gone)
        trace_tables = {"trace_info", "trace_tags", "trace_request_metadata", "spans", "assessments"}
        assert trace_tables | {"review_queue_items", "entity_associations"} <= set(before)
        assert await store.delete_traces(ORG_A, "p1", ["gone"]) == ["gone"]
        assert rows_naming(gone) == {"entity_associations": 1}
        assert rows_naming(kept) == before


def artifact_root_of_length(tmp_path: Path, length: int) -> Path:
    """A folder under ``tmp_path`` whose absolute path is ``length`` characters long."""
    root = tmp_path.resolve()
    while (missing := length - len(str(root)) - 1) > 60:
        root = root / ("a" * 50)
    if missing > 0:
        root = root / ("a" * missing)
    assert len(str(root)) == length, "the test's temporary folder is longer than the root it needs"
    return root


async def artifact_uri(store: EvalStore, project_id: str, run_id: str) -> str:
    """The artifact location MLflow keeps for the run."""
    from martian.evals.store.mlflow_local import MlflowStore

    assert isinstance(store, MlflowStore)
    experiment = await store._call(ORG_A, lambda: store._experiment(project_id))
    found = await store._call(ORG_A, lambda: store._mlflow_run(experiment.experiment_id, run_id))
    return str(found.info.artifact_uri)


class ArtifactRootContract:
    """Where the binding keeps a run's artifacts. A binding's class provides ``store_at``: a
    function opening the store on the class's database with the artifact root it is given."""

    async def test_a_run_starts_with_the_longest_artifact_root(
        self, store_at: Callable[[Path], EvalStore], tmp_path
    ) -> None:
        root = artifact_root_of_length(tmp_path, LONGEST_ARTIFACT_ROOT)
        store = store_at(root)
        # An organisation's MLflow workspace has the longest name, 44 characters.
        await store.create_project(ORG_A, Project(id="p1", name="Agent"))
        run = await store.start_run(ORG_A, Run(id="r1", project_id="p1", source="local", state="running"))
        await store.put_artifact(ORG_A, "p1", "r1", "summary.json", b'{"passed": 1}')
        finished = await store.finish_run(ORG_A, "p1", "r1", state="completed", counts=Counts(cases=1, passed=1))

        assert await store.get_run(ORG_A, "p1", "r1") == finished
        assert await store.get_artifact(ORG_A, "p1", "r1", "summary.json") == b'{"passed": 1}'
        location = await artifact_uri(store, "p1", "r1")
        assert location == f"{root}/{run.store.run_id}/artifacts"
        assert len(location) == 200

    async def test_runs_written_with_the_earlier_artifact_layout_still_read(
        self, store_at: Callable[[Path], EvalStore], tmp_path, monkeypatch
    ) -> None:
        """An earlier release kept a project's experiment artifacts in MLflow's own layout,
        ``workspaces/<workspace>/<experiment id>`` under the root. Such an experiment keeps that
        folder, for the runs it already has and for new ones."""
        from mlflow.store.tracking.sqlalchemy_workspace_store import WorkspaceAwareSqlAlchemyStore

        from martian.evals._mlflow import WorkspaceStore
        from martian.evals.store.mlflow_local import workspace_name

        root = tmp_path / "artifacts"
        with monkeypatch.context() as earlier:
            earlier.setattr(
                WorkspaceStore, "_get_artifact_location", WorkspaceAwareSqlAlchemyStore._get_artifact_location
            )
            before = store_at(root)
            project = await before.create_project(ORG_A, Project(id="p1", name="Agent"))
            old = await before.start_run(ORG_A, Run(id="r1", project_id="p1", source="local", state="running"))
            await before.put_artifact(ORG_A, "p1", "r1", "summary.json", b"old")
        experiment_folder = root.resolve() / "workspaces" / workspace_name(ORG_A) / str(project.store.experiment_id)
        assert await artifact_uri(before, "p1", "r1") == f"{experiment_folder}/{old.store.run_id}/artifacts"

        store = store_at(root)
        assert await store.get_run(ORG_A, "p1", "r1") == old
        assert await store.get_artifact(ORG_A, "p1", "r1", "summary.json") == b"old"
        assert await store.list_artifacts(ORG_A, "p1", "r1") == ["summary.json"]
        finished = await store.finish_run(ORG_A, "p1", "r1", state="completed", counts=Counts(cases=1, passed=1))
        assert await store.get_run(ORG_A, "p1", "r1") == finished

        # A new run in the earlier experiment goes in that experiment's folder, as MLflow places it.
        second = await store.start_run(ORG_A, Run(id="r2", project_id="p1", source="local", state="running"))
        await store.put_artifact(ORG_A, "p1", "r2", "summary.json", b"new")
        assert await store.get_artifact(ORG_A, "p1", "r2", "summary.json") == b"new"
        assert await artifact_uri(store, "p1", "r2") == f"{experiment_folder}/{second.store.run_id}/artifacts"
        # A new project's runs go in the root itself.
        await store.create_project(ORG_A, Project(id="p2", name="Other agent"))
        later = await store.start_run(ORG_A, Run(id="r3", project_id="p2", source="local", state="running"))
        assert await artifact_uri(store, "p2", "r3") == f"{root.resolve()}/{later.store.run_id}/artifacts"
        assert await store.get_run(ORG_A, "p2", "r3") == later


class TestMlflowStoreOnSqlite(
    StoreContract,
    MlflowTracingContract,
    MlflowPagingContract,
    TraceLayoutContract,
    ArtifactRootContract,
    TraceDeletionContract,
):
    @pytest.fixture
    def tracking_uri(self, tmp_path) -> str:
        return f"sqlite:///{tmp_path / 'mlflow.db'}"

    @pytest.fixture
    def store_at(self, tracking_uri) -> Callable[[Path], EvalStore]:
        from martian.evals.store.mlflow_local import MlflowStore

        return lambda root: MlflowStore(tracking_uri, root)

    @pytest.fixture
    def store(self, tmp_path, store_at) -> EvalStore:
        return store_at(tmp_path / "artifacts")

    async def test_sqlite_takes_an_artifact_root_longer_than_postgres_allows(self, store_at, tmp_path) -> None:
        """SQLite does not enforce the length of MLflow's ``runs.artifact_uri``."""
        store = store_at(artifact_root_of_length(tmp_path, LONGEST_ARTIFACT_ROOT + 1))
        await store.create_project(ORG_A, Project(id="p1", name="Agent"))
        run = await store.start_run(ORG_A, Run(id="r1", project_id="p1", source="local", state="running"))
        assert await store.get_run(ORG_A, "p1", "r1") == run


@pytest.mark.skipif(not POSTGRES_URL, reason="set MARTIAN_TEST_POSTGRES_URL to run the store contract on Postgres")
@pytest.mark.usefixtures("unpooled")
class TestMlflowStoreOnPostgres(
    StoreContract,
    MlflowTracingContract,
    MlflowPagingContract,
    TraceLayoutContract,
    ArtifactRootContract,
    TraceDeletionContract,
):
    @pytest.fixture
    def schema(self) -> str:
        return f"mlflow_test_{uuid.uuid4().hex[:10]}"

    @pytest.fixture
    def tracking_uri(self, schema) -> str:
        from martian.evals._sql import postgres_url

        assert POSTGRES_URL is not None
        return postgres_url(POSTGRES_URL, schema)

    @pytest.fixture
    def store_at(self, schema) -> Iterator[Callable[[Path], EvalStore]]:
        from sqlalchemy import create_engine, text

        from martian.evals.store.mlflow_local import MlflowStore

        assert POSTGRES_URL is not None
        yield lambda root: MlflowStore(POSTGRES_URL, root, schema=schema)
        engine = create_engine(POSTGRES_URL)
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()

    @pytest.fixture
    def store(self, tmp_path, store_at) -> EvalStore:
        return store_at(tmp_path / "artifacts")

    def test_postgres_refuses_an_artifact_root_too_long_for_mlflow_before_writing(
        self, store_at, schema, tmp_path
    ) -> None:
        from sqlalchemy import create_engine, text

        root = artifact_root_of_length(tmp_path, LONGEST_ARTIFACT_ROOT + 1)
        with pytest.raises(ValueError) as refused:
            store_at(root)
        message = str(refused.value)
        assert "artifact_root" in message and f"at most {LONGEST_ARTIFACT_ROOT} characters" in message
        assert f"{LONGEST_ARTIFACT_ROOT + 1} characters" in message
        assert not root.exists()
        engine = create_engine(POSTGRES_URL)
        try:
            with engine.begin() as connection:
                found = connection.execute(
                    text("SELECT 1 FROM pg_namespace WHERE nspname = :schema"), {"schema": schema}
                ).first()
            assert found is None, "the store created its schema before refusing the root"
        finally:
            engine.dispose()


async def test_the_binding_never_turns_workspaces_on_for_the_whole_process(tmp_path):
    from martian.evals.store.mlflow_local import MlflowStore

    store = MlflowStore(f"sqlite:///{tmp_path / 'mlflow.db'}", tmp_path / "artifacts")
    await store.create_project(ORG_A, Project(id="p1", name="Agent"))
    assert "MLFLOW_ENABLE_WORKSPACES" not in os.environ
    assert "MLFLOW_WORKSPACE" not in os.environ


async def test_each_organisation_and_the_local_user_get_their_own_workspace(tmp_path):
    from martian.evals.store.mlflow_local import MlflowStore

    store = MlflowStore(f"sqlite:///{tmp_path / 'mlflow.db'}", tmp_path / "artifacts")
    org = organisation_principal("u", "Org With Spaces/And:Symbols", frozenset())
    names = {store._workspace(p) for p in (SINGLE_USER, org, ORG_A)}
    assert len(names) == 3 and "local" in names
    assert all(n == "local" or (n.startswith("org-") and len(n) == 44) for n in names)
    assert store._workspace(org) == store._workspace(
        organisation_principal("other user", "Org With Spaces/And:Symbols", frozenset())
    )


@pytest.mark.skipif(not POSTGRES_URL, reason="set MARTIAN_TEST_POSTGRES_URL to check the schema on Postgres")
@pytest.mark.usefixtures("unpooled")
async def test_mlflow_tables_live_in_their_own_postgres_schema(tmp_path):
    from sqlalchemy import create_engine, text

    from martian.evals.store.mlflow_local import MlflowStore

    schema = f"mlflow_test_{uuid.uuid4().hex[:10]}"
    store = MlflowStore(POSTGRES_URL, tmp_path / "artifacts", schema=schema)
    await store.create_project(ORG_A, Project(id="p1", name="Agent"))
    engine = create_engine(POSTGRES_URL)
    try:
        with engine.begin() as connection:
            tables = connection.execute(
                text("SELECT table_schema, table_name FROM information_schema.tables WHERE table_name = 'experiments'")
            ).all()
            assert (schema, "experiments") in tables
            assert ("public", "experiments") not in tables
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    finally:
        engine.dispose()
