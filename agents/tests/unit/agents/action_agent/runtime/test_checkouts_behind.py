"""How far a workspace checkout is behind, read from refs already fetched."""

from __future__ import annotations

import subprocess
from pathlib import Path

from pantaray_agents.agents.action_agent.runtime.checkouts_behind import (
    render_checkouts_behind,
)

_HEADING = (
    "Git checkouts behind their remote, as of the last fetch "
    "(merge commits not counted):\n"
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            *("-c", "user.name=t", "-c", "user.email=t@example.com"),
            *("-c", "commit.gpgsign=false"),
            *args,
        ],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def _commit(repo: Path, name: str) -> None:
    (repo / name).write_text(name)
    _git(repo, "add", name)
    _git(repo, "commit", "-m", name)


def _clone_pair(tmp_path: Path) -> tuple[Path, Path]:
    """A checkout and a second clone that moves the remote on."""

    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(remote))
    _git(tmp_path, "clone", str(remote), "work")
    work = tmp_path / "work"
    _git(work, "checkout", "-b", "main")
    _commit(work, "base")
    _git(work, "push", "-u", "origin", "main")
    _git(work, "remote", "set-head", "origin", "main")
    _git(tmp_path, "clone", str(remote), "other")
    return work, tmp_path / "other"


def test_a_checkout_behind_its_upstream_is_named_with_the_count(
    tmp_path: Path,
) -> None:
    work, other = _clone_pair(tmp_path)
    assert render_checkouts_behind([str(work)]) == ""

    _commit(other, "one")
    _commit(other, "two")
    _git(other, "push")
    # Nothing is fetched: the count is as fresh as the last fetch.
    assert render_checkouts_behind([str(work)]) == ""
    _git(work, "fetch")

    assert render_checkouts_behind([str(work), str(tmp_path / "plain")]) == (
        f"{_HEADING}- {work}: 2 commits behind origin/main"
    )


def test_a_feature_branch_level_with_its_upstream_still_shows_the_default_branch(
    tmp_path: Path,
) -> None:
    work, other = _clone_pair(tmp_path)
    _git(work, "checkout", "-b", "feature")
    _commit(work, "feature")
    _git(work, "push", "-u", "origin", "feature")
    _commit(other, "landed")
    _git(other, "push")
    _git(work, "fetch")
    (work / "sub").mkdir()

    assert render_checkouts_behind([str(work / "sub")]) == (
        f"{_HEADING}- {work / 'sub'}: 1 commits behind origin/main"
    )


def test_merge_commits_alone_do_not_make_a_checkout_behind(tmp_path: Path) -> None:
    """A release that merges develop into main leaves develop up to date."""

    work, other = _clone_pair(tmp_path)
    _git(work, "checkout", "-b", "develop")
    _commit(work, "change")
    _git(work, "push", "-u", "origin", "develop")
    _git(other, "fetch")
    _git(other, "merge", "--no-ff", "-m", "release", "origin/develop")
    _git(other, "push")
    _git(work, "fetch")

    assert render_checkouts_behind([str(work)]) == ""
