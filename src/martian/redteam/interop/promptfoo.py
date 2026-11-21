"""promptfoo red team configs as version 2 run files, and promptfoo red team results as red team runs.

promptfoo is not installed and not called. :func:`convert_config` reads a ``promptfooconfig.yaml``
with a ``redteam`` section and writes the run file where its features map, listing everything that
does not (:data:`PLUGINS`, :data:`STRATEGIES`). :func:`import_results` reads the JSON file
``promptfoo redteam run -o results.json`` (or ``promptfoo eval -o``) writes and keeps each provider's
rows as one :class:`~martian.redteam.model.RedteamRun` in the local store. The SDK records imported
hits as suspected evidence (level 1), based on the verdicts in the archive.

``docs/redteam-interop.md`` lists every mapping.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
from collections import Counter
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import ValidationError

from martian._privacy import safe_text
from martian.redteam.model import (
    Attempt,
    AttemptOutcome,
    CheckResult,
    EvidenceLevel,
    Probe,
    RedteamRun,
    TargetRef,
    Turn,
)
from martian.redteam.model.maps import counts_of, findings_of, objective_of, probe_id
from martian.redteam.targets.identity import identity_digest

if TYPE_CHECKING:
    from martian.evals.model import Run
    from martian.evals.promptfoo_import import PromptfooResult, PromptfooResults
    from martian.redteam.store import RedteamStore


class PromptfooConversionError(Exception):
    """A promptfoo config this converter cannot read or write as a run file. The message says why."""


# --- what maps ------------------------------------------------------------------------------------

PLUGINS: dict[str, tuple[str, list[str] | None]] = {
    "harmful:hate": ("Toxicity", ["hate"]),
    "harmful:insults": ("Toxicity", ["insults"]),
    "harmful:profanity": ("Toxicity", ["profanity"]),
    "harmful:violent-crime": ("IllegalActivity", ["violent_crimes"]),
    "harmful:cybercrime": ("IllegalActivity", ["cybercrime"]),
    "harmful:cybercrime:malicious-code": ("IllegalActivity", ["cybercrime"]),
    "harmful:misinformation-disinformation": ("Misinformation", ["factual_errors"]),
    "harmful:specialized-advice": ("Misinformation", ["expertize_misrepresentation"]),
    "harmful:privacy": ("PIILeakage", ["direct_disclosure"]),
    "hallucination": ("Misinformation", ["unsupported_claims"]),
    "overreliance": ("Robustness", ["input_overreliance"]),
    "hijacking": ("Robustness", ["hijacking"]),
    "pii:direct": ("PIILeakage", ["direct_disclosure"]),
    "pii:session": ("PIILeakage", ["session_leak"]),
    "pii:api-db": ("PIILeakage", ["database_access"]),
    "cross-session-leak": ("PIILeakage", ["session_leak"]),
    "bias:gender": ("Bias", ["gender"]),
    "bias:race": ("Bias", ["race"]),
    "religion": ("Bias", ["religion"]),
    "politics": ("Bias", ["politics"]),
    "rbac": ("RBAC", None),
    "bfla": ("RBAC", ["privilege_escalation"]),
    "ssrf": ("SSRF", None),
    "sql-injection": ("SQLInjection", None),
    "shell-injection": ("ShellInjection", None),
    "excessive-agency": ("ExcessiveAgency", None),
    "prompt-extraction": ("PromptLeakage", ["system_prompt"]),
}
"""promptfoo plugin id → the catalogue vulnerability (its YAML name) and its types (``None``: every
type). ``policy`` becomes a custom vulnerability whose criteria are the policy's text."""

COLLECTIONS: dict[str, list[str]] = {
    "pii": ["pii:direct", "pii:session", "pii:api-db", "pii:social"],
    "bias": ["bias:age", "bias:disability", "bias:gender", "bias:race"],
}
"""promptfoo plugin collections whose members are fixed, expanded before mapping."""

DATASET_PLUGINS = frozenset(
    {
        "aegis",
        "beavertails",
        "cyberseceval",
        "donotanswer",
        "harmbench",
        "pliny",
        "toxic-chat",
        "xstest",
        "unsafebench",
        "vlguard",
        "vlsu",
    }
)

PLUGIN_REASONS: dict[str, str] = {
    "default": "a collection that promptfoo's version decides; name its plugins",
    "harmful": "a collection of every harmful:* plugin; name the ones you want",
    "intent": "fixed prompts promptfoo sends as written; the attacker here writes each attack",
    "indirect-prompt-injection": (
        "needs a variable the agent reads; use tools.proxy in inject mode to deliver an injection"
    ),
    "harmful:non-violent-crime": "broader than the catalogue's fraud type",
    "bola": "object-level authorization has no vulnerability in the catalogue",
}
"""Why a plugin does not convert, for those with a reason of their own."""

STRATEGIES: dict[str, str] = {
    "base64": "Base64",
    "rot13": "ROT13",
    "leetspeak": "Leetspeak",
    "hex": "Hex",
    "homoglyph": "Homoglyph",
    "morse": "Morse",
    "prompt-injection": "PromptInjection",
    "citation": "Citation",
    "jailbreak:tree": "TreeSearch",
    "crescendo": "Escalation",
    "best-of-n": "Mutation",
}
"""promptfoo strategy id → the attack method (its YAML name, docs/redteam-methods.md)."""

STRATEGY_SETTINGS: dict[str, dict[str, str]] = {
    "crescendo": {"maxTurns": "max_turns", "maxBacktracks": "max_backtracks"},
}
"""A strategy's ``config`` keys that become its attack method's settings."""

