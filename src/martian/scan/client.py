"""The client's view of the backend's /v1/scanner routes (MartianSecurity/martian-notes#468, #470).

``POST /v1/scanner/scans`` carries a request id the client chose (``Idempotency-Key``), so the upload is
retried safely: on a transport error, 429 and 500, 502, 503 and 504, the same bytes go again under the
same id and the server answers the scan the first attempt created (200) instead of starting another. An
id the server says was used for another upload (409 ``request_id_reused``) is an error, never retried.
Checking the policy file (``POST /gate-policy/validate``) changes nothing and is retried like the reads
(the scan, its findings and its exports). The gate (``POST /scans/{id}/gate``) stores an evaluation and is
sent once.

``tests/scan/contract/scanner-scans.json`` is an unchanged copy of the backend's contract for these
routes. A server from before the findings, export and gate routes answers them with FastAPI's bare
``404 {"detail": "Not Found"}``, which the client reports as the route being absent (``None``); any
other 404 is an error.

Nothing here interprets a finding. Response bodies other than the fields named below, and every
message the server sends, stay out of the client's output except a refusal's ``detail.code`` and
``detail.limit``.
"""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Callable, Iterator
from typing import Any, Literal
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from martian._errors import message, reference
from martian.config import MartianConfig

ScanState = Literal["queued", "running", "completed", "failed"]
TERMINAL: frozenset[str] = frozenset({"completed", "failed"})
TRANSIENT = frozenset({429, 500, 502, 503, 504})
UPLOAD_ATTEMPTS = 4
MAX_RETRY_AFTER = 30.0
_SERVER_CODE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_DIGEST = r"^sha256:[0-9a-f]{64}$"


class _Read(BaseModel):
    # The server adds fields over time; the client reads only these.
    model_config = ConfigDict(extra="ignore")


class ScanFailure(_Read):
    code: str
    message: str | None = None


class Subject(_Read):
    kind: str
    name: str


class Scan(_Read):
    id: UUID
    subject: Subject
    content_digest: str = Field(pattern=_DIGEST)
    state: ScanState
    stage: str | None = None
    error: ScanFailure | None = None
    coverage: dict[str, Any] | None = None
    counts: dict[str, Any] | None = None


class Finding(_Read):
    rule_id: str
    severity: str
    evidence_level: int = Field(ge=0, le=6)
    evidence_level_name: str | None = None
    path: str
    line_start: int | None = None
    line_end: int | None = None
    title: str
    status: str = "open"
    match: str | None = None
    gating: bool = True


class FindingPage(_Read):
    items: list[Finding]
    next_cursor: str | None = None


class Baseline(_Read):
    scan_id: UUID
    git: dict[str, Any] | None = None


class Gate(_Read):
    """The server's verdict on a scan against the policy file: ``exit_code`` is what the command exits with."""

    id: UUID
    scan_id: UUID
    verdict: Literal["passed", "failed", "not_judged"]
    exit_code: Literal[0, 1, 2]
    reasons: list[str]
    new_findings_only: bool
    baseline: Baseline | None = None
    coverage_shortfalls: list[str] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    policy_sha256: str

    @field_validator("exit_code", mode="before")
    @classmethod
    def integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("exit_code must be an integer")
        return value

    @model_validator(mode="after")
    def consistent(self) -> Gate:
        if {"passed": 0, "failed": 1, "not_judged": 2}[self.verdict] != self.exit_code:
            raise ValueError("the verdict and the exit code disagree")
        return self


class Policy(_Read):
    """The policy file as the server validated it, every default filled in."""

    policy: dict[str, Any]
    policy_sha256: str


class PolicyRefused(Exception):
    """The server refused the policy file: each error's place in the file and why, on one line each."""

    def __init__(self, errors: list[tuple[str, str]]) -> None:
        self.errors = errors
        super().__init__("the server refused the policy file")


class ScanApiError(RuntimeError):
    """A failed call. ``code`` is the SDK's (``http_403``, ``connection_error``, ...); ``server_code`` and
    ``limit`` are the refusal's ``detail.code`` and ``detail.limit`` when the server sent safe ones."""

    def __init__(
        self,
        code: str,
        *,
        status: int = 0,
        server_code: str | None = None,
        limit: int | None = None,
        request_id: str | None = None,
        explanation: str | None = None,
    ) -> None:
        self.code = code
        self.status = status
        self.server_code = server_code
        self.limit = limit
        self.request_id = reference(request_id)
        detail = explanation or message(code, request_id=self.request_id)
        refusal = f" Server code: {server_code}" if server_code else ""
        if refusal and limit is not None:
            refusal += f" (limit {limit})"
        super().__init__(detail + (refusal + "." if refusal else ""))


