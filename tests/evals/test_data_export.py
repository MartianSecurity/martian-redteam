"""The full data export: everything an organisation holds, exported, imported into a fresh install
and exported again with nothing lost, on the in-memory store and on the local edition's MLflow
store; an interrupted export resumed to the same files; and what an import refuses."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from martian.cli import app
from martian.evals import data_export
from martian.evals.data_export import (
    FILES,
    MANIFEST,
    PROGRESS,
    ExportError,
    export_data,
    export_local,
    import_data,
    import_local,
)
from martian.evals.inspect_import import import_inspect_logs
from martian.evals.local import create_local_app, open_stores
from martian.evals.model import (
    Case,
    CaseResult,
    Counts,
    DatasetDraft,
    Policy,
    Project,
    Review,
    ReviewQuestion,
    ReviewQueue,
    Run,
    Score,
    ScorerVersion,
    ScoringJob,
    Span,
    StoreRef,
    Suite,
    Trace,
    suite_digest,
)
from martian.evals.records import RecordStore, SavedView
from martian.evals.records.sql import SqlRecordStore
from martian.evals.server.principal import SINGLE_USER, Principal, organisation_principal
from martian.evals.store import EvalStore, MemoryStore

ORG_A = organisation_principal("user-a", "org-a", frozenset({"admin"}))
ORG_B = organisation_principal("user-b", "org-b", frozenset({"admin"}))
T0 = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
INSPECT = Path(__file__).resolve().parents[1] / "fixtures" / "inspect"

# What the receiving store assigns itself for what it creates (docs/data-export.md): left out of
# the comparison. Every other field of every object must come back as it was exported.
REASSIGNED = {
    "projects.jsonl": ("project", {"created_at"}),
    "datasets.jsonl": ("dataset", {"created_at", "created_by"}),
    "drafts.jsonl": ("draft", {"revision", "updated_at", "updated_by"}),
    "views.jsonl": ("view", {"revision", "updated_at"}),
}


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("MLFLOW_DISABLE_TELEMETRY", "true")
    monkeypatch.setenv("MARTIAN_EVALS_HOME", str(tmp_path / "evals-home"))


def ids() -> tuple[str, str]:
    """A run id and a trace id in the forms the core's routes take."""
    return str(uuid.uuid4()), "tr-" + uuid.uuid4().hex


