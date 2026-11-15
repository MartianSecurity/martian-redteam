"""The shared eval data model: one set of objects for the SDK, the local edition and the server.

A suite file, a local ``results.jsonl`` and the server's evals tables all describe these objects;
``docs/eval-model.md`` maps every field of each onto them. Tenancy, request receipts and storage
mechanics are not part of the model: each edition adds its own around it. MLflow stays the store
underneath, so an object that lives there carries its MLflow identifiers in ``store``.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SerializerFunctionWrapHandler, model_serializer, model_validator

MODEL_VERSION = "martian-eval-model-v1"

Identifier = Annotated[str, Field(min_length=1, max_length=200)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Value = bool | int | float | str | None

TargetKind = Literal["function", "http", "agent", "stored"]
ScorerKind = Literal["llm_judge", "check", "code", "human"]
DatasetSource = Literal["file", "draft", "global", "upload", "traces"]
RunSource = Literal["local", "console", "ci"]
RunState = Literal["queued", "running", "completed", "partial", "failed", "cancelled", "stopped"]
"""``stopped``: a run or scoring job that ended before its last case at one of its limits
(:class:`RunLimits`). It keeps its finished cases and can be resumed."""
LimitName = Literal["max_seconds", "max_judge_tokens"]
CaseStatus = Literal["pending", "passed", "failed", "error", "skipped"]
ScoreSource = Literal["code", "llm_judge", "human"]
QuestionKind = Literal["boolean", "numeric", "categorical", "text"]
CombineRule = Literal["all", "majority", "any"]
"""How a check's verdicts over a case's attempts become one: every attempt passes, more than half
pass, or at least one passes (``martian.evals.repeats``)."""

MAX_ATTEMPTS = 100
"""The most attempts a suite runs of each case."""

MAX_TURNS = 100
"""The most user turns one conversation case has."""

CheckScope = Literal["turn", "conversation"]
"""What a scorer grades in a conversation case: each turn's reply (``turn``, the default), or the
whole conversation once (``conversation``). A case of one input is graded by every scorer alike."""


def digest(value: Any) -> str:
    """SHA-256 of a value's canonical JSON: keys sorted, no whitespace, UTF-8."""
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class _Object(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class StoreRef(_Object):
    """Where an object lives in the MLflow store. Empty for an object never written there."""

    experiment_id: str | None = None
    run_id: str | None = None
    dataset_id: str | None = None
    trace_id: str | None = None
    assessment_id: str | None = None


class Project(_Object):
    """A named body of evaluation work. In MLflow, one experiment."""

    id: Identifier
    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    created_at: datetime | None = None
    archived_at: datetime | None = None
    store: StoreRef = StoreRef()


class Turn(_Object):
    """One user turn of a conversation case: what the target receives this turn, what to check its
    reply against, and the turn-scoped scorers that grade it, by name. ``checks`` left out (None)
    means every turn-scoped scorer of the suite grades the turn; an empty list means none does."""

    inputs: dict[str, Any] = Field(min_length=1)
    expectations: dict[str, Any] = Field(default_factory=dict)
    checks: list[Identifier] | None = None


class SimulatedUser(_Object):
    """A user a model plays in a conversation case, in place of scripted turns
    (:mod:`martian.evals.simulation`): what the user wants (``goal``), who they are and how they
    write (``persona``), the most messages they send (``max_turns``), and when they stop
    (``stop_when``). The simulator writes each message from the conversation so far, and says at
    the end whether the goal was reached."""

    goal: str = Field(min_length=1, max_length=4000)
    persona: str = Field(min_length=1, max_length=4000)
    max_turns: int = Field(ge=1, le=MAX_TURNS)
    stop_when: str = Field(min_length=1, max_length=4000)


class Case(_Object):
    """One test case: what the target receives, what to check it against, and optionally a stored
    output to grade instead of calling a target.

    A conversation case has ``turns`` instead of ``inputs``: the user turns the target receives in
    order, in one conversation. Its ``expectations`` are the conversation's, for the scorers that
    grade it as a whole. It always runs against a target, so it has no stored ``outputs``.
    ``turns`` is left out when empty, so a case of one input has the digest it had before the field
    existed.

    A simulated conversation case has a ``simulated_user`` instead of ``turns``: a model writes the
    user's turns as the conversation goes. Its ``expectations`` are the conversation's and each of
    its turns'. ``simulated_user`` is left out when there is none, so other cases keep their
    digests."""

    id: Identifier
    inputs: dict[str, Any] = Field(default_factory=dict)
    expectations: dict[str, Any] = Field(default_factory=dict)
    outputs: Any = None
    tags: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    turns: list[Turn] = Field(default_factory=list, max_length=MAX_TURNS)
    simulated_user: SimulatedUser | None = None

    @model_validator(mode="after")
    def inputs_or_turns(self) -> Case:
        given = [name for name in ("inputs", "turns", "simulated_user") if getattr(self, name)]
        if not given:
            raise ValueError("a case needs inputs, turns for a conversation, or a simulated user")
        if self.turns and self.inputs:
            raise ValueError("a conversation case gives its inputs in each turn, not as the case's inputs")
        if len(given) > 1:
            raise ValueError("a case with a simulated user has neither inputs nor turns: the simulator writes them")
        if (self.turns or self.simulated_user) and self.outputs is not None:
            raise ValueError("a conversation case runs against a target, so it has no stored outputs")
        return self

    @model_serializer(mode="wrap")
    def _without_empty_turns(self, handler: SerializerFunctionWrapHandler) -> Any:
        written = handler(self)
        if isinstance(written, dict) and not self.turns:
            written.pop("turns", None)
        if isinstance(written, dict) and self.simulated_user is None:
            written.pop("simulated_user", None)
        return written


class DatasetVersion(_Object):
    """An immutable set of cases. ``digest`` identifies the content whatever it is called."""

    id: Identifier
    name: str = Field(min_length=1, max_length=200)
    version: int | None = Field(default=None, ge=1)
    digest: Digest
    case_count: int = Field(ge=0)
    source: DatasetSource
    location: str = ""
    created_at: datetime | None = None
    created_by: str | None = None
    store: StoreRef = StoreRef()


class DatasetDraft(_Object):
    """An editable working copy of a dataset. Publishing it creates a ``DatasetVersion``; every edit
    raises ``revision``, so two people cannot overwrite each other's changes unnoticed."""

    id: Identifier
    project_id: Identifier
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    revision: int = Field(default=1, ge=1)
    state: Literal["editing", "published"] = "editing"
    source_dataset_id: str | None = None
    case_count: int = Field(default=0, ge=0)
    published_digest: Digest | None = None
    created_by: str | None = None
    updated_by: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


HEADER_NAME = r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,100}$"
"""An HTTP header name: one or more of the characters RFC 9110 allows in a token."""
MODEL_NAME_LENGTH = 200
"""The longest model name a trace, a run or a schedule keeps."""