def _refusal(response: httpx.Response) -> tuple[str | None, int | None]:
    try:
        body = response.json()
    except ValueError:
        return None, None
    detail = body.get("detail") if isinstance(body, dict) else None
    if not isinstance(detail, dict):
        return None, None
    code = detail.get("code")
    limit = detail.get("limit")
    return (
        code if isinstance(code, str) and _SERVER_CODE.fullmatch(code) else None,
        limit if type(limit) is int else None,
    )


def _policy_errors(response: httpx.Response) -> list[tuple[str, str]]:
    """Where and why from a refused policy file (``detail.errors``), at most 20, each on one line."""
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        return []
    errors = detail.get("errors") if isinstance(detail, dict) else None
    found = []
    for item in errors if isinstance(errors, list) else []:
        if isinstance(item, dict) and isinstance(item.get("loc"), str) and isinstance(item.get("message"), str):
            found.append((item["loc"][:200], item["message"][:300]))
    return found[:20]


def _absent(response: httpx.Response) -> bool:
    """FastAPI's answer for a path the server does not route."""
    if response.status_code != 404:
        return False
    try:
        return bool(response.json() == {"detail": "Not Found"})
    except ValueError:
        return False


def _retry_after(response: httpx.Response) -> float:
    try:
        value = float(response.headers.get("Retry-After", "0"))
    except ValueError:
        return 0.0
    return min(value, MAX_RETRY_AFTER) if math.isfinite(value) and value > 0 else 0.0


