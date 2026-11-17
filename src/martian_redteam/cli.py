"""CLI: `martian-redteam run config.yaml` and `martian-redteam validate config.yaml`."""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from pathlib import Path
from typing import Annotated, Any, NoReturn
from uuid import uuid4

import typer
import yaml
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from martian._errors import message, reference
from martian._privacy import safe_text
from martian.config import ConfigError, MartianConfig
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
errors = Console(stderr=True)


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
    """Martian red-team CLI. Runs probes locally; attacks are generated and judged by your models or by Martian."""


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
    infrastructure = [case for case in assessment.test_cases if case.error]
    if infrastructure:
        counts = Counter((case.error_code, case.error_kind) for case in infrastructure)
        for code, kind in list(counts)[:10]:
            sample = next(case for case in infrastructure if (case.error_code, case.error_kind) == (code, kind))
            errors.print(
                f"{counts[(code, kind)]} probe(s): " + (message(code) if code else sample.error or "Execution failed"),
                markup=False,
            )
            if identifier := reference(sample.evaluation_operation_id or sample.simulation_operation_id):
                errors.print("Operation ID: " + identifier, markup=False)
        if len(counts) > 10:
            errors.print(f"{len(counts) - 10} more error group(s); see the report.")
    failed = [case for case in assessment.test_cases if case.score == 0 and not case.error]
    if not failed:
        return
    detail = Table(title="Failed probes")
    detail.add_column("Vulnerability")
    detail.add_column("Attack")
    detail.add_column("Score")
    detail.add_column("Detail")
    for case in failed[:10]:
        detail.add_row(
            f"{case.vulnerability}/{case.vulnerability_type}",
            case.attack_method,
            "-" if case.score is None else str(case.score),
            "Probe failed its evaluation policy; see the report.",
        )
    console.print(detail)
    if len(failed) > 10:
        console.print(f"{len(failed) - 10} more failed probe(s); see the report.")


