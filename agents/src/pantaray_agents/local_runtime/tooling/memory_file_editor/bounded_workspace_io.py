from __future__ import annotations

import os
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TextIO

from pantaray_agents.local_runtime.descriptor_access import (
    DescriptorPathError,
    open_directory_descriptor,
    open_regular_file_at_descriptor,
    open_regular_file_descriptor,
)
from pantaray_agents.local_runtime.tooling.brokering.broker_common import (
    BrokerPolicyError,
)
from pantaray_agents.local_runtime.tooling.brokering.broker_direct_read_text import (
    read_text_descriptor_lines,
)
from pantaray_agents.local_runtime.tooling.fs_sandbox import (
    TextFileError,
    TextFileErrorCode,
    resolve_sandbox_path,
)

DEFAULT_READ_LINE_LIMIT = 400
MAX_READ_LINE_LIMIT = 2_000
SEARCH_MAX_ENTRIES = 20_000
SEARCH_MAX_FILES = 5_000
SEARCH_MAX_FILE_BYTES = 1024 * 1024
SEARCH_MAX_TOTAL_BYTES = 8 * 1024 * 1024
SEARCH_MAX_SECONDS = 5.0
SEARCH_MAX_LINE_CHARS = 2_000


class SearchTruncationReason(StrEnum):
    MATCH_LIMIT = "match_limit"
    TIME_LIMIT = "time_limit"
    ENTRY_LIMIT = "entry_limit"
    FILE_LIMIT = "file_limit"
    BYTE_LIMIT = "byte_limit"


@dataclass(frozen=True, slots=True)
class TextPage:
    relative_path: str
    text: str
    offset: int
    column: int
    end_line: int
    end_column: int
    total_lines: int | None
    next_offset: int | None
    next_column: int | None
    truncated: bool
    truncation_reason: str | None
    retry_hint: str | None


@dataclass(frozen=True, slots=True)
class SearchMatch:
    path: str
    line_number: int
    line: str


@dataclass(frozen=True, slots=True)
class SearchResult:
    matches: tuple[SearchMatch, ...]
    truncation_reason: SearchTruncationReason | None
    skipped_files: int


def read_text_page(
    *,
    sandbox_root: Path,
    canonical_sandbox_root: Path | None,
    path: str,
    offset: int,
    column: int,
    limit: int,
) -> TextPage:
    if offset < 1:
        raise ValueError("offset must be >= 1")
    if column < 1:
        raise ValueError("column must be >= 1")
    if not 1 <= limit <= MAX_READ_LINE_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_READ_LINE_LIMIT}")
    resolved = resolve_sandbox_path(
        sandbox_root=sandbox_root,
        canonical_sandbox_root=canonical_sandbox_root,
        relative_path=path,
        must_exist=True,
        must_be_file=True,
    )
    try:
        # The checked real path is opened again from the root without following
        # any link, so a directory swapped for one after the check fails here.
        descriptor = open_regular_file_descriptor(
            root_path=resolved.root,
            relative_path=resolved.path.relative_to(resolved.root).as_posix(),
        )
        try:
            page = read_text_descriptor_lines(
                descriptor=descriptor,
                offset=offset,
                column=column,
                limit=limit,
            )
        finally:
            os.close(descriptor)
    except BrokerPolicyError as exc:
        raise TextFileError(exc.code, str(exc)) from exc
    except UnicodeDecodeError as exc:
        raise TextFileError(
            TextFileErrorCode.UNSUPPORTED_FILE,
            f"file is not valid utf-8 text: {resolved.relative_path}",
        ) from exc
    except (DescriptorPathError, OSError) as exc:
        raise _io_error(path=resolved.relative_path, operation="read", exc=exc) from exc
    return TextPage(
        relative_path=resolved.relative_path,
        text=page.content,
        offset=offset,
        column=column,
        end_line=page.end_line,
        end_column=page.end_column,
        total_lines=page.total_lines,
        next_offset=page.next_offset,
        next_column=page.next_column,
        truncated=page.truncated,
        truncation_reason=page.truncation_reason,
        retry_hint=page.retry_hint,
    )


