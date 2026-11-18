from __future__ import annotations

import shlex
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
@pytest.mark.parametrize("exists", [True, False])
async def test_a_command_with_the_whole_home_writable_cannot_change_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exists: bool
) -> None:
    home = tmp_path.resolve() / "home"
    user_data = home / "Library" / "Application Support" / "Pantaray"
    db_path = user_data / "local-backend.sqlite3"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv(
        "LOCAL_ARTIFACT_ROOT", str(user_data / "local-backend-artifacts")
    )
    for root in app_owned_roots(db_path):
        root.mkdir(parents=True)
    folder = home / ".pantaray"
    folder.mkdir()
    agents_md = folder / "AGENTS.md"
    if exists:
        agents_md.write_text("Be brief.\n", encoding="utf-8")

    q = shlex.quote
    attacks = "\n".join(
        (
            f"echo rule >> {q(str(agents_md))} 2>/dev/null || echo write-denied",
            f"echo rule > {q(str(home / '.PANTARAY' / 'agents.md'))} 2>/dev/null"
            " || echo alias-denied",
            f"ln -s {q(str(agents_md))} {q(str(home / 'link'))}"
            f" && {{ echo rule > {q(str(home / 'link'))} 2>/dev/null"
            " || echo link-denied; }",
            f"rm -f {q(str(agents_md))} 2>/dev/null; test -e {q(str(agents_md))}"
            " && echo present",
            f"mv {q(str(folder))} {q(str(home / 'moved'))} 2>/dev/null"
            " || echo move-denied",
            f"echo x > {q(str(folder / 'notes.md'))} && echo sibling-ok",
            "",
        )
    )

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

    _, output = await run_unrecorded_sandbox_command(
        db_path=db_path, build_request=build_request
    )

    present = "present\n" if exists else ""
    assert output.stdout == (
        f"write-denied\nalias-denied\nlink-denied\n{present}move-denied\nsibling-ok\n"
    ), output
    if exists:
        assert agents_md.read_text(encoding="utf-8") == "Be brief.\n"
    else:
        assert not agents_md.exists()