async def populate(store: EvalStore, records: RecordStore, principal: Principal, *, default_exists: bool) -> None:
    """One of every kind of object an export covers, linked as the core links them."""
    if not default_exists:
        await store.create_project(principal, Project(id="default", name="Default", description="Org project"))
    await store.create_project(principal, Project(id=str(uuid.uuid4()), name="Old work", description="Retired"))
    retired = next(p for p in await store.list_projects(principal) if p.name == "Old work")
    project = "default"

    judge = ScorerVersion(name="politeness", kind="llm_judge", type="guidelines", model="glm-5")
    await store.register_scorer(principal, project, judge)
    await store.register_scorer(principal, project, judge.model_copy(update={"criteria": {"guidelines": ["Kind."]}}))
    await store.register_scorer(principal, retired.id, ScorerVersion(name="accuracy", kind="code", type="exact"))
    await store.save_question(principal, project, ReviewQuestion(name="helpful", instruction="Was it helpful?"))
    await store.save_question(
        principal,
        project,
        ReviewQuestion(name="tone", kind="categorical", instruction="The tone", options=["warm", "cold"]),
    )

    cases = [
        Case(id=f"c{i}", inputs={"message": f"question {i}"}, expectations={"expected_response": f"answer {i}"})
        for i in range(3)
    ]
    first = await store.create_dataset(principal, project, "support", cases)
    second = await store.create_dataset(
        principal, project, "support", [*cases[:2], Case(id="c9", inputs={"message": "new"}, tags={"k": "v"})]
    )
    await store.create_dataset(principal, retired.id, "legacy", [Case(id="x", inputs={"q": 1}, metadata={"n": 2})])

    suite = Suite(
        id="suite-1",
        name="Support",
        dataset_name="support",
        dataset_digest=first.digest,
        scorers=[judge],
        policies=[Policy(scorer="politeness", threshold=0.5)],
        digest="0" * 64,
        created_at=T0,
    )
    await records.save_suite(principal, project, suite.model_copy(update={"digest": suite_digest(suite)}))
    changed = suite.model_copy(update={"input_key": "prompt"})
    await records.save_suite(principal, project, changed.model_copy(update={"digest": suite_digest(changed)}))

    # A completed run, in the store and the record store, with traces, scores and reviews.
    run_id, _ = ids()
    run = Run(
        id=run_id,
        project_id=project,
        code="support-1",
        source="console",
        state="running",
        dataset_name="support",
        dataset_digest=first.digest,
        scorers=[judge],
        counts=Counts(cases=3),
        provenance={"note": "exported"},
        requested_by=principal.user_id,
        created_at=T0,
        started_at=T0,
    )
    native = await store.start_run(principal, run)
    stored_run = StoreRef(
        experiment_id=native.store.experiment_id, run_id=native.store.run_id, dataset_id=first.store.dataset_id
    )
    run = await records.create_run(principal, run.model_copy(update={"store": stored_run}))
    results, scores, traced = [], [], []
    for position, case in enumerate(cases):
        _, trace_id = ids()
        when = T0 + timedelta(minutes=position)
        score = Score(
            run_id=run_id,
            case_id=case.id,
            trace_id=trace_id,
            scorer="politeness",
            scorer_version="2",
            source="llm_judge",
            source_id="glm-5",
            value=position != 1,
            passed=position != 1,
            rationale="Kind." if position != 1 else "Curt.",
            created_at=when,
        )
        saved = await store.record_trace(
            principal,
            Trace(
                id=trace_id,
                project_id=project,
                name=f"support-1 {case.id}",
                state="ok",
                request_time=when,
                latency_ms=120.0,
                request=case.inputs["message"],
                response=f"answer {position}",
                run_id=run_id,
                tags={"martian.case_id": case.id},
                scores=[score],
                spans=[
                    Span(id=f"root-{position}", name="agent", kind="AGENT", status="ok", start_ms=1.0, end_ms=9.0),
                    Span(
                        id=f"llm-{position}",
                        parent_id=f"root-{position}",
                        name="chat",
                        kind="LLM",
                        status="ok",
                        start_ms=2.0,
                        end_ms=8.0,
                        inputs={"messages": [{"role": "user", "content": case.inputs["message"]}]},
                        outputs={"content": f"answer {position}"},
                        attributes={"tokens": 12},
                    ),
                ],
            ),
        )
        traced.append(trace_id)
        assessment = saved.scores[0].store
        results.append(
            CaseResult(
                run_id=run_id,
                case_id=case.id,
                position=position,
                status="passed" if position != 1 else "failed",
                inputs=case.inputs,
                expectations=case.expectations,
                output=f"answer {position}",
                latency_ms=120.0,
                finished_at=when,
                store=StoreRef(
                    experiment_id=run.store.experiment_id,
                    run_id=run.store.run_id,
                    trace_id=trace_id,
                    assessment_id=assessment.assessment_id,
                ),
            )
        )
        scores.append(score.model_copy(update={"store": assessment}))
    await store.link_traces(principal, project, run_id, traced)
    await records.put_results(principal, project, results)
    await records.put_scores(principal, project, scores)
    await store.put_artifact(principal, project, run_id, "reports/summary.md", b"# Support run\n")
    await store.finish_run(principal, project, run_id, state="completed", counts=Counts(cases=3, passed=2, failed=1))
    run = await records.update_run(
        principal,
        run.model_copy(
            update={
                "state": "completed",
                "counts": Counts(cases=3, passed=2, failed=1),
                "finished_at": T0 + timedelta(hours=1),
            }
        ),
        expected_state="running",
    )

    # Scored again by a scoring job, reviewed by hand and handed to a review queue.
    job = ScoringJob(id=str(uuid.uuid4()), run_id=run_id, scorers=[judge], state="completed", total=3, succeeded=3)
    await records.create_scoring_job(principal, project, job)
    await records.put_scores(
        principal,
        project,
        [
            Score(run_id=run_id, case_id=c.id, scorer="politeness", source="llm_judge", value=1, scoring_job_id=job.id)
            for c in cases
        ],
    )
    queue = ReviewQueue(
        id=str(uuid.uuid4()),
        run_id=run_id,
        name="Check curt answers",
        questions=[ReviewQuestion(name="helpful", instruction="Was it helpful?")],
        case_ids=["c0", "c1"],
        assignee="user-z",
        created_by=principal.user_id,
        created_at=T0,
    )
    await store.create_queue(principal, project, queue, traced[:2])
    pending = Review(
        id=str(uuid.uuid4()),
        run_id=run_id,
        case_id="c0",
        criterion="helpful",
        state="pending",
        queue_id=queue.id,
        created_at=T0 + timedelta(hours=2),
    )
    await records.add_review(principal, project, pending)
    answered = pending.model_copy(
        update={
            "id": str(uuid.uuid4()),
            "state": "complete",
            "value": True,
            "comment": "Fine.",
            "reviewer": principal.user_id,
            "previous_id": pending.id,
            "created_at": T0 + timedelta(hours=3),
        }
    )
    await records.add_review(principal, project, answered)
    await store.set_queue_item(principal, project, queue.id, traced[0], "completed")

    # A run admitted but never started: in the record store only.
    queued_id, _ = ids()
    await records.create_run(
        principal,
        Run(
            id=queued_id,
            project_id=project,
            source="console",
            state="queued",
            dataset_name="support",
            dataset_digest=second.digest,
            created_at=T0 + timedelta(days=1),
            store=StoreRef(dataset_id=second.store.dataset_id),
        ),
    )

    # Live traffic: a session's turns, one annotated by a person.
    for turn in range(2):
        _, trace_id = ids()
        annotation = Score(
            trace_id=trace_id,
            scorer="correct",
            source="human",
            source_id=principal.user_id,
            value="yes",
            rationale="Checked.",
            metadata={"martian.annotation": "feedback"},
            created_at=T0 + timedelta(days=2, minutes=turn),
        )
        await store.record_trace(
            principal,
            Trace(
                id=trace_id,
                project_id=project,
                name="chat",
                state="error" if turn else "ok",
                request_time=T0 + timedelta(days=2, minutes=turn),
                request=f"turn {turn}",
                response="...",
                session_id="conversation-1",
                user_id="customer-7",
                model="glm-5",
                tokens=40,
                cost=0.002,
                tags={"env": "prod"},
                metadata={"release": "1.4"},
                scores=[annotation] if turn == 0 else [],
            ),
        )

    # Dataset drafts, one published, and saved views.
    draft = DatasetDraft(id=str(uuid.uuid4()), project_id=project, name="support next", source_dataset_id=first.id)
    saved_draft = await records.create_draft(principal, project, draft)
    await records.put_draft_cases(principal, project, draft.id, cases[:2], expected_revision=saved_draft.revision)
    done = DatasetDraft(id=str(uuid.uuid4()), project_id=project, name="published", description="Shipped")
    saved_done = await records.create_draft(principal, project, done)
    saved_done = await records.put_draft_cases(
        principal, project, done.id, cases[2:], expected_revision=saved_done.revision
    )
    await records.publish_draft(
        principal, project, done.id, expected_revision=saved_done.revision, digest=second.digest
    )
    for visibility in ("private", "organization"):
        await records.save_view(
            principal,
            project,
            SavedView(
                id=str(uuid.uuid4()),
                project_id=project,
                owner=principal.user_id,
                name=f"errors ({visibility})",
                visibility=visibility,  # type: ignore[arg-type]
                definition={"states": ["error"]},
            ),
        )
    await store.archive_project(principal, retired.id)