_ADAPTIVE = (
    "an adaptive method that promptfoo runs on its own service, or one with no attack method here "
    "(docs/redteam-methods.md)"
)
_TRANSFORM = "a transform with no attack method here"
STRATEGY_REASONS: dict[str, str] = {
    **dict.fromkeys(
        (
            "jailbreak",
            "jailbreak:meta",
            "jailbreak:composite",
            "jailbreak:likert",
            "jailbreak:hydra",
            "jailbreak:goblin",
            "goat",
            "custom",
            "mischievous-user",
            "gcg",
            "math-prompt",
            "authoritative-markup-injection",
        ),
        _ADAPTIVE,
    ),
    **dict.fromkeys(
        (
            "piglatin",
            "camelcase",
            "emoji",
            "arabic-presentation-forms",
            "jailbreak-templates",
            "other-encodings",
            "multilingual",
        ),
        _TRANSFORM,
    ),
    **dict.fromkeys(("image", "audio", "video"), "media input; the targets here take text"),
    "basic": "promptfoo's plain plugin tests; every attack method here writes its own attack, so it needs none",
    "indirect-web-pwn": "promptfoo hosts the page; use tools.proxy in inject mode to deliver an injection",
    "layer": "chains other strategies; convert each one",
    "retry": "re-adds failed tests; pin confirmed findings with martian redteam pin add instead",
}
"""Why a strategy does not convert."""

DEFAULT_STRATEGIES = ["basic", "jailbreak:meta", "jailbreak:composite"]
"""What promptfoo's ``default`` strategy (and a config without ``strategies``) runs."""

SEVERITIES = {"critical": "critical", "high": "high", "medium": "medium", "low": "low", "informational": "info"}
_SEVERITY_RANK = ["info", "low", "medium", "high", "critical"]

_REDTEAM_KEYS_LISTED = {
    "language": "attacks are written in the attacker's language",
    "frameworks": "framework maps are fixed by the catalogue (martian redteam catalog)",
    "entities": "no field holds them; put what matters in target.purpose",
    "contexts": "a run file has one purpose",
    "testGenerationInstructions": "no field holds them; put what matters in target.purpose",
    "graderExamples": "the judge takes no examples; state checks in advance instead",
    "delay": "no delay between calls",
    "maxCharsPerMessage": "no limit on an attack's length",
    "excludeTargetOutputFromAgenticAttackGeneration": "no adaptive attacker in this release",
    "tracing": "tool calls come from the reply contract, the proxy or traces (docs/redteam-targets.md)",
    "sharing": "nothing is shared",
}

_TEMPLATE = re.compile(r"\{\{\s*([^{}]*?)\s*\}\}")
_JSON_PATH = re.compile(r"""^json((?:\.[A-Za-z_][A-Za-z0-9_]*|\[\d+\]|\['[^']*'\]|\["[^"]*"\])+)$""")
_PATH_PART = re.compile(r"""\.([A-Za-z_][A-Za-z0-9_]*)|\[(\d+)\]|\['([^']*)'\]|\["([^"]*)"\]""")


# --- converting a config --------------------------------------------------------------------------


@dataclass
class Conversion:
    """A converted config: the run file, what was not converted, and how what converted differs."""

    run_file: dict[str, Any]
    skipped: list[tuple[str, str]] = field(default_factory=list)
    """``(what, why)`` for everything that did not convert."""
    notes: list[str] = field(default_factory=list)
    """How what converted differs from promptfoo."""


def _ids(entries: Any, where: str) -> list[tuple[str, dict[str, Any]]]:
    if entries is None:
        return []
    if not isinstance(entries, list):
        raise PromptfooConversionError(f"redteam.{where} is a list")
    found: list[tuple[str, dict[str, Any]]] = []
    for index, entry in enumerate(entries):
        if isinstance(entry, str):
            found.append((entry, {}))
        elif isinstance(entry, dict) and isinstance(entry.get("id"), str):
            found.append((entry["id"], entry))
        else:
            raise PromptfooConversionError(f"redteam.{where}[{index}] is an id or an object with an id")
    return found


def _model(provider: Any) -> tuple[dict[str, Any] | None, str | None]:
    """A promptfoo provider as a run file model, or why it does not convert."""
    identifier = provider if isinstance(provider, str) else (provider or {}).get("id")
    if not isinstance(identifier, str):
        return None, "names no provider id"
    parts = identifier.split(":")
    if parts[0] != "openai" or len(parts) not in (2, 3) or (len(parts) == 3 and parts[1] != "chat"):
        return None, f"{identifier}: only openai:chat:MODEL and openai:MODEL map, as openai:/MODEL"
    model: dict[str, Any] = {"model": f"openai:/{parts[-1]}"}
    config = provider.get("config", {}) if isinstance(provider, dict) else {}
    params = {key: config[key] for key in ("temperature", "max_tokens") if key in config}
    if params:
        model["inference_params"] = params
    if other := sorted(set(config) - {"temperature", "max_tokens"}):
        return model, f"{identifier}: its config {', '.join(other)} does not convert"
    return model, None


def _env_headers(headers: dict[str, Any]) -> tuple[dict[str, str] | None, str | None]:
    converted: dict[str, str] = {}
    for name, value in headers.items():
        text = str(value)

        def replace(found: re.Match[str]) -> str:
            expression = found.group(1)
            if not re.fullmatch(r"env\.[A-Za-z_][A-Za-z0-9_]*", expression):
                raise ValueError(expression)
            return "${" + expression[4:] + "}"

        try:
            converted[str(name)] = _TEMPLATE.sub(replace, text)
        except ValueError as error:
            return None, f"header {name} uses {{{{{error}}}}}; only {{{{env.NAME}}}} converts, as ${{NAME}}"
    return converted, None


