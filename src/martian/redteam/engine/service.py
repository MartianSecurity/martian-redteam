"""Which service generates and judges the attacks of a version 2 run.

- **Local**, on the user's own models (:class:`~martian.redteam.engine.local.LocalService`), when the run
  file names ``models.attacker`` and no Martian connection is configured.
- **Hosted**, by the Martian platform (:class:`martian.redteam.client.Client`), when the run file names no
  models and a connection is configured (``MARTIAN_API_URL`` and ``MARTIAN_API_TOKEN``).
- **Local without an attacker**, when none of the run file's attack methods calls the attacker model
  (fixed templates, mutation, transforms layered on them) and no connection is configured: the
  judge, if the run file names one, is the user's own; without one, only checks decided by code run.

Anything else stops before the run starts, with a message that says what to change. A run never
moves from one service to the other: both configured is a choice the user makes, not one the SDK
guesses, and neither is an error.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Literal

from martian.redteam.methods import needs_attacker
from martian.redteam.runfile import RunFile

ServiceKind = Literal["local", "hosted"]

CONNECTION_VARIABLES = ("MARTIAN_API_URL", "MARTIAN_API_TOKEN")


class ServiceSelectionError(ValueError):
    """The run file and the environment do not name exactly one service."""


def connection_configured(environ: Mapping[str, str] | None = None) -> bool:
    """Whether a Martian connection is configured: either connection variable is set."""
    environ = os.environ if environ is None else environ
    return any(environ.get(name, "").strip() for name in CONNECTION_VARIABLES)


def select_service(run_file: RunFile, environ: Mapping[str, str] | None = None) -> ServiceKind:
    """``local`` or ``hosted`` for this run file in this environment, or :class:`ServiceSelectionError`."""
    connected = connection_configured(environ)
    models = run_file.models
    if run_file.environment is not None:
        raise ServiceSelectionError(
            "environment runs the probes in a Martian environment, which only a Martian server's campaign "
            "engine can do; this command runs the local engine. Remove environment to run here."
        )
    if models.attacker is not None and connected:
        raise ServiceSelectionError(
            "The run file names models, so attacks run locally with your own key, and a Martian connection "
            "is also configured (MARTIAN_API_URL, MARTIAN_API_TOKEN). Unset those variables to run locally, "
            "or remove models to have the Martian platform generate and judge the attacks."
        )
    if models.attacker is not None:
        return "local"
    if not needs_attacker(run_file):
        return _without_attacker(run_file, connected)
    if models.judge is not None:
        raise ServiceSelectionError(
            "models.judge needs models.attacker: name both to run locally. Only the attack methods that "
            "need no attacker (Templates, Mutation) run with a judge alone."
        )
    if connected:
        return "hosted"
    raise ServiceSelectionError(
        "Nothing can generate the attacks: name models.attacker and models.judge to run locally with your own "
        "key, or set MARTIAN_API_URL and MARTIAN_API_TOKEN to use the Martian platform."
    )


def _without_attacker(run_file: RunFile, connected: bool) -> ServiceKind:
    """The service of a run whose methods never call the attacker."""
    judged = any(
        not spec.checks or any(check.type == "judge" for check in spec.checks) for spec in run_file.vulnerabilities
    )
    if run_file.models.judge is not None:
        if connected:
            raise ServiceSelectionError(
                "The run file names models.judge, so the judge runs locally with your own key, and a Martian "
                "connection is also configured (MARTIAN_API_URL, MARTIAN_API_TOKEN). Unset those variables, or "
                "remove models to have the Martian platform judge."
            )
        return "local"
    if connected:
        return "hosted"
    if judged:
        raise ServiceSelectionError(
            "A vulnerability is decided by the judge (it has no checks, or a judge check), and nothing can judge: "
            "name models.judge, give it checks decided by code, or set MARTIAN_API_URL and MARTIAN_API_TOKEN."
        )
    return "local"


__all__ = ["CONNECTION_VARIABLES", "ServiceKind", "ServiceSelectionError", "connection_configured", "select_service"]
