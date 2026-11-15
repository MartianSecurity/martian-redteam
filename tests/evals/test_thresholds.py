"""Thresholds on monitor scores and the alerts they send (MorningHQ/martian-notes#198).

A threshold is judged on the 95% interval of the scores in its window, not on the point estimate,
so noise around its value raises nothing, and its state machine sends exactly one alert when it
breaches and one when it recovers, however often it is evaluated and across a restart. Alerts go
by signed webhook, which a receiver verifies with the method docs/evals-core.md documents, and by
email; failed deliveries are retried with backoff; the secret is never returned, logged or stored
with an alert; and nothing is sent unless the installation is configured to send it. A loopback
webhook receiver and a fake SMTP server stand in for the receivers, and a fake judge for the
model; nothing reaches a network.
"""

from __future__ import annotations

import email
import json
import logging
import random
import re
import socketserver
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from martian.evals import alerts as alerts_module
from martian.evals.alerts import (
    ALERT_ID_HEADER,
    NO_EMAIL,
    NO_WEBHOOKS,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    AlertSender,
    AlertSettings,
    EmailSettings,
    WebhookSettings,
    verify,
)
from martian.evals.judge_settings import MemoryJudgeSettings
from martian.evals.model import Project, Score, Span, Trace
from martian.evals.records import (
    Alert,
    EmailChannel,
    Monitor,
    MonitoredTrace,
    MonitorScores,
    Threshold,
    WebhookChannel,
)
from martian.evals.records.queue import SqlWorkQueue
from martian.evals.records.sql import SqlRecordStore
from martian.evals.server import COMMUNITY, DEFAULT_PROJECT, ENTERPRISE, SINGLE_USER, create_app
from martian.evals.server.principal import organisation_principal
from martian.evals.store import MemoryStore
from martian.evals.thresholds import MonitorSource, Window, evaluate_thresholds, next_state, observe, raised_event
from martian.evals.worker import InProcessWorker, JudgeRequest, JudgeResult, QueueWorker, Verdict
from martian.evals.worker.scorers import builtin_scorer

DOCS = Path(__file__).resolve().parents[2] / "docs" / "evals-core.md"
SECRET = "whsec-canary-" + "7f3a9c" * 4
"""A webhook secret no route, log line or alert may hold."""
SAFETY = builtin_scorer("safety")
MEMBER = organisation_principal("member-1", "org-1", frozenset({"member"}))
VIEWER = organisation_principal("viewer-1", "org-1", frozenset({"viewer"}))
SETTLE, EVERY, POLL = 0.3, 0.3, 0.05
SECONDS = 30
WEBHOOKS = WebhookSettings(private_addresses=True)  # the loopback receiver is not a public address
T = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=6)
"""The start of the tests' timeline, in the past, so every alert is due."""


# --- the receivers -------------------------------------------------------------------------------


class Receiver:
    """A webhook receiver on 127.0.0.1: it keeps every request and answers each with the next of
    ``statuses``, then 204."""

    def __init__(self, statuses: tuple[int, ...] = ()) -> None:
        self.requests: list[dict[str, Any]] = []
        self.statuses = list(statuses)
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - http.server's name
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                receiver.requests.append({"path": self.path, "headers": dict(self.headers.items()), "body": body})
                self.send_response(receiver.statuses.pop(0) if receiver.statuses else 204)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *_args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/hooks/martian"

    def payloads(self) -> list[dict[str, Any]]:
        return [json.loads(r["body"]) for r in self.requests]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class FakeSmtp:
    """A minimal SMTP server on 127.0.0.1 that keeps every message it is sent, and counts the
    connections it was opened with."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, list[str], Message]] = []
        self.connections = 0
        smtp = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                smtp.connections += 1
                sender, recipients = "", []

                def say(line: str) -> None:
                    self.wfile.write(line.encode() + b"\r\n")

                say("220 fake.local ESMTP")
                while line := self.rfile.readline():
                    command = line.decode().strip()
                    verb = command[:4].upper()
                    if verb == "EHLO":
                        say("250-fake.local")
                        say("250 8BITMIME")
                    elif verb == "HELO":
                        say("250 fake.local")
                    elif verb == "MAIL":
                        sender = command.split(":", 1)[1].strip().strip("<>")
                        say("250 OK")
                    elif verb == "RCPT":
                        recipients.append(command.split(":", 1)[1].strip().strip("<>"))
                        say("250 OK")
                    elif verb == "DATA":
                        say("354 End data with <CR><LF>.<CR><LF>")
                        lines = []
                        while (data := self.rfile.readline()) not in (b".\r\n", b""):
                            lines.append(data[1:] if data.startswith(b"..") else data)
                        smtp.messages.append((sender, recipients, email.message_from_bytes(b"".join(lines))))
                        say("250 OK")
                    elif verb == "QUIT":
                        say("221 Bye")
                        return
                    else:
                        say("250 OK")

        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def receiver() -> Iterator[Receiver]:
    found = Receiver()
    yield found
    found.close()


@pytest.fixture
def smtp() -> Iterator[FakeSmtp]:
    found = FakeSmtp()
    yield found
    found.close()


@pytest.fixture
def records(tmp_path: Path) -> Iterator[SqlRecordStore]:
    found = SqlRecordStore(f"sqlite:///{tmp_path / 'martian.db'}")
    yield found
    found.close()


def documented_verify() -> Callable[[str, bytes, str, str], bool]:
    """The receiver's verification as docs/evals-core.md writes it, run as it is written."""
    blocks = re.findall(r"```python\n(.*?)```", DOCS.read_text(encoding="utf-8"), flags=re.DOTALL)
    [code] = [block for block in blocks if "def verify_martian_webhook(" in block]
    namespace: dict[str, Any] = {}
    exec(compile(code, str(DOCS), "exec"), namespace)  # noqa: S102 - the documented example itself
    found: Callable[[str, bytes, str, str], bool] = namespace["verify_martian_webhook"]
    return found


