"""Which service generates and judges the attacks of a version 2 run.

- **Local**, on the user's own models (:class:`~martian.redteam.engine.local.LocalService`), when the run
  file names ``models.attacker`` and no Martian connection is configured.
- **Hosted**, by the Martian platform (:class:`martian.redteam.client.Client`), when the run file names no
  models and a connection is configured (``MARTIAN_API_URL`` and ``MARTIAN_API_TOKEN``).

Anything else stops before the run starts, with a message that says what to change. A run never
moves from one service to the other: both configured is a choice the user makes, not one the SDK
guesses, and neither is an error.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Literal

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
    if models.judge is not None:
        raise ServiceSelectionError("models.judge needs models.attacker: name both to run locally.")
    if connected:
        return "hosted"
    raise ServiceSelectionError(
        "Nothing can generate the attacks: name models.attacker and models.judge to run locally with your own "
        "key, or set MARTIAN_API_URL and MARTIAN_API_TOKEN to use the Martian platform."
    )


__all__ = ["CONNECTION_VARIABLES", "ServiceKind", "ServiceSelectionError", "connection_configured", "select_service"]
