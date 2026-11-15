"""The targets a worker calls: functions, HTTP targets it calls itself, and registered agents it calls
through martian-agents.

A run names its target by id, the launch request's ``agent_id``, and the worker knows each id as
one of these:

- a function target, the shared model's ``Target(kind="function")``: a function in a Python file,
  which the worker runs in a process of its own for each run (:class:`FunctionProcessCall`, through
  :class:`martian.evals.isolation.TargetProcess`). It is called with the case's inputs as keyword
  arguments, as a suite file's function target is, and the worker's private paths
  (``WorkerServices.private_paths``), such as its stores' files, are hidden from it on Linux. The
  worker pins it by the target as written and the contents of its file;
- a ``TargetCall``, a function in the worker's own process, for tests and code the installation
  trusts with everything the work's process holds: nothing isolates it;
- an HTTP target, the shared model's ``Target(kind="http")``. The worker POSTs each case to its
  ``url`` itself, so a run needs no martian-agents service. The rules are a suite file's URL
  target's (:func:`martian.evals.runner._http_target`, whose pieces this reuses): one request per
  case, no retry, no redirect, no proxy from the environment, and ``body`` filled in with
  ``{{input}}`` (the case's prompt, the text the server sends a registered agent),
  ``{{inputs.NAME}}`` and ``{{uuid}}``. The output is the response's text, or the value at
  ``response_path``. A response that is not 2xx, a missing value or an unreachable endpoint is the
  case's error, in words that name the setting to check and never repeat the address, a header
  or the response;
- a registered agent of the worker's :class:`~martian.evals.worker.AgentExecutor`, which needs
  leased credentials and runs through martian-agents. The worker asks the executor only for ids
  it does not know itself.

Whatever the target, it is given only the case's id and inputs (:func:`sent_case`): never its
expectations, a stored output, its tags or its metadata. An HTTP target's body placeholders name
only the case's inputs, and one that names anything else, such as ``{{expectations.answer}}``, is
refused when the worker checks the target, before any run calls it.

``${NAME}`` in an HTTP target's url or a header value is filled in from the worker's
:class:`~martian.evals.worker.Credentials`, asked with the principal, the target's id and NAME in
the process that calls the target, each time it calls it. Neither the launch request nor a record
carries a credential: the run keeps the target's id, and its pin keeps a digest of the target as
written, with ``${NAME}`` unexpanded. A reference with no value fails the case with the setting
that holds it.

A run calls only the target it was admitted with. Before it calls its target, on its first start
and on every resume, the worker pins the target as it has it now (:func:`target_pin`), in the
process that calls it, and a pin that differs from the run's fails the run, naming the target,
before any call (:func:`changed_target`). So an HTTP target is compared by its digest, as written;
a function target by its digest and its file's contents; a ``TargetCall`` by its id and kind only,
since the installation's own code has nothing to digest; and a registered agent by the pin its
executor gives. A credential's value is not part of any pin.

Every call has the worker's time limit, ``target_timeout_seconds``, whatever the target: a call
that outlasts it is the case's error, ``item timed out``. An HTTP target's client and a registered
agent's executor stop their own calls. A function target's process runs each call on a thread of
its own, and ends with the run. A ``TargetCall`` is the installation's own code, which may block
its event loop or never return. So it runs on an event loop of a thread of its own
(:class:`FunctionTargets`), and the worker's loop waits for it with the deadline: a function that
outlasts it ends its case, and the run goes on. Python cannot stop a thread, so a function that
does not end once it is cancelled keeps its thread until the process ends, and the next function
call gets a new thread. One loop for every call, rather than one per call, keeps what a function
shares between calls, such as an HTTP client, on one loop. Each run's process ends when its run
does (:class:`~martian.evals.worker.ProcessWorker`), which ends such a thread too.
"""

from __future__ import annotations

import asyncio
import hashlib
import threading
from collections.abc import Collection, Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from martian.evals.isolation import TargetProcess, TargetRaised
from martian.evals.model import Case, Run, Target, digest
from martian.evals.runner import (
    TargetCallError,
    _case_values,
    _check_url,
    _expand,
    _fill,
    _reply,
    _response_steps,
    _unreachable,
    check_body,
)
from martian.evals.server.projection import prompt_text
from martian.evals.store import StoreError
from martian.evals.worker import AgentExecutor, Credentials, TargetCall

if TYPE_CHECKING:
    from martian.evals.server.principal import Principal

CLIENT_SLACK_SECONDS = 5.0
"""How much longer than the worker's own bound the HTTP client waits, so that the worker's bound
ends a slow call first, as ``item timed out``."""

NOT_FOUND = "Agent not found"

PIN_KEY = "target"
"""``Run.provenance`` key of the pin of the target a run was admitted with."""

TARGET_CHANGED = (
    "The target {target} changed since this run started; the run did not call the changed target. "
    "Start a new run to evaluate it as it is now"
)
"""The error of a run whose target is not the one it was admitted with (:func:`changed_target`)."""

