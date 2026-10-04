from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from pantaray_agents.schema.read_access import ReadAccessScope

from .support import (
    SEATBELT_SKIP_REASON,
    bootstrap_runtime_testbed,
    execute_bash,
    seatbelt_available,
)

pytestmark = pytest.mark.skipif(not seatbelt_available(), reason=SEATBELT_SKIP_REASON)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("read_access_scope", "use_login_environment"),
    [
        ("full_access", False),
        ("full_access", True),
        ("workspace", False),
        ("workspace", True),
    ],
)
async def test_command_reads_follow_setting_and_keep_boundaries(
    tmp_path: Path,
    outside_temp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    read_access_scope: ReadAccessScope,
    use_login_environment: bool,
) -> None:
    home_readable = read_access_scope == "full_access"
    # Only the login environment points HOME at the real one, so only then does
    # Git pick up the user's ~/.gitconfig.
    git_user_visible = home_readable and use_login_environment
    app_data = tmp_path / "app-data"
    app_data.mkdir()
    testbed = bootstrap_runtime_testbed(
        tmp_path=app_data,
        monkeypatch=monkeypatch,
        read_access_scope=read_access_scope,
    )
    home = outside_temp_path / "home"
    (home / ".config").mkdir(parents=True)
    (home / ".config" / "cli-token").write_text("signed-in", encoding="utf-8")
    (home / ".gitconfig").write_text("[user]\n\tname = Login User\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    secret = app_data / "credentials.json"
    secret.write_text("private", encoding="utf-8")
    probes = {
        "home": f"cat {shlex.quote(str(home / '.config' / 'cli-token'))}",
        "private": f"cat {shlex.quote(str(secret))}",
        "db": f"cat {shlex.quote(str(testbed.db_path))}",
        "home-write": f": > {shlex.quote(str(home / 'written'))}",
        "workspace-write": ": > written",
        "git": "git init -q",
    }
    command = "\n".join(
        f"if ({probe}) >/dev/null 2>&1; then echo {name}=yes; else echo {name}=no; fi"
        for name, probe in probes.items()
    )
    command += "\ngit config --get user.name 2>/dev/null || true"

    outcome = await execute_bash(
        testbed=testbed,
        command=command,
        use_login_environment=use_login_environment,
    )

    assert outcome.status == "success", outcome.output
    lines = outcome.output["stdout"].splitlines()
    assert f"home={'yes' if home_readable else 'no'}" in lines
    assert "private=no" in lines
    assert "db=no" in lines
    assert "home-write=no" in lines
    assert "workspace-write=yes" in lines
    # An existing but unreadable ~/.gitconfig must not make Git abort.
    assert "git=yes" in lines
    assert ("Login User" in lines) == git_user_visible
    assert not (home / "written").exists()
    assert secret.read_text(encoding="utf-8") == "private"


@pytest.mark.asyncio
@pytest.mark.parametrize("use_login_environment", [False, True])
async def test_keychain_is_reachable_only_with_login_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    use_login_environment: bool,
) -> None:
    testbed = bootstrap_runtime_testbed(tmp_path=tmp_path, monkeypatch=monkeypatch)

    outcome = await execute_bash(
        testbed=testbed,
        command="/usr/bin/security list-keychains",
        use_login_environment=use_login_environment,
    )

    assert (outcome.status == "success") == use_login_environment, outcome.output
