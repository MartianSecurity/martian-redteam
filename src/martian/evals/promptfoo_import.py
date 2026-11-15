"""promptfoo results files as runs in the local edition's data folder, so that ``martian ui``
opens, compares and reviews them.

``martian evals import promptfoo <results.json>`` reads the JSON file ``promptfoo eval -o
results.json`` writes: promptfoo's ``OutputFile`` (``evalId``, ``results``, ``config``,
``shareableUrl``, ``metadata``) around an ``EvaluateSummary`` of version 3, as promptfoo's
configuration reference documents it. promptfoo itself is not needed. Each provider in the file
is one run, written through the store and the record store as the single local user in the
default project, the way ``martian evals import inspect`` writes its runs:

- The run is a ``Run`` of source ``local`` with a ``stored`` target: its outputs come from the
  file. Its code is ``promptfoo-``, the provider's label (else its id) and the run's number among
  that provider's imported runs. ``provenance["promptfoo"]`` keeps the file's path and SHA-256,
  the eval id, the provider, the timestamp and promptfoo's own stats.
- Each result row (a test, once per prompt and repeat) is a ``CaseResult`` with the rendered
  prompt and the vars as its inputs, the test's assertions as its expectations, the provider's
  output and latency, and promptfoo's verdict: ``failureReason`` 2 is ``error``, a row that
  passed is ``passed``, any other ``failed``.
- Every assertion promptfoo graded is a ``Score``: its pass as the value and the verdict, its
  numeric score in the metadata and its reason as the rationale. Model-graded assertions have
  source ``llm_judge``.

``docs/eval-model.md`` maps every field. Importing the same file again makes more runs.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from martian._version import __version__
from martian.evals.inspect_import import ImportedCase, ImportedRun, record_imported_run
from martian.evals.model import (
    CaseResult,
    CaseStatus,
    Counts,
    Run,
    Score,
    ScorerVersion,
    Span,
    Target,
    Trace,
    digest,
)
from martian.evals.server.principal import SINGLE_USER
from martian.evals.server.projection import DEFAULT_PROJECT

if TYPE_CHECKING:
    from martian.evals.records import RecordStore

RESULTS_VERSION = 3
"""The version of promptfoo's ``EvaluateSummary`` this importer reads: the one without the table."""

FAILED_WITH_ERROR = 2
"""promptfoo's ``ResultFailureReason.ERROR``: the provider or a grader failed, not an assertion."""

MODEL_GRADED = frozenset(
    {
        "agent-rubric",
        "answer-relevance",
        "context-faithfulness",
        "context-recall",
        "context-relevance",
        "conversation-relevance",
        "factuality",
        "g-eval",
        "llm-rubric",
        "model-graded-closedqa",
        "model-graded-factuality",
        "search-rubric",
        "select-best",
        "trajectory:goal-success",
    }
)
"""promptfoo's assertion types that a model grades. Their scores have source ``llm_judge``, as do
promptfoo's red team graders (``promptfoo:redteam:*``)."""


class PromptfooResultsError(Exception):
    """A file this importer cannot read as promptfoo results. The message says why."""


# --- promptfoo's output file: the parts the import reads -------------------------------------------


class _Part(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)


class PromptfooGrading(_Part):
    """promptfoo's ``GradingResult``: one assertion's grade, or a row's grade with its assertions
    in ``componentResults``. An omitted score is 1 when the grade passed and 0 otherwise."""

    passed: bool = Field(alias="pass")
    score: float | None = None
    reason: str | None = None
    named_scores: dict[str, Any] | None = Field(default=None, alias="namedScores")
    component_results: list[PromptfooGrading] | None = Field(default=None, alias="componentResults")
    assertion: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None


class PromptfooProvider(_Part):
    id: str = Field(min_length=1)
    label: str | None = None


class PromptfooPrompt(_Part):
    raw: str = ""
    label: str = ""


class PromptfooResult(_Part):
    """promptfoo's ``EvaluateResult``: one test on one prompt and one provider."""

    id: str | None = None
    prompt_idx: int = Field(alias="promptIdx", ge=0)
    test_idx: int = Field(alias="testIdx", ge=0)
    test_case: dict[str, Any] = Field(default_factory=dict, alias="testCase")
    prompt_id: str = Field(default="", alias="promptId")
    provider: PromptfooProvider
    prompt: PromptfooPrompt
    vars: dict[str, Any] = Field(default_factory=dict)
    response: dict[str, Any] | None = None
    error: str | None = None
    failure_reason: Literal[0, 1, 2] = Field(default=0, alias="failureReason")
    success: bool
    score: float | None = None
    latency_ms: float | None = Field(default=None, alias="latencyMs")
    grading_result: PromptfooGrading | None = Field(default=None, alias="gradingResult")
    named_scores: dict[str, Any] = Field(default_factory=dict, alias="namedScores")
    cost: float | None = None
    metadata: dict[str, Any] | None = None
    token_usage: dict[str, Any] | None = Field(default=None, alias="tokenUsage")
    repeat_index: int | None = Field(default=None, alias="repeatIndex", ge=0)


