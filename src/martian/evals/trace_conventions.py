"""The mapping of GenAI trace conventions onto the shared model's trace fields.

Applications describe a model call, a tool call or an agent step in span attributes, in one of
three styles: the OpenTelemetry GenAI semantic conventions, OpenInference, or MLflow's own span
attributes. One versioned table maps all three onto the fields the core reads: a span's kind,
model, provider, token counts, inputs, outputs and tool calls, and the session and user a trace
belongs to.

- A table is data. Each rule names one of our fields, a style, an attribute key and how its
  value is read. A field's rules are tried in order and the first that gives a value wins.
- A released table never changes. A convention change is a new table with a new version, added
  to ``TABLES``; ``MAPPING_VERSION`` is the newest. Traces keep the version they were mapped
  with, so a new table never changes what an older trace shows.
- ``map_attributes`` is a pure function over one span's attributes. ``map_span`` adds what the
  span's own fields say, and ``map_trace`` fills a trace's unset fields from its spans and
  records the version in its metadata under ``MAPPING_KEY``.
- Nothing is dropped. The mapping never removes or rewrites an attribute, so one that no rule
  names stays on its span.

``docs/evals-core.md`` (trace conventions) has the table in prose.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from martian.evals.model import Span, Trace

Style = Literal["otel_genai", "openinference", "mlflow"]
Field = Literal[
    "kind",
    "model",
    "provider",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "inputs",
    "outputs",
    "tool_name",
    "tool_call_id",
    "tool_calls",
    "session_id",
    "user_id",
]
Read = Literal["text", "int", "json", "kind", "oi_value", "oi_messages", "tool_calls"]

MAPPING_KEY = "martian.trace_mapping"
"""The trace metadata key that names the mapping version a trace's fields were filled with."""

STYLES: tuple[Style, ...] = ("otel_genai", "openinference", "mlflow")


@dataclass(frozen=True)
class Rule:
    """One row of a table: ``field`` is read from the attribute ``key`` of style ``style``.

    ``read`` says how the value is read: ``text`` as text (decoded when it is a JSON-encoded
    string, as MLflow writes every attribute value), ``int`` as a count, ``json`` decoded when it
    is JSON text, ``kind`` through the table's span kinds, ``oi_value`` with OpenInference's
    sibling ``.mime_type``, ``oi_messages`` rebuilt from OpenInference's flattened message
    attributes, and ``tool_calls`` as the tool calls in the style's output messages. ``part``
    names a key inside a value that is an object."""

    field: Field
    style: Style
    key: str
    read: Read
    part: str = ""


@dataclass(frozen=True)
class MappingTable:
    """One version of the mapping. ``conventions`` names the release of each style it follows,
    and ``kinds`` maps each style's span kind values onto ours (MLflow's span types)."""

    version: str
    conventions: Mapping[Style, str]
    rules: tuple[Rule, ...]
    kinds: Mapping[Style, Mapping[str, str]]


class ToolCall(BaseModel):
    """A call to a tool: the one a tool span executes, or one a model asked for."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str | None = None
    name: str
    arguments: Any = None


class SpanFields(BaseModel):
    """What the mapping reads from one span. ``None`` is a field no rule found. ``total_tokens``
    is the reported total, or the input and output counts added when only those are reported.
    ``tool`` is the call a tool span executes; ``tool_calls`` are the calls a model span asked
    for."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str | None = None
    model: str | None = None
    provider: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    inputs: Any = None
    outputs: Any = None
    tool: ToolCall | None = None
    tool_calls: list[ToolCall] = []
    session_id: str | None = None
    user_id: str | None = None

    def token_usage(self) -> dict[str, int] | None:
        """The span's token counts in MLflow's ``mlflow.chat.tokenUsage`` form, or ``None``."""
        usage = {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
        }
        found = {key: value for key, value in usage.items() if value is not None}
        return found or None


def _rule(field: Field, style: Style, key: str, read: Read, part: str = "") -> Rule:
    return Rule(field=field, style=style, key=key, read=read, part=part)


_MLFLOW_SPAN_TYPES = (
    "LLM",
    "CHAT_MODEL",
    "CHAIN",
    "AGENT",
    "TOOL",
    "RETRIEVER",
    "PARSER",
    "EMBEDDING",
    "RERANKER",
    "MEMORY",
    "WORKFLOW",
    "TASK",
    "GUARDRAIL",
    "EVALUATOR",
)

