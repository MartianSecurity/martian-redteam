"""The targets a worker calls: functions in its process, HTTP targets it calls itself, and registered
agents it calls through martian-agents.

A run names its target by id, the launch request's ``agent_id``, and the worker knows each id as
one of these:

- a ``TargetCall``, a function in the worker's process;
- an HTTP target, the shared model's ``Target(kind="http")``. The worker POSTs each case to its
  ``url`` itself, so a run needs no martian-agents service. The rules are a suite file's URL
  target's (:func:`martian.evals.runner._http_target`, whose pieces this reuses): one request per
  case, no retry, no redirect, no proxy from the environment, and ``body`` filled in by
  :func:`martian.evals.runner.request_body` with ``{{input}}`` (the case's prompt, the text the
  server sends a registered agent), ``{{inputs.NAME}}``, ``{{uuid}}``, and the placeholders of a
  conversation of one turn: ``{{session_id}}`` a new id, ``{{history}}`` empty and
  ``{{messages}}`` the prompt as one user message. The output is the response's text, or the value at
  ``response_path``. A response that is not 2xx, a missing value or an unreachable endpoint is the
  case's error, in words that name the setting to check and never repeat the address, a header
  or the response. The model that answered is the text at ``model_path`` in the response's JSON,
  or the ``model_header`` header, when the target names one (:func:`response_model`);
- a registered agent of the worker's :class:`~martian.evals.worker.AgentExecutor`, which needs
  leased credentials and runs through martian-agents. The worker asks the executor only for ids
  it does not know itself.

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
a function by its id and kind only, since the installation's own code has nothing to digest; and
a registered agent by the pin its executor gives. A credential's value is not part of any pin.

Every call has the worker's time limit, ``target_timeout_seconds``, whatever the target: a call
that outlasts it is the case's error, ``item timed out``. An HTTP target's client and a registered
agent's executor stop their own calls, but a function is the installation's own code, which may
block its event loop or never return. So a function runs on an event loop of a thread of its own
(:class:`FunctionTargets`), and the worker's loop waits for it with the deadline: a function that
outlasts it ends its case, and the run goes on. Python cannot stop a thread, so a function that
does not end once it is cancelled keeps its thread until the process ends, and the next function
call gets a new thread. One loop for every call, rather than one per call, keeps what a function
shares between calls, such as an HTTP client, on one loop. Each run's process ends when its run
does (:class:`~martian.evals.worker.ProcessWorker`), which ends such a thread too.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Collection, Mapping
from typing import TYPE_CHECKING, Any

import httpx

from martian.evals._digits import ascii_digits
from martian.evals.model import MODEL_NAME_LENGTH, Case, Run, Target, digest
from martian.evals.runner import (
    TargetCallError,
    _check_url,
    _expand,
    _reply,
    _response_steps,
    _unreachable,
    request_body,
)
from martian.evals.server.projection import prompt_text
from martian.evals.store import StoreError
from martian.evals.worker import AgentExecutor, Credentials, TargetCall, TargetReply

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


class TargetRefused(ValueError):
    """A target the worker has but will not call, such as one in ``targets`` that is not an HTTP
    target. The message names the worker's setting and never repeats a value of it."""


def reported_model(value: Any) -> str | None:
    """The model a target reported, as a run and a trace keep it: text, stripped, of at most
    :data:`~martian.evals.model.MODEL_NAME_LENGTH` characters; None for anything else."""
    if not isinstance(value, str):
        return None
    name = value.strip()
    return name if 0 < len(name) <= MODEL_NAME_LENGTH and name.isprintable() else None


def http_target(target: Target) -> Target:
    """``target``, which must be an HTTP target."""
    if target.kind != "http":
        raise ValueError("the worker calls a Target itself only when it is an http target")
    return target


