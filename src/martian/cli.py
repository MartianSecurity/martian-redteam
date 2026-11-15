"""One SDK entry point for evals, redteam, and their combined CI gate."""

from __future__ import annotations

import importlib.util
import ipaddress
import os
from pathlib import Path
from typing import Annotated, Any

import typer

from martian import __version__
from martian._errors import concise_problems
from martian.config import MartianConfig
from martian_redteam.cli import app as redteam_app

app = typer.Typer(no_args_is_help=True, add_completion=False, pretty_exceptions_show_locals=False)
evals_app = typer.Typer(no_args_is_help=True, add_completion=False)
import_app = typer.Typer(no_args_is_help=True, add_completion=False)
convert_app = typer.Typer(no_args_is_help=True, add_completion=False)
app.add_typer(evals_app, name="evals", help="Dataset regressions and scorer checks.")
evals_app.add_typer(
    import_app, name="import", help="Bring runs from other eval tools, or a Martian export, into the local Evals UI."
)
evals_app.add_typer(convert_app, name="convert", help="Turn another eval tool's config into suite files.")
app.add_typer(redteam_app, name="redteam", help="Adversarial tests and risk reports.")

DATA_DIR_HELP = (
    "The local Evals UI's data folder, as for `martian ui`. Default: MARTIAN_EVALS_HOME, else ~/.martian/evals."
)


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: Annotated[bool, typer.Option("--version", help="Print the SDK version.")] = False,
) -> None:
    """Evaluate and redteam your agent, then gate CI on both results."""
    if version:
        typer.echo(__version__)
        raise typer.Exit()
    if ctx.invoked_subcommand == "redteam":
        # Validate/catalog are secret-free. Construct settings only for an actual run.
        ctx.obj = MartianConfig


