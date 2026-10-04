from __future__ import annotations

from pathlib import Path

import pytest

from pantaray_agents.local_runtime.tooling.brokering import (
    command_runtime,
    login_shell_path,
)
from pantaray_agents.schema.read_access import ReadAccessScope

from .support import (
    SEATBELT_SKIP_REASON,
    bootstrap_runtime_testbed,
    execute_bash,
    seatbelt_available,
)

pytestmark = pytest.mark.skipif(not seatbelt_available(), reason=SEATBELT_SKIP_REASON)

# What an app opened from the Dock or Finder inherits from launchd.
LAUNCHD_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


@pytest.mark.asyncio
@pytest.mark.parametrize("read_access_scope", ["workspace", "full_access"])
async def test_commands_find_tools_from_the_login_shell_path(
    tmp_path: Path,
    outside_temp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    read_access_scope: ReadAccessScope,
) -> None:
    tools = outside_temp_path
    probe = tools / "pantaray-probe"
    probe.write_text("#!/bin/sh\necho probe ok\n", encoding="utf-8")
    probe.chmod(0o755)
    shell = tools / "login-shell"
    shell.write_text(
        f'#!/bin/sh\necho banner\nexport PATH={tools}:{LAUNCHD_PATH}\n/bin/sh -c "$2"\n',
        encoding="utf-8",
    )
    shell.chmod(0o755)
    testbed = bootstrap_runtime_testbed(
        tmp_path=tmp_path, monkeypatch=monkeypatch, read_access_scope=read_access_scope
    )
    monkeypatch.setenv("PATH", LAUNCHD_PATH)
    monkeypatch.setenv("SHELL", str(shell))
    monkeypatch.setattr(
        command_runtime, "login_shell_path", login_shell_path.login_shell_path
    )
    login_shell_path._read_once.cache_clear()
    try:
        outcome = await execute_bash(
            testbed=testbed, command="pantaray-probe || echo probe missing; ls -d /"
        )
    finally:
        login_shell_path._read_once.cache_clear()
    assert outcome.status == "success", outcome.output
    stdout = outcome.output["stdout"]
    if read_access_scope == "full_access":
        assert "probe ok" in stdout
    else:
        # The tools directory stays on PATH, but the sandbox cannot read it, so
        # the lookup skips it and the system directories still work.
        assert "probe missing" in stdout
    assert stdout.rstrip().endswith("/")
