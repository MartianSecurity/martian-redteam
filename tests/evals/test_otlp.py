"""OpenTelemetry's ``POST /v1/traces``: an application's exported spans become traces in the store,
shown by the Traces routes, for the principal the edition's sign-in names."""

from __future__ import annotations

import asyncio
import gzip
import json
import socket
import threading
import time
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any

import httpx
import pytest
import requests
import uvicorn
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
from google.rpc.status_pb2 import Status as RpcStatus
from opentelemetry.exporter.otlp.proto.http import Compression
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExportResult
from opentelemetry.trace import Status, StatusCode

from martian.evals.local import create_local_app
from martian.evals.model import Project, Trace
from martian.evals.records.sql import SqlRecordStore
from martian.evals.server import SINGLE_USER, Principal, create_app, organisation_principal, otlp
from martian.evals.store import REQUEST_PREVIEW_TRUNCATED, RESPONSE_PREVIEW_TRUNCATED, MemoryStore, StoreError
from martian.evals.store.mlflow_local import TRACE_SPAN_KEY, MlflowStore
from martian.evals.trace_conventions import MAPPING_KEY, MAPPING_VERSION
from tests.evals.trace_samples import MODEL, QUESTION, STYLES, UNKNOWN, sample_spans

PROTOBUF = {"content-type": "application/x-protobuf"}
JSON = {"content-type": "application/json"}
TRACE = "5b8efff798038103d269b633813fc60c"
ROOT, CHILD, TOOL = "eee19b7ec3c1b173", "eee19b7ec3c1b174", "eee19b7ec3c1b175"
NANOS = 1_763_200_000_000_000_000


@pytest.fixture(autouse=True)
def no_telemetry(monkeypatch):
    monkeypatch.setenv("MLFLOW_DISABLE_TELEMETRY", "true")
    monkeypatch.delenv("MLFLOW_WORKSPACE", raising=False)


@pytest.fixture(params=["memory", "mlflow"])
def store(request, tmp_path):
    if request.param == "memory":
        return MemoryStore()
    return MlflowStore(f"sqlite:///{tmp_path / 'mlflow.db'}", tmp_path / "artifacts")


@pytest.fixture
def records(tmp_path):
    found = SqlRecordStore(f"sqlite:///{tmp_path / 'martian.db'}")
    yield found
    found.close()


@pytest.fixture
def ui(tmp_path) -> Path:
    folder = tmp_path / "dist-evals"
    folder.mkdir()
    (folder / "index.html").write_text("<!doctype html><title>Martian Evals</title>", encoding="utf-8")
    return folder


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


@contextmanager
def serving(app: FastAPI) -> Iterator[str]:
    """``app`` served on a loopback port of its own until the block ends; yields its address."""
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=free_port(), log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert thread.is_alive() and time.monotonic() < deadline, "the server did not start"
        time.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{server.config.port}"
    finally:
        server.should_exit = True
        thread.join(10)


class Recording(OTLPSpanExporter):
    """The SDK's OTLP/HTTP exporter, keeping the size and result of each export."""

    def __init__(self, endpoint: str, **options: Any) -> None:
        session = requests.Session()
        session.trust_env = False  # straight to loopback, whatever proxy the environment names
        super().__init__(endpoint=endpoint, session=session, timeout=10, **options)
        self.results: list[tuple[int, SpanExportResult]] = []

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        result = super().export(spans)
        self.results.append((len(spans), result))
        return result


