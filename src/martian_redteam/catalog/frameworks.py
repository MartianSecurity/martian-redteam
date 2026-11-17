"""Framework maps: each framework's items, and how far the catalogue covers each one.

A catalogue entry names the framework items it tests in its ``frameworks`` tags
(:mod:`martian_redteam.catalog.entries`); a map lists a framework's items, each marked ``covered``,
``partly`` or ``not_covered`` with the reason, and takes each item's vulnerability ids from those tags.
The maps are pinned to one revision of each framework and are not live feeds.

The labels say what the catalogue tests, never that a system complies with a framework. ``covered``
means the catalogue has entries whose deterministic forms test the item's main risk; ``partly`` that
it tests part of the item, or only with the judge; ``not_covered`` that it tests none of it. A run
that selects a framework's entries shows what was tested, not that the framework is met.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from martian_redteam.catalog.entries import ENTRIES
from martian_redteam.catalog.vulnerabilities import Vulnerability, get_vulnerability

Coverage = Literal["covered", "partly", "not_covered"]

DISCLAIMER = (
    "Coverage labels say what this catalogue tests. They are not a certification, an audit or a claim of "
    "compliance, and no framework is fully covered."
)


class FrameworkItem(BaseModel):
    """One item of a framework: its id and title, how far the catalogue covers it and why."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    title: str
    coverage: Coverage
    note: str = ""


class FrameworkMap(BaseModel):
    """A framework at one revision, its items and the catalogue's coverage of each."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    label: str
    source_revision: str
    source: str
    items: tuple[FrameworkItem, ...]
    notes: str

    @property
    def coverage(self) -> str:
        """The map's own label: ``subset``, always. No framework is fully covered."""
        return "subset"

    def item(self, item_id: str) -> FrameworkItem:
        for item in self.items:
            if item.id == item_id:
                return item
        raise KeyError(f"{self.id} has no item {item_id}")

    def vulnerabilities_of(self, item_id: str) -> tuple[str, ...]:
        """The catalogue ids tagged with this item, in catalogue order."""
        tag = f"{self.id}:{item_id}"
        return tuple(entry.id for entry in ENTRIES.values() if tag in entry.frameworks)

    @property
    def vulnerability_ids(self) -> tuple[str, ...]:
        """Every catalogue id tagged with an item of this framework, in catalogue order."""
        prefix = f"{self.id}:"
        return tuple(entry.id for entry in ENTRIES.values() if any(tag.startswith(prefix) for tag in entry.frameworks))

    def counts(self) -> dict[str, int]:
        return {
            label: sum(item.coverage == label for item in self.items) for label in ("covered", "partly", "not_covered")
        }


def _item(identifier: str, title: str, coverage: Coverage, note: str = "") -> FrameworkItem:
    return FrameworkItem(id=identifier, title=title, coverage=coverage, note=note)


