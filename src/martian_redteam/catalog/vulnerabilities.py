"""Built-in vulnerability ids, types, and gate policy."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from pydantic import BaseModel, ConfigDict, Field

CATALOG_VERSION = "0.2.0"


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


def Bias(types: Sequence[str] | None = None) -> Vulnerability:
    return _vuln("bias", "Bias", ("religion", "politics", "gender", "race")).with_types(types)


def Misinformation(types: Sequence[str] | None = None) -> Vulnerability:
    return _vuln(
        "misinformation",
        "Misinformation",
        ("factual_errors", "unsupported_claims", "expertize_misrepresentation"),
    ).with_types(types)


def RBAC(types: Sequence[str] | None = None) -> Vulnerability:
    return _vuln(
        "rbac",
        "RBAC",
        ("role_bypass", "privilege_escalation", "unauthorized_role_assumption"),
        gated=True,
    ).with_types(types)


def SSRF(types: Sequence[str] | None = None) -> Vulnerability:
    return _vuln(
        "ssrf",
        "SSRF",
        ("internal_service_access", "cloud_metadata_access", "port_scanning"),
        gated=True,
    ).with_types(types)


def SQLInjection(types: Sequence[str] | None = None) -> Vulnerability:
    return _vuln(
        "sql_injection",
        "SQL injection",
        ("blind_sql_injection", "union_based_injection", "error_based_injection"),
        gated=True,
    ).with_types(types)


def ShellInjection(types: Sequence[str] | None = None) -> Vulnerability:
    return _vuln(
        "shell_injection",
        "Shell injection",
        ("command_injection", "system_command_execution", "shell_escape_sequences"),
        gated=True,
    ).with_types(types)


def Robustness(types: Sequence[str] | None = None) -> Vulnerability:
    return _vuln("robustness", "Robustness", ("input_overreliance", "hijacking")).with_types(types)


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
    "Bias": Bias,
    "Misinformation": Misinformation,
    "RBAC": RBAC,
    "SSRF": SSRF,
    "SQLInjection": SQLInjection,
    "ShellInjection": ShellInjection,
    "Robustness": Robustness,
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
    """Resolve a YAML or Python name to a vulnerability.

    ``gated`` overrides the catalog gate policy when it is not ``None``, for
    built-ins and custom vulnerabilities alike. ``criteria`` is only valid for
    custom vulnerabilities: built-in rubrics live in martian-agents, so a
    criteria string on a built-in would be silently unused.
    """
    key = name.strip().lower().replace("-", "_")
    if key in {"custom", "customvulnerability"}:
        if id is None or criteria is None:
            raise ValueError("custom vulnerability requires id and criteria")
        return CustomVulnerability(id=id, criteria=criteria, types=types or ("custom",), gated=bool(gated))
    factory = _VULNERABILITY_LOOKUP.get(key)
    if factory is None:
        raise KeyError(f"unknown vulnerability: {name}")
    if criteria is not None:
        raise ValueError("criteria is only valid for custom vulnerabilities")
    vulnerability = factory(types)
    if gated is None:
        return vulnerability
    # An explicit gate choice must win over the catalog default; dropping it
    # would leave a customer-gated type report-only and the build green.
    return vulnerability.model_copy(update={"gated": gated})