class Target(_Object):
    """The thing under test. An HTTP target's ``url`` and ``headers`` are kept as the suite file
    writes them, with ``${NAME}`` references unexpanded: the values a run reads from the
    environment are never part of the model.

    An HTTP target may say where its response reports the model that answered:
    ``model_path``, a path into the JSON response written as ``response_path`` is (``model`` for
    an OpenAI-compatible reply), or ``model_header``, a response header. Unset, each is left out
    when the target is written, so a target without them keeps the digest it had before the
    fields existed."""

    kind: TargetKind
    file: str | None = None
    function: str | None = None
    url: str | None = None
    body: dict[str, Any] | None = None
    response_path: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    agent_id: str | None = None
    model_path: str | None = Field(default=None, min_length=1, max_length=200)
    model_header: str | None = Field(default=None, pattern=HEADER_NAME)

    @model_validator(mode="after")
    def fields_for_kind(self) -> Target:
        given = {
            name
            for name in (
                "file",
                "function",
                "url",
                "body",
                "response_path",
                "headers",
                "agent_id",
                "model_path",
                "model_header",
            )
            if getattr(self, name)
        }
        allowed = {
            "function": {"file", "function"},
            "http": {"url", "body", "response_path", "headers", "model_path", "model_header"},
            "agent": {"agent_id"},
            "stored": set(),
        }[self.kind]
        required = {"function": {"file", "function"}, "http": {"url", "body"}, "agent": {"agent_id"}, "stored": set()}
        if missing := sorted(required[self.kind] - given):
            raise ValueError(f"a {self.kind} target needs {', '.join(missing)}")
        if extra := sorted(given - allowed):
            raise ValueError(f"{', '.join(extra)} does not apply to a {self.kind} target")
        if self.model_path and self.model_header:
            raise ValueError("a target reports its model at model_path or in model_header, not both")
        return self

    @model_serializer(mode="wrap")
    def _without_unset_model_source(self, handler: SerializerFunctionWrapHandler) -> Any:
        written = handler(self)
        if isinstance(written, dict):
            for name in ("model_path", "model_header"):
                if written.get(name, "") is None:
                    del written[name]
        return written


