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
    formats: Annotated[
        list[str] | None,
        typer.Option(
            "-f",
            "--format",
            help="Version 2 files: also write html, csv, sarif, junit or all. Repeat it or separate with commas.",
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
            formats=_formats(formats or []),
        )
        return
    if resume is not None:
        _stop("--resume continues a version 2 run; this is a version 0.2 file.")
    if formats:
        _stop("--format writes the reports of a version 2 run; this is a version 0.2 file.")
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


def _formats(values: list[str]) -> list[str]:
    from martian.redteam.report.outputs import parse_formats

    try:
        return parse_formats(values)
    except ValueError as error:
        _stop(str(error))


def _shown_path(path: Path) -> str:
    """``path`` as SARIF results point at it: relative to the working folder when it is inside it."""
    resolved = path.resolve()
    try:
        return resolved.relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _run_version_2(
    config_path: Path,
    *,
    output: Path | None,
    store: bool,
    max_concurrent: int | None,
    attempts_per_probe: int | None,
    resume: str | None = None,
    formats: list[str] | None = None,
) -> None:
    """Run a version 2 file with the red team engine, write its reports, keep it and apply its gate."""
    from contextlib import AsyncExitStack

    from martian.redteam.engine.plan import PlanError, plan
    from martian.redteam.engine.report import write_reports
    from martian.redteam.engine.runs import TargetUnavailable, build_service, build_target, execute, unsupported
    from martian.redteam.engine.service import ServiceSelectionError
    from martian.redteam.gate import evaluate
    from martian.redteam.runfile import RunFile, problems

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

    async def go() -> Any:
        async with AsyncExitStack() as stack:
            service, provenance = build_service(run_file, stack)
            target = build_target(run_file.target)
            stack.push_async_callback(target.close)
            run = await execute(
                run_file, service=service, target=target, provenance=provenance, store=keeper, resume=earlier
            )
            return run, service

    try:
        run, service = asyncio.run(go())
    except (ServiceSelectionError, TargetUnavailable, ConfigError) as error:
        _stop(safe_text(error))
    except (FileNotFoundError, ImportError, AttributeError):
        _stop("Could not load the target. Check its file, dependencies and function name.")
    finally:
        if keeper is not None:
            keeper.close()
    decided = evaluate(run, run_file.gate)
    written, markdown, report = write_reports(
        run,
        destination,
        policy=service.redaction_policy,
        gate=decided.outcome(run_file.gate),
        formats=formats or [],
        run_file=_shown_path(config_path),
    )
    _print_run(report)
    console.print(f"Wrote {written} and {markdown}")
    if keeper is not None:
        console.print(f"Kept in the local store as {run.id}")
    if reused := run.provenance.get("resumed_attempts"):
        console.print(f"Reused {reused} finished attempt(s) of the earlier session; no model was called for them.")
    for warning in decided.warnings:
        errors.print("Gate warning: " + safe_text(warning), style="yellow", markup=False, soft_wrap=True)
    if decided.failures:
        errors.print(
            "Gate failed: " + safe_text("; ".join(decided.failures)), style="red", markup=False, soft_wrap=True
        )
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


@app.command("report")
def report_command(
    run: Annotated[str, typer.Argument(help="A redteam-run.json, or the id of a run in the local store.")],
    formats: Annotated[
        list[str] | None,
        typer.Option("-f", "--format", help="html, csv, sarif, junit or all (the default). Repeat or use commas."),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option("-o", "--output", help="Folder to write to. Default: the run file's folder, else results."),
    ] = None,
    run_file: Annotated[
        Path | None, typer.Option("--run-file", help="The run file, which SARIF results point at.")
    ] = None,
) -> None:
    """Write a version 2 run's reports again, in other formats: redteam-report.json and .md, and each format."""
    from martian.redteam.engine.report import MARKDOWN_NAME, REPORT_NAME, for_report
    from martian.redteam.report import build_report, render_markdown
    from martian.redteam.report.outputs import write_formats

    chosen = _formats(formats or ["all"])
    found, folder = _load_run(run)
    destination = output or folder
    copy = for_report(found)
    standard = build_report(copy)
    destination.mkdir(parents=True, exist_ok=True)
    (destination / REPORT_NAME).write_text(standard.model_dump_json(indent=2), encoding="utf-8")
    (destination / MARKDOWN_NAME).write_text(render_markdown(copy, standard), encoding="utf-8")
    shown = _shown_path(run_file) if run_file is not None else None
    written = write_formats(copy, standard, destination, chosen, run_file=shown)
    names = [REPORT_NAME, MARKDOWN_NAME, *(item.name for item in written)]
    console.print(f"Wrote {', '.join(names)} to {destination}", markup=False)


def _load_run(run: str) -> tuple[Any, Path]:
    """The run ``run`` names, a ``redteam-run.json`` or a run id in the local store, and the folder
    its reports go to by default: the file's folder, else ``results``."""
    from martian.redteam.model import RedteamRun

    path = Path(run)
    if path.is_file():
        try:
            return RedteamRun.model_validate_json(path.read_text(encoding="utf-8")), path.parent
        except (OSError, ValueError):
            _stop(f"{safe_text(path)} is not a red team run (redteam-run.json).")
    return _stored_run(run), Path("results")


def _stored_run(run_id: str) -> Any:
    """The run ``run_id`` from the local store; stops when it is not there."""
    try:
        from martian.redteam.store import RedteamStore
    except ImportError:
        _stop("Reading a run from the local store needs martian-sdk[evals]; or give a redteam-run.json.")
    keeper = RedteamStore.local()
    try:
        found = asyncio.run(keeper.get("default", run_id))
    finally:
        keeper.close()
    if found is None:
        _stop(f"{safe_text(run_id)} is neither a file nor a red team run in the local store.")
    return found


@app.command("compare")
def compare_command(
    baseline: Annotated[str, typer.Argument(help="The baseline run: a redteam-run.json or a run id.")],
    candidate: Annotated[str, typer.Argument(help="The candidate run: a redteam-run.json or a run id.")],
    output: Annotated[
        Path | None,
        typer.Option("-o", "--output", help="Also write redteam-comparison.json and .md to this folder."),
    ] = None,
) -> None:
    """Compare two red team runs on the same probes: new, fixed and unchanged findings, and a paired test."""
    from martian.redteam.report.compare import compare_runs, render_markdown

    before, _ = _load_run(baseline)
    after, _ = _load_run(candidate)
    comparison = compare_runs(before, after)
    text = render_markdown(comparison)
    if output is not None:
        output.mkdir(parents=True, exist_ok=True)
        (output / "redteam-comparison.json").write_text(comparison.model_dump_json(indent=2), encoding="utf-8")
        (output / "redteam-comparison.md").write_text(text, encoding="utf-8")
    console.print(text, markup=False, highlight=False)
    for warning in comparison.warnings:
        errors.print("Not comparable: " + safe_text(warning), style="yellow", markup=False, soft_wrap=True)


pin_app = typer.Typer(no_args_is_help=True, add_completion=False)
app.add_typer(pin_app, name="pin", help="Keep confirmed findings as regression tests, and re-run them with no model.")


@pin_app.command("add")
def pin_add_command(
    run: Annotated[str, typer.Argument(help="A redteam-run.json, or the id of a run in the local store.")],
    finding: Annotated[
        list[str] | None, typer.Option("--finding", help="A finding id to pin. Default: every confirmed finding.")
    ] = None,
    output: Annotated[Path, typer.Option("-o", "--output", help="Folder of pin files.")] = Path("pins"),
) -> None:
    """Pin confirmed findings: save each one's attack, its checks and the reply they hit as a pin file."""
    from martian.redteam.pins import PinError, pin_findings, save

    found, _ = _load_run(run)
    try:
        pins = pin_findings(found, finding)
    except PinError as error:
        _stop(f"Cannot pin: {error}")
    for path in save(pins, output):
        console.print(f"Pinned {path}", markup=False)
    console.print("A pin holds the attack and the reply it hit in clear text: review it before you commit it.")


@pin_app.command("run")
def pin_run_command(
    config_path: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True, help="The run file.")],
    pins: Annotated[Path, typer.Option("--pins", help="Folder of pin files.")] = Path("pins"),
    output: Annotated[Path | None, typer.Option("-o", "--output", help="Overrides output_folder.")] = None,
) -> None:
    """Re-run pinned findings against the run file's target, with no attacker or judge model."""
    from contextlib import AsyncExitStack

    from martian.redteam.engine.runs import TargetUnavailable, build_target
    from martian.redteam.pins import STATUS_WORDS, load, render_markdown, replay
    from martian.redteam.runfile import RunFile, problems

    if found := problems(config_path):
        _stop("The run file is not valid:\n" + "\n".join("  " + safe_text(line) for line in found))
    run_file = RunFile.from_yaml(config_path)
    try:
        loaded = load(pins)
    except (OSError, ValueError) as error:
        _stop(safe_text(error))
    if not loaded:
        _stop(f"No pin file in {safe_text(pins)}: pin a finding with martian redteam pin add.")

    async def go() -> Any:
        async with AsyncExitStack() as stack:
            target = build_target(run_file.target)
            stack.push_async_callback(target.close)
            return await replay(loaded, target, timeout=run_file.target.timeout_seconds or 120.0)

    try:
        report = asyncio.run(go())
    except TargetUnavailable as error:
        _stop(safe_text(error))
    except (FileNotFoundError, ImportError, AttributeError):
        _stop("Could not load the target. Check its file, dependencies and function name.")
    destination = output or Path(run_file.output_folder)
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "redteam-pins.json").write_text(report.model_dump_json(indent=2), encoding="utf-8")
    (destination / "redteam-pins.md").write_text(render_markdown(report), encoding="utf-8")
    table = Table(title="Pinned red team findings (no model called)")
    for column in ("Pin", "Label", "Status"):
        table.add_column(column)
    for item in report.results:
        table.add_row(item.pin_id, item.label, STATUS_WORDS[item.status])
    console.print(table)
    console.print(f"Wrote {destination / 'redteam-pins.json'}", markup=False)
    if not report.passed:
        errors.print("Pins failed: a pinned finding did not show as not reproduced.", style="red", markup=False)
        raise typer.Exit(code=1)


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