def _body(body: Any, inject: str) -> tuple[Any, str | None]:
    """The body with promptfoo's attack variable as ``{{input}}``, or why it does not convert."""
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            return None, "the body is not JSON; the HTTP target posts a JSON body"
    if not isinstance(body, dict):
        return None, "the body is not a JSON object"
    names = {inject: "input", "sessionId": "session_id"}

    def walk(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: walk(item) for key, item in value.items()}
        if isinstance(value, list):
            return [walk(item) for item in value]
        if isinstance(value, str):

            def replace(found: re.Match[str]) -> str:
                if found.group(1) not in names:
                    raise ValueError(found.group(1))
                return "{{" + names[found.group(1)] + "}}"

            return _TEMPLATE.sub(replace, value)
        return value

    try:
        return walk(body), None
    except ValueError as error:
        return None, f"the body uses {{{{{error}}}}}, which promptfoo fills and this target does not"


def response_path(transform: str) -> str | None:
    """promptfoo's ``transformResponse`` as a ``response_path`` when it only reads a path of ``json``,
    such as ``json.choices[0].message.content``; otherwise ``None``."""
    found = _JSON_PATH.match(transform.strip())
    if not found:
        return None
    steps = [next(group for group in part.groups() if group is not None) for part in _PATH_PART.finditer(found[1])]
    return ".".join(steps)


def _http_target(provider: dict[str, Any], inject: str) -> tuple[dict[str, Any] | None, list[str], list[str]]:
    """The run file's ``url`` target for a promptfoo HTTP provider: the target, why it does not convert,
    and what of it does not."""
    identifier = provider["id"]
    config = dict(provider.get("config") or {})
    url = config.pop("url", None) if identifier in ("http", "https") else identifier
    config.pop("url", None)
    if not isinstance(url, str):
        return None, ["names no url"], []
    method = str(config.pop("method", "POST")).upper()
    if method != "POST":
        return None, [f"method {method}; the HTTP target posts JSON"], []
    target: dict[str, Any] = {"url": url}
    body, problem = _body(config.pop("body", None), inject)
    if problem:
        return None, [problem], []
    target["body"] = body
    if headers := config.pop("headers", None):
        converted, problem = _env_headers(headers)
        if problem:
            return None, [problem], []
        target["headers"] = converted
    skipped: list[str] = []
    if (transform := config.pop("transformResponse", None)) is not None:
        path = response_path(transform) if isinstance(transform, str) else None
        if path is None:
            return None, ["transformResponse is code; set response_path to where the reply is"], []
        target["response_path"] = path
    if (timeout := config.pop("timeout", None)) is not None and isinstance(timeout, (int, float)):
        target["timeout_seconds"] = min(600.0, max(1.0, timeout / 1000))
    skipped += [f"config.{key}" for key in sorted(config)]
    return target, [], skipped


def _target(
    config: dict[str, Any], inject: str, conversion: Conversion
) -> tuple[dict[str, Any] | None, list[tuple[str, str]]]:
    providers = config.get("targets", config.get("providers"))
    if isinstance(providers, (str, dict)):
        providers = [providers]
    if not isinstance(providers, list) or not providers:
        return None, [("targets", "the config names no target")]
    reasons: list[tuple[str, str]] = []
    for extra in providers[1:]:
        name = extra if isinstance(extra, str) else (extra or {}).get("id", "?")
        reasons.append((f"targets: {name}", "a run file attacks one target; the first is converted"))
    first = providers[0]
    provider = {"id": first} if isinstance(first, str) else dict(first or {})
    identifier = provider.get("id")
    if not isinstance(identifier, str):
        return None, [*reasons, ("targets[0]", "names no id")]
    if identifier in ("http", "https") or identifier.startswith(("http://", "https://")):
        target, problems, skipped = _http_target(provider, inject)
        if target is None:
            return None, [*reasons, *((f"target {identifier}", problem) for problem in problems)]
        for key in skipped:
            conversion.skipped.append((f"target {key}", "the HTTP target has no such setting"))
        return target, reasons
    if identifier.startswith("file://") and identifier.split(":", 2)[1].split("#")[0].endswith(".py"):
        why = (
            "promptfoo calls call_api(prompt, options, context) and reads {'output': ...}; give --target "
            "FILE.py:FUNCTION with a function that takes the message and returns the reply"
        )
    else:
        why = "a model or provider, not an HTTP endpoint; give --target FILE.py:FUNCTION"
    return None, [*reasons, (f"target {identifier}", why)]


def _function_target(spec: str, output: Path) -> dict[str, Any]:
    """``--target FILE.py[:FUNCTION]`` as a ``file`` target, its path relative to the run file's folder."""
    file, _, function = spec.partition(":")
    path = Path(file).expanduser().resolve()
    if path.suffix != ".py":
        raise PromptfooConversionError(f"--target {spec}: give FILE.py or FILE.py:FUNCTION")
    target: dict[str, Any] = {"file": Path(os.path.relpath(path, output.resolve())).as_posix()}
    if function:
        target["function"] = function
    return target


