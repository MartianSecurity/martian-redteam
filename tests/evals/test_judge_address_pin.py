"""A judge's endpoint is connected to at the address that was checked (MartianSecurity/martian-notes#533).

An installation that calls an endpoint a customer named checks the address the name resolves to,
and an HTTP client that resolves the name again when it connects would call whatever the name
answers then: a name can answer a public address for the check and an internal one for the call
(DNS rebinding). The SDK judge, given an address policy (``sdk_judge(address_policy=...)``),
resolves the name when it opens each connection, asks the policy about every address, and connects
to one of those addresses by number, keeping the name for the ``Host`` header and for TLS.

Two loopback addresses stand in for the two places a name can point: ``127.0.0.1`` answers as the
public endpoint and ``127.0.0.2`` as an internal server the judge must never reach. The policy
allows the first only, and the name's answers are scripted: what each lookup returns, call by call.
Every server counts the connections it accepted, not only the requests it read, so a connection
that carried no request is seen too.
"""

from __future__ import annotations

import asyncio
import json
import socket
import ssl
import sys
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from martian.evals import _judge_transport, _pinned_address
from martian.evals.server import SINGLE_USER
from martian.evals.worker import JudgeError, JudgeRequest, public_addresses_only, sdk_judge
from martian.evals.worker.scorers import builtin_scorer

HOST = "judge.example.test"
KEY = "fixture-judge-key"
PUBLIC = "127.0.0.1"
INTERNAL = "127.0.0.2"
PROXY_VARIABLES = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "OPENAI_API_BASE",
    "OPENAI_BASE_URL",
)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="127.0.0.2 is a loopback address on Linux")


def allow_only(*allowed: str):
    """A policy that allows the addresses it is given and no other."""
    return lambda host, address: address in allowed


LOOKUP = socket.getaddrinfo


class Names:
    """What the name resolves to, lookup by lookup: each answer is a list of addresses, and the
    last one repeats. A name it does not script is looked up as usual. It stands in for
    ``socket.getaddrinfo``, which is all the judge's connection resolves names with."""

    def __init__(self, *answers: list[str]) -> None:
        self.answers = list(answers)
        self.lookups: list[str] = []
        self.lock = threading.Lock()

    def __call__(self, host: str, port: int, *args: Any, **kwargs: Any) -> list[Any]:
        if host != HOST:
            return LOOKUP(host, port, *args, **kwargs)
        with self.lock:
            self.lookups.append(host)
            answer = self.answers[min(len(self.lookups), len(self.answers)) - 1]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port)) for address in answer]