class ScanClient:
    """One organisation API key with ``scan:run`` (upload) and ``scan:read`` (reads)."""

    def __init__(
        self,
        config: MartianConfig,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self._token = config.token.get_secret_value()
        self._http = httpx.Client(
            base_url=config.api_url,
            headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json"},
            timeout=httpx.Timeout(config.request_timeout_seconds),
            follow_redirects=False,
            trust_env=config.trust_env,
            transport=transport,
        )
        self._sleep = sleep
        self._clock = clock

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> ScanClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _error(self, response: httpx.Response) -> ScanApiError:
        server_code, limit = _refusal(response)
        return ScanApiError(
            f"http_{response.status_code}",
            status=response.status_code,
            server_code=server_code,
            limit=limit,
            request_id=reference(response.headers.get("x-request-id"), exclude=self._token),
        )

    def _parse(self, response: httpx.Response, model: type[_Read]) -> Any:
        try:
            return model.model_validate(response.json())
        except (ValueError, ValidationError):
            raise ScanApiError("invalid_response", status=response.status_code) from None

    # --- the upload ------------------------------------------------------------------------------

    def upload(self, manifest: dict[str, Any], archive: bytes, *, request_id: UUID) -> tuple[Scan, bool]:
        """``POST /v1/scanner/scans`` under ``request_id``: the ``manifest`` part, then the ``archive``
        part. Retried with the same id and bytes on a transport error or a transient status, at most
        :data:`UPLOAD_ATTEMPTS` times. Returns the scan and whether the server answered an earlier
        attempt's scan (200)."""
        files: dict[str, tuple[str | None, bytes | str, str]] = {
            "manifest": (None, json.dumps(manifest, separators=(",", ":")), "application/json"),
            "archive": ("bundle.zip", archive, "application/zip"),
        }
        headers = {"Idempotency-Key": str(request_id)}
        last = ScanApiError("connection_error")
        for attempt in range(1, UPLOAD_ATTEMPTS + 1):
            delay = min(10.0, 0.5 * 2 ** (attempt - 1))
            try:
                response = self._http.post("/v1/scanner/scans", files=files, headers=headers)
            except httpx.TransportError:
                last = ScanApiError(
                    "connection_error",
                    explanation=(
                        f"The upload got no answer in {attempt} attempt(s) under request id {request_id}; "
                        "sending it again with that id gives the same scan."
                    ),
                )
            else:
                if response.status_code in (200, 201):
                    return self._parse(response, Scan), response.status_code == 200
                if response.status_code not in TRANSIENT:
                    raise self._error(response)
                last = self._error(response)
                delay = max(delay, _retry_after(response))
            if attempt < UPLOAD_ATTEMPTS:
                self._sleep(delay)
        raise last

    # --- the policy file and the gate --------------------------------------------------------------

    def validate_policy(self, text: str) -> Policy | None:
        """``POST /v1/scanner/gate-policy/validate``: the file as the server reads it, or None when the
        server has no gate. Raises :class:`PolicyRefused` for a file it refuses."""
        response = self._retried("POST", "/v1/scanner/gate-policy/validate", body={"policy": text})
        if _absent(response):
            return None
        if response.status_code == 422:
            raise PolicyRefused(_policy_errors(response))
        if response.status_code != 200:
            raise self._error(response)
        return self._parse(response, Policy)  # type: ignore[no-any-return]

    def gate(self, scan_id: UUID, text: str) -> Gate | None:
        """``POST /v1/scanner/scans/{scan_id}/gate``, sent once: the server's stored verdict, or None when
        the server has no gate."""
        try:
            response = self._http.post(f"/v1/scanner/scans/{scan_id}/gate", json={"policy": text})
        except httpx.TransportError:
            raise ScanApiError("connection_error") from None
        if _absent(response):
            return None
        if response.status_code == 422:
            raise PolicyRefused(_policy_errors(response))
        if response.status_code != 201:
            raise self._error(response)
        gate: Gate = self._parse(response, Gate)
        if gate.scan_id != scan_id:
            raise ScanApiError("invalid_response", status=201)
        return gate

    # --- reads -----------------------------------------------------------------------------------

    def _get(
        self, path: str, *, params: dict[str, str | int] | None = None, deadline: float | None = None
    ) -> httpx.Response:
        return self._retried("GET", path, params=params, deadline=deadline)

    def _retried(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str | int] | None = None,
        body: dict[str, Any] | None = None,
        deadline: float | None = None,
    ) -> httpx.Response:
        """A request that changes nothing, retried on transport errors and transient statuses: four
        attempts, or until ``deadline`` (a ``clock`` value) when one is given."""
        attempt = 0
        while True:
            attempt += 1
            last: ScanApiError
            delay = min(10.0, 0.5 * 2 ** (attempt - 1))
            try:
                response = self._http.request(method, path, params=params, json=body)
            except httpx.TransportError:
                last = ScanApiError("connection_error")
            else:
                if response.status_code not in TRANSIENT:
                    return response
                last = self._error(response)
                delay = max(delay, _retry_after(response))
            out_of_time = deadline is not None and self._clock() + delay > deadline
            if (deadline is None and attempt >= 4) or out_of_time:
                raise last
            self._sleep(delay)

    def scan(self, scan_id: UUID, *, deadline: float | None = None) -> Scan:
        response = self._get(f"/v1/scanner/scans/{scan_id}", deadline=deadline)
        if response.status_code != 200:
            raise self._error(response)
        scan: Scan = self._parse(response, Scan)
        if scan.id != scan_id:
            raise ScanApiError("invalid_response", status=200)
        return scan

    def wait(
        self,
        scan: Scan,
        *,
        timeout_seconds: float,
        on_change: Callable[[Scan], None] | None = None,
    ) -> Scan:
        """Poll the scan until it completed or failed. Raises ``ScanApiError("scan_timeout")`` when it
        has not ended by the deadline; that is never a result."""
        deadline = self._clock() + timeout_seconds
        interval = 1.0
        seen = (scan.state, scan.stage)
        while scan.state not in TERMINAL:
            if self._clock() + interval > deadline:
                raise ScanApiError(
                    "scan_timeout",
                    explanation=(
                        f"Scan {scan.id} was still {scan.state} after {timeout_seconds:g} seconds. "
                        "It is not a result; read it again later or raise --timeout."
                    ),
                )
            self._sleep(interval)
            interval = min(interval * 1.5, 10.0)
            scan = self.scan(scan.id, deadline=deadline)
            if (scan.state, scan.stage) != seen:
                seen = (scan.state, scan.stage)
                if on_change is not None:
                    on_change(scan)
        return scan

    def findings(self, scan_id: UUID) -> list[Finding] | None:
        """Every finding of the scan in the order reported, or None when the server lists none."""
        found: list[Finding] = []
        for page in self._pages(scan_id):
            if page is None:
                return None
            found.extend(page.items)
        return found

    def _pages(self, scan_id: UUID) -> Iterator[FindingPage | None]:
        cursor: str | None = None
        for _ in range(1000):
            params: dict[str, str | int] = {"limit": 500}
            if cursor is not None:
                params["cursor"] = cursor
            response = self._get(f"/v1/scanner/scans/{scan_id}/findings", params=params)
            if _absent(response):
                yield None
                return
            if response.status_code != 200:
                raise self._error(response)
            page: FindingPage = self._parse(response, FindingPage)
            yield page
            if not page.next_cursor:
                return
            cursor = page.next_cursor
        raise ScanApiError("invalid_response", status=200)

    def export(self, scan_id: UUID, form: Literal["json", "sarif"]) -> bytes | None:
        """The scan as the server exports it, byte for byte, or None when the server has no export.
        The body must be a JSON object, and a SARIF log of version 2.1.0."""
        response = self._get(f"/v1/scanner/scans/{scan_id}/export", params={"format": form})
        if _absent(response):
            return None
        if response.status_code != 200:
            raise self._error(response)
        try:
            body = response.json()
        except ValueError:
            raise ScanApiError("invalid_response", status=200) from None
        if not isinstance(body, dict) or (form == "sarif" and body.get("version") != "2.1.0"):
            raise ScanApiError("invalid_response", status=200)
        return response.content