class PromptfooSummary(_Part):
    version: int
    timestamp: str | None = None
    results: list[PromptfooResult]
    prompts: list[dict[str, Any]] = Field(default_factory=list)
    stats: dict[str, Any] = Field(default_factory=dict)


class PromptfooOutputFile(_Part):
    eval_id: str | None = Field(default=None, alias="evalId")
    results: PromptfooSummary
    config: dict[str, Any] = Field(default_factory=dict)
    shareable_url: str | None = Field(default=None, alias="shareableUrl")
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class PromptfooResults:
    """One promptfoo results file as it was read: where from, its SHA-256 and its content."""

    path: Path
    sha256: str
    file: PromptfooOutputFile


def read_promptfoo_results(path: Path) -> PromptfooResults:
    """The promptfoo results file at ``path``. Raises :class:`PromptfooResultsError` for a file
    that is not the JSON ``promptfoo eval -o`` writes with results of version 3."""
    path = path.expanduser().resolve()
    if path.suffix != ".json":
        raise PromptfooResultsError(f"{path.name}: a promptfoo results file ends in .json (promptfoo eval -o *.json)")
    try:
        data = path.read_bytes()
    except OSError as error:
        raise PromptfooResultsError(f"cannot read {path}: {error.strerror or error}") from None
    try:
        found = json.loads(data)
    except (UnicodeDecodeError, ValueError):
        raise PromptfooResultsError(f"{path.name} is not promptfoo results: it is not JSON") from None
    summary = found.get("results") if isinstance(found, dict) else None
    if not isinstance(summary, dict):
        raise PromptfooResultsError(
            f"{path.name} is not promptfoo results: it has no results object, which promptfoo eval -o results.json "
            "writes"
        )
    if summary.get("version") != RESULTS_VERSION:
        raise PromptfooResultsError(
            f"{path.name}: promptfoo results version {summary.get('version')!r}; this importer reads version "
            f"{RESULTS_VERSION}, which current promptfoo writes"
        )
    try:
        file = PromptfooOutputFile.model_validate(found)
    except ValidationError as error:
        problem = error.errors()[0]
        where = ".".join(str(part) for part in problem["loc"])
        raise PromptfooResultsError(f"{path.name} is not promptfoo results: {where}: {problem['msg']}") from None
    rows = file.results.results
    if not rows:
        raise PromptfooResultsError(f"{path.name} has no results")
    providers = {row.provider for row in rows}
    if len({provider_name(provider) for provider in providers}) != len(providers):
        raise PromptfooResultsError(f"{path.name}: two providers have the same label; give each its own label")
    prompts = len({row.prompt_idx for row in rows})
    seen: set[tuple[str, str]] = set()
    for row in rows:
        name = provider_name(row.provider)
        identifier = case_id(row.test_idx, row.prompt_idx, prompts=prompts, repeat=row.repeat_index)
        if (name, identifier) in seen:
            raise PromptfooResultsError(f"{path.name}: provider {name} has {identifier} twice")
        seen.add((name, identifier))
    return PromptfooResults(path=path, sha256=hashlib.sha256(data).hexdigest(), file=file)


# --- the file as runs ------------------------------------------------------------------------------


def case_id(test: int, prompt: int, *, prompts: int, repeat: int | None = None) -> str:
    """The case id of test ``test`` (promptfoo's zero-based ``testIdx``) on prompt ``prompt``:
    ``test 1``, with ``, prompt 2`` when the eval has more than one prompt and `` (repeat 2)`` for
    a later repeat. ``martian evals convert promptfoo`` gives its cases the same ids."""
    found = f"test {test + 1}" if prompts == 1 else f"test {test + 1}, prompt {prompt + 1}"
    return found if not repeat else f"{found} (repeat {repeat + 1})"


def provider_name(provider: PromptfooProvider) -> str:
    """A provider as one name: its label, else its id."""
    return provider.label or provider.id