V1 = MappingTable(
    version="martian-trace-mapping-v1",
    conventions={
        "otel_genai": "OpenTelemetry semantic conventions 1.41.0, gen_ai.* (the last release before the GenAI "
        "conventions moved to open-telemetry/semantic-conventions-genai)",
        "openinference": "OpenInference semantic conventions 0.1.45 (openinference-semantic-conventions)",
        "mlflow": "MLflow 3.18 span attributes (mlflow.*)",
    },
    rules=(
        _rule("kind", "otel_genai", "gen_ai.operation.name", "kind"),
        _rule("kind", "openinference", "openinference.span.kind", "kind"),
        _rule("kind", "mlflow", "mlflow.spanType", "kind"),
        _rule("model", "otel_genai", "gen_ai.response.model", "text"),
        _rule("model", "otel_genai", "gen_ai.request.model", "text"),
        _rule("model", "openinference", "llm.response.model_name", "text"),
        _rule("model", "openinference", "llm.model_name", "text"),
        _rule("model", "openinference", "llm.request.model_name", "text"),
        _rule("model", "openinference", "embedding.model_name", "text"),
        _rule("model", "mlflow", "mlflow.llm.model", "text"),
        _rule("provider", "otel_genai", "gen_ai.provider.name", "text"),
        _rule("provider", "otel_genai", "gen_ai.system", "text"),  # before 1.37.0
        _rule("provider", "openinference", "llm.provider", "text"),
        _rule("provider", "openinference", "llm.system", "text"),
        _rule("provider", "mlflow", "mlflow.llm.provider", "text"),
        _rule("input_tokens", "otel_genai", "gen_ai.usage.input_tokens", "int"),
        _rule("input_tokens", "otel_genai", "gen_ai.usage.prompt_tokens", "int"),  # before 1.27.0
        _rule("input_tokens", "openinference", "llm.token_count.prompt", "int"),
        _rule("input_tokens", "mlflow", "mlflow.chat.tokenUsage", "int", part="input_tokens"),
        _rule("output_tokens", "otel_genai", "gen_ai.usage.output_tokens", "int"),
        _rule("output_tokens", "otel_genai", "gen_ai.usage.completion_tokens", "int"),  # before 1.27.0
        _rule("output_tokens", "openinference", "llm.token_count.completion", "int"),
        _rule("output_tokens", "mlflow", "mlflow.chat.tokenUsage", "int", part="output_tokens"),
        _rule("total_tokens", "openinference", "llm.token_count.total", "int"),
        _rule("total_tokens", "mlflow", "mlflow.chat.tokenUsage", "int", part="total_tokens"),
        _rule("inputs", "otel_genai", "gen_ai.input.messages", "json"),
        _rule("inputs", "otel_genai", "gen_ai.tool.call.arguments", "json"),
        _rule("inputs", "otel_genai", "gen_ai.retrieval.query.text", "text"),
        _rule("inputs", "openinference", "input.value", "oi_value"),
        _rule("inputs", "openinference", "llm.input_messages", "oi_messages"),
        _rule("inputs", "mlflow", "mlflow.spanInputs", "json"),
        _rule("outputs", "otel_genai", "gen_ai.output.messages", "json"),
        _rule("outputs", "otel_genai", "gen_ai.tool.call.result", "json"),
        _rule("outputs", "otel_genai", "gen_ai.retrieval.documents", "json"),
        _rule("outputs", "openinference", "output.value", "oi_value"),
        _rule("outputs", "openinference", "llm.output_messages", "oi_messages"),
        _rule("outputs", "mlflow", "mlflow.spanOutputs", "json"),
        _rule("tool_name", "otel_genai", "gen_ai.tool.name", "text"),
        _rule("tool_name", "openinference", "tool.name", "text"),
        _rule("tool_call_id", "otel_genai", "gen_ai.tool.call.id", "text"),
        _rule("tool_call_id", "openinference", "tool.id", "text"),
        _rule("tool_calls", "otel_genai", "gen_ai.output.messages", "tool_calls"),
        _rule("tool_calls", "openinference", "llm.output_messages", "tool_calls"),
        _rule("tool_calls", "mlflow", "mlflow.spanOutputs", "tool_calls"),
        _rule("session_id", "otel_genai", "gen_ai.conversation.id", "text"),
        # Also OpenTelemetry's general session.id and user.id, which MLflow writes on a root span it exports.
        _rule("session_id", "openinference", "session.id", "text"),
        _rule("user_id", "openinference", "user.id", "text"),
    ),
    kinds={
        "otel_genai": {
            "chat": "CHAT_MODEL",
            "text_completion": "LLM",
            "generate_content": "LLM",
            "embeddings": "EMBEDDING",
            "retrieval": "RETRIEVER",
            "execute_tool": "TOOL",
            "create_agent": "AGENT",
            "invoke_agent": "AGENT",
            "invoke_workflow": "WORKFLOW",
        },
        "openinference": {
            kind: kind
            for kind in (
                "LLM",
                "CHAIN",
                "TOOL",
                "RETRIEVER",
                "EMBEDDING",
                "AGENT",
                "RERANKER",
                "GUARDRAIL",
                "EVALUATOR",
            )
        },
        "mlflow": {kind: kind for kind in _MLFLOW_SPAN_TYPES},
    },
)

