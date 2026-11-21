"""Review archives preserve decisions over the destination's saved evidence."""

import json
from datetime import timedelta

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from martian.cli import app
from martian.evals.local import create_local_app
from tests.redteam.test_local_findings import BASE, ORIGIN, push, ui_folder
from tests.redteam.test_remediation_verification import saved_scans


def cli(folder, command, *args, code=0):
    result = CliRunner().invoke(app, ["redteam", "findings", command, "--data-dir", str(folder), *map(str, args)])
    assert result.exit_code == code, result.output
    return result.output


def test_transfer_triage_preserves_verified_fix_and_recurrence(tmp_path):
    source, destination = tmp_path / "source", tmp_path / "destination"
    ui = ui_folder(tmp_path)
    archive = tmp_path / "history.json"
    baseline, follow_up, _ = saved_scans()
    late = baseline.model_copy(
        update={
            "id": "observed-again",
            "started_at": follow_up.started_at + timedelta(hours=1),
            "finished_at": follow_up.finished_at + timedelta(hours=1),
        }
    )
    with TestClient(create_local_app(source, ui), base_url=ORIGIN) as client:
        push(client, baseline)
        push(client, follow_up)
        fix = json.loads(cli(source, "record-fix", baseline.id, follow_up.id, baseline.findings[0].id))
        finding_id = fix["finding_id"]
        note = json.loads(cli(source, "triage", finding_id, "--severity", "high", "--comment", "Reviewed locally"))
        cli(source, "export-triage", "-o", archive, "--finding-id", finding_id)

    original = archive.read_text()
    assert archive.stat().st_mode & 0o777 == 0o600
    assert "synthetic-private" not in original
    url = f"{BASE}/findings/{finding_id}"
    with TestClient(create_local_app(destination, ui), base_url=ORIGIN) as client:
        for run in (baseline, follow_up, late):
            push(client, run)
        report_url = f"{BASE}/runs/{baseline.id}/remediation"
        before = client.get(report_url).content
        assert "different project" in cli(destination, "import-triage", archive, "--project-id", "other", code=2)
        archive.write_text(original.replace("Reviewed locally", "Changed comment"))
        assert "checksum does not match" in cli(destination, "import-triage", archive, code=2)
        archive.write_text(original)
        preview = json.loads(cli(destination, "import-triage", archive, "--dry-run"))
        assert preview == {"project_id": "default", "findings": 1, "added": 2, "reused": 0, "dry_run": True}
        assert client.get(url).json()["notes"] == []
        assert json.loads(cli(destination, "import-triage", archive)) == {**preview, "dry_run": False}
        retry = json.loads(cli(destination, "import-triage", archive))
        assert retry["added"] == 0 and retry["reused"] == 2
        assert client.get(report_url).content == before

    with TestClient(create_local_app(destination, ui), base_url=ORIGIN) as client:
        restored = client.get(url).json()
        assert restored["notes"] == [fix["note"], note]
        assert restored["finding"]["status"] == "fixed"
        assert restored["finding"]["severity"] == "high"
        assert restored["finding"]["seen_after_fix"] is True


def test_archive_requires_destination_observations_and_matching_proof(tmp_path):
    source, destination = tmp_path / "source", tmp_path / "destination"
    ui = ui_folder(tmp_path)
    archive = tmp_path / "history.json"
    baseline, follow_up, _ = saved_scans()
    with TestClient(create_local_app(source, ui), base_url=ORIGIN) as client:
        push(client, baseline)
        push(client, follow_up)
        fix = json.loads(cli(source, "record-fix", baseline.id, follow_up.id, baseline.findings[0].id))
        finding_id = fix["finding_id"]
        cli(source, "export-triage", "-o", archive)

    with TestClient(create_local_app(destination, ui), base_url=ORIGIN) as client:
        assert "matching scan observation" in cli(destination, "import-triage", archive, code=2)
        push(client, baseline)
        assert "run not found" in cli(destination, "import-triage", archive, code=2)
        changed = follow_up.model_copy(update={"attempts": list(follow_up.attempts)})
        changed.attempts[0] = changed.attempts[0].model_copy(update={"task_completed": False})
        push(client, changed)
        assert "did not verify the benign task" in cli(destination, "import-triage", archive, code=2)
        assert client.get(f"{BASE}/findings/{finding_id}").json()["notes"] == []
