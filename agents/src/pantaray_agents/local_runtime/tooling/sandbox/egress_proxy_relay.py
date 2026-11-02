"""Byte relay of the egress proxy once a destination is connected.

The relay has no idle timeout: a long download or a quiet tunnel stays open
until either side closes it or the command's lease ends.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

RELAY_CHUNK_BYTES = 64 * 1024

type CountBytes = Callable[[int], None]


async def _send(writer: asyncio.StreamWriter, data: bytes, count: CountBytes) -> None:
    writer.write(data)
    count(len(data))
    await writer.drain()


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
    for direction in done:
        error = direction.exception()
        # A reset or broken pipe ends the tunnel; anything else is a defect.
        if error is not None and not isinstance(error, OSError):
            raise error
