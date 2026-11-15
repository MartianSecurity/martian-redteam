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
    baseline: Annotated[
        Path | None,
        typer.Option(
            "--baseline",
            file_okay=False,
            show_default=False,
            help="Suite file only: an earlier run's output folder. Compare with it case by case and gate on "
            "the comparison: fail on a change worse than noise, on any case that newly fails a security check, "
            "or on a failed case it has no result for.",
        ),
    ] = None,
    cache: Annotated[
        bool | None,
        typer.Option(
            "--cache/--no-cache",
            show_default=False,
            help="Suite file only: replay the target's and the judges' replies stored by an earlier run for the "
            "same request, and store new ones; --no-cache turns off the suite file's cache block. Default: the "
            "suite file's cache block, else off.",
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
            baseline=baseline,
            cache=cache,
        )
        return
    if any(value is not None for value in (output, concurrency, limit, repeats, baseline, data_dir, cache)) or fresh:
        typer.echo(
            "--output, --concurrency, --limit, --repeats, --fresh, --cache, --baseline and --data-dir apply to a "
            "suite file (evals.yaml) only.",
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
    baseline: Path | None = None,
    cache: bool | None = None,
) -> None:
    from rich.console import Console
    from rich.table import Table

    from martian.config import ConfigError
    from martian.evals.evaluate import EvaluationError
    from martian.evals.local import data_folder
    from martian.evals.runner import cache_line, false_refusal_line, run_suite
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
            baseline=baseline,
            cache=cache,
        )
    except (ConfigError, EvaluationError, ValueError) as error:
        typer.echo(f"Eval run failed: {error}", err=True)
        raise typer.Exit(code=2) from None
    if "combinations" in summary:
        raise typer.Exit(code=_print_matrix(console, path, summary, baseline=baseline))
    groups = summary["groups"]
    plan = summary.get("repeats")
    notes = [f"Intervals allow for {groups} case {'group' if groups == 1 else 'groups'}"] if groups else []
    if plan:
        notes.append(
            "Counts are of cases. Each check combines its attempts by its own rule (check), all for a "
            "security check (security), or the suite's rule (suite). Reliability counts the cases that "
            "passed every attempt"
        )
    table = Table(
        title=f"{path.name}: {summary['cases']} cases" + (f", {plan['attempts']} attempts each" if plan else ""),
        caption="; ".join(notes) or None,
    )
    columns = ["Check", "Passed", "Failed", "Errors", "Pass rate (95% CI)"]
    for column in [*columns, "Combined by"] if plan else columns:
        table.add_column(column)
    # With repeats, the share of cases that passed every attempt sits under the pass rate.
    reliable = summary.get("reliability")
    totals = [("All checks", summary), *([("Reliability", reliable)] if reliable else [])]
    for name, counts in [*summary["checks"].items(), *totals]:
        row = [
            name,
            str(counts["passed"]),
            str(counts["failed"]),
            str(counts["errors"]),
            rate_text(counts["pass_rate"], counts["confidence_95"]),
        ]
        if plan and counts is reliable:
            row.append("every attempt")
        elif plan and counts is summary:
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
    if (used := summary.get("cache")) is not None:
        console.print(cache_line(used), markup=False, highlight=False, soft_wrap=True)
    unit = "attempt(s)" if plan else "case(s)"
    for reason, count in list(summary.get("target_errors", {}).items())[:5]:
        console.print(f"{count} {unit}: {reason}", markup=False, highlight=False)
    for label, reasons in summary.get("check_errors", {}).items():
        for reason, count in list(reasons.items())[:5]:
            console.print(f"{count} {unit}, {label}: {reason}", markup=False, highlight=False)
    comparison = summary.get("comparison")
    if comparison is not None:
        for line in _comparison_lines(comparison):
            console.print(line, markup=False, highlight=False, soft_wrap=True)
    elif baseline is not None:
        console.print(_NO_RESULT.format(where=baseline, what="this suite"), markup=False, highlight=False)
    files = "results.jsonl, summary.json, junit.xml, verdicts.json" + (", comparison.json" if comparison else "")
    console.print(f"Results: {summary['output']}/{files}")
    recorded = summary["run"]
    ui = (
        "martian ui" if data_folder() == Path(recorded["data_dir"]) else f"martian ui --data-dir {recorded['data_dir']}"
    )
    # One line, never wrapped, so the command can be copied.
    console.print(
        f"Run {recorded['code']} is in the local Evals UI: {ui}", markup=False, highlight=False, soft_wrap=True
    )
    # With a baseline the comparison is the gate; a case without a verdict still fails the run.
    if summary["errors"]:
        raise typer.Exit(code=2)
    if comparison is not None:
        raise typer.Exit(code=1 if comparison["gate"] == "failed" else 0)
    raise typer.Exit(code=1 if summary["failed"] else 0)


_NO_RESULT = "Compared with {where}: it holds no result of {what}, so it is gated on its own"
_LABELS = {"better": "better", "worse": "worse, beyond noise", "within_noise": "within noise"}