def normalised(folder: Path, references: dict[str, dict[str, str]] | None = None) -> dict[str, list[Any]]:
    """An export's objects, with the source store's identifiers replaced by the receiving store's
    and the fields the receiving store assigns left out."""
    refs = references or {}

    def new(kind: str, value: Any) -> Any:
        return refs.get(kind, {}).get(value, value) if isinstance(value, str) else value

    def walk(value: Any) -> Any:
        if isinstance(value, list):
            return [walk(item) for item in value]
        if not isinstance(value, dict):
            return value
        out = {}
        for key, item in value.items():
            if key == "store" and isinstance(item, dict):
                out[key] = {name: new(name, ref) for name, ref in item.items()}
            elif key in ("dataset_id", "source_dataset_id"):
                out[key] = new("dataset_id", item)
            else:
                out[key] = walk(item)
        return out

    found: dict[str, list[Any]] = {}
    for name in FILES:
        lines = [walk(json.loads(raw)) for raw in (folder / name).read_text(encoding="utf-8").splitlines()]
        if name in REASSIGNED:
            key, fields = REASSIGNED[name]
            for line in lines:
                for field in fields:
                    line[key].pop(field)
        for line in lines:
            if name == "datasets.jsonl":
                line["dataset"]["id"] = new("dataset_id", line["dataset"]["id"])
            if name == "projects.jsonl":
                line["project"]["archived_at"] = line["project"]["archived_at"] is not None
            if name == "traces.jsonl":
                line["trace"]["scores"].sort(key=lambda score: json.dumps(score, sort_keys=True))
        found[name] = lines
    return found


