from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import regex  # type: ignore[import-untyped]

from pantaray_agents.local_runtime.tooling.brokering.broker_common import (
    BrokerPolicyError,
)
from pantaray_agents.local_runtime.tooling.brokering.broker_direct_read_text import (
    read_text_descriptor_lines,
    read_text_value_lines,
)
from pantaray_agents.local_runtime.tooling.brokering.broker_grep_lines import (
    GREP_MAX_LINE_CHARS,
    GREP_OMITTED_TEXT_MARKER,
    RipgrepGrepMatch,
    binary_match_warning,
    grep_match,
)
from pantaray_agents.local_runtime.tooling.brokering.private_app_storage import (
    PRIVATE_APP_STORAGE_MESSAGE,
    is_within_any,
)
from pantaray_agents.local_runtime.tooling.brokering.workspace_descriptor_access import (
    SEARCH_TIMEOUT_SECONDS,
    DescriptorTruncationReason,
    WorkspaceGrepScan,
    WorkspaceScanSkips,
    glob_workspace_files,
    grep_workspace_files,
    matches_workspace_glob,
    open_workspace_file_descriptor,
    scan_skip_notes,
    scan_workspace_entries,
)
from pantaray_agents.schema.agent.base import JSONValue

from .roots import MemoryReadRoot, ReadOnlyRoot, WorkspaceReadRoot

READ_MAX_BYTES = 4_000
RESULT_CONTENT_MAX_CHARS = 4_800
LIST_MAX_DEPTH = 4
# Suggestions have memory_search, not the Action's memory_sql.
_PRIVATE_APP_STORAGE_ERROR = (
    f"{PRIVATE_APP_STORAGE_MESSAGE} Search Pantaray's own records with "
    "memory_search instead of opening its files."
)


@dataclass(frozen=True, slots=True)
class _Page:
    items: list[JSONValue]
    next_offset: int | None
    truncated: bool
    truncation_reason: str | None


@dataclass(frozen=True, slots=True)
class _Found:
    items: list[JSONValue]
    reason: DescriptorTruncationReason | None = None
    skips: WorkspaceScanSkips = field(default_factory=WorkspaceScanSkips)


