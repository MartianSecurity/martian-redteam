"""Aegra Agent Protocol helpers for POST /runs/wait.

The SDK speaks the same stateless wait endpoint the martian-agents smoke test uses.
Aegra returns graph state as JSON, with periodic ``\\n`` heartbeat bytes before the
final object. Some protocol envelopes wrap that state in ``output`` or ``values``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

_GRAPH_MARKERS = frozenset({"attack", "attack_method", "score", "reason", "messages"})
_FAILED_STATUSES = frozenset({"error", "failed", "timeout", "cancelled", "interrupted"})


def parse_wait_body(text: str) -> dict[str, Any]:
    """Decode a wait response, ignoring leading heartbeat whitespace."""
    stripped = text.strip()
    if not stripped:
        raise ValueError("wait response body is empty")
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        payload = _last_json_value(stripped)
    if not isinstance(payload, dict):
        raise ValueError("wait response is not a JSON object")
    return payload


def _last_json_value(text: str) -> Any:
    decoder = json.JSONDecoder()
    index = 0
    last: Any = None
    while index < len(text):
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text):
            break
        last, index = decoder.raw_decode(text, index)
    if last is None:
        raise ValueError("wait response contains no JSON object")
    return last


def graph_state(payload: dict[str, Any]) -> dict[str, Any]:
    """Return graph state from a wait body or a nested protocol envelope."""
    for key in ("output", "values"):
        nested = payload.get(key)
        if isinstance(nested, dict) and _GRAPH_MARKERS.intersection(nested):
            return nested
    return payload


def failed_run_status(payload: dict[str, Any]) -> str | None:
    status = payload.get("status")
    if isinstance(status, str) and status.lower() in _FAILED_STATUSES:
        return status
    return None


def parse_retry_after(value: str | None) -> float | None:
    """Parse a Retry-After header as seconds. Returns None when missing or invalid."""
    if value is None or not value.strip():
        return None
    raw = value.strip()
    try:
        seconds = float(raw)
    except ValueError:
        try:
            when = parsedate_to_datetime(raw)
        except (TypeError, ValueError, IndexError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = (when - datetime.now(tz=UTC)).total_seconds()
    return max(0.0, seconds)


def extract_graph_revision(state: dict[str, Any]) -> str | None:
    revision = state.get("graph_revision")
    return revision if isinstance(revision, str) and revision else None
