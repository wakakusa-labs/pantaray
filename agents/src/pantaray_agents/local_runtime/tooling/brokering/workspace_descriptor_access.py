from __future__ import annotations

import errno
import os
import stat
import time
from collections.abc import Callable, Generator
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath
from typing import Literal, NamedTuple

import regex  # type: ignore[import-untyped]

from pantaray_agents.local_runtime.descriptor_access import (
    DescriptorPathError,
    DescriptorPathMissingError,
    DescriptorPathPolicyError,
    open_directory_descriptor,
    open_regular_file_descriptor,
)

from .broker_common import BrokerPolicyError
from .broker_grep_lines import RipgrepGrepMatch, grep_match

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY
_FILE_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
_WORKSPACE_FILE_POLICY_ERROR = (
    "workspace path is missing, not a regular file, or uses a symlink"
)
SEARCH_TIMEOUT_SECONDS = 5.0

type DescriptorTruncationReason = Literal["limit", "timeout"]


class WorkspaceDescriptorEntry(NamedTuple):
    root_relative_path: str
    kind: Literal["file", "directory"]


@dataclass(slots=True)
class WorkspaceScanSkips:
    """What a scan passed over without a word in its entries."""

    symlinks: int = 0
    unreadable: int = 0
    first_unreadable_error: str | None = None
    # Listed directories at max_depth, whose contents were not walked.
    unexpanded_directories: int = 0


class WorkspaceDescriptorScan(NamedTuple):
    entries: tuple[WorkspaceDescriptorEntry, ...]
    truncation_reason: DescriptorTruncationReason | None
    skips: WorkspaceScanSkips


class WorkspaceGrepScan(NamedTuple):
    matches: tuple[RipgrepGrepMatch, ...]
    truncation_reason: DescriptorTruncationReason | None
    skips: WorkspaceScanSkips
    # Files holding a NUL byte that match; their lines are not returned.
    binary_match_paths: tuple[str, ...]


class WorkspacePathMissingError(BrokerPolicyError):
    """Raised when a required workspace path component does not exist."""


def open_workspace_file_descriptor(*, root_path: Path, relative_path: str) -> int:
    try:
        return open_regular_file_descriptor(
            root_path=root_path,
            relative_path=relative_path,
        )
    except DescriptorPathMissingError as exc:
        raise WorkspacePathMissingError(_WORKSPACE_FILE_POLICY_ERROR) from exc
    except DescriptorPathPolicyError as exc:
        raise BrokerPolicyError(_WORKSPACE_FILE_POLICY_ERROR) from exc


def open_workspace_entry_descriptor(*, root_path: Path, relative_path: str) -> int:
    """Open a file or directory below root without following any symlink.

    The caller reads the kind from the descriptor. Opened like a file so a FIFO
    does not block; a directory opened this way still lists with os.scandir.
    """

    components = _relative_components(relative_path, allow_dot=True)
    if not components:
        return _open_directory(root_path, ".")
    parent = _open_directory(root_path, "/".join(components[:-1]) or ".")
    try:
        return _open(components[-1], _FILE_FLAGS, parent=parent)
    finally:
        os.close(parent)


def scan_workspace_entries(
    *,
    root_path: Path,
    base_path: str,
    max_depth: int | None,
    limit: int,
    include_path: Callable[[Path], bool] | None = None,
    exclude_subtree: Callable[[Path], bool] | None = None,
    file_pattern: str | None = None,
    deadline: float | None = None,
) -> WorkspaceDescriptorScan:
    """Scan entries under base_path in path order, up to limit selected entries.

    The order is the same on every call, so a caller pages by asking for more.
    include_path filters entries after they are opened and still walks into a
    directory it drops. exclude_subtree drops an entry before it is opened, and
    neither it nor anything under it is walked.
    """

    selected: list[WorkspaceDescriptorEntry] = []
    skips = WorkspaceScanSkips()
    iterator = _entries(
        root_path=root_path,
        base_path=base_path,
        max_depth=max_depth,
        deadline=deadline,
        exclude_subtree=exclude_subtree,
        skips=skips,
    )
    reason: DescriptorTruncationReason | None = None
    prefix = "" if base_path == "." else f"{base_path}/"
    try:
        for entry, _descriptor in iterator:
            relative = entry.root_relative_path.removeprefix(prefix)
            path = root_path.joinpath(*entry.root_relative_path.split("/"))
            if include_path is not None and not include_path(path):
                continue
            if file_pattern is not None and (
                entry.kind != "file"
                or not matches_workspace_glob(relative, file_pattern)
            ):
                continue
            if len(selected) >= limit:
                reason = "limit"
                break
            selected.append(entry)
            if entry.kind == "directory" and relative.count("/") + 1 == max_depth:
                skips.unexpanded_directories += 1
    except TimeoutError:
        reason = "timeout"
    finally:
        iterator.close()
    return WorkspaceDescriptorScan(tuple(selected), reason, skips)