def tracer_for(exporter: OTLPSpanExporter) -> tuple[TracerProvider, Any]:
    # Each span is exported the moment it ends, in a request of its own: children before their root.
    provider = TracerProvider(resource=Resource.create({"service.name": "support-agent"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, provider.get_tracer("support-agent")


def trace_id_of(span: Any) -> str:
    return "tr-" + format(span.get_span_context().trace_id, "032x")


def json_span(
    span_id: str, *, parent: str = "", offset_ms: int = 0, duration_ms: int = 100, **fields: Any
) -> dict[str, Any]:
    start = NANOS + offset_ms * 1_000_000
    return {
        "traceId": TRACE,
        "spanId": span_id,
        "parentSpanId": parent,
        "name": "span " + span_id[-4:],
        "startTimeUnixNano": str(start),
        "endTimeUnixNano": str(start + duration_ms * 1_000_000),
        **fields,
    }


def json_body(*spans: dict[str, Any]) -> str:
    return json.dumps({"resourceSpans": [{"scopeSpans": [{"spans": list(spans)}]}]})


def attribute(key: str, value: str) -> dict[str, Any]:
    return {"key": key, "value": {"stringValue": value}}


def search(client: TestClient | httpx.Client, headers: dict[str, str] | None = None, **query: Any) -> list[Any]:
    response = client.post("/v1/evals/traces/search", json={"order": "oldest", **query}, headers=headers or {})
    assert response.status_code == 200, response.text
    items: list[Any] = response.json()["items"]
    return items


def test_an_application_exporting_with_the_opentelemetry_sdk_shows_in_traces(tmp_path, ui):
    with serving(create_local_app(tmp_path / "home", ui)) as url:
        exporter = Recording(url + "/v1/traces", compression=Compression.Gzip)
        provider, tracer = tracer_for(exporter)
        ids = {}
        for style in STYLES:
            root, model, tool = sample_spans(style)
            with tracer.start_as_current_span(root.name, attributes=root.attributes) as agent:
                with tracer.start_as_current_span(model.name, attributes=model.attributes):
                    pass
                with tracer.start_as_current_span(tool.name, attributes=tool.attributes):
                    pass
            ids[style] = trace_id_of(agent)
        with tracer.start_as_current_span("refund") as failing:
            failing.set_status(Status(StatusCode.ERROR, "no such order"))
        provider.shutdown()
        assert exporter.results == [(1, SpanExportResult.SUCCESS)] * 10

        with httpx.Client(base_url=url, timeout=10, trust_env=False) as client:
            traces = search(client)
            assert [(t["id"], t["name"], t["state"]) for t in traces] == [
                *[(ids[style], sample_spans(style)[0].name, "OK") for style in STYLES],
                (trace_id_of(failing), "refund", "ERROR"),
            ]
            for row in traces[:3]:
                assert (row["model"], row["tokens"], row["session_id"], row["user_id"]) == (
                    MODEL,
                    20,
                    "conversation-1",
                    "alice",
                ), row["name"]
                assert QUESTION in row["request"] and row["latency_ms"] is not None
            sessions = client.post("/v1/evals/sessions/search", json={}).json()["items"]
            assert [(s["id"], s["turns"]) for s in sessions] == [("conversation-1", 3)]
            detail = client.get(f"/v1/evals/traces/{ids['otel_genai']}").json()
    assert [(s["name"], s["type"], s["depth"]) for s in detail["spans"]] == [
        ("invoke_agent support", "AGENT", 0),
        ("chat example-chat-1", "CHAT_MODEL", 1),
        ("execute_tool order_status", "TOOL", 1),
    ]
    assert detail["spans"][0]["attributes"].items() >= UNKNOWN.items()
    assert detail["info"]["trace_metadata"][MAPPING_KEY] == MAPPING_VERSION


TOKENS = {
    "token-a": organisation_principal("user-a", "org-a", frozenset({"member"})),
    "token-b": organisation_principal("user-b", "org-b", frozenset({"member"})),
    "token-viewer": organisation_principal("user-v", "org-a", frozenset({"viewer"})),
}


def signed_in(authorization: Annotated[str | None, Header()] = None) -> Principal:
    """A server's sign-in: a bearer token names an organisation member."""
    token = (authorization or "").removeprefix("Bearer ")
    if token not in TOKENS:
        raise HTTPException(401, "Sign in", headers={"WWW-Authenticate": "Bearer"})
    return TOKENS[token]


def test_a_request_without_credentials_is_refused(records):
    app = create_app(store=MemoryStore(), records=records, principal=signed_in)
    bearer = {"Authorization": "Bearer token-a"}
    with serving(app) as url:
        for headers in ({}, {"Authorization": "Bearer stolen"}, {"Authorization": "Bearer token-viewer"}, bearer):
            exporter = Recording(url + "/v1/traces", headers=headers)
            provider, tracer = tracer_for(exporter)
            with tracer.start_as_current_span("answer") as span:
                pass
            provider.shutdown()
            expected = SpanExportResult.SUCCESS if headers == bearer else SpanExportResult.FAILURE
            assert exporter.results == [(1, expected)], headers

        refused = httpx.post(
            url + "/v1/traces", content=ExportTraceServiceRequest().SerializeToString(), headers=PROTOBUF
        )
        assert (refused.status_code, refused.headers["www-authenticate"]) == (401, "Bearer")
        assert RpcStatus.FromString(refused.content).message == "Sign in"
        viewer = httpx.post(
            url + "/v1/traces", content=json_body(), headers={**JSON, "Authorization": "Bearer token-viewer"}
        )
        assert (viewer.status_code, viewer.json()) == (403, {"message": "Requires member role"})

    client = TestClient(app)
    assert [t["id"] for t in search(client, bearer)] == [trace_id_of(span)]
    assert search(client, {"Authorization": "Bearer token-b"}) == []


def test_spans_of_one_trace_in_several_requests_make_one_trace(store, records):
    client = TestClient(create_app(store=store, records=records, principal=lambda: SINGLE_USER))
    chat = json_span(
        CHILD,
        parent=ROOT,
        offset_ms=10,
        duration_ms=50,
        attributes=[attribute("gen_ai.operation.name", "chat"), attribute("gen_ai.request.model", MODEL)],
    )
    first = client.post("/v1/traces", content=json_body(chat), headers=JSON)
    assert (first.status_code, first.json()) == (200, {})
    (waiting,) = search(client)
    assert (waiting["id"], waiting["state"], waiting["model"]) == ("tr-" + TRACE, "IN_PROGRESS", MODEL)
    # Until its root span arrives, the trace's spans are not one tree, so its detail is refused.
    assert client.get(f"/v1/evals/traces/tr-{TRACE}").status_code == 409
    asyncio.run(store.set_trace_tag(SINGLE_USER, "default", "tr-" + TRACE, "martian.evals.triage", '{"status":"open"}'))

    root = json_span(
        ROOT, status={"code": 1}, attributes=[attribute("gen_ai.conversation.id", "conversation-1")], name="answer"
    )
    # The root, and the model span again, as an exporter retrying a request sends it.
    second = client.post("/v1/traces", content=json_body(root, chat), headers=JSON)
    assert (second.status_code, second.json()) == (200, {})
    (done,) = search(client)
    assert (done["name"], done["state"], done["model"], done["session_id"], done["latency_ms"]) == (
        "answer",
        "OK",
        MODEL,
        "conversation-1",
        100.0,
    )
    assert done["triage"] == {"status": "open"}
    detail = client.get(f"/v1/evals/traces/tr-{TRACE}").json()
    assert [(s["id"], s["parent_id"], s["type"]) for s in detail["spans"]] == [
        (ROOT, None, "UNKNOWN"),
        (CHILD, ROOT, "CHAT_MODEL"),
    ]
    assert detail["info"]["trace_metadata"][MAPPING_KEY] == MAPPING_VERSION


def test_a_long_request_stays_whole_in_one_root_span_as_batches_arrive(tmp_path, records):
    """On the in-process binding the request and response the mapping reads from a trace's root
    span are kept whole in the binding's own root span, and searched as shortened previews the
    trace's metadata flags, through every batch that records the trace again."""
    store = MlflowStore(f"sqlite:///{tmp_path / 'mlflow.db'}", tmp_path / "artifacts")
    client = TestClient(create_app(store=store, records=records, principal=lambda: SINGLE_USER))
    request, response = "Where is order 42? " * 100, "Order 42 has shipped. " * 100
    chat = json_span(CHILD, parent=ROOT, offset_ms=10, duration_ms=50, attributes=[attribute("llm.model_name", MODEL)])
    root = json_span(
        ROOT,
        status={"code": 1},
        attributes=[attribute("input.value", request), attribute("output.value", response)],
        name="answer",
    )
    # An application span may carry any attribute, the binding's root span marker among them, and
    # start before its parent, as a span from a host with a skewed clock does. It stays the
    # application's span.
    marked = {"key": TRACE_SPAN_KEY, "value": {"boolValue": True}}
    tool = json_span(TOOL, parent=ROOT, offset_ms=-50, duration_ms=20, attributes=[marked])

    def parentless() -> list[Any]:
        """The trace's spans without a parent, as MLflow itself reads them."""
        native = store._store.get_trace("tr-" + TRACE, allow_partial=True)
        return [span for span in native.data.spans if span.parent_id is None]

    for batch, spans in (((chat,), [CHILD]), ((root, chat), [ROOT, CHILD]), ((tool,), [ROOT, TOOL, CHILD])):
        assert client.post("/v1/traces", content=json_body(*batch), headers=JSON).json() == {}
        (stored,) = asyncio.run(store._call(SINGLE_USER, parentless))
        assert stored.get_attribute(TRACE_SPAN_KEY) is True
        if batch == (chat,):
            # Until the root span arrives the trace has no request.
            assert (stored.inputs, search(client)[0]["request"]) == ("", "")
            continue
        assert (stored.inputs, stored.outputs) == (request, response)
        (row,) = search(client)
        assert (row["request"], row["response"]) == (request[:997] + "...", response[:997] + "...")
        detail = client.get(f"/v1/evals/traces/tr-{TRACE}").json()
        assert (detail["info"]["request_preview"], detail["info"]["response_preview"]) == (request, response)
        assert (
            detail["info"]["trace_metadata"].items()
            >= {
                REQUEST_PREVIEW_TRUNCATED: "true",
                RESPONSE_PREVIEW_TRUNCATED: "true",
                MAPPING_KEY: MAPPING_VERSION,
            }.items()
        )
        assert [s["id"] for s in detail["spans"]] == spans


class Refusing(MemoryStore):
    """A store that refuses one trace."""

    async def record_trace(self, principal: Principal, trace: Trace) -> Trace:
        if trace.id == "tr-" + "b" * 32:
            raise StoreError("invalid", "this trace is refused")
        return await super().record_trace(principal, trace)


def test_spans_the_core_cannot_take_are_rejected_and_the_rest_written(records):
    client = TestClient(create_app(store=Refusing(), records=records, principal=lambda: SINGLE_USER))
    exported = ExportTraceServiceRequest()
    spans = exported.resource_spans.add().scope_spans.add().spans
    for trace_id, span_id, parent in (
        ("a" * 32, "a" * 16, ""),
        ("b" * 32, "b" * 16, ""),
        ("b" * 32, "c" * 16, "b" * 16),
        ("d" * 16, "d" * 16, ""),  # an 8-byte trace id
        ("e" * 32, "0" * 16, ""),  # an all-zero span id
    ):
        span = spans.add(trace_id=bytes.fromhex(trace_id), span_id=bytes.fromhex(span_id), name="turn")
        span.parent_span_id = bytes.fromhex(parent)
        span.start_time_unix_nano, span.end_time_unix_nano = NANOS, NANOS + 1
    response = client.post("/v1/traces", content=exported.SerializeToString(), headers=PROTOBUF)
    assert (response.status_code, response.headers["content-type"]) == (200, "application/x-protobuf")
    answer = ExportTraceServiceResponse.FromString(response.content).partial_success
    assert answer.rejected_spans == 4
    assert answer.error_message == (
        "2 span(s) without a valid trace id, span id or parent span id; "
        f"2 span(s) of trace tr-{'b' * 32}: this trace is refused"
    )
    assert [t["id"] for t in search(client)] == ["tr-" + "a" * 32]


def test_the_project_header_names_the_project(records):
    store = MemoryStore()
    project = str(uuid.uuid4())
    asyncio.run(store.create_project(SINGLE_USER, Project(id=project, name="Checkout agent")))
    client = TestClient(create_app(store=store, records=records, principal=lambda: SINGLE_USER))
    sent = client.post("/v1/traces", content=json_body(json_span(ROOT)), headers={**JSON, otlp.PROJECT_HEADER: project})
    assert sent.status_code == 200
    assert [t["id"] for t in search(client, project_id=project)] == ["tr-" + TRACE]
    assert search(client) == []


def gzipped(text: str) -> bytes:
    return gzip.compress(text.encode(), mtime=0)  # the same bytes in every pytest-xdist worker


@pytest.mark.parametrize(
    ("headers", "body", "status", "message"),
    [
        ({"content-type": "text/plain"}, "{}", 415, "Send OTLP as application/x-protobuf or application/json"),
        (PROTOBUF, b"\xff\xff\xff", 400, "The body is not an OTLP ExportTraceServiceRequest in application/x-protobuf"),
        (JSON, "not json", 400, "The body is not an OTLP ExportTraceServiceRequest in application/json"),
        (
            JSON,
            json_body({"traceId": "abc"}),
            400,
            "The body is not an OTLP ExportTraceServiceRequest in application/json",
        ),
        (
            {**JSON, "content-encoding": "br"},
            "{}",
            415,
            "Content-Encoding br is not supported; send gzip, deflate or none",
        ),
        ({**JSON, "content-encoding": "gzip"}, "{}", 400, "The body is not valid gzip"),
        ({**JSON, otlp.PROJECT_HEADER: "checkout"}, "{}", 400, "x-martian-project-id must be a project id"),
        ({**JSON, otlp.PROJECT_HEADER: str(uuid.UUID(int=1))}, "{}", 404, "Not found"),
        (JSON, json_body(json_span(ROOT)) + " " * 64, 413, "The body is larger than 64 bytes"),
        (
            {**JSON, "content-encoding": "gzip"},
            gzipped("{}" + " " * 64),
            413,
            "The decompressed body is larger than 64 bytes",
        ),
    ],
    ids=[
        "content type",
        "protobuf",
        "json",
        "json id",
        "content encoding",
        "gzip",
        "project id",
        "unknown project",
        "too large",
        "too large decompressed",
    ],
)
def test_a_request_the_endpoint_cannot_take_is_refused_with_a_status(
    monkeypatch, records, headers, body, status, message
):
    monkeypatch.setattr(otlp, "MAX_BODY_BYTES", 64 if status == 413 else otlp.MAX_BODY_BYTES)
    store = MemoryStore()
    client = TestClient(create_app(store=store, records=records, principal=lambda: SINGLE_USER))
    response = client.post("/v1/traces", content=body, headers=headers)
    assert response.status_code == status
    if headers["content-type"] == JSON["content-type"]:
        assert response.json() == {"message": message}
    else:
        assert RpcStatus.FromString(response.content).message == message
    assert asyncio.run(store.list_projects(SINGLE_USER)) == []


def test_an_archived_project_takes_no_traces(records):
    store = MemoryStore()
    client = TestClient(create_app(store=store, records=records, principal=lambda: SINGLE_USER))
    assert client.post("/v1/traces", content=json_body(), headers=JSON).status_code == 200
    asyncio.run(store.archive_project(SINGLE_USER, "default"))
    refused = client.post("/v1/traces", content=json_body(json_span(ROOT)), headers=JSON)
    assert (refused.status_code, refused.json()) == (
        409,
        {"message": "This evaluation project is archived or unavailable"},
    )
