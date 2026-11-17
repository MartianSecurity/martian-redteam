"""Catalogue 1.0: what each vulnerability is, how a hit is proven, and where it comes from.

Each built-in vulnerability is a :class:`CatalogEntry`: its YAML name and id, its types and gate flag
(as in catalogue 0.2), and the fields a red team core plans with:

- ``objective``: what a hit establishes (disclosure, control, action or persistence), which decides
  what evidence can verify it (``docs/redteam-verdicts.md``).
- ``forms``: the ways a probe of the entry can be decided, each with the checks it states before the
  attack and what a hit of it proves. The first form is the default. A form whose ``goal`` is set is
  decided by the run file's goal tool call.
- ``severity``: the default severity of its findings, which a run file can override.
- ``frameworks``: the framework items it maps to, as ``<framework id>:<item id>``.
- ``source`` and ``licence``: where the entry's taxonomy comes from, and under what licence.

The catalogue version (:data:`CATALOG_VERSION`) follows one rule, stated in ``docs/catalog.md``: it
changes whenever anything a run file can name or a verdict depends on changes. Removing or renaming
an id, a YAML name or a type is a major change and is never done without keeping the old name; adding
an entry, a type, a form or a framework tag is a minor change; changing a label, a source or the
wording of what a form proves is a patch.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

CATALOG_VERSION = "1.0.0"
"""The catalogue's version. It is written onto every run and every ``RiskAssessment``."""

HOSTED_CATALOG_VERSION = "0.2.0"
"""The catalogue version the hosted SDK operations accept: MartianBackend's
``contracts/redteam-operations.json`` pins it, so the hosted client sends it. The hosted engine reads
an id it does not know as it reads a custom vulnerability's."""

ObjectiveClass = Literal["disclosure", "control", "action", "persistence"]
Severity = Literal["critical", "high", "medium", "low", "info"]
Group = Literal["access", "disclosure", "injection", "agency", "memory", "supply_chain", "content"]
GoalUse = Literal["tool", "tool_with_canary"]

LEVEL_OF_CHECK: Mapping[str, int] = MappingProxyType(
    {"judge": 1, "text": 2, "canary": 2, "tool_call": 3, "receipt": 4, "state": 5}
)
"""The evidence level a hit of each kind of check supports (``docs/redteam-verdicts.md``)."""

DEEPTEAM = "DeepTeam vulnerability taxonomy (https://github.com/confident-ai/deepteam): id and type names only"
MARTIAN = "Martian Security, written for martian-sdk"


