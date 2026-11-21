"""Local findings and triage commands for both SDK entry points."""

from __future__ import annotations

import asyncio
import importlib.util
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Annotated, NoReturn
from uuid import UUID, uuid4

import typer
from pydantic import TypeAdapter, ValidationError
from rich.console import Console
from rich.table import Table

from martian._privacy import safe_text

if TYPE_CHECKING:
    from martian.evals.records.sql import SqlRecordStore
    from martian.redteam.findings import InventoryPage

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Inspect, review and export the local Findings inventory.",
)
console = Console(markup=False, highlight=False)

DataDir = Annotated[
    Path | None,
    typer.Option("--data-dir", file_okay=False, help="Local data folder, as for martian ui. Uses MARTIAN_EVALS_HOME."),
]
Project = Annotated[str, typer.Option("--project-id", help="Project to read or review.")]
FindingId = Annotated[str, typer.Argument(help="Finding ID from the local inventory.")]


def _stop(message: str) -> NoReturn:
    typer.echo(safe_text(message), err=True)
    raise typer.Exit(code=2)


def _require_evals() -> None:
    if any(importlib.util.find_spec(module) is None for module in ("alembic", "fastapi", "mlflow", "sqlalchemy")):
        _stop("Local findings need martian-sdk[evals].")


@contextmanager
def _local_records(data_dir: Path | None) -> Iterator[SqlRecordStore]:
    from alembic.util import CommandError
    from sqlalchemy.exc import SQLAlchemyError

    from martian.evals.local import data_folder
    from martian.evals.records.sql import SqlRecordStore
    from martian.evals.store import StoreError

    database = data_folder(data_dir) / "martian.db"
    if not database.is_file():
        _stop("No local record store in the data folder. Choose the folder with your saved scans.")
    records = None
    try:
        records = SqlRecordStore(f"sqlite:///{database}")
        yield records
    except StoreError as error:
        _stop(error.detail)
    except (CommandError, OSError, SQLAlchemyError):
        _stop("Could not read or update the local record store. Check the data folder and its permissions.")
    finally:
        if records is not None:
            records.close()


def _invalid(error: ValidationError, *, field: str) -> NoReturn:
    issue = error.errors(include_input=False)[0]
    name = ".".join(map(str, issue["loc"])) or field
    _stop(f"{name}: {issue['msg']}")


def _table(page: InventoryPage, offset: int) -> None:
    if page.items:
        table = Table("Finding ID", "Finding", "Target", "Severity", "Status", "Evidence", "Scans")
        for row in page.items:
            table.add_row(
                row.id,
                row.title,
                row.target.name,
                row.severity or "unrated",
                row.status,
                row.counted_as,
                str(row.scan_count),
            )
        console.print(table)
    start = offset + 1 if page.items else 0
    end = offset + len(page.items) if page.items else 0
    typer.echo(f"Showing {start}–{end} of {page.total} matching findings.")


def _write_export(output: Path, content: bytes) -> None:
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=output.parent, prefix=".findings-") as staging:
            staged = Path(staging) / "export"
            staged.write_bytes(content)
            staged.chmod(0o600)
            output.hardlink_to(staged)
    except FileExistsError:
        _stop("The output file already exists. Choose a new path.")
    except OSError as error:
        _stop(f"Could not write the findings export. {error.strerror}")


