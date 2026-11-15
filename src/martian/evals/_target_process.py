"""The process a function target runs in, away from the run that grades it.

:mod:`martian.evals.isolation` starts this file as a script, ``python -P _target_process.py``, so
the child imports only the standard library before the target: never the evals package, whose
memory would hold nothing of the run's anyway, but which is slow to import. The protocol is one
JSON object per line:

- the parent's first line names the target (``file``, ``function``), the paths the target must not
  reach (``private``), the parent's ``sys.path`` and whether to capture MLflow spans (``trace``),
  with the modules MLflow's autologging traces (``traced``). Spans are captured only when the
  target's module imported MLflow or one of those, since loading MLflow takes seconds;
- the child answers ``{"ready": true, "files": <reason or null>}`` once the target is imported, or
  ``{"ready": false, "error": ...}``. ``files`` is null when the private paths are hidden, else why
  they are not;
- each later line is a call, ``{"id": N, "inputs": {...}}``: the case's inputs and nothing else.
  The child calls the function with them as keyword arguments on a thread of its own and answers
  ``{"id": N, "value": ...}``, ``{"id": N, "raised": <exception type>}`` or, for a reply that is
  not JSON, ``{"id": N, "error": <reason>}``, each with ``trace``: the spans the call recorded,
  when it records MLflow spans;
- the parent closes the child's input to end it.

The protocol uses the process's standard input and output as it starts; the child then points its
own standard output at standard error and its standard input at the null device, so a target that
prints or reads cannot disturb it.

On Linux with Landlock (5.13 and later, when the kernel enables it), the child hides the private
paths from itself and every process it starts, before it imports the target: it cannot read, write,
create, rename or remove them, and cannot create, rename or remove anything directly in the folders
that hold them, though it can list those folders' names. Everything else stays as the user's
permissions allow. Landlock also stops it tracing or reading the memory of the process that started
it. Elsewhere the paths are not hidden, and ``files`` says why.
"""

from __future__ import annotations

import ctypes
import importlib.util
import json
import os
import struct
import sys
import threading
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any, BinaryIO

TARGET_MODULE = "martian_evals_target"

# Landlock (include/uapi/linux/landlock.h). The system call numbers are the same on every
# architecture Linux added them to at once.
_CREATE_RULESET, _ADD_RULE, _RESTRICT_SELF = 444, 445, 446
_CREATE_RULESET_VERSION = 1
_RULE_PATH_BENEATH = 1
_PR_SET_NO_NEW_PRIVS = 38
_EXECUTE, _WRITE_FILE, _READ_FILE, _READ_DIR = 1 << 0, 1 << 1, 1 << 2, 1 << 3
_REFER, _TRUNCATE, _IOCTL_DEV = 1 << 13, 1 << 14, 1 << 15


def _rights(abi: int) -> tuple[int, int]:
    """Every file system right this Landlock ABI knows, and the ones that apply to a file."""
    every = (1 << 13) - 1  # ABI 1: execute to make_sym
    files = _EXECUTE | _WRITE_FILE | _READ_FILE
    if abi >= 2:
        every |= _REFER
    if abi >= 3:
        every |= _TRUNCATE
        files |= _TRUNCATE
    if abi >= 5:
        every |= _IOCTL_DEV
        files |= _IOCTL_DEV
    return every, files


def _within(path: str, folder: str) -> bool:
    return path == folder or path.startswith(folder.rstrip("/") + "/")