def _plugins(config: dict[str, Any], conversion: Conversion) -> list[dict[str, Any]]:
    redteam = config["redteam"]
    entries = _ids(redteam.get("plugins", ["default"]), "plugins")
    expanded: list[tuple[str, dict[str, Any]]] = []
    for identifier, entry in entries:
        if identifier in COLLECTIONS:
            expanded += [(member, {**entry, "id": member}) for member in COLLECTIONS[identifier]]
        else:
            expanded.append((identifier, entry))
    vulnerabilities: dict[str, dict[str, Any]] = {}
    severities: dict[str, set[str]] = {}
    counts: set[int] = set()
    policies = 0
    for identifier, entry in expanded:
        settings = dict(entry.get("config") or {})
        if isinstance(entry.get("numTests"), int):
            counts.add(entry["numTests"])
        severity = SEVERITIES.get(str(entry.get("severity", "")).lower())
        if identifier == "policy":
            policy = settings.pop("policy", None)
            text = policy.get("text") if isinstance(policy, dict) else policy
            if not isinstance(text, str) or not text.strip():
                conversion.skipped.append(("plugin policy", "has no policy text"))
                continue
            policies += 1
            key = f"policy-{policies}"
            vulnerabilities[key] = {"name": "custom", "id": key, "criteria": text.strip(), "types": ["custom"]}
        elif identifier in PLUGINS:
            name, types = PLUGINS[identifier]
            existing = vulnerabilities.setdefault(name, {"name": name})
            if types is None or existing.get("types", []) is None:
                existing["types"] = None
            else:
                existing["types"] = list(dict.fromkeys([*existing.get("types", []), *types]))
            key = name
        else:
            if identifier.startswith("file://"):
                why = "a custom plugin: its generator and grader prompts; write a custom vulnerability with criteria"
            elif identifier in DATASET_PLUGINS:
                why = "a dataset promptfoo downloads when it generates; no dataset ships here"
            else:
                why = PLUGIN_REASONS.get(identifier, "no vulnerability in the catalogue covers it")
            conversion.skipped.append((f"plugin {identifier}", why))
            continue
        if severity is not None:
            severities.setdefault(key, set()).add(severity)
        for setting in sorted(settings):
            conversion.skipped.append((f"plugin {identifier} config.{setting}", "the run file has no such setting"))
    for key, found in severities.items():
        top = max(found, key=_SEVERITY_RANK.index)
        vulnerabilities[key]["severity"] = top
        if len(found) > 1:
            conversion.notes.append(f"{key} takes the highest severity of the plugins it covers: {top}")
    if len(counts) > 1:
        conversion.skipped.append(
            ("plugins numTests", "plugins ask for different numbers of tests; budget.attempts_per_probe is one")
        )
    for item in vulnerabilities.values():
        if item.get("types") is None:
            item.pop("types", None)
    return list(vulnerabilities.values())


def _strategies(config: dict[str, Any], conversion: Conversion) -> list[dict[str, Any]]:
    entries = _ids(config["redteam"].get("strategies", ["default"]), "strategies")
    expanded: list[tuple[str, dict[str, Any]]] = []
    for identifier, entry in entries:
        if identifier == "default":
            expanded += [(member, {}) for member in DEFAULT_STRATEGIES]
        else:
            expanded.append((identifier, entry))
    attacks: dict[str, dict[str, Any]] = {}
    for identifier, entry in expanded:
        if identifier in STRATEGIES:
            attack = attacks.setdefault(STRATEGIES[identifier], {"name": STRATEGIES[identifier]})
            settings = (entry.get("config") or {}) if isinstance(entry, dict) else {}
            if "plugins" in settings:
                conversion.notes.append(
                    f"strategy {identifier} applied to the plugins it lists; here {STRATEGIES[identifier]} "
                    "applies to every vulnerability"
                )
            mapped = STRATEGY_SETTINGS.get(identifier, {})
            for setting in sorted(set(settings) - {"plugins", "enabled"}):
                if setting in mapped and isinstance(settings[setting], int) and settings[setting] >= 1:
                    attack[mapped[setting]] = settings[setting]
                else:
                    conversion.skipped.append(
                        (f"strategy {identifier} config.{setting}", "the attack method has no such setting")
                    )
        elif identifier.startswith("file://"):
            conversion.skipped.append(
                (f"strategy {identifier}", "custom JavaScript, which this converter does not run")
            )
        else:
            reason = STRATEGY_REASONS.get(identifier, "no attack method in the catalogue covers it")
            conversion.skipped.append((f"strategy {identifier}", reason))
    return list(attacks.values())


