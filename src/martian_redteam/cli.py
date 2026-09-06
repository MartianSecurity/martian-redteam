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

from martian_redteam import __version__
from martian_redteam.catalog.attacks import ATTACKS, get_attack
from martian_redteam.catalog.frameworks import FRAMEWORKS
from martian_redteam.catalog.vulnerabilities import VULNERABILITIES, Vulnerability, get_vulnerability
from martian_redteam.client import Client
from martian_redteam.config import NamedSpec, RunConfig, Settings
from martian_redteam.gate import GateFailure, evaluate_gate
from martian_redteam.orchestrator import a_red_team
from martian_redteam.types import RiskAssessment

app = typer.Typer(no_args_is_help=True, add_completion=False)
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


def _vulnerability_from_spec(spec: NamedSpec) -> Vulnerability:
    return get_vulnerability(
        spec.name,
        spec.types,
        id=spec.id,
        criteria=spec.criteria,
        gated=spec.gated,
    )


async def _run_async(config: RunConfig) -> RiskAssessment:
    callback = _load_callback(config.target.callback.file, config.target.callback.function)
    vulnerabilities = [_vulnerability_from_spec(item) for item in config.vulnerabilities]
    attacks = [get_attack(item.name, weight=item.weight) for item in config.attacks]
    settings = config.settings_overlay(Settings())
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
    """Run a YAML assessment and apply the CI gate."""
    config = RunConfig.from_yaml(config_path)
    assessment = asyncio.run(_run_async(config))
    destination = output or Path(config.system_config.output_folder)
    written = assessment.save(destination)
    _print_assessment(assessment)
    console.print(f"Wrote {written}")
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
