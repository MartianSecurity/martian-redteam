"""``martian redteam import`` and ``martian redteam convert``: other tools' red team files.

:func:`register` adds them to the red team CLI (:mod:`martian_redteam.cli`).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Annotated, NoReturn

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


def register(app: typer.Typer) -> None:
    """Add ``import`` and ``convert`` to the red team CLI."""
    app.add_typer(import_app, name="import", help="Bring another tool's red team results into the local store.")
    app.add_typer(convert_app, name="convert", help="Turn another tool's red team config into a run file.")


__all__ = ["convert_app", "import_app", "register"]