class ScorerVersion(_Object):
    """One way of scoring a case, pinned to the version that produced the score.

    A judge's ``model`` is an MLflow model URI (``openai:/<model>``); ``endpoint`` is the base
    address of the OpenAI-compatible endpoint that serves it, or None for the provider's own. Keys
    are never part of the model. ``scope`` is what it grades in a conversation case: each turn's
    reply, or the conversation once. An unset ``endpoint`` and the default ``scope`` (``turn``) are
    left out when the scorer is written, so a scorer without them has the digest it had before
    the fields existed.
    """

    name: Identifier
    kind: ScorerKind
    type: str = Field(min_length=1, max_length=80)
    version: str = ""
    model: str | None = None
    endpoint: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    criteria: dict[str, Any] = Field(default_factory=dict)
    scope: CheckScope = "turn"

    @model_serializer(mode="wrap")
    def _without_unset_endpoint(self, handler: SerializerFunctionWrapHandler) -> Any:
        written = handler(self)
        if isinstance(written, dict) and written.get("endpoint", "") is None:
            del written["endpoint"]
        if isinstance(written, dict) and self.scope == "turn":
            written.pop("scope", None)
        return written


class Policy(_Object):
    """When a scorer's results pass the suite."""

    scorer: Identifier
    direction: Literal["higher", "lower"] = "higher"
    threshold: float | None = Field(default=None, allow_inf_nan=False)
    minimum_coverage: float = Field(default=1, ge=0, le=1)


class RunLimits(_Object):
    """When a run stops before its last case: once it has run for ``max_seconds`` of wall time, or
    once its judges have used ``max_judge_tokens`` tokens, input and output together, as the judge's
    provider reported them. ``None`` is no limit. Each start or resume of a run has its limits
    afresh. A run that reaches one is ``stopped``, keeps its finished cases and can be resumed."""

    max_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    max_judge_tokens: int | None = Field(default=None, ge=1)

    def stop(self, limit: LimitName, used: float | None) -> dict[str, Any]:
        """What a run that stopped at ``limit`` records of it (``Run.provenance["stopped"]``): the
        limit, its value, what had been used (None for judge tokens a judge did not report), and
        a message that names the setting to raise."""
        value = getattr(self, limit)
        if limit == "max_seconds":
            message = f"Stopped at its time limit (max_seconds {value:g}) after {used or 0:.0f} seconds"
        elif used is None:
            message = "Stopped: the judge reported no token usage, so its token limit (max_judge_tokens) cannot be kept"
        else:
            message = f"Stopped at its judge token limit (max_judge_tokens {value}) after {used:.0f} tokens"
        return {"limit": limit, "value": value, "used": used, "message": message}


_UNSET_LEFT_OUT = ("limits", "simulator_max_retries", "simulator_rate_limit_wait_seconds", "simulator_headers")


class RunSettings(_Object):
    """How a run of the suite is carried out, not what it grades. ``None`` leaves the runner's
    default. A case result's fingerprint leaves these out, so changing one keeps earlier results.
    Unset ``limits`` and simulator settings are left out when the settings are written, so a suite
    without them keeps the digest it had before those fields existed."""

    concurrency: int | None = Field(default=None, ge=1)
    batch: int | None = Field(default=None, ge=1)
    target_timeout_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    judge_max_retries: int | None = Field(default=None, ge=0)
    judge_rate_limit_wait_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    # Headers added to every judge request, values as written with ${NAME} unexpanded.
    judge_headers: dict[str, str] | None = None
    output: str | None = None
    mlflow_tracking_uri: str | None = None
    mlflow_experiment: str | None = None
    limits: RunLimits | None = None
    simulator_max_retries: int | None = Field(default=None, ge=0)
    simulator_rate_limit_wait_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    # Headers added to every simulator request, values as written with ${NAME} unexpanded.
    simulator_headers: dict[str, str] | None = None

    @model_serializer(mode="wrap")
    def _without_unset(self, handler: SerializerFunctionWrapHandler) -> Any:
        written = handler(self)
        if isinstance(written, dict):
            for name in _UNSET_LEFT_OUT:
                if written.get(name, "") is None:
                    del written[name]
        return written


