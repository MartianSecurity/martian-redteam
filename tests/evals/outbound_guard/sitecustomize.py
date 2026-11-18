"""Refuses and records every outbound network attempt, for ``tests/evals/test_no_outbound.py``.

Python imports this module as ``sitecustomize`` when it starts, before any of the program's own
code, in a process whose ``PYTHONPATH`` names this folder, and so in every Python process that
process starts with its environment. With ``EVALS_TEST_OUTBOUND_DIR`` naming a folder, it:

- writes ``started-<pid>.json`` there, the process's command line, so a test can tell which
  processes ran with the guard;
- refuses every connection, datagram and name lookup for an address that is not loopback, through
  an audit hook, and appends each to ``attempts.log`` there with the stack that made it. The
  refusal is the ``OSError`` a network with no route out raises, so an attempt is recorded even
  when the library that made it swallows the error, as MLflow's telemetry does;
- appends the address of every connection to loopback to ``connections.log`` there, one ``host port``
  a line, so a test can tell which local servers a process reached;
- appends to ``attempts.log``, when the process exits, that MLflow's telemetry client started, if
  it did. MLflow starts the client when it is imported unless its telemetry is off, and sends from
  a background thread that a short process can end before, so the client itself is the evidence.

Without the variable it does nothing.
"""

from __future__ import annotations

import atexit
import errno
import ipaddress
import json
import os
import socket
import sys
import traceback
from pathlib import Path
from typing import Any

FOLDER_ENV = "EVALS_TEST_OUTBOUND_DIR"
"""Not a ``MARTIAN_*`` name: a function target's process does not inherit those
(``martian.evals.isolation``), and it runs under the guard too."""
ATTEMPTS = "attempts.log"
CONNECTIONS = "connections.log"
TELEMETRY_STARTED = "MLflow's telemetry client started"

_SENDS = frozenset({"socket.connect", "socket.sendto", "socket.sendmsg"})
_LOOKUPS = frozenset(
    {
        "socket.getaddrinfo",
        "socket.gethostbyname",
        "socket.gethostbyname_ex",
        "socket.gethostbyaddr",
        "socket.getnameinfo",
    }
)


def is_loopback(host: object) -> bool:
    """Whether ``host`` names this machine: ``localhost``, a loopback address, or no host at all,
    which ``getaddrinfo`` reads as the local addresses to listen on."""
    if host is None:
        return True
    name = (host.decode("ascii", "replace") if isinstance(host, bytes) else str(host)).split("%", 1)[0]
    if name.rstrip(".").lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def _record(folder: Path, what: str, *, stack: bool = True) -> None:
    where = "".join(traceback.format_stack()[:-2]) if stack else ""
    with (folder / ATTEMPTS).open("a", encoding="utf-8") as log:
        log.write(f"[{os.getpid()}: {' '.join(sys.orig_argv)}] {what}\n{where}\n")


def _guard(folder: Path) -> None:
    def hook(event: str, args: tuple[Any, ...]) -> None:
        if event in _SENDS:
            sock, address = args[0], args[1]
            internet = sock.family in (socket.AF_INET, socket.AF_INET6)
            if internet and address is not None and not is_loopback(address[0]):
                _record(folder, f"{event} {address!r}")
                raise OSError(errno.ENETUNREACH, f"outbound network blocked by the test: {address!r}")
            if internet and address is not None and event == "socket.connect":
                with (folder / CONNECTIONS).open("a", encoding="utf-8") as log:
                    log.write(f"{address[0]} {address[1]}\n")
        elif event in _LOOKUPS:
            host = args[0][0] if event == "socket.getnameinfo" else args[0]
            if not is_loopback(host):
                _record(folder, f"{event} {host!r}")
                raise socket.gaierror(socket.EAI_NONAME, f"outbound network blocked by the test: {host!r}")

    def telemetry() -> None:
        if "mlflow" not in sys.modules:
            return
        client = getattr(sys.modules.get("mlflow.telemetry"), "get_telemetry_client", None)
        if client is None:
            _record(folder, "MLflow has no mlflow.telemetry.get_telemetry_client: check how it turns telemetry off")
        elif client() is not None:
            _record(folder, TELEMETRY_STARTED, stack=False)

    sys.addaudithook(hook)
    (folder / f"started-{os.getpid()}.json").write_text(json.dumps(sys.orig_argv), encoding="utf-8")
    atexit.register(telemetry)  # registered first, so it runs after every other exit handler


if folder := os.environ.get(FOLDER_ENV):
    _guard(Path(folder))
