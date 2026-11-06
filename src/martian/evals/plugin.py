"""pytest plugin: local MLflow store, fail-closed session rules, and the upload to Backend.

Enable with `-p martian.evals.plugin --martian-evals-gate`. It is deliberately not
auto-registered (it would make every pytest run import MLflow) and, without the flag, it does
nothing, so a project's ordinary unit tests are unaffected.

A pytest session is recorded as one CI session holding every regression test that reached a
gate (MLflow's one-run-per-session shape). With pytest-xdist each worker runs its own local
store and hands its gate records to the controller, which uploads once.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable, Generator, Sequence
from typing import Any

import pytest

from martian.evals.config import ConfigError, current_config
from martian.evals.state import STATE, GateRecord
from martian.result import SessionResult

# The server applies a strict policy unless the uploader declared one, so anything but `passed`
# (including a gate value this kit does not know) fails the job.
_original_evaluate: Callable[..., Any] | None = None
_previous_tracking_uri: str | None = None
_previous_experiment_id: str | None = None
_previous_env: dict[str, str | None] = {}


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--martian-require-export", action="store_true", default=False)
    parser.addoption("--martian-require-evals", action="store_true", default=False)
    parser.addoption("--martian-report", default="", help="Safe session receipt JSON path.")
    parser.addoption(
        "--martian-require-redteam",
        action="store_true",
        default=False,
        help="Require assert_redteam to record a gate in this CI session.",
    )
    group = parser.getgroup("martian-evals")
    group.addoption(
        "--martian-evals-gate",
        action="store_true",
        default=False,
        help="Enforce the Martian evals gate: gates must run, nothing may skip, results go to Backend.",
    )


def _enabled(config: pytest.Config) -> bool:
    return bool(config.getoption("--martian-evals-gate", default=False))


def _is_worker(config: pytest.Config) -> bool:
    return hasattr(config, "workerinput")


def pytest_configure(config: pytest.Config) -> None:
    global _previous_tracking_uri, _previous_experiment_id
    if not _enabled(config):
        return
    STATE.reset()
    STATE.enforce = True
    STATE.worker = _is_worker(config)
    store = os.environ.get("MARTIAN_EVALS_STORE_DIR") or tempfile.mkdtemp(prefix="martian-evals-")
    if STATE.worker:
        store = os.path.join(store, config.workerinput["workerid"])  # type: ignore[attr-defined]
    os.makedirs(store, mode=0o700, exist_ok=True)
    STATE.store_dir = store
    STATE.receipt_path = config.getoption("--martian-report") or os.path.join(store, "martian-session.json")
    STATE.receipt = SessionResult()
    STATE.receipt.save(STATE.receipt_path)
    try:
        configured = current_config()
        if not configured.offline:
            STATE.receipt._bind_redaction(configured.connection.redaction_policy)
    except ConfigError as exc:
        raise pytest.UsageError(f"martian-evals: {exc}") from None
    remote = os.environ.get("MLFLOW_TRACKING_URI", "")
    if remote.startswith(("http://", "https://")):
        raise pytest.UsageError(
            "martian-evals: unset MLFLOW_TRACKING_URI; the kit runs against a local store and "
            "never talks to an MLflow server"
        )
    # `evaluate()` makes an extra first-row prediction unless trace validation is skipped; that
    # would call a side-effecting agent twice. The agent must be traced (the gate fails otherwise).
    import mlflow

    if mlflow.active_run() is not None:
        raise pytest.UsageError("martian-evals: finish the active MLflow run before starting CI")
    for name, value in {
        "MLFLOW_GENAI_EVAL_SKIP_TRACE_VALIDATION": "true",
        "MLFLOW_DISABLE_TELEMETRY": "true",
        "MLFLOW_DISABLE_AGENT_HINT": "1",
    }.items():
        _previous_env[name] = os.environ.get(name)
        os.environ.setdefault(name, value)
    _previous_tracking_uri = mlflow.get_tracking_uri()
    # MLflow 3.16.1 has no public setter that restores this without reading an external store.
    _previous_experiment_id = mlflow.tracking.fluent._active_experiment_id

    mlflow.set_tracking_uri("sqlite:///" + os.path.join(store, "mlflow.db"))
    mlflow.set_experiment("martian-evals")
    config.pluginmanager.import_plugin("mlflow.pytest.plugin")
    _record_scorers()


def _scorer_name(scorer: Any) -> str | None:
    name = getattr(scorer, "name", None) or getattr(scorer, "__name__", None)
    return str(name) if name else None


def _record_scorers() -> None:
    """Remember which scorers each `mlflow.genai.evaluate()` call was given.

    A scorer that returns nothing leaves no column in the result frame, so inferring scorer names
    from the frame cannot see it; `assert_checks` compares against this list instead.
    """
    import mlflow.genai as genai

    global _original_evaluate

    original = genai.evaluate
    _original_evaluate = original
    if getattr(original, "_martian_wrapped", False):
        return

    def evaluate(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        given = kwargs.get("scorers", args[1] if len(args) > 1 else None)
        # An unnamed scorer cannot be asserted by name; keep it visible so the gate fails on it.
        names = [_scorer_name(s) or "<unnamed>" for s in (given or [])]
        try:
            result.__dict__["_martian_scorers"] = names
            result.__dict__["_martian_checked"] = False
        except AttributeError:
            pass
        STATE.evaluated.append(result)
        return result

    evaluate.__dict__["_martian_wrapped"] = True
    genai.evaluate = evaluate


def pytest_unconfigure(config: pytest.Config) -> None:
    global _original_evaluate, _previous_tracking_uri, _previous_experiment_id
    if not _enabled(config):
        return
    STATE.enforce = False
    STATE.current_test = ""
    if _original_evaluate is not None:
        import mlflow.genai as genai

        genai.evaluate = _original_evaluate
        _original_evaluate = None
    if _previous_tracking_uri is not None:
        import mlflow

        mlflow.set_tracking_uri(_previous_tracking_uri)
        vars(mlflow.tracking.fluent)["_active_experiment_id"] = _previous_experiment_id
        _previous_tracking_uri = None
        _previous_experiment_id = None
    for name, value in _previous_env.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    _previous_env.clear()


def pytest_runtest_setup(item: pytest.Item) -> None:
    if STATE.enforce:
        STATE.current_test = item.nodeid


def _is_regression_test(item: pytest.Item) -> bool:
    try:
        from mlflow.pytest.decorator import MLFLOW_TEST_ATTR
    except ImportError:  # the marker module is part of the pinned MLflow
        return False
    return getattr(getattr(item, "function", None), MLFLOW_TEST_ATTR, False) is True


def _failure(test: str, message: str) -> None:
    STATE.record(GateRecord(test=test, kind="checks", check_rows=None, failure=message))


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Generator[None, Any, None]:
    """A regression test must reach a gate, and every evaluate() result must be asserted.

    Without this a test that swallows an error, returns early, or asserts `result.passed` records
    nothing, and the session passes on the strength of another test's gate.
    """
    if not STATE.enforce or not _is_regression_test(item):
        yield
        return
    before, evaluated_before = len(STATE.records), len(STATE.evaluated)
    outcome = yield
    if outcome.excinfo is not None:
        # The saved session must retain failures before a gate or after a pass.
        if not any(record.failure for record in STATE.records[before:]):
            _failure(STATE.test_name(), f"regression test did not complete: {outcome.excinfo[0].__name__}")
        return
    if len(STATE.records) == before:
        _failure(item.nodeid, "the test passed without reaching assert_gate, assert_checks or assert_redteam")
        return
    for result in STATE.evaluated[evaluated_before:]:
        if getattr(result, "_martian_checked", False) is not True:
            _failure(item.nodeid, "an evaluate() result was never passed to assert_gate or assert_checks")
            break


def pytest_deselected(items: Sequence[pytest.Item]) -> None:
    """A deselected regression test (`-k`, `--deselect`, `--lf`) is a gate that did not run."""
    if STATE.enforce and os.environ.get("MARTIAN_EVALS_ALLOW_PARTIAL", "") != "1":
        for item in items:
            if _is_regression_test(item):
                _failure(item.nodeid, "the regression test was deselected (set MARTIAN_EVALS_ALLOW_PARTIAL=1 to allow)")


def pytest_collectreport(report: pytest.CollectReport) -> None:
    """A module skipped at collection (`importorskip`) never produces a test or a skip report."""
    if STATE.enforce and report.skipped:
        _failure(report.nodeid or "collection", "collection was skipped")


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    # Reports are forwarded to the xdist controller; count them once, there.
    if STATE.enforce and not STATE.worker and report.skipped:
        STATE.skipped += 1


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node: Any, error: Any) -> None:
    """xdist controller: collect a finished worker's gate records."""
    output = getattr(node, "workeroutput", None) or {}
    for raw in output.get("martian_records", []):
        STATE.record(GateRecord(**raw))
    if error is not None:
        STATE.worker_errors += 1


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    from martian.evals.session import finish

    finish(session, exitstatus)