def convert_config(
    path: Path,
    *,
    output: Path,
    target: str | None = None,
    purpose: str | None = None,
    attacks: list[str] | None = None,
) -> Conversion:
    """The run file for the promptfoo config at ``path``, written later into ``output``.

    ``target`` (``FILE.py[:FUNCTION]``) replaces the config's target; ``purpose`` gives one when the
    config has none; ``attacks`` (catalogue names) are added to the strategies that convert. Raises
    :class:`PromptfooConversionError` when the config is not a promptfoo red team config, or when what
    converts is not a valid run file."""
    from martian.redteam.runfile import RunFile

    try:
        config = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise PromptfooConversionError(f"cannot read {path}: {error.strerror or error}") from None
    except yaml.YAMLError:
        raise PromptfooConversionError(f"{path.name} is not YAML") from None
    if not isinstance(config, dict) or not isinstance(config.get("redteam"), dict):
        raise PromptfooConversionError(
            f"{path.name} has no redteam section; for an eval config use martian evals convert promptfoo"
        )
    redteam = config["redteam"]
    conversion = Conversion(run_file={"version": 2})
    inject = redteam.get("injectVar") or "prompt"
    if target is not None:
        block: dict[str, Any] | None = _function_target(target, output)
    else:
        block, reasons = _target(config, inject, conversion)
        conversion.skipped += reasons
    if block is None:
        raise PromptfooConversionError(
            "no target converts: "
            + "; ".join(f"{what}: {why}" for what, why in conversion.skipped if what.startswith("target"))
        )
    stated = purpose or redteam.get("purpose")
    if not isinstance(stated, str) or not stated.strip():
        raise PromptfooConversionError(
            "the config has no redteam.purpose, which promptfoo would ask its service for; give --purpose"
        )
    conversion.run_file["target"] = {"purpose": stated.strip(), **block}
    if (provider := redteam.get("provider")) is not None:
        model, problem = _model(provider)
        if model is not None:
            conversion.run_file["models"] = {"attacker": model, "judge": json.loads(json.dumps(model))}
        if problem:
            conversion.skipped.append(("redteam.provider", problem))
    else:
        conversion.notes.append(
            "no redteam.provider: add models.attacker and models.judge to run with your own model; without "
            "them the run needs a Martian connection"
        )
    conversion.run_file["vulnerabilities"] = _plugins(config, conversion)
    chosen = _strategies(config, conversion)
    for name in attacks or []:
        if name not in [item["name"] for item in chosen]:
            chosen.append({"name": name})
    conversion.run_file["attacks"] = chosen
    budget: dict[str, Any] = {}
    if isinstance(redteam.get("numTests"), int) and redteam["numTests"] >= 1:
        budget["attempts_per_probe"] = min(100, redteam["numTests"])
    if isinstance(redteam.get("maxConcurrency"), int) and redteam["maxConcurrency"] >= 1:
        budget["max_concurrent"] = min(64, redteam["maxConcurrency"])
    if budget:
        conversion.run_file["budget"] = budget
    for key, why in _REDTEAM_KEYS_LISTED.items():
        if key in redteam:
            conversion.skipped.append((f"redteam.{key}", why))
    known = {"purpose", "provider", "plugins", "strategies", "numTests", "maxConcurrency", "injectVar"}
    for key in sorted(set(redteam) - known - set(_REDTEAM_KEYS_LISTED)):
        conversion.skipped.append((f"redteam.{key}", "the run file has no such setting"))
    if not conversion.run_file["vulnerabilities"]:
        raise PromptfooConversionError("no plugin converts to a vulnerability: " + _listed(conversion, "plugin"))
    if not conversion.run_file["attacks"]:
        raise PromptfooConversionError(
            "no strategy converts to an attack method; give one with --attack, such as --attack PromptInjection ("
            + _listed(conversion, "strategy")
            + ")"
        )
    try:
        RunFile.model_validate(conversion.run_file)
    except ValidationError as error:
        invalid = error.errors(include_input=False, include_context=False, include_url=False)[0]
        where = ".".join(str(part) for part in invalid["loc"])
        raise PromptfooConversionError(f"the converted run file is not valid: {where}: {invalid['msg']}") from None
    return conversion


def _listed(conversion: Conversion, kind: str) -> str:
    return "; ".join(f"{what}: {why}" for what, why in conversion.skipped if what.startswith(kind)) or "none given"


RUN_FILE = "redteam.yaml"


def write_conversion(conversion: Conversion, output: Path, *, source: Path) -> Path:
    """Write the run file into ``output`` as :data:`RUN_FILE`, with what did not convert as comments at
    its top. Refuses a folder that already has one."""
    output.mkdir(parents=True, exist_ok=True)
    path = output / RUN_FILE
    if path.exists():
        raise PromptfooConversionError(f"{path} already exists; choose another --output")
    lines = [f"# Converted from the promptfoo config {source.name} by martian redteam convert promptfoo."]
    if conversion.skipped:
        lines.append("# Not converted:")
        lines += [f"#   {what}: {why}" for what, why in conversion.skipped]
    if conversion.notes:
        lines.append("# Converted differently:")
        lines += [f"#   {note}" for note in conversion.notes]
    text = "\n".join(lines) + "\n" + yaml.safe_dump(conversion.run_file, sort_keys=False, allow_unicode=True)
    path.write_text(text, encoding="utf-8")
    return path


def report_lines(conversion: Conversion) -> list[str]:
    """What the command prints: what converted, what did not and how what converted differs."""
    run_file = conversion.run_file
    lines = [
        f"Converted: {len(run_file['vulnerabilities'])} vulnerabilities, {len(run_file['attacks'])} attack methods, "
        f"a {'url' if 'url' in run_file['target'] else 'file'} target"
        + (", the attacker and judge model" if "models" in run_file else "")
    ]
    if conversion.skipped:
        lines.append(f"Not converted ({len(conversion.skipped)}):")
        lines += [f"  {what}: {why}" for what, why in conversion.skipped]
    if conversion.notes:
        lines.append("Converted differently:")
        lines += [f"  {note}" for note in conversion.notes]
    return lines


# --- importing results ----------------------------------------------------------------------------

GRADER = "promptfoo-grader"
"""The check id of promptfoo's grader on an imported attempt: a model's verdict, level 1."""

LIMITATION = (
    "Imported from promptfoo. Findings use the archived grader verdicts as suspected evidence (level 1). "
    "The archive is a record of the source evaluation."
)


def _row_metadata(row: PromptfooResult) -> dict[str, Any]:
    source = row.test_case.get("metadata")
    return {**(row.metadata or {}), **(source if isinstance(source, dict) else {})}


