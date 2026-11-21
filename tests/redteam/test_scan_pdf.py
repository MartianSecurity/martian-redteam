"""The CLI and local workbench export the same inert scan document."""

import asyncio
import subprocess
import sys
from io import BytesIO

from fastapi.testclient import TestClient
from pypdf import PdfReader
from typer.testing import CliRunner

from martian.cli import app
from martian.evals.local import create_local_app
from martian.redteam.store import RedteamStore
from tests.redteam.test_local_findings import BASE, ORIGIN, push, scan, ui_folder


def test_saved_scan_pdf_agrees_with_cli_and_keeps_evidence_private(tmp_path, monkeypatch):
    monkeypatch.setenv("MARTIAN_REPORT_INCLUDE_CONTENT", "1")
    run = scan("library-review", project="library", state="partial")
    label = 'Café & <a href="https://example.org">hours</a>'
    run.probes[0] = run.probes[0].model_copy(
        update={"vulnerability": "promptfoo:policy", "metadata": {"promptfoo": {"policy_name": label}}}
    )
    run.findings[0] = run.findings[0].model_copy(update={"vulnerability": "promptfoo:policy"})
    source = tmp_path / "scan.json"
    source.write_text(run.model_dump_json())
    output = tmp_path / "reports"
    result = CliRunner().invoke(app, ["redteam", "report", str(source), "--pdf", "-o", str(output)])
    assert result.exit_code == 0, result.output
    pdf = output / "redteam-report.pdf"
    assert pdf.stat().st_mode & 0o777 == 0o600

    url = f"{BASE}/runs/{run.id}/report.pdf"
    with TestClient(create_local_app(tmp_path / "data", ui_folder(tmp_path)), base_url=ORIGIN) as client:
        push(client, run)
        assert client.get(url).status_code == 404
        params = {"project_id": run.project_id}
        downloaded = client.get(url, params=params)
        assert downloaded.status_code == 200
        assert downloaded.headers["content-type"] == "application/pdf"
        assert downloaded.headers["content-disposition"] == 'attachment; filename="redteam-report.pdf"'
        assert downloaded.headers["cache-control"] == "no-store"
        assert downloaded.content == pdf.read_bytes()
        findings = client.get(f"{BASE}/findings", params=params).json()
        reviewed = client.post(
            f"{BASE}/findings/{findings['items'][0]['id']}/triage",
            params=params,
            json={"comment": "Private reviewer note", "status": "fixed"},
        )
        assert reviewed.status_code == 201
        assert client.get(url, params=params).content == downloaded.content

    reader = PdfReader(BytesIO(downloaded.content))
    text = " ".join(" ".join(page.extract_text().split()) for page in reader.pages)
    assert "Scan library-review" in text and "Project library" in text
    assert "1 confirmed" in text and "1 suspected" in text
    assert "Scan coverage is incomplete" in text and "Prioritized remediation" in text
    assert "Café & <a" in text and all(not page.get("/Annots") for page in reader.pages)
    assert "synthetic-private" not in text and "Private reviewer note" not in text


def test_scan_pdf_refuses_active_scan(tmp_path):
    run = scan("in-progress", state="running").model_copy(update={"finished_at": None})
    source = tmp_path / "running.json"
    source.write_text(run.model_dump_json())
    output = tmp_path / "reports"
    cli = CliRunner().invoke(app, ["redteam", "report", str(source), "--pdf", "-o", str(output)])
    assert cli.exit_code == 2 and "when this scan finishes" in cli.output
    assert not output.exists()

    home = tmp_path / "data"
    with TestClient(create_local_app(home, ui_folder(tmp_path)), base_url=ORIGIN) as client:
        records = RedteamStore.local(home)
        try:
            asyncio.run(records.save(run))
        finally:
            records.close()
        refused = client.get(f"{BASE}/runs/{run.id}/report.pdf")
        assert refused.status_code == 409 and "when this scan finishes" in refused.json()["detail"]


def test_saved_file_report_works_without_optional_dependencies(tmp_path):
    run = tmp_path / "saved-scan.json"
    run.write_text(scan("offline-review").model_dump_json())
    script = """
from importlib.abc import MetaPathFinder
import sys

class BaseInstall(MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {"mlflow", "fastapi", "reportlab"}:
            raise ModuleNotFoundError(fullname, name=fullname)

sys.meta_path.insert(0, BaseInstall())
from martian.cli import app
app()
"""
    command = [sys.executable, "-c", script, "redteam", "report", str(run)]
    plain = tmp_path / "plain"
    result = subprocess.run([*command, "-o", str(plain)], capture_output=True, text=True, timeout=15, check=False)
    assert result.returncode == 0, result.stderr
    assert (plain / "redteam-report.json").is_file()
    pdf = tmp_path / "pdf"
    result = subprocess.run(
        [*command, "--pdf", "-o", str(pdf)], capture_output=True, text=True, timeout=15, check=False
    )
    assert result.returncode == 2 and "require martian-sdk[evals]" in result.stderr
    assert not pdf.exists()
