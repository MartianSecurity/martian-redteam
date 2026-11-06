"""Adversarial testing, with customer callbacks and hosted simulation/judging."""

from martian.redteam.client import Client as Client
from martian.redteam.client import RedteamConfig as RedteamConfig
from martian.redteam.client import RedteamOperation as RedteamOperation
from martian.redteam.client import RedteamOperationError as RedteamOperationError
from martian_redteam import (
    RBAC as RBAC,
)
from martian_redteam import (
    ROT13 as ROT13,
)
from martian_redteam import (
    SSRF as SSRF,
)
from martian_redteam import (
    AssessmentSession as AssessmentSession,
)
from martian_redteam import (
    Base64 as Base64,
)
from martian_redteam import (
    Bias as Bias,
)
from martian_redteam import (
    ClientError as ClientError,
)
from martian_redteam import (
    CustomVulnerability as CustomVulnerability,
)
from martian_redteam import (
    ExcessiveAgency as ExcessiveAgency,
)
from martian_redteam import (
    GateFailure as GateFailure,
)
from martian_redteam import (
    IllegalActivity as IllegalActivity,
)
from martian_redteam import (
    Leetspeak as Leetspeak,
)
from martian_redteam import (
    MCPConnection as MCPConnection,
)
from martian_redteam import (
    Misinformation as Misinformation,
)
from martian_redteam import (
    PIILeakage as PIILeakage,
)
from martian_redteam import (
    PromptInjection as PromptInjection,
)
from martian_redteam import (
    PromptLeakage as PromptLeakage,
)
from martian_redteam import (
    PromptProbing as PromptProbing,
)
from martian_redteam import (
    RetryableClientError as RetryableClientError,
)
from martian_redteam import (
    RiskAssessment as RiskAssessment,
)
from martian_redteam import (
    Robustness as Robustness,
)
from martian_redteam import (
    Roleplay as Roleplay,
)
from martian_redteam import (
    ShellInjection as ShellInjection,
)
from martian_redteam import (
    SQLInjection as SQLInjection,
)
from martian_redteam import (
    TestCase as TestCase,
)
from martian_redteam import (
    Toxicity as Toxicity,
)
from martian_redteam import (
    TraceRecorder as TraceRecorder,
)
from martian_redteam import (
    Turn as Turn,
)
from martian_redteam import (
    __all__ as _legacy_exports,
)
from martian_redteam import (
    __version__ as __version__,
)
from martian_redteam import (
    a_red_team as a_red_team,
)
from martian_redteam import (
    bind_sandbox_mcp as bind_sandbox_mcp,
)
from martian_redteam import (
    bind_tools as bind_tools,
)
from martian_redteam import (
    evaluate_gate as evaluate_gate,
)
from martian_redteam import (
    parse_session_payload as parse_session_payload,
)
from martian_redteam import (
    red_team as red_team,
)
from martian_redteam import (
    wrap_callback as wrap_callback,
)

__all__ = [*_legacy_exports, "RedteamConfig", "RedteamOperation", "RedteamOperationError"]