def promptfoo_runs(found: PromptfooResults, *, codes: dict[str, str]) -> list[ImportedRun]:
    """The file ``found`` as one run per provider, in the order the file first names them, each
    coded ``codes[provider name]``."""
    summary = found.file.results
    prompts = len({row.prompt_idx for row in summary.results})
    providers: dict[tuple[str, str], list[PromptfooResult]] = {}
    for row in summary.results:
        providers.setdefault((row.provider.id, row.provider.label or ""), []).append(row)
    return [
        _run(found, rows, run_id=str(uuid.uuid4()), code=codes[provider_name(rows[0].provider)], prompts=prompts)
        for rows in providers.values()
    ]


def _run(found: PromptfooResults, rows: list[PromptfooResult], *, run_id: str, code: str, prompts: int) -> ImportedRun:
    summary = found.file.results
    ordered = sorted(rows, key=lambda row: (row.test_idx, row.prompt_idx, row.repeat_index or 0))
    identifiers = [case_id(row.test_idx, row.prompt_idx, prompts=prompts, repeat=row.repeat_index) for row in ordered]
    cases = [
        _case(row, run_id=run_id, code=code, identifier=identifier, position=position)
        for position, (row, identifier) in enumerate(zip(ordered, identifiers, strict=True))
    ]
    statuses = [case.result.status for case in cases]
    counts = Counts(
        cases=len(cases),
        passed=statuses.count("passed"),
        failed=statuses.count("failed"),
        errors=statuses.count("error"),
    )
    started = _when(summary.timestamp)
    duration = summary.stats.get("durationMs")
    finished = started + timedelta(milliseconds=duration) if started and isinstance(duration, int | float) else None
    description = found.file.config.get("description")
    run = Run(
        id=run_id,
        project_id=DEFAULT_PROJECT,
        code=code,
        source="local",
        state="completed",
        dataset_name=description if isinstance(description, str) and description else found.path.stem,
        dataset_digest=digest(
            sorted(
                ({"id": case.result.case_id, **case.result.inputs, **case.result.expectations} for case in cases),
                key=lambda case: str(case["id"]),
            )
        ),
        target=Target(kind="stored"),
        scorers=_scorers(cases),
        counts=counts,
        provenance={"promptfoo": _provenance(found, rows[0].provider), "sdk_version": __version__},
        requested_by=SINGLE_USER.user_id,
        created_at=started,
        started_at=started,
        finished_at=finished,
    )
    return ImportedRun(run=run, cases=cases)


def _case(row: PromptfooResult, *, run_id: str, code: str, identifier: str, position: int) -> ImportedCase:
    response = row.response or {}
    errored = row.failure_reason == FAILED_WITH_ERROR
    error = (row.error or str(response.get("error") or "") or "the provider or a grader failed") if errored else ""
    output = _plain(response.get("output"))
    scores = promptfoo_scores(row.grading_result, run_id=run_id, case_id=identifier)
    assertions = row.test_case.get("assert")
    result = CaseResult(
        run_id=run_id,
        case_id=identifier,
        position=position,
        status=_status(row),
        inputs={"prompt": row.prompt.raw, "vars": _plain(row.vars)},
        expectations={"assert": [_assertion(item) for item in assertions]} if isinstance(assertions, list) else {},
        output=output,
        error=error,
        latency_ms=row.latency_ms if row.latency_ms is not None and row.latency_ms >= 0 else None,
    )
    usage = row.token_usage or response.get("tokenUsage") or {}
    tokens = usage.get("total") if isinstance(usage, dict) else None
    trace_id = "tr-" + uuid.uuid4().hex
    description = row.test_case.get("description")
    trace = Trace(
        id=trace_id,
        project_id=DEFAULT_PROJECT,
        name=f"{code} {identifier}",
        state="error" if errored else "ok",
        latency_ms=result.latency_ms,
        request=row.prompt.raw,
        response=output if isinstance(output, str) else json.dumps(output, ensure_ascii=False),
        run_id=run_id,
        model=row.provider.id,
        tokens=tokens if isinstance(tokens, int) and tokens >= 0 else None,
        cost=row.cost if row.cost is not None and math.isfinite(row.cost) and row.cost >= 0 else None,
        tags={
            "martian.case_id": identifier,
            "promptfoo.test_idx": str(row.test_idx),
            "promptfoo.prompt_idx": str(row.prompt_idx),
            "promptfoo.provider": provider_name(row.provider),
        },
        metadata={
            key: value
            for key, value in (
                ("promptfoo.result_id", row.id or ""),
                ("promptfoo.description", description if isinstance(description, str) else ""),
                ("promptfoo.score", "" if row.score is None else str(row.score)),
                ("promptfoo.named_scores", json.dumps(_plain(row.named_scores)) if row.named_scores else ""),
            )
            if value
        },
        scores=[score.model_copy(update={"trace_id": trace_id}) for score in scores],
        spans=[_span(row, output, errored=errored)],
    )
    return ImportedCase(result=result, scores=scores, trace=trace)


