"""Version-pinned framework maps. Compatibility subsets, not live feeds."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from martian_redteam.catalog.vulnerabilities import CATALOG_VERSION, Vulnerability, get_vulnerability


class FrameworkMap(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    label: str
    source_revision: str
    coverage: str
    vulnerability_ids: tuple[str, ...]
    notes: str


FRAMEWORKS: dict[str, FrameworkMap] = {
    "owasp_llm_2025": FrameworkMap(
        id="owasp_llm_2025",
        label="OWASP LLM Top 10 (2025 compatibility subset)",
        source_revision="2025",
        coverage="subset",
        vulnerability_ids=(
            "prompt_leakage",
            "excessive_agency",
            "pii_leakage",
            "ssrf",
            "sql_injection",
            "shell_injection",
            "rbac",
        ),
        notes=(
            "Not a complete OWASP LLM Top 10 implementation. Maps prompt leakage and "
            "PII leakage toward sensitive-information disclosure, excessive agency toward "
            "LLM06, ssrf/sql_injection/shell_injection toward improper output handling, "
            "and rbac toward authorization/agency risks. Prompt injection is an attack "
            "method, not a vulnerability row."
        ),
    ),
    "owasp_agentic_2026": FrameworkMap(
        id="owasp_agentic_2026",
        label="OWASP Agentic Top 10 (2026 compatibility subset)",
        source_revision="2026",
        coverage="subset",
        vulnerability_ids=("excessive_agency", "prompt_leakage"),
        notes=(
            "Placeholder subset for agentic agency and instruction-leakage risks. "
            "Multi-turn agent tool-abuse coverage is Phase 3."
        ),
    ),
    "mitre_atlas_2026": FrameworkMap(
        id="mitre_atlas_2026",
        label="MITRE ATLAS (2026 compatibility subset)",
        source_revision="2026",
        coverage="subset",
        vulnerability_ids=("ssrf", "prompt_leakage", "excessive_agency"),
        notes=(
            "Placeholder subset. Not a full MITRE ATLAS mapping or scoring implementation. "
            "Includes a few catalog ids that loosely correspond to prompt extraction, "
            "agency/tool misuse, and server-side request risks."
        ),
    ),
}


def vulnerabilities_for(framework_id: str) -> list[Vulnerability]:
    """Instantiate catalog vulnerabilities for a framework map using default types."""
    try:
        framework = FRAMEWORKS[framework_id]
    except KeyError:
        raise KeyError(f"unknown framework: {framework_id}") from None
    return [get_vulnerability(vulnerability_id) for vulnerability_id in framework.vulnerability_ids]


# Catalog version is the SDK map version, not the upstream standard edition.
assert CATALOG_VERSION