class Simulator(_Object):
    """The model that plays a suite's simulated users (``Case.simulated_user``): an MLflow model URI
    (``openai:/<model>``) on the judges' provider path, and its inference ``parameters``. Never a
    key: the provider's key comes from the environment."""

    model: str = Field(min_length=1, max_length=240)
    parameters: dict[str, Any] = Field(default_factory=dict)


class Repeats(_Object):
    """How many times a suite runs each case, and how each check's verdicts over those attempts
    combine into the case's verdict for that check. ``combine`` is the suite's rule and ``checks``
    the rules checks name for themselves, by scorer name. A check that names none takes ``all`` if
    it is a security check and the suite's rule otherwise; the runner decides which checks are
    security checks. Repeats change verdicts, so they are part of the suite's definition and of a
    case result's fingerprint, not a run setting."""

    attempts: int = Field(default=1, ge=1, le=MAX_ATTEMPTS)
    combine: CombineRule = "all"
    checks: dict[str, CombineRule] = Field(default_factory=dict)


class Suite(_Object):
    """A dataset, a target and scorers, run together. A new version for every change.

    ``dataset_location`` is where the cases are read from: a suite file's ``data`` path, as written
    there. ``input_key`` names the input that judges grade against and that a plain-text case
    input fills. ``repeats`` is left out when it is the default (one attempt, rule ``all``, no
    check rules), so a suite without repeats has the digest it had before the field existed.
    ``simulator`` is the model that plays its cases' simulated users, left out when there is none.
    """

    id: Identifier
    name: str = Field(min_length=1, max_length=120)
    version: int = Field(default=1, ge=1)
    dataset_name: str | None = None
    dataset_digest: Digest | None = None
    dataset_location: str | None = None
    input_key: str = Field(default="message", min_length=1)
    target: Target | None = None
    scorers: list[ScorerVersion] = Field(min_length=1)
    policies: list[Policy] = Field(default_factory=list)
    settings: RunSettings = RunSettings()
    repeats: Repeats = Repeats()
    simulator: Simulator | None = None
    digest: Digest
    created_at: datetime | None = None
    created_by: str | None = None

    @model_validator(mode="after")
    def consistent(self) -> Suite:
        names = [scorer.name for scorer in self.scorers]
        if len(set(names)) != len(names):
            raise ValueError("scorer names must be unique within a suite")
        if unknown := sorted({policy.scorer for policy in self.policies} - set(names)):
            raise ValueError(f"policies name scorers the suite does not have: {', '.join(unknown)}")
        if unknown := sorted(set(self.repeats.checks) - set(names)):
            raise ValueError(f"repeats name scorers the suite does not have: {', '.join(unknown)}")
        return self

    @model_serializer(mode="wrap")
    def _without_default_repeats(self, handler: SerializerFunctionWrapHandler) -> Any:
        written = handler(self)
        if isinstance(written, dict) and self.repeats == Repeats():
            written.pop("repeats", None)
        if isinstance(written, dict) and self.simulator is None:
            written.pop("simulator", None)
        return written


def suite_digest(suite: Suite) -> str:
    """The digest of a suite's definition: every field but ``id``, ``version``, ``digest`` and who
    saved the version when. Run settings count, so a store that keeps one version per digest never
    drops a change to them."""
    return digest(suite.model_dump(mode="json", exclude={"id", "version", "digest", "created_at", "created_by"}))


class Counts(_Object):
    cases: int = Field(default=0, ge=0)
    passed: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    errors: int = Field(default=0, ge=0)
    skipped: int = Field(default=0, ge=0)


class Run(_Object):
    """One execution of a suite, or of a dataset, target and scorers, over its cases."""

    id: Identifier
    project_id: Identifier
    code: str = ""
    source: RunSource
    state: RunState
    suite_id: str | None = None
    suite_version: int | None = Field(default=None, ge=1)
    suite_digest: Digest | None = None
    dataset_name: str = ""
    dataset_digest: Digest | None = None
    target: Target | None = None
    scorers: list[ScorerVersion] = Field(default_factory=list)
    counts: Counts = Counts()
    max_cases: int | None = Field(default=None, ge=1)
    truncated: bool = False
    error: str = ""
    provenance: dict[str, Any] = Field(default_factory=dict)
    requested_by: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    cancel_requested_at: datetime | None = None
    """When someone asked to stop the run. Whoever executes it stops before its next case."""
    store: StoreRef = StoreRef()


class CheckVerdict(_Object):
    """One check's verdict on one attempt. ``passed`` is None when there is no verdict."""

    passed: bool | None = None
    rationale: str = ""
    error: str = ""