def _status(row: PromptfooResult) -> CaseStatus:
    """promptfoo's verdict on the row: ``error`` when the provider or a grader failed, else
    ``passed`` or ``failed`` as promptfoo graded it, its test's threshold included."""
    if row.failure_reason == FAILED_WITH_ERROR:
        return "error"
    return "passed" if row.success else "failed"


def _span(row: PromptfooResult, output: Any, *, errored: bool) -> Span:
    """The provider call, the one step promptfoo records for a row."""
    response = row.response or {}
    attributes: dict[str, Any] = {"gen_ai.request.model": row.provider.id, "promptfoo.vars": _plain(row.vars)}
    for key, value in (
        ("promptfoo.provider_label", row.provider.label),
        ("promptfoo.prompt_label", row.prompt.label or None),
        ("promptfoo.cached", response.get("cached")),
        ("promptfoo.token_usage", row.token_usage or response.get("tokenUsage")),
        ("promptfoo.cost", row.cost),
        ("promptfoo.error", row.error if errored else None),
    ):
        if value is not None:
            attributes[key] = _plain(value)
    return Span(
        id="call",
        name=provider_name(row.provider),
        kind="LLM",
        status="error" if errored else "ok",
        inputs=row.prompt.raw,
        outputs=output,
        attributes=attributes,
    )


def promptfoo_scores(grading: PromptfooGrading | None, *, run_id: str, case_id: str) -> list[Score]:
    """Every assertion promptfoo graded in a row, as the model's scores.

    A score is named after its assertion's type; a type that appears again in the same test is
    ``<type> (2)``, and an assertion inside an ``assert-set`` is ``<set>/<type>``. The value and
    the verdict are its pass, as a suite file's checks record theirs, so the UI shows each
    assertion's pass rate; promptfoo's numeric score is ``metadata["promptfoo.score"]`` (1 or 0
    when promptfoo gives none) and its reason the rationale. A grade promptfoo marks as a grader
    error has no value or verdict, and its reason as the error."""
    if grading is None:
        return []
    parts = grading.component_results or ([grading] if grading.assertion is not None else [])
    return [_score(name, part, run_id, case_id) for name, part in _named(parts, "")]


def _named(parts: list[PromptfooGrading], prefix: str) -> list[tuple[str, PromptfooGrading]]:
    found: list[tuple[str, PromptfooGrading]] = []
    seen: dict[str, int] = {}
    for part in parts:
        kind = _type_of(part)
        seen[kind] = seen.get(kind, 0) + 1
        name = f"{prefix}{kind}" if seen[kind] == 1 else f"{prefix}{kind} ({seen[kind]})"
        found.append((name[:200], part))
        if part.component_results:
            found += _named(part.component_results, f"{name}/")
    return found


def _type_of(part: PromptfooGrading) -> str:
    kind = (part.assertion or {}).get("type")
    return kind if isinstance(kind, str) and kind else "assertion"


def _score(name: str, part: PromptfooGrading, run_id: str, case_id: str) -> Score:
    assertion = _assertion(part.assertion or {})
    kind = _type_of(part)
    judged = kind in MODEL_GRADED or kind.startswith("promptfoo:redteam:")
    grader = assertion.get("provider")
    failed_grader = bool((part.metadata or {}).get("graderError"))
    score = part.score if part.score is not None else (1.0 if part.passed else 0.0)
    metadata: dict[str, Any] = {"promptfoo.score": _plain(score)}
    if assertion:
        metadata["promptfoo.assertion"] = assertion
    if part.named_scores:
        metadata["promptfoo.named_scores"] = _plain(part.named_scores)
    if part.metadata:
        metadata["promptfoo.metadata"] = _plain(part.metadata)
    return Score(
        run_id=run_id,
        case_id=case_id,
        scorer=name,
        source="llm_judge" if judged else "code",
        source_id=grader if judged and isinstance(grader, str) else "",
        value=None if failed_grader else part.passed,
        passed=None if failed_grader else part.passed,
        rationale="" if failed_grader else part.reason or "",
        error=(part.reason or "the grader failed") if failed_grader else "",
        metadata=metadata,
    )