def manifest(folder: Path) -> dict[str, Any]:
    return json.loads((folder / MANIFEST).read_text(encoding="utf-8"))


def assert_round_trip(first: Path, again: Path, references: dict[str, dict[str, str]]) -> None:
    # Every kind of object is in the export, so every one makes the round trip.
    assert {name: entry["lines"] > 0 for name, entry in manifest(first)["files"].items()} == dict.fromkeys(FILES, True)
    assert {n: e["lines"] for n, e in manifest(again)["files"].items()} == {
        n: e["lines"] for n, e in manifest(first)["files"].items()
    }
    assert normalised(first, references) == normalised(again)


async def test_an_organisation_comes_back_whole_in_a_fresh_install(tmp_path):
    source, source_records = MemoryStore(), SqlRecordStore(f"sqlite:///{tmp_path / 'source.db'}")
    await populate(source, source_records, ORG_A, default_exists=False)
    # Another organisation in the same install is not part of the export.
    await source.create_project(ORG_B, Project(id="default", name="Default"))
    await source_records.create_run(ORG_B, Run(id="b-run", project_id="default", source="local", state="queued"))

    first = await export_data(source, source_records, ORG_A, tmp_path / "first")
    assert first.manifest["organisation_id"] == "org-a" and not first.resumed
    target, target_records = MemoryStore(), SqlRecordStore(f"sqlite:///{tmp_path / 'target.db'}")
    imported = await import_data(target, target_records, ORG_A, tmp_path / "first")
    again = await export_data(target, target_records, ORG_A, tmp_path / "again")

    assert_round_trip(tmp_path / "first", tmp_path / "again", imported.references)
    assert "b-run" not in (tmp_path / "first" / "runs.jsonl").read_text(encoding="utf-8")
    assert again.lines("runs.jsonl") == 2
    # The receiving store's dataset ids are new, and the references to them follow.
    assert set(imported.references["dataset_id"]) != set(imported.references["dataset_id"].values())
    source_records.close()
    target_records.close()


def served(home: Path, ui: Path) -> dict[str, Any]:
    """What the local Evals UI shows of every run and review, without the ids the store assigns."""

    def without_store_ids(value: Any) -> Any:
        if isinstance(value, list):
            return [without_store_ids(item) for item in value]
        if isinstance(value, dict):
            return {
                k: without_store_ids(v)
                for k, v in value.items()
                if k not in ("dataset_id", "native_id", "evals_assessment_id")
            }
        return value

    client = TestClient(create_local_app(home, ui), base_url="http://127.0.0.1:7600")
    paths = ["/v1/evals", "/v1/evals/reviews"]
    for run in client.get("/v1/evals").json():
        paths += [f"/v1/evals/{run['id']}", f"/v1/evals/{run['id']}/workbench"]
    return {path: without_store_ids(client.get(path).json()) for path in paths}


