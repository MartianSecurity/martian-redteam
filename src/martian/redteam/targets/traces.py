"""Tool-call evidence from traces: the tool spans an agent already records, read as executed calls.

Many agents trace themselves, with MLflow or with OpenTelemetry. A tool span is a call that ran, so it
is evidence of an executed call that needs no change to the agent. The spans are read through the
evals core's trace mapping (:func:`martian.evals.trace_conventions.map_span`), which knows MLflow's
span types, the OpenTelemetry GenAI conventions (``gen_ai.operation.name: execute_tool``,
``gen_ai.tool.name``, ``gen_ai.tool.call.arguments``) and OpenInference. Spans inside a tool span are
the tool's own work and are not read.

The OpenTelemetry GenAI conventions make a tool call's arguments opt-in, so a span may name its tool
and nothing else. Such a call is kept as name-only evidence: its :class:`TracedCall` says
``arguments_captured`` is false, and its evidence says so too. Every call is marked with where it
came from: the trace's style (``otel_genai``, ``openinference`` or ``mlflow``) and its trace and
span ids.

Three routes reach an attempt's spans:

- a function target that runs with ``trace=True``
  (:class:`~martian.redteam.targets.function.FunctionTarget`): its process records each call's MLflow
  spans and hands them back with the reply, as a suite file's function target does;
- :class:`TracedTarget` with a :class:`TraceSource`: after each message, the traces of the reply's
  session that started since. :class:`StoreTraces` reads them from the evals core's trace store,
  which the local edition's OpenTelemetry endpoint (``POST /v1/traces``) writes; the agent puts the
  conversation's id in ``gen_ai.conversation.id`` (or OpenInference's ``session.id``);
- :func:`calls_in_trace` and :func:`calls_in_mlflow` for traces the caller already has.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, Protocol

from martian.evals.model import Span, Trace, TraceQuery
from martian.evals.trace_conventions import map_span
from martian.redteam.model import Evidence, ToolCall
from martian.redteam.targets import Target, TargetReply
from martian.redteam.targets.reply import reply_text, with_observed

if TYPE_CHECKING:
    from martian.evals.server.principal import Principal
    from martian.evals.store import EvalStore

Style = Literal["otel_genai", "openinference", "mlflow"]
RESULT_LIMIT = 4_000
_MERGED_TOOLS = "(merged tools)"


@dataclass(frozen=True)
class TracedCall:
    """One executed tool call a trace records."""

    call: ToolCall
    style: Style
    arguments_captured: bool
    trace_id: str
    span_id: str

    @property
    def source(self) -> str:
        return f"trace:{self.style}"

    def evidence(self) -> Evidence:
        return Evidence(
            id=f"trace-{self.trace_id}-{self.span_id}",
            kind="tool_call",
            tier="tool",
            source=self.source,
            metadata={
                "tool": self.call.name,
                "call_id": self.call.call_id,
                "executed": True,
                "arguments_captured": self.arguments_captured,
                "trace_id": self.trace_id,
                "span_id": self.span_id,
            },
        )


def _style(attributes: Mapping[str, Any]) -> Style:
    if any(key.startswith("gen_ai.") for key in attributes):
        return "otel_genai"
    if "openinference.span.kind" in attributes or any(key.startswith("tool.") for key in attributes):
        return "openinference"
    return "mlflow"


def _arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {"input": value}
    return value if isinstance(value, dict) else {"input": value}


def calls_in_spans(spans: Sequence[Span], *, trace_id: str) -> list[TracedCall]:
    """The executed tool calls of a trace's ``spans``, in the order they started."""
    mapped = {span.id: map_span(span) for span in spans}
    by_id = {span.id: span for span in spans}

    def in_tool(span: Span) -> bool:
        seen, parent = set(), span.parent_id
        while parent is not None and parent in by_id and parent not in seen:
            seen.add(parent)
            if mapped[parent].kind == "TOOL":
                return True
            parent = by_id[parent].parent_id
        return False

    found = []
    order = sorted(enumerate(spans), key=lambda pair: (pair[1].start_ms is None, pair[1].start_ms or 0.0, pair[0]))
    for _, span in order:
        fields = mapped[span.id]
        if fields.kind != "TOOL" or in_tool(span) or fields.tool is None or fields.tool.name == _MERGED_TOOLS:
            continue
        captured = fields.tool.arguments is not None
        outputs = fields.outputs
        found.append(
            TracedCall(
                call=ToolCall(
                    name=fields.tool.name,
                    arguments=_arguments(fields.tool.arguments) if captured else {},
                    call_id=fields.tool.id or f"trace-{span.id}",
                    executed=True,
                    result=None if outputs is None else reply_text(outputs)[:RESULT_LIMIT],
                ),
                style=_style(span.attributes),
                arguments_captured=captured,
                trace_id=trace_id,
                span_id=span.id,
            )
        )
    return found


