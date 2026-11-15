"""The full data export: everything an organisation holds, exported, imported into a fresh install
and exported again with nothing lost, on the in-memory store and on the local edition's MLflow
store; every table of the record store and every kind of object of the two store interfaces either
exported or left out with a reason; monitors and schedules imported paused, and a threshold's
webhook secret in no export; a judge calibration's re-check exported unfinished imported failed;
prompt versions imported with their authors and times; an interrupted export resumed to the same
files; and what an import refuses."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import typing
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy import create_engine, func, select
from typer.testing import CliRunner

from martian.cli import app
from martian.evals import data_export
from martian.evals.data_export import (
    EXPORTED_KINDS,
    EXPORTED_TABLES,
    FILES,
    LEFT_OUT_KINDS,
    LEFT_OUT_TABLES,
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
from martian.evals.local_runs import INTERRUPTED
from martian.evals.manifests import checked_cases, manifest_for
from martian.evals.mlflow_import import import_mlflow_store
from martian.evals.model import (
    Case,
    CaseResult,
    Counts,
    DatasetDraft,
    PanelMember,
    Policy,
    Project,
    Review,
    ReviewQuestion,
    ReviewQueue,
    Run,
    RunLimits,
    Score,
    ScorerVersion,
    ScoringJob,
    Span,
    StoreRef,
    Suite,
    Target,
    Trace,
    suite_digest,
)
from martian.evals.outbox import save_queue_item
from martian.evals.records import (
    RECORD_METHODS,
    Alert,
    CalibrationRecheck,
    EmailChannel,
    JudgeCalibration,
    LabelledItem,
    ModelChange,
    ModelWatch,
    ModelWatchProgress,
    Monitor,
    MonitoredTrace,
    MonitorFilter,
    MonitorProgress,
    MonitorScores,
    Observation,
    RecordStore,
    SavedView,
    Schedule,
    ScheduledRun,
    ScheduleGates,
    ScheduleProgress,
    SuiteAudit,
    Threshold,
    ThresholdStatus,
    WebhookChannel,
)
from martian.evals.records.sql import SqlRecordStore
from martian.evals.records.tables import metadata
from martian.evals.server.principal import SINGLE_USER, Principal, organisation_principal
from martian.evals.store import STORE_METHODS, EvalStore, MemoryStore
from tests.evals.store_contract import EmptyFirstPages

ORG_A = organisation_principal("user-a", "org-a", frozenset({"admin"}))
ORG_B = organisation_principal("user-b", "org-b", frozenset({"admin"}))
T0 = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
INSPECT = Path(__file__).resolve().parents[1] / "fixtures" / "inspect"
CANARY = "whsec-export-canary-" + "5d1e9b" * 4
"""The webhook secret of the threshold the export test fills in: no file of an export may hold it."""

# What the receiving store assigns itself for what it creates (docs/data-export.md): left out of
# the comparison. Every other field of every object must come back as it was exported.
REASSIGNED = {
    "projects.jsonl": ("project", {"created_at"}),
    "datasets.jsonl": ("dataset", {"created_at", "created_by"}),
    "drafts.jsonl": ("draft", {"revision", "updated_at", "updated_by"}),
    "views.jsonl": ("view", {"revision", "updated_at"}),
    "manifests.jsonl": ("manifest", {"published_at"}),
    # An import creates monitors and schedules paused, whatever they were (checked on its own).
    "monitors.jsonl": ("monitor", {"revision", "updated_at", "state"}),
    "schedules.jsonl": ("schedule", {"revision", "updated_at", "state"}),
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
    # The first version's audit, which an export leaves out: it is derived (LEFT_OUT_TABLES).
    audit = SuiteAudit(
        suite_id="suite-1",
        suite_version=1,
        suite_digest=suite_digest(suite),
        dataset_name="support",
        dataset_digest=first.digest,
        flagged=False,
        result={"audit": "martian-suite-audit-v1", "cases": 3},
    )
    await records.put_suite_audit(principal, project, audit)

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
    # A person's answer named like the run's judge: a score of its own, beside the judge's.
    named_like_the_judge = Score(
        run_id=run_id,
        case_id="c0",
        scorer="politeness",
        source="human",
        source_id=principal.user_id,
        value=False,
        passed=False,
        rationale="Too curt.",
        metadata={"martian.review_criterion": "politeness"},
        created_at=T0 + timedelta(hours=4),
    )
    await records.put_scores(principal, project, [named_like_the_judge])
    # Through the outbox, as the review routes set it: a delivery the export leaves out.
    delivered = await save_queue_item(store, records, principal, project, queue.id, traced[0], "completed")
    assert delivered.state == "delivered"

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
    live: list[str] = []
    for turn in range(2):
        _, trace_id = ids()
        live.append(trace_id)
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
    await monitoring(store, records, principal, project, run_id=run_id, dataset=first, judge=judge, live=live)

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
    # The manifest that pins the version publishing wrote, and a publication that never finished.
    second_cases = (await store.read_cases(principal, project, second.id, offset=0, limit=10)).items
    pinned = manifest_for(
        str(uuid.uuid4()), project, "support", second_cases, draft_id=done.id, published_by=principal.user_id
    )
    await records.begin_publication(principal, project, pinned.model_copy(update={"started_at": T0}))
    await records.finish_publication(principal, project, pinned.id, second.id)
    abandoned = manifest_for(str(uuid.uuid4()), project, "support next", cases[:2], draft_id=draft.id)
    await records.begin_publication(principal, project, abandoned.model_copy(update={"started_at": T0}))
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

    # A judge graded by a panel of judge models, and a judge's calibrations against people's labels:
    # one saved by hand, one a re-check measured, and a re-check still queued.
    panel = ScorerVersion(
        name="consensus",
        kind="llm_judge",
        type="guidelines",
        panel=[
            PanelMember(model="openai:/glm-5"),
            PanelMember(model="openai:/glm-4", endpoint="http://127.0.0.1:4000/v1"),
        ],
        criteria={"guidelines": ["Kind."]},
    )
    await store.register_scorer(principal, project, panel)
    measured = JudgeCalibration(
        id=str(uuid.uuid4()),
        project_id=project,
        judge="politeness",
        version="2",
        model="glm-5",
        definition="d" * 64,
        labelled=2,
        items=[
            LabelledItem(run_id=run_id, case_id="c1", label=False, verdict=False),
            LabelledItem(trace_id=live[0], label="yes", verdict="no"),
        ],
        traces_examined=2,
        report={"judge": "politeness", "version": "2", "status": "too_few", "labelled": 2, "agreed": 1},
        created_by=principal.user_id,
        created_at=T0 + timedelta(days=3),
    )
    await records.save_calibration(principal, project, measured)
    again = measured.model_copy(
        update={"id": str(uuid.uuid4()), "recheck_of": measured.id, "created_at": T0 + timedelta(days=4, minutes=1)}
    )
    await records.save_calibration(principal, project, again)
    done = CalibrationRecheck(
        id=str(uuid.uuid4()),
        project_id=project,
        judge="politeness",
        version="2",
        calibration_id=measured.id,
        scorer=judge.model_copy(update={"version": "2"}),
        definition="d" * 64,
        state="completed",
        total=2,
        graded=2,
        result_id=again.id,
        requested_by=principal.user_id,
        created_at=T0 + timedelta(days=4),
        finished_at=T0 + timedelta(days=4, minutes=1),
    )
    await records.create_recheck(principal, project, done)
    queued = done.model_copy(
        update={
            "id": str(uuid.uuid4()),
            "calibration_id": again.id,
            "state": "queued",
            "graded": 0,
            "result_id": None,
            "created_at": T0 + timedelta(days=5),
            "finished_at": None,
        }
    )
    await records.create_recheck(principal, project, queued)
    await store.archive_project(principal, retired.id)

    # The organisation's prompts, added by another member: an import keeps who added each version
    # and when, and the version numbers, which a run's provenance.prompts names.
    editor = dataclasses.replace(principal, user_id="user-e")
    await store.create_prompt(editor, "support.terse", description="The terse support prompt")
    await store.add_prompt_version(editor, "support.terse", "Answer in one sentence.", message="First")
    await store.add_prompt_version(editor, "support.terse", "Answer in one short sentence.\n")
    await store.create_prompt(editor, "friendly")
    await store.add_prompt_version(editor, "friendly", "Greet the customer, then answer.")


async def monitoring(
    store: EvalStore,
    records: RecordStore,
    principal: Principal,
    project: str,
    *,
    run_id: str,
    dataset: Any,
    judge: ScorerVersion,
    live: list[str],
) -> None:
    """A monitor on the live traffic with the traces it took, a schedule of the suite with the
    slots it ran and skipped, and thresholds on both with their status and alerts, one signed with
    :data:`CANARY`."""
    day = T0 + timedelta(days=2)
    monitor = await records.create_monitor(
        principal,
        project,
        Monitor(
            id=str(uuid.uuid4()),
            project_id=project,
            name="Live politeness",
            scorers=[judge],
            sampling_rate=0.5,
            filter=MonitorFilter(state="OK", user_id="customer-7"),
            max_judge_tokens_per_day=10_000,
            created_by=principal.user_id,
            created_at=day,
        ),
    )
    await records.set_monitor_progress(
        principal,
        project,
        monitor.id,
        MonitorProgress(
            watermark=day + timedelta(minutes=5),
            day=day.date(),
            judge_tokens=40,
            examined=2,
            sampled=2,
            scored=1,
            last_pass_at=day + timedelta(minutes=6),
        ),
    )
    verdict = Score(
        trace_id=live[1],
        scorer="politeness",
        scorer_version="2",
        source="llm_judge",
        source_id="glm-5",
        value=True,
        passed=True,
        rationale="Kind.",
        metadata={"martian.monitor": monitor.id},
        created_at=day + timedelta(minutes=6),
    )
    logged = await store.log_score(principal, project, live[1], verdict)
    for taken in (
        MonitoredTrace(
            monitor_id=monitor.id,
            trace_id=live[1],
            state="scored",
            pass_id="pass-1",
            tokens=40,
            scores=[verdict.model_copy(update={"store": logged.store})],
            created_at=day + timedelta(minutes=5),
            finished_at=day + timedelta(minutes=6),
        ),
        # Taken by a pass that stopped before it stored its scores.
        MonitoredTrace(
            monitor_id=monitor.id,
            trace_id=live[0],
            state="scoring",
            pass_id="pass-2",
            created_at=day + timedelta(hours=1),
        ),
    ):
        await records.finish_monitored_trace(principal, project, taken)

    schedule = await records.create_schedule(
        principal,
        project,
        Schedule(
            id=str(uuid.uuid4()),
            project_id=project,
            name="Nightly support",
            cron="0 2 * * *",
            on_model_change=ModelWatch(source="runs"),
            suite_id="suite-1",
            suite_version=2,
            suite_name="Support",
            scorers=[judge],
            policies=[Policy(scorer="politeness", threshold=0.5)],
            target=Target(kind="agent", agent_id="support-agent"),
            dataset_name="support",
            dataset_id=dataset.id,
            dataset_digest=dataset.digest,
            max_cases=3,
            limits=RunLimits(max_seconds=600),
            created_by=principal.user_id,
            created_at=T0 - timedelta(days=1),
        ),
    )
    await records.set_schedule_progress(
        principal,
        project,
        schedule.id,
        ScheduleProgress(
            next_slot=T0 + timedelta(days=2, hours=2),
            last_slot=T0 + timedelta(days=1),
            last_turn_at=T0 + timedelta(days=1, seconds=3),
            model_watch=ModelWatchProgress(model="glm-5", watermark=T0 + timedelta(hours=1), observed=1),
        ),
    )
    await records.add_scheduled_run(
        principal,
        project,
        ScheduledRun(schedule_id=schedule.id, slot=T0, state="admitted", run_id=run_id, created_at=T0),
    )
    await records.set_scheduled_run_gate(principal, project, schedule.id, T0, "passed")
    change = ModelChange(previous="glm-4", current="glm-5", at=T0 + timedelta(hours=20))
    await records.add_scheduled_run(
        principal,
        project,
        ScheduledRun(
            schedule_id=schedule.id,
            slot=T0 + timedelta(days=1),
            state="skipped",
            reason="the target is not registered",
            missed=1,
            trigger="model_change",
            model_change=change,
            created_at=T0 + timedelta(days=1, seconds=3),
        ),
    )

    on_monitor = Threshold(
        id=str(uuid.uuid4()),
        project_id=project,
        name="Live politeness pass rate",
        source=MonitorScores(monitor_id=monitor.id, scorer="politeness"),
        measure="pass_rate",
        direction="at_least",
        value=0.9,
        window_seconds=3600,
        min_count=1,
        webhook=WebhookChannel(url="https://alerts.example.com/martian"),
        email=EmailChannel(to=["oncall@example.com"]),
        created_by=principal.user_id,
        created_at=day,
    )
    await records.create_threshold(principal, project, on_monitor, webhook_secret=CANARY)
    seen = Observation(
        at=day + timedelta(minutes=7),
        window_start=day - timedelta(minutes=53),
        count=1,
        estimate=0.0,
        interval=[0.0, 0.79],
        verdict="breach",
    )
    alerts = [
        Alert(
            id=str(uuid.uuid4()),
            threshold_id=on_monitor.id,
            channel=channel,
            event="breached",
            sequence=1,
            payload={"type": "threshold.breached", "sequence": 1, "threshold": {"id": on_monitor.id}},
            created_at=seen.at,
            next_attempt_at=seen.at,
        )
        for channel in ("webhook", "email")
    ]
    breached = ThresholdStatus(state="breached", sequence=1, since=seen.at, last=seen)
    await records.set_threshold_status(principal, project, on_monitor.id, breached, expected_sequence=0, alerts=alerts)
    sent = seen.at + timedelta(seconds=1)
    await records.finish_alert(
        principal,
        project,
        alerts[0].model_copy(
            update={"state": "delivered", "attempts": 1, "next_attempt_at": None, "delivered_at": sent}
        ),
    )
    await records.finish_alert(
        principal,
        project,
        alerts[1].model_copy(
            update={"attempts": 1, "last_error": "SMTP 451", "next_attempt_at": sent + timedelta(minutes=1)}
        ),
    )
    on_gates = Threshold(
        id=str(uuid.uuid4()),
        project_id=project,
        name="Nightly gate pass rate",
        source=ScheduleGates(schedule_id=schedule.id),
        measure="pass_rate",
        direction="at_least",
        value=0.5,
        window_seconds=7 * 86400,
        min_count=1,
        created_at=T0,
    )
    await records.create_threshold(principal, project, on_gates, webhook_secret=None)


def lines_of(folder: Path, name: str) -> list[dict[str, Any]]:
    return [json.loads(raw) for raw in (folder / name).read_text(encoding="utf-8").splitlines()]


def assert_monitoring_comes_back_paused_and_unsigned(first: Path, again: Path, imported: Any) -> None:
    """Monitors and schedules active in the export come back paused, with nothing queued for them,
    and the threshold with a webhook without its secret, which no file of the export holds."""
    for path in first.rglob("*"):
        if path.is_file():
            assert CANARY.encode() not in path.read_bytes(), path.name
    active = []
    for name, key in (("monitors.jsonl", "monitor"), ("schedules.jsonl", "schedule")):
        assert [line[key]["state"] for line in lines_of(first, name)] == ["active"], name
        assert [line[key]["state"] for line in lines_of(again, name)] == ["paused"], name
        active += [line[key]["id"] for line in lines_of(first, name)]
    assert imported.paused == tuple(active)
    signed = [line["threshold"]["id"] for line in lines_of(first, "thresholds.jsonl") if line["threshold"]["webhook"]]
    assert len(signed) == 1 and imported.needs_secret == tuple(signed)


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
            if name == "rechecks.jsonl":
                # An import fails a re-check exported queued or running, at the time of the import.
                recheck = line["recheck"]
                if recheck["state"] in data_export.UNFINISHED:
                    recheck.update(state="failed", error=data_export.RECHECK_INTERRUPTED)
                if recheck["error"] == data_export.RECHECK_INTERRUPTED:
                    recheck.pop("finished_at")
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


def table_rows(database: Path, space: str) -> dict[str, int]:
    """How many rows of one organisation (or the single local user) each table of the record
    store's database holds."""
    engine = create_engine(f"sqlite:///{database}")
    try:
        with engine.connect() as connection:
            return {
                table.name: connection.execute(
                    select(func.count()).select_from(table).where(table.c.space == space)
                ).scalar_one()
                for table in metadata.sorted_tables
            }
    finally:
        engine.dispose()


