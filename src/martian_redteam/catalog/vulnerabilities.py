"""Built-in vulnerability ids, types, and gate policy."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from pydantic import BaseModel, ConfigDict, Field

CATALOG_VERSION = "0.1.0"


class Vulnerability(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    label: str
    allowed_types: tuple[str, ...]
    gated: bool = False
    types: tuple[str, ...] = Field(default_factory=tuple)
    criteria: str | None = None

    def with_types(self, types: Sequence[str] | None) -> Vulnerability:
        selected = tuple(types) if types else self.allowed_types
        unknown = [item for item in selected if item not in self.allowed_types]
        if unknown:
            raise ValueError(f"{self.id} does not allow types {unknown}")
        return self.model_copy(update={"types": selected})


def _vuln(id: str, label: str, allowed: tuple[str, ...], *, gated: bool = False) -> Vulnerability:
    return Vulnerability(id=id, label=label, allowed_types=allowed, gated=gated, types=allowed)


def PromptLeakage(types: Sequence[str] | None = None) -> Vulnerability:
    return _vuln(
        "prompt_leakage",
        "Prompt leakage",
        ("system_prompt", "instructions", "credentials"),
        gated=True,
    ).with_types(types)


def ExcessiveAgency(types: Sequence[str] | None = None) -> Vulnerability:
    return _vuln(
        "excessive_agency",
        "Excessive agency",
        ("functionality", "permissions", "autonomy"),
        gated=True,
    ).with_types(types)


def Toxicity(types: Sequence[str] | None = None) -> Vulnerability:
    return _vuln("toxicity", "Toxicity", ("insults", "profanity", "hate")).with_types(types)


def IllegalActivity(types: Sequence[str] | None = None) -> Vulnerability:
    return _vuln(
        "illegal_activity",
        "Illegal activity",
        ("violent_crimes", "cybercrime", "fraud"),
    ).with_types(types)


def PIILeakage(types: Sequence[str] | None = None) -> Vulnerability:
    return _vuln(
        "pii_leakage",
        "PII leakage",
        ("direct_disclosure", "session_leak", "database_access"),
    ).with_types(types)


def CustomVulnerability(
    *,
    id: str,
    criteria: str,
    types: Sequence[str] = ("custom",),
    gated: bool = False,
    label: str | None = None,
) -> Vulnerability:
    """Customer-authored vulnerability. The evaluator treats `criteria` as authoritative."""
    if not id.strip():
        raise ValueError("custom vulnerability id must be non-empty")
    if not criteria.strip():
        raise ValueError("custom vulnerability criteria must be non-empty")
    selected = tuple(types) if types else ("custom",)
    return Vulnerability(
        id=id,
        label=label or id,
        allowed_types=selected,
        gated=gated,
        types=selected,
        criteria=criteria,
    )


VULNERABILITIES: dict[str, Callable[..., Vulnerability]] = {
    "PromptLeakage": PromptLeakage,
    "ExcessiveAgency": ExcessiveAgency,
    "Toxicity": Toxicity,
    "IllegalActivity": IllegalActivity,
    "PIILeakage": PIILeakage,
}

_VULNERABILITY_LOOKUP: dict[str, Callable[..., Vulnerability]] = {}
for _name, _factory in VULNERABILITIES.items():
    _VULNERABILITY_LOOKUP[_name.lower()] = _factory
    _VULNERABILITY_LOOKUP[_factory().id] = _factory


def get_vulnerability(
    name: str,
    types: Sequence[str] | None = None,
    *,
    id: str | None = None,
    criteria: str | None = None,
    gated: bool | None = None,
) -> Vulnerability:
    key = name.strip().lower().replace("-", "_")
    if key in {"custom", "customvulnerability"}:
        if id is None or criteria is None:
            raise ValueError("custom vulnerability requires id and criteria")
        return CustomVulnerability(id=id, criteria=criteria, types=types or ("custom",), gated=bool(gated))
    factory = _VULNERABILITY_LOOKUP.get(key)
    if factory is None:
        raise KeyError(f"unknown vulnerability: {name}")
    return factory(types)
