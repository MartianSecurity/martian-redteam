"""Remove transport settings from portable Evals records and mask known credential values."""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from typing import Any
from urllib.parse import parse_qsl, quote, quote_plus, unquote, urlsplit, urlunsplit

_HEADERS = {"headers", "judge_headers", "simulator_headers"}
_URLS = {"url", "endpoint", "mlflow_tracking_uri", "base_url"}
_REFERENCE = re.compile(r"\$\{([a-zA-Z_][a-zA-Z_0-9]*)\}")
_MASK = "[redacted]"
_SECRET_FIELDS = {"authorization", "cookie", "set_cookie", "api_key", "access_token", "token", "password", "secret"}


def _secret_field(name: str) -> bool:
    return name in _SECRET_FIELDS or name.endswith(("_secret", "_password", "_api_key", "_token"))


def _address_field(name: str, value: str) -> bool:
    return name in _URLS or (name == "source" and value.startswith(("postgresql://", "postgresql+", "postgres://")))


def _address(value: str) -> tuple[str, Iterator[str]]:
    try:
        parts = urlsplit(value)
    except ValueError:
        return "https://redacted.invalid", iter((value,))
    cleaned = urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1], parts.path, "", ""))
    secrets = (parts.username, parts.password, parts.fragment, *(v for _, v in parse_qsl(parts.query)))
    return cleaned, (form for secret in secrets if secret for form in (secret, unquote(secret)))


def _secrets(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            key = key.lower().replace("-", "_")
            if isinstance(item, str) and _address_field(key, item):
                yield from _address(item)[1]
            elif key in _HEADERS and isinstance(item, dict):
                for name, header in item.items():
                    if not isinstance(header, str):
                        continue
                    yield header
                    scheme, _, credential = header.partition(" ")
                    if scheme.lower() in {"bearer", "basic"}:
                        yield credential
                    if name.lower() in {"cookie", "set-cookie"}:
                        for cookie in header.split(";"):
                            _, _, credential = cookie.partition("=")
                            yield credential.strip()
            elif _secret_field(key) and isinstance(item, str):
                yield item
            else:
                yield from _secrets(item)
    elif isinstance(value, list):
        for item in value:
            yield from _secrets(item)


class ShareRedaction:
    def __init__(self) -> None:
        self._short: set[str] = set()
        self._pattern: re.Pattern[str] | None = None

    @classmethod
    def from_config(cls, *configurations: Any) -> ShareRedaction:
        redaction = cls()
        redaction.collect(*configurations)
        return redaction

    def collect(self, *configurations: Any) -> None:
        """Collect literal settings before projecting their config or related evidence."""
        values = {secret for config in configurations for secret in _secrets(config) if secret and secret != _MASK}
        values.update(
            value for secret in tuple(values) for name in _REFERENCE.findall(secret) if (value := os.environ.get(name))
        )
        # Keep short values out of substring matching so an ordinary one-letter header value
        # cannot erase readable evidence. Whole-value copies are still masked.
        self._short = {value for value in values if len(value) < 4}
        terms = {
            variant for value in values - self._short for variant in (value, quote(value, safe=""), quote_plus(value))
        }
        self._pattern = (
            re.compile("|".join(map(re.escape, sorted(terms, key=lambda term: (-len(term), term))))) if terms else None
        )

    def text(self, value: str) -> str:
        if value in self._short:
            return _MASK
        return self._pattern.sub(_MASK, value) if self._pattern else value

    def evidence(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {self.text(key): self.evidence(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.evidence(item) for item in value]
        return value

    def configuration(self, value: Any) -> Any:
        if isinstance(value, dict):
            projected: dict[str, Any] = {}
            for key, item in value.items():
                name = key.lower().replace("-", "_")
                if name in _HEADERS and isinstance(item, dict):
                    projected[key] = {}
                elif isinstance(item, str) and _address_field(name, item):
                    address, _ = _address(item)
                    projected[key] = address if self.text(address) == address else "https://redacted.invalid"
                elif _secret_field(name) and isinstance(item, str):
                    projected[key] = _MASK
                else:
                    projected[key] = self.configuration(item)
            return projected
        if isinstance(value, list):
            return [self.configuration(item) for item in value]
        return self.evidence(value)

    def target(self, target: dict[str, Any] | None) -> dict[str, Any] | None:
        if not target or target.get("kind") != "http":
            return target
        return self.fields(target, (), ("headers", "url", "body"))

    def scorer(self, scorer: dict[str, Any]) -> dict[str, Any]:
        return self.fields(scorer, (), ("endpoint", "panel", "parameters", "criteria"))

    def fields(self, record: dict[str, Any], fields: tuple[str, ...], config: tuple[str, ...] = ()) -> dict[str, Any]:
        projected = dict(record)
        projected.update({key: self.evidence(record[key]) for key in fields if key in record})
        projected.update(self.configuration({key: record[key] for key in config if key in record}))
        return projected

    def run(self, run: dict[str, Any]) -> dict[str, Any]:
        result = self.fields(run, ("code", "error"), ("provenance",))
        if (
            run.get("kind") == "redteam"
            and isinstance(header := result["provenance"].get("redteam"), dict)
            and isinstance(provenance := header.get("provenance"), dict)
        ):
            provenance.pop("launch", None)
        result["target"] = self.target(run.get("target"))
        result["scorers"] = [self.scorer(scorer) for scorer in run["scorers"]]
        return result

    def suite(self, suite: dict[str, Any]) -> dict[str, Any]:
        result = self.fields(suite, (), ("settings", "matrix", "simulator"))
        result["target"] = self.target(suite.get("target"))
        result["scorers"] = [self.scorer(scorer) for scorer in suite["scorers"]]
        return result

    def score(self, score: dict[str, Any]) -> dict[str, Any]:
        return self.fields(score, ("value", "rationale", "error", "metadata", "source_id"))

    def job(self, job: dict[str, Any]) -> dict[str, Any]:
        projected = self.fields(job, ("error",))
        projected["scorers"] = [self.scorer(scorer) for scorer in job["scorers"]]
        return projected

    def result(self, result: dict[str, Any]) -> dict[str, Any]:
        fields = ("inputs", "expectations", "output", "error", "scores")
        projected = self.fields(result, fields)
        if "turns" in result:
            projected["turns"] = [self.fields(turn, fields) for turn in result["turns"]]
        if "attempts" in result:
            projected["attempts"] = [self.result(attempt) for attempt in result["attempts"]]
        return projected

    def trace(self, trace: dict[str, Any]) -> dict[str, Any]:
        projected = self.fields(trace, ("name", "request", "response", "tags", "metadata"))
        projected["scores"] = [self.score(score) for score in trace["scores"]]
        projected["spans"] = [self.fields(span, ("name", "inputs", "outputs", "attributes")) for span in trace["spans"]]
        return projected