class TurnResult(_Object):
    """One turn of a conversation case in one run: the turn's inputs and expectations, its verdict
    (every check that grades the turn passed it), each of those checks' verdict, the reply, why
    the target call failed if it did, and the trace the turn was recorded in. A turn no check
    grades passes when the target answered."""

    number: int = Field(ge=1, le=MAX_TURNS)
    status: CaseStatus
    inputs: dict[str, Any] = Field(default_factory=dict)
    expectations: dict[str, Any] = Field(default_factory=dict)
    checks: dict[str, CheckVerdict] = Field(default_factory=dict)
    output: Any = None
    error: str = ""
    latency_ms: float | None = Field(default=None, ge=0)
    trace_id: str | None = None


def _without_unused_conversation(written: Any, owner: Any) -> Any:
    """A result written without the conversation fields it does not use, so a case of one input
    is written as it was before they existed."""
    if isinstance(written, dict):
        if not owner.turns:
            written.pop("turns", None)
        if owner.session_id is None:
            written.pop("session_id", None)
    return written


class Attempt(_Object):
    """One attempt of a case that its suite runs more than once (``Suite.repeats``): its verdict,
    each check's verdict, the reply, why the target call failed if it did, and the trace it was
    recorded in, if any. An attempt of a conversation case has its own conversation: its
    ``session_id`` and its ``turns``, left out for a case of one input."""

    number: int = Field(ge=1, le=MAX_ATTEMPTS)
    status: CaseStatus
    checks: dict[str, CheckVerdict] = Field(default_factory=dict)
    output: Any = None
    error: str = ""
    latency_ms: float | None = Field(default=None, ge=0)
    trace_id: str | None = None
    session_id: str | None = None
    turns: list[TurnResult] = Field(default_factory=list)

    @model_serializer(mode="wrap")
    def _without_unused_fields(self, handler: SerializerFunctionWrapHandler) -> Any:
        return _without_unused_conversation(handler(self), self)


class CaseResult(_Object):
    """What one case produced in one run. ``fingerprint`` identifies everything that produced it,
    so a resumed run reuses the result only while that is unchanged.

    A case its suite runs more than once has every attempt in ``attempts``, in order; its
    ``status`` and scores are the attempts combined by each check's rule, and its ``output``,
    ``error`` and trace are those of the first attempt whose verdict is the case's, else of its
    first attempt. ``attempts`` is left out when empty, so a result without repeats is written as
    it was before the field existed.

    A conversation case has its conversation's ``session_id``, the id its turns' traces share, and
    every turn it sent in ``turns``; its ``output`` is the conversation as ``role`` and ``content``
    messages, and its scores are the conversation's verdicts, a turn check's passing when it
    passed every turn it grades. With repeats, these are the attempt's that stands for the case.
    Both are left out for a case of one input.

    ``simulated`` marks the result of a case whose user turns a simulator wrote
    (``Case.simulated_user``), left out for every other case."""

    run_id: Identifier
    case_id: Identifier
    position: int = Field(default=0, ge=0)
    status: CaseStatus
    inputs: dict[str, Any] = Field(default_factory=dict)
    expectations: dict[str, Any] = Field(default_factory=dict)
    output: Any = None
    error: str = ""
    latency_ms: float | None = Field(default=None, ge=0)
    fingerprint: Digest | None = None
    finished_at: datetime | None = None
    store: StoreRef = StoreRef()
    attempts: list[Attempt] = Field(default_factory=list)
    session_id: str | None = None
    turns: list[TurnResult] = Field(default_factory=list)
    simulated: bool = False

    @model_serializer(mode="wrap")
    def _without_empty_attempts(self, handler: SerializerFunctionWrapHandler) -> Any:
        written = handler(self)
        if isinstance(written, dict) and not self.attempts:
            written.pop("attempts", None)
        if isinstance(written, dict) and not self.simulated:
            written.pop("simulated", None)
        return _without_unused_conversation(written, self)


class Score(_Object):
    """One scorer's verdict on a case result, or on a trace of live traffic (no run or case).
    ``passed`` is None when there is no verdict."""

    run_id: str | None = None
    case_id: str | None = None
    trace_id: str | None = None
    scorer: Identifier
    scorer_version: str = ""
    source: ScoreSource
    source_id: str = ""
    value: Value = None
    passed: bool | None = None
    rationale: str = ""
    error: str = ""
    scoring_job_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None
    store: StoreRef = StoreRef()

    @model_validator(mode="after")
    def finite(self) -> Score:
        if isinstance(self.value, float) and not math.isfinite(self.value):
            raise ValueError("a score value must be finite")
        return self


