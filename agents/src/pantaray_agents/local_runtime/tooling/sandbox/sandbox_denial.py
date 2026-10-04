"""Spot a command failure the sandbox likely caused, the way Codex does.

Adapted from Codex `is_likely_sandbox_denied` (codex-rs/sandboxing/src/denial.rs).
A denied write fails like any other error, often in words that do not name the
path (`codex exec`: "Operation not permitted"), so the model cannot tell it apart
from the command's own failure without a hint. The match is a guess, so the
hint says "may" and the exit status and output stay exactly as they were.
"""

from __future__ import annotations

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


__all__ = ["WRITE_FOLDER_REQUEST_HINT", "is_likely_sandbox_denied"]