def assert_tables_round_trip(source: Path, target: Path, space: str) -> None:
    """Every table the export carries holds rows in the source, and as many in the fresh install;
    the outbox's deliveries stay behind, and the import queued nothing, for a run, a monitor or a
    schedule."""
    before, after = table_rows(source, space), table_rows(target, space)
    assert {name: before[name] > 0 for name in EXPORTED_TABLES} == dict.fromkeys(EXPORTED_TABLES, True)
    assert {name: after[name] for name in EXPORTED_TABLES} == {name: before[name] for name in EXPORTED_TABLES}
    assert (before["martian_deliveries"] > 0, after["martian_deliveries"]) == (True, 0)
    assert after["martian_work_queue"] == 0


def models_in(hint: Any) -> set[str]:
    """The names of the model classes a type hint names, with unions, lists and generic models
    such as ``Page[Run]`` opened up."""
    found: set[str] = set()
    if isinstance(hint, type) and issubclass(hint, BaseModel):
        generic = hint.__pydantic_generic_metadata__
        found.add((generic["origin"] or hint).__name__)
        for arg in generic["args"]:
            found |= models_in(arg)
    for arg in typing.get_args(hint):
        found |= models_in(arg)
    return found


def interface_kinds(interface: type, methods: frozenset[str]) -> set[str]:
    """Every model class a store interface's methods take or return."""
    found: set[str] = set()
    for name in methods:
        for hint in typing.get_type_hints(getattr(interface, name), localns={"Principal": Principal}).values():
            found |= models_in(hint)
    return found