# --- records and evaluations, called directly -----------------------------------------------------


async def make_monitor(records: SqlRecordStore) -> None:
    monitor = Monitor(id="m1", project_id=DEFAULT_PROJECT, name="Live safety", scorers=[SAFETY], sampling_rate=1)
    await records.create_monitor(SINGLE_USER, DEFAULT_PROJECT, monitor)


async def make_threshold(
    records: SqlRecordStore,
    *,
    url: str | None = None,
    to: list[str] | None = None,
    threshold_id: str = "t1",
    **fields: Any,
) -> Threshold:
    values: dict[str, Any] = {
        "id": threshold_id,
        "project_id": DEFAULT_PROJECT,
        "name": "Live safety pass rate",
        "source": MonitorScores(monitor_id="m1", scorer="safety"),
        "measure": "pass_rate",
        "direction": "at_least",
        "value": 0.9,
        "window_seconds": 3600,
        "min_count": 20,
        "webhook": WebhookChannel(url=url) if url else None,
        "email": EmailChannel(to=to) if to else None,
    }
    made = Threshold(**{**values, **fields})
    return await records.create_threshold(SINGLE_USER, DEFAULT_PROJECT, made, webhook_secret=SECRET if url else None)


async def scored(records: SqlRecordStore, outcomes: list[bool], at: datetime, *, first: int = 0) -> int:
    """Record that the monitor scored one trace per outcome, a second apart from ``at``."""
    for offset, outcome in enumerate(outcomes):
        trace_id = f"tr-{first + offset}"
        score = Score(trace_id=trace_id, scorer="safety", source="llm_judge", value=outcome, passed=outcome)
        taken = at + timedelta(seconds=offset)
        await records.finish_monitored_trace(
            SINGLE_USER,
            DEFAULT_PROJECT,
            MonitoredTrace(
                monitor_id="m1", trace_id=trace_id, state="scored", pass_id="p", scores=[score], created_at=taken
            ),
        )
    return first + len(outcomes)


async def evaluate(records: SqlRecordStore, now: datetime, sender: AlertSender | None = None) -> list[Alert]:
    """One evaluation of the monitor's thresholds at ``now``, then a delivery, as a pass ends."""
    found = await records.list_thresholds(SINGLE_USER, DEFAULT_PROJECT, source_kind="monitor", source_id="m1")
    raised = await evaluate_thresholds(records, SINGLE_USER, DEFAULT_PROJECT, found, MonitorSource(records), now=now)
    if sender is not None:
        await sender.deliver_due(records, SINGLE_USER, DEFAULT_PROJECT, [t.id for t in found])
    return raised


async def stored_alerts(records: SqlRecordStore, threshold_id: str = "t1") -> list[Alert]:
    return list((await records.list_alerts(SINGLE_USER, DEFAULT_PROJECT, threshold_id, limit=200)).items)


def window(outcomes: list[bool | float]) -> Window:
    return Window(start=T, end=T + timedelta(hours=1), values=outcomes)


