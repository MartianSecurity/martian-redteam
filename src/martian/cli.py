"""One SDK entry point for evals, redteam, and their combined CI gate."""

from __future__ import annotations

import importlib.util
import ipaddress
import json
import math
import os
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer

from martian import __version__
from martian._errors import concise_problems
from martian.config import MartianConfig
from martian_redteam.cli import app as redteam_app

if TYPE_CHECKING:
    from rich.console import Console

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
    repeats: Annotated[
        int | None,
        typer.Option(
            "--repeats",
            min=1,
            max=100,
            help="Suite file only: run each case N times and combine the attempts by each check's rule.",
        ),
    ] = None,
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
            Path(suite_files[0]),
            output=output,
            concurrency=concurrency,
            limit=limit,
            repeats=repeats,
            fresh=fresh,
            home=data_dir,
        )
        return
    if any(value is not None for value in (output, concurrency, limit, repeats, data_dir)) or fresh:
        typer.echo(
            "--output, --concurrency, --limit, --repeats, --fresh and --data-dir apply to a suite file "
            "(evals.yaml) only.",
            err=True,
        )
        raise typer.Exit(code=2)
    import pytest

    required = ["--martian-require-redteam"] if require_redteam else []
    code = pytest.main(["-p", "martian.evals.plugin", "--martian-evals-gate", *required, *ctx.args])
    raise typer.Exit(code=int(code))


def _run_suite(
    path: Path,
    *,
    output: Path | None,
    concurrency: int | None,
    limit: int | None,
    repeats: int | None,
    fresh: bool,
    home: Path | None,
) -> None:
    from rich.console import Console
    from rich.table import Table

    from martian.config import ConfigError
    from martian.evals.evaluate import EvaluationError
    from martian.evals.local import data_folder
    from martian.evals.runner import false_refusal_line, run_suite
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
            repeats=repeats,
        )
    except (ConfigError, EvaluationError, ValueError) as error:
        typer.echo(f"Eval run failed: {error}", err=True)
        raise typer.Exit(code=2) from None
    groups = summary["groups"]
    plan = summary.get("repeats")
    notes = [f"Intervals allow for {groups} case {'group' if groups == 1 else 'groups'}"] if groups else []
    if plan:
        notes.append(
            "Counts are of cases. Each check combines its attempts by its own rule (check), all for a "
            "security check (security), or the suite's rule (suite). Reliability counts the cases that "
            "passed every attempt"
        )
    talks = summary.get("conversations")
    if talks:
        notes.append(
            f"{talks['cases']} of the cases {'is a conversation' if talks['cases'] == 1 else 'are conversations'}: "
            "each counts once, passing when each turn check passed every turn it grades and each conversation "
            "check passed. Turns counts the conversations' turns"
        )
    simulated = summary.get("simulator")
    if simulated:
        notes.append(
            f"{simulated['cases']} {'conversation has' if simulated['cases'] == 1 else 'conversations have'} a "
            f"simulated user, played by {simulated['model']}: Simulated counts their verdicts, and goal_reached "
            "is whether each reached its goal"
        )
    table = Table(
        title=f"{path.name}: {summary['cases']} cases"
        + (f", {simulated['cases']} simulated" if simulated else "")
        + (f", {plan['attempts']} attempts each" if plan else ""),
        caption="; ".join(notes) or None,
    )
    columns = ["Check", "Passed", "Failed", "Errors", "Pass rate (95% CI)"]
    for column in [*columns, "Combined by"] if plan else columns:
        table.add_column(column)
    # With repeats, the share of cases that passed every attempt sits under the pass rate; with
    # simulated users, the verdicts of the cases a model played the user of.
    reliable = summary.get("reliability")
    turns = talks["turns"] if talks else None
    totals = [
        ("All checks", summary),
        *([("Reliability", reliable)] if reliable else []),
        *([("Simulated", simulated)] if simulated else []),
    ]
    for name, counts in [*summary["checks"].items(), *totals, *([("Turns", turns)] if turns else [])]:
        row = [
            name,
            str(counts["passed"]),
            str(counts["failed"]),
            str(counts["errors"]),
            # Turns are parts of their conversations, so their share has no interval of its own.
            "—" if counts is turns else rate_text(counts["pass_rate"], counts["confidence_95"]),
        ]
        if plan and counts is turns:
            row.append("each turn")
        elif plan and counts is reliable:
            row.append("every attempt")
        elif plan and (counts is summary or counts is simulated):
            row.append("every check")
        elif plan:
            rule = plan["checks"][name]
            row.append(f"{rule['combine']} ({rule['from']})")
        table.add_row(*row, style="bold" if counts is summary or counts is reliable else None)
    console.print(table)
    if reliable:
        flaky = reliable["flaky"]
        shown = ", ".join(flaky[:5]) + (f" and {len(flaky) - 5} more" if len(flaky) > 5 else "")
        console.print(
            f"Flaky cases, whose verdict changed between attempts: {shown}"
            if flaky
            else "No flaky cases: no case's verdict changed between attempts",
            markup=False,
            highlight=False,
        )
    if (refusals := summary.get("false_refusals")) is not None:
        console.print(false_refusal_line(refusals), markup=False, highlight=False, soft_wrap=True)
    unit = "attempt(s)" if plan else "case(s)"
    for reason, count in list(summary.get("target_errors", {}).items())[:5]:
        console.print(f"{count} {unit}: {reason}", markup=False, highlight=False)
    for label, reasons in summary.get("check_errors", {}).items():
        for reason, count in list(reasons.items())[:5]:
            console.print(f"{count} {unit}, {label}: {reason}", markup=False, highlight=False)
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


