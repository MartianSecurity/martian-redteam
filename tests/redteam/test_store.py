"""A red team run is stored as an eval run, on SQLite and Postgres from one schema (martian-notes#228)."""

import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from martian.evals.model import CaseResult, Run
from martian.evals.records import RedteamRecords
from martian.evals.records.sql import SqlRecordStore
from martian.evals.server.principal import Principal
from martian.evals.store import StoreError
from martian.redteam.model import Evidence, Verification
from martian.redteam.model.maps import from_risk_assessment
from martian.redteam.store import UNDETERMINED, RedteamStore
from martian_redteam.types import RiskAssessment

POSTGRES_URL = os.environ.get("MARTIAN_TEST_POSTGRES_URL")
needs_postgres = pytest.mark.skipif(not POSTGRES_URL, reason="set MARTIAN_TEST_POSTGRES_URL to run on Postgres")
FIXTURES = Path(__file__).parent / "fixtures"
ORG = Principal(user_id="u1", organisation_id="org-a", roles=frozenset({"owner"}))
OTHER = Principal(user_id="u2", organisation_id="org-b", roles=frozenset({"owner"}))


def a_run(run_id: str = "rt-1", project_id: str = "p1"):
    report = RiskAssessment.model_validate_json((FIXTURES / "risk-assessment-0.2.json").read_text("utf-8"))
    run = from_risk_assessment(report, run_id=run_id, project_id=project_id)
    first = run.attempts[0]
    evidence = Evidence(id="ev-1", kind="reply", tier="text", source="target", content=first.output)
    verification = Verification(probe_id=first.probe_id, outcome="unverifiable", summary="judged only")
    return run.model_copy(update={"evidence": [evidence], "verifications": [verification]})


@pytest.fixture
def schema():
    name = f"martian_rt_{uuid.uuid4().hex[:10]}"
    yield name
    if POSTGRES_URL:
        engine = create_engine(POSTGRES_URL)
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{name}" CASCADE'))
        engine.dispose()


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=needs_postgres)])
def records(request, tmp_path, schema):
    url = f"sqlite:///{tmp_path / 'martian.db'}" if request.param == "sqlite" else POSTGRES_URL
    store = SqlRecordStore(url, schema=schema)
    yield store
    store.close()


async def test_a_red_team_run_reads_back_as_it_was_saved(records):
    store = RedteamStore(records)
    run = a_run()
    await store.save(run, principal=ORG)
    assert await store.get("p1", "rt-1", principal=ORG) == run


async def test_its_attempts_are_case_results_and_its_checks_are_scores(records):
    await RedteamStore(records).save(a_run(), principal=ORG)
    stored = await records.get_run(ORG, "p1", "rt-1")
    assert stored is not None and stored.kind == "redteam"
    assert stored.counts.cases == 6 and stored.counts.failed == 2 and stored.counts.errors == 2
    results = (await records.list_results(ORG, "p1", "rt-1")).items
    assert [result.status for result in results] == ["failed", "passed", "failed", "error", "error", "passed"]
    scores = await records.list_scores(ORG, "p1", "rt-1", case_id=results[0].case_id)
    assert [(score.scorer, score.source, score.value, score.passed) for score in scores] == [
        ("judge", "llm_judge", True, False)
    ]


async def test_an_undetermined_attempt_is_an_error_case_never_a_pass(records):
    run = a_run()
    undecided = run.attempts[1].model_copy(update={"outcome": "undetermined", "check_results": []})
    run = run.model_copy(update={"attempts": [run.attempts[0], undecided, *run.attempts[2:]]})
    await RedteamStore(records).save(run, principal=ORG)
    results = (await records.list_results(ORG, "p1", "rt-1")).items
    assert (results[1].status, results[1].error) == ("error", UNDETERMINED)
    assert (await RedteamStore(records).get("p1", "rt-1", principal=ORG)).attempts[1].outcome == "undetermined"


async def test_eval_runs_and_red_team_runs_in_one_project_do_not_mix_in_listings(records):
    await records.create_run(ORG, Run(id="eval-1", project_id="p1", source="local", state="completed"))
    await RedteamStore(records).save(a_run(), principal=ORG)
    assert [run.id for run in (await records.list_runs(ORG, "p1")).items] == ["eval-1"]
    assert [run.id for run in (await RedteamStore(records).list("p1", principal=ORG)).items] == ["rt-1"]
    assert {run.id for run in (await records.list_runs(ORG, "p1", kind=None)).items} == {"eval-1", "rt-1"}


async def test_an_eval_run_is_written_without_a_kind(records):
    saved = await records.create_run(ORG, Run(id="eval-1", project_id="p1", source="local", state="completed"))
    assert "kind" not in saved.model_dump()
    assert Run.model_validate(saved.model_dump()).kind == "eval"


async def test_an_organisation_does_not_see_another_organisations_red_team_run(records):
    await RedteamStore(records).save(a_run(), principal=ORG)
    assert await RedteamStore(records).get("p1", "rt-1", principal=OTHER) is None
    assert (await RedteamStore(records).list("p1", principal=OTHER)).items == []


async def test_only_a_red_team_run_takes_red_team_records(records):
    await records.create_run(ORG, Run(id="eval-1", project_id="p1", source="local", state="completed"))
    with pytest.raises(StoreError) as caught:
        await records.put_redteam_records(ORG, "p1", "eval-1", RedteamRecords())
    assert caught.value.kind == "invalid"
    with pytest.raises(StoreError) as caught:
        await records.get_redteam_records(ORG, "p1", "missing")
    assert caught.value.kind == "not_found"
    with pytest.raises(StoreError):
        await RedteamStore(records).save(a_run("eval-1"), principal=ORG)


async def test_saving_again_replaces_the_red_team_records(records):
    store = RedteamStore(records)
    run = a_run()
    await store.save(run, principal=ORG)
    changed = run.model_copy(update={"findings": run.findings[:1], "verifications": []})
    await store.save(changed, principal=ORG)
    again = await store.get("p1", "rt-1", principal=ORG)
    assert again.findings == run.findings[:1] and again.verifications == []


async def test_a_deleted_red_team_run_leaves_none_of_its_records(records):
    await RedteamStore(records).save(a_run(), principal=ORG)
    await records.put_results(ORG, "p1", [CaseResult(run_id="rt-1", case_id="extra", status="passed")])
    removed = await records.delete_run(ORG, "p1", "rt-1")
    assert removed["martian_redteam_probes"] == 6
    assert removed["martian_redteam_findings"] == 2
    assert removed["martian_redteam_evidence"] == 1
    assert removed["martian_redteam_verifications"] == 1
    assert await RedteamStore(records).get("p1", "rt-1", principal=ORG) is None


async def test_a_local_red_team_run_survives_a_restart(tmp_path):
    first = RedteamStore.local(tmp_path)
    try:
        await first.save(a_run(project_id="default"))
    finally:
        first.close()
    second = RedteamStore.local(tmp_path)
    try:
        assert await second.get("default", "rt-1") == a_run(project_id="default")
        assert (tmp_path / "martian.db").exists()
    finally:
        second.close()
