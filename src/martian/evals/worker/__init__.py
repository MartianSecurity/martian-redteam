"""The worker interface: what the evals core hands its long-running work to.

A write route that starts work records it first, as a queued ``Run`` or ``ScoringJob`` in the
record store, and then calls :meth:`Worker.submit` with a :class:`Work` naming it. The worker does
the work later, outside the request: it calls the target once per case, runs the scorers and
judges, and writes case results, traces and scores through the store and the record store. The API
process never calls a target or a model while answering a request.

The interface is deliberately small, so the editions and the issues that follow can each supply
their own worker without changing the routes:

- :class:`InProcessWorker` runs work as tasks of the API's own event loop. It is enough for the
  local edition and for tests; a run ends with the process that runs it.
- A durable queue and a worker process (MorningHQ/martian-notes#139) implement ``submit`` by
  enqueueing; one process per evaluation run (#140) executes each ``Work`` in its own process.
- Calling HTTP targets from the worker (#137) and running the SDK judge in it (#133) are targets
  and a judge given to the worker; the routes do not change.

A worker learns that someone asked it to stop from the record itself
(``Run.cancel_requested_at``, ``ScoringJob.cancel_requested_at``), which it reads before each case,
so a worker in another process sees it too. :func:`execute_run` and :func:`execute_scoring` are
the work itself, for any worker to call.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from martian.evals.model import Case, ScorerVersion, Target, Value

if TYPE_CHECKING:
    from martian.evals.server.principal import Principal


@dataclass(frozen=True)
class Work:
    """One piece of work: a queued run (``kind="run"``) or a queued scoring job of a finished run
    (``kind="scoring"``, with ``job_id``). The record store holds everything else about it."""

    kind: Literal["run", "scoring"]
    principal: Principal
    project_id: str
    run_id: str
    job_id: str | None = None

    def __post_init__(self) -> None:
        if (self.kind == "scoring") != (self.job_id is not None):
            raise ValueError("scoring work names its job, and only scoring work does")


TargetCall = Callable[[Case], Awaitable[Any]]
"""Calls the thing under test with one case and returns its output. An exception or a timeout is
the case's error; the worker never retries."""


@dataclass(frozen=True)
class JudgeRequest:
    """One saved output for a judge to grade, with the judge scorers to apply. Each scorer's
    ``criteria`` carry its definition: a registered judge's instructions, or a policy judge's
    guidelines.

    Every scorer of one request runs on the same model: ``model``, an MLflow model URI such as
    ``openai:/gpt-4o-mini``, served at ``endpoint``, the base address of an OpenAI-compatible
    endpoint, or at the provider's own address when it is None. ``model`` is None when neither the
    judges nor the default judge model name one; the judge then uses its own. The request never
    carries a key: the judge reads the provider's key from its own environment."""

    principal: Principal
    prompt: str
    expected: str
    output: str
    scorers: tuple[ScorerVersion, ...]
    model: str | None = None
    endpoint: str | None = None


@dataclass(frozen=True)
class Verdict:
    value: Value
    rationale: str = ""


@dataclass(frozen=True)
class JudgeResult:
    """A judge's verdicts by scorer name. ``model`` is the model URI the judge used: the request's
    ``model`` when it names one, or the judge's own. ``model_version`` is the version the provider
    reported for the call (an OpenAI-compatible endpoint's ``model`` in its reply), or empty when
    it reports none. Every score the verdicts make records both, and its ``source_id`` is
    ``model@model_version``, or ``model`` alone without a version."""

    model: str
    verdicts: Mapping[str, Verdict] = field(default_factory=dict)
    model_version: str = ""


Judge = Callable[[JudgeRequest], Awaitable[JudgeResult]]
"""Grades one saved output with a model. A scorer missing from the result, an exception, or a
result from another model than the request names, is that score's error; the worker never
retries."""


@runtime_checkable
class Worker(Protocol):
    @property
    def judges(self) -> bool:
        """Whether this worker can run model judges. When it cannot, the core refuses a scoring
        request that names one, as the server does when no judge is configured."""
        ...

    async def check_target(self, principal: Principal, target: Target) -> dict[str, Any]:
        """Before a run is recorded, check that this worker can call ``target`` for this principal
        and return what pins it, which the run keeps as ``provenance["target"]``. Raises
        ``StoreError`` (``not_found`` for an unknown agent). Never calls the target."""
        ...

    async def submit(self, work: Work) -> None:
        """Accept work to do later. Returns as soon as the work is accepted, before any of it
        runs. Work whose record is no longer queued when its turn comes is skipped."""
        ...


from martian.evals.worker.execute import execute_run, execute_scoring  # noqa: E402
from martian.evals.worker.inprocess import InProcessWorker  # noqa: E402

__all__ = [
    "InProcessWorker",
    "Judge",
    "JudgeRequest",
    "JudgeResult",
    "TargetCall",
    "Verdict",
    "Work",
    "Worker",
    "execute_run",
    "execute_scoring",
]