@evals_app.command("personas")
def build_personas(
    transcripts: Annotated[
        Path | None,
        typer.Argument(
            dir_okay=False,
            show_default=False,
            help='A JSON Lines file of conversations, one per line: {"id": ..., "messages": [...]}.',
        ),
    ] = None,
    model: Annotated[
        str,
        typer.Option(
            "--model", help="The model that reads each conversation, as for simulator.model: openai:/<model>."
        ),
    ] = "",
    output: Annotated[Path, typer.Option("--output", "-o", dir_okay=False, help="The data file to write.")] = Path(
        "personas.jsonl"
    ),
    session: Annotated[
        list[str] | None,
        typer.Option(
            "--session", help="Read this session's traces from the local Evals UI's data folder instead; repeatable."
        ),
    ] = None,
    data_dir: Annotated[
        Path | None,
        typer.Option(
            "--data-dir",
            file_okay=False,
            show_default=False,
            help="With --session: the data folder. Default: MARTIAN_EVALS_HOME, else ~/.martian/evals.",
        ),
    ] = None,
    max_turns: Annotated[
        int | None,
        typer.Option(
            "--max-turns", min=1, max=100, help="Each persona's turn budget. Default: the messages its user sent."
        ),
    ] = None,
) -> None:
    """Build simulated users from real conversations: a data file of goal and persona cases.

    martian evals personas transcripts.jsonl --model openai:/<model> -o personas.jsonl
    martian evals personas --session <id> --model openai:/<model> -o personas.jsonl

    Personal data that the no_personal_data check finds is removed before the model reads a
    conversation and from everything written. Names and street addresses are not found: read the
    file before you share it.
    """
    if importlib.util.find_spec("mlflow") is None:
        typer.echo("Building personas needs martian-sdk[evals].", err=True)
        raise typer.Exit(code=2)
    from martian.config import ConfigError
    from martian.evals._policy import personal_data_text
    from martian.evals.local import data_folder
    from martian.evals.personas import build_personas as build
    from martian.evals.personas import read_transcripts, transcripts_from_store, write_personas

    if (transcripts is None) == (not session) or not model:
        typer.echo("Give --model, and a transcripts file or --session, not both.", err=True)
        raise typer.Exit(code=2)
    try:
        found = (
            read_transcripts(transcripts)
            if transcripts
            else transcripts_from_store(data_folder(data_dir), session or [])
        )
        built = build(found, model=model, max_turns=max_turns)
        written = write_personas(built, output)
    except (ConfigError, ValueError) as error:
        typer.echo(f"Building personas failed: {error}", err=True)
        raise typer.Exit(code=2) from None
    plural = "persona" if len(built.cases) == 1 else "personas"
    typer.echo(f"Wrote {len(built.cases)} {plural} to {written}.")
    typer.echo(
        f"Personal data removed: {personal_data_text(built.removed)} (the kinds no_personal_data finds: email "
        "addresses, phone numbers, card numbers, IBANs and national IDs). Names and street addresses are not "
        "found: read the file before you share it."
    )