def glob_workspace_files(
    *,
    root_path: Path,
    base_path: str,
    pattern: str,
    limit: int,
    exclude_subtree: Callable[[Path], bool] | None = None,
) -> WorkspaceDescriptorScan:
    return scan_workspace_entries(
        root_path=root_path,
        base_path=base_path,
        max_depth=None,
        limit=limit,
        exclude_subtree=exclude_subtree,
        file_pattern=pattern,
        deadline=time.monotonic() + SEARCH_TIMEOUT_SECONDS,
    )


def grep_workspace_files(
    *,
    root_path: Path,
    base_path: str,
    pattern: str,
    include_glob: str | None,
    max_matches: int,
    exclude_subtree: Callable[[Path], bool] | None = None,
) -> WorkspaceGrepScan:
    """Search readable files of any size; lines are decoded lossily, split as
    read splits them, and bounded to an excerpt around their first match."""

    deadline = time.monotonic() + SEARCH_TIMEOUT_SECONDS
    try:
        expression = regex.compile(pattern)
    except (regex.error, RecursionError) as exc:
        raise BrokerPolicyError(
            "grep pattern is invalid",
            code="GREP_PATTERN_INVALID",
            fix_hint="grep.pattern must be a valid regular expression.",
        ) from exc
    matches: list[RipgrepGrepMatch] = []
    binary_match_paths: list[str] = []
    skips = WorkspaceScanSkips()
    reason: DescriptorTruncationReason | None = None
    prefix = "" if base_path == "." else f"{base_path}/"
    iterator = _entries(
        root_path=root_path,
        base_path=base_path,
        max_depth=None,
        deadline=deadline,
        exclude_subtree=exclude_subtree,
        skips=skips,
    )
    try:
        for entry, descriptor in iterator:
            relative = entry.root_relative_path.removeprefix(prefix)
            if entry.kind != "file" or (
                include_glob is not None
                and not matches_workspace_glob(relative, include_glob)
            ):
                continue
            try:
                for line_number, body, match_start, binary in _matching_lines(
                    descriptor, expression, deadline
                ):
                    if binary:
                        binary_match_paths.append(entry.root_relative_path)
                        break
                    if len(matches) >= max_matches:
                        reason = "limit"
                        break
                    matches.append(
                        grep_match(
                            relative_path=entry.root_relative_path,
                            line_number=line_number,
                            text=body,
                            match_start=match_start,
                            cut=False,
                        )
                    )
            except TimeoutError:
                raise
            except OSError as exc:
                _skip_unreadable(skips, entry.root_relative_path, exc)
            if reason == "limit":
                break
    except TimeoutError:
        reason = "timeout"
    finally:
        iterator.close()
    return WorkspaceGrepScan(tuple(matches), reason, skips, tuple(binary_match_paths))


