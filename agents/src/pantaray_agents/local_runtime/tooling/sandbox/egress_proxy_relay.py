"""Byte relays of the egress proxy once a destination is connected.

No relay has an idle timeout: a long download or a quiet tunnel stays open
until either side closes it or the command's lease ends.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from .egress_proxy_wire import HEAD_END, RequestBody

RELAY_CHUNK_BYTES = 64 * 1024
_CHUNK_LINE_END = b"\r\n"
_RESPONSE_HEADERS_REPLACED = frozenset(
    b"connection keep-alive proxy-connection".split()
)

type CountBytes = Callable[[int], None]


async def _send(writer: asyncio.StreamWriter, data: bytes, count: CountBytes) -> None:
    writer.write(data)
    # Counted once drained to the socket: a buffer discarded when a lease ends
    # was never sent.
    await writer.drain()
    count(len(data))


async def _pipe(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, count: CountBytes
) -> None:
    while chunk := await reader.read(RELAY_CHUNK_BYTES):
        await _send(writer, chunk, count)
    writer.write_eof()


async def tunnel(
    client: tuple[asyncio.StreamReader, asyncio.StreamWriter],
    upstream: tuple[asyncio.StreamReader, asyncio.StreamWriter],
    *,
    count_sent: CountBytes,
    count_received: CountBytes,
) -> None:
    """Relay both directions until both reach EOF or either side fails.

    Half-close is passed through, so a client that shuts down its sending side
    still receives the rest of the reply.
    """

    directions = [
        asyncio.create_task(_pipe(client[0], upstream[1], count_sent)),
        asyncio.create_task(_pipe(upstream[0], client[1], count_received)),
    ]
    try:
        done, _ = await asyncio.wait(directions, return_when=asyncio.FIRST_EXCEPTION)
    finally:
        for direction in directions:
            direction.cancel()
        await asyncio.wait(directions)
    for direction in done:
        error = direction.exception()
        # A reset or broken pipe ends the tunnel; anything else is a defect.
        if error is not None and not isinstance(error, OSError):
            raise error


async def _copy_exact(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    size: int,
    count: CountBytes,
) -> None:
    while size:
        chunk = await reader.read(min(size, RELAY_CHUNK_BYTES))
        if not chunk:
            raise asyncio.IncompleteReadError(b"", size)
        size -= len(chunk)
        await _send(writer, chunk, count)


async def _copy_request_body(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    body: RequestBody,
    count: CountBytes,
) -> None:
    if body != "chunked":
        await _copy_exact(reader, writer, body, count)
        return
    while True:
        size_line = await reader.readuntil(_CHUNK_LINE_END)
        size = int(size_line.partition(b";")[0].strip(), 16)
        await _send(writer, size_line, count)
        if size == 0:
            while (line := await reader.readuntil(_CHUNK_LINE_END)) != _CHUNK_LINE_END:
                await _send(writer, line, count)
            await _send(writer, line, count)
            return
        await _copy_exact(reader, writer, size + len(_CHUNK_LINE_END), count)


async def _upload(
    client_reader: asyncio.StreamReader,
    upstream_writer: asyncio.StreamWriter,
    body: RequestBody,
    count: CountBytes,
) -> None:
    try:
        await _copy_request_body(client_reader, upstream_writer, body, count)
        # The client was told this connection closes after one response, so
        # anything it still sends (a pipelined request to another host) is
        # dropped rather than delivered to this server.
        while await client_reader.read(RELAY_CHUNK_BYTES):
            pass
        # Pass the client's close on, so a server that kept the connection open
        # despite Connection: close still ends it.
        upstream_writer.write_eof()
    except (EOFError, OSError, ValueError, asyncio.LimitOverrunError):
        # A truncated or malformed body: the server would wait for the rest.
        upstream_writer.close()


def _close_response_head(head: bytes) -> bytes:
    lines = head[: -len(HEAD_END)].split(b"\r\n")
    kept = [
        line
        for line in lines[1:]
        if line.partition(b":")[0].strip().lower() not in _RESPONSE_HEADERS_REPLACED
    ]
    return b"\r\n".join([lines[0], *kept, b"Connection: close"]) + HEAD_END


def _is_interim(head: bytes) -> bool:
    status = head.split(b" ", 2)[1:2]
    return bool(status) and status[0].startswith(b"1") and status[0] != b"101"


async def forward(
    client: tuple[asyncio.StreamReader, asyncio.StreamWriter],
    upstream: tuple[asyncio.StreamReader, asyncio.StreamWriter],
    *,
    head: bytes,
    body: RequestBody,
    count_sent: CountBytes,
    count_received: CountBytes,
) -> bool:
    """Send one request and relay its response; False if no response head came.

    The final response head is rewritten to ``Connection: close`` so a client
    that keeps connections alive opens a new proxy connection for its next
    request instead of reusing this one.
    """

    await _send(upstream[1], head, count_sent)
    upload = asyncio.create_task(_upload(client[0], upstream[1], body, count_sent))
    try:
        try:
            response_head = await upstream[0].readuntil(HEAD_END)
            while _is_interim(response_head):
                await _send(client[1], response_head, count_received)
                response_head = await upstream[0].readuntil(HEAD_END)
        except (EOFError, asyncio.LimitOverrunError):
            return False
        await _send(client[1], _close_response_head(response_head), lambda _n: None)
        count_received(len(response_head))
        while chunk := await upstream[0].read(RELAY_CHUNK_BYTES):
            await _send(client[1], chunk, count_received)
        return True
    finally:
        upload.cancel()
        await asyncio.wait([upload])