@evals_app.command("fidelity")
def check_fidelity(
    transcripts: Annotated[
        Path | None,
        typer.Argument(
            dir_okay=False,
            show_default=False,
            help='The real conversations, as for personas: {"id": ..., "messages": [...], "goal_reached": ...}.',
        ),
    ] = None,
    results: Annotated[
        list[Path] | None,
        typer.Option(
            "--results",
            file_okay=False,
            show_default=False,
            help="The output folder of a martian evals run with simulated users; repeatable.",
        ),
    ] = None,
    session: Annotated[
        list[str] | None,
        typer.Option(
            "--session", help="Read this session's traces from the local Evals UI's data folder instead; repeatable."
        ),
    ] = None,
    data_dir: Annotated[
        Path | None,
        typer.Option(
            "--data-dir",
            file_okay=False,
            show_default=False,
            help="With --session: the data folder. Default: MARTIAN_EVALS_HOME, else ~/.martian/evals.",
        ),
    ] = None,
    output: Annotated[Path, typer.Option("--output", "-o", dir_okay=False, help="The report to write.")] = Path(
        "fidelity.json"
    ),
) -> None:
    """Compare simulated conversations with real ones: turns, corrections and goal reached.

    martian evals fidelity transcripts.jsonl --results results -o fidelity.json
    martian evals fidelity --session <id> --results results -o fidelity.json

    One report per simulator setup (its model, parameters, version and persona source) among the
    output folders. Corrections and the outcome of an unlabelled real conversation are found by
    fixed rules; see docs/evals.md, Simulator fidelity. Calls no model.
    """
    if importlib.util.find_spec("mlflow") is None:
        typer.echo("The fidelity report needs martian-sdk[evals].", err=True)
        raise typer.Exit(code=2)
    from rich.console import Console

    from martian.config import ConfigError
    from martian.evals import fidelity
    from martian.evals.local import data_folder
    from martian.evals.personas import read_transcripts, transcripts_from_store

    if (transcripts is None) == (not session) or not results:
        typer.echo("Give --results, and a transcripts file or --session, not both.", err=True)
        raise typer.Exit(code=2)
    try:
        found = (
            read_transcripts(transcripts)
            if transcripts
            else transcripts_from_store(data_folder(data_dir), session or [])
        )
        source = fidelity.transcripts_source(transcripts) if transcripts else fidelity.sessions_source(session or [])
        report = fidelity.fidelity_report(
            [fidelity.read_simulated(folder) for folder in results], fidelity.real_conversations(found), source
        )
        written = fidelity.write_report(report, output)
    except (ConfigError, OSError) as error:
        typer.echo(f"The fidelity report failed: {error}", err=True)
        raise typer.Exit(code=2) from None
    console = Console()
    for each in report["reports"]:
        _print_fidelity(console, each, source, fidelity.VERSION)
    console.print(f"Wrote the fidelity report to {written}.", markup=False, highlight=False, soft_wrap=True)


_READINGS = {"higher": "simulated higher", "lower": "simulated lower", "within_noise": "within noise"}