def _scorers(cases: list[ImportedCase]) -> list[ScorerVersion]:
    """One scorer per score name, in the order cases first give them."""
    found: dict[str, ScorerVersion] = {}
    for case in cases:
        for score in case.scores:
            if score.scorer not in found:
                kind = (score.metadata.get("promptfoo.assertion") or {}).get("type") or "assertion"
                found[score.scorer] = ScorerVersion(
                    name=score.scorer,
                    kind=score.source,
                    type=str(kind)[:80],
                    model=score.source_id or None,
                )
    return list(found.values())


def _provenance(found: PromptfooResults, provider: PromptfooProvider) -> dict[str, Any]:
    """Where the run came from: the file, the eval in it, the provider and promptfoo's stats."""
    file, summary = found.file, found.file.results
    name = provider_name(provider)
    provenance: dict[str, Any] = _plain(
        {
            "results": str(found.path),
            "results_sha256": found.sha256,
            "results_version": summary.version,
            "eval_id": file.eval_id,
            "timestamp": summary.timestamp,
            "provider": {"id": provider.id, "label": provider.label},
            "description": file.config.get("description"),
            "prompts": [
                {key: prompt.get(key) for key in ("label", "raw", "metrics") if key in prompt}
                for prompt in summary.prompts
                if prompt.get("provider") in (provider.id, name)
            ],
            "stats": summary.stats,
            "metadata": file.metadata or {},
        }
    )
    return provenance


# --- values --------------------------------------------------------------------------------------


def _assertion(assertion: Any) -> Any:
    """An assertion as the run keeps it: a grading ``provider`` given as an object, which may
    carry its own settings and keys, is kept as its id only, in an ``assert-set``'s too."""
    if not isinstance(assertion, dict):
        return _plain(assertion)
    kept = dict(assertion)
    if isinstance(kept.get("provider"), dict):
        kept["provider"] = kept["provider"].get("id")
    if isinstance(kept.get("assert"), list):
        kept["assert"] = [_assertion(item) for item in kept["assert"]]
    return _plain(kept)


def _plain(value: Any) -> Any:
    """``value`` with numbers that are not finite as their text, which every store keeps."""
    if isinstance(value, float) and not math.isfinite(value):
        return str(value).replace("nan", "NaN").replace("inf", "Infinity")
    if isinstance(value, list):
        return [_plain(entry) for entry in value]
    if isinstance(value, dict):
        return {str(key): _plain(entry) for key, entry in value.items()}
    return value


def _when(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        found = datetime.fromisoformat(value)
    except ValueError:
        return None
    return found if found.tzinfo else found.replace(tzinfo=UTC)


# --- writing -------------------------------------------------------------------------------------


def import_promptfoo_results(paths: list[Path], home: Path) -> list[Run]:
    """Read every file first, so that one that cannot be read stops the import before anything
    is written, then record each provider of each file as a run in the data folder ``home``."""
    from martian.evals.local import LocalEditionError, open_stores

    found = [read_promptfoo_results(path) for path in paths]
    try:
        store, records = open_stores(home)
    except LocalEditionError:
        raise
    except Exception as error:  # noqa: BLE001 - a folder may fail to open in many ways; say which
        raise LocalEditionError(f"could not open the local data folder {home}: {type(error).__name__}") from None
    runs: list[Run] = []
    try:
        for results in found:
            names = dict.fromkeys(provider_name(row.provider) for row in results.file.results.results)
            codes = {name: asyncio.run(_code(records, name)) for name in names}
            runs += [record_imported_run(store, records, imported) for imported in promptfoo_runs(results, codes=codes)]
        return runs
    finally:
        records.close()


async def _code(records: RecordStore, provider: str) -> str:
    """``promptfoo-``, the provider's name and the run's number among that provider's imported runs."""
    count, cursor = 0, None
    while True:
        page = await records.list_runs(SINGLE_USER, DEFAULT_PROJECT, limit=500, cursor=cursor)
        for run in page.items:
            source = (run.provenance.get("promptfoo") or {}).get("provider")
            count += isinstance(source, dict) and (source.get("label") or source.get("id")) == provider
        if (cursor := page.next_cursor) is None:
            slug = re.sub(r"[^A-Za-z0-9._:-]+", "-", provider).strip("-") or "provider"
            return f"promptfoo-{slug}-{count + 1}"


__all__ = [
    "FAILED_WITH_ERROR",
    "MODEL_GRADED",
    "RESULTS_VERSION",
    "PromptfooGrading",
    "PromptfooOutputFile",
    "PromptfooResults",
    "PromptfooResultsError",
    "case_id",
    "import_promptfoo_results",
    "promptfoo_runs",
    "promptfoo_scores",
    "provider_name",
    "read_promptfoo_results",
]
