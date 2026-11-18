"""promptfoo red team configs as version 2 run files, and promptfoo red team results as red team runs.

promptfoo is not installed and not called. :func:`convert_config` reads a ``promptfooconfig.yaml``
with a ``redteam`` section and writes the run file where its features map, listing everything that
does not (:data:`PLUGINS`, :data:`STRATEGIES`). :func:`import_results` reads the JSON file
``promptfoo redteam run -o results.json`` (or ``promptfoo eval -o``) writes and keeps each provider's
rows as one :class:`~martian.redteam.model.RedteamRun` in the local store. promptfoo's graders are
models reading the reply, so every imported hit is suspected (evidence level 1).

``docs/redteam-interop.md`` lists every mapping.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import ValidationError

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

if TYPE_CHECKING:
    from martian.evals.promptfoo_import import PromptfooResult, PromptfooResults


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
    "Imported from promptfoo. Each verdict is promptfoo's grader, a model that read the reply, so every "
    "finding is suspected (level 1). The checks were not stated in advance and nothing was re-run."
)


def plugin_of(row: PromptfooResult) -> str | None:
    """The row's promptfoo plugin: ``testCase.metadata.pluginId``, else its red team assertion's type."""
    metadata = row.test_case.get("metadata") or {}
    if isinstance(metadata.get("pluginId"), str):
        return str(metadata["pluginId"])
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
    if row.failure_reason == FAILED_WITH_ERROR:
        outcome = "error"
        error = (row.error or response.get("error") or "promptfoo recorded an error")[:2000]
        error_kind = "target" if response.get("error") else "judge"
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


def _target_ref(provider: Any, purpose: str) -> TargetRef:
    identifier = provider.id
    if identifier.startswith(("http://", "https://")) or identifier in ("http", "https"):
        kind = "http"
    elif identifier.startswith("file://") and ".py" in identifier:
        kind = "function"
    else:
        kind = "callback"
    return TargetRef(kind=kind, name=provider.label or identifier, purpose=purpose)  # type: ignore[arg-type]


def results_runs(found: PromptfooResults) -> list[RedteamRun]:
    """One red team run per provider of a promptfoo results file. Raises
    :class:`PromptfooConversionError` for a file whose rows are not red team rows."""
    from martian.evals.promptfoo_import import provider_name
    from martian.redteam.store import finished

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
    providers: dict[str, list[PromptfooResult]] = {}
    for row in file.results.results:
        providers.setdefault(provider_name(row.provider), []).append(row)
    started = _when(file.results.timestamp) or datetime.now(UTC)
    runs: list[RedteamRun] = []
    for name, rows in providers.items():
        probes: dict[str, Probe] = {}
        attempts: list[Attempt] = []
        numbers: dict[str, int] = {}
        severity: dict[str, str] = {}
        purpose = (redteam or {}).get("purpose") or ""
        for position, row in enumerate(rows, start=1):
            metadata = row.test_case.get("metadata") or {}
            plugin = plugin_of(row) or ""
            strategy = str(metadata.get("strategyId") or "basic")
            purpose = purpose or str(metadata.get("purpose") or "")
            vulnerability, kind, method = _probe_names(plugin, strategy)
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
                    criteria=_policy(metadata) if plugin == "policy" else None,
                    metadata={"promptfoo": {"plugin": plugin, "strategy": strategy}},
                )
            if (level := SEVERITIES.get(str(metadata.get("severity", "")).lower())) is not None:
                severity[key] = max(severity.get(key, level), level, key=_SEVERITY_RANK.index)
            attempts.append(
                _attempt(
                    row,
                    identifier=f"attempt-{position:04d}",
                    probe=key,
                    number=numbers[key],
                    inject=inject,
                )
            )
        probe_list = [probe.model_copy(update={"planned_attempts": numbers[probe.id]}) for probe in probes.values()]
        findings = [
            finding.model_copy(update={"severity": severity.get(finding.probe_id)})
            for finding in findings_of(probe_list, attempts, [])
        ]
        provider = rows[0].provider
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", f"{file.eval_id or found.sha256[:12]}-{name}").strip("-")
        run = RedteamRun(
            id=f"promptfoo-{slug}"[:200],
            source="local",
            state="completed",
            catalog_version="promptfoo",
            purpose=purpose or None,
            target=_target_ref(provider, purpose),
            started_at=started,
            probes=probe_list,
            attempts=attempts,
            findings=findings,
            counts=counts_of(attempts, findings),
            limitations=[LIMITATION],
            provenance={
                "promptfoo": {
                    "results": str(found.path),
                    "results_sha256": found.sha256,
                    "eval_id": file.eval_id,
                    "timestamp": file.results.timestamp,
                    "provider": {"id": provider.id, "label": provider.label},
                    "version": (file.metadata or {}).get("promptfooVersion"),
                }
            },
        )
        runs.append(finished(run))
    return runs


def _policy(metadata: dict[str, Any]) -> str | None:
    policy = (metadata.get("pluginConfig") or {}).get("policy")
    text = policy.get("text") if isinstance(policy, dict) else policy
    return text if isinstance(text, str) and text.strip() else None


def _when(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        found = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return found if found.tzinfo else found.replace(tzinfo=UTC)


def import_results(paths: list[Path], home: Path | None = None) -> list[RedteamRun]:
    """Read every promptfoo results file first, so one that cannot be read stops the import before
    anything is written, then keep each provider's rows as a red team run in the local store
    (``home``, else ``MARTIAN_EVALS_HOME``, else ``~/.martian/evals``), as a run of the engine is kept:
    content as digests unless ``MARTIAN_REPORT_INCLUDE_CONTENT=1``, credentials redacted. Importing a
    file again replaces its runs. Returns the runs as read, with their content."""
    from martian.evals.promptfoo_import import PromptfooResultsError, read_promptfoo_results
    from martian.redteam.engine.report import for_report
    from martian.redteam.store import RedteamStore

    try:
        found = [read_promptfoo_results(path) for path in paths]
    except PromptfooResultsError as error:
        raise PromptfooConversionError(str(error)) from None
    runs = [run for results in found for run in results_runs(results)]
    store = RedteamStore.local(home)
    try:
        for run in runs:
            asyncio.run(store.save(for_report(run)))
    finally:
        store.close()
    return runs


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