def test_the_local_edition_comes_back_whole_in_a_fresh_data_folder(tmp_path):
    home, fresh = tmp_path / "home", tmp_path / "fresh"
    import_inspect_logs([INSPECT / "glm5.json", INSPECT / "mockllm-epochs.eval"], home)
    store, records = open_stores(home)
    asyncio.run(populate(store, records, SINGLE_USER, default_exists=True))
    records.close()

    export_local(home, tmp_path / "first")
    imported = import_local(tmp_path / "first", fresh)
    export_local(fresh, tmp_path / "again")

    assert_round_trip(tmp_path / "first", tmp_path / "again", imported.references)
    # MLflow assigns its own experiment, run and assessment ids; every reference follows them.
    assert all(imported.references[kind] for kind in ("experiment_id", "run_id", "dataset_id", "assessment_id"))
    # The Evals UI shows the imported runs, their results, scores and reviews as it showed them.
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text("<!doctype html><title>Martian Evals</title>", encoding="utf-8")
    shown = served(home, ui)
    assert len(shown) == 2 + 2 * 4 and shown == served(fresh, ui)


class Interrupted(Exception):
    """Stands in for the export being stopped."""


class Interrupting:
    """A store or record store whose ``n``-th call of ``method`` raises, as if the export was
    stopped there."""

    def __init__(self, wrapped: Any, method: str, n: int) -> None:
        self._wrapped, self._method, self._n, self._calls = wrapped, method, n, 0

    def __getattr__(self, name: str) -> Any:
        found = getattr(self._wrapped, name)
        if name != self._method:
            return found

        async def call(*args: Any, **kwargs: Any) -> Any:
            self._calls += 1
            if self._calls == self._n:
                raise Interrupted
            return await found(*args, **kwargs)

        return call


async def test_an_interrupted_export_resumes_to_the_same_files(tmp_path, monkeypatch):
    monkeypatch.setattr(data_export, "TRACE_PAGE", 2)
    store, records = MemoryStore(), SqlRecordStore(f"sqlite:///{tmp_path / 'source.db'}")
    await populate(store, records, ORG_A, default_exists=False)
    whole = await export_data(store, records, ORG_A, tmp_path / "whole")
    folder = tmp_path / "resumed"

    # Stopped inside the completed run's step (the second, newest first), once its case results
    # are written but before its scores.
    with pytest.raises(Interrupted):
        await export_data(store, Interrupting(records, "list_scores", 2), ORG_A, folder)  # type: ignore[arg-type]
    progress = json.loads((folder / PROGRESS).read_text(encoding="utf-8"))
    assert not (folder / MANIFEST).exists()
    assert (folder / "case_results.jsonl").stat().st_size > progress["sizes"]["case_results.jsonl"]
    # Stopped again on the second page of traces, after the first was written.
    with pytest.raises(Interrupted):
        await export_data(Interrupting(store, "get_trace", 3), records, ORG_A, folder)  # type: ignore[arg-type]
    assert json.loads((folder / PROGRESS).read_text(encoding="utf-8"))["cursor"] is not None

    resumed = await export_data(store, records, ORG_A, folder)
    assert resumed.resumed and not (folder / PROGRESS).exists()
    for name in FILES:
        assert (folder / name).read_bytes() == (tmp_path / "whole" / name).read_bytes(), name
    assert resumed.manifest["files"] == whole.manifest["files"]
    records.close()


