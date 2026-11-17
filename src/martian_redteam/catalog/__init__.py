"""Vulnerability and attack catalog: catalogue 1.0 entries (:mod:`.entries`), attack ids and framework maps.

The generator and judge prompts are in :mod:`martian.redteam.catalog.prompts`."""

from martian_redteam.catalog.attacks import (
    ATTACKS,
    ROT13,
    Base64,
    Leetspeak,
    PromptInjection,
    PromptProbing,
    Roleplay,
    get_attack,
)
from martian_redteam.catalog.entries import ENTRIES, CatalogEntry, Form, entry, find_entry
from martian_redteam.catalog.frameworks import FRAMEWORKS, vulnerabilities_for
from martian_redteam.catalog.vulnerabilities import (
    CATALOG_VERSION,
    RBAC,
    SSRF,
    VULNERABILITIES,
    Bias,
    CustomVulnerability,
    ExcessiveAgency,
    IllegalActivity,
    Misinformation,
    PIILeakage,
    PromptLeakage,
    Robustness,
    ShellInjection,
    SQLInjection,
    Toxicity,
    get_vulnerability,
)

__all__ = [
    "ATTACKS",
    "CATALOG_VERSION",
    "ENTRIES",
    "FRAMEWORKS",
    "VULNERABILITIES",
    "Bias",
    "CatalogEntry",
    "CustomVulnerability",
    "Base64",
    "ExcessiveAgency",
    "Form",
    "IllegalActivity",
    "Leetspeak",
    "Misinformation",
    "PIILeakage",
    "PromptInjection",
    "PromptLeakage",
    "PromptProbing",
    "RBAC",
    "ROT13",
    "Robustness",
    "Roleplay",
    "SQLInjection",
    "SSRF",
    "ShellInjection",
    "Toxicity",
    "entry",
    "find_entry",
    "get_attack",
    "get_vulnerability",
    "vulnerabilities_for",
]
