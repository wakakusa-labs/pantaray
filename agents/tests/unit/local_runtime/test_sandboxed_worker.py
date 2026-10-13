"""A sandboxed worker is collected however its exchange ends, within a bound."""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.tooling.documents import sandboxed_worker as module
from pantaray_agents.local_runtime.tooling.documents.sandboxed_worker import (
    sandboxed_worker,
)

_FLOOD = "import sys\nwhile True:\n    sys.stdout.buffer.write(b'x' * 65536)\n"
# The grandchild inherits stdout and outlives the worker, so the pipe never
# reaches its end however much of it is read.
_HOLD_PIPE = (
    "import subprocess, sys\n"
    "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(5)'])\n"
    "sys.stdout.write('started')\n"
    "sys.stdout.flush()\n"
    "import time; time.sleep(5)\n"
)


@pytest.mark.asyncio
async def test_a_worker_killed_with_its_output_unread_is_collected(
    tmp_path: Path,
) -> None:
    script = tmp_path / "flood.py"
    script.write_text(_FLOOD)

    started = time.monotonic()
    async with sandboxed_worker(
        script=script, read_files=(), python_executable=Path(sys.executable)
    ) as process:
        # Nothing is read: the pipe fills and asyncio stops reading it.
        await asyncio.sleep(0.5)

    assert time.monotonic() - started < module.REAP_SECONDS
    assert process.returncode is not None


@pytest.mark.asyncio
async def test_collecting_a_worker_never_outlasts_its_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(module, "REAP_SECONDS", 0.5)
    script = tmp_path / "hold.py"
    script.write_text(_HOLD_PIPE)

    async with sandboxed_worker(
        script=script, read_files=(), python_executable=Path(sys.executable)
    ) as process:
        assert process.stdout is not None
        await process.stdout.readexactly(len("started"))
        started = time.monotonic()

    assert time.monotonic() - started < 3
