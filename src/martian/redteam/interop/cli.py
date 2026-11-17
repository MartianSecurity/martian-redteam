"""``martian redteam import``, ``convert`` and ``garak``: other tools' red team files and probes.

:func:`register` adds them to the red team CLI (:mod:`martian_redteam.cli`).
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer
from rich.console import Console

from martian._privacy import safe_text

import_app = typer.Typer(no_args_is_help=True, add_completion=False)
convert_app = typer.Typer(no_args_is_help=True, add_completion=False)
console = Console()
errors = Console(stderr=True)

DATA_DIR_HELP = (
    "The local data folder that martian ui reads, as for `martian ui`. Default: MARTIAN_EVALS_HOME, else "
    "~/.martian/evals."
)


def _stop(message: str) -> NoReturn:
    errors.print(message, markup=False, soft_wrap=True)
    raise typer.Exit(code=2)


def _needs_evals(what: str) -> None:
    if any(importlib.util.find_spec(name) is None for name in ("mlflow", "sqlalchemy")):
        _stop(f"{what} needs martian-sdk[evals].")


def _ui_hint(data_dir: Path | None) -> str:
    from martian.evals.local import data_folder

    home = data_folder(data_dir)
    return "martian ui" if data_folder() == home else f"martian ui --data-dir {home}"


@import_app.command("promptfoo")
def import_promptfoo(
    files: Annotated[
        list[Path],
        typer.Argument(
            dir_okay=False,
            show_default=False,
            help="promptfoo red team results, as `promptfoo redteam run -o results.json` writes.",
        ),
    ],
    data_dir: Annotated[
        Path | None, typer.Option("--data-dir", file_okay=False, show_default=False, help=DATA_DIR_HELP)
    ] = None,
) -> None:
    """Import promptfoo red team results as red team runs in the local store: one run per provider.
    Every hit is suspected: promptfoo's grader is a model that read the reply.

    martian redteam import promptfoo results.json
    """
    _needs_evals("Importing promptfoo results")
    from martian.redteam.interop.promptfoo import PromptfooConversionError, import_results

    try:
        runs = import_results(files, data_dir)
    except PromptfooConversionError as error:
        _stop(f"Import failed: {safe_text(error)}")
    for run in runs:
        counts = run.counts
        source = run.provenance["promptfoo"]
        console.print(
            f"{Path(source['results']).name}: provider {source['provider']['label'] or source['provider']['id']}, "
            f"run {run.id}: {counts.attempts} attempts, {counts.hits} vulnerable, {counts.resisted} held, "
            f"{counts.errors} errors; {counts.suspected} suspected findings",
            markup=False,
            highlight=False,
            soft_wrap=True,
        )
    console.print(f"Open them in the local UI: {_ui_hint(data_dir)}", markup=False, highlight=False)


@convert_app.command("promptfoo")
def convert_promptfoo(
    config: Annotated[
        Path,
        typer.Argument(dir_okay=False, show_default=False, help="A promptfoo config with a redteam section."),
    ],
    output: Annotated[
        Path,
        typer.Option("--output", file_okay=False, show_default=False, help="The folder to write redteam.yaml to."),
    ],
    target: Annotated[
        str | None,
        typer.Option(
            "--target",
            show_default=False,
            help="FILE.py[:FUNCTION], a function that takes the message and returns the reply. Replaces the "
            "config's target.",
        ),
    ] = None,
    purpose: Annotated[
        str | None,
        typer.Option("--purpose", show_default=False, help="What the target is for, when the config has no purpose."),
    ] = None,
    attack: Annotated[
        list[str] | None,
        typer.Option(
            "--attack",
            show_default=False,
            help="An attack method to add, such as PromptInjection (martian redteam catalog). Repeat it for more.",
        ),
    ] = None,
) -> None:
    """Convert a promptfoo red team config into a version 2 run file, where its features map, and list
    everything that does not.

    martian redteam convert promptfoo promptfooconfig.yaml --output converted
    """
    from martian.redteam.interop.promptfoo import (
        PromptfooConversionError,
        convert_config,
        report_lines,
        write_conversion,
    )

    try:
        conversion = convert_config(config, output=output, target=target, purpose=purpose, attacks=attack)
        written = write_conversion(conversion, output, source=config)
    except PromptfooConversionError as error:
        _stop(f"Conversion failed: {safe_text(error)}")
    for line in report_lines(conversion):
        console.print(line, markup=False, highlight=False, soft_wrap=True)
    console.print(
        f"Wrote {written}. Check it with: martian redteam validate {written}",
        markup=False,
        highlight=False,
        soft_wrap=True,
    )


GARAK_MISSING = (
    "garak is not installed, so the garak probes were skipped. garak is not bundled with martian-sdk; "
    "install it in this environment (pip install garak, Apache-2.0) and run the command again."
)


def _run_file(path: Path) -> Any:
    from martian.redteam.runfile import RunFile, problems

    if found := problems(path):
        _stop("The run file is not valid:\n" + "\n".join("  " + safe_text(line) for line in found))
    run_file = RunFile.from_yaml(path)
    if run_file.environment is not None or run_file.tools.proxy:
        _stop("garak attacks the run file's target directly: remove environment and tools.proxy.")
    return run_file


def _summary(run: Any) -> str:
    counts = run.counts
    return (
        f"run {run.id}: {counts.attempts} attempts, {counts.hits} vulnerable, {counts.resisted} held, "
        f"{counts.undetermined} undetermined, {counts.errors} errors; {counts.confirmed} confirmed and "
        f"{counts.suspected} suspected findings"
    )


def _keep(run: Any, data_dir: Path | None) -> None:
    """Keep the run in the local store as a run of the engine is kept: content as digests unless
    MARTIAN_REPORT_INCLUDE_CONTENT=1, credentials redacted."""
    from martian.redteam.engine.report import for_report
    from martian.redteam.store import RedteamStore

    store = RedteamStore.local(data_dir)
    try:
        asyncio.run(store.save(for_report(run)))
    finally:
        store.close()


def garak_command(
    run_file_path: Annotated[
        Path, typer.Argument(exists=True, dir_okay=False, readable=True, help="A version 2 run file: its target.")
    ],
    probes: Annotated[str, typer.Option("--probes", help="garak's probes, such as encoding or dan.DanInTheWild.")],
    output: Annotated[
        Path | None, typer.Option("-o", "--output", help="Output folder. Default: the run file's output_folder.")
    ] = None,
    generations: Annotated[
        int | None, typer.Option("--generations", min=1, help="Outputs garak asks for per prompt.")
    ] = None,
    garak_arg: Annotated[
        list[str] | None, typer.Option("--garak-arg", help="One more argument for garak. Repeat it for more.")
    ] = None,
    store: Annotated[
        bool, typer.Option("--store/--no-store", help="Keep the run in the local store that martian ui reads.")
    ] = True,
    data_dir: Annotated[
        Path | None, typer.Option("--data-dir", file_okay=False, show_default=False, help=DATA_DIR_HELP)
    ] = None,
) -> None:
    """Run garak's probes against a run file's target, then report them as a red team run and apply the
    run file's gate. garak's detector hits are suspected; the run file's text, canary and tool-call checks
    confirm. Needs garak installed; it is not bundled.

    martian redteam garak redteam.yaml --probes encoding
    """
    import os
    import subprocess

    from martian.redteam.interop import garak

    if importlib.util.find_spec("garak") is None:
        _stop(GARAK_MISSING)
    if store:
        _needs_evals("Keeping the run in the local store")
    run_file = _run_file(run_file_path)
    destination = (output or Path(run_file.output_folder)).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    prefix = destination / "garak"
    report = prefix.with_name("garak.report.jsonl")
    ledger = destination / "garak-calls.jsonl"
    for stale in (report, ledger):
        stale.unlink(missing_ok=True)
    environment = {
        **os.environ,
        garak.RUN_FILE_ENV: str(run_file_path.resolve()),
        garak.LEDGER_ENV: str(ledger),
    }
    command = garak.garak_command(probes, report_prefix=prefix, generations=generations, extra=garak_arg or [])
    try:
        finished = subprocess.run(command, env=environment, check=False)
        if finished.returncode != 0:
            _stop(f"garak exited with {finished.returncode}; no run was kept.")
        if not report.exists():
            _stop(f"garak wrote no report at {report}; no run was kept.")
        try:
            run = garak.report_run(report, run_file=run_file, ledger=ledger)
        except garak.GarakError as error:
            _stop(safe_text(error))
    finally:
        ledger.unlink(missing_ok=True)  # it holds every prompt and reply in clear text
    _finish(run, run_file, destination, run_file_path, store=store, data_dir=data_dir)


def _finish(run: Any, run_file: Any, destination: Path, path: Path, *, store: bool, data_dir: Path | None) -> None:
    from martian.redteam.engine.report import write_reports
    from martian.redteam.gate import evaluate

    decided = evaluate(run, run_file.gate)
    written, markdown, _ = write_reports(run, destination, gate=decided.outcome(run_file.gate), run_file=str(path))
    console.print(_summary(run), markup=False, highlight=False, soft_wrap=True)
    console.print(f"Wrote {written} and {markdown}", markup=False, highlight=False, soft_wrap=True)
    if store:
        _keep(run, data_dir)
        console.print(f"Kept in the local store as {run.id}. Open it with: {_ui_hint(data_dir)}", markup=False)
    for warning in decided.warnings:
        errors.print("Gate warning: " + safe_text(warning), markup=False, soft_wrap=True)
    if decided.failures:
        errors.print("Gate failed: " + safe_text("; ".join(decided.failures)), markup=False, soft_wrap=True)
        raise typer.Exit(code=1)


@import_app.command("garak")
def import_garak(
    report: Annotated[
        Path, typer.Argument(exists=True, dir_okay=False, readable=True, help="garak's report, *.report.jsonl.")
    ],
    run_file_path: Annotated[
        Path | None,
        typer.Option(
            "--run-file",
            exists=True,
            dir_okay=False,
            help="The run file whose target garak attacked: its purpose, and its text, canary and tool-call checks.",
        ),
    ] = None,
    ledger: Annotated[
        Path | None,
        typer.Option(
            "--ledger",
            exists=True,
            dir_okay=False,
            help="The calls MARTIAN_GARAK_LEDGER recorded, for tool-call checks.",
        ),
    ] = None,
    data_dir: Annotated[
        Path | None, typer.Option("--data-dir", file_okay=False, show_default=False, help=DATA_DIR_HELP)
    ] = None,
) -> None:
    """Import a garak report as a red team run in the local store. garak's detector hits are suspected;
    the run file's text, canary and tool-call checks confirm.

    martian redteam import garak garak.report.jsonl --run-file redteam.yaml
    """
    _needs_evals("Importing a garak report")
    from martian.redteam.interop.garak import GarakError, report_run

    run_file = _run_file(run_file_path) if run_file_path is not None else None
    try:
        run = report_run(report, run_file=run_file, ledger=ledger)
    except GarakError as error:
        _stop(f"Import failed: {safe_text(error)}")
    _keep(run, data_dir)
    console.print(f"{report.name}: {_summary(run)}", markup=False, highlight=False, soft_wrap=True)
    console.print(f"Open it in the local UI: {_ui_hint(data_dir)}", markup=False, highlight=False)


def register(app: typer.Typer) -> None:
    """Add ``import``, ``convert`` and ``garak`` to the red team CLI."""
    app.add_typer(import_app, name="import", help="Bring another tool's red team results into the local store.")
    app.add_typer(convert_app, name="convert", help="Turn another tool's red team config into a run file.")
    app.command("garak")(garak_command)


__all__ = ["GARAK_MISSING", "convert_app", "garak_command", "import_app", "register"]