async def test_an_export_stops_at_a_trace_of_a_run_that_started_after_it(tmp_path):
    store, records = MemoryStore(), SqlRecordStore(f"sqlite:///{tmp_path / 'source.db'}")
    await populate(store, records, ORG_A, default_exists=False)
    folder = tmp_path / "export"
    with pytest.raises(Interrupted):
        await export_data(store, Interrupting(records, "list_scores", 1), ORG_A, folder)  # type: ignore[arg-type]
    # A run starts and records a trace while the export is stopped: its import would fail.
    late, trace_id = ids()
    await store.start_run(ORG_A, Run(id=late, project_id="default", source="local", state="running"))
    await records.create_run(ORG_A, Run(id=late, project_id="default", source="local", state="running"))
    await store.record_trace(ORG_A, Trace(id=trace_id, project_id="default", run_id=late, request_time=T0))
    with pytest.raises(ExportError, match=f"trace {trace_id} belongs to run {late}, which started after"):
        await export_data(store, records, ORG_A, folder)
    assert not (folder / MANIFEST).exists()
    records.close()


async def test_what_an_export_and_an_import_refuse(tmp_path):
    store, records = MemoryStore(), SqlRecordStore(f"sqlite:///{tmp_path / 'source.db'}")
    await populate(store, records, ORG_A, default_exists=False)
    folder = tmp_path / "export"
    await export_data(store, records, ORG_A, folder)

    with pytest.raises(ExportError, match="already holds a complete export"):
        await export_data(store, records, ORG_A, folder)
    (tmp_path / "busy").mkdir()
    (tmp_path / "busy" / "notes.txt").write_text("mine", encoding="utf-8")
    with pytest.raises(ExportError, match="not an empty folder"):
        await export_data(store, records, ORG_A, tmp_path / "busy")
    # An install that holds data already, here the source itself.
    with pytest.raises(ExportError, match=r"already holds data: project \S+ has datasets"):
        await import_data(store, records, ORG_A, folder)

    fresh = MemoryStore(), SqlRecordStore(f"sqlite:///{tmp_path / 'fresh.db'}")
    with (folder / "scores.jsonl").open("ab") as handle:
        handle.write(b'{"project_id":"default"}\n')
    with pytest.raises(ExportError, match="scores.jsonl is not the file the export wrote"):
        await import_data(*fresh, ORG_A, folder)
    found = manifest(folder)
    (folder / MANIFEST).write_text(json.dumps({**found, "version": 2}), encoding="utf-8")
    with pytest.raises(ExportError, match="format version 2; this SDK reads version 1"):
        await import_data(*fresh, ORG_A, folder)
    (folder / MANIFEST).unlink()
    (folder / PROGRESS).write_text("{}", encoding="utf-8")
    with pytest.raises(ExportError, match="is not finished"):
        await import_data(*fresh, ORG_A, folder)
    # Nothing was written to the fresh install.
    assert await fresh[0].list_projects(ORG_A) == []
    records.close()
    fresh[1].close()


def test_the_commands_export_and_import_a_data_folder(tmp_path):
    home, fresh = tmp_path / "home", tmp_path / "fresh"
    import_inspect_logs([INSPECT / "glm5.json"], home)
    out = tmp_path / "out"

    exported = CliRunner().invoke(app, ["evals", "export", str(out), "--data-dir", str(home)])
    assert exported.exit_code == 0, exported.output
    assert f"Exported {home} into {out}: 1 project, 0 dataset versions, 1 run, 4 case results" in exported.output
    imported = CliRunner().invoke(app, ["evals", "import", "martian", str(out), "--data-dir", str(fresh)])
    assert imported.exit_code == 0, imported.output
    assert f"Imported into {fresh}: 1 project, 0 dataset versions, 1 run, 4 case results" in imported.output
    assert f"Open them in the local Evals UI: martian ui --data-dir {fresh}" in imported.output

    again = CliRunner().invoke(app, ["evals", "import", "martian", str(out), "--data-dir", str(fresh)])
    assert again.exit_code == 2 and "Import failed: the install already holds data" in again.output
    missing = CliRunner().invoke(app, ["evals", "export", str(tmp_path / "x"), "--data-dir", str(tmp_path / "none")])
    assert missing.exit_code == 2 and "Export failed: there is no data folder at" in missing.output
    assert not (tmp_path / "none").exists()
