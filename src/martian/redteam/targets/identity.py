"""Stable target identities for grouping recorded findings."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

NETWORK_TARGETS = frozenset({"http", "mcp"})
_ENDPOINT_PREFIX = "endpoint-v1:"


def identity_digest(*parts: str) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=True, separators=(",", ":")).encode("ascii")).hexdigest()


def known_route(kind: str | None, adapter: str | None, *, linked_archive: bool = False) -> bool:
    """A saved target ID alone cannot establish which network route produced the evidence."""
    return kind not in NETWORK_TARGETS or bool(adapter and (linked_archive or adapter.startswith(_ENDPOINT_PREFIX)))


def recorded_target_keys(provenance: Mapping[str, Any], kind: str | None) -> tuple[str, str]:
    """Return the saved target ID and adapter identity recorded by a scan."""
    launch = provenance.get("launch")
    saved = launch.get("target_id") if isinstance(launch, dict) else None
    value = provenance.get("findings_target")
    adapter = value if isinstance(value, str) else ""
    source = provenance.get("promptfoo")
    linked_archive = isinstance(source, dict) and bool(source.get("target_key"))
    if not known_route(kind, adapter, linked_archive=linked_archive):
        return "", ""
    return (saved if isinstance(saved, str) else "", adapter)


def endpoint_identity(kind: str, url: str, *parts: str, has_headers: bool = False) -> tuple[str, str]:
    """An endpoint digest and host label when the address establishes the route."""
    try:
        parsed = urlsplit(url)
        host, port = parsed.hostname, parsed.port
    except ValueError:
        return "", ""
    if not host or not parsed.scheme:
        return "", ""
    default_port = {"http": 80, "https": 443}.get(parsed.scheme)
    name = f"[{host}]" if ":" in host else host
    if port is not None and port != default_port:
        name = f"{name}:{port}"
    if has_headers or parsed.query or parsed.username is not None or parsed.password is not None:
        return "", name
    return _ENDPOINT_PREFIX + identity_digest(kind, parsed.scheme, name, parsed.path or "/", *parts), name
