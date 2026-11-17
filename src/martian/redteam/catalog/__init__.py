"""The red team catalogue: vulnerabilities, attack methods and framework maps.

Catalogue 0.2 lives in :mod:`martian_redteam.catalog`, where existing code imports it; this module is
its home in the red team core. Each vulnerability's objective class is in
:data:`martian.redteam.model.maps.OBJECTIVES`.
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
from martian_redteam.catalog.vulnerabilities import Vulnerability

__all__ = [
    "ATTACKS",
    "CATALOG_VERSION",
    "FRAMEWORKS",
    "RBAC",
    "ROT13",
    "SSRF",
    "VULNERABILITIES",
    "Attack",
    "Base64",
    "Bias",
    "CustomVulnerability",
    "ExcessiveAgency",
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
    "get_attack",
    "get_vulnerability",
    "vulnerabilities_for",
]
