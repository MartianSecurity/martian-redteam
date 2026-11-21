"""The deterministic checks a scoring job runs on a run's saved outputs (MartianSecurity/martian-notes#334).

A scoring request's or saved suite's ``checks`` are scorers of kind ``check`` whose ``type`` is one of
the suite file's check types and whose ``parameters`` are its options
(:mod:`martian.evals.check_options`). The worker builds each as ``martian evals run`` builds the same
entry of a suite file (``runner._build_check``) and runs it on each saved output with the case's
expectations: no model is called, and the verdict and reason are the suite file's. What a saved output
cannot give is the target's call, as for a suite file whose cases bring their outputs:

- the tool-call checks read the calls in the saved reply, unless they name ``source: trace``, which
  a saved output has no spans for, so it is a score's error;
- ``latency`` reads the latency the worker recorded for the case's call, and the case has none when
  its run was imported rather than run by the worker;
- ``cost`` reads the call's token usage and cost, which the worker does not record, so it is a
  score's error, with the words a suite file's run of an untraced target gives.

A check the worker cannot build, or one that raises on an output, is that score's error and never a
pass; a score without a value counts as an error in the job.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from martian.evals.model import CaseResult, ScorerVersion, Value

ERROR_LENGTH = 300


@dataclass(frozen=True)
class Prepared:
    """A check built once for a job: its scorer, or why it could not be built."""

    scorer: Any = None
    error: str = ""


def is_check(scorer: ScorerVersion) -> bool:
    """Whether ``scorer`` is a deterministic check a request named, not one of the built-in code scorers."""
    from martian.evals.worker.scorers import is_builtin

    return scorer.kind == "check" and not is_builtin(scorer)


def prepare(scorers: list[ScorerVersion]) -> dict[str, Prepared]:
    """Each check of ``scorers`` built, by its name."""
    from pydantic import ValidationError

    from martian.config import ConfigError
    from martian.evals import runner

    built: dict[str, Prepared] = {}
    for scorer in scorers:
        if not is_check(scorer):
            continue
        try:
            spec = runner.CheckSpec.model_validate({"type": scorer.type, "name": scorer.name, **scorer.parameters})
            built[scorer.name] = Prepared(scorer=runner._build_check(spec))
        except (ValidationError, ConfigError, ValueError) as error:
            built[scorer.name] = Prepared(error=f"This check cannot be built: {error}"[:ERROR_LENGTH])
    return built


def recorded_call(result: CaseResult) -> Any:
    """The call a case's result records, as the checks that measure a call read it: how long it took,
    and no token usage or cost, which the worker does not record."""
    return measurement_trace(result.latency_ms)


def measurement_trace(latency_ms: float | None, *, tokens: int | None = None, cost_usd: float | None = None) -> Any:
    """The recorded call fields a latency or cost check reads."""
    return SimpleNamespace(
        info=SimpleNamespace(
            execution_duration=latency_ms,
            token_usage=None if tokens is None else {"total_tokens": tokens},
            cost=None if cost_usd is None else {"total_cost": cost_usd},
        ),
        data=SimpleNamespace(spans=[SimpleNamespace(name="target", span_type="AGENT")]),
    )


async def check_score(scorer: ScorerVersion, prepared: Prepared, result: CaseResult) -> tuple[Value, str, str]:
    """One check's value, reason and error for one saved output. A tool-call check with no source reads the
    reply, because the output was saved rather than produced by a call this scoring job saw."""
    call = recorded_call(result) if scorer.type in ("latency", "cost") else None
    return await score_output(prepared, result.output, dict(result.expectations), call)


async def score_output(
    prepared: Prepared, output: Any, expectations: dict[str, Any], trace: Any = None
) -> tuple[Value, str, str]:
    """Apply a prepared deterministic check to one response and its recorded measurements."""
    if prepared.scorer is None:
        return None, "", prepared.error
    try:
        found = await asyncio.to_thread(prepared.scorer.run, outputs=output, expectations=expectations, trace=trace)
    except Exception as failure:  # noqa: BLE001 - a check that raises is that score's error, whatever it raised
        return None, "", str(failure)[:ERROR_LENGTH]
    if found.error is not None:
        return None, "", str(found.error.error_message or "no verdict")[:ERROR_LENGTH]
    value = found.value
    return (value if isinstance(value, bool | int | float | str) else None), str(found.rationale or ""), ""


__all__ = ["Prepared", "check_score", "is_check", "measurement_trace", "prepare", "recorded_call", "score_output"]
