"""A cache of the model calls a suite-file run makes, so that a second identical run makes none.

    martian evals run evals.yaml --cache

A run sends two kinds of model call: the target's, a URL target's ``POST`` (and a function
target's call when the suite opts in, since a function is your code and may do more than answer),
and the LLM judges', through :func:`martian.evals._judge_transport.invoke_response_judge`. With
the cache on, each successful call's reply is stored under a SHA-256 digest of its full request,
and the same request in a later run is answered from the store instead of the model.

- **The key** is everything that decides the reply. For a URL target: its address, its headers,
  its body with the case's values and the combination's filled in, and its ``response_path``.
  For a function target: its file's SHA-256, the function's name and its arguments. For a judge:
  its model, the address serving it, the messages (its prompt with the criteria, the request and
  the reply under test), the reply schema, its inference parameters and headers, the stage and
  the judge's version. Never a credential: a header or query parameter whose name says it holds
  one (:func:`is_credential`) is left out, and ``{{uuid}}`` counts as written, not as the new id
  each call sends.
- **Errors are never stored.** A call that fails raises as it does without the cache, and the
  next run makes it again.
- **Attempts.** Within one invocation the nth identical request is attempt n and has its own
  entry, so a case's repeats sample the model as they do without the cache, and a first run
  makes exactly the calls it makes without it.
- **Time to live.** An entry older than ``ttl_seconds`` is not used; the next successful call
  replaces it.

An entry is a JSON file named by its key's digest, holding the reply, when it was stored, and
its kind. It holds no request, header or key. The folder and its files are readable by their
owner only.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Literal, TypeVar
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from martian.config import ConfigError

FORMAT = "martian-evals-cache/1"
FOLDER = "cache"
"""The cache's folder under the local data folder, when the suite file names none."""
DEFAULT_TTL_SECONDS = 7 * 24 * 3600.0
MAX_TTL_SECONDS = 366 * 24 * 3600.0
HIT_KEY = "martian.cache"
"""Judge feedback metadata that marks a judgment replayed from the cache, with the value ``hit``."""

Kind = Literal["target", "judge"]
KINDS: tuple[Kind, ...] = ("target", "judge")
T = TypeVar("T")

# A header or query parameter is a credential when its name holds one of these, or is one of the
# short names after them (a signature, a function key, a password).
_CREDENTIAL_WORDS = ("auth", "key", "token", "secret", "passw", "credential", "cookie", "session", "signature")
_CREDENTIAL_NAMES = frozenset({"sig", "code", "pwd"})


class CacheError(RuntimeError):
    """The cache could not store a reply. The message names the setting to check, never a path's
    contents or a reply."""


def is_credential(name: str) -> bool:
    """Whether a header or query parameter of this name holds a credential, so a cache key leaves
    it out: ``Authorization``, ``Cookie``, ``X-API-Key``, ``api_key``, ``X-Auth-Token``, ``sig``."""
    lowered = name.strip().lower()
    return lowered in _CREDENTIAL_NAMES or any(word in lowered for word in _CREDENTIAL_WORDS)


