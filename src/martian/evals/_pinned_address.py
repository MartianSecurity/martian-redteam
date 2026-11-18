"""Connect to the address that was checked, and to no other (MartianSecurity/martian-notes#533).

An installation that calls an endpoint a customer named checks the address the name resolves to:
a private or reserved address would aim its worker at its own network. A check that only looks
the name up is not enough, because an HTTP client that resolves the name again when it connects
calls whatever the name answers then, and a name can answer a public address for the check and an
internal one for the call (DNS rebinding).

:func:`pinned_session` is a ``requests`` session that cannot be steered that way. Its connections
resolve the name when each one is opened, ask the :data:`AddressPolicy` about every address the
name answered, and refuse the name (:class:`AddressRefused`) if the policy refuses any of them. It
then connects to one of the addresses it checked, by number: the socket address the policy was
asked about is the one connected to, so nothing resolves the name between the check and the
connection. The URL's name stays the connection's host, so the ``Host`` header, the TLS server name
(SNI) and the certificate check are the name's, not the address's.

The session also never follows a redirect and never uses a proxy from the environment, since a
redirect names a new address and a proxy resolves the name itself. A caller that gives a policy gets
this session or a refusal; nothing falls back to an unpinned connection. A certificate authority
named by ``REQUESTS_CA_BUNDLE`` or ``CURL_CA_BUNDLE`` is still used, as an installation with its own
certificate authority needs.

The core never asks for a policy by itself: a local run calls the endpoint its user named, which may
be on the same machine. An edition that serves other people's endpoints passes one
(``sdk_judge(address_policy=...)``); :func:`public_addresses_only` is the usual one.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import sys
from collections.abc import Callable, Mapping
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import ConnectTimeoutError, HTTPError, NameResolutionError, NewConnectionError

AddressPolicy = Callable[[str, str], bool]
"""``policy(host, address)``: whether a connection for the name ``host`` may go to ``address``, an IP
address as text. It is asked about every address a name answers, and the name is refused if it says
no to any."""


class AddressRefused(HTTPError):
    """A name resolved to an address the policy does not allow. It is not an ``OSError``, so no layer
    treats it as a connection failure to retry."""


def public_addresses_only(host: str, address: str) -> bool:
    """Whether ``address`` is a public one: not loopback, private, link-local, shared, multicast or
    otherwise reserved. An IPv6 address that embeds an IPv4 one (``::ffff:10.0.0.1``) is judged by
    the IPv4 address. Anything that is not an IP address is not public."""
    try:
        parsed = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        return parsed.ipv4_mapped.is_global
    return parsed.is_global


def _open(family: int, proto: int, address: Any, timeout: object, source_address: Any, options: Any) -> socket.socket:
    """One connection to a socket address that was resolved and checked, as ``urllib3`` opens one:
    its socket options, its timeout (an unset one leaves the socket's default) and source address."""
    sock = socket.socket(family, socket.SOCK_STREAM, proto)
    try:
        for option in options or ():
            sock.setsockopt(*option)
        if timeout is None or isinstance(timeout, (int, float)):
            sock.settimeout(timeout)
        if source_address:
            sock.bind(source_address)
        sock.connect(address)
    except BaseException:
        sock.close()
        raise
    return sock


def _connect(
    host: str, port: int, timeout: object, source_address: Any, options: Any, policy: AddressPolicy
) -> socket.socket:
    """Resolve ``host`` once, require the policy to allow every address it answered, and connect to
    the first of them that accepts."""
    name = host[1:-1] if host.startswith("[") and host.endswith("]") else host  # an IPv6 literal, as in a URL
    rows = [
        (family, proto, address)
        for family, _, proto, _, address in socket.getaddrinfo(name, port, type=socket.SOCK_STREAM)
        if family in (socket.AF_INET, socket.AF_INET6)
    ]
    if not rows:
        raise socket.gaierror(socket.EAI_NONAME, "the name has no address")
    for _, _, address in rows:
        if not policy(name, str(address[0])):
            raise AddressRefused("the name resolves to an address the policy does not allow")
    failure: OSError | None = None
    for family, proto, address in rows:
        try:
            return _open(family, proto, address, timeout, source_address, options)
        except OSError as error:
            failure = error
    assert failure is not None
    raise failure


def _pools(policy: AddressPolicy) -> Mapping[str, type[HTTPConnectionPool]]:
    """Connection pools, for http and https, whose connections are pinned by ``policy``."""

    class Pinned:
        """The ``_new_conn`` of ``urllib3``'s connections, with the address pinned. Its errors are the
        stock ones, so a failure to connect is read as any other."""

        def _new_conn(self) -> socket.socket:
            connection: Any = self
            try:
                sock = _connect(
                    connection._dns_host,
                    connection.port,
                    connection.timeout,
                    connection.source_address,
                    connection.socket_options,
                    policy,
                )
            except socket.gaierror as error:
                raise NameResolutionError(connection.host, connection, error) from error
            except TimeoutError as error:
                message = f"Connection to {connection.host} timed out. (connect timeout={connection.timeout})"
                raise ConnectTimeoutError(connection, message) from error
            except OSError as error:
                raise NewConnectionError(connection, f"Failed to establish a new connection: {error}") from error
            sys.audit("http.client.connect", connection, connection.host, connection.port)
            return sock

    class HTTP(Pinned, HTTPConnection):
        pass

    class HTTPS(Pinned, HTTPSConnection):
        pass

    class HTTPPool(HTTPConnectionPool):
        ConnectionCls = HTTP

    class HTTPSPool(HTTPSConnectionPool):
        ConnectionCls = HTTPS

    return {"http": HTTPPool, "https": HTTPSPool}


class _Adapter(HTTPAdapter):
    def __init__(self, policy: AddressPolicy) -> None:
        self._pools = _pools(policy)
        super().__init__(max_retries=0)

    def init_poolmanager(self, *args: Any, **kwargs: Any) -> None:
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = dict(self._pools)

    def proxy_manager_for(self, *args: Any, **kwargs: Any) -> Any:
        raise AddressRefused("a pinned connection is not made through a proxy")


class _Session(requests.Session):
    """A session that never follows a redirect: it names an address nothing has checked."""

    def request(self, *args: Any, **kwargs: Any) -> requests.Response:
        kwargs["allow_redirects"] = False
        return super().request(*args, **kwargs)


def pinned_session(policy: AddressPolicy) -> requests.Session:
    """A session whose every connection is pinned by ``policy``, as the module describes."""
    session = _Session()
    session.trust_env = False
    session.verify = os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("CURL_CA_BUNDLE") or True
    adapter = _Adapter(policy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


__all__ = ["AddressPolicy", "AddressRefused", "pinned_session", "public_addresses_only"]
