from __future__ import annotations

import shlex
import shutil
import uuid
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.tooling.action_session_temp_paths import (
    create_private_temp_dir,
)
from pantaray_agents.local_runtime.tooling.sandbox.command_sandbox_client import (
    SANDBOX_TEMP_DIR_PREFIX,
)
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


@pytest.mark.asyncio
async def test_unquoted_tmpdir_works_and_other_commands_temp_dirs_stay_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The app's data dir is ~/Library/Application Support/pantaray.
    storage = tmp_path / "Application Support"
    storage.mkdir()
    # With full disk read, only the private-storage deny keeps these closed.
    testbed = bootstrap_runtime_testbed(
        tmp_path=storage, monkeypatch=monkeypatch, read_access_scope="full_access"
    )
    # Another command of this profile, and one of another profile (the
    # installed app next to a dev build).
    others = [
        create_private_temp_dir(db_path=db_path, prefix=SANDBOX_TEMP_DIR_PREFIX)
        for db_path in (testbed.db_path, tmp_path / "other-profile" / "runtime.db")
    ]
    probes = []
    for other in others:
        (other / "secret").write_text("private", encoding="utf-8")
        secret = shlex.quote(str(other / "secret"))
        probes.append(
            f"cat {secret} 2>/dev/null || echo read-denied\n"
            f"{{ echo changed > {secret}; }} 2>/dev/null || echo write-denied\n"
        )
    root = shlex.quote(str(others[0].parent))
    try:
        outcome = await execute_bash(
            testbed=testbed,
            command=(
                "echo hi > $TMPDIR/x.log && cat $TMPDIR/x.log\n"
                + "".join(probes)
                # Moving a root would carry its dirs out from under the deny.
                + f"mv {root} {root}-moved 2>/dev/null || echo move-denied\n"
            ),
        )
        assert outcome.status == "success", outcome.output
        assert outcome.output["stdout"] == (
            "hi\n" + "read-denied\nwrite-denied\n" * 2 + "move-denied\n"
        )
        for other in others:
            assert (other / "secret").read_text(encoding="utf-8") == "private"
    finally:
        for other in others:
            shutil.rmtree(other)
