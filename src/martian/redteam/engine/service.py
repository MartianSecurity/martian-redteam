"""Which service generates and judges the attacks of a version 2 run.

- **Local**, on the user's own models (:class:`~martian.redteam.engine.local.LocalService`), when the run
  file names ``models.attacker`` and no Martian connection is configured.
- **Hosted**, by the Martian platform (:class:`martian.redteam.client.Client`), when the run file names no
  models and a connection is configured (``MARTIAN_API_URL`` and ``MARTIAN_API_TOKEN``).

Anything else stops before the run starts, with a message that says what to change. A run never
moves from one service to the other: both configured is a choice the user makes, not one the SDK
guesses, and neither is an error.

A run file that names an ``environment`` runs in a Martian environment, which only a Martian server
has: it needs the connection whatever generates the attacks (:func:`require_environment_connection`).
With the connection, ``models`` still decides the service: the user's own models when it names an
attacker, else the platform. Without it the run stops before anything is called.
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


class EnvironmentNeedsServer(ServiceSelectionError):
    """The run file names an environment and no Martian connection is configured."""


def connection_configured(environ: Mapping[str, str] | None = None) -> bool:
    """Whether a Martian connection is configured: either connection variable is set."""
    environ = os.environ if environ is None else environ
    return any(environ.get(name, "").strip() for name in CONNECTION_VARIABLES)


def require_environment_connection(run_file: RunFile, environ: Mapping[str, str] | None = None) -> None:
    """Stop a run file that names an environment when no Martian connection is configured: an
    environment exists only on a Martian server, never in this package."""
    if run_file.environment is None or connection_configured(environ):
        return
    raise EnvironmentNeedsServer(
        f"environment {run_file.environment!r} is a Martian environment, and environments run only on a "
        "Martian server. Set MARTIAN_API_URL and MARTIAN_API_TOKEN to run this file there, or remove "
        "environment to run it here without one. Nothing was called."
    )


def select_service(run_file: RunFile, environ: Mapping[str, str] | None = None) -> ServiceKind:
    """``local`` or ``hosted`` for this run file in this environment, or :class:`ServiceSelectionError`."""
    require_environment_connection(run_file, environ)
    connected = connection_configured(environ)
    models = run_file.models
    if run_file.environment is not None:
        # The connection is the environment's; models alone decide who writes the attacks.
        if models.attacker is not None:
            return "local"
        if models.judge is not None:
            raise ServiceSelectionError("models.judge needs models.attacker: name both to run the attacks locally.")
        return "hosted"
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


__all__ = [
    "CONNECTION_VARIABLES",
    "EnvironmentNeedsServer",
    "ServiceKind",
    "ServiceSelectionError",
    "connection_configured",
    "require_environment_connection",
    "select_service",
]