class ScoringJob(_Object):
    """Scoring a finished run again with more scorers. Its scores add to the run's, never replace them.
    A job that applies a saved suite names its version and carries the suite's policies."""

    id: Identifier
    run_id: Identifier
    scorers: list[ScorerVersion] = Field(min_length=1)
    policies: list[Policy] = Field(default_factory=list)
    suite_id: str | None = None
    suite_version: int | None = Field(default=None, ge=1)
    state: RunState
    total: int = Field(default=0, ge=0)
    succeeded: int = Field(default=0, ge=0)
    errors: int = Field(default=0, ge=0)
    error: str = ""
    requested_by: str | None = None
    created_at: datetime | None = None
    finished_at: datetime | None = None
    cancel_requested_at: datetime | None = None
    """When someone asked to stop the job. Whoever executes it stops before its next case."""


class ReviewQuestion(_Object):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,59}$")
    kind: QuestionKind = "boolean"
    instruction: str = Field(min_length=1, max_length=2000)
    options: list[str] = Field(default_factory=list, max_length=20)
    minimum: float = Field(default=0, allow_inf_nan=False)
    maximum: float = Field(default=1, allow_inf_nan=False)


class ReviewQueue(_Object):
    """Case results of one run handed to people to answer questions about."""

    id: Identifier
    run_id: Identifier
    name: str = Field(min_length=1, max_length=120)
    questions: list[ReviewQuestion] = Field(min_length=1)
    case_ids: list[Identifier] = Field(min_length=1)
    assignee: str | None = None
    created_by: str | None = None
    created_at: datetime | None = None
    store: StoreRef = StoreRef()


class Review(_Object):
    """A person's answer about one case result. A new answer is a new review that names the one it
    replaces in ``previous_id``; reviews are never edited."""

    id: Identifier
    run_id: Identifier
    case_id: Identifier
    criterion: str = Field(default="human_review", min_length=1, max_length=80)
    state: Literal["pending", "complete"]
    value: Value = None
    comment: str = ""
    reviewer: str | None = None
    assignee: str | None = None
    queue_id: str | None = None
    previous_id: str | None = None
    score_id: str | None = None
    created_at: datetime | None = None

    @model_validator(mode="after")
    def answered_when_complete(self) -> Review:
        if (self.state == "complete") != (self.value is not None):
            raise ValueError("a complete review has a value and a pending one has none")
        return self


TraceState = Literal["ok", "error", "in_progress", "unknown"]


class Span(_Object):
    """One step inside a trace: a model call, a tool call, a retrieval."""

    id: Identifier
    parent_id: str | None = None
    name: str = ""
    kind: str = ""
    status: TraceState = "unknown"
    start_ms: float | None = None
    end_ms: float | None = None
    inputs: Any = None
    outputs: Any = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class Trace(_Object):
    """A recorded call to the thing under test, from an eval run or from live traffic. A case result
    is a trace of a run; a trace need not belong to one. ``spans`` is empty in search results and
    filled when one trace is read. A search may give a preview of a long ``request`` or
    ``response``, and the trace's ``martian.request_preview_truncated`` or
    ``martian.response_preview_truncated`` metadata is then ``"true"``; one trace read has both whole."""

    id: Identifier
    project_id: Identifier
    name: str = ""
    state: TraceState = "unknown"
    request_time: datetime | None = None
    latency_ms: float | None = Field(default=None, ge=0)
    request: str = ""
    response: str = ""
    run_id: str | None = None
    session_id: str | None = None
    user_id: str | None = None
    model: str | None = None
    tokens: int | None = Field(default=None, ge=0)
    cost: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    tags: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, str] = Field(default_factory=dict)
    scores: list[Score] = Field(default_factory=list)
    spans: list[Span] = Field(default_factory=list)


class TraceQuery(_Object):
    """Which traces of a project to return, newest first unless ``oldest_first``. ``model`` keeps
    the traces whose model is exactly that name."""

    project_id: Identifier
    run_id: str | None = None
    session_id: str | None = None
    user_id: str | None = None
    model: str | None = Field(default=None, min_length=1, max_length=MODEL_NAME_LENGTH)
    trace_ids: list[Identifier] = Field(default_factory=list, max_length=500)
    text: str | None = Field(default=None, max_length=500)
    states: list[TraceState] = Field(default_factory=list)
    tags: dict[str, str] = Field(default_factory=dict)
    since: datetime | None = None
    until: datetime | None = None
    oldest_first: bool = False
    limit: int = Field(default=50, ge=1, le=500)
    cursor: str | None = None


