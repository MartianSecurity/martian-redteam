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


VULNERABILITIES: dict[str, Callable[..., Vulnerability]] = {
    "PromptLeakage": PromptLeakage,
    "ExcessiveAgency": ExcessiveAgency,
    "Toxicity": Toxicity,
    "IllegalActivity": IllegalActivity,
    "PIILeakage": PIILeakage,
}


def get_vulnerability(name: str, types: Sequence[str] | None = None) -> Vulnerability:
    factory = VULNERABILITIES.get(name)
    if factory is None:
        raise KeyError(f"unknown vulnerability: {name}")
    return factory(types)