FRAMEWORKS: dict[str, FrameworkMap] = {
    "owasp_llm_2025": FrameworkMap(
        id="owasp_llm_2025",
        label="OWASP Top 10 for LLM Applications 2025",
        source_revision="2025",
        source="https://genai.owasp.org/llm-top-10/",
        items=(
            _item(
                "LLM01",
                "Prompt Injection",
                "partly",
                "Direct injection (Robustness), indirect injection through an inject variable, a tool result or a "
                "web page (IndirectPromptInjection), and through tool descriptions and skills (ToolPoisoning); "
                "retrieval and multimodal injection are not tested",
            ),
            _item(
                "LLM02",
                "Sensitive Information Disclosure",
                "partly",
                "Canary-backed disclosure of prompts, credentials and personal data, across sessions and out "
                "through tools and links; training-data disclosure is not tested",
            ),
            _item(
                "LLM03",
                "Supply Chain",
                "partly",
                "Poisoned tool descriptions and skills only; models, packages and datasets are not tested",
            ),
            _item(
                "LLM04",
                "Data and Model Poisoning",
                "partly",
                "Poisoning of the agent's memory; training and fine-tuning data are not tested",
            ),
            _item(
                "LLM05",
                "Improper Output Handling",
                "partly",
                "Attack text reaching SQL and shell tools, and images and links that carry data out",
            ),
            _item(
                "LLM06",
                "Excessive Agency",
                "covered",
                "Tool calls beyond the request, confirmed by the call itself: tool misuse, object- and "
                "function-level authorisation, roles, debug tools",
            ),
            _item("LLM07", "System Prompt Leakage", "covered", "A canary planted in the system prompt or instructions"),
            _item("LLM08", "Vector and Embedding Weaknesses", "not_covered", "No retrieval store is attacked"),
            _item("LLM09", "Misinformation", "partly", "Judged from the reply only"),
            _item("LLM10", "Unbounded Consumption", "not_covered", "No resource exhaustion is attempted"),
        ),
        notes="Items are the 2025 list. Prompt injection is also an attack method of every probe.",
    ),
    "owasp_agentic_2026": FrameworkMap(
        id="owasp_agentic_2026",
        label="OWASP Top 10 for Agentic Applications 2026",
        source_revision="2026",
        source="https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/",
        items=(
            _item(
                "ASI01",
                "Agent Goal Hijack",
                "partly",
                "Task hijacking judged from the reply (Robustness), and goals injected through what the agent reads, "
                "decided by a tool call or the receipt listener (IndirectPromptInjection)",
            ),
            _item(
                "ASI02",
                "Tool Misuse and Exploitation",
                "covered",
                "Unauthorised tool calls with attacker-chosen arguments, confirmed by the call; exfiltration "
                "through tool arguments",
            ),
            _item(
                "ASI03",
                "Identity and Privilege Abuse",
                "partly",
                "Role, object- and function-level authorisation and debug access through the agent's tools; "
                "credential and identity delegation are not tested",
            ),
            _item(
                "ASI04",
                "Agentic Supply Chain Vulnerabilities",
                "partly",
                "Poisoned MCP tool descriptions and skills; packages, registries and agent cards are not tested",
            ),
            _item("ASI05", "Unexpected Code Execution (RCE)", "partly", "Attack text reaching a shell tool"),
            _item(
                "ASI06",
                "Memory & Context Poisoning",
                "partly",
                "Memory written in one session and triggered in another, and leakage between sessions; "
                "retrieval context is not tested",
            ),
            _item("ASI07", "Insecure Inter-Agent Communication", "not_covered", "One agent is attacked at a time"),
            _item("ASI08", "Cascading Failures", "not_covered", "No multi-agent or multi-step failure is traced"),
            _item("ASI09", "Human-Agent Trust Exploitation", "not_covered", "No human in the loop is simulated"),
            _item("ASI10", "Rogue Agents", "not_covered", "An agent's own drift is not monitored"),
        ),
        notes="Items are the list of December 2025.",
    ),
    "mitre_atlas_2026": FrameworkMap(
        id="mitre_atlas_2026",
        label="MITRE ATLAS (LLM and agent techniques)",
        source_revision="2026.10",
        source="https://atlas.mitre.org/",
        items=(
            _item("AML.T0051.000", "LLM Prompt Injection: Direct", "partly", "Judged from the reply (Robustness)"),
            _item(
                "AML.T0051.001",
                "LLM Prompt Injection: Indirect",
                "partly",
                "Through an inject variable, a tool result, a web page, tool descriptions and skills; retrieval stores "
                "are not tested",
            ),
            _item(
                "AML.T0054", "LLM Jailbreak", "partly", "Harmful-content datasets and requests, judged from the reply"
            ),
            _item("AML.T0048", "External Harms", "partly", "Harmful content judged from the reply"),
            _item("AML.T0053", "AI Agent Tool Invocation", "covered", "Tool calls confirmed by the call itself"),
            _item("AML.T0050", "Command and Scripting Interpreter", "partly", "Attack text reaching a shell tool"),
            _item("AML.T0069.002", "Discover LLM System Information: System Prompt", "covered", "A planted canary"),
            _item("AML.T0084", "Discover AI Agent Configuration", "partly", "Debug and internal tools"),
            _item("AML.T0085", "Data from AI Services", "partly", "Personal data in a private record, by canary"),
            _item(
                "AML.T0085.002",
                "Data from AI Services: Conversation Context and Memory",
                "partly",
                "Another user's conversation, by canary",
            ),
            _item(
                "AML.T0086",
                "Exfiltration via AI Agent Tool Invocation",
                "covered",
                "The canary in a tool call's arguments",
            ),
            _item(
                "AML.T0077",
                "LLM Response Rendering",
                "covered",
                "An image or link in the reply, received by the listener",
            ),
            _item(
                "AML.T0080.000", "AI Agent Context Poisoning: Memory", "partly", "Memory written and triggered later"
            ),
            _item("AML.T0110", "AI Agent Tool Poisoning", "partly", "A poisoned tool description of the test server"),
            _item("AML.T0070", "RAG Poisoning", "not_covered", "No retrieval store is attacked"),
            _item("AML.T0061", "LLM Prompt Self-Replication", "not_covered"),
            _item("AML.T0034.002", "Agentic Resource Consumption", "not_covered"),
            _item("AML.T0118", "Autonomous AI Agent Communication", "not_covered"),
            _item("AML.T0020", "Training Data Poisoning", "not_covered"),
            _item("AML.T0024", "Exfiltration via AI Inference API", "not_covered"),
        ),
        notes=(
            "ATLAS has many more techniques, most about models and infrastructure. The map lists those about "
            "LLM and agent behaviour; every technique not listed is not covered."
        ),
    ),
    "nist_ai_rmf_1_0": FrameworkMap(
        id="nist_ai_rmf_1_0",
        label="NIST AI Risk Management Framework 1.0",
        source_revision="1.0 (January 2023)",
        source="https://www.nist.gov/itl/ai-risk-management-framework",
        items=(
            _item("GOVERN", "Govern", "not_covered", "Policies, roles and culture are an organisation's process"),
            _item("MAP", "Map", "not_covered", "Context and risk identification are an organisation's process"),
            _item(
                "MEASURE-2.5",
                "The AI system is demonstrated to be valid and reliable",
                "partly",
                "Misinformation and robustness under attack, judged from the reply",
            ),
            _item(
                "MEASURE-2.6",
                "The AI system is evaluated regularly for safety risks",
                "partly",
                "Toxicity, illegal activity and harmful content, judged from the reply",
            ),
            _item(
                "MEASURE-2.7",
                "AI system security and resilience are evaluated and documented",
                "partly",
                "The security entries, with evidence levels; the run is one evaluation, not a programme",
            ),
            _item(
                "MEASURE-2.10",
                "Privacy risk of the AI system is examined and documented",
                "partly",
                "Personal-data and cross-session leakage and exfiltration, by canary",
            ),
            _item("MEASURE-2.11", "Fairness and bias are evaluated and results documented", "partly", "Judged only"),
            _item("MANAGE", "Manage", "not_covered", "Risk response and monitoring are an organisation's process"),
        ),
        notes="Subcategories of MEASURE not listed are not covered.",
    ),
    "eu_ai_act_2024": FrameworkMap(
        id="eu_ai_act_2024",
        label="EU AI Act (Regulation (EU) 2024/1689)",
        source_revision="2024/1689",
        source="https://eur-lex.europa.eu/eli/reg/2024/1689/oj",
        items=(
            _item("Art5", "Prohibited AI practices", "not_covered"),
            _item(
                "Art9",
                "Risk management system",
                "partly",
                "Testing for some foreseeable risks to safety and fundamental rights; not the risk management system",
            ),
            _item("Art10", "Data and data governance", "not_covered"),
            _item("Art13", "Transparency and provision of information to deployers", "not_covered"),
            _item("Art14", "Human oversight", "not_covered"),
            _item(
                "Art15",
                "Accuracy, robustness and cybersecurity",
                "partly",
                "Adversarial tests of robustness and of attacks on the system; evidence for an assessment, "
                "not the assessment",
            ),
            _item("Art50", "Transparency obligations for providers and deployers", "not_covered"),
            _item(
                "Art55",
                "Obligations for providers of general-purpose AI models with systemic risk",
                "partly",
                "Adversarial testing of model behaviour only",
            ),
            _item("Art72", "Post-market monitoring", "not_covered"),
        ),
        notes="Articles not listed are not covered. Nothing here establishes conformity with the Act.",
    ),
}
"""Every framework map, by id."""


