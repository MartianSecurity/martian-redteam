"""Martian Security SDK for adversarial testing of AI agents."""

from martian_redteam.callback import wrap_callback
from martian_redteam.catalog import (
    ROT13,
    Base64,
    CustomVulnerability,
    ExcessiveAgency,
    IllegalActivity,
    Leetspeak,
    PIILeakage,
    PromptInjection,
    PromptLeakage,
    Toxicity,
)
from martian_redteam.client import Client, ClientError, RetryableClientError
from martian_redteam.gate import GateFailure, evaluate_gate
from martian_redteam.orchestrator import a_red_team, red_team
from martian_redteam.types import RiskAssessment, TestCase, Turn

__version__ = "0.1.0"

__all__ = [
    "Base64",
    "Client",
    "ClientError",
    "CustomVulnerability",
    "ExcessiveAgency",
    "GateFailure",
    "IllegalActivity",
    "Leetspeak",
    "PIILeakage",
    "PromptInjection",
    "PromptLeakage",
    "ROT13",
    "RetryableClientError",
    "RiskAssessment",
    "TestCase",
    "Toxicity",
    "Turn",
    "a_red_team",
    "evaluate_gate",
    "red_team",
    "wrap_callback",
    "__version__",
]