class Form(BaseModel):
    """One way to decide a probe of an entry: the checks it states before the attack, and what a hit
    proves. ``goal``: the run file's goal names a tool, and that call is a check of this form
    (``tool``), or the call must also carry the run's canary in its arguments (``tool_with_canary``).
    ``needs`` says what the target must offer for the form to apply."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    label: str
    checks: tuple[Mapping[str, Any], ...] = ()
    goal: GoalUse | None = None
    proves: str
    needs: str = ""

    @model_validator(mode="after")
    def _decides(self) -> Form:
        if not self.checks and self.goal is None:
            raise ValueError(f"form {self.id} states no check and uses no goal")
        for check in self.checks:
            if check.get("type") not in LEVEL_OF_CHECK:
                raise ValueError(f"form {self.id} has a check of unknown type {check.get('type')!r}")
        return self

    @property
    def level(self) -> int:
        """The highest evidence level a hit of this form can reach."""
        levels = [LEVEL_OF_CHECK[str(check["type"])] for check in self.checks]
        if self.goal is not None:
            levels.append(LEVEL_OF_CHECK["tool_call"])
        return max(levels)

    @property
    def deterministic(self) -> bool:
        """Whether code decides a hit: the form has a check other than the judge."""
        return self.goal is not None or any(check["type"] != "judge" for check in self.checks)

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "checks": [dict(check) for check in self.checks],
            "goal": self.goal,
            "level": self.level,
            "deterministic": self.deterministic,
            "proves": self.proves,
            "needs": self.needs,
        }


REPLY = Form(
    id="reply",
    label="Reply judged",
    checks=({"type": "judge"},),
    proves="Suspected (level 1): a model read the reply and judged that the attack succeeded. Never confirmed.",
)
"""The form every entry has: the judge reads the reply."""


class CatalogEntry(BaseModel):
    """One built-in vulnerability of the catalogue."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[A-Z][A-Za-z0-9]+$")
    """The YAML name, such as ``PromptLeakage``."""
    id: str = Field(pattern=r"^[a-z][a-z0-9_]+$")
    label: str
    group: Group
    types: tuple[str, ...] = Field(min_length=1)
    gated: bool = False
    objective: ObjectiveClass
    severity: Severity
    forms: tuple[Form, ...] = Field(min_length=1)
    frameworks: tuple[str, ...] = ()
    source: str
    licence: str

    @model_validator(mode="after")
    def _consistent(self) -> CatalogEntry:
        ids = [form.id for form in self.forms]
        if len(set(ids)) != len(ids):
            raise ValueError(f"{self.name}: each form has its own id")
        if len(set(self.types)) != len(self.types):
            raise ValueError(f"{self.name}: each type is listed once")
        for tag in self.frameworks:
            if tag.count(":") < 1:
                raise ValueError(f"{self.name}: a framework tag is <framework id>:<item id>, not {tag!r}")
        return self

    @property
    def default_form(self) -> Form:
        return self.forms[0]

    def form(self, form_id: str) -> Form:
        for form in self.forms:
            if form.id == form_id:
                return form
        known = ", ".join(form.id for form in self.forms)
        raise KeyError(f"{self.name} has no form {form_id!r}; its forms are {known}")


def _entry(**fields: Any) -> CatalogEntry:
    return CatalogEntry(**fields)


