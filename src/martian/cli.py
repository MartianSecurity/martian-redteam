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
    max_seconds: Annotated[
        float | None,
        typer.Option(
            "--max-seconds",
            min=1,
            show_default=False,
            help="Suite file only: stop the run after this many seconds; run it again to resume. "
            "Replaces limits.max_seconds.",
        ),
    ] = None,
    max_judge_tokens: Annotated[
        int | None,
        typer.Option(
            "--max-judge-tokens",
            min=1,
            show_default=False,
            help="Suite file only: stop the run once its LLM judges and simulated users have used this many "
            "tokens together; run it again to resume. Replaces limits.max_judge_tokens.",
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
            max_seconds=max_seconds,
            max_judge_tokens=max_judge_tokens,
        )
        return
    suite_only = (output, concurrency, limit, repeats, data_dir, max_seconds, max_judge_tokens)
    if fresh or any(option is not None for option in suite_only):
        typer.echo(
            "--output, --concurrency, --limit, --repeats, --fresh, --max-seconds, --max-judge-tokens and "
            "--data-dir apply to a suite file (evals.yaml) only.",
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
    max_seconds: float | None = None,
    max_judge_tokens: int | None = None,
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
            max_seconds=max_seconds,
            max_judge_tokens=max_judge_tokens,
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
    title = (
        f"{path.name}: {summary['cases']} cases"
        + (f", {simulated['cases']} simulated" if simulated else "")
        + (f", {plan['attempts']} attempts each" if plan else "")
    )
    if summary["not_run"]:
        title += f", {summary['not_run']} not run"
    table = Table(title=title, caption="; ".join(notes) or None)
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
    if summary["not_run"]:
        # A run stopped at a limit, or cancelled, counts only the cases it finished above.
        why = "It was cancelled" if summary.get("cancelled") else summary["stopped"]["message"]
        partly = " A case the run stopped in the middle of runs all of its attempts again." if plan else ""
        console.print(
            f"{why}: {summary['not_run']} case(s) not run. Run the same command again to resume after the cases "
            f"it finished.{partly}",
            markup=False,
            highlight=False,
            soft_wrap=True,
        )
    console.print(f"Results: {summary['output']}/results.jsonl, summary.json, junit.xml")
    recorded = summary["run"]
    ui = (
        "martian ui" if data_folder() == Path(recorded["data_dir"]) else f"martian ui --data-dir {recorded['data_dir']}"
    )
    # One line, never wrapped, so the command can be copied.
    console.print(
        f"Run {recorded['code']} is in the local Evals UI: {ui}", markup=False, highlight=False, soft_wrap=True
    )
    # A case with no verdict, or one the run stopped before, is never a pass.
    raise typer.Exit(code=2 if summary["errors"] or summary["not_run"] else 1 if summary["failed"] else 0)


@evals_app.command("audit")
def audit_evals(
    suite_file: Annotated[Path, typer.Argument(dir_okay=False, show_default=False, help="The suite file to audit.")],
    output: Annotated[
        Path | None, typer.Option("--output", help="Folder for audit.json. Default: the suite's output folder.")
    ] = None,
    limit: Annotated[int | None, typer.Option("--limit", min=1, help="Audit the first N cases.")] = None,
    run_judges: Annotated[
        bool,
        typer.Option(
            "--run-judges",
            help="Also ask the suite's LLM judges about each reply the other checks pass: one model call per "
            "judge, case and reply, at your provider's cost.",
        ),
    ] = False,
) -> None:
    """Audit a suite file: grade replies that do nothing or cheat, in place of its target's.

    martian evals audit evals.yaml    exits 1 when a weak check lets one of them through
    """
    if importlib.util.find_spec("mlflow") is None:
        typer.echo("Auditing a suite needs martian-sdk[evals].", err=True)
        raise typer.Exit(code=2)
    from rich.console import Console
    from rich.table import Table

    from martian.config import ConfigError
    from martian.evals.audit import audit_suite

    console = Console()
    try:
        audit = audit_suite(suite_file, run_judges=run_judges, limit=limit, output=output, progress=console.print)
    except (ConfigError, ValueError) as error:
        typer.echo(f"Audit failed: {error}", err=True)
        raise typer.Exit(code=2) from None

    def say(text: str) -> None:
        console.print(text, markup=False, highlight=False, soft_wrap=True)

    table = Table(title=f"{suite_file.name}: audit of {audit['cases']} cases")
    for column in ("Target", "Kind", "Replies", "Cases passed"):
        table.add_column(column)
    for target in audit["targets"]:
        kind = "does nothing" if target["kind"] == "do_nothing" else "cheats"
        table.add_row(target["name"], kind, str(target["replies"]), str(target["cases_passed"]))
    console.print(table)
    for weak in audit["weak_checks"]:
        shown = ", ".join(weak["examples"][:5]) + (f" and {weak['cases'] - 5} more" if weak["cases"] > 5 else "")
        say(f"Weak check {weak['label']} ({weak['type']}): {'; '.join(weak['why'])}. Cases: {shown}")
    findings = audit["findings"]
    quiet = [finding for finding in findings if not finding["flags"]]
    if quiet:
        say(f"{len(quiet)} other pass(es) do not flag the suite:")
        for finding in quiet[:10]:
            say(f"  {finding['case']} by {finding['target']}: {finding['note']}")
    for check in audit["not_audited"]:
        say(f"Not audited: {check['label']} ({check['type']}): {check['reason']}")
    if audit["skipped"]:
        say(f"Not audited: {audit['skipped']} conversation case(s): an audit grades one reply per case.")
    if findings and audit["not_audited"]:
        say("A case a target passed may still fail a check that was not audited.")
    if audit["judges_run"]:
        say(f"Judge calls: {audit['judge_calls']}")
    if not audit["cases"]:
        say("No case of this suite could be audited, so the audit says nothing about it.")
    elif not audit["audited"]:
        say("No check of this suite could be audited, so the audit says nothing about it.")
    elif audit["flagged"]:
        say(f"Flagged: {len(audit['weak_checks'])} weak check(s) let a target through.")
    else:
        say("Not flagged: no target got through a check that asks for something.")
    say(f"Audit: {audit['output']}/audit.json")
    raise typer.Exit(code=2 if not (audit["audited"] and audit["cases"]) else 1 if audit["flagged"] else 0)


@evals_app.command("inspect-cheating")
def inspect_cheating(
    suite_file: Annotated[
        Path, typer.Argument(dir_okay=False, show_default=False, help="The suite file whose run to inspect.")
    ],
    run: Annotated[
        str | None,
        typer.Option("--run", show_default=False, help="The run's code, such as evals-3, or id. Default: the newest."),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            file_okay=False,
            show_default=False,
            help="The folder the run wrote its results to, when it was run with --output; cheating.json goes "
            "there. Default: the suite's output folder.",
        ),
    ] = None,
    data_dir: Annotated[Path | None, typer.Option("--data-dir", file_okay=False, help=DATA_DIR_HELP)] = None,
    judge: Annotated[
        str | None,
        typer.Option(
            "--judge",
            show_default=False,
            help="Also ask this judge model (an MLflow model URI, such as openai:/MODEL) about each transcript no "
            "signal flagged: one model call each, at your provider's cost. Needs --accept-judge-cost.",
        ),
    ] = None,
    accept_judge_cost: Annotated[
        bool, typer.Option("--accept-judge-cost", help="Agree to the judge's model calls that --judge makes.")
    ] = False,
) -> None:
    """Inspect a suite's run for cheating: signs that the agent reached its answers, tests or grader.

    martian evals inspect-cheating evals.yaml    exits 1 when a case is flagged for review
    """
    if importlib.util.find_spec("mlflow") is None:
        typer.echo("Inspecting a run needs martian-sdk[evals].", err=True)
        raise typer.Exit(code=2)
    from rich.console import Console
    from rich.table import Table

    from martian.config import ConfigError
    from martian.evals.cheating import inspect_suite_run, judge_transcripts, unflagged, write_report

    console = Console()

    def say(text: str) -> None:
        console.print(text, markup=False, highlight=False, soft_wrap=True)

    try:
        report, transcripts = inspect_suite_run(suite_file, run=run, output=output, home=data_dir)
    except (ConfigError, ValueError) as error:
        typer.echo(f"Inspection failed: {error}", err=True)
        raise typer.Exit(code=2) from None
    if judge is not None:
        asked = len(unflagged(report, transcripts))
        if not accept_judge_cost:
            typer.echo(
                f"--judge asks {judge} once for each of the {asked} transcript(s) no signal flagged, at your "
                "provider's cost. Add --accept-judge-cost to go ahead.",
                err=True,
            )
            raise typer.Exit(code=2)
        say(f"Asking {judge} about {asked} transcript(s)")
        judge_transcripts(report, transcripts, model=judge)
    path = write_report(report, report["output"])
    found = report["run"]
    table = Table(title=f"{suite_file.name}: run {found['code'] or found['id']}, {report['cases']} cases")
    for column in ("Signal", "Transcripts"):
        table.add_column(column)
    for signal, count in report["signal_counts"].items():
        if count:
            table.add_row(signal, str(count))
    console.print(table)
    for finding in report["findings"][:20]:
        attempt = f" attempt {finding['attempt']}" if finding["attempt"] is not None else ""
        turn = f" turn {finding['turn']}" if finding.get("turn") is not None else ""
        say(f"{finding['case']}{attempt}{turn}: {finding['reason']}")
    if len(report["findings"]) > 20:
        say(f"… and {len(report['findings']) - 20} more in cheating.json")
    if report["untraced"]:
        say(
            f"{len(report['untraced'])} case(s) have no trace in this run, so only their replies were read: "
            "a case reused from an earlier run keeps its trace there."
        )
    if not report["target_traced"]:
        say("The target records no model or tool step, so its replies cannot be checked for work behind them.")
    if (summary := report["judge"]) is not None:
        say(
            f"Judge calls: {summary['calls']}, flagged {len(summary['flagged'])}, uncertain "
            f"{len(summary['uncertain'])}, failed {len(summary['errors'])}"
        )
    flagged = report["flagged_cases"]
    say(
        f"Flagged for review: {len(flagged)} case(s). A signal is a reason to read the transcript, not a verdict."
        if flagged
        else "Not flagged: no transcript shows a sign of reaching its answers, tests or grader."
    )
    say(f"Inspection: {path}")
    raise typer.Exit(code=1 if flagged else 0)


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
    if summary.paused:
        typer.echo(
            f"Monitors and schedules come back paused (active in the export: {len(summary.paused)}): "
            "resume each to start it again."
        )
    if summary.needs_secret:
        typer.echo(
            f"Webhook secrets are never exported (thresholds with a webhook: {len(summary.needs_secret)}): give each "
            "its secret again with POST /v1/evals/thresholds/{id}/secret before its alerts are signed."
        )
    ui = "martian ui" if data_folder() == home else f"martian ui --data-dir {home}"
    typer.echo(f"Open them in the local Evals UI: {ui}")


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
    concurrency: Annotated[
        int,
        typer.Option(
            "--concurrency",
            envvar="MARTIAN_EVALS_CONCURRENCY",
            min=1,
            help="How many runs, and how many scoring jobs, may be in progress at once; each runs in a process of "
            "its own.",
        ),
    ] = 1,
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
        local = create_local_app(home, ui, host_names(os.environ.get(ALLOWED_HOSTS_ENV)), concurrency=concurrency)
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
