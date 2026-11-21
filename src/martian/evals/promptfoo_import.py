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
  passed is ``passed``, any other ``failed``. Its id names the test and the prompt the same way
  under every provider, so two providers' runs compare case by case.
- Every assertion promptfoo graded is a ``Score``: its pass as the value and the verdict, its
  numeric score in the metadata and its reason as the rationale. Model-graded assertions have
  source ``llm_judge``.

``docs/eval-model.md`` maps every field. An unchanged archive reuses its completed runs.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import sqlite3
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterator
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import cached_property
from itertools import count
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from martian._version import __version__
from martian.evals import _embedded_json, _xml_output, named_metrics
from martian.evals._promptfoo_artifacts import (
    MEDIA_PREFIX,
    ArchiveArtifacts,
    ArchivedTrace,
    ArtifactError,
    prepare_artifacts,
)
from martian.evals._promptfoo_comparison import case_contract
from martian.evals._promptfoo_inputs import case_id, inline_prompt, prompt_group
from martian.evals.case_gate import CaseGate
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
from martian.evals.store import StoreError

if TYPE_CHECKING:
    from martian.evals.records import RecordStore
    from martian.evals.store import EvalStore

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
    named_score_weights: dict[str, Any] | None = Field(default=None, alias="namedScoreWeights")
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
    trace_id: str | None = Field(default=None, alias="traceId")
    evaluation_id: str | None = Field(default=None, alias="evaluationId")


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
    traces: Any = None
    blob_assets: Any = Field(default=None, alias="blobAssets")


@dataclass(frozen=True)
class ProviderResults:
    provider: PromptfooProvider
    name: str
    rows: list[PromptfooResult]
    position: int | None = None

    def provenance(self) -> dict[str, Any]:
        detail: dict[str, Any] = {"id": self.provider.id, "label": self.provider.label}
        if self.name != provider_name(self.provider):
            detail["display_name"] = self.name
        if self.position is not None:
            detail["position"] = self.position
        return detail


@dataclass(frozen=True)
class PromptfooResults:
    """One promptfoo results file as it was read: where from, its SHA-256 and its content."""

    path: Path
    sha256: str
    file: PromptfooOutputFile
    archive_sha256: str | None = None

    @cached_property
    def providers(self) -> list[ProviderResults]:
        return _provider_results(self.file, self.path.name)

    @cached_property
    def prompt_positions(self) -> tuple[dict[_Column, int], int]:
        return _prompt_positions(self)

    @cached_property
    def artifacts(self) -> ArchiveArtifacts:
        try:
            return prepare_artifacts(self.file)
        except ArtifactError as error:
            raise PromptfooResultsError(f"{self.path.name}: {error}") from None