@app.command("list")
def list_findings(
    data_dir: DataDir = None,
    project_id: Project = "default",
    q: Annotated[str | None, typer.Option("--search", help="Search findings, targets and methods.")] = None,
    target: Annotated[str | None, typer.Option(help="Target ID from the inventory facets.")] = None,
    status: Annotated[str | None, typer.Option(help="Filter by triage status.")] = None,
    severity: Annotated[str | None, typer.Option(help="Filter by severity, including unrated.")] = None,
    category: Annotated[str | None, typer.Option(help="Filter by risk category.")] = None,
    vulnerability_type: Annotated[str | None, typer.Option(help="Filter by vulnerability type.")] = None,
    evidence: Annotated[str | None, typer.Option(help="confirmed, attempted or suspected.")] = None,
    run_id: Annotated[str | None, typer.Option(help="Findings observed in this scan.")] = None,
    seen_after_fix: Annotated[bool, typer.Option(help="Only findings observed after a fix decision.")] = False,
    sort: Annotated[str, typer.Option(help="Sort by severity or recent observation.")] = "severity",
    offset: Annotated[int, typer.Option(min=0, help="Rows to skip in table or JSON output.")] = 0,
    limit: Annotated[int, typer.Option(min=1, max=200, help="Page size in table or JSON output.")] = 50,
    format: Annotated[str, typer.Option(help="table, json, csv, sarif, colang-v1 or colang-v2.")] = "table",
    output: Annotated[Path | None, typer.Option("-o", "--output", dir_okay=False, help="Write to a new file.")] = None,
) -> None:
    """List findings or export every finding matching the filters."""
    _require_evals()
    from martian.evals.server.principal import SINGLE_USER
    from martian.redteam.findings import FindingExport, InventoryQuery, inventory_page, read_inventory, select_findings
    from martian.redteam.report.inventory import render_findings

    try:
        query = InventoryQuery.model_validate(
            {
                "project_id": project_id,
                "q": q,
                "target": target,
                "status": status,
                "severity": severity,
                "category": category,
                "vulnerability_type": vulnerability_type,
                "evidence": evidence,
                "run_id": run_id,
                "seen_after_fix": seen_after_fix,
                "sort": sort,
                "offset": offset,
                "limit": limit,
            }
        )
        export = TypeAdapter(FindingExport).validate_python(format) if format not in ("table", "json") else None
    except ValidationError as error:
        _invalid(error, field="format")
    if export is not None:
        if output is None:
            _stop("Set --output for CSV, SARIF or Colang exports.")
        if offset != 0 or limit != 50:
            _stop("Pagination applies to table and JSON output. Exports include every match.")
    elif format == "table" and output is not None:
        _stop("Choose JSON or an export format when using --output.")

    with _local_records(data_dir) as records:
        scans, inventory = asyncio.run(read_inventory(records, SINGLE_USER, query.project_id))
    if export is not None:
        assert output is not None
        _write_export(output, render_findings(select_findings(inventory, query), export))
    else:
        page = inventory_page(scans, inventory, query)
        if format == "table":
            _table(page, offset)
        elif output is None:
            typer.echo(page.model_dump_json())
        else:
            _write_export(output, (page.model_dump_json(indent=2) + "\n").encode("utf-8"))


@app.command("show")
def show_finding(finding_id: FindingId, data_dir: DataDir = None, project_id: Project = "default") -> None:
    """Print a finding, its scan observations and triage history as JSON."""
    _require_evals()
    from martian.evals.server.principal import SINGLE_USER
    from martian.redteam.findings import read_finding

    with _local_records(data_dir) as records:
        detail = asyncio.run(read_finding(records, SINGLE_USER, project_id, finding_id))
    typer.echo(detail.model_dump_json())


@app.command("triage")
def triage_finding(
    finding_id: FindingId,
    data_dir: DataDir = None,
    project_id: Project = "default",
    comment: Annotated[str, typer.Option(help="Add a triage comment.")] = "",
    status: Annotated[str | None, typer.Option(help="open, accepted, fixed, false_positive or ignored.")] = None,
    severity: Annotated[str | None, typer.Option(help="Override the severity.")] = None,
    restore_severity: Annotated[bool, typer.Option(help="Restore the recorded severity.")] = False,
) -> None:
    """Append a review and print the saved triage entry as JSON."""
    _require_evals()
    from martian.evals.server.principal import SINGLE_USER
    from martian.redteam.findings import TriageChange, record_triage

    if severity is not None and restore_severity:
        _stop("Choose either --severity or --restore-severity.")
    fields: dict[str, str | None] = {"comment": comment, "status": status}
    if severity is not None or restore_severity:
        fields["severity"] = severity
    try:
        change = TriageChange.model_validate(fields)
    except ValidationError as error:
        _invalid(error, field="triage")
    with _local_records(data_dir) as records:
        note = asyncio.run(record_triage(records, SINGLE_USER, project_id, finding_id, change))
    typer.echo(note.model_dump_json())


