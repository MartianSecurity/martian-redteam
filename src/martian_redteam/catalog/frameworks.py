"""Version-pinned framework maps. Compatibility subsets, not live feeds."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from martian_redteam.catalog.vulnerabilities import CATALOG_VERSION


class FrameworkMap(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    label: str
    source_revision: str
    coverage: str
    vulnerability_ids: tuple[str, ...]


FRAMEWORKS: dict[str, FrameworkMap] = {
    "owasp_llm_2025": FrameworkMap(
        id="owasp_llm_2025",
        label="OWASP LLM Top 10 (2025 compatibility subset)",
        source_revision="2025",
        coverage="subset",
        vulnerability_ids=("prompt_leakage", "excessive_agency", "pii_leakage"),
    ),
}

# Catalog version is the SDK map version, not the upstream standard edition.
assert CATALOG_VERSION