GRACE_SECONDS = 1.0
"""How long a function whose call timed out has to end once it is cancelled. One that does not is
left on its thread, and the next call gets a new one."""


class FunctionTargets:
    """Runs a worker's function targets on an event loop of a thread of its own, so that a function
    that blocks its loop, ignores cancellation or never returns cannot hold up the worker's loop.
    The thread starts with the first call; one whose function did not end after its deadline is
    left behind, a daemon thread that ends with the process, and the next call starts another."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    def _current(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is None:
                loop = asyncio.new_event_loop()
                threading.Thread(target=loop.run_forever, name="martian-evals-function-target", daemon=True).start()
                self._loop = loop
            return self._loop

    def _leave(self, loop: asyncio.AbstractEventLoop) -> None:
        with self._lock:
            if self._loop is loop:
                self._loop = None

    def call(self, function: TargetCall) -> TargetCall:
        """``function`` as a target call that runs on this thread's loop. Cancelled, as by the
        worker's deadline, it cancels the function and returns at once, whether or not the
        function ends."""

        async def call(case: Case) -> Any:
            loop = self._current()
            ended = threading.Event()

            async def run() -> Any:
                try:
                    return await function(case)
                finally:
                    ended.set()

            future = asyncio.run_coroutine_threadsafe(run(), loop)
            try:
                return await asyncio.wrap_future(future)
            except asyncio.CancelledError:
                future.cancel()
                if not await asyncio.to_thread(ended.wait, GRACE_SECONDS):
                    self._leave(loop)  # stuck: the next call gets a thread of its own
                raise

        return call


FUNCTIONS = FunctionTargets()
"""The function targets' thread, one for the process."""


def sent_case(case: Case) -> Case:
    """What a target is given of ``case``: its id and its inputs. Never its expectations, a stored
    output, its tags or its metadata, which grade it or describe it to the grader."""
    return Case(id=case.id, inputs=dict(case.inputs))


def http_target(target: Target) -> Target:
    """``target``, which must be an HTTP target."""
    if target.kind != "http":
        raise ValueError("the worker calls a Target over HTTP only when it is an http target")
    return target


def own_target(target: Target) -> Target:
    """``target``, which must be a target the worker calls itself: an HTTP or a function target."""
    if target.kind not in ("http", "function"):
        raise ValueError("the worker calls a Target itself only when it is an http or a function target")
    return target


def _steps(target_id: str, target: Target) -> list[str]:
    """The steps of the HTTP target ``target_id``'s ``response_path``, refused as a suite file's
    are but naming the worker's setting, ``targets[target_id].response_path``, after its body's
    placeholders, refused as a suite file's are. Both are checked each time a check or a run names
    the target, so one wrong target never stops the others."""
    check_body(http_target(target).body, f"targets[{target_id!r}].body")
    return _response_steps(target.response_path, f"targets[{target_id!r}].response_path")


def http_pin(target_id: str, target: Target) -> dict[str, Any]:
    """What pins a run of an HTTP target: its id and a digest of it as written, never a value."""
    _steps(target_id, target)
    return {"kind": "http", "agent_id": target_id, "digest": digest(target.model_dump(mode="json"))}


def function_pin(target_id: str, target: Target) -> dict[str, Any]:
    """What pins a run of a function target: its id and a digest of it as written and of its
    file's contents, so a run never calls code that changed since it was admitted."""
    try:
        contents = hashlib.sha256(Path(target.file or "").read_bytes()).hexdigest()
    except OSError:
        contents = None
    written = target.model_dump(mode="json")
    return {"kind": "function", "agent_id": target_id, "digest": digest({"target": written, "file": contents})}


class FunctionProcessCall:
    """A function target's call for one run: the target in a process of its own
    (:class:`~martian.evals.isolation.TargetProcess`), which cannot reach ``private`` on Linux,
    started by :meth:`ready` before the run's first case and ended by :meth:`close` after its last.
    Each call sends the case's inputs as keyword arguments and returns the function's reply. One
    that raised is the case's error, ``the target raised <its type>``; the exception's own text is
    not kept, since it may hold anything."""

    def __init__(self, target: Target, *, private: Iterable[str | Path], timeout_seconds: float) -> None:
        self._target = target
        self._private = list(private)
        self._timeout = timeout_seconds + CLIENT_SLACK_SECONDS
        self._process: TargetProcess | None = None

    async def ready(self) -> None:
        """Start the target's process and wait until it has imported the target. A target it
        cannot import is a ``ConfigError`` naming why."""
        process = TargetProcess(
            Path(self._target.file or ""), self._target.function or "", private=self._private, trace=False
        )
        self._process = process
        await asyncio.to_thread(process.ready)

    async def __call__(self, case: Case) -> Any:
        assert self._process is not None, "ready() starts the target's process"
        try:
            value, _ = await asyncio.to_thread(self._process.call, dict(case.inputs), self._timeout)
        except TargetRaised as raised:
            raise TargetCallError(f"the target raised {raised}") from None
        return value

    def close(self) -> None:
        if self._process is not None:
            self._process.close()


