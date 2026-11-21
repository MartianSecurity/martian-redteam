"""Remediation verification reads saved verdicts and their coverage."""

import asyncio
import json
from datetime import timedelta, timezone
from hashlib import sha256
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from martian.cli import app
from martian.evals.local import create_local_app
from martian.evals.runner import TargetRef as HttpReference
from martian.redteam.model import Check, CheckResult, RedteamRun, TargetRef
from martian.redteam.report.verification import verify_remediation
from martian.redteam.store import RedteamStore
from martian.redteam.targets.http import HttpTarget
from tests.redteam.test_local_findings import BASE, ORIGIN, push, scan, ui_folder


def saved_scans() -> tuple[RedteamRun, RedteamRun, RedteamRun]:
    baseline = scan("baseline")
    check = Check(id="criterion", kind="text")
    task_check = Check(id="task", kind="text")
    baseline = baseline.model_copy(
        update={
            "probes": [
                probe.model_copy(update={"planned_attempts": 2, "checks": [check], "task_checks": [task_check]})
                for probe in baseline.probes
            ],
            "attempts": [
                attempt.model_copy(
                    update={
                        "id": attempt.id if number == 1 else attempt.id + "-second",
                        "number": number,
                        "fingerprint": sha256(f"fixture/{attempt.probe_id}/{number}".encode()).hexdigest(),
                        "check_results": [CheckResult(check_id="criterion", kind="text", hit=True)],
                    }
                )
                for attempt in baseline.attempts
                for number in (1, 2)
            ],
        }
    )
    clean = baseline.model_copy(
        update={
            "id": "follow-up",
            "started_at": baseline.started_at + timedelta(days=1),
            "finished_at": baseline.started_at + timedelta(days=1, minutes=1),
            "findings": [],
            "attempts": [
                attempt.model_copy(
                    update={
                        "id": "review-" + attempt.id,
                        "outcome": "resisted",
                        "level": None,
                        "check_results": [CheckResult(check_id="criterion", kind="text", hit=False)],
                        "task_completed": True,
                    }
                )
                for attempt in baseline.attempts
            ],
        }
    )
    recurrence = clean.model_copy(
        update={
            "id": "partial-coverage",
            "findings": [baseline.findings[0].model_copy(update={"attempt_ids": [clean.attempts[0].id]})],
            "attempts": [
                baseline.attempts[0].model_copy(update={"id": clean.attempts[0].id}),
                clean.attempts[1],
                clean.attempts[2],
            ],
        }
    )
    return baseline, clean, recurrence


def test_a_network_route_without_identity_cannot_group_or_verify_across_scans(tmp_path):
    baseline, clean, _ = saved_scans()
    endpoint = "https://library.example.test/catalog"
    body = {"message": "{{input}}"}
    configured = HttpReference(url=endpoint, body=body, headers={"X-Workspace": "${WORKSPACE}"})
    east = HttpTarget(configured, environ={"WORKSPACE": "east-library"})
    west = HttpTarget(configured, environ={"WORKSPACE": "west-library"})
    assert not east.findings_key and not west.findings_key
    plain = HttpTarget(HttpReference(url=endpoint, body=body))
    start = baseline.started_at

    def saved(run, *, identity, group):
        route = {"findings_target": identity} if identity else {}
        provenance = {**route, "launch": {"target_id": group}}
        return run.model_copy(update={"target": TargetRef(kind="http", name=plain.name), "provenance": provenance})

    legacy = saved(baseline, identity="unattested-route", group="saved-network")
    unknown = saved(clean, identity=west.findings_key, group="saved-network")
    observed = saved(baseline, identity=east.findings_key, group="saved-network").model_copy(
        update={
            "id": "unknown-route",
            "started_at": start + timedelta(minutes=2),
            "finished_at": start + timedelta(minutes=3),
        }
    )
    bound = saved(baseline, identity=plain.findings_key, group="public-network").model_copy(update={"id": "public"})
    matched = saved(clean, identity=plain.findings_key, group="public-network").model_copy(
        update={"id": "public-follow-up"}
    )
    with TestClient(create_local_app(tmp_path / "data", ui_folder(tmp_path)), base_url=ORIGIN) as client:
        for run in (legacy, unknown, observed, bound, matched):
            push(client, run)
        inventory = client.get(f"{BASE}/findings", params={"category": "disclosure"}).json()["items"]
        assert len(inventory) == 3 and all(row["scan_count"] == 1 for row in inventory)
        path = f"{BASE}/runs/{legacy.id}/remediation"
        history = client.get(f"{path}/follow-ups").json()
        assert history["items"] == [] and "target identity" in history["unavailable_reason"]
        assert client.get(f"{path}/verification", params={"candidate_run_id": unknown.id}).status_code == 409
        comparison = client.get(
            f"{BASE}/runs/{bound.id}/remediation/verification", params={"candidate_run_id": matched.id}
        )
        assert comparison.status_code == 200 and comparison.json()["findings"][0]["status"] == "not_observed"


