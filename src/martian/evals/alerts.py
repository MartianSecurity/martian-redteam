"""Delivering a threshold's alerts by signed webhook and by email (MartianSecurity/martian-notes#198).

An installation sends nothing unless it is configured to: :class:`AlertSettings` with no channel,
the default, sends no alert at all, so an on-premises install makes no outbound call for alerts
until its operator sets a webhook or an SMTP server (MartianSecurity/martian-notes#169). A threshold may
name only the channels its installation's worker sends (the routes refuse the others), and an alert
for a channel the installation stopped sending fails without a call.

**Webhooks.** Each alert is one ``POST`` of its JSON payload (:func:`~martian.evals.thresholds.alert_payload`)
to the threshold's URL, with these headers:

- ``X-Martian-Alert-Id``: the alert's id, the same on every attempt, for a receiver to drop a repeat.
- ``X-Martian-Timestamp``: the attempt's time, in whole seconds since 1970 (UTC).
- ``X-Martian-Signature``: ``v1=`` and the hex HMAC-SHA256, keyed with the threshold's secret, of
  the timestamp, a full stop, and the body's bytes (:func:`sign`).

A receiver recomputes the HMAC over the timestamp header and the raw body as received, compares it
in constant time, and refuses a timestamp more than five minutes from its clock, so a captured
request cannot be replayed later (:func:`verify`, or the standard-library example in
docs/evals-core.md). The secret is the threshold's, given when it is created; the record store
keeps it apart from the threshold and only this module reads it back, to sign.

A URL is ``https``, or ``http`` to a loopback host. Redirects are not followed and no proxy setting
of the environment is used. Before each attempt the host is resolved, and an address that is not
public (loopback, private, link-local, reserved) is refused unless the installation allows them
(``WebhookSettings.private_addresses``, for an on-premises receiver inside its network). A status
other than 2xx is a failed attempt.

**Email** goes through the SMTP server of :class:`EmailSettings`: its host and port, ``starttls``
(the default), ``ssl`` or ``none``, an optional login, and the sender address. The recipients are
the threshold's. The message is plain text and carries the alert's id as ``X-Martian-Alert-Id``.

**Retries.** A failed attempt is tried again after ``retry_seconds``, doubling each time up to
``max_retry_seconds``, until ``max_attempts``; then the alert is ``failed``. An alert is taken for
delivery before it is sent, so two deliveries never send it at once. A worker that stops after a
receiver accepted an alert and before it recorded the delivery sends it again when it is next due:
delivery is at least once, and the alert id is how a receiver tells.

**What is logged and stored.** A failed attempt is logged, and recorded as the alert's
``last_error``, as a status or an error class (``HTTP 503``, ``ConnectTimeout``, ``SMTP 550``): never
the URL, a header, the body, the secret, the SMTP password or a recipient.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import smtplib
import socket
import ssl
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

import httpx

from martian.evals._digits import ascii_digits
from martian.evals._pinned_address import AddressRefused, pinned_session, public_addresses_only
from martian.evals.records import Alert, RecordStore

if TYPE_CHECKING:
    from martian.evals.server.principal import Principal

log = logging.getLogger("martian.evals.alerts")

ALERT_ID_HEADER = "X-Martian-Alert-Id"
TIMESTAMP_HEADER = "X-Martian-Timestamp"
SIGNATURE_HEADER = "X-Martian-Signature"
SIGNATURE_VERSION = "v1"
TOLERANCE_SECONDS = 300
"""How far from a receiver's clock :func:`verify` accepts a timestamp."""
HOLD_SECONDS = 120.0
"""How long a delivery holds an alert it took, so no other delivery takes it meanwhile."""
PER_DELIVERY = 20
"""The most alerts one delivery tries."""
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
NO_WEBHOOKS = "This installation sends no webhook alerts"
NO_EMAIL = "This installation sends no email alerts"
Channel = Literal["webhook", "email"]


def _now() -> datetime:
    """The time a delivery reads which alerts are due, and from which it schedules a retry."""
    return datetime.now(UTC)


