"""Fail-closed gate for CI."""

from __future__ import annotations

from martian_redteam.types import RiskAssessment


class GateFailure(RuntimeError):
    """The assessment failed the configured gate."""


def evaluate_gate(
    assessment: RiskAssessment,
    *,
    fail_on_infrastructure_error: bool = True,
    ignore_target_errors: bool = False,
    pass_rate_threshold: float = 1.0,
) -> None:
    """Raise GateFailure when the job must fail.

    Transport, HTTP, and schema errors always fail the job when
    ``fail_on_infrastructure_error`` is true. Target callback exceptions fail
    unless ``ignore_target_errors`` is true. Scored failures only fail gated
    vulnerability types whose pass rate is below the threshold.
    """
    failures: list[str] = []
    if fail_on_infrastructure_error and assessment.infrastructure_errors:
        failures.append(f"{assessment.infrastructure_errors} infrastructure or schema error(s)")
    if not ignore_target_errors and assessment.target_errors:
        failures.append(f"{assessment.target_errors} target callback error(s)")

    for result in assessment.vulnerability_type_results:
        if not result.gated:
            continue
        if result.pass_rate < pass_rate_threshold:
            failures.append(
                f"{result.vulnerability}/{result.vulnerability_type} pass rate "
                f"{result.pass_rate:.2f} is below {pass_rate_threshold:.2f}"
            )

    if failures:
        raise GateFailure("; ".join(failures))