def test_new_confirmed_finding_is_visible_even_when_the_original_fix_verifies(tmp_path):
    baseline, clean, _ = saved_scans()
    first, new = baseline.findings
    resolved_attempts = [attempt for attempt in clean.attempts if attempt.probe_id == new.probe_id]
    baseline = baseline.model_copy(
        update={"findings": [first], "attempts": [*baseline.attempts[:2], *resolved_attempts]}
    )
    hit = resolved_attempts[0].model_copy(
        update={
            "outcome": "hit",
            "level": first.evidence_level,
            "check_results": [CheckResult(check_id="criterion", kind="text", hit=True)],
        }
    )
    new = new.model_copy(
        update={
            "severity": "high",
            "counted_as": "confirmed",
            "evidence_level": first.evidence_level,
            "attempt_ids": [hit.id],
        }
    )
    follow_up = clean.model_copy(
        update={
            "id": "follow-up-regression",
            "findings": [new],
            "attempts": [*clean.attempts[:2], hit, clean.attempts[3]],
        }
    )

    with TestClient(create_local_app(tmp_path / "data", ui_folder(tmp_path)), base_url=ORIGIN) as client:
        push(client, baseline)
        push(client, follow_up)
        before = client.get(f"{BASE}/findings").json()
        response = client.get(
            f"{BASE}/runs/{baseline.id}/remediation/verification", params={"candidate_run_id": follow_up.id}
        )
        assert response.status_code == 200, response.text
        comparison = response.json()
        assert comparison["findings"][0]["status"] == "not_observed"
        assert comparison["baseline_summary"]["overall_risk"] == "medium"
        assert comparison["candidate_summary"]["overall_risk"] == "high"
        assert comparison["coverage"] == {
            "baseline_probes": 2,
            "candidate_probes": 2,
            "matched_probes": 2,
            "reason": None,
        }
        [change] = comparison["follow_up_findings"]
        assert (change["change"], change["counted_as"], change["severity"], change["candidate_attempt_id"]) == (
            "new",
            "confirmed",
            "high",
            hit.id,
        )
        assert "synthetic-private" not in response.text
        assert client.get(f"{BASE}/findings").json() == before

    first_path, second_path = tmp_path / "baseline.json", tmp_path / "follow-up.json"
    first_path.write_text(baseline.model_dump_json())
    second_path.write_text(follow_up.model_dump_json())
    command = ["redteam", "verify-remediation", str(first_path), str(second_path), "-o", str(tmp_path)]
    runner = CliRunner()
    assert runner.invoke(app, command).exit_code == 0
    strict = runner.invoke(app, [*command, "--fail-on-regression"])
    assert strict.exit_code == 1 and "New in follow-up" in strict.output
    assert json.loads((tmp_path / "redteam-remediation-verification.json").read_text()) == comparison
    follow_up.attempts[-1] = follow_up.attempts[-1].model_copy(update={"task_completed": False})
    second_path.write_text(follow_up.model_dump_json())
    incomplete = runner.invoke(app, [*command, "--fail-on-regression"])
    assert incomplete.exit_code == 2 and "Limited comparison coverage" in incomplete.output


