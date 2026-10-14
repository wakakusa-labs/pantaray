"""How far the git checkout a tool call works in is behind its remote.

A checkout many commits behind answers questions about "the current code" with
old code, and nothing in the files says so. So the first tool call of an Action
(or of a subagent) to work in a checkout gets, beside its result, how far that
checkout is behind its upstream, the remote's default branch and the remote's
line of work updated last -- the same first-touch path that attaches the
repository's AGENTS.md. The counts read the remote-tracking refs already on
disk and never fetch: no network, no wait, and as fresh as the last fetch,
which the model can renew itself.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

logger = logging.getLogger(__name__)

# Local refs answer in milliseconds; a repository that takes longer is skipped,
# so a hung git delays a tool result by at most this per call.
_GIT_TIMEOUT_SECONDS = 2
# How long a tool result waits for its checkouts' status. Reading it takes a few
# git calls of up to _GIT_TIMEOUT_SECONDS each, run off the event loop; past
# this the result goes on without the sentence.
_STATUS_TIMEOUT_SECONDS = 2
# What ``attached`` holds for a checkout already described, beside AGENTS.md paths.
CHECKOUT_KEY_PREFIX = "checkout:"


def enclosing_checkout(path: Path) -> Path | None:
    """The checkout a path is in: its nearest ancestor holding ``.git``."""

    try:
        directory = path if path.is_dir() else path.parent
        return next(
            (
                parent
                for parent in (directory, *directory.parents)
                if (parent / ".git").exists()
            ),
            None,
        )
    except OSError:
        # An ancestor the runtime may not stat names no checkout; the call goes on.
        return None


def checkout_status(checkout: Path) -> str | None:
    """One sentence on how far the checkout is behind, or None when it is not."""

    try:
        if not _git_runs_without_prompt():
            return None
        return _describe(str(checkout))
    except Exception as exc:  # noqa: BLE001
        # The sentence is an aside to a tool result. Whatever goes wrong -- a
        # revoked permission, a ref name git and Python read differently, a git
        # that misbehaves -- leaves it out and must never fail the call.
        logger.warning("Skipped a checkout's status: %s", type(exc).__name__)
        return None


async def checkout_statuses(checkouts: Sequence[Path]) -> list[str]:
    """The sentences for the checkouts that are behind, read in a thread.

    The git calls block, so they never run on the event loop: sibling tool
    calls and the Stop watch keep going while they run.
    """

    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_statuses, checkouts), _STATUS_TIMEOUT_SECONDS
        )
    except TimeoutError:
        logger.warning(
            "Skipped checkout status: took over %ss", _STATUS_TIMEOUT_SECONDS
        )
        return []


def _statuses(checkouts: Sequence[Path]) -> list[str]:
    return [status for checkout in checkouts if (status := checkout_status(checkout))]


@functools.cache
def _git_runs_without_prompt() -> bool:
    # macOS's /usr/bin/git is a stub that opens an install dialog when the
    # developer tools are missing; `xcode-select -p` tells without one.
    if sys.platform != "darwin":
        return True
    return _run(["xcode-select", "-p"]) is not None


def _describe(path: str) -> str | None:
    branch, upstream = _current_branch(path)
    if branch is not None:
        state = f"branch {_shown(branch)}"
    else:
        commit = _git(path, "rev-parse", "--short", "HEAD")
        if commit is None:
            return None
        state = f"detached at {commit}"
    # The branch's upstream; the remote's default branch, which a pushed
    # feature branch never shows; and the remote branch updated last, where a
    # project whose work lands on a branch such as develop moves. Merge commits
    # are not counted: a release flow that merges into the default branch
    # leaves an up-to-date checkout "behind" by its merges alone.
    refs = dict.fromkeys(
        ref for ref in (upstream, *_remote_mainlines(path)) if ref is not None
    )
    behind = [
        f"{count} commits behind {_shown(ref)}"
        for ref in refs
        if (count := _git(path, "rev-list", "--count", "--no-merges", f"HEAD..{ref}"))
        not in (None, "0")
    ]
    if not behind:
        return None
    return (
        f"Git checkout {path} ({state}) is {'; '.join(behind)}, as of the last "
        "fetch (merge commits not counted); `git fetch` updates these counts."
    )


def _current_branch(path: str) -> tuple[str | None, str | None]:
    """The checked-out branch and its upstream, in one git call."""

    listed = _git(
        path,
        "for-each-ref",
        "--format=%(HEAD)%00%(refname:short)%00%(upstream:short)",
        "refs/heads",
    )
    for line in (listed or "").splitlines():
        marker, name, upstream = line.split("\0")
        if marker == "*":
            return name, upstream or None
    return None, None


def _remote_mainlines(path: str) -> tuple[str | None, str | None]:
    """The remote's default branch and its unprefixed branch updated last, in
    one git call."""

    listed = _git(
        path,
        "for-each-ref",
        "--sort=-committerdate",
        "--format=%(refname:short)%00%(symref:short)",
        "refs/remotes/origin",
    )
    default: str | None = None
    latest: str | None = None
    for line in (listed or "").splitlines():
        name, target = line.split("\0")
        if target:
            default = target  # origin/HEAD, which points at the default branch
        elif latest is None and "/" not in name.removeprefix("origin/"):
            # Feature branches live under a prefix (feat/, fix/, dependabot/)
            # and are pushed often; a line of work such as develop is not.
            latest = name
    return default, latest


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


__all__ = [
    "CHECKOUT_KEY_PREFIX",
    "checkout_status",
    "checkout_statuses",
    "enclosing_checkout",
]
