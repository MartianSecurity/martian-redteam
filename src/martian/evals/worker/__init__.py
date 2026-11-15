"""The worker interface: what the evals core hands its long-running work to.

A write route that starts work records it first, as a queued ``Run`` or ``ScoringJob`` in the
record store, and then calls :meth:`Worker.submit` with a :class:`Work` naming it. The worker does
the work later, outside the request: it calls the target once per case, runs the scorers and
judges, and writes case results, traces and scores through the store and the record store. The API
process never calls a target or a model while answering a request.

The interface is deliberately small, so the editions and the issues that follow can each supply
their own worker without changing the routes:

- :class:`ProcessWorker` runs each piece of work in a new process of its own, with the MLflow
  workspace of the work's organisation fixed for the life of that process. MLflow keeps its
  workspace, active experiment and tracking URI for the whole process, so no process ever runs
  two organisations' evaluations (:mod:`martian.evals.worker.process`).
- :class:`InProcessWorker` runs one organisation's work as tasks of the calling process's event
  loop and refuses any other organisation's. It is enough for the local edition and for tests,
  and it runs the work inside each of ``ProcessWorker``'s processes.
- A durable queue (MorningHQ/martian-notes#139) implements ``submit`` by enqueueing, and hands
  each ``Work`` to a process of its own.
- Calling HTTP targets from the worker (#137) is a target given to the worker; the routes do not
  change. The judge given to the worker is :func:`sdk_judge`, the SDK's own judge code, or
  :func:`agents_judge`, which asks martian-agents (:mod:`martian.evals.worker.judges`).

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
    carries a key: the judge reads the provider's key from its own environment. ``job_id`` is the
    scoring job the request belongs to."""

    principal: Principal
    prompt: str
    expected: str
    output: str
    scorers: tuple[ScorerVersion, ...]
    model: str | None = None
    endpoint: str | None = None
    job_id: str = ""


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
    ``model@model_version``, or ``model`` alone without a version. ``judge_version`` names the
    judge's own prompt and rules (``response-rubrics-v1`` for the SDK judge), recorded on each
    score. ``errors`` holds, by scorer name, why the judge gave a scorer of the request no verdict,
    such as a judge kind it cannot grade."""

    model: str
    verdicts: Mapping[str, Verdict] = field(default_factory=dict)
    model_version: str = ""
    judge_version: str = ""
    errors: Mapping[str, str] = field(default_factory=dict)


class JudgeError(Exception):
    """A judge's failure or refusal whose message is safe to show: it becomes the error of every
    score of the request. It never holds the provider's own error text, a key, or the graded
    text. Any other exception is logged, and its scores get a fixed error."""


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


from martian.evals.worker.execute import execute_run, execute_scoring, fail_unfinished  # noqa: E402
from martian.evals.worker.inprocess import InProcessWorker  # noqa: E402
from martian.evals.worker.judges import AgentsConnection, agents_judge, sdk_judge  # noqa: E402
from martian.evals.worker.process import ProcessWorker, WorkerServices, dump_work, load_work  # noqa: E402

__all__ = [
    "AgentsConnection",
    "InProcessWorker",
    "Judge",
    "JudgeError",
    "JudgeRequest",
    "JudgeResult",
    "ProcessWorker",
    "TargetCall",
    "Verdict",
    "Work",
    "Worker",
    "WorkerServices",
    "agents_judge",
    "dump_work",
    "execute_run",
    "execute_scoring",
    "fail_unfinished",
    "load_work",
    "sdk_judge",
]
