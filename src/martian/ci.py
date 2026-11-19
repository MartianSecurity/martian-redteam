"""The Python and CLI CI runner: one pytest execution, one persisted Backend session."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from martian._privacy import REDACTION
from martian.config import CiContext, ConfigError, MartianConfig
from martian.evals.config import CONFIG, EvalsConfig, resolve_ci_config
from martian.evals.gate import GateFailure
from martian.evals.redteam import MAX_PROBES, assert_redteam
from martian.evals.state import STATE, GateRecord
from martian.redteam.client import Client
from martian.result import SessionResult
from martian_redteam.config import RunConfig
from martian_redteam.runner import _resolve_catalog, _run_async
from martian_redteam.types import TestCase

_RUN_LOCK = threading.Lock()


@dataclass(frozen=True)
class CiSuite:
    evals_paths: tuple[Path, ...] = ()
    redteam_config: Path | None = None
    scan: Path | None = None
    """A repository folder to scan (``martian scan``, with its ``.martian/scan.yml``); a required test."""
    output_dir: Path = Path("results")
    pytest_args: tuple[str, ...] = ()
    require_export: bool = False


class _ScanItem(pytest.Item):
    """``martian scan`` of the suite's folder as one test of the session (MartianSecurity/martian-notes#473).
    The scan must be of the session's commit; the test names the gate evaluation the server stored, whose
    verdict the server takes, or says why the scan has none."""

    def __init__(self, *, suite: CiSuite, connection: MartianConfig, ci: CiContext, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.suite = suite
        self.connection = connection
        self.ci = ci

    def runtest(self) -> None:
        from martian.scan import ScanOptions, scan, write_export

        assert self.suite.scan is not None
        destination = self.suite.output_dir / "scan"
        lines: list[str] = []

        def echo(text: str = "", err: bool = False) -> None:
            lines.append(text)

        try:
            outcome = scan(
                ScanOptions(
                    folder=self.suite.scan,
                    sarif_path=destination / "scan.sarif",
                    json_path=destination / "scan.json",
                    expect_sha=self.ci.git_sha,
                ),
                echo=echo,
                config=self.connection,
            )
        except Exception as exc:  # a defect, not a verdict: the test fails with the error's type only
            reason = f"the scan did not run: {type(exc).__name__}"
            STATE.record(GateRecord(test=self.nodeid, kind="scan", scan={"error": reason}, failure=reason))
            raise GateFailure(reason) from None
        finally:
            destination.mkdir(mode=0o700, parents=True, exist_ok=True)
            write_export(destination / "scan.log", ("\n".join(lines) + "\n").encode("utf-8"))
        report: dict[str, Any] = {"scan_id": str(outcome.scan_id)} if outcome.scan_id else {}
        failure = ""
        if outcome.gate_id is not None:
            report["gate_id"] = str(outcome.gate_id)
            if outcome.exit_code:
                failure = f"scan gate exit {outcome.exit_code}: " + (outcome.reasons[0] if outcome.reasons else "")
        else:
            failure = "the scan was not judged: " + (outcome.error or "no verdict")
            report["error"] = failure[:2000]
        STATE.record(GateRecord(test=self.nodeid, kind="scan", scan=report, failure=failure))
        if failure:
            raise GateFailure(failure)

    def repr_failure(self, excinfo: Any, style: Any = None) -> str:
        return str(excinfo.value)

    def reportinfo(self) -> tuple[Path, None, str]:
        return self.path, None, "Martian scan"


class _RedteamItem(pytest.Item):
    def __init__(self, *, suite: CiSuite, connection: MartianConfig, ci: CiContext, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.suite = suite
        self.connection = connection
        self.ci = ci

    def runtest(self) -> None:
        before = len(STATE.records)
        try:
            assert self.suite.redteam_config is not None
            config = RunConfig.from_yaml(self.suite.redteam_config)
            config.require_platform_origin()
            if not config.gate.fail_on_infrastructure_error or config.gate.ignore_target_errors:
                raise GateFailure("required CI redteam probes cannot ignore execution errors")
            vulnerabilities, attacks = _resolve_catalog(config)
            expected = {
                (v.id, t): len(attacks) * config.system_config.attacks_per_vulnerability_type
                for v in vulnerabilities
                for t in (v.types or v.allowed_types)
            }
            if not expected or sum(expected.values()) > MAX_PROBES:
                raise GateFailure(f"redteam suite must plan between 1 and {MAX_PROBES} probes")
            destination = self.suite.output_dir / "redteam"
            destination.mkdir(mode=0o700, parents=True, exist_ok=True)

            def checkpoint(case: TestCase) -> None:
                # Keep completed probes if a later target/graph call or the whole process stops.
                safe = case.model_dump()
                from martian._privacy import redact_data, report_text

                for field in ("input", "actual_output", "reason"):
                    if safe[field] is not None:
                        flag = "MARTIAN_EVALS_REDACT_INPUTS" if field == "input" else "MARTIAN_EVALS_REDACT_OUTPUTS"
                        safe[field] = report_text(safe[field], redact_env=flag, policy=self.connection.redaction_policy)
                safe["turns"] = []
                if safe["error"]:
                    safe["error"] = f"redteam {case.error_kind or 'probe'} error"
                safe = redact_data(safe, policy=self.connection.redaction_policy)
                with (destination / "probes.jsonl").open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(safe) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())

            async def execute() -> Any:
                async with Client(config=self.connection, agent_id=self.ci.agent_id) as client:
                    return await _run_async(config, on_probe=checkpoint, client=client)

            assessment = asyncio.run(execute())
            assessment.save(destination)
            assert_redteam(assessment, threshold=config.gate.pass_rate_threshold, expected=expected)
        except Exception as exc:
            if len(STATE.records) == before:
                STATE.record(
                    GateRecord(
                        test=self.nodeid, kind="redteam", failure=f"redteam execution failed: {type(exc).__name__}"
                    )
                )
            raise GateFailure(STATE.records[-1].failure or "redteam execution failed") from None

    def repr_failure(self, excinfo: Any, style: Any = None) -> str:
        return str(excinfo.value)

    def reportinfo(self) -> tuple[Path, None, str]:
        return self.path, None, "Martian redteam suite"


class _SuitePlugin:
    def __init__(self, suite: CiSuite, connection: MartianConfig, ci: CiContext) -> None:
        self.suite = suite
        self.connection = connection
        self.ci = ci

    @pytest.hookimpl(tryfirst=True)
    def pytest_collection_modifyitems(self, session: pytest.Session, items: list[pytest.Item]) -> None:
        if self.suite.scan is not None:
            items.append(
                _ScanItem.from_parent(
                    session, name="martian:scan", suite=self.suite, connection=self.connection, ci=self.ci
                )
            )
        if self.suite.redteam_config is not None:
            items.append(
                _RedteamItem.from_parent(
                    session,
                    name="martian:redteam",
                    suite=self.suite,
                    connection=self.connection,
                    ci=self.ci,
                )
            )


def run(
    suite: CiSuite,
    *,
    config: MartianConfig | EvalsConfig | None = None,
    ci: CiContext | None = None,
    redteam_settings: object | None = None,
) -> SessionResult:
    """Run required suites once, persist one session, and return a safe result.

    Call serially in a process: pytest and MLflow maintain process-global state.
    No target or judge call is repeated to obtain a passing result. An explicit
    offline pytest preview is available through `martian evals run`, not this gate.
    """
    destination = suite.output_dir / "ci-session.json"
    initial = SessionResult()
    if not _RUN_LOCK.acquire(blocking=False):
        initial.state, initial.exit_code = "failed", 2
        initial.problems = ["a CI suite is already running in this process"]
        return initial
    context = None
    privacy_context = None
    try:
        if redteam_settings is not None:
            raise ConfigError("CI uses one Backend config; direct Agents redteam_settings is no longer accepted")
        configured = resolve_ci_config(config, ci)
        context = CONFIG.set(configured)
        connection = configured.connection
        initial._bind_redaction(connection.redaction_policy)
        privacy_context = REDACTION.set(connection.redaction_policy)
        initial.save(destination)
        if os.environ.get("MARTIAN_EVALS_ALLOW_PARTIAL") == "1":
            raise ConfigError("required CI suites cannot use MARTIAN_EVALS_ALLOW_PARTIAL")
        if configured.offline:
            raise ConfigError("a persisted CI gate requires Backend credentials; offline previews cannot pass CI")
        if not suite.evals_paths and suite.redteam_config is None and suite.scan is None:
            raise ConfigError("select evals, redteam, a scan, or a combination")
        if suite.scan is not None and not suite.scan.is_dir():
            raise ConfigError("the scan folder does not exist")
        if any(not p.exists() for p in suite.evals_paths):
            raise ConfigError("an evals path does not exist")
        if any(
            a in ("-n", "--numprocesses", "--dist") or a.startswith(("-n=", "--numprocesses=", "--dist="))
            for a in suite.pytest_args
        ):
            raise ConfigError("the combined runner is serial; use the pytest plugin for distributed evals")
        # An empty directory prevents pytest from collecting unrelated working-directory tests.
        empty = suite.output_dir / ".collection"
        empty.mkdir(parents=True, exist_ok=True)
        paths = [str(p) for p in suite.evals_paths] or [str(empty)]
        required = ["--martian-require-evals"] if suite.evals_paths else []
        if suite.require_export:
            required.append("--martian-require-export")
        if suite.redteam_config is not None:
            required.append("--martian-require-redteam")
        if suite.scan is not None:
            required.append("--martian-require-scan")
        code = pytest.main(
            [
                "-p",
                "martian.evals.plugin",
                "--martian-evals-gate",
                "--martian-report",
                str(destination),
                "--tb=short",
                "--show-capture=no",
                *required,
                *suite.pytest_args,
                *paths,
            ],
            plugins=[_SuitePlugin(suite, connection, configured.ci or CiContext.from_env())],
        )
        result = (STATE.receipt or initial)._bind_redaction(connection.redaction_policy)
        result.exit_code = int(code)
        if code or result.upload != "persisted":
            result.state = "failed"
            if not result.problems:
                result.problems.append(f"pytest exited with status {int(code)} before a complete session receipt")
            if not code:
                result.exit_code = 1
        result.finished_at = datetime.now(UTC)
        result.save(destination)
        return result
    except Exception as exc:
        initial.state, initial.exit_code = "failed", 2
        # Configuration messages from our own ValueError are controlled; vendor errors can contain secrets.
        initial.problems = [str(exc) if isinstance(exc, ConfigError) else f"CI setup failed: {type(exc).__name__}"]
        initial.finished_at = datetime.now(UTC)
        initial.save(destination)
        return initial
    finally:
        if context is not None:
            CONFIG.reset(context)
        if privacy_context is not None:
            REDACTION.reset(privacy_context)
        _RUN_LOCK.release()
