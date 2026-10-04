from __future__ import annotations

import os
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.tooling.brokering import (
    command_runtime,
    login_shell_path,
)
from pantaray_agents.local_runtime.tooling.brokering.command_runtime import (
    build_command_env,
)

APP_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


@pytest.fixture(autouse=True)
def fresh_login_path(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("PATH", APP_PATH)
    login_shell_path._read_once.cache_clear()
    yield
    login_shell_path._read_once.cache_clear()


def _fake_shell(tmp_path: Path, body: str) -> Path:
    # Called as `<shell> -ilc <command>`, like a real login shell.
    shell = tmp_path / "fake-shell"
    shell.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    shell.chmod(0o755)
    return shell


def _build_env(workspace: Path, *, full_disk_read: bool) -> dict[str, str]:
    return build_command_env(
        command_cwd=workspace,
        execution_kind="workspace_command",
        workspace_root=workspace,
        resolved_executable=Path("/bin/bash"),
        use_login_environment=False,
        full_disk_read=full_disk_read,
    )


def test_commands_get_only_the_login_shell_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shell = _fake_shell(
        tmp_path,
        # Startup files that prompt, print banners, and export other secrets.
        "read answer\n"
        "echo 'Welcome back'\n"
        "printf 'no newline before the command'\n"
        "export PANTARAY_SYNTHETIC_SECRET=from-the-rc-file\n"
        "export PATH=/login/bin:/usr/bin:/bin\n"
        '/bin/sh -c "$2"\n'
        "echo 'goodbye'",
    )
    monkeypatch.setenv("SHELL", str(shell))
    monkeypatch.setattr(
        command_runtime, "login_shell_path", login_shell_path.login_shell_path
    )
    env = _build_env(tmp_path.resolve(), full_disk_read=True)
    assert env["PATH"] == "/bin:/login/bin:/usr/bin"
    assert "PANTARAY_SYNTHETIC_SECRET" not in env
    assert all("from-the-rc-file" not in value for value in env.values())


def test_startup_files_run_once_per_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = tmp_path / "runs"
    shell = _fake_shell(
        tmp_path,
        f'echo run >> {runs}\nexport PATH=/login/bin\n/bin/sh -c "$2"',
    )
    monkeypatch.setenv("SHELL", str(shell))
    assert login_shell_path.login_shell_path() == "/login/bin"
    assert login_shell_path.login_shell_path() == "/login/bin"
    assert runs.read_text().splitlines() == ["run"]


def test_hanging_startup_file_is_killed_and_the_app_path_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child_pid = tmp_path / "child.pid"
    shell = _fake_shell(tmp_path, f"sleep 30 &\necho $! > {child_pid}\nwait")
    monkeypatch.setenv("SHELL", str(shell))
    # Long enough for a loaded machine to start the shell and record its child
    # before the timeout fires; 0.5 s was not.
    monkeypatch.setattr(login_shell_path, "LOGIN_SHELL_TIMEOUT_SECONDS", 3.0)
    started = time.monotonic()
    assert login_shell_path.login_shell_path() == APP_PATH
    assert time.monotonic() - started < 10
    # The whole group dies, not only the shell: a leftover child would keep
    # running whatever the startup file was waiting on.
    pid = int(child_pid.read_text())
    deadline = time.monotonic() + 5
    while _is_running(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _is_running(pid)


def test_failing_login_shell_keeps_the_app_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SHELL", str(_fake_shell(tmp_path, "exit 3")))
    assert login_shell_path.login_shell_path() == APP_PATH


def _is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True
