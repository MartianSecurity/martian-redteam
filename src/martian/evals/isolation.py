"""Function targets run in a process of their own, which cannot reach what grades them.

A function target is the code under test, and an agent under test may look for its answers: read
the data file, the suite file or the results, search the memory of the process it runs in, or
change a result. So neither ``martian evals run`` nor the evals core's worker calls a function
target in its own process. Each starts a :class:`TargetProcess` for the run, and sends it each
case's inputs and nothing else; the expectations, the checks and their scorers, the results and
the stores stay in the process that grades, which the target's process cannot read.

What the target's process has:

- **its arguments**: the case's inputs, as keyword arguments, as before, and in a conversation's
  turn the conversation's history and session id, those of them the function takes
  (:meth:`TargetProcess.signature`);
- **no memory of the run**: it is a new Python process (``python -P``) that has imported nothing
  of the evals package, and its parent sends it only inputs. On Linux, Landlock also stops it, and
  every process it starts, from tracing or reading the memory of its parent;
- **the environment, without the run's locations**: the parent's environment without
  ``MARTIAN_*`` variables or MLflow's tracking, experiment, registry, run and workspace variables
  (:data:`DROPPED_PREFIXES`, :data:`DROPPED`). ``TMPDIR`` is a folder of its own, removed with it,
  and ``MLFLOW_TRACKING_URI`` names a store in that folder;
- **the files, without the private paths**: on Linux with Landlock, the run's private paths, such
  as the suite file, the data file and the results folder, are hidden from it before it imports the
  target (:func:`martian.evals._target_process.hide`). It cannot read, write, create, rename or
  remove them, and cannot create, rename or remove anything directly in the folders that hold
  them, though it can list their names. On other systems the files are not hidden, and
  :meth:`TargetProcess.ready` says why;
- **the same working folder and import path** as its parent, so the target imports what it did.

Its reply comes back as JSON, so a target returns text, a number, a list or a dict. When the parent
asks for spans and the target's module imported MLflow or a library MLflow's autologging traces,
such as the OpenAI or Anthropic client, the process loads MLflow with that autologging on, and each
call's spans come back with its reply; the suite runner adds them under the case's trace. A library
imported only inside the function is not traced.

The cost: the process starts with the run, in about half a second, and one that loads MLflow takes
a few seconds more, before the run's first call. Its module-level state is shared by the run's
calls, as before, but not with the run's own process.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import inspect
import itertools
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import IO, Any

from martian.config import ConfigError
from martian.evals.runner import TIMED_OUT, TargetCallError

CHILD = Path(__file__).with_name("_target_process.py")

DROPPED_PREFIXES = ("MARTIAN_", "MLFLOW_TRACKING_", "MLFLOW_EXPERIMENT_", "MLFLOW_REGISTRY_")
"""Environment variables the target's process does not get: where the run, its stores and the
platform are, and their credentials."""
DROPPED = frozenset({"MLFLOW_RUN_ID", "MLFLOW_WORKSPACE"})

ENDED = "the target's process ended before it answered"
NOT_STARTED = "the target's process ended before it was ready"


class TargetRaised(Exception):
    """The target raised. The message is the exception's type only: its text is the target's own.
    ``trace`` holds the spans the call recorded before it raised, when it records any."""

    def __init__(self, raised: str, trace: dict[str, Any] | None = None) -> None:
        super().__init__(raised)
        self.trace = trace


def target_environment(environment: Mapping[str, str], private_folder: Path) -> dict[str, str]:
    """The environment of a target's process: ``environment`` without the run's locations, with
    its own temporary folder and an MLflow store of its own in it, so that MLflow, when the target
    or the process uses it, never writes beside the run. The spans the parent asks for are taken
    from MLflow's memory and never written there."""
    kept = {
        name: value
        for name, value in environment.items()
        if name not in DROPPED and not name.startswith(DROPPED_PREFIXES)
    }
    kept["TMPDIR"] = str(private_folder)
    kept["MLFLOW_TRACKING_URI"] = f"sqlite:///{private_folder / 'traces.db'}"
    return kept


def private_paths(paths: Iterable[str | Path]) -> list[str]:
    """``paths`` as the target's process hides them: absolute, with links resolved, each once."""
    return sorted({os.path.realpath(path) for path in paths})


