"""A transport that serves :class:`FakeScanner` to the scan client and holds every exchange to the
checked-in contract (``contract/scanner-scans.json``)."""

from __future__ import annotations

import email.parser
import email.policy
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from jsonschema import Draft202012Validator

from martian.config import MartianConfig
from martian.scan.client import ScanClient
from tests.scan.fake_backend import TOKEN, FakeScanner

CONTRACT_PATH = Path(__file__).parent / "contract" / "scanner-scans.json"
API_URL = "https://backend.example"


class ContractDrift(AssertionError):
    pass


def contract() -> dict[str, Any]:
    return json.loads(CONTRACT_PATH.read_text("utf-8"))


def _template(paths: dict[str, Any], path: str) -> str | None:
    for template in paths:
        pattern = "^" + re.sub(r"\\\{[^}]+\\\}", "[^/]+", re.escape(template)) + "$"
        if re.match(pattern, path):
            return template
    return None


def _validate(schema: dict[str, Any], body: Any, document: dict[str, Any], what: str) -> None:
    errors = sorted(Draft202012Validator({**schema, "components": document["components"]}).iter_errors(body), key=str)
    if errors:
        raise ContractDrift(f"{what} does not match the contract: {errors[0].message}")


def check_exchange(document: dict[str, Any], request: httpx.Request, status: int, body: bytes) -> str:
    """Hold one request and its answer to ``document``; returns the route template."""
    method = request.method.lower()
    template = _template(document["paths"], request.url.path)
    if template is None or method not in document["paths"][template]:
        raise ContractDrift(f"{request.method} {request.url.path} is not a route of the contract")
    operation = document["paths"][template][method]
    allowed = {parameter["name"] for parameter in operation.get("parameters", []) if parameter["in"] == "query"}
    unknown = set(request.url.params) - allowed
    if unknown:
        raise ContractDrift(f"{request.method} {template} sends query parameters {sorted(unknown)} the contract lacks")
    if method == "post" and template == "/v1/scanner/scans":
        content = operation["requestBody"]["content"]["multipart/form-data"]
        message = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
            b"Content-Type: " + request.headers["content-type"].encode() + b"\r\n\r\n" + request.content
        )
        parts = list(message.iter_parts())
        names = [part.get_param("name", header="content-disposition") for part in parts]
        if names != content["schema"]["required"]:
            raise ContractDrift(f"the upload sends parts {names}")
        for part, name in zip(parts, names, strict=True):
            expected = content["encoding"][name]["contentType"]
            if part.get_content_type() != expected:
                raise ContractDrift(f"the {name} part is {part.get_content_type()}, not {expected}")
        manifest = json.loads(parts[0].get_payload(decode=True))
        _validate(content["schema"]["properties"]["manifest"], manifest, document, "the manifest")
    declared = operation["responses"].get(str(status))
    if declared and "application/json" in declared.get("content", {}):
        _validate(declared["content"]["application/json"]["schema"], json.loads(body), document, f"{template} {status}")
    return template


class Transport(httpx.BaseTransport):
    """Serves ``scanner``; ``before`` may answer or fail a request first (to inject faults)."""

    def __init__(
        self,
        scanner: FakeScanner,
        *,
        before: Callable[[httpx.Request], httpx.Response | None] | None = None,
        document: dict[str, Any] | None = None,
    ) -> None:
        self.scanner, self.before = scanner, before
        self.document = document if document is not None else contract()
        self.routes: list[tuple[str, str]] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        request.read()
        if self.before is not None and (injected := self.before(request)) is not None:
            return injected
        status, headers, body = self.scanner.handle(
            request.method, str(request.url.raw_path, "ascii"), dict(request.headers), request.content
        )
        self.routes.append((request.method, check_exchange(self.document, request, status, body)))
        return httpx.Response(status, headers=headers, content=body)


class Clock:
    """A clock that only moves when the client sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def config() -> MartianConfig:
    return MartianConfig(API_URL, TOKEN)


def factory(transport: httpx.BaseTransport, clock: Clock | None = None) -> Callable[[MartianConfig], ScanClient]:
    clock = clock or Clock()
    return lambda chosen: ScanClient(chosen, transport=transport, sleep=clock.sleep, clock=clock)
