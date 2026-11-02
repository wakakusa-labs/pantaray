"""Client handshakes the egress proxy accepts: HTTP proxy requests and SOCKS5.

Parsing only: every function here reads what the client sent and turns it into
a typed request, or raises ``HandshakeRejected`` with the reply to send back.
Authorization, destination policy and dialing belong to ``egress_proxy``.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import ipaddress
import re
from dataclasses import dataclass
from typing import Literal

SOCKS_VERSION = 0x05
_SOCKS_USERPASS_METHOD = 0x02
_SOCKS_NO_ACCEPTABLE_METHOD = 0xFF
_SOCKS_USERPASS_VERSION = 0x01
_SOCKS_CONNECT = 0x01
_SOCKS_ATYP_IPV4 = 0x01
_SOCKS_ATYP_DOMAIN = 0x03
_SOCKS_ATYP_IPV6 = 0x04

SOCKS_SUCCEEDED = 0x00
SOCKS_GENERAL_FAILURE = 0x01
SOCKS_NOT_ALLOWED = 0x02
SOCKS_HOST_UNREACHABLE = 0x04
SOCKS_COMMAND_NOT_SUPPORTED = 0x07
SOCKS_ADDRESS_TYPE_NOT_SUPPORTED = 0x08

HEAD_END = b"\r\n\r\n"
_HTTP_DEFAULT_PORT = 80
# ASCII names only: an internationalized name reaches a proxy as punycode.
_HOSTNAME = re.compile(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*\.?")
# Hop-by-hop headers this proxy owns. Host is replaced by the request-target's
# authority (RFC 9112 section 3.2.2) so the server sees the host that was dialed.
_REQUEST_HEADERS_REPLACED = frozenset(
    b"connection keep-alive proxy-connection proxy-authorization te upgrade host".split()
)


@dataclass(frozen=True, slots=True)
class ProxyTarget:
    """Where the client asked to go: a lowercase name or a canonical IP literal."""

    host: str
    port: int


# Body framing of a forwarded request: an exact length, or chunked coding.
type RequestBody = int | Literal["chunked"]


@dataclass(frozen=True, slots=True)
class HttpConnect:
    token: str | None
    target: ProxyTarget


@dataclass(frozen=True, slots=True)
class HttpForward:
    """A plain ``http://`` request, rewritten to origin form for the server."""

    token: str | None
    target: ProxyTarget
    head: bytes
    body: RequestBody


@dataclass(frozen=True, slots=True)
class SocksConnect:
    target: ProxyTarget


type ProxyRequest = HttpConnect | HttpForward | SocksConnect


class HandshakeRejected(Exception):
    """The client's handshake is malformed or unsupported; ``reply`` says so."""

    def __init__(self, reply: bytes) -> None:
        super().__init__("proxy handshake rejected")
        self.reply = reply


def http_reply(status: str, extra_headers: str = "") -> bytes:
    return (
        f"HTTP/1.1 {status}\r\n{extra_headers}"
        "Content-Length: 0\r\nConnection: close\r\n\r\n"
    ).encode("ascii")


CONNECT_ESTABLISHED = b"HTTP/1.1 200 Connection Established\r\n\r\n"
BAD_REQUEST = http_reply("400 Bad Request")
PROXY_AUTH_REQUIRED = http_reply(
    "407 Proxy Authentication Required",
    'Proxy-Authenticate: Basic realm="pantaray"\r\n',
)
HEADERS_TOO_LARGE = http_reply("431 Request Header Fields Too Large")
FORBIDDEN = http_reply("403 Forbidden")
BAD_GATEWAY = http_reply("502 Bad Gateway")


def socks_reply(code: int) -> bytes:
    # The bound address is unused by CONNECT clients; 0.0.0.0:0 says nothing.
    return bytes([SOCKS_VERSION, code, 0x00, _SOCKS_ATYP_IPV4, 0, 0, 0, 0, 0, 0])


def proxy_target(host: str, port: int) -> ProxyTarget:
    if not 0 < port < 65536:
        raise ValueError("port out of range")
    try:
        return ProxyTarget(str(ipaddress.ip_address(host)), port)
    except ValueError:
        pass
    if _HOSTNAME.fullmatch(host) is None:
        raise ValueError("invalid host name")
    return ProxyTarget(host.lower(), port)


def _parse_authority(authority: str, default_port: int | None) -> ProxyTarget:
    if authority.startswith("["):
        host, closed, rest = authority[1:].partition("]")
        if not closed or ":" not in host:
            raise ValueError("bracketed host must be IPv6")
    else:
        host, colon, port_text = authority.partition(":")
        rest = colon + port_text
    if rest:
        port_text = rest.removeprefix(":")
        if port_text == rest or not (port_text.isascii() and port_text.isdigit()):
            raise ValueError("invalid port")
        return proxy_target(host, int(port_text))
    if default_port is None:
        raise ValueError("port required")
    return proxy_target(host, default_port)


def _parse_head(head: bytes) -> tuple[list[bytes], list[tuple[bytes, bytes]]]:
    lines = head[: -len(HEAD_END)].split(b"\r\n")
    start_line = lines[0].split(b" ")
    headers: list[tuple[bytes, bytes]] = []
    for line in lines[1:]:
        name, colon, value = line.partition(b":")
        if not colon or not name or name != name.strip():
            raise ValueError("malformed header line")
        headers.append((name, value.strip()))
    return start_line, headers


def _values(headers: list[tuple[bytes, bytes]], name: bytes) -> list[bytes]:
    return [value for key, value in headers if key.lower() == name]