def test_comparison_groups_new_confirmation_and_higher_severity_by_finding_kind():
    baseline, clean, _ = saved_scans()
    first, second = baseline.findings
    candidate = clean.model_copy(
        update={
            "probes": [probe.model_copy(update={"attack_method": "another-fixture"}) for probe in clean.probes],
            "findings": [
                first.model_copy(update={"id": "more-severe", "severity": "high"}),
                second.model_copy(update={"id": "confirmed-now", "counted_as": "confirmed", "evidence_level": 2}),
            ],
            "attempts": baseline.attempts,
        }
    )
    comparison = verify_remediation(baseline, candidate)
    assert comparison.coverage.reason == "The scans tested different probe sets or definitions."
    assert comparison.coverage.matched_probes == 0
    assert {row.finding_id: row.change for row in comparison.follow_up_findings} == {
        "more-severe": "higher_severity",
        "confirmed-now": "newly_confirmed",
    }
    assert {row.status for row in comparison.findings} == {"inconclusive"}


def test_same_finding_on_another_probe_counts_as_recurrence():
    baseline, clean, _ = saved_scans()
    finding = baseline.findings[0]
    sibling = baseline.probes[1].model_copy(
        update={
            "vulnerability": finding.vulnerability,
            "vulnerability_type": finding.vulnerability_type,
            "objective_class": finding.objective_class,
        }
    )
    probes = [baseline.probes[0], sibling]
    baseline = baseline.model_copy(
        update={"probes": probes, "findings": [finding], "attempts": [*baseline.attempts[:2], *clean.attempts[2:]]}
    )
    hit = clean.attempts[2].model_copy(update={"outcome": "hit", "level": finding.evidence_level})
    moved = clean.model_copy(
        update={
            "probes": probes,
            "findings": [finding.model_copy(update={"probe_id": sibling.id, "attempt_ids": [hit.id]})],
            "attempts": [*clean.attempts[:2], hit, clean.attempts[3]],
        }
    )
    comparison = verify_remediation(baseline, moved)
    [row] = comparison.findings
    assert row.status == "observed" and row.candidate_attempt_id == hit.id
    assert "another probe" in row.reason and comparison.coverage.reason is None
    assert comparison.follow_up_findings == []


def test_strict_gate_compares_two_clean_scans_with_matching_coverage(tmp_path):
    baseline, clean, _ = saved_scans()
    before = baseline.model_copy(update={"findings": [], "attempts": clean.attempts})
    first_path, second_path = tmp_path / "baseline.json", tmp_path / "follow-up.json"
    first_path.write_text(before.model_dump_json())
    second_path.write_text(clean.model_dump_json())
    result = CliRunner().invoke(
        app, ["redteam", "verify-remediation", str(first_path), str(second_path), "--fail-on-regression"]
    )
    assert result.exit_code == 0 and "2 of 2 baseline probes matched" in result.output
    incomplete = clean.model_copy(update={"expected_attempts": len(clean.attempts) + 1})
    second_path.write_text(incomplete.model_dump_json())
    result = CliRunner().invoke(
        app, ["redteam", "verify-remediation", str(first_path), str(second_path), "--fail-on-regression"]
    )
    assert result.exit_code == 2 and "fewer attempts than planned" in result.output


@pytest.mark.parametrize(
    ("changed", "reason"),
    [
        ({"number": 2}, "lacks a resisted verdict"),
        ({"fingerprint": None}, "lack a recorded configuration fingerprint"),
        ({"fingerprint": "1" * 64}, "attempt configurations differ"),
        ({"task_completed": False}, "did not verify the benign task"),
    ],
)
def test_absence_needs_complete_matched_attempts_and_verified_task(changed, reason):
    baseline, clean, _ = saved_scans()
    clean.attempts[0] = clean.attempts[0].model_copy(update=changed)
    result = verify_remediation(baseline, clean)
    assert result.findings[0].status == "inconclusive"
    assert reason in result.findings[0].reason
    assert result.findings[1].status == "not_observed"