def _allowed(private: list[str]) -> tuple[list[str], list[str]]:
    """The folders on the way to a private path, which keep only the right to list them, and every
    other file and folder beside them, which keep every right. Nothing at or under a private path
    is in either list, nor is a symbolic link: what a link points to is covered where it is."""
    on_the_way: list[str] = []
    free: list[str] = []

    def cover(folder: str) -> None:
        on_the_way.append(folder)
        below = [path for path in private if path != folder and _within(path, folder)]
        try:
            names = sorted(os.listdir(folder))
        except OSError:  # unreadable: keep only the way down to the private paths
            prefix = folder.rstrip("/") + "/"
            names = sorted({path[len(prefix) :].split("/", 1)[0] for path in below})
        for name in names:
            path = os.path.join(folder, name)
            if path in private or os.path.islink(path):
                continue
            if any(_within(hidden, path) for hidden in below) and os.path.isdir(path):
                cover(path)
            elif not any(_within(hidden, path) for hidden in below):
                free.append(path)

    cover("/")
    return on_the_way, free


def hide(private: list[str]) -> str | None:
    """Hide ``private`` (absolute, resolved paths) from this process and its children with
    Landlock. Returns None once they are hidden, or why they could not be."""
    if not private:
        return None
    if sys.platform != "linux":
        return f"the target's files are not hidden on {sys.platform}; Landlock needs Linux"
    libc = ctypes.CDLL(None, use_errno=True)
    syscall = libc.syscall
    syscall.restype = ctypes.c_long
    abi = syscall(ctypes.c_long(_CREATE_RULESET), None, ctypes.c_size_t(0), ctypes.c_uint32(_CREATE_RULESET_VERSION))
    if abi < 1:
        return "the target's files are not hidden: this Linux kernel does not enable Landlock"
    every, files = _rights(abi)
    attribute = ctypes.create_string_buffer(struct.pack("=Q", every))
    ruleset = syscall(ctypes.c_long(_CREATE_RULESET), attribute, ctypes.c_size_t(8), ctypes.c_uint32(0))
    if ruleset < 0:
        return f"the target's files are not hidden: Landlock refused a ruleset ({os.strerror(ctypes.get_errno())})"
    try:
        on_the_way, free = _allowed(private)
        for path, rights in [*((folder, _READ_DIR) for folder in on_the_way), *((path, every) for path in free)]:
            try:
                descriptor = os.open(path, os.O_PATH | os.O_CLOEXEC)
            except OSError:
                continue  # gone since it was listed
            try:
                given = rights if os.path.isdir(path) else rights & files
                rule = ctypes.create_string_buffer(struct.pack("=Qi", given, descriptor))
                if syscall(ctypes.c_long(_ADD_RULE), ctypes.c_int(ruleset), ctypes.c_int(_RULE_PATH_BENEATH), rule, 0):
                    error = os.strerror(ctypes.get_errno())
                    return f"the target's files are not hidden: Landlock refused a rule ({error})"
            finally:
                os.close(descriptor)
        if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0):
            return "the target's files are not hidden: the process could not give up new privileges"
        if syscall(ctypes.c_long(_RESTRICT_SELF), ctypes.c_int(ruleset), ctypes.c_uint32(0)):
            return f"the target's files are not hidden: Landlock refused ({os.strerror(ctypes.get_errno())})"
    finally:
        os.close(ruleset)
    return None


class _Tracer:
    """Captures the MLflow spans of each call, so the parent can add them to the case's trace: each
    call runs in a span of its own, and the spans under it are taken from MLflow's memory before
    that span ends, so nothing is written anywhere. MLflow's autologging is on, as the evaluation
    harness turns it on, so a target that calls a supported library records its spans as before."""

    def __init__(self) -> None:
        import mlflow
        from mlflow.models.evaluation.utils.trace import configure_autologging_for_evaluation
        from mlflow.tracing.trace_manager import InMemoryTraceManager

        self._mlflow = mlflow
        self._manager = InMemoryTraceManager.get_instance()  # type: ignore[no-untyped-call]
        configure_autologging_for_evaluation(enable_tracing=True).__enter__()
        self.call(lambda: None)  # opens MLflow's tracing now, not in the first case's time

    def call(self, function: Callable[[], Any]) -> tuple[Any, BaseException | None, dict[str, Any] | None]:
        value: Any = None
        failure: BaseException | None = None
        with self._mlflow.start_span(name="martian-target") as root:
            try:
                value = function()
            except Exception as error:  # noqa: BLE001 - the target's code may raise anything
                failure = error
            with self._manager.get_trace(root.trace_id) as trace:
                if trace is None:
                    return value, failure, None
                info = trace.info.to_dict()
                spans = [span.to_dict() for span in trace.span_dict.values() if span.span_id != root.span_id]
            self._manager.pop_trace(root._span.context.trace_id)  # nothing to export when the span ends
        if not spans:
            return value, failure, None
        known = {span["span_id"] for span in spans}
        for span in spans:  # the spans directly under this call's span go under the parent's
            if span.get("parent_span_id") not in known:
                span["parent_span_id"] = None
        info["state"] = "ERROR" if failure is not None else "OK"
        return value, failure, {"info": info, "data": {"spans": spans}}