TABLES: Mapping[str, MappingTable] = {V1.version: V1}
"""Every released table by version. A table stays here once released, for the traces mapped with it."""

MAPPING_VERSION = V1.version
"""The newest table's version: the one a trace without a recorded version is mapped with."""


def mapping_table(version: str) -> MappingTable:
    """The table of a version. A version this release does not have is ``ValueError``."""
    try:
        return TABLES[version]
    except KeyError:
        raise ValueError(f"trace mapping {version!r} is not one this release knows") from None


# --- reading values --------------------------------------------------------------------------


def _decoded(value: Any) -> Any:
    """A JSON-encoded value decoded; any other value as it is."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _text(value: Any) -> str | None:
    if isinstance(value, str) and value.startswith('"'):
        value = _decoded(value)
    return value if isinstance(value, str) and value.strip() else None


def _count(value: Any) -> int | None:
    found = _decoded(value) if isinstance(value, str) else value
    if isinstance(found, str) and found.strip().isdigit():
        found = int(found)
    if isinstance(found, bool) or not isinstance(found, int | float):
        return None
    if found < 0 or found != int(found):
        return None
    return int(found)


def _unflatten(attributes: Mapping[str, Any], key: str) -> Any:
    """OpenInference's flattened attributes under ``key`` (``key.0.message.role``) as the value
    they flatten, with numbered levels as lists; the attribute itself when it is not flattened."""
    if (whole := attributes.get(key)) is not None:
        return _decoded(whole)
    start = key + "."
    tree: dict[str, Any] = {}
    for name in sorted(k for k in attributes if k.startswith(start)):
        *path, leaf = name[len(start) :].split(".")
        node: Any = tree
        for part in path:
            node = node.setdefault(part, {}) if isinstance(node, dict) else None
        if isinstance(node, dict) and leaf not in node:
            node[leaf] = attributes[name]
    return _listed(tree) if tree else None


def _listed(node: Any) -> Any:
    if not isinstance(node, dict):
        return node
    if node and all(key.isdigit() for key in node):
        return [_listed(node[key]) for key in sorted(node, key=int)]
    return {key: _listed(value) for key, value in node.items()}


def _oi_messages(attributes: Mapping[str, Any], key: str) -> list[dict[str, Any]] | None:
    """OpenInference messages, each one's ``message`` and its tool calls' ``tool_call`` unwrapped."""
    found = _unflatten(attributes, key)
    if not isinstance(found, list):
        return None
    messages = []
    for entry in found:
        message = entry.get("message", entry) if isinstance(entry, dict) else entry
        if isinstance(message, dict) and isinstance(message.get("tool_calls"), list):
            calls = [c.get("tool_call", c) if isinstance(c, dict) else c for c in message["tool_calls"]]
            message = {**message, "tool_calls": calls}
        messages.append(message)
    return messages


def _items(value: Any, key: str) -> list[Any]:
    found = value.get(key) if isinstance(value, dict) else None
    return found if isinstance(found, list) else []


def _call(found: Any) -> ToolCall | None:
    if not isinstance(found, dict):
        return None
    if isinstance(function := found.get("function"), dict):  # OpenAI's and OpenInference's form
        found = {**found, **function}
    if not _text(name := found.get("name")):
        return None
    call_id = found.get("id")
    return ToolCall(
        id=call_id if isinstance(call_id, str) else None, name=name, arguments=_decoded(found.get("arguments"))
    )


def _tool_calls(style: Style, attributes: Mapping[str, Any], key: str) -> list[ToolCall] | None:
    """The tool calls in a style's output messages: OpenTelemetry's ``tool_call`` parts,
    OpenInference's ``message.tool_calls``, and in MLflow's outputs OpenAI's chat format
    (``choices[].message.tool_calls``, or a message or list of messages with ``tool_calls``)."""
    if style == "otel_genai":
        messages = _decoded(attributes.get(key))
        parts = [p for m in messages for p in _items(m, "parts")] if isinstance(messages, list) else []
        calls = [p for p in parts if isinstance(p, dict) and p.get("type") == "tool_call"]
    else:
        if style == "openinference":
            messages = _oi_messages(attributes, key) or []
        elif choices := _items(outputs := _decoded(attributes.get(key)), "choices"):
            messages = [choice.get("message") for choice in choices if isinstance(choice, dict)]
        else:
            messages = outputs if isinstance(outputs, list) else [outputs]
        calls = [call for message in messages for call in _items(message, "tool_calls")]
    return [found for call in calls if (found := _call(call)) is not None] or None


def _kind(table: MappingTable, style: Style, value: Any) -> str | None:
    value = _decoded(value)
    return table.kinds[style].get(value) if isinstance(value, str) else None


def _read(rule: Rule, table: MappingTable, attributes: Mapping[str, Any]) -> Any:
    if rule.read == "oi_messages":
        return _oi_messages(attributes, rule.key)
    if rule.read == "tool_calls":
        return _tool_calls(rule.style, attributes, rule.key)
    value = attributes.get(rule.key)
    if rule.part:
        value = _decoded(value)
        value = value.get(rule.part) if isinstance(value, dict) else None
    if value is None:
        return None
    if rule.read == "text":
        return _text(value)
    if rule.read == "int":
        return _count(value)
    if rule.read == "kind":
        return _kind(table, rule.style, value)
    if rule.read == "oi_value":
        json_value = attributes.get(rule.key.removesuffix(".value") + ".mime_type") == "application/json"
        return _decoded(value) if json_value else value
    return _decoded(value)


# --- the mapping -----------------------------------------------------------------------------


def map_attributes(attributes: Mapping[str, Any], *, version: str = MAPPING_VERSION) -> SpanFields:
    """The trace fields one span's attributes give under a mapping version. Pure: it reads the
    attributes only, and an attribute no rule names is ignored, never removed."""
    table = mapping_table(version)
    found: dict[str, Any] = {}
    for rule in table.rules:
        if rule.field in found:
            continue
        value = _read(rule, table, attributes)
        if value is None or value == "" or value == []:
            continue
        found[rule.field] = value
    inputs, outputs = found.get("input_tokens"), found.get("output_tokens")
    total = found.get("total_tokens")
    if total is None and (inputs is not None or outputs is not None):
        total = (inputs or 0) + (outputs or 0)
    name = found.get("tool_name")
    return SpanFields(
        kind=found.get("kind"),
        model=found.get("model"),
        provider=found.get("provider"),
        input_tokens=inputs,
        output_tokens=outputs,
        total_tokens=total,
        inputs=found.get("inputs"),
        outputs=found.get("outputs"),
        tool=ToolCall(id=found.get("tool_call_id"), name=name, arguments=found.get("inputs")) if name else None,
        tool_calls=found.get("tool_calls", []),
        session_id=found.get("session_id"),
        user_id=found.get("user_id"),
    )


def map_span(span: Span, *, version: str = MAPPING_VERSION) -> SpanFields:
    """The trace fields of a span: its attributes, with its own kind, inputs and outputs read as
    MLflow's span type, inputs and outputs, which is how MLflow keeps them. The span's own
    fields win over its attributes; a kind of ``UNKNOWN`` counts as unset. A tool span with no
    tool attributes executes the tool its name names."""
    own: dict[str, Any] = {}
    if span.kind and span.kind != "UNKNOWN":
        own["mlflow.spanType"] = span.kind
    if span.inputs is not None:
        own["mlflow.spanInputs"] = json.dumps(span.inputs, default=str)
    if span.outputs is not None:
        own["mlflow.spanOutputs"] = json.dumps(span.outputs, default=str)
    mapped = map_attributes({**span.attributes, **own}, version=version)
    update: dict[str, Any] = {}
    if span.kind and span.kind != "UNKNOWN":
        update["kind"] = span.kind
    if span.inputs is not None:
        update["inputs"] = span.inputs
    if span.outputs is not None:
        update["outputs"] = span.outputs
    inputs = update.get("inputs", mapped.inputs)
    if mapped.tool is not None:
        update["tool"] = mapped.tool.model_copy(update={"arguments": inputs})
    elif update.get("kind", mapped.kind) == "TOOL" and span.name:
        update["tool"] = ToolCall(name=span.name, arguments=inputs)
    return mapped.model_copy(update=update)


def _total_tokens(spans: list[Span], mapped: list[SpanFields]) -> int | None:
    """The trace's tokens: each span's total, leaving out a span whose ancestor reports a total,
    because a parent that reports usage reports its children's too (as MLflow adds them up)."""
    index = {span.id: i for i, span in enumerate(spans)}
    total, reported = 0, False
    for i, span in enumerate(spans):
        if (tokens := mapped[i].total_tokens) is None:
            continue
        seen, parent, above = {span.id}, span.parent_id, False
        while parent in index and parent not in seen:
            seen.add(parent)
            if mapped[index[parent]].total_tokens is not None:
                above = True
                break
            parent = spans[index[parent]].parent_id
        if not above:
            total, reported = total + tokens, True
    return total if reported else None