def key_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Headers as a cache key holds them: names in lower case, credential headers left out."""
    return {name.lower(): value for name, value in headers.items() if not is_credential(name)}


def key_address(url: str) -> str:
    """An address as a cache key holds it: scheme, host, port, path and the query parameters that
    are not credentials. No user name or password, and no fragment."""
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        port = None
    host = (parts.hostname or "") + (f":{port}" if port is not None else "")
    query = [(name, value) for name, value in parse_qsl(parts.query, keep_blank_values=True) if not is_credential(name)]
    return urlunsplit((parts.scheme.lower(), host, parts.path, urlencode(query), ""))


def digest(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def exact_json(value: Any) -> bool:
    """Whether ``value`` reads back from JSON as itself, so a stored reply is the reply. A tuple,
    a key that is not text, NaN or an object JSON cannot hold is not."""
    try:
        return bool(json.loads(json.dumps(value, allow_nan=False)) == value)
    except (TypeError, ValueError):
        return False


def _empty() -> dict[Kind, dict[str, int]]:
    return {kind: {"hits": 0, "calls": 0, "stored": 0} for kind in KINDS}


class ModelCallCache:
    """The cache in ``folder``. ``functions`` says whether a function target's calls are cached.

    One instance serves one invocation: it numbers identical requests across it, and counts each
    run's hits and calls from :meth:`begin`, so each combination of a matrix reports its own."""

    def __init__(self, folder: Path, *, ttl_seconds: float = DEFAULT_TTL_SECONDS, functions: bool = False) -> None:
        if not 0 < ttl_seconds <= MAX_TTL_SECONDS:
            raise ConfigError(f"cache.ttl_seconds must be more than 0 and at most {MAX_TTL_SECONDS:.0f}")
        try:
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as error:
            raise ConfigError(f"could not open the cache folder {folder}: {type(error).__name__}") from None
        self.folder = folder
        self.ttl_seconds = ttl_seconds
        self.functions = functions
        self._lock = threading.Lock()
        self._seen: dict[str, int] = {}
        self._counts = _empty()

    def settings(self) -> dict[str, Any]:
        """The cache a run used, as its provenance and summary name it."""
        return {"folder": str(self.folder), "ttl_seconds": self.ttl_seconds, "functions": self.functions}

    def begin(self) -> None:
        """Count a new run's hits and calls from zero. Attempts keep their numbers."""
        with self._lock:
            self._counts = _empty()

    def counts(self) -> dict[Kind, dict[str, int]]:
        """This run's ``hits`` (replies from the cache), ``calls`` (requests sent to the model)
        and ``stored`` (replies written to the cache) of each kind."""
        with self._lock:
            return {kind: dict(found) for kind, found in self._counts.items()}

    def call(
        self,
        kind: Kind,
        request: Mapping[str, Any],
        fresh: Callable[[], T],
        *,
        store: Callable[[T], Any] = lambda reply: reply,
        replay: Callable[[Any], T] = lambda stored: stored,
    ) -> T:
        """The reply to ``request``: from the cache when it holds a live entry for this attempt of
        it, else ``fresh()``, stored as ``store`` gives it unless that is None. ``replay`` turns a
        stored reply back into one. Whatever ``fresh`` raises is raised, and nothing is stored."""
        path, found = self.lookup(kind, request)
        if found is not None:
            return replay(found)
        reply = fresh()
        self.keep(kind, path, store(reply))
        return reply

    def lookup(self, kind: Kind, request: Mapping[str, Any]) -> tuple[Path, Any]:
        """The entry of this attempt of ``request`` and its stored reply, None when it has no live
        one. Counts a hit, or a call that the caller then makes and passes to :meth:`keep`."""
        identity = digest({"kind": kind, "request": request})
        with self._lock:
            attempt = self._seen[identity] = self._seen.get(identity, 0) + 1
        key = digest({"format": FORMAT, "request": identity, "attempt": attempt})
        path = self.folder / key[:2] / f"{key}.json"
        found = self._read(path, kind)
        self._count(kind, "calls" if found is None else "hits")
        return path, found

    def keep(self, kind: Kind, path: Path, stored: Any) -> None:
        """Store a successful call's reply at the entry :meth:`lookup` gave, unless it is None."""
        if stored is not None:
            self._write(path, kind, stored)
            self._count(kind, "stored")

    def _count(self, kind: Kind, what: str) -> None:
        with self._lock:
            self._counts[kind][what] += 1

    def _read(self, path: Path, kind: Kind) -> Any:
        """The stored reply, or None when there is no live entry. An entry that cannot be read is
        no entry: the next successful call replaces it."""
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(entry, dict) or entry.get("format") != FORMAT or entry.get("kind") != kind:
            return None
        stored_at = entry.get("stored_at")
        if not isinstance(stored_at, (int, float)) or isinstance(stored_at, bool):
            return None
        if time.time() - stored_at > self.ttl_seconds:
            return None
        return entry.get("reply")

    def _write(self, path: Path, kind: Kind, reply: Any) -> None:
        """Write the entry to a new file only its owner can read, renamed over ``path``."""
        entry = {"format": FORMAT, "kind": kind, "stored_at": time.time(), "reply": reply}
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
        try:
            path.parent.mkdir(mode=0o700, exist_ok=True)
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                    json.dump(entry, file, ensure_ascii=False, allow_nan=False)
                os.replace(temporary, path)
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
        except (OSError, TypeError, ValueError) as error:
            raise CacheError(f"the cache could not store a reply: {type(error).__name__}; check cache.folder") from None


__all__ = [
    "DEFAULT_TTL_SECONDS",
    "FOLDER",
    "FORMAT",
    "HIT_KEY",
    "MAX_TTL_SECONDS",
    "CacheError",
    "ModelCallCache",
    "exact_json",
    "is_credential",
    "key_address",
    "key_headers",
]