def _setup_error(error: Exception) -> tuple[str, str]:
    if isinstance(error, ConfigError):
        return "configuration_error", safe_text(error)
    if isinstance(error, (ValidationError, ValueError, KeyError)):
        return (
            "configuration_error",
            "Invalid assessment configuration. Run 'martian redteam validate' for field checks.",
        )
    if isinstance(error, (FileNotFoundError, ImportError, AttributeError)):
        return "callback_load_failed", "Could not load the callback. Check its file, dependencies, and function name."
    if isinstance(error, OSError):
        return "file_access_failed", "Could not read or write an assessment file. Check paths and permissions."
    return "assessment_execution_failed", "Assessment execution failed. Report the saved error receipt and SDK version."


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
    store: Annotated[
        bool,
        typer.Option(
            "--store/--no-store",
            help="Keep the run in the local store that martian ui reads (needs martian-sdk[evals]).",
        ),
    ] = True,
    resume: Annotated[
        str | None,
        typer.Option(
            "--resume",
            help="Version 2 files: continue this run from the local store, reusing its finished attempts.",
        ),
    ] = None,
) -> None:
    """Run a YAML assessment and apply the CI gate."""
    destination = output or Path("results")
    if _is_version_2(config_path):
        _run_version_2(
            config_path,
            output=output,
            store=store,
            max_concurrent=max_concurrent,
            attempts_per_probe=attacks_per_vuln,
            resume=resume,
        )
        return
    if resume is not None:
        _stop("--resume continues a version 2 run; this is a version 0.2 file.")
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
        if store:
            _keep_locally(assessment)
    except Exception as error:
        code, detail = _setup_error(error)
        identifier = str(uuid4())
        try:
            destination.mkdir(mode=0o700, parents=True, exist_ok=True)
            (destination / "execution-error.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "state": "failed",
                        "error_type": safe_text(type(error).__name__)[:80],
                        "error_code": code,
                        "message": detail,
                        "error_id": identifier,
                        "sdk_version": __version__,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            receipt = "Receipt: " + safe_text(destination / "execution-error.json")
        except OSError:
            receipt = "Could not save the error receipt. Check output directory permissions."
        errors.print(
            f"Assessment failed [{code}]: {detail}\nReport ID: {identifier}\n{receipt}",
            markup=False,
        )
        raise typer.Exit(code=2) from None
    try:
        evaluate_gate(
            assessment,
            fail_on_infrastructure_error=config.gate.fail_on_infrastructure_error,
            ignore_target_errors=config.gate.ignore_target_errors,
            pass_rate_threshold=config.gate.pass_rate_threshold,
        )
    except GateFailure as error:
        errors.print("Gate failed: " + safe_text(error), style="red", markup=False)
        raise typer.Exit(code=1) from None


def _is_version_2(config_path: Path) -> bool:
    from martian.redteam.runfile import read_yaml

    try:
        return "version" in read_yaml(config_path)
    except (OSError, ValueError, yaml.YAMLError):
        return False


def _stop(message: str) -> NoReturn:
    errors.print(message, markup=False, soft_wrap=True)
    raise typer.Exit(code=2)


def _run_version_2(
    config_path: Path,
    *,
    output: Path | None,
    store: bool,
    max_concurrent: int | None,
    attempts_per_probe: int | None,
    resume: str | None = None,
) -> None:
    """Run a version 2 file with the red team engine, write its reports, keep it and apply its gate."""
    from contextlib import AsyncExitStack

    from martian.redteam.engine.gate import gate_failures
    from martian.redteam.engine.plan import PlanError, plan
    from martian.redteam.engine.report import write_reports
    from martian.redteam.engine.runs import (
        SessionHandoverError,
        TargetUnavailable,
        build_service,
        build_target,
        execute,
        run_canary,
        tool_boundary,
        unsupported,
    )
    from martian.redteam.engine.service import ServiceSelectionError
    from martian.redteam.runfile import RunFile, problems
    from martian.redteam.targets import TargetChanged
    from martian.redteam.verify.canary import planted_run_file

    found = problems(config_path)
    if found:
        _stop("The run file is not valid:\n" + "\n".join("  " + safe_text(line) for line in found))
    run_file = RunFile.from_yaml(config_path)
    budget = run_file.budget.model_copy(
        update={
            key: value
            for key, value in (("max_concurrent", max_concurrent), ("attempts_per_probe", attempts_per_probe))
            if value is not None
        }
    )
    run_file = run_file.model_copy(update={"budget": budget})
    if missing := unsupported(run_file):
        _stop("This run file asks for what this release cannot do: " + "; ".join(missing))
    try:
        plan(run_file)
    except PlanError as error:
        _stop(f"The run cannot be planned: {error}")
    destination = output or Path(run_file.output_folder)
    keeper: Any = None
    if store:
        try:
            from martian.redteam.store import RedteamStore

            keeper = RedteamStore.local()
        except ImportError:
            console.print("Not kept in the local store: install martian-sdk[evals] for that.")
    earlier = None
    if resume is not None:
        if keeper is None:
            _stop("--resume reads the run from the local store: drop --no-store, and install martian-sdk[evals].")
        earlier = asyncio.run(keeper.get("default", resume))
        if earlier is None:
            keeper.close()
            _stop(f"No red team run {resume} is in the local store.")

    canary = run_canary(earlier)

    async def go() -> Any:
        async with AsyncExitStack() as stack:
            service, provenance = build_service(run_file, stack)
            planted = planted_run_file(run_file, canary)
            target = build_target(planted.target, base=config_path.resolve().parent)
            stack.push_async_callback(target.close)
            target = await tool_boundary(planted, target, stack, folder=destination)
            run = await execute(
                run_file,
                service=service,
                target=target,
                provenance=provenance,
                store=keeper,
                resume=earlier,
                canary=canary,
            )
            return run, service

    try:
        run, service = asyncio.run(go())
    except (
        ServiceSelectionError,
        TargetUnavailable,
        ConfigError,
        SessionHandoverError,
        ConnectionError,
        TargetChanged,
    ) as error:
        _stop(safe_text(error))
    except (FileNotFoundError, ImportError, AttributeError):
        _stop("Could not load the target. Check its file, dependencies and function name.")
    finally:
        if keeper is not None:
            keeper.close()
    written, markdown, report = write_reports(run, destination, policy=service.redaction_policy)
    _print_run(report)
    console.print(f"Wrote {written} and {markdown}")
    if keeper is not None:
        console.print(f"Kept in the local store as {run.id}")
    if reused := run.provenance.get("resumed_attempts"):
        console.print(f"Reused {reused} finished attempt(s) of the earlier session; no model was called for them.")
    failures = gate_failures(run, run_file.gate)
    if failures:
        errors.print("Gate failed: " + safe_text("; ".join(failures)), style="red", markup=False, soft_wrap=True)
        raise typer.Exit(code=1)


def _print_run(run: Any) -> None:
    counts = run.counts
    table = Table(title=f"Martian red team run {run.id}")
    for column in ("Attempts", "Vulnerable", "Held", "Undetermined", "Stopped", "Errors", "Confirmed", "Suspected"):
        table.add_column(column)
    table.add_row(
        *(
            str(value)
            for value in (
                counts.attempts,
                counts.hits,
                counts.resisted,
                counts.undetermined,
                counts.stopped,
                counts.errors,
                counts.confirmed,
                counts.suspected,
            )
        )
    )
    console.print(table)
    if counts.generations:
        console.print(
            f"Attacker refused or left empty {counts.refusals} of {counts.generations} generations "
            f"({counts.refusals / counts.generations:.0%})."
        )
    if run.budget.get("stopped_by"):
        console.print(f"Stopped by the budget: {run.budget['stopped_by']} ran out.")


def _keep_locally(assessment: RiskAssessment) -> None:
    """Record the run in the local store, where martian ui lists it."""
    try:
        from martian.redteam.model.maps import from_risk_assessment
        from martian.redteam.store import RedteamStore
    except ImportError:
        console.print("Not kept in the local store: install martian-sdk[evals] for that.")
        return
    run = from_risk_assessment(assessment.for_report())
    local = RedteamStore.local()
    try:
        asyncio.run(local.save(run))
    finally:
        local.close()
    console.print(f"Kept in the local store as {run.id}")


def _validate_version_2(config_path: Path) -> None:
    from martian.redteam.runfile import RunFile, problems

    found = problems(config_path)
    if found:
        console.print(f"[red]Validation failed:[/red] {len(found)} problem(s)")
        for line in found:
            console.print("  " + safe_text(line), markup=False, no_wrap=True, overflow="ignore", crop=False)
        raise typer.Exit(code=1)
    run_file = RunFile.from_yaml(config_path)
    console.print(
        f"OK: version 2, {len(run_file.vulnerabilities)} vulnerabilities, {len(run_file.attacks)} attacks, "
        f"{run_file.target.kind} target",
        no_wrap=True,
        overflow="ignore",
        crop=False,
    )


@app.command("validate")
def validate_command(
    ctx: typer.Context,
    config_path: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
) -> None:
    """Lint a YAML config without contacting Martian Agents. Version 2 files list every problem."""
    from martian.redteam.runfile import read_yaml

    try:
        is_version_2 = "version" in read_yaml(config_path)
    except (OSError, ValueError, yaml.YAMLError):
        is_version_2 = False
    if is_version_2:
        _validate_version_2(config_path)
        return
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