def test_every_table_and_kind_of_object_is_exported_or_left_out_with_a_reason():
    # A table or a kind of object added to the record store or the store fails here until the
    # export writes it (a new file, so a new format version) or names it as left out, and why.
    tables = {table.name for table in metadata.sorted_tables}
    unlisted = tables - set(EXPORTED_TABLES) - set(LEFT_OUT_TABLES)
    assert not unlisted, f"the export neither writes nor leaves out the record store's tables {sorted(unlisted)}"
    assert set(EXPORTED_TABLES) | set(LEFT_OUT_TABLES) == tables, "the export names tables the record store lacks"
    assert not set(EXPORTED_TABLES) & set(LEFT_OUT_TABLES)

    kinds = interface_kinds(EvalStore, STORE_METHODS) | interface_kinds(RecordStore, RECORD_METHODS)
    unlisted = kinds - set(EXPORTED_KINDS) - set(LEFT_OUT_KINDS)
    assert not unlisted, f"the export neither writes nor leaves out the stores' kinds of object {sorted(unlisted)}"
    assert set(EXPORTED_KINDS) | set(LEFT_OUT_KINDS) == kinds, "the export names kinds the stores lack"
    assert not set(EXPORTED_KINDS) & set(LEFT_OUT_KINDS)

    # What it writes, it writes in a file of the format; what it leaves out has a reason.
    written = set(EXPORTED_TABLES.values()) | {name for names in EXPORTED_KINDS.values() for name in names}
    assert written <= set(FILES)
    assert all(reason.strip() for reason in [*LEFT_OUT_TABLES.values(), *LEFT_OUT_KINDS.values()])


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

    assert_monitoring_comes_back_paused_and_unsigned(tmp_path / "first", tmp_path / "again", imported)
    assert_round_trip(tmp_path / "first", tmp_path / "again", imported.references)
    assert_tables_round_trip(tmp_path / "source.db", tmp_path / "target.db", "org-a")
    assert "b-run" not in (tmp_path / "first" / "runs.jsonl").read_text(encoding="utf-8")
    # A suite version's audit is left out: the fresh install has none until it audits again.
    assert await source_records.get_suite_audit(ORG_A, "default", "suite-1", 1) is not None
    assert await target_records.get_suite_audit(ORG_A, "default", "suite-1", 1) is None
    assert again.lines("runs.jsonl") == 2
    # The person's answer named like the run's judge comes back beside the judge's own score.
    for folder in ("first", "again"):
        named = sorted(
            (line["score"]["source"], line["score"]["value"])
            for line in lines_of(tmp_path / folder, "scores.jsonl")
            if (line["score"]["case_id"], line["score"]["scorer"], line["score"]["scoring_job_id"])
            == ("c0", "politeness", None)
        )
        assert named == [("human", False), ("llm_judge", True)], folder
    # The re-check exported queued came back failed, so none blocks the next re-check of its judge.
    before = [recheck.state for recheck in await source_records.list_rechecks(ORG_A, "default")]
    after = await target_records.list_rechecks(ORG_A, "default")
    assert before == ["queued", "completed"]
    assert [(r.state, r.error) for r in after] == [("failed", data_export.RECHECK_INTERRUPTED), ("completed", "")]
    # Prompt versions keep their numbers, who added them (not the importing user) and when.
    kept = await target.prompt_versions(ORG_A, "support.terse")
    assert kept == await source.prompt_versions(ORG_A, "support.terse")
    assert [(v.version, v.created_by) for v in kept] == [(1, "user-e"), (2, "user-e")]
    # The receiving store's dataset ids are new, and the references to them follow, the schedule's too.
    assert set(imported.references["dataset_id"]) != set(imported.references["dataset_id"].values())
    # The published version is still pinned by its manifest: a run reads it only as published.
    published = [d for d in await target.list_datasets(ORG_A, "default") if d.version == 2]
    assert (await target_records.manifest_of(ORG_A, "default", published[0].id)) is not None
    assert len(await checked_cases(target, target_records, ORG_A, "default", published[0])) == 3
    [schedule] = await target_records.list_schedules(ORG_A, "default")
    assert await target.get_dataset(ORG_A, "default", schedule.dataset_id) is not None
    # The threshold's webhook signs nothing until its secret is given again, once.
    [webhook] = imported.needs_secret
    assert await source_records.threshold_secret(ORG_A, "default", webhook) == CANARY
    assert await target_records.threshold_secret(ORG_A, "default", webhook) is None
    assert not await target_records.threshold_signed(ORG_A, "default", webhook)
    assert await target_records.set_threshold_secret(ORG_A, "default", webhook, "a-new-secret-of-yours")
    assert await target_records.threshold_signed(ORG_A, "default", webhook)
    assert not await target_records.set_threshold_secret(ORG_A, "default", webhook, "another-secret-of-yours")
    assert await target_records.threshold_secret(ORG_A, "default", webhook) == "a-new-secret-of-yours"
    source_records.close()
    target_records.close()


