from __future__ import annotations

import os
from pathlib import Path

from .broker_protocol import BrokerExecutionKind
from .login_shell_path import login_shell_path

PATH_ENV_ALLOWLIST = ("LANG", "LC_ALL")
LOGIN_ENV_ALLOWLIST = ("USER", "LOGNAME", "SSH_AUTH_SOCK")


def build_command_env(
    *,
    command_cwd: Path,
    execution_kind: BrokerExecutionKind,
    workspace_root: Path,
    resolved_executable: Path,
    use_login_environment: bool,
    full_disk_read: bool,
) -> dict[str, str]:
    env: dict[str, str] = {}
    allowlist = (
        (*PATH_ENV_ALLOWLIST, *LOGIN_ENV_ALLOWLIST)
        if use_login_environment
        else PATH_ENV_ALLOWLIST
    )
    for key in allowlist:
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    env["PWD"] = str(command_cwd)
    # Git's system config can live in a Homebrew etc/ directory outside the
    # sandbox's read-only toolchain roots. Only a whole-filesystem read makes
    # the host Git configuration readable, as it is in the user's terminal.
    if not full_disk_read:
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        if use_login_environment:
            # Git aborts every command when the real HOME's config exists but
            # the sandbox denies reading it, instead of skipping the file.
            env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    path_entries = [str(resolved_executable.parent)]
    if execution_kind == "workspace_command":
        for current in (command_cwd, *command_cwd.parents):
            if not current.is_relative_to(workspace_root):
                break
            for relative in (".venv/bin", "node_modules/.bin"):
                candidate = (current / relative).resolve()
                if candidate.is_dir() and candidate.is_relative_to(workspace_root):
                    path_entries.append(str(candidate))
    env["PATH"] = os.pathsep.join(
        dict.fromkeys([*path_entries, *login_shell_path().split(os.pathsep)])
    )
    return env


__all__ = [
    "build_command_env",
]
