from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from .support import SEATBELT_SKIP_REASON, seatbelt_available

pytestmark = pytest.mark.skipif(not seatbelt_available(), reason=SEATBELT_SKIP_REASON)

AGENTS_ROOT = Path(__file__).resolve().parents[3]
# The broker is the local backend process; it runs as its own interpreter here so
# the test can end it the way the app does (SIGTERM on quit, SIGKILL on a hang).
BROKER_SCRIPT = textwrap.dedent(
    """
    import asyncio
    import sys
    from pathlib import Path

    import pytest
    from tests.integration.local_runtime.support import (
        bootstrap_runtime_testbed,
        execute_bash,
    )

    testbed = bootstrap_runtime_testbed(
        tmp_path=Path(sys.argv[1]), monkeypatch=pytest.MonkeyPatch()
    )
    asyncio.run(execute_bash(testbed=testbed, command="sleep 600"))
    """
)
STARTUP_TIMEOUT_SECONDS = 30.0
GROUP_EXIT_TIMEOUT_SECONDS = 5.0
POLL_INTERVAL_SECONDS = 0.05


def _registered_process_group(db_path: Path) -> tuple[int, int] | None:
    if not db_path.exists():
        return None
    try:
        with sqlite3.connect(db_path) as connection:
            row = connection.execute(
                "SELECT pid, pgid FROM tool_runtime_resources"
                " WHERE resource_kind = 'process_group' AND status = 'active'"
            ).fetchone()
    except sqlite3.OperationalError:
        return None  # The broker is still applying migrations.
    return None if row is None else (int(row[0]), int(row[1]))


def _children(pid: int) -> list[int]:
    found = subprocess.run(
        ["pgrep", "-P", str(pid)], capture_output=True, text=True
    ).stdout.split()
    return [int(child) for child in found]


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # macOS answers EPERM for a group left with only zombies, or whose id now
        # belongs to another user's process; either way the command is gone.
        return False
    return True


def _wait_for_running_command(
    broker: subprocess.Popen[bytes], db_path: Path
) -> tuple[int, int]:
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        assert broker.poll() is None, broker.stderr.read() if broker.stderr else ""
        registered = _registered_process_group(db_path)
        if registered is not None and _children(registered[0]):
            return registered
        time.sleep(POLL_INTERVAL_SECONDS)
    raise AssertionError("the sandboxed command never started")


@pytest.mark.parametrize("broker_signal", [signal.SIGTERM, signal.SIGKILL])
def test_command_dies_with_its_broker(
    tmp_path: Path, broker_signal: signal.Signals
) -> None:
    db_path = tmp_path / "runtime.db"
    broker = subprocess.Popen(
        [sys.executable, "-c", BROKER_SCRIPT, str(tmp_path)],
        cwd=AGENTS_ROOT,
        env={**os.environ, "PYTHONPATH": f"{AGENTS_ROOT}:{AGENTS_ROOT / 'src'}"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    pgid: int | None = None
    try:
        worker_pid, pgid = _wait_for_running_command(broker, db_path)
        assert pgid == worker_pid
        broker.send_signal(broker_signal)
        broker.wait(timeout=GROUP_EXIT_TIMEOUT_SECONDS)

        deadline = time.monotonic() + GROUP_EXIT_TIMEOUT_SECONDS
        while _group_alive(pgid) and time.monotonic() < deadline:
            time.sleep(POLL_INTERVAL_SECONDS)
        assert not _group_alive(pgid), "the command outlived its broker"
    finally:
        if broker.poll() is None:
            broker.kill()
            broker.wait()
        if pgid is not None and _group_alive(pgid):
            os.killpg(pgid, signal.SIGKILL)
