"""The OpenTelemetry traces endpoint: OTLP/HTTP ``POST /v1/traces``, written through the store.

An application exports its spans with an OpenTelemetry SDK's OTLP/HTTP exporter, in the protobuf
or the JSON encoding, and the core records them as traces of a project: the default project, or
the one the ``x-martian-project-id`` header names. Traces only; the core serves no metrics or logs.

- **Who.** The request's principal is the edition's principal provider, as on every other route,
  so the server's sign-in refuses a request without credentials before the body is read. The
  principal's most privileged role must be one that writes (owner, admin or member).
- **Traces.** Spans are grouped by trace id. A trace's id is ``tr-`` and the OpenTelemetry trace id
  in hex, MLflow's form (:func:`mlflow_trace_id`), and its spans keep their OpenTelemetry span
  ids. Its name, state, start
  and latency are its root span's (the span without a parent); until the root span arrives the
  trace is ``in_progress``. Everything else (kind, model, tokens, session, user, request and
  response) is the trace conventions mapping, applied by the store's ``record_trace``.
- **Partial batches.** Exporters send spans as they end, so one trace's spans arrive in several
  requests, usually children before their root. Each request merges its spans into the trace
  already stored (a span sent again replaces itself) and records the trace again, so the mapping
  sees every span. Writes are serialised within the process, so two requests carrying spans of
  one trace cannot lose each other's spans; processes sharing one store do not coordinate.
- **Limits.** A body, and a gzip or deflate body once decompressed, of at most
  :data:`MAX_BODY_BYTES`; more is ``413``.
- **An edition's own route.** :func:`export` is the endpoint after the principal: an edition that
  signs exporters in differently from its other routes serves a route of its own, with
  :class:`OtlpRoute`, and calls it with the principal and store it chose, its own mapping from
  OpenTelemetry trace ids to the store's (:data:`TraceIds`) and its own body limit. Martian Cloud
  does, to keep one organisation's ids apart from another's.
- **Answers.** ``200`` with an ``ExportTraceServiceResponse`` in the request's encoding. Spans the
  core cannot take (an invalid trace or span id, or a trace the store refuses) are rejected and
  the rest written: the response's ``partial_success`` counts them and says why, as OTLP defines.
  A refused request is answered with its status code and a ``google.rpc.Status`` body in the
  request's encoding (only its ``message``).
- **Not kept.** Resource and scope attributes, span events and links: the shared model has no
  place for them. Span attributes are kept as they arrive.

The protobuf messages are ``opentelemetry-proto``'s, which ``mlflow-skinny`` already installs.
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import uuid
import zlib
from collections.abc import Callable, Coroutine, Iterator
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.routing import APIRoute
from google.protobuf import json_format  # type: ignore[import-untyped]  # protobuf ships no types
from google.protobuf.message import DecodeError  # type: ignore[import-untyped]
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)
from opentelemetry.proto.common.v1.common_pb2 import AnyValue
from opentelemetry.proto.trace.v1.trace_pb2 import Span as OtlpSpan
from opentelemetry.proto.trace.v1.trace_pb2 import Status as OtlpStatus

from martian.evals._digits import ascii_digits
from martian.evals.model import Span, Trace, TraceState
from martian.evals.server.capabilities import WRITERS, highest_role
from martian.evals.server.dependencies import PrincipalDep, StoreDep
from martian.evals.server.principal import Principal
from martian.evals.server.runs import writable_project
from martian.evals.store import EvalStore, StoreError
from martian.evals.trace_conventions import MAPPING_KEY

TRACES_PATH = "/v1/traces"
PROJECT_HEADER = "x-martian-project-id"
"""The request header naming the project the traces go to, a project id; without it, the default project."""

MAX_BODY_BYTES = 16 * 1024 * 1024
"""The largest body the endpoint reads, 16 MiB, before and after decompression."""

TraceIds = Callable[[Principal, str, bytes], str]
"""How an exported trace's store id is made, from the principal, the store's key of the project the
traces go to and the OpenTelemetry trace id (16 bytes). The id must be one the store takes; the
same arguments must give the same id, so that a trace's spans sent in several requests, or sent
again, make one trace."""

PROTOBUF = "application/x-protobuf"
JSON = "application/json"
_INFLATE = {"gzip": 16 + zlib.MAX_WBITS, "deflate": zlib.MAX_WBITS}
_SPAN_STATUS: dict[int, TraceState] = {OtlpStatus.STATUS_CODE_OK: "ok", OtlpStatus.STATUS_CODE_ERROR: "error"}
_ID_FIELDS = ("traceId", "trace_id", "spanId", "span_id", "parentSpanId", "parent_span_id")
_REASONS_SHOWN = 5


def _encoding(request: Request) -> str:
    return (request.headers.get("content-type") or "").split(";")[0].strip().lower()


def _varint(value: int) -> bytes:
    out = bytearray()
    while value > 0x7F:
        out.append(value & 0x7F | 0x80)
        value >>= 7
    return bytes([*out, value])


def _status(request: Request, error: HTTPException) -> Response:
    """A refusal as OTLP answers one: a ``google.rpc.Status`` with the message, in the request's
    encoding. The message is field 2; the code is left out, as OTLP allows."""
    message = str(error.detail)
    if _encoding(request) == JSON:
        return Response(json.dumps({"message": message}), error.status_code, error.headers, media_type=JSON)
    text = message.encode("utf-8")
    return Response(b"\x12" + _varint(len(text)) + text, error.status_code, error.headers, media_type=PROTOBUF)


class OtlpRoute(APIRoute):
    """A route whose refusals, the principal provider's among them, are OTLP's ``Status``."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handle = super().get_route_handler()

        async def answer(request: Request) -> Response:
            try:
                return await handle(request)
            except HTTPException as error:
                return _status(request, error)

        return answer