def search_text(
    *,
    sandbox_root: Path,
    canonical_sandbox_root: Path | None,
    query: str,
    limit: int,
    match_paths: bool,
) -> SearchResult:
    if not query.strip():
        raise ValueError("query must not be blank")
    root = resolve_sandbox_path(
        sandbox_root=sandbox_root,
        canonical_sandbox_root=canonical_sandbox_root,
        relative_path=".",
        must_exist=True,
        must_be_dir=True,
    ).path
    matches: list[SearchMatch] = []
    # Root-relative directories. Each is opened from the root without following
    # a link, so one swapped for a link after it was listed is not entered.
    pending = ["."]
    deadline = time.monotonic() + SEARCH_MAX_SECONDS
    entries_seen = files_seen = bytes_seen = skipped_files = 0
    reason: SearchTruncationReason | None = None
    while pending and reason is None:
        if time.monotonic() >= deadline:
            reason = SearchTruncationReason.TIME_LIMIT
            break
        directory = pending.pop()
        try:
            directory_descriptor = open_directory_descriptor(
                root_path=root, relative_path=directory
            )
        except (DescriptorPathError, OSError) as exc:
            if directory == ".":
                raise _io_error(path=".", operation="search", exc=exc) from exc
            skipped_files += 1
            continue
        try:
            try:
                with os.scandir(directory_descriptor) as iterator:
                    entries = []
                    for entry in iterator:
                        entries_seen += 1
                        if entries_seen > SEARCH_MAX_ENTRIES:
                            reason = SearchTruncationReason.ENTRY_LIMIT
                            break
                        entries.append(entry)
            except OSError as exc:
                if directory == ".":
                    raise _io_error(path=".", operation="search", exc=exc) from exc
                skipped_files += 1
                continue
            if reason is not None:
                break
            directories: list[str] = []
            for entry in sorted(entries, key=lambda item: item.name):
                relative_path = (
                    entry.name if directory == "." else f"{directory}/{entry.name}"
                )
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        directories.append(relative_path)
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    observed_size = entry.stat(follow_symlinks=False).st_size
                except OSError:
                    skipped_files += 1
                    continue
                files_seen += 1
                if files_seen > SEARCH_MAX_FILES:
                    reason = SearchTruncationReason.FILE_LIMIT
                    break
                if match_paths and query in relative_path:
                    matches.append(SearchMatch(relative_path, 0, ""))
                if len(matches) >= limit:
                    reason = SearchTruncationReason.MATCH_LIMIT
                    break
                if observed_size > SEARCH_MAX_FILE_BYTES:
                    skipped_files += 1
                    continue
                fd = -1
                try:
                    fd = open_regular_file_at_descriptor(
                        parent_descriptor=directory_descriptor,
                        relative_path=entry.name,
                    )
                    file_stat = os.fstat(fd)
                    if file_stat.st_size > SEARCH_MAX_FILE_BYTES:
                        skipped_files += 1
                        continue
                    if bytes_seen + file_stat.st_size > SEARCH_MAX_TOTAL_BYTES:
                        reason = SearchTruncationReason.BYTE_LIMIT
                        break
                    handle = open(fd, encoding="utf-8")
                    fd = -1
                    bytes_seen += file_stat.st_size
                    with handle:
                        matches.extend(
                            _search_file(
                                handle=handle,
                                relative_path=relative_path,
                                query=query,
                                limit=limit - len(matches),
                            )
                        )
                except (DescriptorPathError, OSError, UnicodeDecodeError):
                    skipped_files += 1
                finally:
                    if fd >= 0:
                        os.close(fd)
                if len(matches) >= limit:
                    reason = SearchTruncationReason.MATCH_LIMIT
                    break
            pending.extend(reversed(directories))
        finally:
            os.close(directory_descriptor)
    return SearchResult(tuple(matches), reason, skipped_files)


def _search_file(
    *, handle: TextIO, relative_path: str, query: str, limit: int
) -> tuple[SearchMatch, ...]:
    matches: list[SearchMatch] = []
    for line_number, line in enumerate(handle, start=1):
        if query not in line:
            continue
        visible = line.rstrip("\r\n")
        if len(visible) > SEARCH_MAX_LINE_CHARS:
            visible = f"{visible[:SEARCH_MAX_LINE_CHARS]}...[line truncated]"
        matches.append(SearchMatch(relative_path, line_number, visible))
        if len(matches) >= limit:
            break
    return tuple(matches)


def _io_error(
    *, path: str, operation: str, exc: DescriptorPathError | OSError
) -> TextFileError:
    return TextFileError(
        TextFileErrorCode.IO_FAILED,
        f"failed to {operation} text path {path}: {exc}",
    )


__all__ = [
    "DEFAULT_READ_LINE_LIMIT",
    "MAX_READ_LINE_LIMIT",
    "SearchResult",
    "SearchTruncationReason",
    "TextPage",
    "read_text_page",
    "search_text",
]
