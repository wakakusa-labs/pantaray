"""How far the checkout a tool call works in is behind, from refs already
fetched."""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from pathlib import Path

import pytest

from pantaray_agents.agents.action_agent.runtime import checkout_status as status
from pantaray_agents.agents.action_agent.runtime.checkout_status import (
    checkout_status,
)

_TAIL = (
    ", as of the last fetch (merge commits not counted); "
    "`git fetch` updates these counts."
)


def _line(work: Path, state: str, behind: str) -> str:
    return f"Git checkout {work} ({state}) is {behind}{_TAIL}"


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
    assert checkout_status(work) is None

    _commit(other, "one")
    _commit(other, "two")
    _git(other, "push")
    # Nothing is fetched: the count is as fresh as the last fetch.
    assert checkout_status(work) is None
    _git(work, "fetch")

    assert checkout_status(work) == _line(
        work, "branch main", "2 commits behind origin/main"
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

    assert checkout_status(work) == _line(
        work, "branch feature", "1 commits behind origin/main"
    )


def test_the_line_of_work_updated_last_is_counted_but_not_a_feature_branch(
    tmp_path: Path,
) -> None:
    """Work that lands on develop never shows against the default branch, and a
    feature branch pushed after it does not stand in for it."""

    work, other = _clone_pair(tmp_path)
    _git(other, "checkout", "-b", "develop")
    _commit(other, "landed")
    _git(other, "push", "origin", "develop")
    _git(other, "checkout", "-b", "fix/typo")
    (other / "typo").write_text("typo")
    _git(other, "add", "typo")
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]
        + ["-c", "commit.gpgsign=false", "commit", "-m", "typo"],
        cwd=other,
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_COMMITTER_DATE": "2090-01-01T00:00:00 +0000"},
    )
    _git(other, "push", "origin", "fix/typo")
    _git(work, "fetch")
    _git(work, "checkout", "--detach")

    line = checkout_status(work)
    assert line is not None
    assert line.startswith(f"Git checkout {work} (detached at ")
    assert line.endswith(f") is 1 commits behind origin/develop{_TAIL}")


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

    assert checkout_status(work) is None


def test_a_ref_name_that_is_not_utf8_is_counted_and_shown_safely(
    tmp_path: Path,
) -> None:
    """A Shift-JIS branch name goes back to git byte for byte; the model reads
    replacement characters instead of the call failing."""

    work, other = _clone_pair(tmp_path)
    _commit(other, "landed")
    _git(other, "push")
    _git(work, "fetch")
    landed = subprocess.run(
        ["git", "rev-parse", "origin/main"],
        cwd=work,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    branch = "日本".encode("shift_jis")
    # Packed, because a file system may refuse a ref file named in these bytes
    # (APFS takes UTF-8 names only).
    with (work / ".git" / "packed-refs").open("ab") as packed:
        packed.write(landed.encode() + b" refs/remotes/origin/" + branch + b"\n")
    subprocess.run(
        [b"git", b"config", b"branch.main.merge", b"refs/heads/" + branch],
        cwd=work,
        check=True,
    )

    shown = os.fsencode(os.fsdecode(branch)).decode("utf-8", errors="replace")
    assert checkout_status(work) == _line(
        work,
        "branch main",
        f"1 commits behind origin/{shown}; 1 commits behind origin/main",
    )


def test_any_failure_leaves_the_status_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work, _other = _clone_pair(tmp_path)

    def _broken(_path: str) -> str:
        raise LookupError("a codec git output could not be read with")

    monkeypatch.setattr(status, "_describe", _broken)

    assert checkout_status(work) is None


def test_a_hung_git_lets_the_run_end_within_the_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Past the limit the thread starts no more git, so nothing is left for the
    loop's executor to wait on when the run (or a subagent) ends."""

    work, _other = _clone_pair(tmp_path)
    hung = tmp_path / "bin"
    hung.mkdir()
    (hung / "git").write_text("#!/bin/sh\nexec sleep 30\n")
    (hung / "git").chmod(0o755)
    monkeypatch.setenv("PATH", f"{hung}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(status, "_STATUS_TIMEOUT_SECONDS", 0.5)

    started = time.monotonic()
    statuses = asyncio.run(status.checkout_statuses([work]))

    assert statuses == {}
    assert time.monotonic() - started < 1.5