class ReadOnlyFileAccess:
    def __init__(self, *, roots: tuple[ReadOnlyRoot, ...]) -> None:
        if any(not root.root_id.strip() for root in roots):
            raise ValueError("readable root ids must not be empty")
        self._roots = {root.root_id: root for root in roots}
        if len(self._roots) != len(roots):
            raise ValueError("readable root ids must be unique")

    def read(
        self,
        *,
        root_id: str,
        path: str,
        offset: int,
        column: int,
        limit: int,
    ) -> dict[str, JSONValue]:
        root = self._root(root_id)
        relative_path = _relative_path(path)
        if isinstance(root, MemoryReadRoot):
            content = _memory_document(root, relative_path)
            result = read_text_value_lines(
                text=content,
                offset=offset,
                column=column,
                limit=limit,
                max_bytes=READ_MAX_BYTES,
            )
        else:
            _reject_private_app_storage(root, relative_path)
            descriptor = open_workspace_file_descriptor(
                root_path=root.canonical_path,
                relative_path=relative_path,
            )
            try:
                result = read_text_descriptor_lines(
                    descriptor=descriptor,
                    offset=offset,
                    column=column,
                    limit=limit,
                    max_bytes=READ_MAX_BYTES,
                )
            finally:
                os.close(descriptor)
        return {
            "status": "success",
            "root": root_id,
            "path": relative_path,
            "content": result.content,
            "offset": offset,
            "column": column,
            "end_line": result.end_line,
            "end_column": result.end_column,
            "total_lines": result.total_lines,
            "next_offset": result.next_offset,
            "next_column": result.next_column,
            "truncated": result.truncated,
            "truncation_reason": result.truncation_reason,
            "retry_hint": result.retry_hint,
        }

    def list(
        self,
        *,
        root_id: str,
        path: str,
        max_depth: int,
        offset: int,
        limit: int,
    ) -> dict[str, JSONValue]:
        root = self._root(root_id)
        relative_path = _relative_path(path, allow_dot=True)
        if isinstance(root, MemoryReadRoot):
            found = _memory_list_entries(
                root=root,
                base_path=relative_path,
                max_depth=max_depth,
            )
        else:
            found = _workspace_list_entries(
                root=root,
                base_path=relative_path,
                max_depth=max_depth,
                limit=offset - 1 + limit,
            )
        page = _bounded_page(found, offset=offset, limit=limit)
        warnings, hints = _page_notes(found, page, max_depth=max_depth)
        return {
            "status": "success",
            "root": root_id,
            "path": relative_path,
            "entries": page.items,
            **_page_fields(page, warnings, hints),
        }

    def glob(
        self,
        *,
        root_id: str,
        base_path: str,
        pattern: str,
        offset: int,
        limit: int,
    ) -> dict[str, JSONValue]:
        root = self._root(root_id)
        relative_base = _relative_path(base_path, allow_dot=True)
        _validate_glob_pattern(pattern)
        if isinstance(root, MemoryReadRoot):
            found = _Found(
                _memory_glob_matches(
                    root=root, base_path=relative_base, pattern=pattern
                )
            )
        else:
            found = _workspace_glob_matches(
                root=root,
                base_path=relative_base,
                pattern=pattern,
                limit=offset - 1 + limit,
            )
        page = _bounded_page(found, offset=offset, limit=limit)
        warnings, hints = _page_notes(found, page)
        return {
            "status": "success",
            "root": root_id,
            "base_path": relative_base,
            "matches": page.items,
            **_page_fields(page, warnings, hints),
        }

    def grep(
        self,
        *,
        root_id: str,
        base_path: str,
        pattern: str,
        include_glob: str | None,
        offset: int,
        max_matches: int,
    ) -> dict[str, JSONValue]:
        root = self._root(root_id)
        relative_base = _relative_path(base_path, allow_dot=True)
        if isinstance(root, MemoryReadRoot):
            result = _memory_grep_matches(
                root=root,
                base_path=relative_base,
                pattern=pattern,
                include_glob=include_glob,
            )
        else:
            _reject_private_app_storage(root, relative_base)
            result = grep_workspace_files(
                root_path=root.canonical_path,
                base_path=relative_base,
                pattern=pattern,
                include_glob=include_glob,
                max_matches=offset - 1 + max_matches,
                exclude_subtree=_in_private_app_storage(root),
            )
        found = _Found(
            [_grep_match_json(match) for match in result.matches],
            result.truncation_reason,
            result.skips,
        )
        page = _bounded_page(found, offset=offset, limit=max_matches)
        warnings, hints = _page_notes(found, page)
        shown = result.matches[offset - 1 : offset - 1 + len(page.items)]
        if any(match.line_truncated for match in shown):
            warnings.append(
                f"Matching lines longer than {GREP_MAX_LINE_CHARS} characters are "
                f"shown as an excerpt around their first match; "
                f"{GREP_OMITTED_TEXT_MARKER} marks omitted text."
            )
            hints.append("To see more of such a line, read it at offset=line_number.")
        if result.binary_match_paths:
            warnings.append(binary_match_warning(result.binary_match_paths))
        return {
            "status": "success",
            "root": root_id,
            "base_path": relative_base,
            "matches": page.items,
            **_page_fields(page, warnings, hints),
            "skipped_files": found.skips.unreadable,
        }

    def _root(self, root_id: str) -> ReadOnlyRoot:
        root = self._roots.get(root_id)
        if root is None:
            raise BrokerPolicyError(f"Unknown readable root: {root_id}")
        return root


