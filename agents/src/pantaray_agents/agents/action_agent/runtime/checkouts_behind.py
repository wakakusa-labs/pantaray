"""How far each workspace folder's git checkout is behind its upstream.

A checkout many commits behind answers questions about "the current code" with
old code, and nothing in the files says so. The count reads the remote-tracking
refs already on disk and never fetches: no network, no wait, and as fresh as
the user's last fetch.
"""

from __future__ import annotations

import functools
import logging
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

logger = logging.getLogger(__name__)

# Local refs answer in milliseconds; a repository that takes longer is skipped,
# so a hung git delays an Action's start by at most this per call.
_GIT_TIMEOUT_SECONDS = 2


def render_checkouts_behind(folder_paths: Sequence[str]) -> str:
    """Lines for the folders whose checkout is behind, or "" when none is."""

    # Asked once per render, and only once a folder is in a repository.
    can_run_git = functools.cache(_git_runs_without_prompt)
    lines = [line for path in folder_paths if (line := _folder_line(path, can_run_git))]
    if not lines:
        return ""
    heading = (
        "Git checkouts behind their remote, as of the last fetch "
        "(merge commits not counted):"
    )
    return "\n".join((heading, *lines))


def _folder_line(path: str, can_run_git: Callable[[], bool]) -> str | None:
    try:
        if not _inside_git_work_tree(path) or not can_run_git():
            return None
        return _behind_line(path)
    except Exception as exc:  # noqa: BLE001
        # The count is an aside to the Action's context. Whatever goes wrong
        # for one folder -- a revoked permission, a ref name git and Python read
        # differently, a git that misbehaves -- drops that folder's line and
        # must never stop the Action from starting.
        logger.warning("Skipped a checkout's behind count: %s", type(exc).__name__)
        return None


def _inside_git_work_tree(path: str) -> bool:
    folder = Path(path)
    return any((parent / ".git").exists() for parent in (folder, *folder.parents))


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
        f"{count} commits behind {_shown(ref)}"
        for ref in refs
        if (count := _git(path, "rev-list", "--count", "--no-merges", f"HEAD..{ref}"))
        not in (None, "0")
    ]
    return f"- {path}: {'; '.join(behind)}" if behind else None


def _shown(ref: str) -> str:
    """A ref name as the model reads it: bytes git gave that are not UTF-8
    become replacement characters, which the request can always encode."""

    return os.fsencode(ref).decode("utf-8", errors="replace")


def _git(path: str, *args: str) -> str | None:
    return _run(["git", "-C", path, *args])


def _run(command: list[str]) -> str | None:
    """The command's trimmed output, or None when it fails or is missing.

    The output is decoded the way the OS decodes file names, so a ref name in
    any encoding goes back to git byte for byte.
    """

    try:
        completed = subprocess.run(
            command,
            capture_output=True,
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
    return os.fsdecode(completed.stdout).strip() or None


__all__ = ["render_checkouts_behind"]