def _matching_lines(
    descriptor: int, expression: regex.Pattern[str], deadline: float
) -> Generator[tuple[int, str, int, bool], None, None]:
    # Yields each matching line with whether the file has shown a NUL byte yet.
    # Design limit: one line is held whole while it is searched, as ripgrep
    # does; the search timeout bounds the time, not the memory.
    with open(
        descriptor, encoding="utf-8", errors="replace", newline="", closefd=False
    ) as handle:
        binary = False
        for line_number, line in enumerate(handle, start=1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            binary = binary or "\0" in line
            body = line.rstrip("\r\n")
            found = expression.search(body, timeout=remaining)
            if found is not None:
                yield line_number, body, found.start(), binary


def matches_workspace_glob(path: str, pattern: str) -> bool:
    return fnmatch(path, pattern) or (
        pattern.startswith("**/") and fnmatch(path, pattern.removeprefix("**/"))
    )


def _entries(
    *,
    root_path: Path,
    base_path: str,
    max_depth: int | None,
    deadline: float | None,
    exclude_subtree: Callable[[Path], bool] | None,
    skips: WorkspaceScanSkips,
) -> Generator[tuple[WorkspaceDescriptorEntry, int], None, None]:
    base = _open_directory(root_path, base_path)
    relative = "/".join(_relative_components(base_path, allow_dot=True)) or "."
    excluded = (
        None
        if exclude_subtree is None
        else lambda child: exclude_subtree(root_path.joinpath(*child.split("/")))
    )
    try:
        yield from _walk(base, relative, 0, max_depth, deadline, excluded, skips)
    finally:
        os.close(base)


def _walk(
    descriptor: int,
    relative: str,
    depth: int,
    max_depth: int | None,
    deadline: float | None,
    excluded: Callable[[str], bool] | None,
    skips: WorkspaceScanSkips,
) -> Generator[tuple[WorkspaceDescriptorEntry, int], None, None]:
    try:
        context = os.scandir(descriptor)
    except OSError as exc:
        if depth > 0:
            _skip_unreadable(skips, relative, exc)
            return
        _raise_policy(exc)
        raise
    with context as iterator:
        # Design limit: a directory's names are held while it is walked, so the
        # order is the same on every call; fine below 10^6 entries a directory.
        for item in sorted(iterator, key=lambda entry: entry.name):
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError
            child_relative = item.name if relative == "." else f"{relative}/{item.name}"
            if excluded is not None and excluded(child_relative):
                continue
            try:
                if item.is_symlink():
                    skips.symlinks += 1
                    continue
                is_directory = item.is_dir(follow_symlinks=False)
                flags = _DIRECTORY_FLAGS if is_directory else _FILE_FLAGS
                child = _open(item.name, flags, parent=descriptor)
            except OSError as exc:
                _skip_unreadable(skips, child_relative, exc)
                continue
            child_depth = depth + 1
            try:
                mode = os.fstat(child).st_mode
                if is_directory:
                    if not stat.S_ISDIR(mode):
                        raise BrokerPolicyError(
                            "workspace path must reference a directory"
                        )
                    yield WorkspaceDescriptorEntry(child_relative, "directory"), child
                    if max_depth is None or child_depth < max_depth:
                        yield from _walk(
                            child,
                            child_relative,
                            child_depth,
                            max_depth,
                            deadline,
                            excluded,
                            skips,
                        )
                elif stat.S_ISREG(mode):
                    yield WorkspaceDescriptorEntry(child_relative, "file"), child
            finally:
                os.close(child)


def scan_skip_notes(
    skips: WorkspaceScanSkips, *, max_depth: int | None = None, depth_limit: int = 0
) -> tuple[list[str], list[str]]:
    """Warnings and retry hints for what a scan passed over."""

    warnings: list[str] = []
    hints: list[str] = []
    if skips.unexpanded_directories:
        warnings.append(
            f"{skips.unexpanded_directories} listed director(y/ies) at "
            f"max_depth={max_depth} were not opened; their contents are not listed."
        )
        hints.append(
            f"Raise max_depth (up to {depth_limit}) or list one of them to see inside."
        )
    if skips.symlinks:
        warnings.append(
            f"{skips.symlinks} symlink(s) were skipped: symlinks are not followed, "
            "and neither they nor their targets are listed or searched."
        )
    if skips.unreadable:
        warnings.append(
            f"{skips.unreadable} path(s) could not be read and were skipped, with "
            "anything under them. First error: "
            f"{skips.first_unreadable_error}."
        )
    return warnings, hints


def _skip_unreadable(skips: WorkspaceScanSkips, relative: str, exc: OSError) -> None:
    # A path swapped for a symlink or removed mid-scan stays a policy error.
    _raise_policy(exc)
    skips.unreadable += 1
    if skips.first_unreadable_error is None:
        skips.first_unreadable_error = f"{relative}: {exc.strerror or exc}"


def _open_directory(root_path: Path, relative_path: str) -> int:
    try:
        return open_directory_descriptor(
            root_path=root_path,
            relative_path=relative_path,
        )
    except DescriptorPathError as exc:
        raise BrokerPolicyError(
            "workspace path is missing, not a directory, or uses a symlink"
        ) from exc


def _open(component: str, flags: int, *, parent: int | None = None) -> int:
    try:
        if parent is None:
            return os.open(component, flags)
        return os.open(component, flags, dir_fd=parent)
    except OSError as exc:
        _raise_policy(exc)
        raise


def _raise_policy(exc: OSError) -> None:
    if exc.errno in {errno.ELOOP, errno.ENOENT, errno.ENOTDIR}:
        raise BrokerPolicyError(
            "workspace path is missing, not a directory, or uses a symlink"
        ) from exc


def _relative_components(value: str, *, allow_dot: bool) -> tuple[str, ...]:
    if value == ".":
        if allow_dot:
            return ()
        raise BrokerPolicyError("workspace path must reference a file")
    path = PurePosixPath(value)
    if not value or path.is_absolute() or not path.parts:
        raise BrokerPolicyError("workspace path must be root-relative")
    if any(
        component in {"", ".", ".."} or "\0" in component for component in path.parts
    ):
        raise BrokerPolicyError("workspace path contains an unsafe component")
    return path.parts
