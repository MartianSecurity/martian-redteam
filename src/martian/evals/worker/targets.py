"""The targets a worker calls: functions in its process, HTTP targets it calls itself, and registered
agents it calls through martian-agents.

A run names its target by id, the launch request's ``agent_id``, and the worker knows each id as
one of these:

- a ``TargetCall``, a function in the worker's process;
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

``${NAME}`` in an HTTP target's url or a header value is filled in from the worker's
:class:`~martian.evals.worker.Credentials`, asked with the principal, the target's id and NAME in
the process that calls the target, each time it calls it. Neither the launch request nor a record
carries a credential: the run keeps the target's id, and its pin keeps a digest of the target as
written, with ``${NAME}`` unexpanded. A reference with no value fails the case with the setting
that holds it.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import TYPE_CHECKING, Any

import httpx

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


def http_pin(target_id: str, target: Target) -> dict[str, Any]:
    """What pins a run of an HTTP target: its id and a digest of it as written, never a value."""
    _steps(target_id, target)
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
    target: Target,
    *,
    calls: Collection[str],
    http: Mapping[str, Target],
    executor: AgentExecutor | None,
) -> dict[str, Any]:
    """What pins a run of ``target``: an HTTP target's :func:`http_pin`, a function's id, or the
    executor's pin of a registered agent. An id the worker does not know is ``not_found``."""
    agent_id = target.agent_id if target.kind == "agent" else None
    if agent_id is None:
        raise StoreError("not_found", NOT_FOUND)
    if agent_id in http:
        return http_pin(agent_id, http[agent_id])
    if agent_id in calls:
        return {"kind": "agent", "agent_id": agent_id}
    if executor is not None:
        return await executor.check(principal, agent_id)
    raise StoreError("not_found", NOT_FOUND)


def target_call(
    principal: Principal,
    run: Run,
    *,
    targets: Mapping[str, TargetCall | Target],
    credentials: Credentials | None,
    executor: AgentExecutor | None,
    timeout_seconds: float,
) -> TargetCall | None:
    """The call for ``run``'s target, or None when the worker cannot call it."""
    target = run.target
    if target is None or target.kind != "agent" or target.agent_id is None:
        return None
    found = targets.get(target.agent_id)
    if isinstance(found, Target):
        return http_call(principal, target.agent_id, found, credentials=credentials, timeout_seconds=timeout_seconds)
    if found is not None:
        return found
    if executor is None:
        return None
    agents = executor

    async def call(case: Case) -> Any:
        return await agents.call(principal, run, case, timeout_seconds=timeout_seconds)

    return call


__all__ = ["CLIENT_SLACK_SECONDS", "http_call", "http_pin", "http_target", "target_call", "target_pin"]
