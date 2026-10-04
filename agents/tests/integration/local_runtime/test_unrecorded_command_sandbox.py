from __future__ import annotations

import asyncio
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

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


def _running(marker: str) -> bool:
    return subprocess.run(["pgrep", "-f", marker], capture_output=True).returncode == 0


@pytest.mark.asyncio
async def test_cancelling_an_unrecorded_command_kills_it_and_removes_its_temp_dir(
    tmp_path: Path,
) -> None:
    marker = f"pantaray-unrecorded-probe-{uuid.uuid4().hex}"
    temp_dirs: list[Path] = []

    def build_request(temp_dir: Path) -> BrokerToSandboxCommandRequest:
        temp_dirs.append(temp_dir)
        return compose_sandbox_request(
            temp_dir=temp_dir,
            real_read_roots=[],
            real_write_roots=[],
            private_storage_roots=[str(tmp_path)],
            action_storage=None,
            app_runtime_python=Path(sys.executable),
            cwd=str(temp_dir),
            argv=[
                "/bin/bash",
                "--noprofile",
                "--norc",
                "-c",
                f"exec -a {marker} sleep 60",
            ],
            env={"PATH": "/usr/bin:/bin"},
            timeout_ms=60_000,
            stdout_max_bytes=65_536,
            stderr_max_bytes=65_536,
            temp_storage_limit_bytes=1_048_576,
            network_policy="deny",
            use_login_environment=False,
            run_outside_sandbox=False,
        )

    task = asyncio.create_task(
        run_unrecorded_sandbox_command(
            db_path=tmp_path / "runtime.db", build_request=build_request
        )
    )
    async with asyncio.timeout(10):
        while not _running(marker):
            await asyncio.sleep(0.1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not _running(marker)
    assert not temp_dirs[0].exists()