class Server:
    """A loopback server that counts its connections and keeps each request it reads."""

    def __init__(
        self, host: str = PUBLIC, port: int = 0, *, tls: ssl.SSLContext | None = None, redirect: str | None = None
    ) -> None:
        self.requests: list[dict[str, Any]] = []
        self.connections = 0
        self.names: list[str | None] = []  # the TLS server name each handshake asked for
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                pass

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.requests.append(
                    {"path": self.path, "headers": {name.lower(): value for name, value in self.headers.items()}}
                )
                if redirect is not None:
                    self.send_response(307)
                    self.send_header("Location", redirect)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                data = json.dumps(chat_completion(body)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        class Counting(ThreadingHTTPServer):
            def get_request(self) -> tuple[Any, Any]:
                accepted = super().get_request()
                owner.connections += 1
                return accepted

        self.server = Counting((host, port), Handler)
        if tls is not None:
            tls.sni_callback = lambda sock, name, context: owner.names.append(name)
            self.server.socket = tls.wrap_socket(self.server.socket, server_side=True)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def port(self) -> int:
        return int(self.server.server_port)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def chat_completion(body: dict[str, Any]) -> dict[str, Any]:
    """A judge's model that passes every rubric it is asked about."""
    rubrics = json.loads(body["messages"][0]["content"].split("\nRubrics:\n", 1)[1])
    verdicts = [
        {"rubric_index": number, "reason": "Checked.", "verdict": "pass", "evidence_ids": []}
        for number, _ in enumerate(rubrics, 1)
    ]
    message = {"role": "assistant", "content": json.dumps({"result": verdicts})}
    return {
        "id": "chatcmpl-fixture",
        "object": "chat.completion",
        "created": 0,
        "model": "your-model",
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


@pytest.fixture
def servers():
    """Starts servers and closes them all: ``servers(INTERNAL)`` is a server at that address."""
    started: list[Server] = []

    def start(host: str = PUBLIC, port: int = 0, **options: Any) -> Server:
        started.append(server := Server(host, port, **options))
        return server

    yield start
    for server in started:
        server.close()


@pytest.fixture(autouse=True)
def environment(monkeypatch):
    for name in PROXY_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    monkeypatch.setenv("MLFLOW_GATEWAY_ROUTE_TIMEOUT_SECONDS", "10")  # a connection that hangs fails the test


def pair(servers) -> tuple[Server, Server]:
    """The public server and the internal one, listening on the same port of their two addresses."""
    internal = servers(INTERNAL)
    return servers(PUBLIC, internal.port), internal


def judged(endpoint: str, policy: Any, **options: Any) -> Any:
    judge = sdk_judge(model="openai:/your-model", address_policy=policy, **options)
    request = JudgeRequest(
        principal=SINGLE_USER,
        prompt="How do I reset my password?",
        expected="Use the reset link.",
        output="Use the reset link.",
        scorers=(builtin_scorer("correctness"),),
        endpoint=endpoint,
    )
    return asyncio.run(judge(request))


def test_a_name_that_rebinds_after_its_check_is_never_connected_to(servers, monkeypatch):
    public, internal = pair(servers)
    # The first lookup answers the public address, every later one the internal address.
    names = Names([PUBLIC], [INTERNAL])
    monkeypatch.setattr(socket, "getaddrinfo", names)
    endpoint = f"http://{HOST}:{public.port}/v1"
    asked: list[tuple[str, str]] = []

    def policy(host: str, address: str) -> bool:
        asked.append((host, address))
        return address == PUBLIC

    first = judged(endpoint, policy)
    assert first.verdicts["correctness"].value is True
    # One lookup for the one connection, the policy asked about the address it answered, and the
    # connection went to that address: the connect did not look the name up again.
    assert (len(names.lookups), asked) == (1, [(HOST, PUBLIC)])
    assert (len(public.requests), public.connections) == (1, 1)

    with pytest.raises(JudgeError) as refused:
        judged(endpoint, policy)
    assert str(refused.value).startswith("Judge draft failed [judge_provider_address_refused].")
    # The next connection looked the name up, was told the internal address, and was refused.
    assert (len(names.lookups), asked[1:]) == (2, [(HOST, INTERNAL)])
    assert (internal.connections, internal.requests) == (0, [])
    assert (len(public.requests), public.connections) == (1, 1)


def test_a_name_with_one_refused_address_among_its_answers_is_refused_whole(servers, monkeypatch):
    public, internal = pair(servers)
    monkeypatch.setattr(socket, "getaddrinfo", Names([PUBLIC, INTERNAL]))
    with pytest.raises(JudgeError) as refused:
        judged(f"http://{HOST}:{public.port}/v1", allow_only(PUBLIC))
    assert "[judge_provider_address_refused]" in str(refused.value)
    assert (public.connections, internal.connections) == (0, 0)


def test_a_refused_address_is_refused_once_without_a_connection_or_a_retry(servers, monkeypatch):
    public, internal = pair(servers)
    names = Names([INTERNAL])
    monkeypatch.setattr(socket, "getaddrinfo", names)
    waited: list[float] = []
    monkeypatch.setattr(_judge_transport, "_sleep", waited.append)
    with pytest.raises(JudgeError) as refused:
        judged(f"http://{HOST}:{public.port}/v1", allow_only(PUBLIC), max_retries=5)
    assert str(refused.value) == (
        "Judge draft failed [judge_provider_address_refused]. The endpoint's name resolves to an address this "
        "installation does not allow a judge to call. Check the judge's endpoint."
    )
    assert (len(names.lookups), waited) == (1, [])
    assert (public.connections, internal.connections) == (0, 0)


def test_the_request_keeps_the_names_host_header_and_the_key(servers, monkeypatch):
    public = servers()
    monkeypatch.setattr(socket, "getaddrinfo", Names([PUBLIC]))
    judged(f"http://{HOST}:{public.port}/v1", allow_only(PUBLIC))
    request = public.requests[0]
    assert request["path"] == "/v1/chat/completions"
    assert request["headers"]["host"] == f"{HOST}:{public.port}"
    assert request["headers"]["authorization"] == f"Bearer {KEY}"


def certificate(folder: Path, name: str) -> tuple[Path, Path]:
    """A self-signed certificate that is valid for ``name`` only, and its key, written to ``folder``."""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.now(UTC)
    built = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(True, False, False, False, False, True, False, False, False), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    certificate_file, key_file = folder / "judge.pem", folder / "judge.key"
    certificate_file.write_bytes(built.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    return certificate_file, key_file


def test_tls_is_verified_and_asked_for_by_the_name_not_the_address(servers, monkeypatch, tmp_path):
    certificate_file, key_file = certificate(tmp_path, HOST)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate_file, key_file)
    public = servers(tls=context)
    monkeypatch.setattr(socket, "getaddrinfo", Names([PUBLIC]))
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(certificate_file))  # an installation's own CA, as before

    judged(f"https://{HOST}:{public.port}/v1", allow_only(PUBLIC))

    # The certificate names the host and not 127.0.0.1, so it verified against the name, and the
    # server was asked for the name (SNI): the connection went to the address by number.
    assert public.names == [HOST]
    assert public.requests[0]["headers"]["host"] == f"{HOST}:{public.port}"


def test_a_certificate_the_name_does_not_match_is_not_accepted(servers, monkeypatch, tmp_path):
    certificate_file, key_file = certificate(tmp_path, "another.example.test")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate_file, key_file)
    public = servers(tls=context)
    monkeypatch.setattr(socket, "getaddrinfo", Names([PUBLIC]))
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(certificate_file))
    with pytest.raises(JudgeError) as refused:
        judged(f"https://{HOST}:{public.port}/v1", allow_only(PUBLIC), max_retries=0)
    assert "[judge_provider_unreachable]" in str(refused.value)
    assert public.requests == []


