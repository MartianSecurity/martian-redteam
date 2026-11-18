"""The client's view of the backend's /v1/scanner routes (MartianSecurity/martian-notes#468).

Upload once, then read. ``POST /v1/scanner/scans`` is sent exactly once: the backend has no
idempotency key for it yet, so a repeated request would start a second scan. A transport error or a
gateway status on the upload leaves its outcome unknown, and the client says so instead of sending it
again. Reads (the scan, its findings, its check and its exports) are GETs and are retried on transport
errors and transient statuses within one deadline.

The findings, check and export routes are read when the server has them. A server without one answers
FastAPI's bare ``404 {"detail": "Not Found"}``, which the client reports as the route being absent
(``None``); any other 404 is an error.

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
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from martian._errors import message, reference
from martian.config import MartianConfig

ScanState = Literal["queued", "running", "completed", "failed"]
TERMINAL: frozenset[str] = frozenset({"completed", "failed"})
TRANSIENT = frozenset({429, 500, 502, 503, 504})
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


class FindingPage(_Read):
    items: list[Finding]
    next_cursor: str | None = None


class Check(_Read):
    passed: bool
    blocking: list[str] = Field(default_factory=list)
    coverage_shortfalls: list[str] = Field(default_factory=list)

    @field_validator("passed", mode="before")
    @classmethod
    def boolean(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("passed must be a boolean")
        return value


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

    def upload(self, manifest: dict[str, Any], archive: bytes) -> Scan:
        """``POST /v1/scanner/scans``, sent once: the ``manifest`` part, then the ``archive`` part."""
        files: dict[str, tuple[str | None, bytes | str, str]] = {
            "manifest": (None, json.dumps(manifest, separators=(",", ":")), "application/json"),
            "archive": ("bundle.zip", archive, "application/zip"),
        }
        try:
            response = self._http.post("/v1/scanner/scans", files=files)
        except httpx.TransportError:
            raise ScanApiError(
                "connection_error",
                explanation=(
                    "The upload did not get an answer, so whether the server queued a scan is unknown. "
                    "It was not sent again."
                ),
            ) from None
        if response.status_code != 201:
            error = self._error(response)
            if response.status_code in (500, 502, 503, 504):
                error.args = (str(error) + " Whether the server queued a scan is unknown; it was not sent again.",)
            raise error
        return self._parse(response, Scan)  # type: ignore[no-any-return]

    # --- reads -----------------------------------------------------------------------------------

    def _get(
        self, path: str, *, params: dict[str, str | int] | None = None, deadline: float | None = None
    ) -> httpx.Response:
        """A GET, retried on transport errors and transient statuses: four attempts, or until
        ``deadline`` (a ``clock`` value) when one is given."""
        attempt = 0
        while True:
            attempt += 1
            last: ScanApiError
            delay = min(10.0, 0.5 * 2 ** (attempt - 1))
            try:
                response = self._http.get(path, params=params)
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

    def check(self, scan_id: UUID) -> Check | None:
        """The server's gate for a completed scan, or None when the server has no gate."""
        response = self._get(f"/v1/scanner/scans/{scan_id}/check")
        if _absent(response):
            return None
        if response.status_code != 200:
            raise self._error(response)
        return self._parse(response, Check)  # type: ignore[no-any-return]

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