def calls_in_trace(trace: Trace) -> list[TracedCall]:
    """The executed tool calls of a trace of the evals core's model, such as one its store holds."""
    return calls_in_spans(trace.spans, trace_id=trace.id)


def calls_in_mlflow(trace: Any) -> list[TracedCall]:
    """The executed tool calls of an MLflow trace: an ``mlflow.entities.Trace``, or its dictionary
    form, as a function target's process hands it back."""
    from mlflow.entities import Trace as MlflowTrace

    found = MlflowTrace.from_dict(dict(trace)) if isinstance(trace, Mapping) else trace
    spans = [
        Span(
            id=str(span.span_id),
            parent_id=None if span.parent_id is None else str(span.parent_id),
            name=span.name or "",
            kind=str(span.span_type or ""),
            start_ms=None if span.start_time_ns is None else span.start_time_ns / 1e6,
            inputs=span.inputs,
            outputs=span.outputs,
            attributes=dict(span.attributes or {}),
        )
        for span in found.data.spans
    ]
    return calls_in_spans(spans, trace_id=str(found.info.trace_id))


def with_traced(reply: TargetReply, calls: Sequence[TracedCall]) -> TargetReply:
    """``reply`` with the calls the traces recorded, and their evidence."""
    if not calls:
        return reply
    return with_observed(
        reply,
        [traced.call for traced in calls],
        [traced.evidence() for traced in calls],
        captured=[traced.arguments_captured for traced in calls],
    )


class TraceSource(Protocol):
    """Where :class:`TracedTarget` finds the traces of a session."""

    async def traces(self, *, session: str, since: datetime) -> list[Trace]:
        """The traces of ``session`` that started at ``since`` or later, with their spans."""
        ...


class StoreTraces:
    """Traces in the evals core's trace store, by the session they belong to. ``slack`` widens the
    window back in time, for clocks that differ a little between the agent and the harness."""

    def __init__(
        self,
        store: EvalStore,
        principal: Principal,
        *,
        project_id: str = "default",
        slack: timedelta = timedelta(seconds=1),
    ) -> None:
        self._store = store
        self._principal = principal
        self._project = project_id
        self._slack = slack

    async def traces(self, *, session: str, since: datetime) -> list[Trace]:
        query = TraceQuery(
            project_id=self._project, session_id=session, since=since - self._slack, oldest_first=True, limit=500
        )
        page = await self._store.search_traces(self._principal, query)
        found = []
        for item in page.items:
            trace = await self._store.get_trace(self._principal, self._project, item.id)
            if trace is not None:
                found.append(trace)
        return found


class TracedTarget:
    """``target`` with the tool calls its traces record added to each reply: the calls of the
    traces of the reply's session that started since the message was sent. ``settle_seconds`` waits
    that long after the reply first, since exporters send spans in batches."""

    def __init__(self, target: Target, source: TraceSource, *, settle_seconds: float = 0.0) -> None:
        self._target = target
        self._source = source
        self._settle = settle_seconds
        self._read: set[tuple[str, str]] = set()

    @property
    def kind(self) -> str:
        return self._target.kind

    async def send(self, message: str, *, session: str | None = None) -> TargetReply:
        import asyncio

        since = datetime.now(UTC)
        started = time.monotonic()
        reply = await self._target.send(message, session=session)
        if reply.session is None:
            return reply
        if self._settle:
            await asyncio.sleep(max(0.0, self._settle - (time.monotonic() - started)))
        calls = []
        for trace in await self._source.traces(session=reply.session, since=since):
            for traced in calls_in_trace(trace):
                key = (traced.trace_id, traced.span_id)
                if key not in self._read:
                    self._read.add(key)
                    calls.append(traced)
        return with_traced(reply, calls)

    async def close(self) -> None:
        await self._target.close()


__all__ = [
    "StoreTraces",
    "TraceSource",
    "TracedCall",
    "TracedTarget",
    "calls_in_mlflow",
    "calls_in_spans",
    "calls_in_trace",
    "with_traced",
]