def _relative_path(path: str, *, allow_dot: bool = False) -> str:
    stripped = path.strip()
    if not stripped:
        raise BrokerPolicyError("path must not be empty")
    if stripped.startswith(("/", "~")) or "\\" in stripped:
        raise BrokerPolicyError("path must be root-relative")
    normalized = PurePosixPath(stripped)
    if normalized.is_absolute() or ".." in normalized.parts:
        raise BrokerPolicyError("path must not escape its readable root")
    rendered = normalized.as_posix()
    if rendered == "." and not allow_dot:
        raise BrokerPolicyError("path must reference a file")
    return rendered


def _memory_document(root: MemoryReadRoot, relative_path: str) -> str:
    for document in root.documents:
        if document.source_path == relative_path:
            return document.content
    raise BrokerPolicyError("path is not a public file in the memory snapshot")


def _memory_list_entries(
    *,
    root: MemoryReadRoot,
    base_path: str,
    max_depth: int,
) -> _Found:
    paths = {document.source_path for document in root.documents}
    directories = {
        parent.as_posix()
        for path in paths
        for parent in PurePosixPath(path).parents
        if parent.as_posix() != "."
    }
    if base_path != "." and base_path not in directories:
        raise BrokerPolicyError("list path must reference a memory directory")
    base_parts = () if base_path == "." else PurePosixPath(base_path).parts
    entries: list[JSONValue] = []
    skips = WorkspaceScanSkips()
    for path in sorted(paths | directories):
        parts = PurePosixPath(path).parts
        if parts[: len(base_parts)] != base_parts or len(parts) <= len(base_parts):
            continue
        depth = len(parts) - len(base_parts)
        if depth > max_depth:
            continue
        if path in directories and depth == max_depth:
            skips.unexpanded_directories += 1
        entries.append(
            {
                "path": path,
                "kind": "file" if path in paths else "directory",
                "name": parts[-1],
            }
        )
    return _Found(entries, skips=skips)


def _reject_private_app_storage(root: WorkspaceReadRoot, relative_path: str) -> None:
    if is_within_any(root.canonical_path / relative_path, root.private_app_storage):
        raise BrokerPolicyError(_PRIVATE_APP_STORAGE_ERROR)


def _in_private_app_storage(root: WorkspaceReadRoot) -> Callable[[Path], bool]:
    return lambda path: is_within_any(path, root.private_app_storage)


def _workspace_list_entries(
    *,
    root: WorkspaceReadRoot,
    base_path: str,
    max_depth: int,
    limit: int,
) -> _Found:
    _reject_private_app_storage(root, base_path)
    result = scan_workspace_entries(
        root_path=root.canonical_path,
        base_path=base_path,
        max_depth=max_depth,
        limit=limit,
        exclude_subtree=_in_private_app_storage(root),
    )
    entries: list[JSONValue] = [
        {
            "path": entry.root_relative_path,
            "kind": entry.kind,
            "name": PurePosixPath(entry.root_relative_path).name,
        }
        for entry in result.entries
    ]
    return _Found(entries, result.truncation_reason, result.skips)


def _memory_glob_matches(
    *, root: MemoryReadRoot, base_path: str, pattern: str
) -> list[str]:
    prefix = "" if base_path == "." else f"{base_path.rstrip('/')}/"
    matches = [
        document.source_path
        for document in root.documents
        if document.source_path.startswith(prefix)
        and matches_workspace_glob(document.source_path[len(prefix) :], pattern)
    ]
    return sorted(matches)


def _workspace_glob_matches(
    *, root: WorkspaceReadRoot, base_path: str, pattern: str, limit: int
) -> _Found:
    _reject_private_app_storage(root, base_path)
    result = glob_workspace_files(
        root_path=root.canonical_path,
        base_path=base_path,
        pattern=pattern,
        limit=limit,
        exclude_subtree=_in_private_app_storage(root),
    )
    return _Found(
        [entry.root_relative_path for entry in result.entries],
        result.truncation_reason,
        result.skips,
    )