def test_a_proxy_in_the_environment_is_not_used(servers, monkeypatch):
    public = servers()
    proxy = servers()
    monkeypatch.setattr(socket, "getaddrinfo", Names([PUBLIC]))
    # A proxy would resolve the name itself, which is what the pin stops.
    monkeypatch.setenv("HTTP_PROXY", f"http://{PUBLIC}:{proxy.port}")
    monkeypatch.setenv("http_proxy", f"http://{PUBLIC}:{proxy.port}")
    judged(f"http://{HOST}:{public.port}/v1", allow_only(PUBLIC))
    assert (len(public.requests), proxy.connections) == (1, 0)


def test_a_redirect_is_not_followed(servers, monkeypatch):
    elsewhere = servers()
    public = servers(redirect=f"http://{PUBLIC}:{elsewhere.port}/v1/chat/completions")
    monkeypatch.setattr(socket, "getaddrinfo", Names([PUBLIC]))
    with pytest.raises(JudgeError) as refused:
        judged(f"http://{HOST}:{public.port}/v1", allow_only(PUBLIC))
    assert "[judge_provider_address]" in str(refused.value)  # it did not answer as a chat endpoint
    assert (len(public.requests), elsewhere.connections) == (1, 0)


def test_an_ipv6_address_in_brackets_is_resolved_and_checked_without_them(monkeypatch):
    looked_up: list[str] = []
    asked: list[tuple[str, str]] = []

    def lookup(host: str, port: int, *args: Any, **kwargs: Any) -> list[Any]:
        looked_up.append(host)
        return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700:4700::1111", port, 0, 0))]

    def refuse(host: str, address: str) -> bool:
        asked.append((host, address))
        return False

    monkeypatch.setattr(socket, "getaddrinfo", lookup)
    with pytest.raises(_pinned_address.AddressRefused):
        _pinned_address._connect("[2606:4700:4700::1111]", 443, 1.0, None, None, refuse)
    assert looked_up == ["2606:4700:4700::1111"]
    assert asked == [("2606:4700:4700::1111", "2606:4700:4700::1111")]


def test_a_provider_that_cannot_be_pinned_is_refused_not_called_without_the_pin(monkeypatch):
    # LiteLLM, which MLflow uses for the providers that are not OpenAI compatible, opens its own
    # connections. With an address policy there is no unpinned way to call it.
    monkeypatch.setattr(_judge_transport, "get_adapter", lambda *args: _judge_transport.LiteLLMAdapter())

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("an unpinned call")

    monkeypatch.setattr(_judge_transport, "_litellm_completion", forbidden)
    judge = sdk_judge(model="bedrock:/zai.glm-5", address_policy=allow_only(PUBLIC), max_retries=0)
    request = JudgeRequest(
        principal=SINGLE_USER,
        prompt="Hi",
        expected="Hello",
        output="Hello",
        scorers=(builtin_scorer("safety"),),
    )
    with pytest.raises(JudgeError) as refused:
        asyncio.run(judge(request))
    assert str(refused.value) == (
        "Judge draft failed [judge_provider_config]. This installation lets a judge call only an OpenAI-compatible "
        "endpoint: check judge.model."
    )


@pytest.mark.parametrize(
    ("address", "public"),
    [
        ("93.184.216.34", True),
        ("2606:4700:4700::1111", True),
        ("::ffff:93.184.216.34", True),
        ("127.0.0.1", False),
        ("::1", False),
        ("10.1.2.3", False),
        ("172.16.0.9", False),
        ("192.168.1.1", False),
        ("169.254.169.254", False),  # a cloud's metadata service
        ("100.64.0.1", False),
        ("0.0.0.0", False),
        ("fc00::1", False),
        ("fe80::1", False),
        ("fe80::1%eth0", False),
        ("::ffff:10.1.2.3", False),  # a private address written as an IPv6 one
        ("::ffff:127.0.0.1", False),
        ("not an address", False),
    ],
)
def test_public_addresses_only_allows_a_public_address_and_nothing_else(address, public):
    assert public_addresses_only(HOST, address) is public
