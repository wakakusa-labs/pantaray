from __future__ import annotations

from pathlib import Path

import pytest
from tests.unit.local_runtime.test_seatbelt_profiles import _validated_command_request

from pantaray_agents.local_runtime.tooling.brokering.broker_protocol import (
    BrokerExecutionKind,
)
from pantaray_agents.local_runtime.tooling.brokering.command_runtime import (
    build_command_env,
)
from pantaray_agents.local_runtime.tooling.sandbox.command_sandbox_request import (
    build_sandbox_request,
)
from pantaray_agents.local_runtime.tooling.sandbox.macos_runtime import (
    app_python_runtime_root,
)


@pytest.mark.parametrize("kind", ["agent_generated", "workspace_command"])
def test_command_environment_keeps_trusted_path_and_isolates_cache_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: BrokerExecutionKind
) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("PANTARAY_SYNTHETIC_SECRET", "not-for-the-child")
    monkeypatch.setenv("USER", "someone")
    monkeypatch.setenv("HOME", str(tmp_path / "user-home"))
    monkeypatch.setenv("RUSTUP_HOME", str(tmp_path / "installed-rust"))
    env = build_command_env(
        command_cwd=tmp_path,
        execution_kind=kind,
        workspace_root=tmp_path,
        resolved_executable=tmp_path / ".venv/bin/python",
        use_login_environment=False,
        full_disk_read=False,
    )
    assert env["PATH"].endswith("/usr/bin:/bin")
    assert "PANTARAY_SYNTHETIC_SECRET" not in env
    assert "USER" not in env
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    temp_dir = tmp_path / "invocation"
    request = build_sandbox_request(
        request=_validated_command_request(network_policy="deny").model_copy(
            update={"env": env}
        ),
        temp_dir=temp_dir,
    )
    assert request.env["HOME"] == request.env["TMPDIR"] == str(temp_dir)
    assert request.env["RUSTUP_HOME"] == str(tmp_path / "installed-rust")


@pytest.mark.parametrize("full_disk_read", [False, True])
def test_login_environment_adds_identity_and_real_home_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, full_disk_read: bool
) -> None:
    home = tmp_path / "user-home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USER", "someone")
    monkeypatch.setenv("LOGNAME", "someone")
    monkeypatch.setenv("SSH_AUTH_SOCK", str(tmp_path / "agent.sock"))
    monkeypatch.setenv("PANTARAY_SYNTHETIC_SECRET", "not-for-the-child")
    env = build_command_env(
        command_cwd=tmp_path,
        execution_kind="workspace_command",
        workspace_root=tmp_path,
        resolved_executable=Path("/bin/bash"),
        use_login_environment=True,
        full_disk_read=full_disk_read,
    )
    assert env["USER"] == env["LOGNAME"] == "someone"
    assert env["SSH_AUTH_SOCK"] == str(tmp_path / "agent.sock")
    assert "PANTARAY_SYNTHETIC_SECRET" not in env
    if full_disk_read:
        # The host Git configuration is readable, as in the user's terminal.
        assert "GIT_CONFIG_NOSYSTEM" not in env
        assert "GIT_CONFIG_GLOBAL" not in env
    else:
        # Git aborts on an existing but unreadable ~/.gitconfig.
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    temp_dir = tmp_path / "invocation"
    request = build_sandbox_request(
        request=_validated_command_request(network_policy="deny").model_copy(
            update={"env": env, "use_login_environment": True}
        ),
        temp_dir=temp_dir,
    )
    assert request.env["HOME"] == str(home)
    assert request.env["TMPDIR"] == str(temp_dir)
    assert request.use_login_environment


@pytest.mark.parametrize(
    ("executable", "expected"),
    [
        ("runtime/bin/python3", "runtime"),
        ("Python.framework/Versions/3.12/bin/python3", "Python.framework"),
    ],
)
def test_app_python_includes_libraries_outside_bin(
    tmp_path: Path, executable: str, expected: str
) -> None:
    python = tmp_path / executable
    python.parent.mkdir(parents=True)
    python.touch()
    alias = tmp_path / "python-link"
    alias.symlink_to(python)
    assert app_python_runtime_root(alias) == tmp_path / expected


def test_shell_path_prefers_nearest_repo_environment_and_stops_at_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    project = root / "agents"
    cwd = project / "tests"
    cwd.mkdir(parents=True)
    nearest_env = project / ".venv/bin"
    outer_env = root / ".venv/bin"
    outside_env = tmp_path / "outside/bin"
    for directory in (nearest_env, outer_env, outside_env):
        directory.mkdir(parents=True)
    (cwd / ".venv").symlink_to(outside_env.parent, target_is_directory=True)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    env = build_command_env(
        command_cwd=cwd,
        execution_kind="workspace_command",
        workspace_root=root,
        resolved_executable=Path("/bin/bash"),
        use_login_environment=False,
        full_disk_read=False,
    )
    entries = env["PATH"].split(":")
    assert entries.index(str(nearest_env)) < entries.index(str(outer_env))
    assert str(outside_env) not in entries
    assert entries == [
        "/bin",
        str(nearest_env.resolve()),
        str(outer_env.resolve()),
        "/usr/bin",
    ]