def rate_threshold(**fields: Any) -> Threshold:
    values: dict[str, Any] = {
        "id": "t",
        "project_id": DEFAULT_PROJECT,
        "name": "Rate",
        "source": MonitorScores(monitor_id="m1", scorer="safety"),
        "measure": "pass_rate",
        "direction": "at_least",
        "value": 0.9,
        "window_seconds": 3600,
        "min_count": 20,
    }
    return Threshold(**{**values, **fields})


# --- the interval and the state machine ---------------------------------------------------------


def test_a_verdict_is_on_the_95_percent_interval_not_the_point_estimate() -> None:
    threshold = rate_threshold()
    # 85 of 100 is below 90%, but its interval (77-91%) holds 90%: not yet a breach.
    undecided = observe(threshold, window([True] * 85 + [False] * 15))
    assert undecided.estimate == 0.85 and undecided.verdict == "undecided"
    assert undecided.interval is not None and undecided.interval[0] < 0.9 < undecided.interval[1]
    # 80 of 100: the whole interval (71-87%) is below it.
    breach = observe(threshold, window([True] * 80 + [False] * 20))
    assert breach.verdict == "breach" and breach.interval is not None and breach.interval[1] < 0.9
    # 97 of 100: the whole interval (92-99%) is above it.
    healthy = observe(threshold, window([True] * 97 + [False] * 3))
    assert healthy.verdict == "healthy" and healthy.interval is not None and healthy.interval[0] >= 0.9
    # Nineteen failures are fewer than min_count: no verdict yet.
    assert observe(threshold, window([False] * 19)).verdict == "insufficient"
    # Scores of other kinds are not counted: a pass rate reads booleans only.
    assert observe(threshold, window([True] * 97 + [False] * 3 + [0.5, 3])).count == 100

    # A mean reads numbers, at most a value here, with the t interval.
    mean = rate_threshold(measure="mean", direction="at_most", value=500, min_count=5)
    assert observe(mean, window([600, 610, 590, 605, 595])).verdict == "breach"
    assert observe(mean, window([100, 120, 90, 110, 95])).verdict == "healthy"
    assert observe(mean, window([400, 620, 380, 610, 500])).verdict == "undecided"
    assert observe(mean, window([True] * 10)).verdict == "insufficient"


def test_noise_around_the_value_raises_nothing_and_a_real_drop_one_alert_each_way() -> None:
    """At a real rate of 93% against a threshold of 90%, windows of 200 scores fall below 90% by
    chance about one time in twenty: the point estimate would alert again and again, the interval
    never does. A real drop to 80% breaches once, and a recovery to 97% recovers once."""
    threshold = rate_threshold(min_count=50)
    sample = random.Random(198)
    phases = [(0.93, 300), (0.80, 50), (0.97, 50)]
    state, events, point_breaches, below = "unknown", [], 0, False
    for rate, windows in phases:
        for _ in range(windows):
            outcomes = [sample.random() < rate for _ in range(200)]
            seen = observe(threshold, window(list(outcomes)))
            before, state = state, next_state(state, seen.verdict)  # type: ignore[arg-type]
            if (event := raised_event(before, state)) is not None:  # type: ignore[arg-type]
                events.append(event)
            assert seen.estimate is not None
            if seen.estimate < 0.9 and not below and rate == 0.93:
                point_breaches += 1
            below = seen.estimate < 0.9
    assert point_breaches >= 5, point_breaches  # what alerting on the point estimate would have sent
    assert events == ["breached", "recovered"]
    assert next_state("ok", "undecided") == "ok" and next_state("breached", "insufficient") == "breached"
    assert raised_event("unknown", "ok") is None and raised_event("unknown", "breached") == "breached"


def test_a_pass_rate_threshold_of_0_or_1_and_a_mean_of_one_score_are_refused() -> None:
    for fields in ({"value": 1.0}, {"value": 0.0}, {"measure": "mean", "value": 3, "min_count": 1}):
        with pytest.raises(ValueError, match="between 0 and 1|at least 2"):
            rate_threshold(**fields)


# --- one alert per change, through the record store -------------------------------------------------