def plugin_of(row: PromptfooResult) -> str | None:
    """The recorded plugin ID, or the red team assertion that names it."""
    plugin = _row_metadata(row).get("pluginId")
    if isinstance(plugin, str) and plugin:
        return plugin
    for assertion in row.test_case.get("assert") or []:
        kind = assertion.get("type") if isinstance(assertion, dict) else None
        if isinstance(kind, str) and kind.startswith("promptfoo:redteam:"):
            return kind.removeprefix("promptfoo:redteam:")
    return None


def _probe_names(plugin: str, strategy: str) -> tuple[str, str, str]:
    """The probe's vulnerability, type and attack method: the catalogue's when they map, else promptfoo's
    ids under ``promptfoo:``."""
    from martian.redteam.methods.registry import resolve
    from martian.redteam.runfile import AttackSpec
    from martian_redteam.catalog import get_vulnerability

    if plugin in PLUGINS:
        name, types = PLUGINS[plugin]
        vulnerability = get_vulnerability(name)
        vulnerability_id, kind = vulnerability.id, (types or list(vulnerability.allowed_types))[0]
    elif plugin == "policy":
        vulnerability_id, kind = "promptfoo:policy", "custom"
    else:
        vulnerability_id, kind = f"promptfoo:{plugin}", "promptfoo"
    method = resolve(AttackSpec(name=STRATEGIES[strategy])).id if strategy in STRATEGIES else f"promptfoo:{strategy}"
    return vulnerability_id, kind, method


def _output_text(value: Any) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _turns(row: PromptfooResult, attack: str | None, reply: str | None) -> list[Turn]:
    metadata = (row.response or {}).get("metadata") or {}
    history = metadata.get("redteamHistory")
    turns: list[Turn] = []
    if isinstance(history, list) and history:
        for step in history:
            if not isinstance(step, dict):
                continue
            turns.append(Turn(role="attacker", content=_output_text(step.get("prompt")) or ""))
            turns.append(Turn(role="target", content=_output_text(step.get("output")) or ""))
        return turns
    if attack is not None:
        turns.append(Turn(role="attacker", content=attack))
    if reply is not None:
        turns.append(Turn(role="target", content=reply))
    return turns


def _attempt(row: PromptfooResult, *, identifier: str, probe: str, number: int, inject: str) -> Attempt:
    from martian.evals.promptfoo_import import FAILED_WITH_ERROR

    attack_value = row.vars.get(inject)
    attack = _output_text(attack_value) if attack_value is not None else (row.prompt.raw or None)
    response = row.response or {}
    reply = _output_text(response.get("output"))
    outcome: AttemptOutcome
    error, error_kind = "", None
    grading = row.grading_result
    results: list[CheckResult] = []
    provider_error = _nonempty(response.get("error"))
    if row.failure_reason == FAILED_WITH_ERROR or provider_error:
        outcome = "error"
        error = (row.error or provider_error or "promptfoo recorded an error")[:2000]
        error_kind = "target" if provider_error else "judge"
    else:
        outcome = "resisted" if row.success else "hit"
        results.append(
            CheckResult(
                check_id=GRADER,
                kind="judge",
                hit=not row.success,
                level=None if row.success else EvidenceLevel.JUDGED,
                rationale=(grading.reason or "") if grading else "",
                detail={"source": "promptfoo", "score": row.score},
            )
        )
    return Attempt(
        id=identifier,
        probe_id=probe,
        number=number,
        outcome=outcome,
        level=EvidenceLevel.JUDGED if outcome == "hit" else None,
        input=attack,
        output=reply,
        turns=_turns(row, attack, reply),
        check_results=results,
        error=error,
        error_kind=error_kind,  # type: ignore[arg-type]
    )


def _target_ref(provider: Any, purpose: str, name: str) -> TargetRef:
    identifier = provider.id
    if identifier.startswith(("http://", "https://")) or identifier in ("http", "https"):
        kind = "http"
    elif identifier.startswith("file://") and ".py" in identifier:
        kind = "function"
    else:
        kind = "callback"
    return TargetRef(kind=kind, name=name, purpose=purpose)  # type: ignore[arg-type]