def sign(secret: str, timestamp: str, body: bytes) -> str:
    """The ``X-Martian-Signature`` value: ``v1=`` and the hex HMAC-SHA256 of ``timestamp``, ``.``
    and ``body``, keyed with ``secret``."""
    digest = hmac.new(secret.encode("utf-8"), timestamp.encode("ascii") + b"." + body, hashlib.sha256)
    return f"{SIGNATURE_VERSION}={digest.hexdigest()}"


def verify(
    secret: str,
    body: bytes,
    timestamp: str,
    signature: str,
    *,
    tolerance_seconds: float = TOLERANCE_SECONDS,
    now: float | None = None,
) -> bool:
    """Whether a webhook delivery is the installation's: ``signature`` is :func:`sign` of the
    ``timestamp`` header and the raw ``body`` with ``secret``, compared in constant time, and the
    timestamp is within ``tolerance_seconds`` of ``now`` (this machine's clock by default)."""
    if not ascii_digits(timestamp):
        return False
    if abs((time.time() if now is None else now) - int(timestamp)) > tolerance_seconds:
        return False
    return hmac.compare_digest(sign(secret, timestamp, body), signature)


@dataclass(frozen=True)
class WebhookSettings:
    """That the installation sends webhook alerts. ``private_addresses`` lets a webhook's host
    resolve to an address that is not public, such as a receiver inside an on-premises network;
    without it such a delivery is refused. ``timeout_seconds`` bounds each attempt."""

    private_addresses: bool = False
    timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds is positive")


@dataclass(frozen=True)
class EmailSettings:
    """The SMTP server email alerts go through: ``host`` and ``port``, ``security`` (``starttls``
    upgrades a plain connection, ``ssl`` connects with TLS, ``none`` is for a relay on this machine
    or its network), the login, if any, and ``sender``, the From address. ``password`` is never
    shown in a repr or a log line."""

    host: str
    sender: str
    port: int = 587
    security: Literal["starttls", "ssl", "none"] = "starttls"
    username: str | None = None
    password: str | None = field(default=None, repr=False)
    timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        if not self.host or "@" not in self.sender or not 0 < self.port < 65536 or self.timeout_seconds <= 0:
            raise ValueError("Give an SMTP host, a port, a sender address and a positive timeout")
        if (self.username is None) != (self.password is None):
            raise ValueError("Give an SMTP username and password together")


@dataclass(frozen=True)
class AlertSettings:
    """How an installation's worker delivers alerts: ``webhook`` and ``email`` are the channels it
    sends, each off while None, which is the default. ``retry_seconds`` is the wait after the first
    failed attempt, doubled after each one up to ``max_retry_seconds``, and ``max_attempts`` how
    many attempts an alert has."""

    webhook: WebhookSettings | None = None
    email: EmailSettings | None = None
    retry_seconds: float = 30.0
    max_retry_seconds: float = 3600.0
    max_attempts: int = 8

    def __post_init__(self) -> None:
        if self.retry_seconds <= 0 or self.max_retry_seconds < self.retry_seconds or self.max_attempts < 1:
            raise ValueError("retry_seconds is positive, max_retry_seconds at least that, max_attempts at least 1")

    @property
    def channels(self) -> frozenset[Channel]:
        """The channels this installation sends."""
        found: set[Channel] = set()
        if self.webhook is not None:
            found.add("webhook")
        if self.email is not None:
            found.add("email")
        return frozenset(found)


def webhook_url_problem(url: str) -> str | None:
    """Why ``url`` cannot be a webhook's, or None: it is ``https``, or ``http`` to a loopback host,
    with a host and no user name or password in it."""
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - a port that is not a number raises here
    except ValueError:
        return "Give the webhook as an absolute https URL"
    if parts.scheme not in ("https", "http") or not parts.hostname:
        return "Give the webhook as an absolute https URL"
    if parts.scheme == "http" and parts.hostname not in LOOPBACK_HOSTS:
        return "A webhook is https, or http only to this machine (localhost)"
    if parts.username is not None or parts.password is not None:
        return "A webhook URL holds no user name or password; the signature authenticates its alerts"
    return None


class Undeliverable(Exception):
    """An alert that no attempt can deliver: it fails at once, with this message as its error."""


class Refused(Exception):
    """A receiver answered with a status other than 2xx."""

    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status