async def test_a_breach_sends_one_alert_and_a_recovery_one_however_often_it_is_evaluated(
    records: SqlRecordStore, receiver: Receiver
) -> None:
    sender = AlertSender(AlertSettings(webhook=WEBHOOKS))
    await make_monitor(records)
    await make_threshold(records, url=receiver.url)
    # 40 of 40 is healthy (the interval's lower bound is 91%), and from unknown that sends nothing.
    count = await scored(records, [True] * 40, T)
    for minutes in (10, 11, 12):
        assert await evaluate(records, T + timedelta(minutes=minutes), sender) == []
    status = await records.threshold_status(SINGLE_USER, DEFAULT_PROJECT, "t1")
    assert status is not None and (status.state, status.sequence) == ("ok", 1)

    count = await scored(records, [False] * 40, T + timedelta(minutes=20), first=count)
    raised = []
    for minutes in range(30, 36):  # 40 of 80: a breach, evaluated six times
        raised += await evaluate(records, T + timedelta(minutes=minutes), sender)
    assert [(a.event, a.sequence, a.channel) for a in raised] == [("breached", 2, "webhook")]
    assert [p["type"] for p in receiver.payloads()] == ["threshold.breached"]

    # Passing scores that leave the interval holding 90% (340 of 380, 86-92%) change nothing.
    count = await scored(records, [True] * 300, T + timedelta(minutes=40), first=count)
    assert await evaluate(records, T + timedelta(minutes=46), sender) == []
    status = await records.threshold_status(SINGLE_USER, DEFAULT_PROJECT, "t1")
    assert status is not None and status.state == "breached" and status.last is not None
    assert status.last.verdict == "undecided"

    # Once the failures leave the window, the passing scores clear it: one recovery.
    count = await scored(records, [True] * 100, T + timedelta(minutes=90), first=count)
    raised = []
    for minutes in (95, 96, 97):
        raised += await evaluate(records, T + timedelta(minutes=minutes, seconds=50), sender)
    assert [(a.event, a.sequence) for a in raised] == [("recovered", 3)]
    payloads = receiver.payloads()
    assert [(p["type"], p["sequence"]) for p in payloads] == [("threshold.breached", 2), ("threshold.recovered", 3)]
    breach = payloads[0]
    assert breach["threshold"]["source"] == {"kind": "monitor", "monitor_id": "m1", "scorer": "safety"}
    assert (breach["observation"]["count"], breach["observation"]["confidence"]) == (80, 0.95)
    assert breach["observation"]["estimate"] == pytest.approx(0.5)
    assert [a.state for a in await stored_alerts(records)] == ["delivered", "delivered"]


async def test_a_worker_restart_sends_nothing_again(tmp_path: Path, receiver: Receiver) -> None:
    url = f"sqlite:///{tmp_path / 'restart.db'}"
    first = SqlRecordStore(url)
    await make_monitor(first)
    await make_threshold(first, url=receiver.url)
    await scored(first, [False] * 30, T)
    # The breach is recorded, and the worker stops before delivering it.
    [raised] = await evaluate(first, T + timedelta(minutes=5))
    first.close()

    # A new worker on the same database delivers it once, and evaluating again raises nothing.
    for _ in range(3):
        again = SqlRecordStore(url)
        assert await evaluate(again, T + timedelta(minutes=6), AlertSender(AlertSettings(webhook=WEBHOOKS))) == []
        again.close()
    assert [p["id"] for p in receiver.payloads()] == [raised.id]
    assert len(receiver.requests) == 1


async def test_a_second_source_feeds_the_same_state_machine(records: SqlRecordStore) -> None:
    """The source is an interface: scheduled runs' gate results (MorningHQ/martian-notes#197) will be
    one, as booleans, judged and alerted on as a monitor's scores are."""

    class Gates:
        kind = "monitor"  # a gate source reads ``gate`` thresholds; this one stands in for it

        def __init__(self) -> None:
            self.outcomes: list[bool] = []

        async def windows(self, principal: Any, project_id: str, thresholds: Any, *, now: datetime) -> Any:
            return {t.id: Window(start=now - timedelta(days=7), end=now, values=self.outcomes) for t in thresholds}

    await make_monitor(records)
    threshold = await make_threshold(records, url="https://alerts.example/hook", min_count=3, value=0.5)
    gates = Gates()
    events = []
    for outcomes in ([True, True, True, True], [False] * 8, [True] * 30):
        gates.outcomes = outcomes
        for _ in range(2):
            raised = await evaluate_thresholds(records, SINGLE_USER, DEFAULT_PROJECT, [threshold], gates)
            events += [a.event for a in raised]
    assert events == ["breached", "recovered"]


# --- delivery ----------------------------------------------------------------------------------------