def results_runs(
    found: PromptfooResults, *, provider_name: str | None = None, target_key: str | None = None
) -> list[RedteamRun]:
    """One red team run per provider of a promptfoo results file. Raises
    :class:`PromptfooConversionError` for a file whose rows are not red team rows."""
    file = found.file
    config = file.config or {}
    redteam = config.get("redteam") if isinstance(config.get("redteam"), dict) else {}
    inject = (redteam or {}).get("injectVar") or "prompt"
    plain = [row.test_idx for row in file.results.results if plugin_of(row) is None]
    if plain:
        raise PromptfooConversionError(
            f"{found.path.name}: test {plain[0]} has no red team plugin; for eval results use martian evals "
            "import promptfoo"
        )
    recorded_start = _when(file.results.timestamp) or _when((file.metadata or {}).get("evaluationCreatedAt"))
    providers = [group for group in found.providers if provider_name is None or group.name == provider_name]
    if not providers:
        raise PromptfooConversionError(f"{found.path.name}: no provider named {provider_name!r} in the archive.")
    if target_key is not None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", target_key):
            raise PromptfooConversionError(
                "A target key must be 1–100 ASCII letters, digits, dots, underscores or hyphens, "
                "starting with a letter or digit."
            )
        if len(providers) != 1:
            raise PromptfooConversionError(f"{found.path.name}: use --provider to link exactly one provider.")
        if recorded_start is None:
            raise PromptfooConversionError(f"{found.path.name}: a linked scan needs a recorded start time.")
    started = recorded_start or datetime.now(UTC)
    ended = _finished_at(recorded_start, file.results.stats.get("durationMs"))
    archive_sha256 = found.archive_sha256 or found.sha256
    eval_id = file.eval_id or archive_sha256[:12]
    ids = {group.name: _run_id(eval_id, group.name) for group in found.providers}
    occurrences = Counter(ids.values())
    runs: list[RedteamRun] = []
    for group in providers:
        name, rows = group.name, group.rows
        probes: dict[str, Probe] = {}
        attempts: list[Attempt] = []
        numbers: dict[str, int] = {}
        severity: dict[str, str] = {}
        purpose = (redteam or {}).get("purpose") or ""
        for position, row in enumerate(rows, start=1):
            metadata = _row_metadata(row)
            plugin = plugin_of(row) or ""
            strategy = str(metadata.get("strategyId") or "basic")
            purpose = purpose or str(metadata.get("purpose") or "")
            vulnerability, kind, method = _probe_names(plugin, strategy)
            source = {"plugin": plugin, "strategy": strategy}
            criteria = None
            if plugin == "policy":
                kind, criteria, policy_source = _policy(metadata, row.test_idx)
                source.update(policy_source)
            key = probe_id(vulnerability, kind, method)
            numbers[key] = numbers.get(key, 0) + 1
            if key not in probes:
                probes[key] = Probe(
                    id=key,
                    vulnerability=vulnerability,
                    vulnerability_type=kind,
                    attack_method=method,
                    objective_class=objective_of(vulnerability),
                    goal=str(metadata.get("goal") or ""),
                    criteria=criteria,
                    metadata={"promptfoo": source},
                )
            identifier = f"attempt-{position:04d}"
            if (level := SEVERITIES.get(str(metadata.get("severity", "")).lower())) is not None:
                severity[identifier] = level
            attempts.append(
                _attempt(
                    row,
                    identifier=identifier,
                    probe=key,
                    number=numbers[key],
                    inject=inject,
                )
            )
        probe_list = [probe.model_copy(update={"planned_attempts": numbers[probe.id]}) for probe in probes.values()]
        findings = [
            finding.model_copy(
                update={
                    "severity": max(
                        (severity[item] for item in finding.attempt_ids if item in severity),
                        key=_SEVERITY_RANK.index,
                        default=None,
                    )
                }
            )
            for finding in findings_of(probe_list, attempts, [])
        ]
        provider = group.provider
        provider_source = group.provenance()
        key = _archive_key("v2", archive_sha256, provider_source)
        run_id = _versioned_id(ids[name], key) if occurrences[ids[name]] > 1 else ids[name]
        run = RedteamRun(
            id=run_id,
            source="local",
            state="completed",
            catalog_version="promptfoo",
            purpose=purpose or None,
            target=_target_ref(provider, purpose, name),
            started_at=started,
            finished_at=ended,
            duration_seconds=(ended - started).total_seconds() if ended is not None else 0,
            probes=probe_list,
            attempts=attempts,
            findings=findings,
            counts=counts_of(attempts, findings),
            limitations=[LIMITATION],
            provenance={
                "promptfoo": {
                    "results": str(found.path),
                    "results_sha256": found.sha256,
                    "archive_key": key,
                    "eval_id": file.eval_id,
                    "timestamp": file.results.timestamp,
                    "provider": provider_source,
                    "version": (file.metadata or {}).get("promptfooVersion"),
                }
            },
        )
        runs.append(_linked_target(run, target_key) if target_key is not None else run)
    return runs


def _linked_target(run: RedteamRun, target_key: str) -> RedteamRun:
    assert run.target is not None
    return run.model_copy(
        update={
            "target": run.target.model_copy(update={"name": target_key}),
            "provenance": {
                **run.provenance,
                "findings_target": identity_digest("promptfoo-target-v1", target_key),
                "promptfoo": {**run.provenance["promptfoo"], "target_key": target_key},
            },
        }
    )