router = APIRouter(tags=["OpenTelemetry"], route_class=OtlpRoute)


def mlflow_trace_id(_principal: Principal, _project: str, trace_id: bytes) -> str:
    """The core's :data:`TraceIds`: ``tr-`` and the OpenTelemetry trace id in hex, MLflow's form."""
    return "tr-" + trace_id.hex()


async def _body(request: Request, limit: int) -> bytes:
    """The request's body, decompressed, refused past ``limit`` bytes."""
    declared = request.headers.get("content-length", "")
    if ascii_digits(declared) and int(declared) > limit:
        raise HTTPException(413, f"The body is larger than {limit} bytes")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise HTTPException(413, f"The body is larger than {limit} bytes")
        chunks.append(chunk)
    body = b"".join(chunks)
    coding = (request.headers.get("content-encoding") or "identity").strip().lower()
    if coding == "identity":
        return body
    if coding not in _INFLATE:
        raise HTTPException(415, f"Content-Encoding {coding} is not supported; send gzip, deflate or none")
    inflater = zlib.decompressobj(_INFLATE[coding])
    try:
        found = inflater.decompress(body, limit + 1)
    except zlib.error:
        raise HTTPException(400, f"The body is not valid {coding}") from None
    if len(found) > limit:
        raise HTTPException(413, f"The decompressed body is larger than {limit} bytes")
    if not inflater.eof:
        raise HTTPException(400, f"The body is not valid {coding}")
    return found


def _hex_ids(found: Any) -> None:
    """OTLP/JSON's hex trace and span ids as base64, protobuf's JSON form of bytes, in place."""
    resources = found.get("resourceSpans", found.get("resource_spans")) if isinstance(found, dict) else None
    for resource in resources if isinstance(resources, list) else []:
        scopes = resource.get("scopeSpans", resource.get("scope_spans")) if isinstance(resource, dict) else None
        for scope in scopes if isinstance(scopes, list) else []:
            spans = scope.get("spans") if isinstance(scope, dict) else None
            for span in spans if isinstance(spans, list) else []:
                links = span.get("links") if isinstance(span, dict) else None
                for item in [span, *(links if isinstance(links, list) else [])]:
                    for name in _ID_FIELDS:
                        if isinstance(item, dict) and isinstance(value := item.get(name), str):
                            item[name] = base64.b64encode(bytes.fromhex(value)).decode("ascii")


def _decode(body: bytes, encoding: str) -> ExportTraceServiceRequest:
    request = ExportTraceServiceRequest()
    try:
        if encoding == JSON:
            found = json.loads(body)
            _hex_ids(found)
            json_format.ParseDict(found, request, ignore_unknown_fields=True)
        else:
            request.ParseFromString(body)
    except (ValueError, DecodeError, json_format.Error):
        raise HTTPException(400, f"The body is not an OTLP ExportTraceServiceRequest in {encoding}") from None
    return request


def _value(value: AnyValue) -> Any:
    """An attribute's value as JSON: bytes as base64 text, and a non-finite number as its text."""
    kind = value.WhichOneof("value")
    if kind is None:
        return None
    if kind == "array_value":
        return [_value(item) for item in value.array_value.values]
    if kind == "kvlist_value":
        return {item.key: _value(item.value) for item in value.kvlist_value.values}
    if kind == "bytes_value":
        return base64.b64encode(value.bytes_value).decode("ascii")
    found = getattr(value, kind)
    return str(found) if isinstance(found, float) and not math.isfinite(found) else found


def _span(otlp: OtlpSpan) -> Span:
    return Span(
        id=otlp.span_id.hex(),
        parent_id=otlp.parent_span_id.hex() or None,
        name=otlp.name,
        status=_SPAN_STATUS.get(otlp.status.code, "unknown"),
        start_ms=otlp.start_time_unix_nano / 1e6 if otlp.start_time_unix_nano else None,
        end_ms=otlp.end_time_unix_nano / 1e6 if otlp.end_time_unix_nano else None,
        attributes={item.key: _value(item.value) for item in otlp.attributes},
    )


def _spans(request: ExportTraceServiceRequest) -> Iterator[OtlpSpan]:
    for resource in request.resource_spans:
        for scope in resource.scope_spans:
            yield from scope.spans