async def test_a_delivered_signature_verifies_with_the_documented_method(
    records: SqlRecordStore, receiver: Receiver
) -> None:
    await make_monitor(records)
    await make_threshold(records, url=receiver.url)
    await scored(records, [False] * 25, T)
    await evaluate(records, T + timedelta(minutes=1), AlertSender(AlertSettings(webhook=WEBHOOKS)))
    [delivered] = receiver.requests
    headers, body = delivered["headers"], delivered["body"]
    timestamp, signature = headers[TIMESTAMP_HEADER], headers[SIGNATURE_HEADER]
    assert headers["Content-Type"] == "application/json" and signature.startswith("v1=")
    assert headers[ALERT_ID_HEADER] == json.loads(body)["id"]
    assert abs(int(timestamp) - time.time()) < 60

    documented = documented_verify()
    assert documented(SECRET, body, timestamp, signature)
    assert verify(SECRET, body, timestamp, signature)
    # A changed body, another secret, a changed timestamp or an old one does not verify.
    assert not documented(SECRET, body.replace(b"breached", b"recovered"), timestamp, signature)
    assert not documented("another-secret-of-length", body, timestamp, signature)
    assert not documented(SECRET, body, str(int(timestamp) + 1), signature)
    assert not verify(SECRET, body, timestamp, signature, now=int(timestamp) + 301)


