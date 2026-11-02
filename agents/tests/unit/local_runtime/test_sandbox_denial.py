from __future__ import annotations

from pathlib import Path

import pytest

from pantaray_agents.local_runtime.tooling.repository.command_invocation_audits import (
    TerminalOutcome,
)
from pantaray_agents.local_runtime.tooling.sandbox.sandbox_denial import (
    NETWORK_DENIAL_HINT,
    WRITE_FOLDER_REQUEST_HINT,
    is_likely_sandbox_denied,
    sandbox_denial_hint,
)
from pantaray_agents.tools.files import secret_paths

CODEX_EXEC_STDERR = (
    "Error: failed to initialize in-process app-server client: "
    "Operation not permitted (os error 1)\n"
)


def _exited(*, exit_code: int = 1, stdout: str = "", stderr: str = "") -> bool:
    return is_likely_sandbox_denied(
        terminal_outcome="exited", exit_code=exit_code, stdout=stdout, stderr=stderr
    )


@pytest.mark.parametrize(
    ("stdout", "stderr"),
    [
        ("", CODEX_EXEC_STDERR),
        ("touch: out.txt: Permission denied\n", ""),
        ("", "OSError: [Errno 30] Read-only file system: '/x'\n"),
    ],
)
def test_a_failure_that_names_a_denial_is_likely_denied(
    stdout: str, stderr: str
) -> None:
    assert _exited(stdout=stdout, stderr=stderr)


def test_a_success_is_never_denied_whatever_it_printed() -> None:
    assert not _exited(exit_code=0, stderr=CODEX_EXEC_STDERR)


@pytest.mark.parametrize("exit_code", [2, 126, 127])
def test_usage_exec_and_missing_command_exits_are_not_denied_writes(
    exit_code: int,
) -> None:
    assert not _exited(
        exit_code=exit_code, stderr="bash: ./run.sh: Permission denied\n"
    )


@pytest.mark.parametrize(
    "stdout",
    [
        "error: unknown option '--frobnicate'\n",
        # A failing test run whose traceback walks through a sandbox package.
        "agents/src/pantaray_agents/local_runtime/tooling/sandbox/"
        "command_sandbox_client.py:525: AssertionError\n"
        "FAILED tests/unit/local_runtime/test_sandbox_denial.py::test_x\n"
        "1 failed, 4 passed in 0.12s\n",
        "INFO sandbox: worker ready\nERROR job failed: connection reset\n",
    ],
)
def test_a_failure_without_denial_words_is_the_command_s_own(stdout: str) -> None:
    assert not _exited(stdout=stdout)


@pytest.mark.parametrize(
    "terminal_outcome", ["timed_out", "canceled", "signaled", "budget_exceeded"]
)
def test_a_command_the_runtime_stopped_is_not_denied(
    terminal_outcome: TerminalOutcome,
) -> None:
    assert not is_likely_sandbox_denied(
        terminal_outcome=terminal_outcome,
        exit_code=-1,
        stdout="",
        stderr=CODEX_EXEC_STDERR,
    )


def _hint(
    *,
    cwd: Path,
    stderr: str,
    exit_code: int = 1,
    credentials_readable: bool = False,
    network_allowed: bool = False,
) -> str | None:
    return sandbox_denial_hint(
        terminal_outcome="exited",
        exit_code=exit_code,
        stdout="",
        stderr=stderr,
        cwd=cwd,
        credentials_readable=credentials_readable,
        network_allowed=network_allowed,
    )


@pytest.mark.parametrize(
    ("stderr", "exit_code"),
    [
        ("cat: .env: Operation not permitted\n", 1),
        # grep's exit status for an unreadable file, which write folders skip.
        ("grep: ./sub/.ENV.local: Operation not permitted\n", 2),
        ("PermissionError: [Errno 1] Operation not permitted: '.env'\n", 1),
        ("mv: rename .env to x: Operation not permitted\n", 1),
    ],
)
def test_a_denied_secret_file_is_called_withheld_not_a_write_folder(
    tmp_path: Path, stderr: str, exit_code: int
) -> None:
    hint = _hint(cwd=tmp_path, stderr=stderr, exit_code=exit_code)

    assert hint is not None
    assert "withheld" in hint
    assert "additional_write_folders" not in hint


def test_a_denied_ordinary_file_still_asks_for_write_folders(tmp_path: Path) -> None:
    hint = _hint(cwd=tmp_path, stderr="touch: out.env.txt: Operation not permitted\n")

    assert hint == WRITE_FOLDER_REQUEST_HINT


def test_credentials_are_withheld_only_without_the_login_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(secret_paths, "_home", lambda: tmp_path)
    stderr = f"cat: {tmp_path}/.aws/credentials: Operation not permitted\n"

    withheld = _hint(cwd=tmp_path, stderr=stderr)
    readable = _hint(cwd=tmp_path, stderr=stderr, credentials_readable=True)

    assert withheld is not None and "withheld" in withheld
    assert readable == WRITE_FOLDER_REQUEST_HINT


# What clients print, under the network-on profile, for a direct connection the
# sandbox refused (seen with sandbox-exec on macOS 26; Node's from its docs).
@pytest.mark.parametrize(
    "stderr",
    [
        "ssh: connect to host github.com port 22: Operation not permitted\n",
        "nc: connectx to example.com port 80 (tcp) failed: Operation not permitted\n",
        'psql: error: connection to server at "db.example" (192.0.2.1), port 5432 '
        "failed: Operation not permitted\n",
        "fatal: unable to connect to github.com:\n"
        "github.com[0: 20.27.177.113]: errno=Operation not permitted\n",
        "urllib.error.URLError: <urlopen error [Errno 1] Operation not permitted>\n",
        "Error: connect EPERM 192.0.2.1:5432\n",
        ";; connection timed out; no servers could be reached\n",
        "ping: sendto: Operation not permitted\n",
    ],
)
def test_a_refused_direct_connection_points_to_the_proxy(
    tmp_path: Path, stderr: str
) -> None:
    assert _hint(cwd=tmp_path, stderr=stderr, network_allowed=True) == (
        NETWORK_DENIAL_HINT
    )


def test_a_refused_connection_is_not_called_a_network_block_with_network_off(
    tmp_path: Path,
) -> None:
    # With network off the proxy is not there to point to.
    stderr = "ping: sendto: Operation not permitted\n"

    assert _hint(cwd=tmp_path, stderr=stderr) == WRITE_FOLDER_REQUEST_HINT


def test_a_name_that_does_not_resolve_is_not_called_a_network_block(
    tmp_path: Path,
) -> None:
    # Names resolve in the sandbox, so this is the name's own failure.
    stderr = "curl: (6) Could not resolve host: no-such-host.example\n"

    assert _hint(cwd=tmp_path, stderr=stderr, network_allowed=True) is None


def test_a_denied_write_with_network_on_still_asks_for_write_folders(
    tmp_path: Path,
) -> None:
    stderr = "touch: out.txt: Operation not permitted\n"

    assert _hint(cwd=tmp_path, stderr=stderr, network_allowed=True) == (
        WRITE_FOLDER_REQUEST_HINT
    )