def served(home: Path, ui: Path) -> dict[str, Any]:
    """What the local Evals UI shows of every run and review, without the ids the store assigns.
    Opening the data folder finishes a run nothing is working on as interrupted, so the run an
    export holds queued is shown ``failed`` with that error, and the moment it was finished (which
    each data folder stamps for itself) is left out."""

    def without_store_ids(value: Any) -> Any:
        if isinstance(value, list):
            return [without_store_ids(item) for item in value]
        if isinstance(value, dict):
            interrupted = value.get("error") == INTERRUPTED
            return {
                k: without_store_ids(v)
                for k, v in value.items()
                if k not in ("dataset_id", "native_id", "evals_assessment_id")
                and not (interrupted and k in ("finished_at", "updated_at"))
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

    assert_monitoring_comes_back_paused_and_unsigned(tmp_path / "first", tmp_path / "again", imported)
    assert_round_trip(tmp_path / "first", tmp_path / "again", imported.references)
    assert_tables_round_trip(home / "martian.db", fresh / "martian.db", "~local")
    # MLflow assigns its own experiment, run and assessment ids; every reference follows them.
    assert all(imported.references[kind] for kind in ("experiment_id", "run_id", "dataset_id", "assessment_id"))
    # The Evals UI shows the imported runs, their results, scores and reviews as it showed them.
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text("<!doctype html><title>Martian Evals</title>", encoding="utf-8")
    shown = served(home, ui)
    assert len(shown) == 2 + 2 * 4 and shown == served(fresh, ui)
    # The queued run in the export was not running anywhere: each data folder finished it as interrupted.
    assert [run["state"] for run in shown["/v1/evals"] if run["error"] == INTERRUPTED] == ["failed"]


def test_a_data_folder_copied_from_mlflow_comes_back_whole(tmp_path):
    # What `martian evals import mlflow` copied, as MLflow itself wrote it: sessions, MLflow's
    # own spans, a person's feedback and expectation, an overridden one, evaluation runs.
    from tests.evals.test_mlflow_import import write_store

    source, home = tmp_path / "mlflow" / "store", tmp_path / "home"
    source.parent.mkdir()
    write_store(source)
    import_mlflow_store(str(source / "mlflow.db"), home)

    export_local(home, tmp_path / "first")
    imported = import_local(tmp_path / "first", tmp_path / "fresh")
    export_local(tmp_path / "fresh", tmp_path / "again")
    for name in ("traces.jsonl", "runs.jsonl", "case_results.jsonl", "scores.jsonl", "datasets.jsonl"):
        assert manifest(tmp_path / "first")["files"][name]["lines"] > 0, name
    assert normalised(tmp_path / "first", imported.references) == normalised(tmp_path / "again")


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
    # Stopped again in the monitoring step, at the second threshold's alerts, after the first's.
    with pytest.raises(Interrupted):
        await export_data(store, Interrupting(records, "list_alerts", 2), ORG_A, folder)  # type: ignore[arg-type]
    progress = json.loads((folder / PROGRESS).read_text(encoding="utf-8"))
    assert (folder / "alerts.jsonl").stat().st_size > progress["sizes"]["alerts.jsonl"] == 0
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


async def test_an_export_stops_at_a_slot_whose_run_started_after_it(tmp_path):
    store, records = MemoryStore(), SqlRecordStore(f"sqlite:///{tmp_path / 'source.db'}")
    await populate(store, records, ORG_A, default_exists=False)
    folder = tmp_path / "export"
    with pytest.raises(Interrupted):
        await export_data(store, Interrupting(records, "list_scores", 1), ORG_A, folder)  # type: ignore[arg-type]
    # The schedule's turn admits a run while the export is stopped: the export would not hold it.
    late, _ = ids()
    await records.create_run(ORG_A, Run(id=late, project_id="default", source="console", state="queued"))
    [schedule] = await records.list_schedules(ORG_A, "default")
    entry = ScheduledRun(schedule_id=schedule.id, slot=T0 + timedelta(days=3), state="admitted", run_id=late)
    await records.add_scheduled_run(ORG_A, "default", entry)
    with pytest.raises(ExportError, match=f"schedule {schedule.id} started run {late}, which started after"):
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
    with pytest.raises(ExportError, match=r"already holds data: project \S+ has \w+.*Import into a fresh install"):
        await import_data(store, records, ORG_A, folder)

    fresh = MemoryStore(), SqlRecordStore(f"sqlite:///{tmp_path / 'fresh.db'}")
    with (folder / "scores.jsonl").open("ab") as handle:
        handle.write(b'{"project_id":"default"}\n')
    with pytest.raises(ExportError, match="scores.jsonl is not the file the export wrote"):
        await import_data(*fresh, ORG_A, folder)
    found = manifest(folder)
    # Version 3 was two formats, one with the dataset publications and one with the prompts, and
    # versions 4 (the judge calibrations) and 5 (a score of each source) came without the prompts:
    # version 6 holds all of them, and an export of any of those is refused.
    for older in (3, 4, 5):
        (folder / MANIFEST).write_text(json.dumps({**found, "version": older}), encoding="utf-8")
        with pytest.raises(ExportError, match=f"format version {older}; this SDK reads version 6"):
            await import_data(*fresh, ORG_A, folder)
    # A version 6 folder without the prompt files, or without the dataset publications, is refused.
    (folder / MANIFEST).write_text(json.dumps(found), encoding="utf-8")
    for name in ("prompts.jsonl", "manifests.jsonl"):
        (folder / name).rename(folder / f"{name}.away")
        with pytest.raises(ExportError, match=f"is missing {name}"):
            await import_data(*fresh, ORG_A, folder)
        (folder / f"{name}.away").rename(folder / name)
    (folder / MANIFEST).unlink()
    (folder / PROGRESS).write_text("{}", encoding="utf-8")
    with pytest.raises(ExportError, match="is not finished"):
        await import_data(*fresh, ORG_A, folder)
    # Nothing was written to the fresh install.
    assert await fresh[0].list_projects(ORG_A) == []
    records.close()
    fresh[1].close()


async def test_an_import_finds_the_install_s_traces_past_a_search_page_with_none(tmp_path):
    source, records = MemoryStore(), SqlRecordStore(f"sqlite:///{tmp_path / 'source.db'}")
    await populate(source, records, ORG_A, default_exists=False)
    folder = tmp_path / "export"
    await export_data(source, records, ORG_A, folder)
    records.close()
    # An install whose only data is one trace, behind a binding whose trace search answers a page
    # with no traces and a next_cursor before the trace, as the store interface allows.
    install, install_records = EmptyFirstPages(), SqlRecordStore(f"sqlite:///{tmp_path / 'install.db'}")
    await install.create_project(ORG_A, Project(id="elsewhere", name="Elsewhere"))
    await install.record_trace(ORG_A, Trace(id="tr-" + uuid.uuid4().hex, project_id="elsewhere", request_time=T0))
    with pytest.raises(ExportError, match="project elsewhere has traces. Import into a fresh install"):
        await import_data(install, install_records, ORG_A, folder)
    assert [project.id for project in await install.list_projects(ORG_A)] == ["elsewhere"]
    install_records.close()


def test_the_commands_export_and_import_a_data_folder(tmp_path):
    home, fresh = tmp_path / "home", tmp_path / "fresh"
    import_inspect_logs([INSPECT / "glm5.json"], home)
    _, records = open_stores(home)
    judge = ScorerVersion(name="politeness", kind="llm_judge", type="guidelines", model="glm-5")
    monitor = Monitor(id=str(uuid.uuid4()), project_id="default", name="Live", scorers=[judge], sampling_rate=1)
    signed = Threshold(
        id=str(uuid.uuid4()),
        project_id="default",
        name="Live pass rate",
        source=MonitorScores(monitor_id=monitor.id, scorer="politeness"),
        measure="pass_rate",
        direction="at_least",
        value=0.9,
        window_seconds=3600,
        min_count=1,
        webhook=WebhookChannel(url="https://alerts.example.com/martian"),
    )
    asyncio.run(records.create_monitor(SINGLE_USER, "default", monitor))
    asyncio.run(records.create_threshold(SINGLE_USER, "default", signed, webhook_secret=CANARY))
    records.close()
    out = tmp_path / "out"

    exported = CliRunner().invoke(app, ["evals", "export", str(out), "--data-dir", str(home)])
    assert exported.exit_code == 0, exported.output
    assert f"Exported {home} into {out}: 1 project, 0 dataset versions, 1 run, 4 case results" in exported.output
    imported = CliRunner().invoke(app, ["evals", "import", "martian", str(out), "--data-dir", str(fresh)])
    assert imported.exit_code == 0, imported.output
    assert f"Imported into {fresh}: 1 project, 0 dataset versions, 1 run, 4 case results" in imported.output
    assert "Monitors and schedules come back paused (active in the export: 1)" in imported.output
    assert "Webhook secrets are never exported (thresholds with a webhook: 1)" in imported.output
    assert f"Open them in the local Evals UI: martian ui --data-dir {fresh}" in imported.output

    again = CliRunner().invoke(app, ["evals", "import", "martian", str(out), "--data-dir", str(fresh)])
    assert again.exit_code == 2 and "Import failed: the install already holds data" in again.output
    missing = CliRunner().invoke(app, ["evals", "export", str(tmp_path / "x"), "--data-dir", str(tmp_path / "none")])
    assert missing.exit_code == 2 and "Export failed: there is no data folder at" in missing.output
    assert not (tmp_path / "none").exists()
