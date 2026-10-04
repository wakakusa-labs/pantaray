from __future__ import annotations

import shlex
import uuid
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.tooling.sandbox.macos_runtime import user_temp_dir

from .support import (
    SEATBELT_SKIP_REASON,
    bootstrap_runtime_testbed,
    execute_bash,
    seatbelt_available,
)

pytestmark = pytest.mark.skipif(not seatbelt_available(), reason=SEATBELT_SKIP_REASON)


@pytest.mark.asyncio
async def test_command_writes_system_temp_but_not_home_or_private_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The app's private storage sits under a temp root the command may write.
    assert tmp_path.is_relative_to(user_temp_dir())
    testbed = bootstrap_runtime_testbed(tmp_path=tmp_path, monkeypatch=monkeypatch)
    private_file = tmp_path / "private.txt"
    home_file = Path.home() / f".pantaray-sandbox-probe-{uuid.uuid4().hex}"
    outcome = await execute_bash(
        testbed=testbed,
        command=f"""
/usr/bin/git -c init.defaultBranch=main init -q && /usr/bin/git status --short
scratch=$(mktemp -d /tmp/pantaray-probe.XXXXXX) && rmdir "$scratch" && echo tmp
touch {shlex.quote(str(home_file))} 2>/dev/null || echo home-denied
touch {shlex.quote(str(private_file))} 2>/dev/null || echo private-denied
""",
    )
    assert outcome.status == "success", outcome.output
    # xcrun behind /usr/bin/git reported its unwritable cache here.
    assert outcome.output["stderr"] == ""
    assert outcome.output["stdout"] == "tmp\nhome-denied\nprivate-denied\n"
    assert not home_file.exists()
    assert not private_file.exists()