def _nonempty(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _policy(metadata: dict[str, Any], test_idx: int) -> tuple[str, str | None, dict[str, str]]:
    config = metadata.get("pluginConfig")
    policy = config.get("policy") if isinstance(config, dict) else None
    details = policy if isinstance(policy, dict) else {}
    policy_id = _nonempty(metadata.get("policyId")) or _nonempty(details.get("id"))
    text = _nonempty(metadata.get("policy")) or _nonempty(details.get("text", policy))
    fallback = f"Promptfoo policy · test {test_idx + 1}"
    if policy_id:
        key = identity_digest("policy-id", policy_id)
        fallback = "Promptfoo policy · " + policy_id
    elif text:
        key = identity_digest("policy-text", text)
        fallback = "Promptfoo policy"
    else:
        key = identity_digest("policy-test", str(test_idx))
    name = _nonempty(metadata.get("policyName")) or _nonempty(details.get("name")) or fallback
    source = {"policy_name": safe_text(name)[:200]}
    if policy_id:
        source["policy_id"] = safe_text(policy_id)
    return "policy-" + key[:16], text, source


def _when(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        found = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return found if found.tzinfo else found.replace(tzinfo=UTC)


def _finished_at(start: datetime | None, duration: Any) -> datetime | None:
    if start is not None and type(duration) in (int, float) and duration >= 0:
        try:
            if math.isfinite(duration):
                return start + timedelta(milliseconds=duration)
        except OverflowError:
            return None
    return None


def _run_id(eval_id: str, provider_name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", f"{eval_id}-{provider_name}").strip("-")
    return ("promptfoo-" + slug)[:200]


def _versioned_id(base: str, archive_key: str) -> str:
    suffix = "-" + archive_key[:16]
    return base if base.endswith(suffix) else base[: 200 - len(suffix)] + suffix


def _archive_key(revision: str, sha256: str, provider: dict[str, Any]) -> str:
    parts = [f"promptfoo-archive-{revision}", sha256, provider["id"], provider["label"] or ""]
    if "position" in provider:
        parts.append(str(provider["position"]))
    return identity_digest(*parts)


def _archive_candidates(run: RedteamRun) -> tuple[tuple[str, ...], tuple[str, str]]:
    """Keep identical files addressable under the earlier raw-file archive key, too."""
    source = run.provenance["promptfoo"]
    key = source["archive_key"]
    previous_sha256 = source["results_sha256"]
    provider = source["provider"]
    previous = _archive_key("v1", previous_sha256, provider)
    name = provider.get("display_name") or provider["label"] or provider["id"]
    previous_base = _run_id(source["eval_id"] or previous_sha256[:12], name)
    return (run.id, _versioned_id(run.id, key), previous_base, _versioned_id(previous_base, previous)), (key, previous)


def _stored_archive(record: Run | None) -> dict[str, Any]:
    header = record.provenance.get("redteam") if record is not None and record.kind == "redteam" else None
    provenance = header.get("provenance") if isinstance(header, dict) else None
    source = provenance.get("promptfoo") if isinstance(provenance, dict) else None
    return source if isinstance(source, dict) else {}


async def _save_archives(store: RedteamStore, runs: list[RedteamRun]) -> list[RedteamRun]:
    from martian.evals.server.principal import SINGLE_USER
    from martian.evals.server.projection import DEFAULT_PROJECT
    from martian.redteam.engine.report import for_report

    planned: dict[str, RedteamRun] = {}
    selected: list[RedteamRun] = []
    archives = [_archive_candidates(run) for run in runs]
    alternatives = sorted({run_id for candidates, _ in archives for run_id in candidates})
    with ExitStack() as sessions:
        for run_id in alternatives:
            sessions.enter_context(store.run_session(DEFAULT_PROJECT, run_id))
        existing = {
            run_id: await store.records.get_run(SINGLE_USER, DEFAULT_PROJECT, run_id) for run_id in alternatives
        }
        for run, (candidates, keys) in zip(runs, archives, strict=True):
            source = run.provenance["promptfoo"]
            for run_id in candidates:
                archived = _stored_archive(existing[run_id])
                if run_id in planned:
                    archived = planned[run_id].provenance["promptfoo"]
                if archived.get("archive_key") not in keys:
                    continue
                saved_key = archived.get("target_key")
                requested_key = source.get("target_key")
                if requested_key is not None and requested_key != saved_key:
                    raise PromptfooConversionError(
                        f"Run {run_id} already has a different archive target assignment. "
                        "Use its original target key, or import in a separate data folder."
                    )
                if requested_key is None and isinstance(saved_key, str):
                    run = _linked_target(run, saved_key)
                break
            else:
                run_id = next((item for item in candidates[:2] if existing[item] is None and item not in planned), "")
                if not run_id:
                    raise PromptfooConversionError(f"Run {run.id} belongs to another record.")
            run = run.model_copy(update={"id": run_id})
            planned.setdefault(run.id, run)
            selected.append(run)
        for run in planned.values():
            if existing[run.id] is not None:
                # The probes are written after the run header and all the attempts.
                records = await store.records.get_redteam_records(SINGLE_USER, run.project_id, run.id)
                if records.probes:
                    continue
            await store.save(for_report(run))
    return selected


def import_results(
    paths: list[Path],
    home: Path | None = None,
    *,
    provider_name: str | None = None,
    target_key: str | None = None,
) -> list[RedteamRun]:
    """Read every promptfoo results file first, so one that cannot be read stops the import before
    anything is written, then keep each provider's rows as a red team run in the local store
    (``home``, else ``MARTIAN_EVALS_HOME``, else ``~/.martian/evals``), as a run of the engine is kept:
    content as digests unless ``MARTIAN_REPORT_INCLUDE_CONTENT=1``, credentials redacted. Each
    archive revision keeps its own reports. A target key groups one explicitly selected provider
    per file into the same Findings history. Returns the runs as read, with their content."""
    from martian.evals.promptfoo_import import PromptfooResultsError, read_promptfoo_results
    from martian.evals.store import StoreError
    from martian.redteam.engine.budget import ResumeError
    from martian.redteam.store import RedteamStore

    try:
        found = [read_promptfoo_results(path) for path in paths]
    except PromptfooResultsError as error:
        raise PromptfooConversionError(str(error)) from None
    runs = [
        run for results in found for run in results_runs(results, provider_name=provider_name, target_key=target_key)
    ]
    store = RedteamStore.local(home)
    try:
        return asyncio.run(_save_archives(store, runs))
    except (StoreError, ResumeError) as error:
        raise PromptfooConversionError(str(error)) from None
    finally:
        store.close()


__all__ = [
    "COLLECTIONS",
    "DEFAULT_STRATEGIES",
    "GRADER",
    "LIMITATION",
    "PLUGINS",
    "PLUGIN_REASONS",
    "RUN_FILE",
    "STRATEGIES",
    "STRATEGY_REASONS",
    "Conversion",
    "PromptfooConversionError",
    "convert_config",
    "import_results",
    "plugin_of",
    "report_lines",
    "response_path",
    "results_runs",
    "write_conversion",
]
