"""Inspect AI logs as runs in the local edition's data folder, so that ``martian ui`` opens,
compares and reviews them.

``martian evals import inspect <log>`` reads a log Inspect AI wrote, in either of its formats: an
``.eval`` archive (a zip holding ``header.json`` and one file per sample, its entries compressed
with Zstandard or Deflate) or a ``.json`` log. It reads them against Inspect's published log
schema (``EvalLog``, log format version 2) without Inspect itself, and writes each log as one run
through the store and the record store, as ``martian evals run`` does
(:mod:`martian.evals.run_record`), as the single local user in the default project:

- The run is a ``Run`` of source ``local`` with a ``stored`` target: its outputs come from the
  log. Its code is ``inspect-<task>-<n>``. ``provenance["inspect"]`` keeps the log's path and
  SHA-256, its eval id, task, model and status, and Inspect's own results: each scorer's metrics.
- Each sample, once per epoch, is a ``CaseResult`` with a ``Score`` for every Inspect score and a
  trace whose spans are the sample's messages and tool calls.
- Score values are kept exactly. A dict or list value is one score per entry, named
  ``<scorer>/<key>``. Inspect's unscored value (NaN) is a score with no value and an error.

``docs/eval-model.md`` maps every field. Importing the same log again makes another run.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import math
import struct
import uuid
import zipfile
import zlib
from collections.abc import Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import zstandard
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from martian._version import __version__
from martian.evals.model import (
    CaseResult,
    CaseStatus,
    Counts,
    Run,
    RunState,
    Score,
    ScorerVersion,
    Span,
    StoreRef,
    Target,
    Trace,
    Value,
    digest,
)
from martian.evals.server.principal import SINGLE_USER
from martian.evals.server.projection import DEFAULT_PROJECT

if TYPE_CHECKING:
    from martian.evals.records import RecordStore
    from martian.evals.store import EvalStore

LOG_VERSION = 2
"""The version of Inspect's log format (``EvalLog.version``) this importer reads."""

ZSTANDARD = 93
"""The zip compression method of Zstandard, which Inspect compresses ``.eval`` entries with."""

SCORER_PLACEHOLDER = "88F74D2C"
"""The name Inspect gives the only scorer's scores in a sample until the log names it."""

ATTACHMENT = "attachment://"
"""How a log refers to text it keeps once, in the sample's ``attachments``."""

VERDICTS = {"C": True, "I": False, "P": False, "N": False}
"""Inspect's letter values as verdicts: only CORRECT passes. PARTIAL (P) and NOANSWER (N) do not."""

RUN_STATES: dict[str, RunState] = {"success": "completed", "cancelled": "cancelled", "error": "failed"}
"""Inspect's log status as the run's state. A log still ``started`` is refused."""


class InspectLogError(Exception):
    """A file this importer cannot read as an Inspect log. The message says why."""


# --- Inspect's log schema: the parts the import reads ------------------------------------------


