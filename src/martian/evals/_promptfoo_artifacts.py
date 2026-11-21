"""Trace graphs and content-addressed media carried by a Promptfoo result archive."""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from collections import defaultdict, deque
from dataclasses import dataclass
from itertools import count
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from martian.evals.model import Span, TraceState

if TYPE_CHECKING:
    from martian.evals.promptfoo_import import PromptfooOutputFile, PromptfooResult

MEDIA_LIMIT = 50 * 1024 * 1024
MEDIA_PREFIX = "promptfoo-media/"
MEDIA_HASH = r"^[a-f0-9]{64}$"
_URI = re.compile(r"promptfoo://blob/([a-f0-9]{64})(?![a-f0-9])", re.IGNORECASE)
_MIME = re.compile(r"[a-z0-9][a-z0-9.+-]{0,63}/[a-z0-9][a-z0-9.+-]{0,63}")
_EpochMilliseconds = Annotated[float, Field(ge=0, lt=9_223_372_036_854)]
_SpanId = Annotated[str, Field(min_length=1, max_length=200)]
_STATE: dict[int, TraceState] = {1: "ok", 2: "error"}


class ArtifactError(ValueError):
    """An archive's referenced evidence cannot be associated or verified."""


class _SourceSpan(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: _SpanId = Field(alias="spanId")
    parent_id: str | None = Field(default=None, alias="parentSpanId", max_length=200)
    name: str
    start_ms: _EpochMilliseconds = Field(alias="startTime")
    end_ms: _EpochMilliseconds | None = Field(default=None, alias="endTime")
    attributes: dict[str, Any] | None = None
    status_code: int | None = Field(default=None, alias="statusCode")
    status_message: str | None = Field(default=None, alias="statusMessage")


class _SourceTrace(BaseModel):
    model_config = ConfigDict(extra="ignore")

    evaluation_id: str = Field(alias="evaluationId")
    test_case_id: str = Field(alias="testCaseId")
    metadata: dict[str, Any] | None = None
    spans: list[_SourceSpan]


class _SourceMedia(BaseModel):
    model_config = ConfigDict(extra="ignore")

    mime_type: str = Field(alias="mimeType")
    size_bytes: Annotated[int, Field(strict=True, ge=0, le=MEDIA_LIMIT)] = Field(alias="sizeBytes")
    data: Annotated[str, Field(max_length=4 * ((MEDIA_LIMIT + 2) // 3))]


@dataclass(frozen=True)
class ArchivedMedia:
    sha256: str
    mime_type: str
    size_bytes: int
    data: str

    def decode(self) -> bytes:
        if len(self.data) != 4 * ((self.size_bytes + 2) // 3):
            raise ArtifactError(f"embedded media {self.sha256} does not match its declared size")
        try:
            binary = base64.b64decode(self.data, validate=True)
        except (ValueError, binascii.Error):
            raise ArtifactError(f"embedded media {self.sha256} is not valid base64") from None
        if len(binary) != self.size_bytes or hashlib.sha256(binary).hexdigest() != self.sha256:
            raise ArtifactError(f"embedded media {self.sha256} does not match its size and SHA-256")
        return binary


@dataclass(frozen=True)
class ArchivedTrace:
    evaluation_id: str
    test_case_id: str
    metadata: dict[str, Any]
    spans: list[Span]
    media_refs: set[str]


@dataclass(frozen=True)
class ArchiveArtifacts:
    traces: dict[str, ArchivedTrace]
    media: dict[str, ArchivedMedia]
    referenced: set[str]

    @property
    def version(self) -> int | None:
        return 1 if self.traces or self.referenced else None

    def media_for(self, row: PromptfooResult) -> set[str]:
        refs = _row_media(row)
        trace = self.traces.get(row.trace_id or "")
        return refs | trace.media_refs if trace is not None else refs


def _media_refs(*values: Any) -> set[str]:
    refs: set[str] = set()
    pending = list(values)
    while pending:
        value = pending.pop()
        if isinstance(value, str):
            refs.update(match.group(1).lower() for match in _URI.finditer(value))
        elif isinstance(value, dict):
            pending.extend(value.values())
        elif isinstance(value, list | tuple):
            pending.extend(value)
    return refs


def _row_media(row: PromptfooResult) -> set[str]:
    grading = row.grading_result.model_dump() if row.grading_result else None
    return _media_refs(row.prompt.raw, row.vars, row.test_case, row.response, row.metadata, grading)


def _span_tree(trace: _SourceTrace) -> list[Span]:
    parents = {span.id for span in trace.spans}
    if len(parents) != len(trace.spans):
        raise ArtifactError("a linked trace repeats a span ID")

    children: dict[str | None, list[Span]] = defaultdict(list)
    for source in trace.spans:
        parent = source.parent_id if source.parent_id in parents else None
        attributes = dict(source.attributes or {})
        if parent is None and source.parent_id:
            attributes["promptfoo.original_parent_span_id"] = source.parent_id
        if source.status_message:
            attributes["promptfoo.status_message"] = source.status_message
        if source.end_ms is not None and source.end_ms < source.start_ms:
            raise ArtifactError("a linked trace ends a span before it starts")
        children[parent].append(
            Span(
                id=source.id,
                parent_id=parent,
                name=source.name,
                kind="CHAIN" if attributes.get("promptfoo.span.role") == "test_case" else "",
                status=_STATE.get(source.status_code or 0, "unknown"),
                start_ms=source.start_ms,
                end_ms=source.end_ms,
                attributes=attributes,
            )
        )

    roots = children[None]
    pending = deque(roots)
    spans: list[Span] = []
    if len(roots) > 1:
        # The SDK trace view requires one root; filtered source traces can have several.
        wrapper = next(f"promptfoo-root-{n}" for n in count(1) if f"promptfoo-root-{n}" not in parents)
        spans.append(
            Span(
                id=wrapper,
                name="Promptfoo trace",
                kind="CHAIN",
                start_ms=min(span.start_ms or 0 for span in roots),
                end_ms=max((span.end_ms for span in roots if span.end_ms is not None), default=None),
            )
        )
        pending = deque(span.model_copy(update={"parent_id": wrapper}) for span in roots)
    while pending:
        span = pending.popleft()
        spans.append(span)
        pending.extend(children[span.id])
    if len(spans) != len(trace.spans) + int(len(roots) > 1):
        raise ArtifactError("a linked trace has a cyclic span hierarchy")
    return spans


def prepare_artifacts(file: PromptfooOutputFile) -> ArchiveArtifacts:
    linked = {row.trace_id for row in file.results.results if row.trace_id}
    traces: dict[str, ArchivedTrace] = {}
    for raw in file.traces if isinstance(file.traces, list) else []:
        if not isinstance(raw, dict) or not isinstance(trace_id := raw.get("traceId"), str) or trace_id not in linked:
            continue
        if trace_id in traces:
            raise ArtifactError("the archive repeats a linked trace ID")
        try:
            trace = _SourceTrace.model_validate(raw)
        except ValidationError:
            raise ArtifactError("the archive has a malformed linked trace") from None
        metadata = trace.metadata or {}
        spans = _span_tree(trace)
        traces[trace_id] = ArchivedTrace(
            trace.evaluation_id,
            trace.test_case_id,
            metadata,
            spans,
            _media_refs(metadata, *(span.attributes for span in spans)),
        )

    referenced: set[str] = set()
    for row in file.results.results:
        referenced.update(_row_media(row))
        if linked_trace := traces.get(row.trace_id or ""):
            expected = (
                ("testIdx", row.test_idx),
                ("promptIdx", row.prompt_idx),
                ("repeatIndex", row.repeat_index or 0),
            )
            if any(
                identity and identity != linked_trace.evaluation_id for identity in (row.evaluation_id, file.eval_id)
            ) or any(key in linked_trace.metadata and linked_trace.metadata[key] != value for key, value in expected):
                raise ArtifactError("a linked trace does not match its recorded evaluation and case")
            referenced.update(linked_trace.media_refs)

    media: dict[str, ArchivedMedia] = {}
    for raw in file.blob_assets if isinstance(file.blob_assets, list) else []:
        if not isinstance(raw, dict) or not isinstance(sha := raw.get("hash"), str) or sha.lower() not in referenced:
            continue
        sha = sha.lower()
        if sha in media:
            raise ArtifactError(f"the archive repeats embedded media {sha}")
        try:
            descriptor = _SourceMedia.model_validate(raw)
        except ValidationError:
            raise ArtifactError(f"embedded media {sha} has invalid metadata or exceeds the 50 MiB limit") from None
        mime = descriptor.mime_type.strip().lower()
        asset = ArchivedMedia(
            sha, mime if _MIME.fullmatch(mime) else "application/octet-stream", descriptor.size_bytes, descriptor.data
        )
        asset.decode()
        media[sha] = asset
    return ArchiveArtifacts(traces, media, referenced)
