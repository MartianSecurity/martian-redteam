"""The red team catalogue: vulnerabilities, attack methods and framework maps.

The catalogue lives in :mod:`martian_redteam.catalog`, where existing code imports it; this module is
its home in the red team core. Each built-in vulnerability is a catalogue 1.0 entry
(:class:`CatalogEntry`, in :data:`ENTRIES`): its objective class, the forms that decide it with their
checks, its default severity, its framework tags, and its source and licence. ``docs/catalog.md``
describes the format and the rule for its version.
"""

from martian_redteam.catalog import (
    ATTACKS,
    CATALOG_VERSION,
    FRAMEWORKS,
    RBAC,
    ROT13,
    SSRF,
    VULNERABILITIES,
    Base64,
    Bias,
    CustomVulnerability,
    ExcessiveAgency,
    IllegalActivity,
    Leetspeak,
    Misinformation,
    PIILeakage,
    PromptInjection,
    PromptLeakage,
    PromptProbing,
    Robustness,
    Roleplay,
    ShellInjection,
    SQLInjection,
    Toxicity,
    get_attack,
    get_vulnerability,
    vulnerabilities_for,
)
from martian_redteam.catalog.attacks import Attack
from martian_redteam.catalog.entries import ENTRIES, CatalogEntry, Form, entry, find_entry
from martian_redteam.catalog.vulnerabilities import Vulnerability

__all__ = [
    "ATTACKS",
    "CATALOG_VERSION",
    "ENTRIES",
    "FRAMEWORKS",
    "RBAC",
    "ROT13",
    "SSRF",
    "VULNERABILITIES",
    "Attack",
    "Base64",
    "Bias",
    "CatalogEntry",
    "CustomVulnerability",
    "ExcessiveAgency",
    "Form",
    "IllegalActivity",
    "Leetspeak",
    "Misinformation",
    "PIILeakage",
    "PromptInjection",
    "PromptLeakage",
    "PromptProbing",
    "Robustness",
    "Roleplay",
    "SQLInjection",
    "SSRF",
    "ShellInjection",
    "Toxicity",
    "Vulnerability",
    "entry",
    "find_entry",
    "get_attack",
    "get_vulnerability",
    "vulnerabilities_for",
]
