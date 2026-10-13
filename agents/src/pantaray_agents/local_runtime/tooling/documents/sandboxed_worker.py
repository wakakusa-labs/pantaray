"""Start a sandboxed worker script, and always collect it when done with it.

The PDF page renderer and the web page worker are both short-lived scripts that
hold untrusted input: each is started the same way, talks over stdin and
stdout, and must be killed and collected however its exchange with the helper
ends -- with a reply, a broken reply, or a time limit.
"""

from __future__ import annotations

import asyncio
import logging
from asyncio.subprocess import Process
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Final

from .page_render_sandbox import sandboxed_argv

logger = logging.getLogger(__name__)

# Collecting a killed worker takes milliseconds; this bounds it only for a pipe
# some other process still holds open, so it can never stall the caller.
REAP_SECONDS: Final = 5.0
_DRAIN_CHUNK_BYTES: Final = 64 * 1024


@asynccontextmanager
async def sandboxed_worker(
    *,
    script: Path,
    read_files: Sequence[Path],
    python_executable: Path,
    allow_network: bool = False,
) -> AsyncIterator[Process]:
    """Run ``script`` confined to reading itself and ``read_files``.

    The worker is killed if it is still running when the block ends, then
    collected.
    """

    process = await asyncio.create_subprocess_exec(
        *sandboxed_argv(
            # -I so the worker takes nothing from the environment or the
            # directory it sits in, -B so it never tries to write a .pyc into a
            # runtime the sandbox has made read-only.
            (str(python_executable), "-I", "-B", str(script)),
            read_files=(script, *read_files),
            python_executable=python_executable,
            allow_network=allow_network,
        ),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        # A worker says everything it has to say on stdout. Its stderr would
        # only carry a traceback from a process holding untrusted input, and a
        # pipe nobody drains is a way to wedge it.
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        yield process
    finally:
        await _collect(process)


async def _collect(process: Process) -> None:
    """Kill the worker if it still runs, read its stdout to the end, then wait.

    asyncio stops reading a pipe once its unread data passes the buffer limit,
    and reports a process finished only after every pipe has closed. A worker
    killed with its reply still unread -- a time limit that fell mid-reply, a
    reply refused part way -- would never be collected, so what is left of the
    reply is read and discarded first.
    """

    if process.returncode is None:
        process.kill()
    assert process.stdout is not None
    try:
        async with asyncio.timeout(REAP_SECONDS):
            while await process.stdout.read(_DRAIN_CHUNK_BYTES):
                pass
            await process.wait()
    except TimeoutError:
        logger.warning("a worker process was not collected within %g s", REAP_SECONDS)


__all__ = ["REAP_SECONDS", "sandboxed_worker"]
