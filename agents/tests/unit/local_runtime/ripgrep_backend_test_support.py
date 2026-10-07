from __future__ import annotations

import fnmatch
import os
import re
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from pantaray_agents.tools.contract import BrokerPolicyError
from pantaray_agents.tools.files.grep_lines import (
    RipgrepGrepMatch,
    grep_match,
)
from pantaray_agents.tools.files.ripgrep import (
    RipgrepGlobResult,
    RipgrepGrepResult,
)


def install_fake_ripgrep_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    from pantaray_agents.local_runtime.tooling.brokering import broker_discovery

    monkeypatch.setattr(broker_discovery, "run_ripgrep_files", _fake_files)
    monkeypatch.setattr(broker_discovery, "run_ripgrep_grep", _fake_grep)


def _fake_files(
    *,
    cwd: Path,
    sandbox_profile: str,
    glob_pattern: str,
    limit: int,
    follow_symlinks: bool = False,
    pruned_relative_paths: tuple[str, ...] = (),
    extra_search_paths: tuple[str, ...] = (),
    include_path: Callable[[Path], bool] | None = None,
) -> RipgrepGlobResult:
    matches: list[str] = []
    truncated = False
    for relative_path in _iter_workspace_files(
        cwd,
        follow_symlinks=follow_symlinks,
        pruned_relative_paths=pruned_relative_paths,
        extra_search_paths=extra_search_paths,
    ):
        if include_path is not None and not include_path(cwd / relative_path):
            continue
        if not _matches_relative_glob(relative_path, glob_pattern):
            continue
        if len(matches) >= limit:
            truncated = True
            break
        matches.append(relative_path)
    return RipgrepGlobResult(
        relative_paths=tuple(matches),
        truncated=truncated,
        truncation_reason="limit" if truncated else None,
        timed_out=False,
        skipped_files=0,
        first_skip_error=None,
    )


def _fake_grep(
    *,
    cwd: Path,
    sandbox_profile: str,
    pattern: str,
    include_glob: str | None,
    max_matches: int,
    follow_symlinks: bool = False,
    pruned_relative_paths: tuple[str, ...] = (),
    extra_search_paths: tuple[str, ...] = (),
    include_path: Callable[[Path], bool] | None = None,
) -> RipgrepGrepResult:
    try:
        compiled_pattern = re.compile(pattern)
    except re.error as exc:
        raise BrokerPolicyError(
            "grep pattern is invalid",
            code="GREP_PATTERN_INVALID",
            fix_hint="grep.pattern must be a valid regular expression.",
        ) from exc

    matches: list[RipgrepGrepMatch] = []
    truncated = False
    for relative_path in _iter_workspace_files(
        cwd,
        follow_symlinks=follow_symlinks,
        pruned_relative_paths=pruned_relative_paths,
        extra_search_paths=extra_search_paths,
    ):
        if include_path is not None and not include_path(cwd / relative_path):
            continue
        if include_glob and not _matches_relative_glob(relative_path, include_glob):
            continue
        path = cwd / relative_path
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line_number, line in enumerate(handle, start=1):
                found = compiled_pattern.search(line)
                if found is None:
                    continue
                if len(matches) >= max_matches:
                    truncated = True
                    break
                matches.append(
                    grep_match(
                        relative_path=relative_path,
                        line_number=line_number,
                        text=line.rstrip("\r\n"),
                        match_start=found.start(),
                        cut=False,
                    )
                )
            if truncated:
                break
    return RipgrepGrepResult(
        matches=tuple(matches),
        truncated=truncated,
        truncation_reason="limit" if truncated else None,
        timed_out=False,
        skipped_files=0,
        first_skip_error=None,
        binary_match_paths=(),
    )


def _iter_workspace_files(
    workspace_path: Path,
    *,
    follow_symlinks: bool = False,
    pruned_relative_paths: tuple[str, ...] = (),
    extra_search_paths: tuple[str, ...] = (),
) -> Iterator[str]:
    # Like ripgrep: a pruned directory is not descended from ".", while an
    # explicit search path is walked even when it lies under one.
    pruned = {path.casefold() for path in pruned_relative_paths}
    # Like ripgrep without --follow: a link is neither listed nor walked.
    for start in (".", *extra_search_paths):
        for dirpath, dirnames, filenames in os.walk(
            workspace_path / start,
            followlinks=follow_symlinks,
        ):
            relative_dir = Path(dirpath).relative_to(workspace_path)
            if start == ".":
                dirnames[:] = [
                    name
                    for name in dirnames
                    if (relative_dir / name).as_posix().casefold() not in pruned
                ]
            dirnames.sort()
            for filename in sorted(filenames):
                path = Path(dirpath) / filename
                if follow_symlinks or not path.is_symlink():
                    yield path.relative_to(workspace_path).as_posix()


def _matches_relative_glob(relative_path: str, pattern: str) -> bool:
    path_parts = tuple(part for part in relative_path.split("/") if part)
    pattern_parts = tuple(part for part in pattern.split("/") if part)
    return _matches_glob_parts(pattern_parts, path_parts, 0, 0)


def _matches_glob_parts(
    pattern_parts: tuple[str, ...],
    path_parts: tuple[str, ...],
    pattern_index: int,
    path_index: int,
) -> bool:
    if pattern_index == len(pattern_parts):
        return path_index == len(path_parts)
    pattern_part = pattern_parts[pattern_index]
    if pattern_part == "**":
        return _matches_glob_parts(
            pattern_parts, path_parts, pattern_index + 1, path_index
        ) or (
            path_index < len(path_parts)
            and _matches_glob_parts(
                pattern_parts, path_parts, pattern_index, path_index + 1
            )
        )
    return (
        path_index < len(path_parts)
        and fnmatch.fnmatchcase(path_parts[path_index], pattern_part)
        and _matches_glob_parts(
            pattern_parts, path_parts, pattern_index + 1, path_index + 1
        )
    )