def _steps(target_id: str, target: Target) -> list[str]:
    """The steps of the HTTP target ``target_id``'s ``response_path``, refused as a suite file's
    are but naming the worker's setting, ``targets[target_id].response_path``. It is checked each
    time a check or a run names the target, so one wrong target never stops the others."""
    return _response_steps(http_target(target).response_path, f"targets[{target_id!r}].response_path")


def _model_steps(target_id: str, target: Target) -> list[str]:
    """The steps of the HTTP target's ``model_path``, refused as its ``response_path`` is."""
    return _response_steps(http_target(target).model_path, f"targets[{target_id!r}].model_path")


def response_model(response: httpx.Response, target: Target, steps: list[str]) -> str | None:
    """The model an HTTP target's response reports: the ``model_header`` header, or the text at
    ``model_path`` (``steps``) in its JSON. None when the target names neither, or the response
    has no such text; that is not the case's error, and the case's output stands."""
    if target.model_header:
        return reported_model(response.headers.get(target.model_header))
    if not steps:
        return None
    try:
        found = response.json()
    except ValueError:
        return None
    for step in steps:
        if isinstance(found, dict) and step in found:
            found = found[step]
        elif isinstance(found, list) and ascii_digits(step) and int(step) < len(found):
            found = found[int(step)]
        else:
            return None
    return reported_model(found)


def http_pin(target_id: str, target: Target) -> dict[str, Any]:
    """What pins a run of an HTTP target: its id and a digest of it as written, never a value."""
    _steps(target_id, target)
    _model_steps(target_id, target)
    return {"kind": "http", "agent_id": target_id, "digest": digest(target.model_dump(mode="json"))}


def http_call(
    principal: Principal,
    target_id: str,
    target: Target,
    *,
    credentials: Credentials | None,
    timeout_seconds: float,
) -> TargetCall:
    """Call the HTTP target ``target_id`` once per case, with ``${NAME}`` filled in from
    ``credentials``. ``timeout_seconds`` is the worker's bound on one call. Each call returns a
    :class:`~martian.evals.worker.TargetReply` with the model the response reports
    (:func:`response_model`)."""
    steps, model_steps = _steps(target_id, target), _model_steps(target_id, target)

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
        body = request_body(target.body, case.inputs, "message", prompt=prompt_text(case.inputs))
        timeout = timeout_seconds + CLIENT_SLACK_SECONDS
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False) as client:
                response = await client.post(url, headers=headers, json=body)
        except httpx.HTTPError as error:
            raise _unreachable(error) from None
        return TargetReply(_reply(response, steps), response_model(response, target, model_steps))

    return call


async def target_pin(
    principal: Principal,
    target: Target | None,
    *,
    calls: Collection[str],
    http: Mapping[str, Target],
    executor: AgentExecutor | None,
) -> dict[str, Any]:
    """What pins a run of ``target``: an HTTP target's :func:`http_pin`, a function's id, or the
    executor's pin of a registered agent. An id the worker does not know is ``not_found``; an HTTP
    target it refuses is :class:`TargetRefused`, naming ``targets[ID]``."""
    agent_id = target.agent_id if target is not None and target.kind == "agent" else None
    if agent_id is None:
        raise StoreError("not_found", NOT_FOUND)
    if agent_id in http:
        try:
            return http_pin(agent_id, http[agent_id])
        except ValueError as error:
            # A refusal that names the target's setting already, as a response_path one does, is
            # not given the setting a second time.
            setting, reason = f"targets[{agent_id!r}]", str(error)
            raise TargetRefused(reason if setting in reason else f"{reason}; check {setting}") from None
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
) -> TargetCall | None:
    """The call for ``run``'s target, or None when the worker cannot call it. A function runs on
    the function targets' thread (:data:`FUNCTIONS`)."""
    target = run.target
    if target is None or target.kind != "agent" or target.agent_id is None:
        return None
    found = targets.get(target.agent_id)
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
    "FunctionTargets",
    "TargetRefused",
    "changed_target",
    "http_call",
    "http_pin",
    "http_target",
    "reported_model",
    "response_model",
    "target_call",
    "target_pin",
]
