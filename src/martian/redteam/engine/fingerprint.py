"""Attempt fingerprints: what produced an attempt, so a resumed run can reuse it while that is unchanged.

An attempt's fingerprint is a sha256 over everything that decides it: the catalogue version, the
prompt and verdict rule revisions, the engine version, its probe (vulnerability, type, attack method,
objective, goal, criteria and checks), its number, the target (its kind, its block of the run file and
its purpose) and the service (local with its models and their settings, or hosted). Changing one of
these invalidates exactly the attempts it reaches: a check, only its vulnerability's attempts; the
catalogue version or a model, every attempt.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from martian.redteam.catalog.prompts import PROMPTS_REVISION
from martian.redteam.engine.plan import PlannedProbe
from martian.redteam.runfile import RunFile
from martian.redteam.verify.rules import RULES_VERSION
from martian_redteam.catalog.vulnerabilities import CATALOG_VERSION

FINGERPRINT_VERSION = "attempt-fingerprint-v1"
_REVISIONS = {"prompts_revision", "policy_revision", "rules_version", "engine_version"}


def attempt_fingerprint(
    planned: PlannedProbe, number: int, *, run_file: RunFile, service: dict[str, Any], engine_version: str
) -> str:
    probe = planned.probe
    material = {
        "version": FINGERPRINT_VERSION,
        "catalog": CATALOG_VERSION,
        "prompts": PROMPTS_REVISION,
        "rules": RULES_VERSION,
        "engine": engine_version,
        "probe": probe.model_dump(mode="json", exclude={"metadata", "planned_attempts", "gated"}),
        "number": number,
        "target": run_file.target.model_dump(mode="json"),
        "service": {key: value for key, value in service.items() if key not in _REVISIONS},
    }
    text = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode()).hexdigest()


__all__ = ["FINGERPRINT_VERSION", "attempt_fingerprint"]