def failure(error: BaseException) -> str:
    """A failed attempt as it is logged and stored: a status or an error class, nothing it carried."""
    if isinstance(error, Refused):
        return f"HTTP {error.status}"
    if isinstance(error, smtplib.SMTPResponseException):
        return f"SMTP {error.smtp_code}"
    if isinstance(error, socket.gaierror):
        return "the host name did not resolve"
    return type(error).__name__


def _subject(payload: dict[str, Any]) -> str:
    name = payload["threshold"]["name"]
    return f"[Martian] {name}: {'breached' if payload['type'] == 'threshold.breached' else 'recovered'}"


def describe(payload: dict[str, Any]) -> str:
    """An alert's payload as an email's text."""
    threshold, seen = payload["threshold"], payload["observation"]
    pass_rate = threshold["measure"] == "pass_rate"
    measure = "pass rate" if pass_rate else "mean score"
    bound = "at least" if threshold["direction"] == "at_least" else "at most"

    def shown(number: float | None) -> str:
        if number is None:
            return "none"
        return f"{number * 100:.1f}%" if pass_rate else f"{number:.4g}"

    interval = seen.get("interval") or [None, None]
    source = threshold["source"]
    what = f"{source['scorer']} scores of monitor {source['monitor_id']}" if source["kind"] == "monitor" else "results"
    breached = payload["type"] == "threshold.breached"
    lines = [
        f'Threshold "{threshold["name"]}" {"breached" if breached else "recovered"} at {payload["created_at"]}.',
        "",
        f"The {measure} of {seen['count']} {what} in the last {threshold['window_seconds']} seconds is "
        f"{shown(seen.get('estimate'))}, with a 95% interval of {shown(interval[0])} to {shown(interval[1])}.",
        f"It is healthy while the {measure} is {bound} {shown(threshold['value'])}. A breach is raised when the "
        "whole interval is on the wrong side of that value, and a recovery when it is wholly back.",
        "",
        f"Alert {payload['id']}, change {payload['sequence']} of threshold {threshold['id']}.",
    ]
    return "\n".join(lines) + "\n"


