"""One SDK entry point for evals, redteam, and their combined CI gate."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Annotated

import typer

from martian import __version__
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
) -> None:
    """Run pytest with the eval gate; forward paths and pytest options unchanged."""
    if importlib.util.find_spec("pytest") is None or importlib.util.find_spec("mlflow") is None:
        typer.echo("Eval dependencies are missing; install martian-sdk[evals].", err=True)
        raise typer.Exit(code=2)
    import pytest

    required = ["--martian-require-redteam"] if require_redteam else []
    code = pytest.main(["-p", "martian.evals.plugin", "--martian-evals-gate", *required, *ctx.args])
    raise typer.Exit(code=int(code))


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
    for problem in result.problems:
        typer.echo(problem, err=True)
    raise typer.Exit(code=result.exit_code)