def _archive_sha256(document: dict[str, Any]) -> str:
    """Digest the exported scan, independent of when and where the CLI wrote it."""
    canonical = {key: value for key, value in document.items() if key != "shareableUrl"}
    metadata = canonical.get("metadata")
    if isinstance(metadata, dict):
        exporter_fields = {"exportedAt", "nodeVersion", "platform", "arch", "promptfooVersion"}
        recorded = {key: value for key, value in metadata.items() if key not in exporter_fields}
        if recorded:
            canonical["metadata"] = recorded
        else:
            del canonical["metadata"]
    encoder = json.JSONEncoder(sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    found = hashlib.sha256()
    for part in encoder.iterencode(canonical):
        found.update(part.encode("ascii"))
    return found.hexdigest()


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
    result = PromptfooResults(
        path=path, sha256=hashlib.sha256(data).hexdigest(), file=file, archive_sha256=_archive_sha256(found)
    )
    positions, prompts = result.prompt_positions
    for group in result.providers:
        seen: set[str] = set()
        for row in group.rows:
            identifier = case_id(row.test_idx, positions[_column(row)], prompts=prompts, repeat=row.repeat_index)
            if identifier in seen:
                raise PromptfooResultsError(f"{path.name}: provider {group.name} has {identifier} twice")
            seen.add(identifier)
    return result


# --- the file as runs ------------------------------------------------------------------------------


_Column = tuple[str, str, int]
"""One of promptfoo's prompt columns: its provider's id and label, and its ``promptIdx``."""


def _column(row: PromptfooResult) -> _Column:
    return (row.provider.id, row.provider.label or "", row.prompt_idx)


def _inline_prompts(file: PromptfooOutputFile) -> list[tuple[str, str]] | None:
    config = file.config.get("prompts")
    if isinstance(config, str):
        config = [config]
    if not isinstance(config, list) or not config:
        return None
    declared = []
    for item in config:
        prompt = inline_prompt(item)
        if prompt is None:
            return None
        declared.append(prompt)
    return declared


def _provider_declarations(file: PromptfooOutputFile) -> list[PromptfooProvider | None]:
    config = file.config.get("providers") or file.config.get("targets") or []
    items = config if isinstance(config, list) else [config]
    declarations = []
    for item in items:
        identifier, label = (item.get("id"), item.get("label")) if isinstance(item, dict) else (item, None)
        declarations.append(
            PromptfooProvider(id=identifier, label=label or None)
            if isinstance(identifier, str) and identifier and (label is None or isinstance(label, str))
            else None
        )
    return declarations


def _provider_slots(file: PromptfooOutputFile, declarations: list[PromptfooProvider | None]) -> dict[int, int] | None:
    """Prove a provider-major layout through the recorded columns and inline declarations."""
    prompts, columns = _inline_prompts(file), file.results.prompts
    if not prompts or len(columns) != len(prompts) * len(declarations):
        return None
    width = len(prompts)
    for index, column in enumerate(columns):
        declared = declarations[index // width]
        if (
            declared is None
            or column.get("provider") != provider_name(declared)
            or (column.get("raw"), column.get("label")) != prompts[index % width]
        ):
            return None
    slots = {}
    for row in file.results.results:
        if row.prompt_idx >= len(columns):
            return None
        slot = row.prompt_idx // width
        declared = declarations[slot]
        column = columns[row.prompt_idx]
        if (
            declared is None
            or declared.id != row.provider.id
            or provider_name(declared) != provider_name(row.provider)
            or column.get("id") != row.prompt_id
            or column.get("label") != row.prompt.label
        ):
            return None
        slots[row.prompt_idx] = slot + 1
    return slots


def _provider_results(file: PromptfooOutputFile, filename: str) -> list[ProviderResults]:
    rows = file.results.results
    declarations = _provider_declarations(file)
    recorded: dict[str, set[str]] = defaultdict(set)
    declared: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        recorded[provider_name(row.provider)].add(row.provider.id)
    for provider in declarations:
        if provider is not None:
            declared[provider_name(provider)][provider.id] += 1
    ambiguous = {
        name
        for name, identifiers in recorded.items()
        if declared[name].total() > len(identifiers)
        and any(declared[name][identifier] != 1 for identifier in identifiers)
    }
    slots = {}
    if ambiguous:
        resolved = _provider_slots(file, declarations)
        if resolved is None:
            raise PromptfooResultsError(
                f"{filename}: the exported prompt columns do not identify the repeated providers"
            )
        slots = resolved

    batches: dict[tuple[str, str, int | None], list[PromptfooResult]] = defaultdict(list)
    for row in rows:
        position = slots[row.prompt_idx] if provider_name(row.provider) in ambiguous else None
        batches[(row.provider.id, row.provider.label or "", position)].append(row)

    counts = Counter(provider_name(batch[0].provider) for batch in batches.values())
    ordinal: Counter[str] = Counter()
    used = set(counts)
    names: dict[tuple[str, str, int | None], str] = {}
    for key, batch in sorted(batches.items(), key=lambda item: (min(row.prompt_idx for row in item[1]), item[0][0])):
        name = provider_name(batch[0].provider)
        position = key[2]
        if counts[name] > 1 or position is not None:
            ordinal[name] += 1
            numbered = f"{name} (provider {position or ordinal[name]})"
            for suffix in count(1):
                candidate = numbered if suffix == 1 else f"{numbered} [{suffix}]"
                if candidate not in used:
                    name = candidate
                    used.add(name)
                    break
        names[key] = name
    return [ProviderResults(batch[0].provider, names[key], batch, key[2]) for key, batch in batches.items()]


def _declared_positions(file: PromptfooOutputFile) -> tuple[dict[_Column, int], int] | None:
    """Use config order when the exported columns identify the declared prompts unambiguously."""
    prompts = _inline_prompts(file)
    if prompts is None or len(set(prompts)) != len(prompts):
        return None
    declared = {prompt: index for index, prompt in enumerate(prompts)}
    positions: dict[_Column, int] = {}
    for result in file.results.results:
        if result.prompt_idx >= len(file.results.prompts):
            return None
        column = file.results.prompts[result.prompt_idx]
        raw, label = column.get("raw"), column.get("label")
        if (
            not isinstance(raw, str)
            or not isinstance(label, str)
            or label != result.prompt.label
            or column.get("id") != result.prompt_id
            or column.get("provider") != provider_name(result.provider)
        ):
            return None
        position = declared.get((raw, label))
        if position is None:
            return None
        positions[_column(result)] = position
    return positions, len(declared)


def _prompt_positions(found: PromptfooResults) -> tuple[dict[_Column, int], int]:
    """Each prompt column of ``rows`` as the position of its prompt among the eval's prompts, so
    that a test on one prompt has one case id under every provider.

    promptfoo gives each provider a column of its own for each prompt it runs, numbered on from the
    previous provider's (``promptIdx``): with two prompts, the second provider's columns are 2 and
    3. A column's prompt is its ``promptId``, promptfoo's hash of the prompt's label, and how many
    of the provider's columns up to it have that id, for two prompts with one label. Prompts are
    numbered in the order of their first column: the config's order when the first provider runs
    every prompt."""
    if declared := _declared_positions(found.file):
        return declared
    prompts: dict[_Column, tuple[str, int]] = {}
    for group in found.providers:
        columns = {_column(row): row.prompt_id for row in group.rows}
        seen: dict[str, int] = {}
        for column in sorted(columns, key=lambda key: key[2]):
            identifier = columns[column]
            seen[identifier] = seen.get(identifier, 0) + 1
            prompts[column] = (identifier, seen[identifier])
    positions: dict[tuple[str, int], int] = {}
    for column in sorted(prompts, key=lambda column: column[2]):
        positions.setdefault(prompts[column], len(positions))
    return {column: positions[prompt] for column, prompt in prompts.items()}, len(positions)


def provider_name(provider: PromptfooProvider) -> str:
    """A provider as one name: its label, else its id."""
    return provider.label or provider.id


def promptfoo_runs(found: PromptfooResults, *, codes: dict[str, str]) -> list[ImportedRun]:
    """The file ``found`` as one run per provider, in the order the file first names them, each
    coded by its display name."""
    positions, prompts = found.prompt_positions
    return [
        _run(
            found,
            group,
            run_id=str(uuid.uuid4()),
            code=codes[group.name],
            positions=positions,
            prompts=prompts,
        )
        for group in found.providers
    ]


def _run(
    found: PromptfooResults,
    group: ProviderResults,
    *,
    run_id: str,
    code: str,
    positions: dict[_Column, int],
    prompts: int,
    import_copy: bool = False,
) -> ImportedRun:
    summary = found.file.results
    artifacts = found.artifacts
    ordered = sorted(group.rows, key=lambda row: (row.test_idx, positions[_column(row)], row.repeat_index or 0))
    verify_checks = not any(found.file.config.get(key) for key in ("extensions", "nunjucksFilters"))
    identifiers = [
        case_id(row.test_idx, positions[_column(row)], prompts=prompts, repeat=row.repeat_index) for row in ordered
    ]
    cases = [
        _case(
            row,
            run_id=run_id,
            code=code,
            identifier=identifier,
            position=position,
            verify_checks=verify_checks,
            archived_trace=artifacts.traces.get(row.trace_id or ""),
        )
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
    source = _provenance(found, group)
    if artifacts.version is not None:
        source["artifact_version"] = artifacts.version
    media_cases: dict[str, list[str]] = {}
    for row, identifier in zip(ordered, identifiers, strict=True):
        if refs := artifacts.media_for(row):
            media_cases[identifier] = sorted(refs)
    named_artifacts = []
    if media_cases:
        media_assets: dict[str, dict[str, str | int | bool]] = {}
        for sha256 in sorted({ref for refs in media_cases.values() for ref in refs}):
            asset = artifacts.media.get(sha256)
            if asset is None:
                media_assets[sha256] = {"available": False}
            else:
                media_assets[sha256] = {
                    "available": True,
                    "mime_type": asset.mime_type,
                    "size_bytes": asset.size_bytes,
                }
                named_artifacts.append((MEDIA_PREFIX + sha256, asset.decode))
        source["media"] = {"cases": media_cases, "assets": media_assets}
    if import_copy:
        source["import_copy"] = True
    provenance: dict[str, Any] = {"promptfoo": source, "sdk_version": __version__}
    if metrics := named_metrics.summarize(case.result.named_metrics for case in cases):
        provenance["named_metrics"] = metrics
    if derived := _derived_columns(found.file, ordered, positions):
        provenance["derived_metrics"] = derived
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
        provenance=provenance,
        requested_by=SINGLE_USER.user_id,
        created_at=started,
        started_at=started,
        finished_at=finished,
    )
    return ImportedRun(run=run, cases=cases, artifacts=tuple(named_artifacts))


def _derived_columns(
    file: PromptfooOutputFile, ordered: list[PromptfooResult], positions: dict[_Column, int]
) -> list[dict[str, Any]]:
    """Read the recorded per-column scores without evaluating an archive's configuration."""
    definitions = file.config.get("derivedMetrics")
    if not isinstance(definitions, list):
        return []
    names = list(
        dict.fromkeys(
            item["name"]
            for item in definitions
            if isinstance(item, dict) and isinstance(item.get("name"), str) and item["name"]
        )
    )
    if not names:
        return []
    counts = Counter(row.prompt_idx for row in ordered)
    columns = {row.prompt_idx: positions[_column(row)] for row in ordered}
    found = []
    for index, position in sorted(columns.items(), key=lambda pair: pair[1]):
        summary = file.results.prompts[index].get("metrics") if index < len(file.results.prompts) else None
        scores = summary.get("namedScores") if isinstance(summary, dict) else None
        recorded = (
            named_metrics.recorded_metrics({name: scores.get(name) for name in names}, {})
            if isinstance(scores, dict)
            else {}
        )
        for name in names:
            found.append(
                {
                    "name": name,
                    "group": prompt_group(position),
                    "cases": counts[index],
                    "score": recorded[name].score if name in recorded else None,
                    "error": None if name in recorded else "Score unavailable in source archive",
                }
            )
    return found


def _verified_contract(row: PromptfooResult, inputs: dict[str, Any]) -> str | None:
    from martian.evals.promptfoo_convert import recorded_checks

    options = row.test_case.get("options", {})
    if (
        row.repeat_index is not None
        or row.test_case.get("assertScoringFunction")
        or not isinstance(options, dict)
        or set(options) - {"disableDefaultAsserts", "prefix", "suffix", "provider", "rubricPrompt"}
    ):
        return None
    plan = recorded_checks(row.test_case.get("assert"), row.test_case.get("threshold"), row.vars)
    if plan is None:
        return None
    scorers, gate, leaves = plan
    if row.failure_reason != FAILED_WITH_ERROR:
        # Structured provider replies have different text representations in Python and JavaScript.
        if not isinstance((row.response or {}).get("output"), str) and any(
            scorer.type in {"is_xml", "levenshtein", "word_count"} or scorer.parameters.get("embedded")
            for scorer in scorers
        ):
            return None
        if not _verified_verdict(row, scorers, gate, leaves):
            return None
    return case_contract(inputs, {}, scorers, gate)


def _verified_verdict(
    row: PromptfooResult, scorers: list[ScorerVersion], gate: CaseGate | None, leaves: list[dict[str, Any]]
) -> bool:
    """Check the recorded component verdicts and their aggregate using the converted gate."""
    grade = row.grading_result
    if grade is None or grade.passed != row.success:
        return False
    components = [
        item for item in (grade.component_results or []) if item.assertion is not None and not item.component_results
    ]
    if len(scorers) != len(leaves) or [item.assertion for item in components] != leaves:
        return False
    for item, assertion, scorer in zip(components, leaves, scorers, strict=True):
        # Promptfoo marks zero-weight assertions as passing while retaining their raw score.
        zero_weight_override = assertion.get("weight") == 0 and item.passed and item.score == 0
        if item.score is not None and item.score != float(item.passed) and not zero_weight_override:
            return False
        matches = None
        try:
            if scorer.parameters.get("embedded") == "promptfoo":
                matches = _embedded_json.matches(
                    (row.response or {}).get("output"), scorer.parameters["schema"], "promptfoo"
                )
            elif scorer.type == "is_xml":
                matches, _ = _xml_output.validate(
                    (row.response or {}).get("output"),
                    _xml_output.paths(scorer.parameters.get("required_elements", [])),
                    fragment=scorer.parameters.get("fragment", False),
                )
        except (TypeError, ValueError, RecursionError):
            return False
        if matches is not None:
            expected = matches != scorer.parameters.get("negate", False)
            if float(expected) != (item.score if item.score is not None else float(item.passed)):
                return False
    if gate is None:
        return all(item.passed for item in components) == row.success
    checks = {
        scorer.name: {"passed": item.passed, "value": item.score if item.score is not None else float(item.passed)}
        for scorer, item in zip(scorers, components, strict=True)
    }
    record: dict[str, Any] = {"checks": checks}
    gate.apply(record, list(checks), [])
    return record["passed"] is row.success


def _case(
    row: PromptfooResult,
    *,
    run_id: str,
    code: str,
    identifier: str,
    position: int,
    verify_checks: bool,
    archived_trace: ArchivedTrace | None,
) -> ImportedCase:
    response = row.response or {}
    errored = row.failure_reason == FAILED_WITH_ERROR
    error = (row.error or str(response.get("error") or "") or "the provider or a grader failed") if errored else ""
    output = _plain(response.get("output"))
    scores = promptfoo_scores(row.grading_result, run_id=run_id, case_id=identifier)
    assertions = row.test_case.get("assert")
    inputs = {"prompt": row.prompt.raw, "vars": _plain(row.vars)}
    grade = row.grading_result
    metric_scores = row.named_scores if grade is None or grade.named_scores is None else grade.named_scores
    metric_weights = {} if grade is None else grade.named_score_weights or {}
    result = CaseResult(
        run_id=run_id,
        case_id=identifier,
        position=position,
        status=_status(row),
        inputs=inputs,
        expectations={"assert": [_assertion(item) for item in assertions]} if isinstance(assertions, list) else {},
        output=output,
        error=error,
        latency_ms=row.latency_ms if row.latency_ms is not None and row.latency_ms >= 0 else None,
        promptfoo_contract=_verified_contract(row, inputs) if verify_checks else None,
        named_metrics=named_metrics.recorded_metrics(metric_scores, metric_weights),
    )
    usage = row.token_usage or response.get("tokenUsage") or {}
    tokens = usage.get("total") if isinstance(usage, dict) else None
    trace_id = "tr-" + uuid.uuid4().hex
    description = row.test_case.get("description")
    spans = [_span(row, output, errored=errored)]
    request_time = None
    archived_metadata: dict[str, str] = {}
    if archived_trace is not None:
        archived_metadata = {
            "promptfoo.source_trace_id": row.trace_id or "",
            "promptfoo.source_evaluation_id": archived_trace.evaluation_id,
            "promptfoo.source_test_case_id": archived_trace.test_case_id,
        }
        if archived_trace.metadata:
            archived_metadata["promptfoo.trace_metadata"] = json.dumps(_plain(archived_trace.metadata))
        if archived_trace.spans:
            spans = [span.model_copy(update={"attributes": _plain(span.attributes)}) for span in archived_trace.spans]
            if spans[0].start_ms is not None:
                request_time = datetime.fromtimestamp(spans[0].start_ms / 1000, tz=UTC)
    trace = Trace(
        id=trace_id,
        project_id=DEFAULT_PROJECT,
        name=f"{code} {identifier}",
        state="error" if errored else "ok",
        request_time=request_time,
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
        metadata=archived_metadata
        | {
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
        spans=spans,
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


def _provenance(found: PromptfooResults, group: ProviderResults) -> dict[str, Any]:
    """Where the run came from: the file, the eval in it, the provider and promptfoo's stats."""
    file, summary = found.file, found.file.results
    columns = {row.prompt_idx for row in group.rows}
    provenance: dict[str, Any] = _plain(
        {
            "results": str(found.path),
            "results_sha256": found.sha256,
            "results_version": summary.version,
            "eval_id": file.eval_id,
            "timestamp": summary.timestamp,
            "provider": group.provenance(),
            "description": file.config.get("description"),
            "prompts": [
                {key: prompt.get(key) for key in ("label", "raw", "metrics") if key in prompt}
                for index, prompt in enumerate(summary.prompts)
                if index in columns
            ],
            "stats": summary.stats,
            "metadata": file.metadata or {},
        }
    )
    if found.archive_sha256 is not None:
        provenance["archive_sha256"] = found.archive_sha256
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


@contextmanager
def _import_session(home: Path) -> Iterator[None]:
    """One writer can look up and record Promptfoo snapshots in this data folder."""
    with ExitStack() as resources:
        try:
            home.mkdir(parents=True, exist_ok=True)
            guard = resources.enter_context(closing(sqlite3.connect(home / ".promptfoo-import.lock", timeout=5)))
            guard.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as error:
            if error.sqlite_errorcode == sqlite3.SQLITE_BUSY:
                raise PromptfooResultsError(
                    "another Promptfoo import is writing to this data folder; try again"
                ) from None
            raise PromptfooResultsError("could not lock the data folder for Promptfoo import") from None
        except OSError:
            raise PromptfooResultsError("could not open the data folder for Promptfoo import") from None
        yield


def import_promptfoo_results(paths: list[Path], home: Path, *, new_id: bool = False) -> list[Run]:
    """Read every file first, then reuse completed snapshots or record each provider as a run.

    ``new_id`` records independent copies for explicit repeat imports.
    """
    found = [read_promptfoo_results(path) for path in paths]
    # Check linked traces and media for every source before opening the destination.
    for results in found:
        _ = results.artifacts
    with _import_session(home):
        return _save_imports(found, home, new_id=new_id)


def _save_imports(found: list[PromptfooResults], home: Path, *, new_id: bool) -> list[Run]:
    from sqlalchemy.exc import SQLAlchemyError

    from martian.evals.local import LocalEditionError, open_stores
    from martian.evals.local_runs import RunHold
    from martian.evals.records.lease import WorkLeaseLost

    try:
        store, records = open_stores(home)
    except LocalEditionError:
        raise
    except Exception as error:  # noqa: BLE001 - a folder may fail to open in many ways; say which
        raise LocalEditionError(f"could not open the local data folder {home}: {type(error).__name__}") from None
    runs: list[Run] = []
    try:
        used, snapshots = asyncio.run(_recorded_imports(records))
        for results in found:
            positions, prompts = results.prompt_positions
            for group in results.providers:
                existing = None
                if not new_id:
                    source = {
                        "archive_sha256": results.archive_sha256,
                        "results_sha256": results.sha256,
                        "provider": group.provenance(),
                        "artifact_version": results.artifacts.version,
                    }
                    for key in _archive_keys(source):
                        candidate = snapshots.get(key)
                        if candidate is not None and candidate.counts.cases == len(group.rows):
                            existing = candidate
                            break
                if existing is not None:
                    saved = asyncio.run(_reuse_snapshot(store, records, existing, results.archive_sha256))
                else:
                    imported = _run(
                        results,
                        group,
                        run_id=str(uuid.uuid4()),
                        code=_reserve_code(group.name, used),
                        positions=positions,
                        prompts=prompts,
                        import_copy=new_id,
                    )
                    hold = asyncio.run(RunHold.take(home, imported.run.id))
                    try:
                        with hold.guard():
                            saved = record_imported_run(store, records, imported)
                    finally:
                        hold.release()
                if not new_id:
                    for key in _archive_keys(saved.provenance["promptfoo"]):
                        snapshots[key] = saved
                runs.append(saved)
        return runs
    except WorkLeaseLost:
        raise PromptfooResultsError("lost the recording hold on an imported run; try the import again") from None
    except StoreError as error:
        raise PromptfooResultsError(error.detail) from None
    except (OSError, SQLAlchemyError) as error:
        raise PromptfooResultsError(f"could not save or read back the import ({type(error).__name__})") from None
    finally:
        records.close()


_ArchiveKey = tuple[str, str, str, int | None, int | None]


def _archive_keys(source: dict[str, Any]) -> tuple[_ArchiveKey, ...]:
    provider = source.get("provider")
    if not isinstance(provider, dict) or not isinstance(identifier := provider.get("id"), str):
        return ()
    label, position = provider.get("label") or "", provider.get("position")
    version = source.get("artifact_version")
    if (
        not isinstance(label, str)
        or (position is not None and type(position) is not int)
        or (version is not None and type(version) is not int)
    ):
        return ()
    return tuple(
        (key, identifier, label, position, version)
        for name in ("archive_sha256", "results_sha256")
        if isinstance(key := source.get(name), str)
    )


async def _recorded_imports(records: RecordStore) -> tuple[set[str], dict[_ArchiveKey, Run]]:
    taken: set[str] = set()
    snapshots: dict[_ArchiveKey, Run] = {}
    cursor = None
    while True:
        page = await records.list_runs(SINGLE_USER, DEFAULT_PROJECT, limit=500, cursor=cursor)
        for run in page.items:
            taken.add(run.code)
            source = run.provenance.get("promptfoo")
            if run.state == "completed" and isinstance(source, dict) and not source.get("import_copy"):
                for key in _archive_keys(source):
                    snapshots.setdefault(key, run)
        if (cursor := page.next_cursor) is None:
            return taken, snapshots


async def _reuse_snapshot(store: EvalStore, records: RecordStore, run: Run, archive_sha256: str | None) -> Run:
    native = await store.get_run(SINGLE_USER, run.project_id, run.id)
    if native is None or native.id != run.id or native.dataset_digest != run.dataset_digest:
        raise PromptfooResultsError(f"the stored run {run.code} has no matching MLflow record")
    if native.state != "completed" or native.counts != run.counts:
        # The run record is marked complete only after its results and traces have been kept.
        await store.finish_run(SINGLE_USER, run.project_id, run.id, state="completed", counts=run.counts)

    recorded = run.provenance["promptfoo"]
    if archive_sha256 is None or "archive_sha256" in recorded:
        return run
    # The original raw SHA-256 matched, so later exports can also find this earlier import.
    provenance = {**run.provenance, "promptfoo": {**recorded, "archive_sha256": archive_sha256}}
    return await records.update_run(
        SINGLE_USER, run.model_copy(update={"provenance": provenance}), expected_state="completed"
    )


def _reserve_code(provider: str, taken: set[str]) -> str:
    slug = re.sub(r"[^A-Za-z0-9._:-]+", "-", provider).strip("-") or "provider"
    prefix = f"promptfoo-{slug}-"
    number = next(number for number in count(1) if f"{prefix}{number}" not in taken)
    code = f"{prefix}{number}"
    taken.add(code)
    return code


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