class AlertSender:
    """Delivers the due alerts of a set of thresholds with ``settings``, which send nothing by
    default."""

    def __init__(self, settings: AlertSettings | None = None) -> None:
        self.settings = settings or AlertSettings()

    @property
    def channels(self) -> frozenset[Channel]:
        return self.settings.channels

    async def deliver_due(
        self, records: RecordStore, principal: Principal, project_id: str, threshold_ids: Sequence[str]
    ) -> list[Alert]:
        """Try each alert of these thresholds that is due, one at a time, at most
        :data:`PER_DELIVERY`, and record each attempt. The alerts as the attempts left them."""
        done: list[Alert] = []
        for _ in range(PER_DELIVERY):
            now = _now()
            taken = await records.take_due_alerts(
                principal, project_id, threshold_ids, now=now, hold_until=now + timedelta(seconds=HOLD_SECONDS), limit=1
            )
            if not taken:
                break
            finished = await self._attempt(records, principal, project_id, taken[0])
            await records.finish_alert(principal, project_id, finished)
            done.append(finished)
        return done

    def _retry_after(self, attempts: int) -> float:
        return float(min(self.settings.retry_seconds * 2 ** (attempts - 1), self.settings.max_retry_seconds))

    async def _attempt(self, records: RecordStore, principal: Principal, project_id: str, alert: Alert) -> Alert:
        attempts = alert.attempts + 1
        try:
            threshold = await records.get_threshold(principal, project_id, alert.threshold_id)
            if threshold is None:
                raise Undeliverable("The threshold is gone")
            if alert.channel == "webhook":
                if self.settings.webhook is None:
                    raise Undeliverable(NO_WEBHOOKS)
                if threshold.webhook is None:
                    raise Undeliverable("The threshold has no webhook")
                secret = await records.threshold_secret(principal, project_id, alert.threshold_id)
                if secret is None:
                    raise Undeliverable("The threshold has no webhook secret")
                await self._post(self.settings.webhook, threshold.webhook.url, alert, secret)
            else:
                if self.settings.email is None:
                    raise Undeliverable(NO_EMAIL)
                if threshold.email is None:
                    raise Undeliverable("The threshold has no email recipients")
                await asyncio.to_thread(self._mail, self.settings.email, threshold.email.to, alert)
        except Undeliverable as error:
            log.warning(
                "Alert %s of threshold %s (%s) cannot be delivered: %s",
                alert.id,
                alert.threshold_id,
                alert.channel,
                error,
            )
            return alert.model_copy(
                update={"state": "failed", "attempts": attempts, "last_error": str(error), "next_attempt_at": None}
            )
        except Exception as error:  # noqa: BLE001 - every failed attempt is retried, and named without its content
            reason = failure(error)
            last = attempts >= self.settings.max_attempts
            log.warning(
                "Alert %s of threshold %s (%s): attempt %d of %d failed (%s)%s",
                alert.id,
                alert.threshold_id,
                alert.channel,
                attempts,
                self.settings.max_attempts,
                reason,
                "; giving up" if last else "",
            )
            retry = _now() + timedelta(seconds=self._retry_after(attempts))
            return alert.model_copy(
                update={
                    "state": "failed" if last else "pending",
                    "attempts": attempts,
                    "last_error": reason,
                    "next_attempt_at": None if last else retry,
                }
            )
        return alert.model_copy(
            update={
                "state": "delivered",
                "attempts": attempts,
                "last_error": "",
                "next_attempt_at": None,
                "delivered_at": _now(),
            }
        )

    async def _post(self, settings: WebhookSettings, url: str, alert: Alert, secret: str) -> None:
        if (problem := webhook_url_problem(url)) is not None:
            raise Undeliverable(problem)
        body = json.dumps(alert.payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        timestamp = str(int(time.time()))
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "martian-evals-alerts",
            ALERT_ID_HEADER: alert.id,
            TIMESTAMP_HEADER: timestamp,
            SIGNATURE_HEADER: sign(secret, timestamp, body),
        }
        if settings.private_addresses:
            async with (
                httpx.AsyncClient(timeout=settings.timeout_seconds, follow_redirects=False, trust_env=False) as client,
                client.stream("POST", url, content=body, headers=headers) as response,
            ):
                status = response.status_code
        else:
            status = await asyncio.to_thread(self._post_public, settings, url, body, headers)
        if not 200 <= status < 300:
            raise Refused(status)

    @staticmethod
    def _post_public(settings: WebhookSettings, url: str, body: bytes, headers: dict[str, str]) -> int:
        try:
            with (
                pinned_session(public_addresses_only) as client,
                client.post(
                    url, data=body, headers=headers, timeout=settings.timeout_seconds, verify=True, stream=True
                ) as response,
            ):
                return response.status_code
        except AddressRefused:
            raise Undeliverable(
                "The webhook's host resolves to an address that is not public, which this "
                "installation's webhook settings do not allow (private_addresses)"
            ) from None

    @staticmethod
    def _mail(settings: EmailSettings, to: Sequence[str], alert: Alert) -> None:
        message = EmailMessage()
        message["From"] = settings.sender
        message["To"] = ", ".join(to)
        message["Subject"] = _subject(alert.payload)
        message[ALERT_ID_HEADER] = alert.id
        message.set_content(describe(alert.payload))
        context = ssl.create_default_context()
        smtp: smtplib.SMTP
        if settings.security == "ssl":
            smtp = smtplib.SMTP_SSL(settings.host, settings.port, timeout=settings.timeout_seconds, context=context)
        else:
            smtp = smtplib.SMTP(settings.host, settings.port, timeout=settings.timeout_seconds)
        with smtp:
            if settings.security == "starttls":
                smtp.starttls(context=context)
            if settings.username is not None and settings.password is not None:
                smtp.login(settings.username, settings.password)
            smtp.send_message(message)


__all__ = [
    "ALERT_ID_HEADER",
    "NO_EMAIL",
    "NO_WEBHOOKS",
    "SIGNATURE_HEADER",
    "TIMESTAMP_HEADER",
    "TOLERANCE_SECONDS",
    "AlertSender",
    "AlertSettings",
    "EmailSettings",
    "WebhookSettings",
    "describe",
    "sign",
    "verify",
    "webhook_url_problem",
]