def _load(file: str, function: str) -> Callable[..., Any]:
    """The target function, imported as a suite run imports it (``martian.evals._custom``)."""
    spec = importlib.util.spec_from_file_location(TARGET_MODULE, file)
    if spec is None or spec.loader is None or not Path(file).is_file():
        raise LookupError("could not load the target file")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as error:  # noqa: BLE001 - the customer's module may raise anything on import
        raise LookupError(f"the target file failed to import: {type(error).__name__}") from None
    found = getattr(module, function, None)
    if found is None or not callable(found):
        raise LookupError(f"the target file has no function {function!r}")
    return found  # type: ignore[no-any-return]


def _json(value: Any) -> str | None:
    """Why ``value`` cannot go back to the parent, or None when it can."""
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return f"the target returned {type(value).__name__}, which is not JSON; return text, a number, a list or a dict"
    return None


def main() -> int:
    requests: BinaryIO = os.fdopen(os.dup(0), "rb")
    replies: BinaryIO = os.fdopen(os.dup(1), "wb")
    null = os.open(os.devnull, os.O_RDONLY)
    os.dup2(null, 0)
    os.close(null)
    os.dup2(2, 1)  # what the target prints goes to standard error
    lock = threading.Lock()

    def send(message: dict[str, Any]) -> None:
        line = json.dumps(message).encode("utf-8") + b"\n"
        with lock:
            replies.write(line)
            replies.flush()

    setup = json.loads(requests.readline())
    files = hide([str(path) for path in setup["private"]])
    sys.path[:] = [str(entry) for entry in setup["path"]]
    try:
        function = _load(str(setup["file"]), str(setup["function"]))
    except LookupError as error:
        send({"ready": False, "error": str(error)})
        return 0
    # MLflow's import hooks fire at once for a library the target already imported.
    traced = setup["trace"] and any(name in sys.modules for name in ("mlflow", *setup["traced"]))
    tracer = _Tracer() if traced else None
    send({"ready": True, "files": files})

    def answer(number: int, inputs: dict[str, Any]) -> None:
        trace = None
        if tracer is not None:
            value, failure, trace = tracer.call(lambda: function(**inputs))
        else:
            try:
                value, failure = function(**inputs), None
            except Exception as error:  # noqa: BLE001 - the target's code may raise anything
                value, failure = None, error
        if failure is not None:  # only its type: the message is the target's own and may hold anything
            send({"id": number, "raised": type(failure).__name__, "trace": trace})
        elif (problem := _json(value)) is not None:
            send({"id": number, "error": problem, "trace": trace})
        else:
            send({"id": number, "value": value, "trace": trace})

    for line in requests:
        request = json.loads(line)
        threading.Thread(
            target=answer, args=(request["id"], request["inputs"]), name="martian-evals-target", daemon=True
        ).start()
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except BaseException:  # noqa: BLE001 - a broken protocol ends the process; say why on stderr
        traceback.print_exc()
        code = 1
    sys.stderr.flush()
    os._exit(code)  # calls still running on their threads end with the process
