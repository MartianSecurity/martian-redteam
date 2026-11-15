"""One SDK entry point for evals, redteam, and their combined CI gate."""

from __future__ import annotations

import importlib.util
import ipaddress
import os
from pathlib import Path
from typing import Annotated

import typer

from martian import __version__
from martian._errors import concise_problems
from martian.config import MartianConfig
from martian_redteam.cli import app as redteam_app

app = typer.Typer(no_args_is_help=True, add_completion=False, pretty_exceptions_show_locals=False)
evals_app = typer.Typer(no_args_is_help=True, add_completion=False)
app.add_typer(evals_app, name="evals", help="Dataset regressions and scorer checks.")
app.add_typer(redteam_app, name="redteam", help="Adversarial tests and risk reports.")


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
    from martian.evals.runner import run_suite
    from martian.evals.stats import rate_text

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
    groups = summary["groups"]
    table = Table(
        title=f"{path.name}: {summary['cases']} cases",
        caption=f"Intervals allow for {groups} case {'group' if groups == 1 else 'groups'}" if groups else None,
    )
    for column in ("Check", "Passed", "Failed", "Errors", "Pass rate (95% CI)"):
        table.add_column(column)
    for name, counts in [*summary["checks"].items(), ("All checks", summary)]:
        table.add_row(
            name,
            str(counts["passed"]),
            str(counts["failed"]),
            str(counts["errors"]),
            rate_text(counts["pass_rate"], counts["confidence_95"]),
            style="bold" if counts is summary else None,
        )
    console.print(table)
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
