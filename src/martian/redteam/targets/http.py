"""An HTTP endpoint as a red team target: the suite file's URL target, with conversations.

The target block is a suite file's (:class:`martian.evals.runner.TargetRef`) and is called by the
same pieces: ``${NAME}`` in ``url`` and header values is read from the environment when the target
is built, ``body`` is filled by :func:`martian.evals.runner.request_body`, the reply is the response's
text or the value at ``response_path``, and a failed call is a
:class:`~martian.evals.runner.TargetCallError` that names the setting to check and never repeats the
address, a header or the response. One request per message: no retry, no redirect, no proxy from the
environment. ``environ`` stands in for the environment when ``${NAME}`` is read, as the local
edition's launcher passes one holding a local target's key.

Each message is one turn of a conversation (:mod:`martian.redteam.targets.conversations`). The body's
``{{session_id}}`` is the conversation's id, ``{{history}}`` its earlier turns and ``{{messages}}``
those and the new message, so the body decides whether the history is resent or only the newest turn
is sent. ``{{input}}`` is the message. ``tool_calls_path`` names where the JSON answer lists the tool
calls the target made or asked for, read by the reply contract (:mod:`martian.redteam.targets.reply`).
The run file's ``target.session`` adds two things a server may need: the id in a header as well, and
an id the server issues itself, read from each reply.

With an ``address_policy`` (:mod:`martian.evals._pinned_address`) every connection goes to an address
the policy allowed, by number, as the judges' connections do; an edition that calls endpoints other
people name passes one. A local run calls the endpoint its user named, which may be on the same
machine, and passes none.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from typing import Any

import httpx

from martian.evals._pinned_address import AddressPolicy, AddressRefused, pinned_session
from martian.evals.runner import (
    CLIENT_SLACK_SECONDS,
    TIMED_OUT,
    TargetCallError,
    TargetRef,
    _from_environment,
    _reply,
    _response_steps,
    _unreachable,
    request_body,
)
from martian.redteam.runfile import TargetSession as SessionSettings
from martian.redteam.targets import TargetReply
from martian.redteam.targets.conversations import Conversations
from martian.redteam.targets.reply import read_path, reply_text, tool_calls


class HttpTarget:
    """A JSON endpoint under attack. ``reference`` is the suite file's target block with a ``url``."""

    kind = "http"

    def __init__(
        self,
        reference: TargetRef,
        *,
        session: SessionSettings | None = None,
        tool_calls_path: str | None = None,
        address_policy: AddressPolicy | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        if reference.url is None:
            raise ValueError("an HTTP target needs a url")
        self._reference = reference
        self._session = session or SessionSettings()
        self._policy = address_policy
        self._url = _from_environment(reference.url, "target.url", environ)
        self._headers = {
            name: _from_environment(value, f"target.headers.{name}", environ)
            for name, value in reference.headers.items()
        }
        self._steps = _response_steps(reference.response_path, "target.response_path")
        self._session_steps = _response_steps(self._session.response_path, "target.session.response_path")
        self._call_steps = _response_steps(tool_calls_path, "target.tool_calls_path")
        self._conversations = Conversations(issued_by_server=self._session.response_path is not None)
        self._client: httpx.AsyncClient | None = None

    async def send(self, message: str, *, session: str | None = None) -> TargetReply:
        async with self._conversations.turn(session) as conversation:
            body = request_body(
                self._reference.body,
                {"message": message},
                "message",
                prompt=message,
                conversation=(conversation.id, list(conversation.history)),
            )
            headers = dict(self._headers)
            if self._session.header and conversation.id:
                headers[self._session.header] = conversation.id
            started = time.perf_counter()
            response = await self._post(headers, body)
            latency = (time.perf_counter() - started) * 1000
            text = reply_text(_reply(response, self._steps))
            if self._session_steps:
                issued = read_path(_json(response), self._session_steps)
                if not isinstance(issued, str | int) or isinstance(issued, bool) or str(issued) == "":
                    raise TargetCallError("the target's response has no session id at target.session.response_path")
                conversation.id = str(issued)
            calls = (
                tool_calls(read_path(_json(response), self._call_steps), "target.tool_calls_path")
                if self._call_steps
                else []
            )
            conversation.answered(message, text)
            return TargetReply(
                text=text,
                tool_calls=calls,
                tool_calls_observed=bool(self._call_steps),
                session=conversation.session,
                latency_ms=latency,
            )

    def end(self, session: str) -> None:
        """Forget a conversation; sending with its session again starts a new one."""
        self._conversations.end(session)

    async def close(self) -> None:
        self._conversations.clear()
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _post(self, headers: Mapping[str, str], body: Any) -> httpx.Response:
        timeout = self._reference.timeout_seconds
        if self._policy is not None:
            return await asyncio.to_thread(self._pinned_post, headers, body, timeout)
        if self._client is None:
            self._client = httpx.AsyncClient(follow_redirects=False, trust_env=False)
        try:
            return await asyncio.wait_for(
                self._client.post(self._url, headers=dict(headers), json=body, timeout=timeout),
                timeout + CLIENT_SLACK_SECONDS,
            )
        except (TimeoutError, httpx.ReadTimeout):
            raise TargetCallError(TIMED_OUT) from None
        except httpx.HTTPError as error:
            raise _unreachable(error) from None

    def _pinned_post(self, headers: Mapping[str, str], body: Any, timeout: float) -> httpx.Response:
        import requests

        assert self._policy is not None
        try:
            with pinned_session(self._policy) as client:
                found = client.post(self._url, headers=dict(headers), json=body, timeout=timeout)
        except AddressRefused:
            raise TargetCallError(
                "the target's address is not one this installation may call; check target.url"
            ) from None
        except requests.Timeout:
            raise TargetCallError(TIMED_OUT) from None
        except requests.RequestException as error:
            raise TargetCallError(
                f"the target could not be reached: {type(error).__name__}; check target.url"
            ) from None
        # requests has already decoded the body, so the headers that describe its encoding are dropped.
        kept = {
            name: value
            for name, value in found.headers.items()
            if name.lower() not in ("content-encoding", "content-length", "transfer-encoding")
        }
        return httpx.Response(found.status_code, headers=kept, content=found.content)


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        raise TargetCallError("the target's response is not JSON, so a path in it cannot be read") from None


__all__ = ["HttpTarget", "SessionSettings"]
