"""CLI: `martian-redteam run config.yaml`."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from martian_redteam.catalog.attacks import get_attack
from martian_redteam.catalog.vulnerabilities import get_vulnerability
from martian_redteam.client import Client
from martian_redteam.config import RunConfig, Settings
from martian_redteam.gate import GateFailure, evaluate_gate
from martian_redteam.orchestrator import a_red_team
from martian_redteam.types import RiskAssessment

app = typer.Typer(no_args_is_help=True, add_completion=False)
console = Console()


def _load_callback(file_path: str, function_name: str) -> Any:
    path = Path(file_path).resolve()
    spec = importlib.util.spec_from_file_location("martian_redteam_target", path)
    if spec is None or spec.loader is None:
        raise FileNotFoundError(f"cannot load callback file: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    callback = getattr(module, function_name, None)
    if callback is None:
        raise AttributeError(f"{path} has no function {function_name}")
    return callback


def _print_assessment(assessment: RiskAssessment) -> None:
    table = Table(title="Martian red-team assessment")
    table.add_column("Vulnerability")
    table.add_column("Type")
    table.add_column("Pass rate")
    table.add_column("Gated")
    table.add_column("Errors")
    for result in assessment.vulnerability_type_results:
        table.add_row(
            result.vulnerability,
            result.vulnerability_type,
            f"{result.pass_rate:.2f}",
            "yes" if result.gated else "no",
            str(result.errored),
        )
    console.print(table)


async def _run_async(config: RunConfig, output: Path | None) -> RiskAssessment:
    callback = _load_callback(config.target.callback.file, config.target.callback.function)
    vulnerabilities = [get_vulnerability(item.name, item.types) for item in config.vulnerabilities]
    attacks = [get_attack(item.name, weight=item.weight) for item in config.attacks]
    settings = Settings()
    if config.api and isinstance(config.api.get("url"), str):
        settings = settings.model_copy(update={"api_url": config.api["url"]})
    async with Client(settings=settings) as client:
        return await a_red_team(
            target=callback,
            vulnerabilities=vulnerabilities,
            attacks=attacks,
            client=client,
            purpose=config.target.purpose,
            max_concurrent=config.system_config.max_concurrent,
            attacks_per_vulnerability_type=config.system_config.attacks_per_vulnerability_type,
        )


@app.command("run")
def run_command(
    config_path: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    output: Annotated[Path | None, typer.Option("-o", "--output")] = None,
) -> None:
    config = RunConfig.from_yaml(config_path)
    assessment = asyncio.run(_run_async(config, output))
    destination = output or Path(config.system_config.output_folder)
    written = assessment.save(destination)
    _print_assessment(assessment)
    console.print(f"Wrote {written}")
    try:
        evaluate_gate(
            assessment,
            fail_on_infrastructure_error=config.gate.fail_on_infrastructure_error,
            pass_rate_threshold=config.gate.pass_rate_threshold,
        )
    except GateFailure as error:
        console.print(f"[red]Gate failed:[/red] {error}")
        raise typer.Exit(code=1) from error


if __name__ == "__main__":
    app()
