"""Spot a command failure the sandbox likely caused, the way Codex does.

Adapted from Codex `is_likely_sandbox_denied` (codex-rs/sandboxing/src/denial.rs).
A denied write fails like any other error, often in words that do not name the
path (`codex exec`: "Operation not permitted"), so the model cannot tell it apart
from the command's own failure without a hint. The match is a guess, so the
hint says "may" and the exit status and output stay exactly as they were.
"""

from __future__ import annotations

import re
from pathlib import Path

from pantaray_agents.tools.files.pantaray_agents_md import (
    PANTARAY_AGENTS_MD_COMMAND_HINT,
    names_pantaray_agents_md,
)
from pantaray_agents.tools.files.secret_paths import (
    secret_path_error,
    withheld_reason,
)

from ..repository.command_invocation_audits import TerminalOutcome

# Usage errors, a file that cannot be executed, and a missing command: none is a
# denied write. Unlike Codex, the keywords are skipped for these too, because the
# hint offers write folders, which cannot fix a non-executable file (126,
# "Permission denied") or a denied read (grep exits 2).
_NOT_A_DENIED_WRITE_EXIT_CODES = frozenset({2, 126, 127})
# Codex's list without the Linux-only "seccomp" and "landlock", and without
# "sandbox", which ordinary output carries (a path under tooling/sandbox/ in a
# traceback, a log line) and would send the model into a needless approval.
_DENIAL_KEYWORDS = (
    "operation not permitted",
    "permission denied",
    "read-only file system",
    "failed to write file",
)

WRITE_FOLDER_REQUEST_HINT = (
    "This may have been blocked because the command tried to write outside the "
    "writable folders. If so, rerun the same call with additional_write_folders "
    "set to the folder it writes (a CLI usually keeps its state in its own folder "
    "under the home folder, such as ~/.<name>) and a justification. Do not work "
    "around the block with another location or means, and never copy credentials "
    "or sign-in files (for example into the work folder); request the folder "
    "instead."
)

NETWORK_DENIAL_HINT = (
    "This may have been blocked because the command tried to connect directly. "
    "Commands reach other hosts only through Pantaray's proxy, which HTTP(S) "
    "clients use from the HTTPS_PROXY and ALL_PROXY environment variables (git "
    "over ssh too); direct connections such as ssh to a host, database clients, "
    "raw sockets, and UDP are blocked. If the task needs one, run that command "
    "with bash and run_outside_sandbox."
)
# How a direct connection the sandbox refused reads in a client's error where
# no line pairs "Operation not permitted" with the call: Node's connect(), git's
# connect, and dig or nslookup asking a DNS server themselves over UDP.
_NETWORK_DENIAL_MARKERS = (
    "connect eperm",
    "errno=operation not permitted",
    "no servers could be reached",
)
# A refused connect() or sendto() is EPERM; on the same line as one of these it
# is a socket call, not a file write (ssh, nc, psql, ping, Python's urllib).
_SOCKET_CALL_WORDS = ("connect", "sendto", "socket", "urlopen")

NETWORK_OFF_DENIAL_HINT = (
    "This may have been blocked because the user's command network setting is "
    "off, which blocks every connection, localhost included. With it on, servers "
    "on localhost and HTTP(S) through Pantaray's proxy work, but direct "
    "connections to other hosts (ssh to a host, database clients) stay blocked. "
    'If the task needs it, ask the user to turn on "Allow network access for '
    'commands" in Settings.'
)
# With the network off, the diagnostics of a refused socket operation, as Node,
# curl, Python's urllib and ping print them under the network-off profile. Each
# names the operation, so a path that holds one of the words is not matched.
_NETWORK_OFF_DENIAL_MARKERS = (
    *_NETWORK_DENIAL_MARKERS,
    "listen eperm",
    "getaddrinfo enotfound localhost",
    "failed to connect to localhost",
    "failed to connect to 127.0.0.1",
    "<urlopen error [errno 1] operation not permitted>",
    "sendto: operation not permitted",
)
# Python's servers (http.server) print the refused bind() as a traceback whose
# error line names no call, so both lines are needed.
_PYTHON_BIND_CALL = "socket.bind("
_PYTHON_EPERM = "permissionerror: [errno 1] operation not permitted"


# What an error line can name a path as: `cat: .env: ...`, `[Errno 1] ...: '.env'`.
_PATH_TOKEN = re.compile(r"[^\s:'\"`]+")


def sandbox_denial_hint(
    *,
    terminal_outcome: TerminalOutcome,
    exit_code: int,
    stdout: str,
    stderr: str,
    cwd: Path,
    credentials_readable: bool,
    network_allowed: bool,
) -> str | None:
    """What to tell the model about a command the sandbox likely blocked.

    A denial that names a file holding secrets gets the file tools' refusal,
    whatever the exit status (grep exits 2): asking for write folders cannot
    fix it, and the model must not look for another way in. Neither can it fix
    one that names the Pantaray-wide AGENTS.md, which only apply_patch writes.
    A refused connection is told apart from a refused write: both can say
    "Operation not permitted". With network on that comes first; with network
    off it comes after the refusals that name a file.
    """

    if terminal_outcome != "exited" or exit_code == 0:
        return None
    if network_allowed and _is_likely_network_denied(f"{stderr}\n{stdout}"):
        return NETWORK_DENIAL_HINT
    for line in f"{stderr}\n{stdout}".splitlines():
        if not any(keyword in line.lower() for keyword in _DENIAL_KEYWORDS):
            continue
        for token in _PATH_TOKEN.findall(line):
            withheld = withheld_reason(
                cwd / token, credentials_readable=credentials_readable
            )
            if withheld is not None:
                error = secret_path_error(withheld)
                return f"{error} {error.fix_hint}"
            if names_pantaray_agents_md(cwd / token):
                return PANTARAY_AGENTS_MD_COMMAND_HINT
    if not network_allowed and _is_likely_network_off_denied(f"{stderr}\n{stdout}"):
        return NETWORK_OFF_DENIAL_HINT
    if is_likely_sandbox_denied(
        terminal_outcome=terminal_outcome,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
    ):
        return WRITE_FOLDER_REQUEST_HINT
    return None


def _is_likely_network_denied(output: str) -> bool:
    for line in output.lower().splitlines():
        if any(marker in line for marker in _NETWORK_DENIAL_MARKERS):
            return True
        if "operation not permitted" in line and any(
            word in line for word in _SOCKET_CALL_WORDS
        ):
            return True
    return False


def _is_likely_network_off_denied(output: str) -> bool:
    lowered = output.lower()
    if _PYTHON_BIND_CALL in lowered and _PYTHON_EPERM in lowered:
        return True
    return any(marker in lowered for marker in _NETWORK_OFF_DENIAL_MARKERS)


def is_likely_sandbox_denied(
    *, terminal_outcome: TerminalOutcome, exit_code: int, stdout: str, stderr: str
) -> bool:
    """Whether a command that exited on its own looks blocked by the sandbox.

    A timeout, cancellation, or signal is the runtime's own stop, not a refusal.
    """

    if terminal_outcome != "exited" or exit_code == 0:
        return False
    if exit_code in _NOT_A_DENIED_WRITE_EXIT_CODES:
        return False
    output = f"{stderr}\n{stdout}".lower()
    return any(keyword in output for keyword in _DENIAL_KEYWORDS)


__all__ = [
    "NETWORK_DENIAL_HINT",
    "NETWORK_OFF_DENIAL_HINT",
    "WRITE_FOLDER_REQUEST_HINT",
    "is_likely_sandbox_denied",
    "sandbox_denial_hint",
]