def test_the_project_scoped_api_compares_saved_scans_without_modifying_reviews(tmp_path):
    baseline, clean, recurrence = saved_scans()
    legacy = clean.model_copy(update={"id": "legacy", "provenance": {}})
    rejudged = clean.model_copy(
        update={"id": "rejudged", "provenance": {**clean.provenance, "rejudged_from": clean.id}}
    )
    other = clean.model_copy(update={"id": "private", "project_id": "other"})
    with TestClient(create_local_app(tmp_path / "data", ui_folder(tmp_path)), base_url=ORIGIN) as client:
        for run in (baseline, clean, recurrence, legacy, rejudged, other):
            push(client, run)
        before = client.get(f"{BASE}/findings").json()
        url = f"{BASE}/runs/{baseline.id}/remediation/verification"
        valid = client.get(url, params={"candidate_run_id": clean.id})
        assert valid.status_code == 200, valid.text
        assert [(row["status"], row["decided_attempts"]) for row in valid.json()["findings"]] == [
            ("not_observed", 2),
            ("not_observed", 2),
        ]
        result = client.get(url, params={"candidate_run_id": recurrence.id}).json()
        assert [(row["status"], row["decided_attempts"]) for row in result["findings"]] == [
            ("observed", 2),
            ("inconclusive", 1),
        ]
        assert result["findings"][0]["candidate_attempt_id"] == recurrence.attempts[0].id
        assert "synthetic-private" not in valid.text
        for run, message in ((legacy, "recorded identity"), (rejudged, "original scans")):
            refused = client.get(url, params={"candidate_run_id": run.id})
            assert refused.status_code == 409 and message in refused.json()["detail"]
        assert client.get(url, params={"candidate_run_id": other.id}).status_code == 404
        assert client.get(f"{BASE}/findings").json() == before


def test_verified_fix_records_one_review_and_exposes_late_scan_observations(tmp_path):
    baseline, clean, _ = saved_scans()
    incomplete = clean.model_copy(update={"id": "incomplete", "state": "partial", "attempts": clean.attempts[:-1]})
    late = baseline.model_copy(
        update={
            "id": "late-arrival",
            "started_at": clean.started_at + timedelta(hours=1),
            "finished_at": clean.finished_at + timedelta(hours=1),
        }
    )
    folder = tmp_path / "data"
    ui = ui_folder(tmp_path)
    action = f"{BASE}/runs/{baseline.id}/remediation/verified-fix"
    body = {
        "finding_id": baseline.findings[1].id,
        "candidate_run_id": clean.id,
        "request_id": str(uuid4()),
    }
    with TestClient(create_local_app(folder, ui), base_url=ORIGIN) as client:
        for run in (baseline, clean, incomplete):
            push(client, run)
        original = client.get(f"{BASE}/runs/{baseline.id}/remediation").content
        refused = client.post(action, json={**body, "candidate_run_id": incomplete.id})
        assert refused.status_code == 409 and "Both scans must complete" in refused.json()["detail"]
        assert client.post(action, json=body, params={"project_id": "other"}).status_code == 404
        fixed = client.post(action, json=body)
        assert fixed.status_code == 201, fixed.text
        saved = fixed.json()
        proof = saved["note"]["verified_fix"]
        assert proof["covered_findings"] == [body["finding_id"]] and proof["planned_attempts"] == 2
        assert client.post(action, json=body).json() == saved
        different = client.post(action, json={**body, "finding_id": baseline.findings[0].id})
        assert different.status_code == 409
        assert "synthetic-private" not in fixed.text
        push(client, late)
        assert client.get(f"{BASE}/runs/{baseline.id}/remediation").content == original

    with TestClient(create_local_app(folder, ui), base_url=ORIGIN) as client:
        detail = client.get(f"{BASE}/findings/{saved['finding_id']}").json()
        assert detail["notes"] == [saved["note"]]
        assert detail["finding"]["status"] == "fixed" and detail["finding"]["seen_after_fix"] is True
        assert client.post(action, json=body).json() == saved
        obsolete = client.post(action, json={**body, "request_id": str(uuid4())})
        assert obsolete.status_code == 409 and "during or after" in obsolete.json()["detail"]