def _basic_auth_user(headers: list[tuple[bytes, bytes]]) -> str | None:
    values = _values(headers, b"proxy-authorization")
    if len(values) != 1:
        return None
    scheme, _, encoded = values[0].partition(b" ")
    if scheme.lower() != b"basic":
        return None
    try:
        credentials = base64.b64decode(encoded.strip(), validate=True).decode("ascii")
    except (binascii.Error, UnicodeDecodeError):
        return None
    return credentials.partition(":")[0]


def _request_body(headers: list[tuple[bytes, bytes]]) -> RequestBody:
    codings = _values(headers, b"transfer-encoding")
    lengths = _values(headers, b"content-length")
    if codings:
        # Anything but a lone "chunked" cannot be framed, and a request carrying
        # both framings is the classic smuggling shape.
        if lengths or len(codings) != 1 or codings[0].lower() != b"chunked":
            raise ValueError("unsupported request framing")
        return "chunked"
    if not lengths:
        return 0
    if len(set(lengths)) != 1 or not lengths[0].isdigit():
        raise ValueError("invalid Content-Length")
    return int(lengths[0])


def _forward_request(
    method: bytes, target: str, version: bytes, headers: list[tuple[bytes, bytes]]
) -> HttpForward:
    if target[:7].lower() != "http://":
        raise ValueError("only absolute http:// targets are forwarded")
    rest = target[7:]
    cut = min((i for i in map(rest.find, "/?#") if i >= 0), default=len(rest))
    authority, path = rest[:cut], rest[cut:].partition("#")[0]
    if "@" in authority:
        raise ValueError("userinfo in request target")
    if not path.startswith("/"):
        path = "/" + path
    kept = [
        name + b": " + value
        for name, value in headers
        if name.lower() not in _REQUEST_HEADERS_REPLACED
    ]
    # One request per proxy connection: Connection: close keeps a reused client
    # connection from carrying a second request to the first host.
    lines = [
        b" ".join((method, path.encode("ascii"), version)),
        b"Host: " + authority.encode("ascii"),
        *kept,
        b"Connection: close",
    ]
    return HttpForward(
        token=_basic_auth_user(headers),
        target=_parse_authority(authority, _HTTP_DEFAULT_PORT),
        head=b"\r\n".join(lines) + HEAD_END,
        body=_request_body(headers),
    )


async def read_http_request(
    first_byte: bytes, reader: asyncio.StreamReader
) -> HttpConnect | HttpForward:
    """Read one proxy request head; the reader's limit caps its size."""

    try:
        head = first_byte + await reader.readuntil(HEAD_END)
    except asyncio.LimitOverrunError as exc:
        raise HandshakeRejected(HEADERS_TOO_LARGE) from exc
    try:
        start_line, headers = _parse_head(head)
        if len(start_line) != 3 or not start_line[2].startswith(b"HTTP/1."):
            raise ValueError("malformed request line")
        method, target, version = start_line
        if method == b"CONNECT":
            return HttpConnect(
                token=_basic_auth_user(headers),
                target=_parse_authority(target.decode("ascii"), None),
            )
        return _forward_request(method, target.decode("ascii"), version, headers)
    except ValueError as exc:
        raise HandshakeRejected(BAD_REQUEST) from exc


async def read_socks_token(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> str | None:
    """Negotiate RFC 1929 username/password auth; the username is the token.

    The version byte has already been read by the caller.
    """

    (method_count,) = await reader.readexactly(1)
    methods = await reader.readexactly(method_count)
    if _SOCKS_USERPASS_METHOD not in methods:
        raise HandshakeRejected(bytes([SOCKS_VERSION, _SOCKS_NO_ACCEPTABLE_METHOD]))
    writer.write(bytes([SOCKS_VERSION, _SOCKS_USERPASS_METHOD]))
    version, user_length = await reader.readexactly(2)
    if version != _SOCKS_USERPASS_VERSION:
        raise HandshakeRejected(socks_auth_reply(accepted=False))
    user = await reader.readexactly(user_length)
    (password_length,) = await reader.readexactly(1)
    await reader.readexactly(password_length)
    try:
        return user.decode("ascii")
    except UnicodeDecodeError:
        return None


def socks_auth_reply(*, accepted: bool) -> bytes:
    return bytes([_SOCKS_USERPASS_VERSION, 0x00 if accepted else 0x01])


async def read_socks_target(reader: asyncio.StreamReader) -> ProxyTarget:
    version, command, _reserved, address_type = await reader.readexactly(4)
    if version != SOCKS_VERSION:
        raise HandshakeRejected(socks_reply(SOCKS_GENERAL_FAILURE))
    if command != _SOCKS_CONNECT:
        raise HandshakeRejected(socks_reply(SOCKS_COMMAND_NOT_SUPPORTED))
    try:
        if address_type == _SOCKS_ATYP_IPV4:
            host = str(ipaddress.IPv4Address(await reader.readexactly(4)))
        elif address_type == _SOCKS_ATYP_IPV6:
            host = str(ipaddress.IPv6Address(await reader.readexactly(16)))
        elif address_type == _SOCKS_ATYP_DOMAIN:
            (name_length,) = await reader.readexactly(1)
            host = (await reader.readexactly(name_length)).decode("ascii")
        else:
            raise HandshakeRejected(socks_reply(SOCKS_ADDRESS_TYPE_NOT_SUPPORTED))
        port = int.from_bytes(await reader.readexactly(2))
        return proxy_target(host, port)
    except ValueError as exc:
        raise HandshakeRejected(socks_reply(SOCKS_GENERAL_FAILURE)) from exc