@evals_app.command("run", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def run_evals(
    ctx: typer.Context,
    require_redteam: Annotated[
        bool, typer.Option("--require-redteam", help="Require a recorded redteam gate in this session.")
    ] = False,
    output: Annotated[Path | None, typer.Option("--output", help="Suite file only: folder for the results.")] = None,
    concurrency: Annotated[
        int | None, typer.Option("--concurrency", min=1, max=64, help="Suite file only: cases run at the same time.")
    ] = None,
    limit: Annotated[int | None, typer.Option("--limit", min=1, help="Suite file only: run the first N cases.")] = None,
    fresh: Annotated[
        bool, typer.Option("--fresh", help="Suite file only: discard earlier results instead of resuming.")
    ] = False,
    data_dir: Annotated[
        Path | None,
        typer.Option(
            "--data-dir",
            file_okay=False,
            show_default=False,
            help="Suite file only: the local Evals UI's data folder to record the run in, as for `martian ui`. "
            "Default: MARTIAN_EVALS_HOME, else ~/.martian/evals.",
        ),
    ] = None,
) -> None:
    """Run an eval suite file locally, or run pytest with the eval gate.

    martian evals run evals.yaml      one suite file: runs locally with only a model key
    martian evals run tests/evals -q  pytest with the eval gate; options are forwarded unchanged
    """
    if importlib.util.find_spec("pytest") is None or importlib.util.find_spec("mlflow") is None:
        typer.echo("Eval dependencies are missing; install martian-sdk[evals].", err=True)
        raise typer.Exit(code=2)
    suite_files = [a for a in ctx.args if a.lower().endswith((".yaml", ".yml"))]
    if suite_files:
        if len(ctx.args) != 1 or require_redteam:
            typer.echo("Give one suite file and no pytest options: martian evals run evals.yaml", err=True)
            raise typer.Exit(code=2)
        _run_suite(
            Path(suite_files[0]), output=output, concurrency=concurrency, limit=limit, fresh=fresh, home=data_dir
        )
        return
    if output is not None or concurrency is not None or limit is not None or fresh or data_dir is not None:
        typer.echo(
            "--output, --concurrency, --limit, --fresh and --data-dir apply to a suite file (evals.yaml) only.",
            err=True,
        )
        raise typer.Exit(code=2)
    import pytest

    required = ["--martian-require-redteam"] if require_redteam else []
    code = pytest.main(["-p", "martian.evals.plugin", "--martian-evals-gate", *required, *ctx.args])
    raise typer.Exit(code=int(code))


def _run_suite(
    path: Path, *, output: Path | None, concurrency: int | None, limit: int | None, fresh: bool, home: Path | None
) -> None:
    from rich.console import Console
    from rich.table import Table

    from martian.config import ConfigError
    from martian.evals.evaluate import EvaluationError
    from martian.evals.local import data_folder
    from martian.evals.runner import false_refusal_line, run_suite

    console = Console()
    try:
        summary = run_suite(
            path,
            output=output,
            concurrency=concurrency,
            limit=limit,
            resume=not fresh,
            progress=console.print,
            home=home,
        )
    except (ConfigError, EvaluationError, ValueError) as error:
        typer.echo(f"Eval run failed: {error}", err=True)
        raise typer.Exit(code=2) from None
    table = Table(title=f"{path.name}: {summary['cases']} cases")
    for column in ("Check", "Passed", "Failed", "Errors"):
        table.add_column(column)
    for name, counts in summary["checks"].items():
        table.add_row(name, str(counts["passed"]), str(counts["failed"]), str(counts["errors"]))
    table.add_row("All checks", str(summary["passed"]), str(summary["failed"]), str(summary["errors"]), style="bold")
    console.print(table)
    if (refusals := summary.get("false_refusals")) is not None:
        console.print(false_refusal_line(refusals), markup=False, highlight=False, soft_wrap=True)
    for reason, count in list(summary.get("target_errors", {}).items())[:5]:
        console.print(f"{count} case(s): {reason}", markup=False, highlight=False)
    for label, reasons in summary.get("check_errors", {}).items():
        for reason, count in list(reasons.items())[:5]:
            console.print(f"{count} case(s), {label}: {reason}", markup=False, highlight=False)
    console.print(f"Results: {summary['output']}/results.jsonl, summary.json, junit.xml")
    recorded = summary["run"]
    ui = (
        "martian ui" if data_folder() == Path(recorded["data_dir"]) else f"martian ui --data-dir {recorded['data_dir']}"
    )
    # One line, never wrapped, so the command can be copied.
    console.print(
        f"Run {recorded['code']} is in the local Evals UI: {ui}", markup=False, highlight=False, soft_wrap=True
    )
    raise typer.Exit(code=2 if summary["errors"] else 1 if summary["failed"] else 0)


@import_app.command("inspect")
def import_inspect(
    logs: Annotated[
        list[Path], typer.Argument(dir_okay=False, show_default=False, help="Inspect AI log files, .eval or .json.")
    ],
    data_dir: Annotated[
        Path | None,
        typer.Option(
            "--data-dir",
            file_okay=False,
            show_default=False,
            help="The local Evals UI's data folder to import into, as for `martian ui`. "
            "Default: MARTIAN_EVALS_HOME, else ~/.martian/evals.",
        ),
    ] = None,
) -> None:
    """Import Inspect AI logs as runs in the local Evals UI: one run per log.

    martian evals import inspect logs/*.eval    every log Inspect wrote to logs/
    """
    if any(importlib.util.find_spec(name) is None for name in ("mlflow", "sqlalchemy", "zstandard")):
        typer.echo("Importing Inspect logs needs martian-sdk[evals].", err=True)
        raise typer.Exit(code=2)
    from martian.evals.inspect_import import InspectLogError, import_inspect_logs
    from martian.evals.local import LocalEditionError, data_folder

    home = data_folder(data_dir)
    try:
        runs = import_inspect_logs(logs, home)
    except (InspectLogError, LocalEditionError) as error:
        typer.echo(f"Import failed: {error}", err=True)
        raise typer.Exit(code=2) from None
    for log, run in zip(logs, runs, strict=True):
        counts = run.counts
        typer.echo(
            f"{log.name}: run {run.code}, {counts.cases} cases: {counts.passed} passed, {counts.failed} failed, "
            f"{counts.errors} errors, {counts.skipped} without a verdict"
        )
    ui = "martian ui" if data_folder() == home else f"martian ui --data-dir {home}"
    typer.echo(f"Open them in the local Evals UI: {ui}")


@import_app.command("promptfoo")
def import_promptfoo(
    files: Annotated[
        list[Path],
        typer.Argument(
            dir_okay=False,
            show_default=False,
            help="promptfoo results files, as `promptfoo eval -o results.json` writes.",
        ),
    ],
    data_dir: Annotated[
        Path | None, typer.Option("--data-dir", file_okay=False, show_default=False, help=DATA_DIR_HELP)
    ] = None,
) -> None:
    """Import promptfoo results as runs in the local Evals UI: one run per provider in each file.

    martian evals import promptfoo results.json
    """
    if any(importlib.util.find_spec(name) is None for name in ("mlflow", "sqlalchemy")):
        typer.echo("Importing promptfoo results needs martian-sdk[evals].", err=True)
        raise typer.Exit(code=2)
    from martian.evals.local import LocalEditionError, data_folder
    from martian.evals.promptfoo_import import PromptfooResultsError, import_promptfoo_results

    home = data_folder(data_dir)
    try:
        runs = import_promptfoo_results(files, home)
    except (PromptfooResultsError, LocalEditionError) as error:
        typer.echo(f"Import failed: {error}", err=True)
        raise typer.Exit(code=2) from None
    for run in runs:
        counts, source = run.counts, run.provenance["promptfoo"]
        typer.echo(
            f"{Path(source['results']).name}: provider {source['provider']['label'] or source['provider']['id']}, "
            f"run {run.code}, {counts.cases} cases: {counts.passed} passed, {counts.failed} failed, "
            f"{counts.errors} errors"
        )
    ui = "martian ui" if data_folder() == home else f"martian ui --data-dir {home}"
    typer.echo(f"Open them in the local Evals UI: {ui}")


@convert_app.command("promptfoo")
def convert_promptfoo(
    config: Annotated[
        Path, typer.Argument(dir_okay=False, show_default=False, help="A promptfoo config, promptfooconfig.yaml.")
    ],
    output: Annotated[
        Path,
        typer.Option(
            "--output", file_okay=False, show_default=False, help="The folder to write the suite and data files to."
        ),
    ],
    target: Annotated[
        str | None,
        typer.Option(
            "--target",
            show_default=False,
            help="FILE.py[:FUNCTION], called as FUNCTION(prompt=..., vars=...) for each case (default function: "
            "target). Without it, the suite grades each test's providerOutput.",
        ),
    ] = None,
    judge_model: Annotated[
        str | None,
        typer.Option(
            "--judge-model",
            show_default=False,
            help="The model the rubric judge calls for llm-rubric assertions, such as openai:/gpt-4.1-mini. "
            "Without it, llm-rubric assertions are not converted.",
        ),
    ] = None,
) -> None:
    """Convert a promptfoo config into suite files for `martian evals run`, where its features map,
    and list everything that does not.

    martian evals convert promptfoo promptfooconfig.yaml --target agent.py:answer --output suite
    """
    if any(importlib.util.find_spec(name) is None for name in ("mlflow", "zstandard")):
        typer.echo("Converting a promptfoo config needs martian-sdk[evals].", err=True)
        raise typer.Exit(code=2)
    from martian.evals.promptfoo_convert import (
        PromptfooConfigError,
        convert_promptfoo_config,
        report_lines,
        write_conversion,
    )

    try:
        conversion = convert_promptfoo_config(config, output=output, target=target, judge_model=judge_model)
        if conversion.suites:
            write_conversion(conversion, output)
    except PromptfooConfigError as error:
        typer.echo(f"Conversion failed: {error}", err=True)
        raise typer.Exit(code=2) from None
    for line in report_lines(conversion, output):
        typer.echo(line)
    if not conversion.suites:
        typer.echo("Nothing written: no test converts.", err=True)
        raise typer.Exit(code=2)
    typer.echo(
        f"Run each with `martian evals run`, such as: martian evals run {output / conversion.suites[0].suite_file}"
    )


@import_app.command("mlflow")
def import_mlflow(
    source: Annotated[
        str,
        typer.Argument(
            show_default=False,
            help="The MLflow tracking store: its SQLite database file (mlflow.db), or a sqlite:/// or "
            "postgresql:// URL.",
        ),
    ],
    data_dir: Annotated[
        Path | None,
        typer.Option(
            "--data-dir",
            file_okay=False,
            show_default=False,
            help="The local Evals UI's data folder to copy into, as for `martian ui`. "
            "Default: MARTIAN_EVALS_HOME, else ~/.martian/evals.",
        ),
    ] = None,
    experiment: Annotated[
        list[str] | None,
        typer.Option(
            "--experiment",
            show_default=False,
            help="Copy only this experiment, by name or id. Repeat for more. Default: every active experiment.",
        ),
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Check and read the store, and report what would be copied.")
    ] = False,
) -> None:
    """Copy an MLflow tracking store into the local Evals UI: experiments, traces, datasets,
    assessments and evaluation runs. The store is only read, and only at the schema revision this
    release's MLflow writes; any other is refused before anything is copied.

    martian evals import mlflow ./mlflow.db
    """
    if any(importlib.util.find_spec(name) is None for name in ("mlflow", "sqlalchemy")):
        typer.echo("Copying an MLflow store needs martian-sdk[evals].", err=True)
        raise typer.Exit(code=2)
    from martian.evals.local import LocalEditionError, data_folder
    from martian.evals.mlflow_import import MlflowSourceError, import_mlflow_store, report_lines

    home = data_folder(data_dir)
    try:
        report = import_mlflow_store(source, home, experiments=experiment or None, dry_run=dry_run)
    except (MlflowSourceError, LocalEditionError) as error:
        typer.echo(f"Import failed: {error}", err=True)
        raise typer.Exit(code=2) from None
    except ImportError as error:  # a postgresql:// source without the driver
        typer.echo(f"Import failed: {error}. A Postgres store needs martian-sdk[postgres].", err=True)
        raise typer.Exit(code=2) from None
    for line in report_lines(report, written=not dry_run):
        typer.echo(line)
    if not dry_run:
        ui = "martian ui" if data_folder() == home else f"martian ui --data-dir {home}"
        typer.echo(f"Open them in the local Evals UI: {ui}")


def _export_counts(lines: dict[str, Any]) -> str:
    """The main counts of an export's manifest, for one line of output."""
    shown = (
        ("projects.jsonl", "project"),
        ("datasets.jsonl", "dataset version"),
        ("runs.jsonl", "run"),
        ("case_results.jsonl", "case result"),
        ("scores.jsonl", "score"),
        ("reviews.jsonl", "review"),
        ("traces.jsonl", "trace"),
        ("artifacts.jsonl", "artifact"),
    )
    counts = [(int(lines[name]["lines"]), noun) for name, noun in shown]
    return ", ".join(f"{count} {noun}{'' if count == 1 else 's'}" for count, noun in counts)


@evals_app.command("export")
def export_evals(
    folder: Annotated[
        Path,
        typer.Argument(
            file_okay=False,
            show_default=False,
            help="A new or empty folder to write the export to, or the folder of an interrupted export to resume.",
        ),
    ],
    data_dir: Annotated[
        Path | None, typer.Option("--data-dir", file_okay=False, show_default=False, help=DATA_DIR_HELP)
    ] = None,
) -> None:
    """Export everything in the local Evals UI's data folder: runs, case results, scores, datasets,
    reviews and annotations, traces and the rest, as JSON Lines files with a manifest.

    martian evals export ./evals-export    run it again with the same folder to resume an interrupted export
    """
    if any(importlib.util.find_spec(name) is None for name in ("mlflow", "sqlalchemy")):
        typer.echo("Exporting the local Evals UI's data needs martian-sdk[evals].", err=True)
        raise typer.Exit(code=2)
    from martian.evals.data_export import ExportError, export_local
    from martian.evals.local import LocalEditionError, data_folder
    from martian.evals.store import StoreError

    home = data_folder(data_dir)
    try:
        summary = export_local(home, folder)
    except (ExportError, LocalEditionError) as error:
        typer.echo(f"Export failed: {error}", err=True)
        raise typer.Exit(code=2) from None
    except StoreError as error:
        typer.echo(f"Export stopped: {error.detail}. Run the same command again to resume it.", err=True)
        raise typer.Exit(code=2) from None
    except KeyboardInterrupt:
        typer.echo("Export interrupted. Run the same command again to resume it.", err=True)
        raise typer.Exit(code=130) from None
    finished = "Resumed and finished exporting" if summary.resumed else "Exported"
    typer.echo(f"{finished} {home} into {summary.folder}: {_export_counts(summary.manifest['files'])}.")


@import_app.command("martian")
def import_martian(
    folder: Annotated[
        Path,
        typer.Argument(file_okay=False, show_default=False, help="A folder `martian evals export` wrote."),
    ],
    data_dir: Annotated[
        Path | None, typer.Option("--data-dir", file_okay=False, show_default=False, help=DATA_DIR_HELP)
    ] = None,
) -> None:
    """Import a `martian evals export` into a data folder that holds no data yet.

    martian evals import martian ./evals-export --data-dir ./evals-data
    """
    if any(importlib.util.find_spec(name) is None for name in ("mlflow", "sqlalchemy")):
        typer.echo("Importing an export needs martian-sdk[evals].", err=True)
        raise typer.Exit(code=2)
    from martian.evals.data_export import ExportError, import_local
    from martian.evals.local import LocalEditionError, data_folder
    from martian.evals.store import StoreError

    home = data_folder(data_dir)
    try:
        summary = import_local(folder, home)
    except (ExportError, LocalEditionError) as error:
        typer.echo(f"Import failed: {error}", err=True)
        raise typer.Exit(code=2) from None
    except StoreError as error:
        typer.echo(f"Import stopped: {error.detail}. Import into a fresh data folder again.", err=True)
        raise typer.Exit(code=2) from None
    typer.echo(f"Imported into {home}: {_export_counts(summary.manifest['files'])}.")
    ui = "martian ui" if data_folder() == home else f"martian ui --data-dir {home}"
    typer.echo(f"Open them in the local Evals UI: {ui}")


@app.command("ui")
def run_ui(
    port: Annotated[int, typer.Option("--port", min=1, max=65535, help="The port to listen on.")] = 7600,
    data_dir: Annotated[
        Path | None,
        typer.Option(
            "--data-dir",
            envvar="MARTIAN_EVALS_HOME",
            file_okay=False,
            show_default=False,
            help="Folder for the local store and its artifacts. Default: ~/.martian/evals.",
        ),
    ] = None,
    ui_dir: Annotated[
        Path | None,
        typer.Option(
            "--ui-dir",
            envvar="MARTIAN_EVALS_UI_DIR",
            file_okay=False,
            show_default=False,
            help="Folder of a built Evals UI (index.html and assets/). Default: the one in this package.",
        ),
    ] = None,
    open_browser: Annotated[bool, typer.Option("--open/--no-open", help="Open the UI in the default browser.")] = True,
) -> None:
    """Open the Evals UI on this machine: one user, no login, everything stored locally on SQLite."""
    if any(importlib.util.find_spec(name) is None for name in ("mlflow", "fastapi", "uvicorn")):
        typer.echo("The local Evals UI needs martian-sdk[evals].", err=True)
        raise typer.Exit(code=2)
    from martian.evals.local import (
        ALLOWED_HOSTS_ENV,
        BIND_ENV,
        LocalEditionError,
        create_local_app,
        data_folder,
        host_names,
        listen_address,
        packaged_ui,
        serve,
    )

    home = data_folder(data_dir)
    ui = (ui_dir or packaged_ui()).expanduser().resolve()
    try:
        # The container image's settings (see BIND_ENV); unset, the server stays on 127.0.0.1.
        host = listen_address(os.environ.get(BIND_ENV))
        local = create_local_app(home, ui, host_names(os.environ.get(ALLOWED_HOSTS_ENV)))
        serve(
            local,
            host=host,
            port=port,
            open_browser=open_browser,
            announce=lambda url: _announce_ui(url, host=host, port=port, home=home),
        )
    except LocalEditionError as error:
        typer.echo(f"martian ui: {error}", err=True)
        if ui_dir is None and not (ui / "index.html").is_file():
            typer.echo(
                "This install of martian-sdk has lost the Evals UI it ships: reinstall it, or pass --ui-dir "
                "with a built UI, such as the dist-evals folder that `npm run build:evals` writes in the frontend.",
                err=True,
            )
        raise typer.Exit(code=2) from None


def _announce_ui(url: str, *, host: str, port: int, home: Path) -> None:
    """What ``martian ui`` prints once it listens. Off loopback, as in the container image, it
    gives the address it listens on and the warning that there is no login."""
    from martian.evals.local import EXPOSURE_WARNING

    if ipaddress.IPv4Address(host).is_loopback:
        typer.echo(f"Martian Evals is at {url} (data in {home}). Press Ctrl+C to stop.")
        return
    typer.echo(f"Martian Evals is listening on {host}:{port} (data in {home}). Press Ctrl+C to stop.")
    typer.echo(EXPOSURE_WARNING, err=True)


@app.command("ci")
def run_ci(
    evals_path: Annotated[
        Path | None, typer.Option("--evals-path", exists=True, help="Required pytest eval file or directory.")
    ] = None,
    redteam_config: Annotated[
        Path | None, typer.Option("--redteam-config", exists=True, dir_okay=False, help="Required redteam YAML suite.")
    ] = None,
    output: Annotated[
        Path, typer.Option("--output", "-o", help="Safe reports and one persisted session receipt.")
    ] = Path("results"),
    require_export: Annotated[
        bool, typer.Option("--require-export", help="Require configured MLflow exports to finish.")
    ] = False,
) -> None:
    """Run evals, redteam, or both once in one persisted CI session."""
    if importlib.util.find_spec("pytest") is None or importlib.util.find_spec("mlflow") is None:
        typer.echo("CI dependencies are missing; install martian-sdk[evals].", err=True)
        raise typer.Exit(code=2)
    from martian.ci import CiSuite, run

    result = run(
        CiSuite(
            evals_paths=(evals_path,) if evals_path else (),
            redteam_config=redteam_config,
            output_dir=output,
            require_export=require_export,
        )
    )
    typer.echo(
        f"CI gate: {result.state}; session {result.session_id or 'not persisted'}; receipt {output / 'ci-session.json'}"
    )
    for problem in concise_problems(result.problems):
        typer.echo(problem, err=True)
    raise typer.Exit(code=result.exit_code)
