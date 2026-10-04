from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from pantaray_agents.agents.artifact_react import ReactToolCall, ToolCallEnvelope
from pantaray_agents.local_runtime.tooling.repository.command_network_settings import (
    update_command_network_enabled,
)
from pantaray_agents.local_runtime.tooling.suggestion_research.commands import (
    SuggestionCommandSession,
)
from pantaray_agents.schema.read_access import ReadAccessScope

from .support import (
    INTEGRATION_APPROVAL_TIMESTAMP,
    ONE_SECOND_MS,
    SEATBELT_SKIP_REASON,
    RuntimeIntegrationTestbed,
    bootstrap_runtime_testbed,
    seatbelt_available,
    start_tcp_probe_server,
)

pytestmark = pytest.mark.skipif(not seatbelt_available(), reason=SEATBELT_SKIP_REASON)


def _call(command: str, *, cwd: Path | None = None, login: bool = False):
    args = {
        "command": command,
        "cwd": None if cwd is None else str(cwd),
        "use_login_environment": login,
    }
    return ReactToolCall(
        tool_name="bash",
        tool_args=args,  # type: ignore[arg-type]
        tool_call_envelope=ToolCallEnvelope(tool_id="bash", reason=None, args=args),  # type: ignore[arg-type]
    )


def _session(
    testbed: RuntimeIntegrationTestbed,
    workspace: Path,
    scope: ReadAccessScope = "workspace",
) -> SuggestionCommandSession:
    return SuggestionCommandSession(
        db_path=testbed.db_path,
        busy_timeout_ms=ONE_SECOND_MS,
        user_id="user-1",
        workspace_roots=(workspace,),
        read_access_scope=scope,
    )


@pytest.fixture
def workspace(tmp_path_factory: pytest.TempPathFactory) -> Path:
    # Outside the database's folder, which the sandbox keeps private.
    root = tmp_path_factory.mktemp("suggestion-workspace").resolve()
    for args in (
        ["init", "-q"],
        ["-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q"]
        + ["--allow-empty", "-m", "first commit"],
    ):
        subprocess.run(["git", "-C", str(root), *args], check=True)
    return root


@pytest.mark.asyncio
async def test_a_read_command_sees_the_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workspace: Path
) -> None:
    testbed = bootstrap_runtime_testbed(tmp_path=tmp_path, monkeypatch=monkeypatch)

    result = await _session(testbed, workspace).run(
        _call(f"git -C {workspace} log -1 --format=%s"), 1
    )

    assert result.status == "success", result.output
    assert result.output["stdout"] == "first commit\n"


@pytest.mark.parametrize("target", ["workspace", "cwd", "tmpdir", "home"])
@pytest.mark.asyncio
async def test_every_write_is_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workspace: Path, target: str
) -> None:
    testbed = bootstrap_runtime_testbed(tmp_path=tmp_path, monkeypatch=monkeypatch)
    path = {
        "workspace": str(workspace / "created.txt"),
        "cwd": "created.txt",
        "tmpdir": '"$TMPDIR/created.txt"',
        "home": '"$HOME/created.txt"',
    }[target]

    result = await _session(testbed, workspace).run(
        _call(f"touch {path}", cwd=workspace), 1
    )

    assert result.output["exit_code"] != 0
    assert "Operation not permitted" in result.output["stderr"]
    # A suggestion command cannot ask for write folders, so it is not told to.
    assert "additional_write_folders" not in json.dumps(result.output)
    assert not (workspace / "created.txt").exists()


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.asyncio
async def test_the_network_follows_the_command_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workspace: Path, enabled: bool
) -> None:
    testbed = bootstrap_runtime_testbed(tmp_path=tmp_path, monkeypatch=monkeypatch)
    update_command_network_enabled(
        db_path=testbed.db_path,
        busy_timeout_ms=ONE_SECOND_MS,
        user_id="user-1",
        command_network_enabled=enabled,
        now=INTEGRATION_APPROVAL_TIMESTAMP,
    )
    server = start_tcp_probe_server()
    try:
        result = await _session(testbed, workspace).run(
            _call(f"echo hi > /dev/tcp/127.0.0.1/{server.port}"), 1
        )
    finally:
        server.close()

    assert (result.output["exit_code"] == 0) is enabled, result.output


@pytest.mark.asyncio
async def test_full_read_access_still_hides_pantaray_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workspace: Path
) -> None:
    testbed = bootstrap_runtime_testbed(
        tmp_path=tmp_path, monkeypatch=monkeypatch, read_access_scope="full_access"
    )

    result = await _session(testbed, workspace, scope="full_access").run(
        _call(f"head -c 16 {testbed.db_path}", login=True), 1
    )

    assert result.output["exit_code"] != 0
    assert "Operation not permitted" in result.output["stderr"]
