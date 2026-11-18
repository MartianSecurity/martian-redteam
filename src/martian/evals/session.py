"""Persist one session and finalize its safe receipt. Never execute an assessment here."""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

import pytest

from martian._errors import concise_problems
from martian._privacy import safe_text
from martian.evals.client import Backend, BackendError
from martian.evals.config import KitConfig, current_config
from martian.evals.receipt import session_receipt
from martian.evals.state import STATE, GateRecord
from martian.result import SessionResult, TestResult

MAX_TESTS = 100
MAX_SESSION_ROWS = 1000
MAX_SESSION_BYTES = 3_800_000


def _placeholder(name: str, reason: str) -> dict[str, Any]:
    return {
        "name": name[:300],
        "kind": "checks",
        "rows": [{"input": "", "output": "", "error": safe_text(reason)[:1900], "scores": {}}],
    }


def _upload_dataset(
    backend: Backend, config: KitConfig, record: GateRecord, problems: list[str], require_export: bool
) -> str | None:
    payload: dict[str, Any] = {
        "request_id": config.request_id(record.dataset_name, scope=record.test),
        "agent_id": config.agent_id,
        "dataset_name": record.dataset_name,
        "dataset_id": record.dataset_id,
        "items_digest": record.items_digest,
        "minimum_coverage": record.minimum_coverage,
        "rows": record.rows,
        "provenance": config.provenance(),
    }
    if record.threshold is not None:
        payload["threshold"] = record.threshold
    try:
        response = backend.upload_run(payload)
    except BackendError as exc:
        problems.append(f"{record.test}: upload to Backend failed: {exc}")
        return None
    server = response.get("verdict")
    if not isinstance(server, dict) or not isinstance(response.get("run_id"), str) or "gate" not in server:
        problems.append(f"{record.test}: Backend returned no verdict")
        return None
    if server["gate"] != "passed" or server.get("errored", 0) or server.get("coverage", 1) < 1:
        problems.append(f"{record.test}: Backend verdict is {server['gate']}")
    local = record.summary.get("scorers", {}).get("exact_match", {})
    if local and (server.get("passed") != local.get("passed") or server.get("total") != record.summary.get("total")):
        problems.append(f"{record.test}: local and server scores disagree")
    STATE.notes.append(
        f"martian-evals: {record.test} -> {response.get('run_code', '?')} gate={server['gate']} "
        f"passed={server.get('passed')} failed={server.get('failed')} errored={server.get('errored')} "
        "(client executed, server scored)"
    )
    if response.get("export_error"):
        STATE.notes.append("martian-evals: dataset MLflow export pending")
        if require_export:
            problems.append(f"{record.test}: required dataset export did not complete")
    return str(response["run_id"])


def _export_receipt(backend: Backend, response: dict[str, Any], required: bool) -> dict[str, Any]:
    """Poll stored export state only. Do not retry model execution or trigger another export."""
    if not required:
        return response
    deadline = time.monotonic() + 30
    while response.get("export_state") == "pending" and time.monotonic() < deadline:
        time.sleep(1)
        response = backend.get_session(str(response["session_id"]))
    return response