def _print_matrix(console: Any, path: Path, found: dict[str, Any], *, baseline: Path | None) -> int:
    """What ``martian evals run`` prints for a suite with a matrix: one row per combination, then
    what each combination has to say, and the exit code: 2 when a combination has a case without a
    verdict, else 1 when one failed its gate (:func:`martian.evals.runner.combination_gate`)."""
    from rich.table import Table

    from martian.evals.local import data_folder
    from martian.evals.runner import cache_line, combined_cache, false_refusal_line
    from martian.evals.stats import rate_text

    compared = baseline is not None
    combinations = found["combinations"]
    first = combinations[0]["summary"]
    plan = first.get("repeats")
    names = [axis["name"] for axis in found["axes"]]
    axes = names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"
    notes = ["One run per combination; each row counts that run's cases"]
    if groups := first["groups"]:
        notes.append(f"Intervals allow for {groups} case {'group' if groups == 1 else 'groups'}")
    if plan:
        notes.append("Reliability counts the cases that passed every attempt")
    table = Table(
        title=f"{path.name}: {len(combinations)} combinations of {axes}, {first['cases']} cases each"
        + (f", {plan['attempts']} attempts each" if plan else ""),
        caption="; ".join(notes),
    )
    columns = ["Combination", "Passed", "Failed", "Errors", "Pass rate (95% CI)"]
    columns += ["Reliability (95% CI)"] if plan else []
    columns += ["Against the baseline", "Gate"] if compared else []
    for column in columns:
        # A combination's id is its identity: wrapped, never cut short.
        table.add_column(column, overflow="fold" if column == "Combination" else "ellipsis")
    for entry in combinations:
        summary, comparison = entry["summary"], entry["comparison"]
        row = [entry["id"], str(summary["passed"]), str(summary["failed"]), str(summary["errors"])]
        row.append(rate_text(summary["pass_rate"], summary["confidence_95"]))
        if plan:
            reliable = summary["reliability"]
            row.append(rate_text(reliable["pass_rate"], reliable["confidence_95"]))
        if compared:
            if comparison is None:
                row.append("no result")
            else:
                row.append(_LABELS[comparison["label"]] if comparison["label"] else "no case compared")
            row.append(entry["gate"])
        table.add_row(*row)
    console.print(table)
    # The cache's counts over every combination, once: each combination's are in matrix.json.
    if used := [entry["summary"]["cache"] for entry in combinations if "cache" in entry["summary"]]:
        console.print(cache_line(combined_cache(used)), markup=False, highlight=False, soft_wrap=True)
    unit = "attempt(s)" if plan else "case(s)"
    for entry in combinations:
        summary, comparison = entry["summary"], entry["comparison"]
        lines = []
        if flaky := (summary.get("reliability") or {}).get("flaky"):
            shown = ", ".join(flaky[:5]) + (f" and {len(flaky) - 5} more" if len(flaky) > 5 else "")
            lines.append(f"Flaky cases, whose verdict changed between attempts: {shown}")
        if (refusals := summary.get("false_refusals")) is not None:
            lines.append(false_refusal_line(refusals))
        lines += [f"{count} {unit}: {reason}" for reason, count in list(summary["target_errors"].items())[:5]]
        for label, reasons in summary["check_errors"].items():
            lines += [f"{count} {unit}, {label}: {reason}" for reason, count in list(reasons.items())[:5]]
        if comparison is not None:
            lines += _comparison_lines(comparison)
        elif compared:
            lines.append(_NO_RESULT.format(where=baseline, what="this combination"))
        for line in lines:
            console.print(f"{entry['id']}: {line}", markup=False, highlight=False, soft_wrap=True)
    console.print(
        f"Results: {found['output']}/<combination>/results.jsonl, summary.json, junit.xml, verdicts.json"
        + (", comparison.json" if compared else "")
        + f"; every combination in {found['output']}/matrix.json and verdicts.json",
        markup=False,
        highlight=False,
        soft_wrap=True,
    )
    recorded = combinations[0]["run"]
    ui = (
        "martian ui" if data_folder() == Path(recorded["data_dir"]) else f"martian ui --data-dir {recorded['data_dir']}"
    )
    console.print(
        f"Each combination is a run in the local Evals UI, named after the suite and the combination, such as "
        f"{recorded['code']}: {ui}",
        markup=False,
        highlight=False,
        soft_wrap=True,
    )
    gates = {entry["gate"] for entry in combinations}
    return 2 if "error" in gates else 1 if "failed" in gates else 0


def _comparison_lines(found: dict[str, Any]) -> list[str]:
    """What ``martian evals run --baseline`` prints about the comparison, under the table."""
    where = f"Compared with {found['baseline']}"
    significance = found["significance"]
    if significance is None:
        lines = [f"{where}: no case could be compared"]
    else:
        label = {"better": "better", "worse": "worse, beyond noise", "within_noise": "within noise"}[found["label"]]
        p = significance["p_value"]
        lines = [
            f"{where}: {label}. {significance['compared']} cases compared: {significance['regressed']} from pass "
            f"to fail, {significance['improved']} from fail to pass "
            f"(exact McNemar test, {'p < 0.001' if p < 0.001 else f'p = {p:.2g}'})"
        ]
    if found["regressed"]:
        shown = ", ".join(f"{case['id']} ({', '.join(case['checks'])})" for case in found["regressed"][:10])
        more = len(found["regressed"]) - 10
        lines.append(f"From pass to fail: {shown}" + (f" and {more} more" if more > 0 else ""))
    if leaked := found["security_regressed"]:
        shown = ", ".join(f"{case['id']} ({', '.join(case['checks'])})" for case in leaked[:10])
        more = len(leaked) - 10
        lines.append(
            f"Newly failed a security check, which fails the gate: {shown}" + (f" and {more} more" if more > 0 else "")
        )
    unpaired = [(found["new"], "new"), (found["changed"], "with changed inputs or expectations")]
    unpaired.append((found["unverified"], "without a verdict in one of the runs"))
    if counted := [f"{len(ids)} {what}" for ids, what in unpaired if ids]:
        lines.append(f"Not compared: {', '.join(counted)}")
    if failures := found["unpaired_failures"]:
        shown = ", ".join(failures[:10]) + (f" and {len(failures) - 10} more" if len(failures) > 10 else "")
        lines.append(f"Failed with no result in the baseline to compare with, which fails the gate: {shown}")
    if found["missing"]:
        lines.append(f"{len(found['missing'])} of the baseline's cases did not run")
    return lines


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