def find_item(tag: str) -> tuple[FrameworkMap, FrameworkItem]:
    """The map and item a tag ``<framework id>:<item id>`` names."""
    framework_id, _, item_id = tag.partition(":")
    try:
        framework = FRAMEWORKS[framework_id]
    except KeyError:
        raise KeyError(f"unknown framework: {framework_id}") from None
    return framework, framework.item(item_id)


def vulnerabilities_for(framework_id: str) -> list[Vulnerability]:
    """Instantiate the catalogue vulnerabilities a framework map names, using default types.
    ``framework_id`` is a framework (``owasp_llm_2025``) or one of its items (``owasp_llm_2025:LLM06``)."""
    framework_name, _, item_id = framework_id.partition(":")
    try:
        framework = FRAMEWORKS[framework_name]
    except KeyError:
        raise KeyError(f"unknown framework: {framework_id}") from None
    ids = framework.vulnerabilities_of(item_id) if item_id else framework.vulnerability_ids
    if item_id:
        framework.item(item_id)
    return [get_vulnerability(vulnerability_id) for vulnerability_id in ids]


__all__ = [
    "DISCLAIMER",
    "FRAMEWORKS",
    "Coverage",
    "FrameworkItem",
    "FrameworkMap",
    "find_item",
    "vulnerabilities_for",
]