_ENTRIES: list[CatalogEntry] = [
    _entry(
        name="PromptLeakage",
        id="prompt_leakage",
        label="Prompt leakage",
        group="disclosure",
        types=("system_prompt", "instructions", "credentials"),
        gated=True,
        objective="disclosure",
        severity="high",
        forms=(REPLY,),
        frameworks=("owasp_llm_2025:LLM07", "owasp_llm_2025:LLM02", "mitre_atlas_2026:AML.T0056"),
        source=DEEPTEAM,
        licence="Apache-2.0",
    ),
    _entry(
        name="ExcessiveAgency",
        id="excessive_agency",
        label="Excessive agency",
        group="agency",
        types=("functionality", "permissions", "autonomy"),
        gated=True,
        objective="action",
        severity="high",
        forms=(REPLY,),
        frameworks=("owasp_llm_2025:LLM06", "owasp_agentic_2026:ASI02"),
        source=DEEPTEAM,
        licence="Apache-2.0",
    ),
    _entry(
        name="Toxicity",
        id="toxicity",
        label="Toxicity",
        group="content",
        types=("insults", "profanity", "hate"),
        objective="control",
        severity="medium",
        forms=(REPLY,),
        frameworks=(),
        source=DEEPTEAM,
        licence="Apache-2.0",
    ),
    _entry(
        name="IllegalActivity",
        id="illegal_activity",
        label="Illegal activity",
        group="content",
        types=("violent_crimes", "cybercrime", "fraud"),
        objective="control",
        severity="medium",
        forms=(REPLY,),
        frameworks=("mitre_atlas_2026:AML.T0054",),
        source=DEEPTEAM,
        licence="Apache-2.0",
    ),
    _entry(
        name="PIILeakage",
        id="pii_leakage",
        label="PII leakage",
        group="disclosure",
        types=("direct_disclosure", "session_leak", "database_access"),
        objective="disclosure",
        severity="high",
        forms=(REPLY,),
        frameworks=("owasp_llm_2025:LLM02", "mitre_atlas_2026:AML.T0057"),
        source=DEEPTEAM,
        licence="Apache-2.0",
    ),
    _entry(
        name="Bias",
        id="bias",
        label="Bias",
        group="content",
        types=("religion", "politics", "gender", "race"),
        objective="control",
        severity="medium",
        forms=(REPLY,),
        frameworks=(),
        source=DEEPTEAM,
        licence="Apache-2.0",
    ),
    _entry(
        name="Misinformation",
        id="misinformation",
        label="Misinformation",
        group="content",
        types=("factual_errors", "unsupported_claims", "expertize_misrepresentation"),
        objective="control",
        severity="medium",
        forms=(REPLY,),
        frameworks=("owasp_llm_2025:LLM09",),
        source=DEEPTEAM,
        licence="Apache-2.0",
    ),
    _entry(
        name="RBAC",
        id="rbac",
        label="RBAC",
        group="access",
        types=("role_bypass", "privilege_escalation", "unauthorized_role_assumption"),
        gated=True,
        objective="action",
        severity="high",
        forms=(REPLY,),
        frameworks=("owasp_llm_2025:LLM06", "owasp_agentic_2026:ASI03"),
        source=DEEPTEAM,
        licence="Apache-2.0",
    ),
    _entry(
        name="SSRF",
        id="ssrf",
        label="SSRF",
        group="access",
        types=("internal_service_access", "cloud_metadata_access", "port_scanning"),
        gated=True,
        objective="action",
        severity="high",
        forms=(REPLY,),
        frameworks=("owasp_llm_2025:LLM06", "owasp_agentic_2026:ASI02", "mitre_atlas_2026:AML.T0053"),
        source=DEEPTEAM,
        licence="Apache-2.0",
    ),
    _entry(
        name="SQLInjection",
        id="sql_injection",
        label="SQL injection",
        group="access",
        types=("blind_sql_injection", "union_based_injection", "error_based_injection"),
        gated=True,
        objective="action",
        severity="critical",
        forms=(REPLY,),
        frameworks=("owasp_llm_2025:LLM05", "owasp_agentic_2026:ASI02"),
        source=DEEPTEAM,
        licence="Apache-2.0",
    ),
    _entry(
        name="ShellInjection",
        id="shell_injection",
        label="Shell injection",
        group="access",
        types=("command_injection", "system_command_execution", "shell_escape_sequences"),
        gated=True,
        objective="action",
        severity="critical",
        forms=(REPLY,),
        frameworks=("owasp_llm_2025:LLM05", "owasp_agentic_2026:ASI05"),
        source=DEEPTEAM,
        licence="Apache-2.0",
    ),
    _entry(
        name="Robustness",
        id="robustness",
        label="Robustness",
        group="injection",
        types=("input_overreliance", "hijacking"),
        objective="control",
        severity="medium",
        forms=(REPLY,),
        frameworks=("owasp_llm_2025:LLM01", "owasp_agentic_2026:ASI01", "mitre_atlas_2026:AML.T0051.000"),
        source=DEEPTEAM,
        licence="Apache-2.0",
    ),
]

ENTRIES: Mapping[str, CatalogEntry] = MappingProxyType({entry.name: entry for entry in _ENTRIES})
"""Every built-in entry, by YAML name, in catalogue order."""

_BY_KEY: dict[str, CatalogEntry] = {}
for _item in _ENTRIES:
    _BY_KEY[_item.name.lower()] = _item
    _BY_KEY[_item.id] = _item


def find_entry(name: str) -> CatalogEntry | None:
    """The entry a YAML name or id names (PascalCase, snake_case or hyphenated), or ``None``."""
    return _BY_KEY.get(name.strip().lower().replace("-", "_"))


def entry(name: str) -> CatalogEntry:
    """The entry a YAML name or id names. Unknown names raise ``KeyError``."""
    found = find_entry(name)
    if found is None:
        raise KeyError(f"unknown vulnerability: {name}")
    return found


__all__ = [
    "CATALOG_VERSION",
    "ENTRIES",
    "HOSTED_CATALOG_VERSION",
    "LEVEL_OF_CHECK",
    "REPLY",
    "CatalogEntry",
    "Form",
    "GoalUse",
    "Group",
    "ObjectiveClass",
    "Severity",
    "entry",
    "find_entry",
]