def _memory_grep_matches(
    *,
    root: MemoryReadRoot,
    base_path: str,
    pattern: str,
    include_glob: str | None,
) -> WorkspaceGrepScan:
    deadline = time.monotonic() + SEARCH_TIMEOUT_SECONDS
    try:
        expression = regex.compile(pattern)
    except (regex.error, RecursionError) as exc:
        raise BrokerPolicyError(f"grep pattern is invalid: {exc}") from exc
    if include_glob is not None:
        _validate_glob_pattern(include_glob)
    prefix = "" if base_path == "." else f"{base_path.rstrip('/')}/"
    matches: list[RipgrepGrepMatch] = []
    try:
        for document in sorted(root.documents, key=lambda item: item.source_path):
            if not document.source_path.startswith(prefix):
                continue
            relative = document.source_path[len(prefix) :]
            if include_glob is not None and not matches_workspace_glob(
                relative, include_glob
            ):
                continue
            lines = document.content.splitlines()
            for line_number, line in enumerate(lines, start=1):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                found = expression.search(line, timeout=remaining)
                if found is None:
                    continue
                matches.append(
                    grep_match(
                        relative_path=document.source_path,
                        line_number=line_number,
                        text=line,
                        match_start=found.start(),
                        cut=False,
                    )
                )
    except TimeoutError:
        return WorkspaceGrepScan(tuple(matches), "timeout", WorkspaceScanSkips(), ())
    return WorkspaceGrepScan(tuple(matches), None, WorkspaceScanSkips(), ())


def _grep_match_json(match: RipgrepGrepMatch) -> JSONValue:
    return {
        "path": match.relative_path,
        "line_number": match.line_number,
        "line": match.line,
    }


def _bounded_page(found: _Found, *, offset: int, limit: int) -> _Page:
    start = offset - 1
    if start < 0:
        raise ValueError("offset must be positive")
    candidates = found.items[start : start + limit]
    selected: list[JSONValue] = []
    for candidate in candidates:
        trial = [*selected, candidate]
        if len(json.dumps(trial, ensure_ascii=False)) > RESULT_CONTENT_MAX_CHARS:
            if not selected:
                raise BrokerPolicyError(
                    "single discovery result exceeds the output budget"
                )
            break
        selected.append(candidate)
    consumed = len(selected)
    has_more = found.reason == "limit" or start + consumed < len(found.items)
    next_offset = offset + consumed if has_more and consumed else None
    timed_out = found.reason == "timeout"
    return _Page(
        items=selected,
        next_offset=next_offset,
        truncated=has_more or timed_out,
        truncation_reason=(
            "timeout" if timed_out else "page_limit" if has_more else None
        ),
    )


def _page_notes(
    found: _Found, page: _Page, *, max_depth: int | None = None
) -> tuple[list[str], list[str]]:
    """Warnings and retry hints naming everything this page left out."""

    warnings, hints = scan_skip_notes(
        found.skips, max_depth=max_depth, depth_limit=LIST_MAX_DEPTH
    )
    if found.reason == "timeout":
        warnings.append(
            f"The search stopped after {SEARCH_TIMEOUT_SECONDS:g} seconds; paths "
            "it had not reached were not searched."
        )
        hints.append("Narrow base_path, pattern or include_glob to search the rest.")
    if page.next_offset is not None:
        hints.append("Continue with offset=next_offset.")
    return warnings, hints


def _page_fields(
    page: _Page, warnings: list[str], hints: list[str]
) -> dict[str, JSONValue]:
    return {
        "next_offset": page.next_offset,
        "truncated": page.truncated,
        "truncation_reason": page.truncation_reason,
        "warning": " ".join(warnings) or None,
        "retry_hint": " ".join(hints) or None,
    }


def _validate_glob_pattern(pattern: str) -> None:
    if (
        not pattern.strip()
        or pattern.startswith(("/", "~"))
        or ".." in PurePosixPath(pattern).parts
    ):
        raise BrokerPolicyError("glob pattern must be root-relative")


__all__ = ["ReadOnlyFileAccess"]