def _valid(otlp: OtlpSpan) -> bool:
    parent = otlp.parent_span_id
    return (
        len(otlp.trace_id) == 16
        and any(otlp.trace_id)
        and len(otlp.span_id) == 8
        and any(otlp.span_id)
        and (not parent or (len(parent) == 8 and any(parent)))
    )


def _merged(trace_id: str, project: str, spans: list[Span], earlier: Trace | None) -> Trace:
    """The trace of ``spans`` and the spans ``earlier`` already holds, a span sent again replacing
    itself. The run, tags and metadata stay as ``earlier`` has them; the rest comes from the spans,
    mapped again with the store's ``record_trace``."""
    found = {span.id: span for span in earlier.spans} if earlier else {}
    found.update((span.id, span) for span in spans)
    ordered = sorted(found.values(), key=lambda span: (span.start_ms is None, span.start_ms or 0.0))
    root = next((span for span in ordered if not span.parent_id), None)
    start = root.start_ms if root else next((s.start_ms for s in ordered if s.start_ms is not None), None)
    latency = None
    if root and root.start_ms is not None and root.end_ms is not None:
        latency = max(0.0, root.end_ms - root.start_ms)
    state: TraceState = "in_progress" if root is None else "error" if root.status == "error" else "ok"
    return Trace(
        id=trace_id,
        project_id=project,
        name=root.name if root else "",
        state=state,
        request_time=datetime.fromtimestamp(start / 1000, UTC) if start is not None else None,
        latency_ms=latency,
        run_id=earlier.run_id if earlier else None,
        tags=dict(earlier.tags) if earlier else {},
        metadata={k: v for k, v in earlier.metadata.items() if k != MAPPING_KEY} if earlier else {},
        spans=ordered,
    )


async def _write(store: EvalStore, principal: Principal, project: str, trace_id: str, spans: list[Span]) -> None:
    earlier = await store.get_trace(principal, project, trace_id)
    await store.record_trace(principal, _merged(trace_id, project, spans, earlier))


def _answer(encoding: str, rejected: int, reasons: list[str]) -> Response:
    response = ExportTraceServiceResponse()
    if rejected:
        shown = reasons[:_REASONS_SHOWN] + (
            [f"and {len(reasons) - _REASONS_SHOWN} more"] if len(reasons) > _REASONS_SHOWN else []
        )
        response.partial_success.rejected_spans = rejected
        response.partial_success.error_message = "; ".join(shown)
    if encoding == JSON:
        return Response(json.dumps(json_format.MessageToDict(response)), media_type=JSON)
    return Response(response.SerializeToString(), media_type=PROTOBUF)


@router.post(TRACES_PATH, response_model=None)
async def export_traces(request: Request, principal: PrincipalDep, store: StoreDep) -> Response:
    return await export(request, principal, store)


async def export(
    request: Request,
    principal: Principal,
    store: EvalStore,
    *,
    trace_ids: TraceIds = mlflow_trace_id,
    max_body_bytes: int | None = None,
) -> Response:
    """Record the request's spans as ``principal``'s traces in ``store``, and answer as OTLP does:
    everything ``POST /v1/traces`` does once its principal and store are known, the role check
    first. ``trace_ids`` makes each trace's store id; ``max_body_bytes`` is the largest body, as
    sent and decompressed, :data:`MAX_BODY_BYTES` when not given. The request's application must
    hold the trace write lock ``create_app`` sets (``state.martian_evals_trace_writes``)."""
    if highest_role(principal.roles) not in WRITERS:
        raise HTTPException(403, "Requires member role")
    encoding = _encoding(request)
    if encoding not in (PROTOBUF, JSON):
        raise HTTPException(415, f"Send OTLP as {PROTOBUF} or {JSON}")
    named = request.headers.get(PROJECT_HEADER)
    try:
        project_id = uuid.UUID(named) if named else None
    except ValueError:
        raise HTTPException(400, f"{PROJECT_HEADER} must be a project id") from None
    body = await _body(request, MAX_BODY_BYTES if max_body_bytes is None else max_body_bytes)
    exported = _decode(body, encoding)
    project = await writable_project(store, principal, project_id)

    traces: dict[str, list[Span]] = {}
    rejected, invalid, reasons = 0, 0, list[str]()
    for otlp in _spans(exported):
        if not _valid(otlp):
            invalid += 1
            continue
        traces.setdefault(trace_ids(principal, project, otlp.trace_id), []).append(_span(otlp))
    if invalid:
        rejected += invalid
        reasons.append(f"{invalid} span(s) without a valid trace id, span id or parent span id")
    lock: asyncio.Lock = request.app.state.martian_evals_trace_writes
    for trace_id, spans in traces.items():
        try:
            async with lock:
                await _write(store, principal, project, trace_id, spans)
        except StoreError as error:
            rejected += len(spans)
            reasons.append(f"{len(spans)} span(s) of trace {trace_id}: {error.detail}")
    return _answer(encoding, rejected, reasons)