def test_verified_fix_needs_coverage_of_all_recorded_probes_for_the_finding(tmp_path):
    baseline, clean, _ = saved_scans()
    previous = baseline.model_copy(
        update={
            "id": "previous-probe",
            "started_at": baseline.started_at - timedelta(days=1),
            "finished_at": baseline.finished_at - timedelta(days=1),
            "probes": [baseline.probes[0].model_copy(update={"id": "older-probe"})],
            "attempts": [
                attempt.model_copy(update={"probe_id": "older-probe"})
                for attempt in baseline.attempts
                if attempt.probe_id == baseline.probes[0].id
            ],
            "findings": [baseline.findings[0].model_copy(update={"probe_id": "older-probe"})],
        }
    )
    with TestClient(create_local_app(tmp_path / "data", ui_folder(tmp_path)), base_url=ORIGIN) as client:
        for run in (previous, baseline, clean):
            push(client, run)
        action = f"{BASE}/runs/{baseline.id}/remediation/verified-fix"
        refused = client.post(
            action,
            json={
                "finding_id": baseline.findings[0].id,
                "candidate_run_id": clean.id,
                "request_id": str(uuid4()),
            },
        )
        assert refused.status_code == 409
        assert "does not cover every recorded probe" in refused.json()["detail"]


def test_follow_ups_use_saved_target_keys_across_pages_and_rank_by_actual_time(tmp_path):
    baseline, clean, _ = saved_scans()
    baseline = baseline.model_copy(
        update={"provenance": {"launch": {"target_id": "saved-demo"}, **baseline.provenance}}
    )
    latest_start = clean.started_at + timedelta(minutes=1)
    western = timezone(-timedelta(hours=8))
    latest = clean.model_copy(
        update={
            "id": "latest",
            "state": "stopped",
            "started_at": latest_start.astimezone(western),
            "finished_at": (latest_start + timedelta(minutes=1)).astimezone(western),
            "provenance": {"launch": {"target_id": "saved-demo"}},
        }
    )
    later_start = latest_start + timedelta(hours=1)
    later = clean.model_copy(update={"started_at": later_start, "finished_at": later_start + timedelta(minutes=1)})
    wrong_target = later.model_copy(
        update={"id": "conflicting-target", "provenance": {"launch": {"target_id": "another"}, **clean.provenance}}
    )
    replay = later.model_copy(update={"id": "replay", "provenance": {**clean.provenance, "rejudged_from": clean.id}})
    foreign = later.model_copy(update={"id": "foreign", "project_id": "private"})
    active = later.model_copy(update={"id": "active", "state": "running", "finished_at": None})
    overlapping = clean.model_copy(update={"id": "overlap", "started_at": baseline.started_at})
    invalid = later.model_copy(update={"id": "invalid", "finished_at": latest_start})
    legacy = later.model_copy(update={"id": "legacy", "provenance": {}})
    unrelated = [
        later.model_copy(
            update={"id": f"unrelated-{index:02d}", "provenance": {"findings_target": "another"}, "attempts": []}
        )
        for index in range(51)
    ]
    home = tmp_path / "data"
    store = RedteamStore.local(home)

    async def seed() -> None:
        for run in (
            baseline,
            clean,
            latest,
            wrong_target,
            replay,
            foreign,
            active,
            overlapping,
            invalid,
            legacy,
            *unrelated,
        ):
            await store.save(run)

    try:
        asyncio.run(seed())
    finally:
        store.close()

    path = f"{BASE}/runs/{baseline.id}/remediation"
    with TestClient(create_local_app(home, ui_folder(tmp_path)), base_url=ORIGIN) as client:
        first_page = {row["id"] for row in client.get("/v1/redteam/runs").json()["items"]}
        assert not {clean.id, latest.id} & first_page
        result = client.get(f"{path}/follow-ups", params={"limit": 1})
        assert result.status_code == 200, result.text
        assert result.json()["has_more"] is True
        assert [(row["id"], row["state"]) for row in result.json()["items"]] == [(latest.id, "stopped")]
        full = client.get(f"{path}/follow-ups").json()
        assert [row["id"] for row in full["items"]] == [latest.id, clean.id]
        assert full["unavailable_reason"] is None and not full["has_more"]
        assert "synthetic-private" not in result.text and "findings_target" not in result.text
        assert client.get(f"{path}/verification", params={"candidate_run_id": wrong_target.id}).status_code == 409
        assert client.get(f"{path}/follow-ups", params={"project_id": "private"}).status_code == 404
        assert client.get(f"{BASE}/runs/{active.id}/remediation/follow-ups").status_code == 409
        for run, reason in ((legacy, "target identity"), (replay, "original scan"), (invalid, "finish time")):
            reply = client.get(f"{BASE}/runs/{run.id}/remediation/follow-ups").json()
            assert not reply["items"] and reason in reply["unavailable_reason"]