class Page[T](BaseModel):
    """One page of results. ``next_cursor`` is None on the last page."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    items: list[T]
    next_cursor: str | None = None


SESSION_TRACE_LIMIT = 1000
"""The most traces one session search groups."""


class Session(_Object):
    """One conversation: the traces that share a session id, each one a turn. Built from traces,
    never stored. ``preview`` is the request of the session's last trace in the order the
    search read them, so its first turn when newest first."""

    id: str = Field(min_length=1)
    turns: int = Field(ge=1)
    errors: int = Field(default=0, ge=0)
    started_at: datetime | None = None
    last_at: datetime | None = None
    preview: str = ""
    tokens: int | None = Field(default=None, ge=0)


class SessionSearch(_Object):
    """The sessions among the first ``limit`` traces a query matches, in the order of each
    session's first trace in the query's order. ``has_more`` says more traces matched, so counts
    may be incomplete; ``ungrouped`` counts the traces read that have no session id."""

    items: list[Session]
    has_more: bool = False
    traces_examined: int = Field(ge=0)
    ungrouped: int = Field(ge=0)
    limit: int = Field(default=SESSION_TRACE_LIMIT, ge=1)


def sessions_from_traces(traces: list[Trace], *, has_more: bool, limit: int = SESSION_TRACE_LIMIT) -> SessionSearch:
    """Group traces, in the order a search returned them, into sessions by ``session_id``."""
    groups: dict[str, list[Trace]] = {}
    for trace in traces:
        if trace.session_id:
            groups.setdefault(trace.session_id, []).append(trace)
    items = []
    for session_id, turns in groups.items():
        times = [t.request_time for t in turns if t.request_time is not None]
        tokens = [t.tokens for t in turns if t.tokens is not None]
        items.append(
            Session(
                id=session_id,
                turns=len(turns),
                errors=sum(t.state == "error" for t in turns),
                started_at=min(times, default=None),
                last_at=max(times, default=None),
                preview=turns[-1].request,
                tokens=sum(tokens) if tokens else None,
            )
        )
    return SessionSearch(
        items=items,
        has_more=has_more,
        traces_examined=len(traces),
        ungrouped=sum(not t.session_id for t in traces),
        limit=limit,
    )


class QueueItem(_Object):
    """One trace in a review queue, and whether its review is done."""

    queue_id: Identifier
    trace_id: Identifier
    case_id: str | None = None
    status: Literal["pending", "completed"] = "pending"


def case_status(passed: bool | None) -> CaseStatus:
    """A case's status from the SDK's three-valued verdict."""
    return "error" if passed is None else "passed" if passed else "failed"


# Every field of a ``martian evals run`` results.jsonl record, and where the model keeps it.
RESULTS_RECORD_FIELDS = {
    "id": "CaseResult.case_id",
    "passed": "CaseResult.status",
    "checks": "Score (one per check label)",
    "inputs": "CaseResult.inputs",
    "output": "CaseResult.output",
    "error": "CaseResult.error",
    "fingerprint": "CaseResult.fingerprint",
    "attempts": "CaseResult.attempts (only when the suite repeats its cases)",
    "session_id": "CaseResult.session_id (only for a conversation case)",
    "turns": "CaseResult.turns (only for a conversation case)",
    "simulated": "CaseResult.simulated (only for a case with a simulated user)",
}

# Every field of one entry of a results.jsonl record's ``attempts``, and where the model keeps it.
ATTEMPT_RECORD_FIELDS = {
    "number": "Attempt.number",
    "passed": "Attempt.status",
    "checks": "Attempt.checks (one CheckVerdict per check label)",
    "output": "Attempt.output",
    "error": "Attempt.error",
    "session_id": "Attempt.session_id (only for a conversation case)",
    "turns": "Attempt.turns (only for a conversation case)",
}

# Every field of one entry of a results.jsonl record's ``turns``, and where the model keeps it.
TURN_RECORD_FIELDS = {
    "number": "TurnResult.number",
    "inputs": "TurnResult.inputs",
    "passed": "TurnResult.status",
    "checks": "TurnResult.checks (one CheckVerdict per check label that grades the turn)",
    "output": "TurnResult.output",
    "error": "TurnResult.error",
}


def _verdict(check: dict[str, Any]) -> CheckVerdict:
    return CheckVerdict(
        passed=check.get("passed"), rationale=check.get("rationale") or "", error=check.get("error") or ""
    )


def _turn(entry: dict[str, Any]) -> TurnResult:
    if unknown := sorted(set(entry) - set(TURN_RECORD_FIELDS)):
        raise ValueError(f"results record has turn fields the model does not map: {', '.join(unknown)}")
    return TurnResult(
        number=entry["number"],
        status=case_status(entry["passed"]),
        inputs=entry.get("inputs") or {},
        checks={label: _verdict(check) for label, check in (entry.get("checks") or {}).items()},
        output=entry.get("output"),
        error=entry.get("error") or "",
    )


def _attempt(entry: dict[str, Any]) -> Attempt:
    if unknown := sorted(set(entry) - set(ATTEMPT_RECORD_FIELDS)):
        raise ValueError(f"results record has attempt fields the model does not map: {', '.join(unknown)}")
    return Attempt(
        number=entry["number"],
        status=case_status(entry["passed"]),
        checks={label: _verdict(check) for label, check in (entry.get("checks") or {}).items()},
        output=entry.get("output"),
        error=entry.get("error") or "",
        session_id=entry.get("session_id"),
        turns=[_turn(turn) for turn in entry.get("turns") or []],
    )


def from_results_record(
    record: dict[str, Any], *, run_id: str, position: int, scorers: dict[str, ScorerVersion] | None = None
) -> tuple[CaseResult, list[Score]]:
    """One ``results.jsonl`` record as a case result and its scores.

    ``scorers`` maps a check label to the scorer that produced it, so each score names its
    version and source; a label not in it is recorded as a check. A record's ``attempts`` become
    the result's attempts, and a conversation's ``session_id`` and ``turns`` its own; its scores
    are the record's own checks: the attempts combined, and a conversation's verdicts.
    """
    if unknown := sorted(set(record) - set(RESULTS_RECORD_FIELDS)):
        raise ValueError(f"results record has fields the model does not map: {', '.join(unknown)}")
    result = CaseResult(
        run_id=run_id,
        case_id=str(record["id"]),
        position=position,
        status=case_status(record["passed"]),
        inputs=record.get("inputs") or {},
        output=record.get("output"),
        error=record.get("error") or "",
        fingerprint=record.get("fingerprint"),
        attempts=[_attempt(entry) for entry in record.get("attempts") or []],
        session_id=record.get("session_id"),
        turns=[_turn(entry) for entry in record.get("turns") or []],
        simulated=record.get("simulated") is True,
    )
    scores = []
    for label, check in (record.get("checks") or {}).items():
        scorer = (scorers or {}).get(label)
        scores.append(
            Score(
                run_id=run_id,
                case_id=result.case_id,
                scorer=label,
                scorer_version=scorer.version if scorer else "",
                source="llm_judge" if scorer and scorer.kind == "llm_judge" else "code",
                value=check.get("passed"),
                passed=check.get("passed"),
                rationale=check.get("rationale") or "",
                error=check.get("error") or "",
            )
        )
    return result, scores


__all__ = [
    "ATTEMPT_RECORD_FIELDS",
    "HEADER_NAME",
    "MAX_ATTEMPTS",
    "MAX_TURNS",
    "MODEL_NAME_LENGTH",
    "MODEL_VERSION",
    "SESSION_TRACE_LIMIT",
    "TURN_RECORD_FIELDS",
    "Attempt",
    "CheckScope",
    "CheckVerdict",
    "CombineRule",
    "Repeats",
    "DatasetDraft",
    "Page",
    "QueueItem",
    "Session",
    "SessionSearch",
    "SimulatedUser",
    "Simulator",
    "Span",
    "Trace",
    "TraceQuery",
    "TraceState",
    "RESULTS_RECORD_FIELDS",
    "Case",
    "CaseResult",
    "CaseStatus",
    "Counts",
    "DatasetVersion",
    "Policy",
    "Project",
    "Review",
    "ReviewQuestion",
    "ReviewQueue",
    "LimitName",
    "Run",
    "RunLimits",
    "RunSettings",
    "RunState",
    "Score",
    "ScorerVersion",
    "ScoringJob",
    "StoreRef",
    "Suite",
    "Target",
    "Turn",
    "TurnResult",
    "case_status",
    "digest",
    "from_results_record",
    "sessions_from_traces",
    "suite_digest",
]
