"""The PATH the user's terminal has, read once from their login shell.

An app opened from the Dock or Finder inherits launchd's minimal PATH, so the
tools the user installed (Homebrew, nvm, pnpm, ~/.local/bin) are only on the
PATH that their shell startup files build.
"""

from __future__ import annotations

import functools
import logging
import os
import pwd
import re
import signal
import subprocess
import tempfile

logger = logging.getLogger(__name__)

# Startup files are arbitrary user code and can hang; a normal interactive
# login shell with nvm measured about 0.9 s, so 5 s leaves room for a slow but
# working setup while a hung one delays only the first command.
LOGIN_SHELL_TIMEOUT_SECONDS = 5.0
_START_MARKER = "__PANTARAY_LOGIN_PATH_START__"
_END_MARKER = "__PANTARAY_LOGIN_PATH_END__"
# Startup files may print anything; only the line between the markers is PATH.
# printenv rather than "$PATH" so shells such as fish print the colon form.
_PRINT_PATH_COMMAND = (
    f"echo {_START_MARKER}; /usr/bin/printenv PATH; echo {_END_MARKER}"
)
_MARKED_PATH = re.compile(f"{_START_MARKER}\n([^\n]*)\n{_END_MARKER}")


def login_shell_path() -> str:
    """The login shell's PATH, or this process's PATH when it cannot be read."""

    path = _read_once()
    return os.environ["PATH"] if path is None else path


@functools.cache
def _read_once() -> str | None:
    # Startup files must not run for every command.
    # Design limit: this blocks the caller (the backend's event loop) once, for
    # about 1 s normally and at most the timeout; move it to a worker thread if
    # that first-command stall becomes visible in the UI.
    shell = os.environ.get("SHELL") or pwd.getpwuid(os.getuid()).pw_shell
    return _read_login_shell_path(
        shell=shell, timeout_seconds=LOGIN_SHELL_TIMEOUT_SECONDS
    )


def _read_login_shell_path(*, shell: str, timeout_seconds: float) -> str | None:
    # A file, not a pipe: a background process a startup file leaves running
    # must not hold the read open after the shell itself has exited.
    with tempfile.TemporaryFile() as output:
        try:
            process = subprocess.Popen(
                [shell, "-ilc", _PRINT_PATH_COMMAND],
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.DEVNULL,
                # No controlling terminal to wait on, and one group to kill.
                start_new_session=True,
            )
        except OSError as exc:
            logger.warning("could not start the login shell for PATH: %s", exc.strerror)
            return None
        try:
            returncode = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            logger.warning(
                "the login shell did not print PATH within %s s", timeout_seconds
            )
            return None
        output.seek(0)
        text = output.read().decode("utf-8", errors="replace")
    if returncode != 0:
        logger.warning("the login shell exited with %s while reading PATH", returncode)
        return None
    match = _MARKED_PATH.search(text)
    if match is None:
        logger.warning("the login shell output had no PATH")
        return None
    return match.group(1)


__all__ = ["login_shell_path"]
