from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.tooling.outside_workspace_grant import (
    app_owned_roots,
)
from pantaray_agents.local_runtime.tooling.sandbox.command_sandbox_protocol import (
    BrokerToSandboxCommandRequest,
)
from pantaray_agents.local_runtime.tooling.sandbox.command_sandbox_request import (
    compose_sandbox_request,
)
from pantaray_agents.local_runtime.tooling.sandbox.unrecorded_command import (
    run_unrecorded_sandbox_command,
)

from .support import SEATBELT_SKIP_REASON, seatbelt_available

pytestmark = pytest.mark.skipif(not seatbelt_available(), reason=SEATBELT_SKIP_REASON)


@pytest.mark.asyncio
async def test_workspace_enclosing_private_storage_cannot_move_its_ancestors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The app's real layout, under a stand-in home the user registered whole.
    home = tmp_path.resolve() / "home"
    user_data = home / "Library" / "Application Support" / "Pantaray"
    db_path = user_data / "local-backend.sqlite3"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv(
        "LOCAL_ARTIFACT_ROOT", str(user_data / "local-backend-artifacts")
    )
    for root in app_owned_roots(db_path):
        root.mkdir(parents=True)
        (root / "secret").write_text("private", encoding="utf-8")
    ancestors = [
        home / "Library" / "Application Support",
        home / "Library" / "Caches",
        home / "Library",
        home,
    ]
    for ancestor in ancestors:
        # macOS guards ~ and ~/Library like this, so an attack strips it first.
        subprocess.run(
            ["/bin/chmod", "+a", "group:everyone deny delete", ancestor], check=True
        )

    attacks = "".join(
        f"chmod -N {a} 2>/dev/null || echo acl-denied\n"
        f"mv {a} {a}.moved 2>/dev/null || echo move-denied\n"
        f"chmod 777 {a} 2>/dev/null || echo mode-denied\n"
        f"mkdir {a}/sib && echo x > {a}/sib/f && mv {a}/sib {a}/sib2 "
        f"&& echo y > {a}/f && mv {a}/f {a}/f2 && rm -r {a}/sib2 {a}/f2 "
        "&& echo siblings-ok\n"
        for a in map(shlex.quote, map(str, ancestors))
    )
    # Deleting the folder above the app data folder stops at the data folder.
    support = shlex.quote(str(ancestors[0]))
    attacks += f"rm -rf {support} 2>/dev/null || echo remove-denied\n"

    def build_request(temp_dir: Path) -> BrokerToSandboxCommandRequest:
        return compose_sandbox_request(
            temp_dir=temp_dir,
            real_read_roots=[str(home)],
            real_write_roots=[str(home)],
            private_storage_roots=[str(root) for root in app_owned_roots(db_path)],
            action_storage=None,
            app_runtime_python=Path(sys.executable),
            cwd=str(home),
            argv=["/bin/bash", "--noprofile", "--norc", "-c", attacks],
            env={"PATH": "/usr/bin:/bin"},
            timeout_ms=30_000,
            stdout_max_bytes=65_536,
            stderr_max_bytes=65_536,
            temp_storage_limit_bytes=1_048_576,
            network_policy="deny",
            use_login_environment=False,
            run_outside_sandbox=False,
        )

    try:
        _, output = await run_unrecorded_sandbox_command(
            db_path=db_path, build_request=build_request
        )
    finally:
        # pytest could not remove the folders through their ACL.
        subprocess.run(["/bin/chmod", "-R", "-N", tmp_path], check=True)

    assert output.stdout == (
        "acl-denied\nmove-denied\nmode-denied\nsiblings-ok\n" * len(ancestors)
        + "remove-denied\n"
    ), output
    for root in app_owned_roots(db_path):
        assert (root / "secret").read_text(encoding="utf-8") == "private"