def _as_text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def map_trace(trace: Trace) -> Trace:
    """The trace with the fields its spans give and it leaves unset, mapped with the newest table,
    and that table's version in its metadata under ``MAPPING_KEY``. A trace that already names a
    version is returned unchanged, so it keeps the fields it was mapped with.

    - Each span's unset kind, inputs and outputs are filled; its attributes are not touched.
    - The model, session and user are the first a span gives, spans in order of start time.
    - The tokens are the spans' totals added up, a span left out when an ancestor reports a total.
    - The request and response are the root span's inputs and outputs, as text or JSON.
    """
    if MAPPING_KEY in trace.metadata:
        return trace
    spans = trace.spans
    mapped = [map_span(span, version=MAPPING_VERSION) for span in spans]
    order = sorted(range(len(spans)), key=lambda i: (spans[i].start_ms is None, spans[i].start_ms or 0.0, i))
    filled = []
    for span, fields in zip(spans, mapped, strict=True):
        unset: dict[str, Any] = {}
        if (not span.kind or span.kind == "UNKNOWN") and fields.kind:
            unset["kind"] = fields.kind
        if span.inputs is None and fields.inputs is not None:
            unset["inputs"] = fields.inputs
        if span.outputs is None and fields.outputs is not None:
            unset["outputs"] = fields.outputs
        filled.append(span.model_copy(update=unset) if unset else span)
    changes: dict[str, Any] = {"spans": filled, "metadata": {**trace.metadata, MAPPING_KEY: MAPPING_VERSION}}
    for name in ("model", "session_id", "user_id"):
        if getattr(trace, name) is None:
            changes[name] = next((value for i in order if (value := getattr(mapped[i], name)) is not None), None)
    if trace.tokens is None:
        changes["tokens"] = _total_tokens(spans, mapped)
    ids = {span.id for span in spans}
    root = next((i for i in order if not spans[i].parent_id or spans[i].parent_id not in ids), None)
    if root is not None:
        if not trace.request and mapped[root].inputs is not None:
            changes["request"] = _as_text(mapped[root].inputs)
        if not trace.response and mapped[root].outputs is not None:
            changes["response"] = _as_text(mapped[root].outputs)
    return trace.model_copy(update=changes)


__all__ = [
    "MAPPING_KEY",
    "MAPPING_VERSION",
    "STYLES",
    "TABLES",
    "V1",
    "MappingTable",
    "Rule",
    "SpanFields",
    "ToolCall",
    "map_attributes",
    "map_span",
    "map_trace",
    "mapping_table",
]
