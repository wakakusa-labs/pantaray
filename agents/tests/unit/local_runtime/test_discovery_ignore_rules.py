"""list, glob and grep leave out .git and what ignore files exclude."""

from __future__ import annotations

import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.tooling.brokering.broker import execute_broker_tool
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tools.files import ripgrep

from .broker_test_support import (
    BROKER_ACTOR_PROCESS_ID,
    _bootstrap_runtime_db_with_registered_folder,
)
from .discovery_output_support import glob_paths, grep_matches

pytestmark = pytest.mark.skipif(
    shutil.which("rg", path=ripgrep.RIPGREP_TRUSTED_PATH) is None,
    reason="ripgrep is not installed in a trusted location",
)

SHOWN = (
    "src/app.py",
    ".github/workflows/ci.yml",
    ".env.example",
)
LEFT_OUT = (
    ".git/config",
    "src/__pycache__/app.cpython-312.pyc",
    "build/app.py",
    "debug.log",
    "private.txt",
)


def _write(root: Path, relative_path: str, text: str = "needle\n") -> None:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _project(root: Path) -> None:
    for relative_path in (*SHOWN, *LEFT_OUT):
        _write(root, relative_path)
    _write(root, ".git/HEAD", "ref: refs/heads/main\n")
    _write(root, ".gitignore", "__pycache__/\nbuild/\n*.log\n")
    _write(root, ".ignore", "private.txt\n")


type _Run = Callable[[str, dict[str, JSONValue]], Awaitable[dict[str, JSONValue]]]


def _registered_repo(tmp_path: Path) -> tuple[Path, _Run]:
    db_path, context, repo, _folder = _bootstrap_runtime_db_with_registered_folder(
        tmp_path
    )

    async def run(tool_id: str, args: dict[str, JSONValue]) -> dict[str, JSONValue]:
        outcome = await execute_broker_tool(
            db_path=db_path,
            busy_timeout_ms=1_000,
            tool_id=tool_id,
            user_id="user-1",
            actor_process_id=BROKER_ACTOR_PROCESS_ID,
            manifest_id=context.manifest_id,
            execution_session_id=context.execution_session_id,
            args=args,
        )
        assert outcome.status == "success", outcome.output
        return outcome.output

    return repo, run


def _listed(output: dict[str, JSONValue], root: Path) -> set[str]:
    entries = output["entries"]
    assert isinstance(entries, list)
    return {
        Path(str(entry["path"])).relative_to(root).as_posix()
        for entry in entries
        if isinstance(entry, dict)
    }


def _relative(paths: list[str], root: Path) -> set[str]:
    return {Path(path).relative_to(root).as_posix() for path in paths}


@pytest.mark.asyncio
async def test_discovery_leaves_out_git_and_ignored_paths_but_keeps_hidden_files(
    tmp_path: Path,
) -> None:
    repo, run = _registered_repo(tmp_path)
    _project(repo)

    listed = _listed(await run("list", {"path": str(repo), "max_depth": 6}), repo)
    globbed = _relative(
        glob_paths(await run("glob", {"base_path": str(repo), "pattern": "**/*.py"})),
        repo,
    )
    # A pattern matching .git itself cannot bring it back.
    globbed_all = _relative(
        glob_paths(await run("glob", {"base_path": str(repo), "pattern": "**/*"})),
        repo,
    )
    grepped = _relative(
        [
            str(match["path"])
            for match in grep_matches(
                await run("grep", {"base_path": str(repo), "pattern": "needle"})
            )
        ],
        repo,
    )

    assert listed >= {*SHOWN, "src", ".github", ".github/workflows", ".gitignore"}
    assert listed.isdisjoint({*LEFT_OUT, ".git", "src/__pycache__", "build"}), listed
    assert globbed == {"src/app.py"}
    assert not any(path.startswith(".git/") for path in globbed_all), globbed_all
    assert grepped == set(SHOWN)


@pytest.mark.asyncio
async def test_discovery_shows_ignored_paths_a_call_names(tmp_path: Path) -> None:
    repo, run = _registered_repo(tmp_path)
    _project(repo)

    listed_build = _listed(await run("list", {"path": str(repo / "build")}), repo)
    listed_git = _listed(await run("list", {"path": str(repo / ".git")}), repo)
    grepped_cache = grep_matches(
        await run(
            "grep",
            {"base_path": str(repo / "src" / "__pycache__"), "pattern": "needle"},
        )
    )
    globbed_logs = _relative(
        glob_paths(await run("glob", {"base_path": str(repo), "pattern": "**/*.log"})),
        repo,
    )

    assert listed_build == {"build/app.py"}
    assert ".git/config" in listed_git
    assert [match["path"] for match in grepped_cache] == [
        str(repo / "src" / "__pycache__" / "app.cpython-312.pyc")
    ]
    assert globbed_logs == {"debug.log"}


@pytest.mark.asyncio
async def test_discovery_applies_ignore_files_above_the_registered_folder(
    tmp_path: Path,
) -> None:
    # The registered folder is part of a repository rooted above it, which the
    # search sandbox otherwise cannot read.
    _write(tmp_path, ".git/HEAD", "ref: refs/heads/main\n")
    _write(tmp_path, ".gitignore", "__pycache__/\n")
    repo, run = _registered_repo(tmp_path)
    _write(repo, "src/app.py")
    _write(repo, "src/__pycache__/app.cpython-312.pyc")

    listed = _listed(await run("list", {"path": str(repo), "max_depth": 6}), repo)
    globbed = _relative(
        glob_paths(await run("glob", {"base_path": str(repo), "pattern": "**/*app*"})),
        repo,
    )
    grepped = [
        match["path"]
        for match in grep_matches(
            await run("grep", {"base_path": str(repo), "pattern": "needle"})
        )
    ]

    assert listed == {"src", "src/app.py"}
    assert globbed == {"src/app.py"}
    assert grepped == [str(repo / "src" / "app.py")]
