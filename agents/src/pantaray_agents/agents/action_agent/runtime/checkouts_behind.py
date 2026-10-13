"""How far each workspace folder's git checkout is behind its upstream.

A checkout many commits behind answers questions about "the current code" with
old code, and nothing in the files says so. The count reads the remote-tracking
refs already on disk and never fetches: no network, no wait, and as fresh as
the user's last fetch.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

logger = logging.getLogger(__name__)

# Local refs answer in milliseconds; a repository that takes longer is skipped,
# so a hung git delays an Action's start by at most this per call.
_GIT_TIMEOUT_SECONDS = 2


def render_checkouts_behind(folder_paths: Sequence[str]) -> str:
    """Lines for the folders whose checkout is behind, or "" when none is."""

    in_repositories = [path for path in folder_paths if _inside_git_work_tree(path)]
    if not in_repositories or not _git_runs_without_prompt():
        return ""
    lines = [line for path in in_repositories if (line := _behind_line(path))]
    if not lines:
        return ""
    heading = (
        "Git checkouts behind their remote, as of the last fetch "
        "(merge commits not counted):"
    )
    return "\n".join((heading, *lines))


def _inside_git_work_tree(path: str) -> bool:
    folder = Path(path)
    try:
        return any((parent / ".git").exists() for parent in (folder, *folder.parents))
    except OSError:
        # A folder the runtime may no longer read, such as one whose permission
        # was revoked, has no line: the count is an aside and must not stop
        # the Action from starting.
        return False


def _git_runs_without_prompt() -> bool:
    # macOS's /usr/bin/git is a stub that opens an install dialog when the
    # developer tools are missing; `xcode-select -p` tells without one.
    if sys.platform != "darwin":
        return True
    return _run(["xcode-select", "-p"]) is not None


def _behind_line(path: str) -> str | None:
    # The branch's upstream, and the remote's default branch as well: a pushed
    # feature branch is level with its own upstream however far the default
    # branch has moved on. Merge commits are not counted: a release flow that
    # merges into the default branch leaves an up-to-date checkout "behind" by
    # its merges alone.
    refs = dict.fromkeys(
        ref
        for ref in (
            _git(path, "rev-parse", "--abbrev-ref", "@{upstream}"),
            _git(path, "rev-parse", "--abbrev-ref", "origin/HEAD"),
        )
        if ref is not None
    )
    behind = [
        f"{count} commits behind {ref}"
        for ref in refs
        if (count := _git(path, "rev-list", "--count", "--no-merges", f"HEAD..{ref}"))
        not in (None, "0")
    ]
    return f"- {path}: {'; '.join(behind)}" if behind else None


def _git(path: str, *args: str) -> str | None:
    return _run(["git", "-C", path, *args])


def _run(command: list[str]) -> str | None:
    """The command's trimmed output, or None when it fails or is missing."""

    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except OSError:
        # git missing, or a folder it cannot enter: that checkout has no line.
        return None
    except subprocess.TimeoutExpired:
        logger.warning(
            "Skipped a checkout: %s took over %ss", command[0], _GIT_TIMEOUT_SECONDS
        )
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


__all__ = ["render_checkouts_behind"]
