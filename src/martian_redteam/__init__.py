"""Martian Security SDK for adversarial testing of AI agents."""

from martian_redteam.callback import wrap_callback
from martian_redteam.catalog import (
    RBAC,
    ROT13,
    SSRF,
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
)
from martian_redteam.client import Client, ClientError, RetryableClientError
from martian_redteam.gate import GateFailure, evaluate_gate
from martian_redteam.integration import AssessmentSession, MCPConnection, TraceRecorder, bind_tools
from martian_redteam.orchestrator import a_red_team, red_team
from martian_redteam.types import RiskAssessment, TestCase, Turn
from martian_redteam.visibility import ToolActivity, VisibilityClient

__version__ = "0.1.0"

__all__ = [
    "ToolActivity",
    "VisibilityClient",
    "AssessmentSession",
    "MCPConnection",
    "TraceRecorder",
    "bind_tools",
    "Base64",
    "Bias",
    "Client",
    "ClientError",
    "CustomVulnerability",
    "ExcessiveAgency",
    "GateFailure",
    "IllegalActivity",
    "Leetspeak",
    "Misinformation",
    "PIILeakage",
    "PromptInjection",
    "PromptLeakage",
    "PromptProbing",
    "RBAC",
    "ROT13",
    "RetryableClientError",
    "RiskAssessment",
    "Robustness",
    "Roleplay",
    "SQLInjection",
    "SSRF",
    "ShellInjection",
    "TestCase",
    "Toxicity",
    "Turn",
    "a_red_team",
    "evaluate_gate",
    "red_team",
    "wrap_callback",
    "__version__",
]