async def test_failed_deliveries_are_retried_with_backoff_then_given_up(
    records: SqlRecordStore, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    flaky, down = Receiver(statuses=(503, 500)), Receiver(statuses=(500,) * 10)
    try:
        settings = AlertSettings(webhook=WEBHOOKS, retry_seconds=0.3, max_retry_seconds=0.6, max_attempts=3)
        sender = AlertSender(settings)
        await make_monitor(records)
        await make_threshold(records, url=flaky.url)
        await make_threshold(records, url=down.url, threshold_id="t2", name="Down")
        await scored(records, [False] * 25, T)
        await evaluate(records, T + timedelta(minutes=1), sender)
        waits = []
        for _attempt in range(2):
            [alert] = [a for a in await stored_alerts(records) if a.state == "pending"]
            assert alert.next_attempt_at is not None
            waits.append((alert.next_attempt_at - datetime.now(UTC)).total_seconds())
            await evaluate(records, T + timedelta(minutes=1), sender)  # not due yet: nothing is sent
            assert (len(flaky.requests), len(down.requests)) == (_attempt + 1, _attempt + 1)
            time.sleep(max(0.0, waits[-1]) + 0.05)
            await evaluate(records, T + timedelta(minutes=1), sender)
        # 0.3 seconds after the first failure, then 0.6: doubled, and no longer than the cap.
        assert 0.1 < waits[0] <= 0.3 and 0.4 < waits[1] <= 0.6, waits
        [flaky_alert] = await stored_alerts(records, "t1")
        assert (flaky_alert.state, flaky_alert.attempts, flaky_alert.last_error) == ("delivered", 3, "")
        [down_alert] = await stored_alerts(records, "t2")
        assert (down_alert.state, down_alert.attempts, down_alert.last_error) == ("failed", 3, "HTTP 500")
        assert down_alert.next_attempt_at is None
        # Every attempt carried the same alert id, signed afresh.
        assert len({r["headers"][ALERT_ID_HEADER] for r in flaky.requests}) == 1
        verifier = documented_verify()
        assert all(
            verifier(SECRET, r["body"], r["headers"][TIMESTAMP_HEADER], r["headers"][SIGNATURE_HEADER])
            for r in flaky.requests + down.requests
        )
        # Failures are logged by status, never with the address or the secret.
        failures = [r.getMessage() for r in caplog.records if r.name == "martian.evals.alerts"]
        assert any("HTTP 503" in line for line in failures) and any("giving up" in line for line in failures)
        assert not [line for line in failures if "127.0.0.1" in line or "/hooks/" in line]
        assert SECRET not in caplog.text
    finally:
        flaky.close()
        down.close()


async def test_email_alerts_go_through_the_configured_smtp_server(records: SqlRecordStore, smtp: FakeSmtp) -> None:
    settings = EmailSettings(host="127.0.0.1", port=smtp.port, security="none", sender="martian@example.com")
    sender = AlertSender(AlertSettings(email=settings))
    await make_monitor(records)
    await make_threshold(records, to=["oncall@example.com", "quality@example.com"])
    await scored(records, [False] * 25, T)
    await evaluate(records, T + timedelta(minutes=1), sender)
    await evaluate(records, T + timedelta(minutes=2), sender)
    [(mail_from, to, message)] = smtp.messages
    assert (mail_from, to) == ("martian@example.com", ["oncall@example.com", "quality@example.com"])
    assert message["Subject"] == "[Martian] Live safety pass rate: breached"
    assert message["From"] == "martian@example.com"
    [alert] = await stored_alerts(records)
    assert (alert.channel, alert.state, message[ALERT_ID_HEADER]) == ("email", "delivered", alert.id)
    text = message.get_payload(decode=True).decode()
    assert "The pass rate of 25 safety scores of monitor m1 in the last 3600 seconds is 0.0%" in text
    assert "at least 90.0%" in text
    assert "password" not in repr(EmailSettings(host="h", sender="a@b.c", username="u", password="hunter2"))


async def test_nothing_is_sent_unless_the_installation_is_configured(
    records: SqlRecordStore, receiver: Receiver, smtp: FakeSmtp
) -> None:
    await make_monitor(records)
    await make_threshold(records, url=receiver.url, to=["oncall@example.com"])
    await scored(records, [False] * 25, T)
    # The default settings send nothing: each alert fails without a connection.
    await evaluate(records, T + timedelta(minutes=1), AlertSender())
    found = {a.channel: (a.state, a.last_error) for a in await stored_alerts(records)}
    assert found == {"webhook": ("failed", NO_WEBHOOKS), "email": ("failed", NO_EMAIL)}
    assert receiver.requests == [] and smtp.connections == 0

    # Webhooks on, but not to private addresses: the loopback receiver is refused before any call.
    await make_threshold(records, url=receiver.url, threshold_id="t2", name="Second")
    await evaluate(records, T + timedelta(minutes=2), AlertSender(AlertSettings(webhook=WebhookSettings())))
    [second] = await stored_alerts(records, "t2")
    assert second.state == "failed" and "not public" in second.last_error
    assert receiver.requests == []


# --- the routes and the queue -------------------------------------------------------------------------


class FakeJudge:
    """Calls every reply ``verdict``, with 10 tokens."""

    def __init__(self, verdict: bool) -> None:
        self.verdict = verdict

    async def __call__(self, request: JudgeRequest) -> JudgeResult:
        return JudgeResult(
            model=request.model or "fake-judge@1",
            verdicts={s.name: Verdict(self.verdict, "Judged.") for s in request.scorers},
            tokens=10,
        )


class Core:
    """The core with a queue whose worker runs in the app's process and sends ``alerts``."""

    def __init__(
        self, records: SqlRecordStore, queue: SqlWorkQueue, alerts: AlertSettings | None, entitlements: Any = None
    ) -> None:
        self.store, self.records = MemoryStore(), records
        self.judge = FakeJudge(verdict=False)
        self.principal = {"current": SINGLE_USER}
        runner = InProcessWorker(self.store, records, judge=self.judge, monitor_settle_seconds=SETTLE, alerts=alerts)
        worker = QueueWorker(queue, runner, records, in_process=True, poll_seconds=POLL, monitor_seconds=EVERY)
        app = create_app(
            store=self.store,
            records=records,
            principal=lambda: self.principal["current"],
            worker=worker,
            entitlements=entitlements or (lambda _p: ENTERPRISE),
            judge_settings=MemoryJudgeSettings(),
        )
        self.client = TestClient(app)

    def call(self, method: str, path: str, status: int, **kwargs: Any) -> Any:
        response = self.client.request(method, path, **kwargs)
        assert response.status_code == status, response.text
        return response.json() if response.content else None

    def text(self, method: str, path: str, **kwargs: Any) -> str:
        return self.client.request(method, path, **kwargs).text

    def record(self, number: int) -> Trace:
        at = datetime.now(UTC)
        start = at.timestamp() * 1000
        span = Span(id=f"{number:016x}", name="chat", kind="CHAT_MODEL", status="ok", start_ms=start, end_ms=start + 5)
        found = Trace(
            id="tr-" + uuid.uuid5(uuid.NAMESPACE_URL, f"threshold-trace-{number}").hex,
            project_id=DEFAULT_PROJECT,
            name="support turn",
            state="ok",
            request_time=at,
            latency_ms=5,
            request=f"Question {number}",
            response=f"Answer {number}",
            spans=[span],
        )
        assert self.client.portal is not None
        recorded: Trace = self.client.portal.call(self.store.record_trace, SINGLE_USER, found)
        return recorded


def until(check: Callable[[], bool], seconds: float = SECONDS) -> None:
    started = time.monotonic()
    while not check():
        if time.monotonic() - started > seconds:
            raise TimeoutError("the condition never held")
        time.sleep(0.02)


@pytest.fixture
def queue(records: SqlRecordStore, tmp_path: Path) -> Iterator[SqlWorkQueue]:
    found = SqlWorkQueue(f"sqlite:///{tmp_path / 'martian.db'}")
    yield found
    found.close()


MONITOR = {
    "name": "Live safety",
    "scorers": ["safety"],
    "sampling_rate": 1,
    "acknowledge_model_cost": True,
    "max_judge_tokens_per_day": 1_000_000,
}


def create(core: Core, path: str, status: int = 201, **body: Any) -> Any:
    return core.call("POST", path, status, json={"request_id": str(uuid.uuid4()), **body})


def a_threshold(monitor_id: str, **fields: Any) -> dict[str, Any]:
    return {
        "name": "Live safety pass rate",
        "source": {"monitor_id": monitor_id, "scorer": "safety"},
        "measure": "pass_rate",
        "value": 0.5,
        "min_count": 3,
        **fields,
    }


def test_a_breach_through_the_queue_sends_one_signed_webhook(
    records: SqlRecordStore, queue: SqlWorkQueue, receiver: Receiver, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    core = Core(records, queue, AlertSettings(webhook=WEBHOOKS))
    with core.client:
        monitor = create(core, "/v1/evals/monitors", **MONITOR)
        body = a_threshold(monitor["id"], webhook={"url": receiver.url, "secret": SECRET})
        made = create(core, "/v1/evals/thresholds", **body)
        assert made["webhook"] == {"url": receiver.url, "signed": True}
        assert made["status"]["state"] == "unknown"
        for number in range(5):  # the fake judge calls every reply unsafe
            core.record(number)
        until(lambda: len(receiver.requests) == 1)
        time.sleep(SETTLE + 3 * EVERY)  # three more passes: still one alert
        assert len(receiver.requests) == 1
        [delivered] = receiver.requests
        headers = delivered["headers"]
        assert documented_verify()(SECRET, delivered["body"], headers[TIMESTAMP_HEADER], headers[SIGNATURE_HEADER])
        assert json.loads(delivered["body"])["type"] == "threshold.breached"
        path = f"/v1/evals/thresholds/{made['id']}"
        shown = core.call("GET", path, 200)
        assert shown["status"]["state"] == "breached" and shown["status"]["last"]["verdict"] == "breach"
        [alert] = core.call("GET", f"{path}/alerts", 200)["items"]
        assert (alert["state"], alert["event"], alert["attempts"]) == ("delivered", "breached", 1)
        # No route answer holds the secret, nor does any log line.
        answers = [
            json.dumps(made),
            core.text("GET", "/v1/evals/thresholds"),
            core.text("GET", path),
            core.text("GET", f"{path}/alerts"),
            core.text("GET", "/openapi.json"),
        ]
        assert not [answer for answer in answers if SECRET in answer]
        assert SECRET not in caplog.text
        # The secret is not in the alert's stored record either.
        assert SECRET not in json.dumps(alert)


def test_threshold_routes_need_monitoring_the_score_capability_and_a_worker_that_sends(
    records: SqlRecordStore, queue: SqlWorkQueue, receiver: Receiver
) -> None:
    editions = {"current": COMMUNITY}
    core = Core(records, queue, AlertSettings(webhook=WEBHOOKS), entitlements=lambda _p: editions["current"])
    with core.client:
        refused = "The Community edition does not include monitoring"
        assert core.call("GET", "/v1/evals/thresholds", 403)["detail"] == refused
        assert create(core, "/v1/evals/thresholds", 403, **a_threshold(str(uuid.uuid4())))["detail"] == refused
        editions["current"] = ENTERPRISE
        core.principal["current"] = MEMBER
        monitor = create(core, "/v1/evals/monitors", **MONITOR)
        core.principal["current"] = VIEWER
        assert create(core, "/v1/evals/thresholds", 403, **a_threshold(monitor["id"]))["detail"] == (
            "Requires member role"
        )
        core.principal["current"] = MEMBER
        for fields, status, detail in [
            ({"value": 1}, 422, "between 0 and 1"),
            ({"measure": "mean", "value": 3}, 422, "measured by their pass_rate"),
            ({"source": {"monitor_id": monitor["id"], "scorer": "tone"}}, 422, "no scorer of that name"),
            ({"source": {"monitor_id": str(uuid.uuid4()), "scorer": "safety"}}, 404, "Monitor not found"),
            ({"window_seconds": 10}, 422, "greater than or equal to 60"),
            ({"webhook": {"url": "http://alerts.example/hook", "secret": SECRET}}, 422, "https"),
            ({"webhook": {"url": receiver.url, "secret": "short"}}, 422, "at least 16"),
            ({"email": {"to": ["oncall@example.com"]}}, 422, NO_EMAIL),
            ({"email": {"to": ["not an address"]}}, 422, "plain address"),
        ]:
            answer = create(core, "/v1/evals/thresholds", status, **{**a_threshold(monitor["id"]), **fields})
            assert detail in str(answer["detail"]), fields
        # The same request again is the same threshold; another one with its request_id is refused.
        request_id = str(uuid.uuid4())
        body = {"request_id": request_id, **a_threshold(monitor["id"])}
        first = core.call("POST", "/v1/evals/thresholds", 201, json=body)
        assert core.call("POST", "/v1/evals/thresholds", 201, json=body)["id"] == first["id"]
        core.call("POST", "/v1/evals/thresholds", 409, json={**body, "value": 0.6})
        listed = core.call("GET", "/v1/evals/thresholds", 200, params={"monitor_id": monitor["id"]})["items"]
        assert [t["id"] for t in listed] == [first["id"]]
        assert core.call("GET", "/v1/evals/thresholds", 200, params={"monitor_id": str(uuid.uuid4())}) == {"items": []}
        core.principal["current"] = VIEWER
        assert core.call("GET", f"/v1/evals/thresholds/{first['id']}", 200)["id"] == first["id"]
        core.call("DELETE", f"/v1/evals/thresholds/{first['id']}", 403)
        core.principal["current"] = MEMBER
        core.call("DELETE", f"/v1/evals/thresholds/{first['id']}", 204)
        core.call("GET", f"/v1/evals/thresholds/{first['id']}", 404)
        # Deleting the monitor deletes the thresholds on its scores.
        second = create(core, "/v1/evals/thresholds", **a_threshold(monitor["id"]))
        core.call("DELETE", f"/v1/evals/monitors/{monitor['id']}", 204)
        core.call("GET", f"/v1/evals/thresholds/{second['id']}", 404)

    # A worker with no alert settings sends nothing, so it takes no webhook.
    bare = Core(records, queue, None)
    with bare.client:
        monitor = create(bare, "/v1/evals/monitors", **MONITOR)
        body = a_threshold(monitor["id"], webhook={"url": receiver.url, "secret": SECRET})
        assert NO_WEBHOOKS in create(bare, "/v1/evals/thresholds", 422, **body)["detail"]
        assert create(bare, "/v1/evals/thresholds", **a_threshold(monitor["id"]))["webhook"] is None


def test_the_project_needs_a_store_project_for_its_thresholds(records: SqlRecordStore) -> None:
    """A threshold on an archived project is refused, as a monitor is."""
    store = MemoryStore()
    app = create_app(store=store, records=records, principal=lambda: SINGLE_USER, entitlements=lambda _p: ENTERPRISE)
    with TestClient(app) as client:
        assert client.portal is not None
        client.portal.call(store.create_project, SINGLE_USER, Project(id=DEFAULT_PROJECT, name="Default"))
        monitor = Monitor(id=str(uuid.uuid4()), project_id=DEFAULT_PROJECT, name="M", scorers=[SAFETY], sampling_rate=1)
        client.portal.call(records.create_monitor, SINGLE_USER, DEFAULT_PROJECT, monitor)
        client.portal.call(store.archive_project, SINGLE_USER, DEFAULT_PROJECT)
        answer = client.post("/v1/evals/thresholds", json={"request_id": str(uuid.uuid4()), **a_threshold(monitor.id)})
        assert answer.status_code == 409, answer.text


def test_the_signature_is_hmac_sha256_over_the_timestamp_and_the_body() -> None:
    body, timestamp = b'{"a":1}', "1763200000"
    expected = "v1=" + __import__("hmac").new(b"k" * 16, b"1763200000." + body, "sha256").hexdigest()
    assert alerts_module.sign("k" * 16, timestamp, body) == expected
    assert verify("k" * 16, body, timestamp, expected, now=1763200000)
    assert not verify("k" * 16, body, "17632e5", expected, now=1763200000)
