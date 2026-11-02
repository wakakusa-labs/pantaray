"""An ssh ProxyCommand: reach ``HOST PORT`` through the command's egress proxy.

A sandboxed command connects only to loopback, and macOS's nc cannot give the
proxy its lease token, so ssh (through GIT_SSH_COMMAND) runs this instead. The
proxy URL, token included, is read from HTTPS_PROXY rather than the arguments,
which keeps it out of process listings.

It runs as a copy in the command's temp dir under the app runtime's Python in
isolated mode, so it uses the standard library alone.
"""

from __future__ import annotations

import base64
import os
import re
import socket
import sys
import threading
from urllib.parse import unquote, urlsplit

# The host comes from a git URL, which a repository chooses: nothing that could
# end the request line gets into it.
_HOST = re.compile(r"[A-Za-z0-9._:-]+")
_HEAD_LIMIT_BYTES = 64 * 1024
_CHUNK_BYTES = 64 * 1024


def _fail(message: str) -> int:
    sys.stderr.write(f"sandbox proxy: {message}\n")
    return 1


def _write_all(fd: int, data: bytes) -> None:
    while data:
        data = data[os.write(fd, data) :]


def _send_stdin(upstream: socket.socket) -> None:
    try:
        while data := os.read(sys.stdin.fileno(), _CHUNK_BYTES):
            upstream.sendall(data)
        upstream.shutdown(socket.SHUT_WR)
    except OSError:
        # The connection is gone; the receiving side reports how it ended.
        return


def main(argv: list[str]) -> int:
    if len(argv) != 3 or not _HOST.fullmatch(argv[1]) or not argv[2].isdigit():
        return _fail("expected HOST PORT")
    host, port = argv[1], argv[2]
    target = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    proxy = urlsplit(os.environ.get("HTTPS_PROXY", ""))
    if proxy.hostname is None or proxy.port is None or proxy.username is None:
        return _fail("HTTPS_PROXY does not name the network proxy")
    credentials = base64.b64encode(
        f"{unquote(proxy.username)}:{unquote(proxy.password or '')}".encode()
    ).decode("ascii")
    try:
        upstream = socket.create_connection((proxy.hostname, proxy.port))
        upstream.sendall(
            f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n"
            f"Proxy-Authorization: Basic {credentials}\r\n\r\n".encode("ascii")
        )
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = upstream.recv(_CHUNK_BYTES)
            if not chunk or len(head) > _HEAD_LIMIT_BYTES:
                return _fail(f"{target}: no answer")
            head += chunk
    except OSError as exc:
        return _fail(f"{target}: {exc}")
    head, _, early = head.partition(b"\r\n\r\n")
    status = head.split(b"\r\n", 1)[0].decode("latin-1").split(" ", 1)[-1]
    if not status.startswith("200"):
        return _fail(f"{target}: {status}")
    # The server may speak first (ssh sends its banner) within the same read.
    _write_all(sys.stdout.fileno(), early)
    threading.Thread(target=_send_stdin, args=(upstream,), daemon=True).start()
    # ssh learns the connection ended when this exits, whatever stdin is doing.
    try:
        while chunk := upstream.recv(_CHUNK_BYTES):
            _write_all(sys.stdout.fileno(), chunk)
    except OSError as exc:
        return _fail(f"{target}: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