def http_call(
    principal: Principal,
    target_id: str,
    target: Target,
    *,
    credentials: Credentials | None,
    timeout_seconds: float,
) -> TargetCall:
    """Call the HTTP target ``target_id`` once per case, with ``${NAME}`` filled in from
    ``credentials``. ``timeout_seconds`` is the worker's bound on one call."""
    steps = _steps(target_id, target)

    def value_of(name: str) -> str | None:
        return credentials(principal, target_id, name) if credentials is not None else None

    def request() -> tuple[str, dict[str, str]]:
        try:
            url = _expand(target.url or "", "target.url", value_of, "the credential")
            _check_url(url)
            headers = {
                name: _expand(value, f"target.headers.{name}", value_of, "the credential")
                for name, value in target.headers.items()
            }
        except ValueError as error:  # ConfigError included: both name the setting, never a value
            raise TargetCallError(str(error)) from None
        return url, headers

    async def call(case: Case) -> Any:
        url, headers = request()
        prompt, values = prompt_text(case.inputs), _case_values(case.inputs, "message")
        body = _fill(target.body, lambda name: prompt if name == "input" else values(name))
        timeout = timeout_seconds + CLIENT_SLACK_SECONDS
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False) as client:
                response = await client.post(url, headers=headers, json=body)
        except httpx.HTTPError as error:
            raise _unreachable(error) from None
        return _reply(response, steps)

    return call


async def target_pin(
    principal: Principal,
    target: Target | None,
    *,
    calls: Collection[str],
    own: Mapping[str, Target],
    executor: AgentExecutor | None,
) -> dict[str, Any]:
    """What pins a run of ``target``: an HTTP target's :func:`http_pin` or a function target's
    :func:`function_pin`, for the targets the worker calls itself (``own``), a ``TargetCall``'s id,
    or the executor's pin of a registered agent. An id the worker does not know is ``not_found``."""
    agent_id = target.agent_id if target is not None and target.kind == "agent" else None
    if agent_id is None:
        raise StoreError("not_found", NOT_FOUND)
    if agent_id in own:
        found = own_target(own[agent_id])
        return function_pin(agent_id, found) if found.kind == "function" else http_pin(agent_id, found)
    if agent_id in calls:
        return {"kind": "agent", "agent_id": agent_id}
    if executor is not None:
        return await executor.check(principal, agent_id)
    raise StoreError("not_found", NOT_FOUND)


def changed_target(run: Run, live: Mapping[str, Any]) -> str | None:
    """Why ``run`` must not call its target, when the target is not the one it was admitted with,
    or None when it is. ``live`` is the target's pin now (:func:`target_pin`), in the process that
    is about to call it, and the run's own is ``provenance["target"]``: every field of the live pin
    must have the value the run was pinned with. A field only admission added, such as the local
    edition's ``name``, is not compared, and a run with no pin, which no launch records, has
    changed. The reason names the target, by its name when its pin has one, and its id."""
    pinned = run.provenance.get(PIN_KEY)
    if isinstance(pinned, Mapping) and all(pinned.get(key) == value for key, value in live.items()):
        return None
    name = pinned.get("name") if isinstance(pinned, Mapping) else None
    agent_id = run.target.agent_id if run.target is not None else None
    return TARGET_CHANGED.format(target=f'"{name}" ({agent_id})' if isinstance(name, str) and name else agent_id)


def target_call(
    principal: Principal,
    run: Run,
    *,
    targets: Mapping[str, TargetCall | Target],
    credentials: Credentials | None,
    executor: AgentExecutor | None,
    timeout_seconds: float,
    private: Iterable[str | Path] = (),
) -> TargetCall | None:
    """The call for ``run``'s target, or None when the worker cannot call it. A function target
    runs in a process of its own that cannot reach ``private`` (:class:`FunctionProcessCall`, which
    the run readies and closes); a ``TargetCall`` on the function targets' thread
    (:data:`FUNCTIONS`). The call is given the case as :func:`sent_case` leaves it."""
    target = run.target
    if target is None or target.kind != "agent" or target.agent_id is None:
        return None
    found = targets.get(target.agent_id)
    if isinstance(found, Target) and found.kind == "function":
        return FunctionProcessCall(found, private=private, timeout_seconds=timeout_seconds)
    if isinstance(found, Target):
        return http_call(principal, target.agent_id, found, credentials=credentials, timeout_seconds=timeout_seconds)
    if found is not None:
        return FUNCTIONS.call(found)
    if executor is None:
        return None
    agents = executor

    async def call(case: Case) -> Any:
        return await agents.call(principal, run, case, timeout_seconds=timeout_seconds)

    return call


__all__ = [
    "CLIENT_SLACK_SECONDS",
    "FUNCTIONS",
    "GRACE_SECONDS",
    "PIN_KEY",
    "TARGET_CHANGED",
    "FunctionProcessCall",
    "FunctionTargets",
    "changed_target",
    "function_pin",
    "http_call",
    "http_pin",
    "http_target",
    "own_target",
    "sent_case",
    "target_call",
    "target_pin",
]