class _Part(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class InspectScore(_Part):
    """Inspect's ``Score``: ``value`` is text, a number, a boolean, or a list or dict of them."""

    value: Any
    answer: str | None = None
    explanation: str | None = None
    reason: str | None = None
    metadata: dict[str, Any] | None = None
    history: list[Any] = Field(default_factory=list)


class InspectError(_Part):
    message: str
    traceback: str = ""


class InspectSample(_Part):
    id: int | str
    epoch: int = Field(ge=1)
    input: str | list[dict[str, Any]]
    choices: list[str] | None = None
    target: str | list[str]
    messages: list[dict[str, Any]] = Field(default_factory=list)
    output: dict[str, Any] = Field(default_factory=dict)
    scores: dict[str, InspectScore] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    events: list[dict[str, Any]] = Field(default_factory=list)
    model_usage: dict[str, dict[str, Any]] = Field(default_factory=dict)
    started_at: str | None = None
    completed_at: str | None = None
    total_time: float | None = None
    working_time: float | None = None
    uuid: str | None = None
    error: InspectError | None = None
    limit: dict[str, Any] | None = None
    attachments: dict[str, str] = Field(default_factory=dict)


class InspectDataset(_Part):
    name: str | None = None
    location: str | None = None
    samples: int | None = None
    sample_ids: list[int | str] | None = None
    shuffled: bool | None = None


class InspectScorer(_Part):
    name: str
    options: dict[str, Any] | None = None


class InspectConfig(_Part):
    epochs: int | None = None
    limit: int | list[int] | None = None


class InspectSpec(_Part):
    eval_id: str = Field(min_length=1)
    run_id: str = ""
    eval_set_id: str | None = None
    created: str
    task: str = Field(min_length=1)
    task_id: str = ""
    task_version: int | str = 0
    task_file: str | None = None
    dataset: InspectDataset
    model: str
    config: InspectConfig
    scorers: list[InspectScorer] | None = None
    tags: list[str] | None = None
    packages: dict[str, str] = Field(default_factory=dict)


class InspectEvalLog(_Part):
    version: int
    status: Literal["started", "success", "cancelled", "error"]
    eval: InspectSpec
    results: dict[str, Any] | None = None
    stats: dict[str, Any] = Field(default_factory=dict)
    error: InspectError | None = None
    samples: list[InspectSample] | None = None


@dataclass(frozen=True)
class InspectLog:
    """One Inspect log as it was read: where from, its SHA-256, its format and its content."""

    path: Path
    sha256: str
    format: Literal["eval", "json"]
    log: InspectEvalLog


def read_inspect_log(path: Path) -> InspectLog:
    """The Inspect log at ``path``, ``.eval`` or ``.json``, with its samples. Raises
    :class:`InspectLogError` for a file that is not a finished Inspect log of format version 2, or
    for a log the model cannot hold (:func:`_one_score_per_name`)."""
    path = path.expanduser().resolve()
    try:
        data = path.read_bytes()
    except OSError as error:
        raise InspectLogError(f"cannot read {path}: {error.strerror or error}") from None
    if path.suffix == ".json":
        found = _json(data, path.name)
        form: Literal["eval", "json"] = "json"
    elif path.suffix == ".eval":
        found = _eval_archive(data, path.name)
        form = "eval"
    else:
        raise InspectLogError(f"{path.name}: an Inspect log ends in .eval or .json")
    if not isinstance(found, dict):
        raise InspectLogError(f"{path.name} is not an Inspect log: it holds no JSON object")
    if found.get("version") != LOG_VERSION:
        raise InspectLogError(
            f"{path.name}: Inspect log format version {found.get('version')!r}; "
            f"this importer reads version {LOG_VERSION}"
        )
    try:
        log = InspectEvalLog.model_validate(found)
    except ValidationError as error:
        problem = error.errors()[0]
        where = ".".join(str(part) for part in problem["loc"])
        raise InspectLogError(f"{path.name} is not an Inspect log: {where}: {problem['msg']}") from None
    if log.status == "started":
        raise InspectLogError(f"{path.name}: the eval has not finished (status started); import its log once it ends")
    if not log.samples:
        raise InspectLogError(f"{path.name} has no samples: Inspect wrote it without them (--no-log-samples)")
    _one_score_per_name(log, path.name)
    return InspectLog(path=path, sha256=hashlib.sha256(data).hexdigest(), format=form, log=log)


def _one_score_per_name(log: InspectEvalLog, name: str) -> None:
    """Refuse a log that gives a sample two scores of one name and source, before anything is
    written: a case holds one score of a name and source. A scorer's dict entry ``<scorer>/<key>``
    can be named like another scorer, such as a code scorer named ``tool_use/calls`` beside the
    entry ``calls`` of the code scorer ``tool_use``. Each score's value is checked as importing
    it does."""
    keys = _entry_keys(log)
    for sample in log.samples or []:
        judges = _judges(sample.events)
        seen: dict[tuple[str, str], Score] = {}
        for scorer, found in _scores_of(sample, log).items():
            for score in inspect_scores(
                scorer, found, run_id="", case_id=case_id(sample), keys=keys.get(scorer), judge=judges.get(scorer)
            ):
                first = seen.setdefault((score.scorer, score.source), score)
                if first is not score:
                    kind = "judge" if score.source == "llm_judge" else "code"
                    raise InspectLogError(
                        f"{name}: sample {case_id(sample)} has two {kind} scores named {score.scorer}: "
                        f"{_origin(first)} and {_origin(score)}; a case holds one score of a name and source"
                    )


def _origin(score: Score) -> str:
    """Which part of the log a score is: a scorer's value, or an entry of its dict or list."""
    scorer = score.metadata.get("inspect.scorer")
    for key in ("inspect.key", "inspect.index"):
        if key in score.metadata:
            return f"the entry {score.metadata[key]} of the scorer {scorer}"
    return f"the scorer {scorer}"


def _json(data: bytes, name: str) -> Any:
    # Inspect writes its unscored value as the JSON constant NaN, which json.loads reads.
    try:
        return json.loads(data)
    except (UnicodeDecodeError, ValueError):
        raise InspectLogError(f"{name} is not an Inspect log: it is not JSON") from None


def _eval_archive(data: bytes, name: str) -> dict[str, Any]:
    """An ``.eval`` log as Inspect's ``EvalLog`` JSON: its header with its samples."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise InspectLogError(f"{name} is not an Inspect log: it is not a zip archive") from None
    with archive:
        entries = {info.filename: info for info in archive.infolist()}
        if "header.json" not in entries:
            raise InspectLogError(f"{name} has no header.json: the eval is still running or stopped before it finished")
        header = _json(_entry(archive, data, entries["header.json"], name), name)
        if not isinstance(header, dict):
            raise InspectLogError(f"{name} is not an Inspect log: header.json holds no JSON object")
        header["samples"] = [
            _json(_entry(archive, data, info, name), name)
            for filename, info in entries.items()
            if filename.startswith("samples/") and filename.endswith(".json")
        ]
    return header


def _entry(archive: zipfile.ZipFile, data: bytes, info: zipfile.ZipInfo, name: str) -> bytes:
    """One entry of the archive. Python's zipfile reads Deflate entries; a Zstandard entry is read
    from after its local header and decompressed here."""
    if info.compress_type != ZSTANDARD:
        try:
            return archive.read(info)
        except (zipfile.BadZipFile, NotImplementedError, zlib.error) as error:
            raise InspectLogError(f"{name}: cannot read {info.filename}: {error}") from None
    header = data[info.header_offset : info.header_offset + 30]
    if len(header) < 30 or header[:4] != b"PK\x03\x04":
        raise InspectLogError(f"{name}: {info.filename} has no local header")
    name_length, extra_length = struct.unpack("<HH", header[26:])
    begin = info.header_offset + 30 + name_length + extra_length
    try:
        with zstandard.ZstdDecompressor().stream_reader(
            data[begin : begin + info.compress_size], read_across_frames=True
        ) as reader:
            content = reader.read()
    except zstandard.ZstdError as error:
        raise InspectLogError(f"{name}: cannot decompress {info.filename}: {error}") from None
    if zlib.crc32(content) != info.CRC:
        raise InspectLogError(f"{name}: {info.filename} is damaged: its checksum does not match")
    return content


# --- the log as a run ----------------------------------------------------------------------------


@dataclass(frozen=True)
class ImportedCase:
    """One sample of the log, once per epoch: its result, its scores and its trace."""

    result: CaseResult
    scores: list[Score]
    trace: Trace


@dataclass(frozen=True)
class ImportedRun:
    """A log as a finished run and its cases, before anything is written."""

    run: Run
    cases: list[ImportedCase]


def inspect_run(found: InspectLog, *, run_id: str, code: str) -> ImportedRun:
    """The log ``found`` as the run ``run_id`` coded ``code``, with its case results, scores and
    traces. Raises :class:`InspectLogError` for a log the model cannot hold."""
    log = found.log
    samples = _ordered(log)
    keys = _entry_keys(log)
    judges = {sample_key: _judges(sample.events) for sample_key, sample in samples}
    cases = [
        _case(log, sample, run_id=run_id, code=code, position=position, keys=keys, judges=judges[sample_key])
        for position, (sample_key, sample) in enumerate(samples)
    ]
    distinct = {str(sample.id) for _, sample in samples}
    statuses = [case.result.status for case in cases]
    counts = Counts(
        cases=len(cases),
        passed=statuses.count("passed"),
        failed=statuses.count("failed"),
        errors=statuses.count("error"),
        skipped=statuses.count("skipped"),
    )
    stats = log.stats
    started = _when(stats.get("started_at"))
    run = Run(
        id=run_id,
        project_id=DEFAULT_PROJECT,
        code=code,
        source="local",
        state=RUN_STATES[log.status],
        dataset_name=log.eval.dataset.name or log.eval.task,
        dataset_digest=_dataset_digest(samples),
        target=Target(kind="stored"),
        scorers=_scorers(log, keys, judges),
        counts=counts,
        truncated=log.eval.dataset.samples is not None and len(distinct) < log.eval.dataset.samples,
        error=log.error.message if log.error else "",
        provenance={"inspect": _provenance(found), "sdk_version": __version__},
        requested_by=SINGLE_USER.user_id,
        created_at=_when(log.eval.created) or started,
        started_at=started,
        finished_at=_when(stats.get("completed_at")),
    )
    return ImportedRun(run=run, cases=cases)


def _ordered(log: InspectEvalLog) -> list[tuple[tuple[str, int], InspectSample]]:
    """The samples in the dataset's order, each sample's epochs together."""
    order = {str(sample_id): index for index, sample_id in enumerate(log.eval.dataset.sample_ids or [])}
    found: dict[tuple[str, int], InspectSample] = {}
    for sample in log.samples or []:
        key = (str(sample.id), sample.epoch)
        if key in found:
            raise InspectLogError(f"the log has sample {sample.id} epoch {sample.epoch} twice")
        found[key] = sample
    arrival = {key: index for index, key in enumerate(found)}
    ranked = sorted(found, key=lambda key: (order.get(key[0], len(order) + arrival[key]), key[1]))
    return [(key, found[key]) for key in ranked]


def case_id(sample: InspectSample) -> str:
    """A sample's case id: its own id for its first epoch, so runs with different numbers of epochs
    pair on it, and the id with the epoch for every later one."""
    return str(sample.id) if sample.epoch == 1 else f"{sample.id} (epoch {sample.epoch})"


def _scores_of(sample: InspectSample, log: InspectEvalLog) -> dict[str, InspectScore]:
    scores = dict(sample.scores or {})
    if SCORER_PLACEHOLDER in scores and log.results and log.results.get("scores"):
        scores[str(log.results["scores"][0]["name"])] = scores.pop(SCORER_PLACEHOLDER)
    return scores


def _entry_keys(log: InspectEvalLog) -> dict[str, list[str]]:
    """The entries each dict- or list-valued scorer gives, in the order samples first give them."""
    keys: dict[str, dict[str, None]] = {}
    for sample in log.samples or []:
        for name, score in _scores_of(sample, log).items():
            if isinstance(score.value, dict):
                keys.setdefault(name, {}).update(dict.fromkeys(str(key) for key in score.value))
            elif isinstance(score.value, list):
                keys.setdefault(name, {}).update(dict.fromkeys(str(index) for index in range(len(score.value))))
    return {name: list(found) for name, found in keys.items()}


def _judges(events: list[dict[str, Any]]) -> dict[str, str]:
    """The model each scorer called, by scorer name: Inspect records a scorer's work in a span of
    type ``scorer``, and a model call inside one is that scorer grading with a model."""
    spans: dict[str, tuple[str | None, str, str]] = {}
    judges: dict[str, str] = {}
    for event in events:
        if event.get("event") == "span_begin" and isinstance(event.get("id"), str):
            spans[event["id"]] = (event.get("parent_id"), str(event.get("type") or ""), str(event.get("name") or ""))
        elif event.get("event") == "model":
            span = event.get("span_id")
            seen: set[str] = set()
            while isinstance(span, str) and span in spans and span not in seen:
                seen.add(span)
                parent, kind, name = spans[span]
                if kind == "scorer":
                    judges.setdefault(name, str(event.get("model") or ""))
                    break
                span = parent
    return judges


def _scorers(
    log: InspectEvalLog, keys: dict[str, list[str]], judges: dict[tuple[str, int], dict[str, str]]
) -> list[ScorerVersion]:
    """The run's scorers: one per score name, in the order the eval declares its scorers."""
    declared = {scorer.name: scorer for scorer in log.eval.scorers or []}
    names = list(declared)
    for sample in log.samples or []:
        names += [name for name in _scores_of(sample, log) if name not in names]
    found = []
    for name in names:
        model = next((by_scorer[name] for by_scorer in judges.values() if name in by_scorer), None)
        options = declared[name].options if name in declared else None
        for score_name in [f"{name}/{key}" for key in keys[name]] if name in keys else [name]:
            found.append(
                ScorerVersion(
                    name=score_name,
                    kind="llm_judge" if model is not None else "code",
                    type=name[:80],
                    model=model or None,
                    parameters=_plain(options or {}, {}),
                )
            )
    return found


def _case(
    log: InspectEvalLog,
    sample: InspectSample,
    *,
    run_id: str,
    code: str,
    position: int,
    keys: dict[str, list[str]],
    judges: dict[str, str],
) -> ImportedCase:
    attachments = sample.attachments
    identifier = case_id(sample)
    finished = _when(sample.completed_at)
    scores = [
        score.model_copy(update={"created_at": finished})
        for name, found in _scores_of(sample, log).items()
        for score in inspect_scores(
            name,
            found,
            run_id=run_id,
            case_id=identifier,
            keys=keys.get(name),
            judge=judges.get(name),
            attachments=attachments,
        )
    ]
    output = _plain(sample.output.get("completion") or "", attachments)
    result = CaseResult(
        run_id=run_id,
        case_id=identifier,
        position=position,
        status=_status(sample, scores),
        inputs=_inputs(sample),
        expectations={"expected_response": sample.target},
        output=output,
        error=sample.error.message if sample.error else "",
        latency_ms=None if sample.total_time is None else sample.total_time * 1000,
        finished_at=finished,
    )
    trace_id = "tr-" + uuid.uuid4().hex
    usage = list(sample.model_usage.values())
    costs = [used.get("total_cost") for used in usage]
    request = result.inputs.get("prompt")
    trace = Trace(
        id=trace_id,
        project_id=DEFAULT_PROJECT,
        name=f"{code} {identifier}",
        state="error" if sample.error else "ok",
        request_time=_when(sample.started_at),
        latency_ms=result.latency_ms,
        request=request if isinstance(request, str) else json.dumps(result.inputs, ensure_ascii=False, sort_keys=True),
        response=output if isinstance(output, str) else json.dumps(output, ensure_ascii=False),
        run_id=run_id,
        model=log.eval.model or None,
        tokens=sum(int(used.get("total_tokens") or 0) for used in usage) if usage else None,
        cost=sum(float(cost) for cost in costs if cost is not None) if usage and None not in costs else None,
        tags={"martian.case_id": identifier, "inspect.sample_id": str(sample.id), "inspect.epoch": str(sample.epoch)},
        metadata={
            key: value
            for key, value in (("inspect.eval_id", log.eval.eval_id), ("inspect.sample_uuid", sample.uuid))
            if value
        },
        scores=[score.model_copy(update={"trace_id": trace_id}) for score in scores],
        spans=spans_of(sample, task=log.eval.task),
    )
    return ImportedCase(result=result, scores=scores, trace=trace)


def _inputs(sample: InspectSample) -> dict[str, Any]:
    """What the model was given: the prompt, or the chat messages without their ids (Inspect gives
    each message a new id every run, and a comparison pairs cases on their inputs)."""
    inputs: dict[str, Any] = (
        {"prompt": _plain(sample.input, sample.attachments)}
        if isinstance(sample.input, str)
        else {
            "messages": [
                {key: value for key, value in _plain(message, sample.attachments).items() if key != "id"}
                for message in sample.input
            ]
        }
    )
    if sample.choices is not None:
        inputs["choices"] = list(sample.choices)
    return inputs


def _dataset_digest(samples: list[tuple[tuple[str, int], InspectSample]]) -> str:
    """The digest of the samples the run covers, whatever their order and epochs: two runs over the
    same samples compare case by case."""
    distinct = {sample_id: sample for (sample_id, _), sample in samples}
    return digest(
        [
            {
                "id": sample_id,
                **_inputs(distinct[sample_id]),
                "target": distinct[sample_id].target,
            }
            for sample_id in sorted(distinct)
        ]
    )


def _status(sample: InspectSample, scores: list[Score]) -> CaseStatus:
    """``error`` when the sample failed; ``failed`` when a score fails; ``error`` when one is
    unscored; ``passed`` when one passes; ``skipped`` when no score gives a verdict."""
    if sample.error is not None:
        return "error"
    verdicts = [score.passed for score in scores if score.passed is not None]
    if False in verdicts:
        return "failed"
    if any(score.error for score in scores):
        return "error"
    return "passed" if verdicts else "skipped"


def inspect_scores(
    name: str,
    score: InspectScore,
    *,
    run_id: str,
    case_id: str,
    keys: list[str] | None = None,
    judge: str | None = None,
    attachments: dict[str, str] | None = None,
) -> list[Score]:
    """One Inspect score as the model's scores, its value kept exactly.

    A text, number or boolean is one score. A dict or list is one score per entry, named
    ``<name>/<key>`` (``<name>/<index>`` for a list); ``keys`` are the entries the scorer gives in
    other samples, so an unscored sample has a score for each. NaN, Inspect's unscored value, is a
    score with no value whose error says so. ``passed`` is the verdict of a boolean, or of
    Inspect's letters: ``C`` passes, ``I``, ``P`` and ``N`` fail; other values have none. A score a
    model graded (``judge``, the model) has source ``llm_judge``; any other, ``code``."""
    attachments = attachments or {}
    common = {
        key: value
        for key, value in (
            ("inspect.scorer", name),
            ("inspect.answer", _plain(score.answer, attachments)),
            ("inspect.reason", score.reason),
            ("inspect.metadata", _plain(score.metadata, attachments) if score.metadata else None),
            ("inspect.history", _plain(score.history, attachments) if score.history else None),
        )
        if value is not None
    }
    value = score.value
    if isinstance(value, dict):
        parts = [(f"{name}/{key}", entry, {"inspect.key": key}) for key, entry in value.items()]
    elif isinstance(value, list):
        parts = [(f"{name}/{index}", entry, {"inspect.index": index}) for index, entry in enumerate(value)]
    elif keys is not None and _unscored(value):
        parts = [(f"{name}/{key}", value, {"inspect.key": key}) for key in keys]
    else:
        parts = [(name, value, {})]
    found = []
    for scorer, entry, where in parts:
        error = ""
        metadata = {**common, **where}
        if _unscored(entry):
            metadata["inspect.value"] = "NaN"
            error = f"unscored: {score.reason}" if score.reason else "unscored"
            entry = None
        elif isinstance(entry, float) and not math.isfinite(entry):
            raise InspectLogError(f"sample {case_id}: score {scorer} is {entry}; a score value must be finite")
        elif entry is not None and not isinstance(entry, bool | int | float | str):
            raise InspectLogError(
                f"sample {case_id}: score {scorer} is a {type(entry).__name__}; Inspect's values are text, "
                "numbers, booleans, and lists and dicts of them"
            )
        kept: Value = entry
        found.append(
            Score(
                run_id=run_id,
                case_id=case_id,
                scorer=scorer,
                source="llm_judge" if judge is not None else "code",
                source_id=judge or "",
                value=kept,
                passed=verdict(kept),
                rationale=_plain(score.explanation or "", attachments),
                error=error,
                metadata=metadata,
            )
        )
    return found


def verdict(value: Value) -> bool | None:
    """The verdict of an Inspect value: a boolean's own, ``C`` passes and ``I``, ``P`` and ``N``
    fail. Numbers and other text give none."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return VERDICTS.get(value)
    return None


def _unscored(value: Any) -> bool:
    return isinstance(value, float) and math.isnan(value)


# --- the trace -----------------------------------------------------------------------------------


def spans_of(sample: InspectSample, *, task: str) -> list[Span]:
    """The sample's messages and tool calls as spans under one root span, the sample.

    Each assistant message is a ``CHAT_MODEL`` span whose inputs are the messages since the one
    before it and whose output is the message; each of its tool calls is a ``TOOL`` span with the
    call's arguments and the tool's reply. Messages no assistant message follows, such as those of
    a sample that failed before the model answered, are one more span. Times and token use come
    from the model and tool events Inspect recorded for them."""
    attachments = sample.attachments
    models: dict[str, dict[str, Any]] = {}
    tools: dict[str, dict[str, Any]] = {}
    for event in sample.events:
        if event.get("event") == "model":
            choices = (event.get("output") or {}).get("choices") or []
            message_id = ((choices[0] if choices else {}).get("message") or {}).get("id")
            if isinstance(message_id, str):
                models.setdefault(message_id, event)
        elif event.get("event") == "tool" and isinstance(event.get("id"), str):
            tools.setdefault(event["id"], event)
    messages: list[dict[str, Any]] = [_plain(message, attachments) for message in sample.messages]
    replies = {
        message["tool_call_id"]: message
        for message in messages
        if message.get("role") == "tool" and isinstance(message.get("tool_call_id"), str)
    }
    root_attributes: dict[str, Any] = {
        "inspect.sample_id": sample.id,
        "inspect.epoch": sample.epoch,
        "inspect.target": sample.target,
    }
    for key, value in (
        ("inspect.sample_uuid", sample.uuid),
        ("inspect.choices", sample.choices),
        ("inspect.metadata", sample.metadata or None),
        ("inspect.limit", sample.limit),
        ("inspect.working_time", sample.working_time),
        ("inspect.model_usage", sample.model_usage or None),
        ("inspect.error", sample.error.model_dump() if sample.error else None),
    ):
        if value is not None:
            root_attributes[key] = _plain(value, attachments)
    spans = [
        Span(
            id="sample",
            name=task,
            kind="AGENT",
            status="error" if sample.error else "ok",
            start_ms=_ms(_when(sample.started_at)),
            end_ms=_ms(_when(sample.completed_at)),
            inputs=_plain(sample.input, attachments),
            outputs=_plain(sample.output.get("completion") or "", attachments),
            attributes=root_attributes,
        )
    ]
    pending: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        if message.get("role") != "assistant":
            pending.append(message)
            continue
        event = models.get(str(message.get("id")), {})
        attributes: dict[str, Any] = {"inspect.message_id": message.get("id")}
        if event.get("model"):
            attributes["gen_ai.request.model"] = event["model"]
        if usage := (event.get("output") or {}).get("usage"):
            attributes["mlflow.chat.tokenUsage"] = _plain(usage, {})
        spans.append(
            Span(
                id=f"m{index}",
                parent_id="sample",
                name=str(message.get("model") or event.get("model") or "model"),
                kind="CHAT_MODEL",
                status="ok",
                start_ms=_ms(_when(event.get("timestamp"))),
                end_ms=_ms(_when(event.get("completed"))),
                inputs=pending,
                outputs=message,
                attributes=attributes,
            )
        )
        pending = []
        for number, call in enumerate(message.get("tool_calls") or []):
            reply = replies.get(str(call.get("id")))
            ran = tools.get(str(call.get("id")), {})
            details = {
                key: value
                for key, value in (
                    ("inspect.tool_call_id", call.get("id")),
                    ("inspect.parse_error", call.get("parse_error")),
                    ("inspect.tool_error", reply.get("error") if reply else None),
                )
                if value is not None
            }
            spans.append(
                Span(
                    id=f"m{index}.t{number}",
                    parent_id="sample",
                    name=str(call.get("function") or "tool"),
                    kind="TOOL",
                    status="unknown" if reply is None else "error" if reply.get("error") else "ok",
                    start_ms=_ms(_when(ran.get("timestamp"))),
                    end_ms=_ms(_when(ran.get("completed"))),
                    inputs=call.get("arguments"),
                    outputs=reply.get("content") if reply else None,
                    attributes=details,
                )
            )
    if pending:
        spans.append(
            Span(
                id="unanswered",
                parent_id="sample",
                name="messages with no model reply",
                kind="UNKNOWN",
                status="error" if sample.error else "unknown",
                inputs=pending,
            )
        )
    return spans


# --- values --------------------------------------------------------------------------------------


def _plain(value: Any, attachments: dict[str, str]) -> Any:
    """``value`` with the log's attachment references replaced by the text they name, and numbers
    that are not finite as their text (``NaN``), which every store keeps."""
    if isinstance(value, str):
        if value.startswith(ATTACHMENT):
            return attachments.get(value[len(ATTACHMENT) :], value)
        return value
    if isinstance(value, float) and not math.isfinite(value):
        return str(value).replace("nan", "NaN").replace("inf", "Infinity")
    if isinstance(value, list):
        return [_plain(entry, attachments) for entry in value]
    if isinstance(value, dict):
        return {str(key): _plain(entry, attachments) for key, entry in value.items()}
    return value


def _when(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        found = datetime.fromisoformat(value)
    except ValueError:
        return None
    return found if found.tzinfo else found.replace(tzinfo=UTC)


def _ms(when: datetime | None) -> float | None:
    return None if when is None else when.timestamp() * 1000


def _provenance(found: InspectLog) -> dict[str, Any]:
    """Where the run came from: the log, the eval in it, and Inspect's own results."""
    log = found.log
    spec = log.eval
    results = {key: value for key, value in (log.results or {}).items() if key != "sample_reductions"}
    provenance: dict[str, Any] = _plain(
        {
            "log": str(found.path),
            "log_sha256": found.sha256,
            "format": found.format,
            "log_version": log.version,
            "status": log.status,
            "eval_id": spec.eval_id,
            "run_id": spec.run_id,
            "eval_set_id": spec.eval_set_id,
            "task": spec.task,
            "task_id": spec.task_id,
            "task_version": spec.task_version,
            "task_file": spec.task_file,
            "model": spec.model,
            "created": spec.created,
            "epochs": spec.config.epochs,
            "dataset": spec.dataset.model_dump(exclude={"sample_ids"}),
            "tags": spec.tags or [],
            "packages": spec.packages,
            "results": results,
            "model_usage": log.stats.get("model_usage") or {},
        },
        {},
    )
    return provenance


# --- writing -------------------------------------------------------------------------------------


def import_inspect_log(path: Path, home: Path) -> Run:
    """Read the Inspect log at ``path`` and record it as a run in the data folder ``home``."""
    return import_inspect_logs([path], home)[0]


def import_inspect_logs(paths: list[Path], home: Path) -> list[Run]:
    """Read every log first, so that one that cannot be read stops the import before anything is
    written, then record each as a run in the data folder ``home``."""
    from martian.evals.local import LocalEditionError, open_stores

    found = [read_inspect_log(path) for path in paths]
    try:
        store, records = open_stores(home)
    except LocalEditionError:
        raise
    except Exception as error:  # noqa: BLE001 - a folder may fail to open in many ways; say which
        raise LocalEditionError(f"could not open the local data folder {home}: {type(error).__name__}") from None
    try:
        return [_record(store, records, log) for log in found]
    finally:
        records.close()


def _wait[T](work: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(work)


def _record(store: EvalStore, records: RecordStore, found: InspectLog) -> Run:
    """Record the log as a run."""
    imported = inspect_run(found, run_id=str(uuid.uuid4()), code=_wait(_code(records, found.log.eval.task)))
    return record_imported_run(store, records, imported)


def record_imported_run(store: EvalStore, records: RecordStore, imported: ImportedRun) -> Run:
    """Record a run read from another tool's files, as ``SINGLE_USER`` in the default project:
    ``running`` while its cases are written, then its own state. An import that stops early
    leaves the run ``failed``, or ``cancelled`` when interrupted. The promptfoo importer records
    its runs the same way."""
    final = imported.run
    started = final.model_copy(
        update={"state": "running", "counts": Counts(cases=final.counts.cases), "error": "", "finished_at": None}
    )
    run = _wait(_start(store, records, started))
    try:
        _wait(_cases(store, records, run, imported.cases))
    except BaseException as error:
        if isinstance(error, KeyboardInterrupt):
            _wait(_end(store, records, run.model_copy(update={"state": "cancelled"})))
        else:
            stopped = {"state": "failed", "error": f"the import stopped early: {type(error).__name__}"}
            _wait(_end(store, records, run.model_copy(update=stopped)))
        raise
    return _wait(
        _end(
            store,
            records,
            run.model_copy(
                update={
                    "state": final.state,
                    "counts": final.counts,
                    "error": final.error,
                    "finished_at": final.finished_at,
                }
            ),
        )
    )


async def _start(store: EvalStore, records: RecordStore, run: Run) -> Run:
    run = await store.start_run(SINGLE_USER, run)
    return await records.create_run(SINGLE_USER, run)


async def _cases(store: EvalStore, records: RecordStore, run: Run, cases: list[ImportedCase]) -> None:
    """Each case's trace, with its scores as assessments, then the case results and scores."""
    results: list[CaseResult] = []
    scores: list[Score] = []
    for case in cases:
        saved = await store.record_trace(SINGLE_USER, case.trace)
        # By name and source: a code score and a judge's may share a name on one case.
        stored = {(score.scorer, score.source): score.store for score in saved.scores}
        scores += [
            score.model_copy(
                update={"trace_id": saved.id, "store": stored.get((score.scorer, score.source), StoreRef())}
            )
            for score in case.scores
        ]
        results.append(case.result.model_copy(update={"store": StoreRef(trace_id=saved.id)}))
    await store.link_traces(SINGLE_USER, DEFAULT_PROJECT, run.id, [result.store.trace_id or "" for result in results])
    await records.put_results(SINGLE_USER, DEFAULT_PROJECT, results)
    if scores:
        await records.put_scores(SINGLE_USER, DEFAULT_PROJECT, scores)


async def _end(store: EvalStore, records: RecordStore, run: Run) -> Run:
    finished = await records.update_run(SINGLE_USER, run, expected_state="running")
    await store.finish_run(SINGLE_USER, DEFAULT_PROJECT, run.id, state=run.state, counts=run.counts)
    return finished


async def _code(records: RecordStore, task: str) -> str:
    """``inspect-``, the task's name and the run's number among the task's imported runs."""
    count, cursor = 0, None
    while True:
        page = await records.list_runs(SINGLE_USER, DEFAULT_PROJECT, limit=500, cursor=cursor)
        count += sum((run.provenance.get("inspect") or {}).get("task") == task for run in page.items)
        if (cursor := page.next_cursor) is None:
            return f"inspect-{task.rsplit('/', 1)[-1]}-{count + 1}"


__all__ = [
    "ImportedCase",
    "ImportedRun",
    "InspectLog",
    "InspectLogError",
    "case_id",
    "import_inspect_log",
    "import_inspect_logs",
    "inspect_run",
    "inspect_scores",
    "read_inspect_log",
    "record_imported_run",
    "spans_of",
    "verdict",
]