def finish(session: pytest.Session, exitstatus: int) -> None:
    if not STATE.enforce:
        return
    config = session.config
    require_export = (
        bool(config.getoption("--martian-require-export")) or os.environ.get("MARTIAN_EVALS_REQUIRE_EXPORT") == "1"
    )
    if STATE.worker:
        config.workeroutput["martian_records"] = [asdict(r) for r in STATE.records]  # type: ignore[attr-defined]
        return
    records = list(STATE.records)
    problems: list[str] = []
    if config.getoption("--martian-require-evals") and not any(r.kind != "redteam" for r in records):
        records.append(
            GateRecord(test="required evals gate", kind="checks", failure="the required evals gate did not run")
        )
    if config.getoption("--martian-require-redteam") and not any(r.kind == "redteam" for r in records):
        records.append(
            GateRecord(
                test="required redteam gate", kind="checks", failure="the required assert_redteam gate did not run"
            )
        )
    if STATE.skipped:
        problems.append(f"{STATE.skipped} test(s) skipped: a skipped gate is a failed gate")
    if STATE.worker_errors:
        problems.append(f"{STATE.worker_errors} xdist worker(s) ended with an error")
    if not records:
        problems.append("no assert_gate, assert_checks or assert_redteam call ran (no test reached its gate)")
    if problems or exitstatus != 0:
        records.append(
            GateRecord(
                test="CI session execution",
                kind="checks",
                failure="; ".join(problems) or f"pytest session exited with status {int(exitstatus)}",
            )
        )
    seen: set[str] = set()
    unique: list[GateRecord] = []
    keys: set[tuple[str, str]] = set()
    for record in records:
        key = (record.test, record.failure)
        if record.rows is None and record.check_rows is None and record.redteam is None:
            if key in keys:
                continue
            keys.add(key)
        base, n = record.test[:290], 2
        while record.test in seen:
            record.test = f"{base}#{n}"
            n += 1
        seen.add(record.test)
        unique.append(record)
        if record.failure:
            problems.append(f"{record.test}: gate failed: {record.failure}")
    records = unique
    receipt = STATE.receipt or SessionResult()
    receipt.state, receipt.upload = "uploading", "pending"
    receipt.tests = [
        TestResult(
            name=r.test,
            kind=r.kind,
            passed=not bool(r.failure),
            provenance=("client_executed_server_scored" if r.kind == "dataset" else "client_executed_client_scored"),
        )
        for r in records
    ]
    receipt.problems = list(problems)
    if STATE.receipt_path:
        receipt.save(STATE.receipt_path)  # Evidence precedes the first required upload.
    kit = current_config()
    backend = (
        None
        if kit.offline
        else Backend(
            kit.connection.api_url,
            kit.connection.token.get_secret_value(),
            timeout=kit.connection.request_timeout_seconds,
            trust_env=kit.connection.trust_env,
        )
    )
    tests: list[dict[str, Any]] = []
    response: dict[str, Any] | None = None
    try:
        for index, record in enumerate(records):
            if record.kind == "dataset":
                run_id = (
                    _upload_dataset(backend, kit, record, problems, require_export)
                    if backend and record.rows is not None
                    else None
                )
                if run_id:
                    tests.append({"name": record.test, "kind": "dataset", "run_id": run_id})
                    receipt.tests[index].run_id = run_id
                    if record.failure:
                        tests.append(_placeholder(record.test[:280] + "#runner", "gate failed: " + record.failure))
                elif backend:
                    tests.append(_placeholder(record.test, record.failure or "the dataset run could not be uploaded"))
            elif record.kind == "redteam" and record.redteam is not None:
                tests.append({"name": record.test, "kind": "redteam", "redteam": record.redteam})
            elif record.check_rows is not None:
                tests.append({"name": record.test, "kind": "checks", "rows": record.check_rows})
            else:
                tests.append(_placeholder(record.test, record.failure or "no results were recorded"))
        if backend and tests:
            payload = {
                "request_id": kit.request_id("session"),
                "agent_id": kit.agent_id,
                "provenance": kit.provenance(),
                "tests": tests,
            }
            size = len(json.dumps(payload, default=str).encode())
            rows = sum(len(t.get("rows", [])) + len(t.get("redteam", {}).get("rows", [])) for t in tests)
            if len(tests) > MAX_TESTS or rows > MAX_SESSION_ROWS or size > MAX_SESSION_BYTES:
                problems.append(f"the session is too large to record ({len(tests)} tests, {rows} rows, {size} bytes)")
            else:
                response = backend.upload_session(payload)
                acknowledged = session_receipt(response, tests)
                run_ids = {test.name: test.run_id for test in receipt.tests}
                receipt.tests = [
                    TestResult(
                        name=test.name,
                        kind=test.kind,
                        passed=test.passed,
                        run_id=run_ids.get(test.name),
                        provenance="client_executed_server_scored"
                        if test.kind == "dataset"
                        else "client_executed_client_scored",
                    )
                    for test in acknowledged.tests
                ]
                receipt.session_id = str(acknowledged.session_id)
                receipt.verdict = acknowledged.verdict
                receipt.upload = "persisted"
                response = _export_receipt(backend, response, require_export)
                session_receipt(response, tests)
                receipt.export_state = str(response.get("export_state", "unknown"))
                if response["verdict"] == "failed":
                    problems.append("Backend session verdict is failed")
                if require_export and receipt.export_state not in (
                    "exported",
                    "not_required",
                ):
                    problems.append("required MLflow export did not complete")
        if backend and receipt.upload != "persisted":
            problems.append("required session upload did not complete")
            receipt.upload = "failed"
    except BackendError as exc:
        problems.append(f"session upload to Backend failed: {exc}")
        if receipt.upload != "persisted":
            receipt.upload = "failed"
    finally:
        if backend:
            backend.close()
    if problems and session.exitstatus in (0, pytest.ExitCode.NO_TESTS_COLLECTED):
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
    receipt.problems = problems
    receipt.finished_at = datetime.now(UTC)
    receipt.exit_code = int(session.exitstatus)
    receipt.state = "failed" if receipt.exit_code or problems else "offline" if kit.offline else "passed"
    if kit.offline:
        receipt.upload = "offline"
    STATE.receipt = receipt
    if STATE.receipt_path:
        receipt.save(STATE.receipt_path)
    terminal = config.pluginmanager.get_plugin("terminalreporter")
    if terminal:
        for note in STATE.notes:
            terminal.write_line(safe_text(note))
        if response:
            terminal.write_line(
                f"martian-evals: session {receipt.session_id} verdict={receipt.verdict} "
                f"tests={response.get('passed_count')}/{response.get('test_count')}"
            )
        for problem in concise_problems(problems):
            terminal.write_line("martian-evals: FAIL " + safe_text(problem))
        terminal.write_line(f"martian-evals: local store {STATE.store_dir}")
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        try:
            with open(summary_path, "a", encoding="utf-8") as handle:
                handle.write(f"\n## Martian CI\n\n{receipt.state}; session `{receipt.session_id or 'not persisted'}`\n")
                for test in receipt.tests:
                    handle.write(
                        f"- {'pass' if test.passed else 'FAIL'} {safe_text(test.name)} "
                        f"({test.kind}, {test.provenance})\n"
                    )
                for problem in problems:
                    handle.write("- " + safe_text(problem).replace("\n", " ") + "\n")
        except OSError:
            pass