@app.command("record-fix")
def record_fix(
    baseline: Annotated[str, typer.Argument(help="Baseline scan ID in the local store.")],
    follow_up: Annotated[str, typer.Argument(help="Follow-up scan ID in the same project.")],
    scan_finding: Annotated[str, typer.Argument(help="Finding ID from the baseline scan's verification report.")],
    data_dir: DataDir = None,
    project_id: Project = "default",
    request_id: Annotated[UUID | None, typer.Option(help="Reuse this ID when retrying a decision.")] = None,
) -> None:
    """Record a fixed decision with evidence from two saved scans."""
    _require_evals()
    from martian.evals.server.principal import SINGLE_USER
    from martian.redteam.verified_fixes import VerifiedFixRequest, record_verified_fix

    try:
        request = VerifiedFixRequest(
            finding_id=scan_finding, candidate_run_id=follow_up, request_id=request_id or uuid4()
        )
    except ValidationError as error:
        _invalid(error, field="verified fix")
    with _local_records(data_dir) as records:
        result = asyncio.run(record_verified_fix(records, SINGLE_USER, project_id, baseline, request))
    typer.echo(result.model_dump_json())


@app.command("export-triage")
def export_triage_archive(
    output: Annotated[Path, typer.Option("-o", "--output", dir_okay=False, help="Write to a new archive file.")],
    data_dir: DataDir = None,
    project_id: Project = "default",
    finding_id: Annotated[str | None, typer.Option(help="Export reviews for a single inventory finding.")] = None,
) -> None:
    """Export project review history as a portable JSON archive."""
    _require_evals()
    from martian.evals.server.principal import SINGLE_USER
    from martian.redteam.findings import Digest, Identifier
    from martian.redteam.triage_archive import export_triage

    try:
        project_id = TypeAdapter(Identifier).validate_python(project_id)
    except ValidationError as error:
        _invalid(error, field="project-id")
    if finding_id is not None:
        try:
            finding_id = TypeAdapter(Digest).validate_python(finding_id)
        except ValidationError as error:
            _invalid(error, field="finding-id")
    with _local_records(data_dir) as records:
        archive, skipped = asyncio.run(export_triage(records, SINGLE_USER, project_id, finding_id=finding_id))
    _write_export(output, (archive.model_dump_json(indent=2) + "\n").encode("utf-8"))
    typer.echo(
        f"Exported {sum(len(item.notes) for item in archive.findings)} reviews for {len(archive.findings)} findings."
    )
    if skipped:
        typer.echo(f"The archive omitted {skipped} reviews without a saved finding.", err=True)


@app.command("import-triage")
def import_triage_archive(
    archive: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="A Findings triage archive.")],
    data_dir: DataDir = None,
    project_id: Annotated[
        str | None, typer.Option(help="Project to restore. Defaults to the archive's project.")
    ] = None,
    dry_run: Annotated[bool, typer.Option(help="Check the archive and count reviews without saving them.")] = False,
) -> None:
    """Restore review history after loading the matching scan evidence."""
    _require_evals()
    from martian.evals.server.principal import SINGLE_USER
    from martian.redteam.triage_archive import TriageArchive, import_triage

    try:
        review_archive = TriageArchive.model_validate_json(archive.read_bytes())
    except OSError:
        _stop("Could not read the triage archive. Check the path and its permissions.")
    except ValidationError as error:
        _invalid(error, field="archive")
    selected_project = review_archive.project_id if project_id is None else project_id
    with _local_records(data_dir) as records:
        result = asyncio.run(import_triage(records, SINGLE_USER, selected_project, review_archive, dry_run=dry_run))
    typer.echo(result.model_dump_json())