class TargetProcess:
    """A function target in a process of its own, started at once. :meth:`ready` waits until it
    has imported the target; :meth:`call` sends one case's inputs; :meth:`close` ends it.

    ``private`` are the files and folders the target must not reach (:func:`private_paths`), and
    ``trace`` asks for the MLflow spans of each call, for a target whose module imported MLflow or a
    library MLflow's autologging traces. Calls may come from many threads at once;
    each runs on a thread of the target's process, as a function target ran before."""

    def __init__(self, file: Path, function: str, *, private: Iterable[str | Path], trace: bool) -> None:
        self._folder = Path(tempfile.mkdtemp(prefix="martian-target-"))
        self._lock = threading.Lock()
        self._numbers = itertools.count(1)
        self._waiting: dict[int, concurrent.futures.Future[dict[str, Any]]] = {}
        self._ready: concurrent.futures.Future[dict[str, Any]] = concurrent.futures.Future()
        self._ended = False
        self._process = subprocess.Popen(
            [sys.executable, "-P", str(CHILD)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            env=target_environment(os.environ, self._folder),
        )
        assert self._process.stdin is not None and self._process.stdout is not None
        self._requests: IO[bytes] = self._process.stdin
        traced: list[str] = []
        if trace:
            from mlflow.ml_package_versions import FLAVOR_TO_MODULE_NAME

            traced = sorted(set(FLAVOR_TO_MODULE_NAME.values()))
        setup = {
            "file": str(file.resolve()),
            "function": function,
            "private": private_paths(private),
            "path": list(sys.path),
            "trace": trace,
            "traced": traced,
        }
        self._send(setup)
        self._reader = threading.Thread(
            target=self._read, args=(self._process.stdout,), name="martian-evals-target-replies", daemon=True
        )
        self._reader.start()

    def _send(self, message: dict[str, Any]) -> None:
        self._requests.write(json.dumps(message).encode("utf-8") + b"\n")
        self._requests.flush()

    def _read(self, replies: IO[bytes]) -> None:
        for line in replies:
            try:
                message = json.loads(line)
            except ValueError:
                break  # not the protocol: the process is no longer one to call
            if "ready" in message:
                self._ready.set_result(message)
                continue
            with self._lock:
                waiting = self._waiting.get(message["id"])
            if waiting is not None and not waiting.done():
                waiting.set_result(message)
        with self._lock:
            self._ended = True
            left = list(self._waiting.values())
        if not self._ready.done():
            self._ready.set_result({"ready": False, "error": NOT_STARTED})
        for waiting in left:
            if not waiting.done():
                waiting.set_exception(TargetCallError(ENDED))

    def ready(self) -> str | None:
        """Wait until the target is imported. Returns None when the private paths are hidden from
        it, or why they are not. A target file that cannot be imported, or has no such function,
        is a ``ConfigError`` naming why, as when the run imported it itself."""
        message = self._ready.result()
        if not message["ready"]:
            raise ConfigError(str(message["error"]))
        files: str | None = message["files"]
        return files

    def signature(self) -> inspect.Signature:
        """The target function's keyword parameters, as its process read them once it imported it:
        one for each name it takes, and its ``**`` parameter when it has one. The target is never
        imported in this process, so this is how a caller learns what the function takes, such as
        whether a conversation's turn may give it ``history`` or ``session_id``. Waits as
        :meth:`ready` does; raises ``ValueError`` when the process could not read them."""
        message = self._ready.result()
        found = message.get("parameters") if message["ready"] else None
        if found is None:
            raise ValueError("the target's process could not read the function's parameters")
        kept = [inspect.Parameter(str(name), inspect.Parameter.KEYWORD_ONLY) for name in found["names"]]
        if found["rest"] is not None:
            kept.append(inspect.Parameter(str(found["rest"]), inspect.Parameter.VAR_KEYWORD))
        return inspect.Signature(kept)

    def call(self, inputs: Mapping[str, Any], timeout_seconds: float) -> tuple[Any, dict[str, Any] | None]:
        """The target's reply to one case's ``inputs``, and the MLflow trace of its spans when it
        records any. A call that has not answered within ``timeout_seconds`` is
        ``TargetCallError(TIMED_OUT)``; the target's thread goes on until it returns or the process
        ends. A target that raised is :class:`TargetRaised`; a reply that is not JSON, or a process
        that ended, is a ``TargetCallError`` saying so."""
        future: concurrent.futures.Future[dict[str, Any]] = concurrent.futures.Future()
        with self._lock:
            if self._ended:
                raise TargetCallError(ENDED)
            number = next(self._numbers)
            self._waiting[number] = future
            try:
                self._send({"id": number, "inputs": dict(inputs)})
            except OSError:
                self._waiting.pop(number, None)
                raise TargetCallError(ENDED) from None
        try:
            message = future.result(timeout_seconds)
        except concurrent.futures.TimeoutError:
            raise TargetCallError(TIMED_OUT) from None
        finally:
            with self._lock:
                self._waiting.pop(number, None)
        trace: dict[str, Any] | None = message.get("trace")
        if "raised" in message:
            raise TargetRaised(str(message["raised"]), trace)
        if "error" in message:
            raise TargetCallError(str(message["error"]))
        return message["value"], trace

    def close(self) -> None:
        """End the target's process and remove its folder. Calls still running end with it."""
        with contextlib.suppress(OSError):
            self._requests.close()
        try:
            self._process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait()
        self._reader.join(timeout=5)
        shutil.rmtree(self._folder, ignore_errors=True)


__all__ = [
    "CHILD",
    "DROPPED",
    "DROPPED_PREFIXES",
    "ENDED",
    "NOT_STARTED",
    "TargetProcess",
    "TargetRaised",
    "private_paths",
    "target_environment",
]