def _print_fidelity(console: Console, report: dict[str, Any], source: dict[str, Any], version: str) -> None:
    """One setup's fidelity report as a table: each measure for the simulated and the real
    conversations, and their difference, simulated minus real, with its interval."""
    from rich.table import Table

    from martian.evals.stats import rate_text

    setup = report["setup"]
    personas = setup.get("personas") or {}

    def mean(found: dict[str, Any]) -> str:
        if found["mean"] is None:
            return "—"
        interval = found["confidence_95"]
        return f"{found['mean']:.1f}" + (f" ({interval[0]:.1f} to {interval[1]:.1f})" if interval else "")

    def share(found: dict[str, Any], count: str) -> str:
        if found["rate"] is None:
            return "—"
        shown = rate_text(found["rate"], found["confidence_95"]) if found["confidence_95"] else ""
        return f"{found[count]} of {found['conversations']}" + (f", {shown}" if shown else "")

    def apart(found: dict[str, Any], *, points: bool) -> str:
        # A share's difference is in percentage points, its bounds rounded outward as rate_text's.
        if found["value"] is None:
            return "—"
        interval = found["confidence_95"]
        if points:
            text = f"{found['value'] * 100:+.0f} points"
            if interval:
                text += f" ({math.floor(interval[0] * 100 + 1e-9):+d} to {math.ceil(interval[1] * 100 - 1e-9):+d})"
        else:
            text = f"{found['value']:+.1f}" + (f" ({interval[0]:+.1f} to {interval[1]:+.1f})" if interval else "")
        return text + (f", {_READINGS[found['reading']]}" if found["reading"] else "")

    turns, fixes, goal = report["turns"], report["corrections"], report["goal_reached"]
    real_goal = goal["real"]
    origin = (
        f"{source['transcripts']}"
        if "transcripts" in source
        else f"{len(source['sessions'])} sessions of the data folder"
    )
    caption = (
        f"Simulated: {turns['simulated']['conversations']} conversations of {report['cases']} cases in "
        + ", ".join(report["results"])
        + (f", {report['left_out']} that ended on an error left out" if report["left_out"] else "")
        + f". Real: {turns['real']['conversations']} conversations from {origin}; goal reached as labelled for "
        f"{real_goal['from'].get('label', 0)}, by the outcome rule for {real_goal['from'].get('rule', 0)}, unknown "
        f"for {real_goal['unknown']}. Corrections and the outcome rule: {version} (docs/evals.md, simulator fidelity)."
    )
    table = Table(
        title=f"Fidelity of {setup['model']} with personas from {personas.get('data')}",
        caption=caption,
    )
    for column in ("Measure", "Simulated", "Real", "Difference (95% CI)"):
        table.add_column(column)
    table.add_row(
        "Turns per conversation",
        mean(turns["simulated"]),
        mean(turns["real"]),
        apart(turns["difference"], points=False),
    )
    table.add_row(
        "Conversations with a correction",
        share(fixes["simulated"], "with_correction"),
        share(fixes["real"], "with_correction"),
        apart(fixes["difference"], points=True),
    )
    table.add_row(
        "Corrections per turn",
        f"{fixes['simulated']['corrections']} of {fixes['simulated']['turns']}",
        f"{fixes['real']['corrections']} of {fixes['real']['turns']}",
        "—",
    )
    table.add_row(
        "Goal reached",
        share(goal["simulated"], "reached"),
        share(goal["real"], "reached"),
        apart(goal["difference"], points=True),
    )
    parameters = json.dumps(setup.get("parameters") or {}, sort_keys=True)
    console.print(
        f"Simulator setup: {setup['model']}, parameters {parameters}, instructions {setup.get('version')}, "
        f"personas {personas.get('data')} (dataset digest {str(personas.get('digest'))[:12]})",
        markup=False,
        highlight=False,
        soft_wrap=True,
    )
    console.print(table)


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
