"""CLI: `martian-redteam run config.yaml` and `martian-redteam validate config.yaml`."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from martian._privacy import safe_text
from martian.config import MartianConfig
from martian_redteam import __version__
from martian_redteam.catalog.attacks import ATTACKS
from martian_redteam.catalog.frameworks import FRAMEWORKS
from martian_redteam.catalog.vulnerabilities import VULNERABILITIES
from martian_redteam.config import RunConfig, Settings
from martian_redteam.gate import GateFailure, evaluate_gate
from martian_redteam.runner import _load_callback, _resolve_catalog, _run_async
from martian_redteam.types import RiskAssessment

app = typer.Typer(no_args_is_help=True, add_completion=False, pretty_exceptions_show_locals=False)
console = Console()


def _version_callback(value: bool) -> None:
    if value:
        console.print(__version__)
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option("--version", help="Print the package version and exit.", callback=_version_callback),
    ] = False,
) -> None:
    """Martian red-team CLI. Orchestrates probes locally; simulation and judging stay on Martian Agents."""


def _print_assessment(assessment: RiskAssessment) -> None:
    table = Table(title="Martian red-team assessment")
    table.add_column("Vulnerability")
    table.add_column("Type")
    table.add_column("Pass rate")
    table.add_column("Pass")
    table.add_column("Fail")
    table.add_column("Errors")
    table.add_column("Gated")
    for result in assessment.vulnerability_type_results:
        table.add_row(
            result.vulnerability,
            result.vulnerability_type,
            f"{result.pass_rate:.2f}",
            str(result.passing),
            str(result.failing),
            str(result.errored),
            "yes" if result.gated else "no",
        )
    console.print(table)
    failed = [case for case in assessment.test_cases if case.score == 0 or case.error]
    if not failed:
        return
    detail = Table(title="Failed or errored probes")
    detail.add_column("Vulnerability")
    detail.add_column("Attack")
    detail.add_column("Score")
    detail.add_column("Detail")
    for case in failed:
        detail.add_row(
            f"{case.vulnerability}/{case.vulnerability_type}",
            case.attack_method,
            "-" if case.score is None else str(case.score),
            case.error or case.reason or "",
        )
    console.print(detail)


def _apply_run_overrides(
    config: RunConfig,
    *,
    max_concurrent: int | None,
    attacks_per_vuln: int | None,
) -> RunConfig:
    updates: dict[str, int] = {}
    if max_concurrent is not None:
        updates["max_concurrent"] = max_concurrent
    if attacks_per_vuln is not None:
        updates["attacks_per_vulnerability_type"] = attacks_per_vuln
    if not updates:
        return config
    return config.model_copy(update={"system_config": config.system_config.model_copy(update=updates)})


@app.command("run")
def run_command(
    ctx: typer.Context,
    config_path: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    max_concurrent: Annotated[
        int | None,
        typer.Option(
            "-c",
            "--max-concurrent",
            help="Maximum concurrent operations (overrides system_config.max_concurrent).",
        ),
    ] = None,
    attacks_per_vuln: Annotated[
        int | None,
        typer.Option(
            "-a",
            "--attacks-per-vuln",
            help="Attacks per vulnerability type (overrides system_config.attacks_per_vulnerability_type).",
        ),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option("-o", "--output", help="Output folder path (overrides system_config.output_folder)."),
    ] = None,
) -> None:
    """Run a YAML assessment and apply the CI gate."""
    destination = output or Path("results")
    try:
        config = _apply_run_overrides(
            RunConfig.from_yaml(config_path),
            max_concurrent=max_concurrent,
            attacks_per_vuln=attacks_per_vuln,
        )
        destination = output or Path(config.system_config.output_folder)
        modern = ctx.find_object(MartianConfig) or ctx.find_object(type) is MartianConfig
        if modern:
            from martian.redteam.client import Client

            config.require_platform_origin()
            connection = ctx.find_object(MartianConfig) or MartianConfig.from_env()

            async def execute() -> RiskAssessment:
                async with Client(config=connection) as client:
                    return await _run_async(config, client=client)

            assessment = asyncio.run(execute())
        else:
            configured = ctx.find_object(Settings) or Settings()
            assessment = asyncio.run(_run_async(config, settings=configured))
        written = assessment.save(destination)
        _print_assessment(assessment.for_report())
        console.print(f"Wrote {written}")
    except Exception as error:
        destination.mkdir(mode=0o700, parents=True, exist_ok=True)
        (destination / "execution-error.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "state": "failed",
                    "error_type": type(error).__name__,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        console.print(f"[red]Assessment setup/execution failed:[/red] {type(error).__name__}")
        raise typer.Exit(code=2) from None
    try:
        evaluate_gate(
            assessment,
            fail_on_infrastructure_error=config.gate.fail_on_infrastructure_error,
            ignore_target_errors=config.gate.ignore_target_errors,
            pass_rate_threshold=config.gate.pass_rate_threshold,
        )
    except GateFailure as error:
        console.print(f"[red]Gate failed:[/red] {error}")
        raise typer.Exit(code=1) from error


@app.command("validate")
def validate_command(
    ctx: typer.Context,
    config_path: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
) -> None:
    """Lint a YAML config without contacting Martian Agents."""
    stage = "configuration"
    try:
        config = RunConfig.from_yaml(config_path)
        if ctx.find_object(MartianConfig) or ctx.find_object(type) is MartianConfig:
            config.require_platform_origin()
        vulnerabilities, attacks = _resolve_catalog(config)
        stage = "callback"
        _load_callback(config.target.callback.file, config.target.callback.function)
    except Exception as error:
        if isinstance(error, ValidationError) and stage == "configuration":
            detail = "; ".join(
                e["msg"] for e in error.errors(include_input=False, include_context=False, include_url=False)
            )
        elif stage == "configuration" and isinstance(error, (ValueError, KeyError)):
            detail = str(error)
        else:
            detail = type(error).__name__
        console.print(f"[red]Validation failed:[/red] {safe_text(detail)}")
        raise typer.Exit(code=1) from None
    console.print(
        f"OK: {len(vulnerabilities)} vulnerabilities, {len(attacks)} attacks, callback {config.target.callback.file}",
        no_wrap=True,
        overflow="ignore",
        crop=False,
    )


@app.command("catalog")
def catalog_command() -> None:
    """Print built-in vulnerability, attack, and framework ids."""
    vulns = Table(title="Vulnerabilities")
    vulns.add_column("YAML name")
    vulns.add_column("Id")
    vulns.add_column("Gated")
    vulns.add_column("Types")
    for vuln_name, vuln_factory in VULNERABILITIES.items():
        vulnerability = vuln_factory()
        vulns.add_row(
            vuln_name,
            vulnerability.id,
            "yes" if vulnerability.gated else "no",
            ", ".join(vulnerability.allowed_types),
        )
    console.print(vulns)

    attacks = Table(title="Attacks")
    attacks.add_column("YAML name")
    attacks.add_column("Id")
    attacks.add_column("Kind")
    for attack_name, attack_factory in ATTACKS.items():
        attack = attack_factory()
        attacks.add_row(attack_name, attack.id, attack.kind)
    console.print(attacks)

    frameworks = Table(title="Framework maps (subsets)")
    frameworks.add_column("Id")
    frameworks.add_column("Coverage")
    frameworks.add_column("Vulnerability ids")
    for framework in FRAMEWORKS.values():
        frameworks.add_row(framework.id, framework.coverage, ", ".join(framework.vulnerability_ids))
    console.print(frameworks)


if __name__ == "__main__":
    app()
