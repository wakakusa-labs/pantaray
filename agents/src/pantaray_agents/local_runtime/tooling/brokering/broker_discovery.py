from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.schema.read_access import READ_ACCESS_SCOPE_FULL_ACCESS

from ..action_plan_document import ACTION_PLAN_FILENAME
from ..sandbox.seatbelt_profiles import render_ripgrep_seatbelt_profile
from .broker_common import (
    BrokerContext,
    BrokerPolicyError,
    ensure_session_capabilities,
)
from .broker_discovery_paths import (
    DiscoveryPath,
    DiscoveryTruncationReason,
    entry_for_discovery_path,
    list_discovery_paths,
)
from .broker_discovery_ripgrep import (
    RIPGREP_TIMEOUT_SECONDS,
    RipgrepGrepResult,
    run_ripgrep_files,
    run_ripgrep_grep,
)
from .broker_grep_lines import (
    GREP_MAX_LINE_CHARS,
    GREP_OMITTED_TEXT_MARKER,
    RIPGREP_MAX_COLUMNS,
    RipgrepGrepMatch,
    binary_match_warning,
)
from .broker_outcome import UnprojectedBrokerToolOutcome
from .broker_protocol import (
    DISCOVERY_RESULT_LIMIT_MAX,
    LIST_MAX_DEPTH,
    ValidatedGlobRequest,
    ValidatedGrepRequest,
    ValidatedListRequest,
)
from .manifest_paths import (
    ResolvedManifestPath,
)
from .private_app_storage import PrivateAppStorage, private_app_storage
from .tool_path_policy import (
    hidden_read_path_filter,
    resolve_read_tool_path,
)
from .workspace_descriptor_access import WorkspaceScanSkips, scan_skip_notes

GREP_MAX_OUTPUT_BYTES = 50 * 1024
TRUNCATION_REASON_PRIORITY: dict[DiscoveryTruncationReason, int] = {
    "line_length": 1,
    "limit": 2,
    "timeout": 4,
    "output_bytes": 5,
}


@dataclass(frozen=True, slots=True)
class GrepAppendResult:
    output_bytes: int
    truncation_reason: DiscoveryTruncationReason | None


def _resolve_directory(
    *,
    context: BrokerContext,
    raw_path: str,
    field_name: str,
) -> ResolvedManifestPath:
    del field_name
    return resolve_read_tool_path(
        context=context,
        raw_path=raw_path,
        must_exist=True,
        must_be_dir=True,
    )


def _reject_unsafe_glob_pattern(pattern: str, *, field_name: str) -> None:
    stripped = pattern.strip()
    if not stripped:
        raise BrokerPolicyError(f"{field_name} must not be empty")
    if stripped.startswith("/") or stripped.startswith("~"):
        raise BrokerPolicyError(f"{field_name} must be relative to base_path")
    if any(part == ".." for part in Path(stripped).parts):
        raise BrokerPolicyError(f"{field_name} must not contain parent directory parts")


def _sort_discovery_paths(paths: Iterable[DiscoveryPath]) -> list[DiscoveryPath]:
    return sorted(paths, key=lambda path: str(path.path))


def run_list_executor(
    *,
    context: BrokerContext,
    request: ValidatedListRequest,
) -> UnprojectedBrokerToolOutcome:
    ensure_session_capabilities(context=context)
    base = _resolve_directory(context=context, raw_path=request.path, field_name="path")
    is_hidden = hidden_read_path_filter(context)
    bounded = list_discovery_paths(
        base=base,
        max_depth=request.max_depth,
        limit=request.limit,
        include_path=lambda path: not is_hidden(path),
        exclude_subtree=private_app_storage(context).prunes,
    )
    entries = [entry_for_discovery_path(path) for path in bounded.selected]
    entry_values: list[JSONValue] = [_discovery_entry_json(entry) for entry in entries]
    search_text = "\n".join(str(entry["path"]) for entry in entries)
    warnings, hints = _list_notes(
        limit_reached=bounded.truncation_reason == "limit",
        limit=request.limit,
        max_depth=request.max_depth,
        skips=bounded.skips,
    )
    return UnprojectedBrokerToolOutcome(
        status="success",
        output={
            "status": "success",
            "entries": entry_values,
            "truncated": bounded.truncation_reason is not None,
            "truncation_reason": bounded.truncation_reason,
            "retry_hint": " ".join(hints) or None,
            "warning": " ".join(warnings) or None,
        },
        search_text=search_text,
        file_paths=tuple(str(entry["path"]) for entry in entries),
        file_reference_paths=(
            tuple(str(entry["path"]) for entry in entries if entry["kind"] == "file")
            if base.root in context.manifest_roots
            else ()
        ),
    )


def run_glob_executor(
    *,
    context: BrokerContext,
    request: ValidatedGlobRequest,
) -> UnprojectedBrokerToolOutcome:
    ensure_session_capabilities(context=context)
    base = _resolve_directory(
        context=context,
        raw_path=request.base_path,
        field_name="base_path",
    )
    _reject_unsafe_glob_pattern(request.pattern, field_name="pattern")
    is_hidden = hidden_read_path_filter(context)
    storage = private_app_storage(context)
    scope = storage.search_scope(base.path)
    backend_result = run_ripgrep_files(
        cwd=base.path,
        sandbox_profile=_ripgrep_sandbox_profile(
            context=context, base=base, storage=storage
        ),
        glob_pattern=request.pattern,
        limit=request.limit,
        follow_symlinks=False,
        excluded_relative_path=_private_plan_relative_to_base(
            context=context,
            base=base,
        ),
        pruned_relative_paths=scope.pruned,
        extra_search_paths=scope.own_roots,
        include_path=lambda path: not is_hidden(path),
    )
    selected = [
        DiscoveryPath(path=_backend_path(base, relative_path), kind="file")
        for relative_path in backend_result.relative_paths
    ]
    matches = [
        entry_for_discovery_path(path) for path in _sort_discovery_paths(selected)
    ]
    match_values: list[JSONValue] = [_discovery_entry_json(match) for match in matches]
    search_text = "\n".join(str(match["path"]) for match in matches)
    warnings, hints = _glob_notes(
        reason=backend_result.truncation_reason,
        limit=request.limit,
        skipped_files=backend_result.skipped_files,
        first_skip_error=backend_result.first_skip_error,
    )
    return UnprojectedBrokerToolOutcome(
        status="success",
        output={
            "status": "success",
            "matches": match_values,
            "truncated": backend_result.truncated,
            "truncation_reason": backend_result.truncation_reason,
            "retry_hint": " ".join(hints) or None,
            "warning": " ".join(warnings) or None,
            "skipped_files": backend_result.skipped_files,
        },
        search_text=search_text,
        file_paths=tuple(str(match["path"]) for match in matches),
        file_reference_paths=(
            tuple(str(match["path"]) for match in matches)
            if base.root in context.manifest_roots
            else ()
        ),
    )


def _ripgrep_sandbox_profile(
    *,
    context: BrokerContext,
    base: ResolvedManifestPath,
    storage: PrivateAppStorage,
) -> str:
    # ripgrep reopens what it walks by name, so only the kernel's check on what
    # it really opens holds; the backend's paths are reported as they come.
    full_access = context.read_access_scope == READ_ACCESS_SCOPE_FULL_ACCESS
    return render_ripgrep_seatbelt_profile(
        read_roots=("/",) if full_access else (str(base.root.canonical_real_path),),
        private_storage_roots=tuple(str(root) for root in storage.storage_roots),
        readable_private_roots=tuple(str(root) for root in storage.readable_roots),
        action_plan_path=str(context.scratch_root_path / ACTION_PLAN_FILENAME),
    )


def _backend_path(base: ResolvedManifestPath, relative_path: str) -> Path:
    return base.path / PurePosixPath(relative_path)


def _private_plan_relative_to_base(
    *,
    context: BrokerContext,
    base: ResolvedManifestPath,
) -> str | None:
    try:
        relative = (context.scratch_root_path / ACTION_PLAN_FILENAME).relative_to(
            base.path
        )
    except ValueError:
        relative = Path(ACTION_PLAN_FILENAME)
        ancestor = context.scratch_root_path
        while True:
            if ancestor.samefile(base.path):
                return relative.as_posix()
            if ancestor.parent == ancestor:
                return None
            relative = Path(ancestor.name) / relative
            ancestor = ancestor.parent
    else:
        return relative.as_posix()


def _discovery_entry_json(entry: dict[str, object]) -> dict[str, JSONValue]:
    return {
        "path": str(entry["path"]),
        "kind": str(entry["kind"]),
        "name": str(entry["name"]),
    }


def _append_grep_match(
    *,
    matches: list[dict[str, JSONValue]],
    output_bytes: int,
    base: ResolvedManifestPath,
    match: RipgrepGrepMatch,
) -> GrepAppendResult:
    path = str(_backend_path(base, match.relative_path))
    byte_size = len(
        f"{path}:{match.line_number}:{match.line}\n".encode("utf-8", errors="replace")
    )
    if output_bytes + byte_size > GREP_MAX_OUTPUT_BYTES:
        return GrepAppendResult(
            output_bytes=output_bytes,
            truncation_reason="output_bytes",
        )
    matches.append(
        {
            "path": path,
            "line_number": match.line_number,
            "line": match.line,
        }
    )
    return GrepAppendResult(
        output_bytes=output_bytes + byte_size,
        truncation_reason="line_length" if match.line_truncated else None,
    )


def _build_grep_outcome(
    *,
    matches: list[dict[str, JSONValue]],
    base: ResolvedManifestPath,
    backend_result: RipgrepGrepResult,
    max_matches: int,
    lines_excerpted: bool,
    truncation_reason: DiscoveryTruncationReason | None,
    include_file_references: bool,
) -> UnprojectedBrokerToolOutcome:
    sorted_matches = sorted(matches, key=_grep_match_sort_key)
    search_text = "\n".join(
        f"{match['path']}:{match['line_number']}:{match['line']}"
        for match in sorted_matches
    )
    match_values: list[JSONValue] = []
    for match in sorted_matches:
        match_values.append(dict(match))
    warnings, retry_hints = _grep_notes(
        reason=truncation_reason,
        max_matches=max_matches,
        lines_excerpted=lines_excerpted,
        skipped_files=backend_result.skipped_files,
        first_skip_error=backend_result.first_skip_error,
        binary_match_paths=tuple(
            str(_backend_path(base, relative_path))
            for relative_path in backend_result.binary_match_paths
        ),
    )
    return UnprojectedBrokerToolOutcome(
        status="success",
        output={
            "status": "success",
            "matches": match_values,
            "truncated": truncation_reason is not None,
            "truncation_reason": truncation_reason,
            "retry_hint": " ".join(retry_hints) or None,
            "warning": " ".join(warnings) or None,
            "skipped_files": backend_result.skipped_files,
        },
        search_text=search_text,
        file_paths=tuple(str(match["path"]) for match in sorted_matches),
        file_reference_paths=(
            tuple(str(match["path"]) for match in sorted_matches)
            if include_file_references
            else ()
        ),
    )


def _grep_match_sort_key(match: dict[str, JSONValue]) -> tuple[str, int]:
    line_number = match["line_number"]
    return (
        str(match["path"]),
        line_number
        if isinstance(line_number, int) and not isinstance(line_number, bool)
        else 0,
    )


def run_grep_executor(
    *,
    context: BrokerContext,
    request: ValidatedGrepRequest,
) -> UnprojectedBrokerToolOutcome:
    ensure_session_capabilities(context=context)
    base = _resolve_directory(
        context=context,
        raw_path=request.base_path,
        field_name="base_path",
    )
    if request.include_glob is not None:
        _reject_unsafe_glob_pattern(request.include_glob, field_name="include_glob")
    is_hidden = hidden_read_path_filter(context)
    storage = private_app_storage(context)
    scope = storage.search_scope(base.path)
    backend_result = run_ripgrep_grep(
        cwd=base.path,
        sandbox_profile=_ripgrep_sandbox_profile(
            context=context, base=base, storage=storage
        ),
        pattern=request.pattern,
        include_glob=request.include_glob,
        max_matches=request.max_matches,
        follow_symlinks=False,
        excluded_relative_path=_private_plan_relative_to_base(
            context=context,
            base=base,
        ),
        pruned_relative_paths=scope.pruned,
        extra_search_paths=scope.own_roots,
        include_path=lambda path: not is_hidden(path),
    )
    matches: list[dict[str, JSONValue]] = []
    truncation_reason: DiscoveryTruncationReason | None = (
        backend_result.truncation_reason
    )
    output_bytes = 0
    lines_excerpted = False
    for backend_match in backend_result.matches:
        append_result = _append_grep_match(
            matches=matches,
            output_bytes=output_bytes,
            base=base,
            match=backend_match,
        )
        output_bytes = append_result.output_bytes
        truncation_reason = _dominant_truncation_reason(
            truncation_reason,
            append_result.truncation_reason,
        )
        if append_result.truncation_reason == "output_bytes":
            break
        lines_excerpted = lines_excerpted or backend_match.line_truncated
    return _build_grep_outcome(
        matches=matches,
        base=base,
        backend_result=backend_result,
        max_matches=request.max_matches,
        lines_excerpted=lines_excerpted,
        truncation_reason=truncation_reason,
        include_file_references=base.root in context.manifest_roots,
    )


def _dominant_truncation_reason(
    current: DiscoveryTruncationReason | None,
    candidate: DiscoveryTruncationReason | None,
) -> DiscoveryTruncationReason | None:
    if candidate is None:
        return current
    if current is None:
        return candidate
    return (
        candidate
        if TRUNCATION_REASON_PRIORITY[candidate] > TRUNCATION_REASON_PRIORITY[current]
        else current
    )


def _list_notes(
    *,
    limit_reached: bool,
    limit: int,
    max_depth: int,
    skips: WorkspaceScanSkips,
) -> tuple[list[str], list[str]]:
    """Warnings and retry hints naming everything list left out."""

    warnings, hints = scan_skip_notes(
        skips, max_depth=max_depth, depth_limit=LIST_MAX_DEPTH
    )
    if limit_reached:
        warnings.insert(0, f"Stopped at limit={limit} entries; more entries exist.")
        hints.insert(
            0,
            f"Raise limit (up to {DISCOVERY_RESULT_LIMIT_MAX}) or list a narrower "
            "path; to page through one directory, read it with offset.",
        )
    return warnings, hints


def _glob_notes(
    *,
    reason: DiscoveryTruncationReason | None,
    limit: int,
    skipped_files: int,
    first_skip_error: str | None,
) -> tuple[list[str], list[str]]:
    """Warnings and retry hints naming every glob limit that applied."""

    warnings: list[str] = []
    hints: list[str] = []
    if reason == "limit":
        warnings.append(f"Stopped at limit={limit} matches; more files match.")
        hints.append(
            f"Raise limit (up to {DISCOVERY_RESULT_LIMIT_MAX}) or narrow base_path "
            "or pattern to see the rest."
        )
    elif reason == "output_bytes":
        warnings.append(
            "Stopped at the search backend's output limit; more files may match."
        )
        hints.append("Narrow base_path or pattern to see the rest.")
    elif reason == "timeout":
        warnings.append(
            f"The search stopped after {RIPGREP_TIMEOUT_SECONDS:g} seconds; "
            "directories it had not reached were not searched."
        )
        hints.append("Narrow base_path to search the rest.")
    if skipped_files > 0:
        warnings.append(
            f"{skipped_files} path(s) could not be read and were not searched, so "
            f"files in them are missing. First error: {first_skip_error}."
        )
    return warnings, hints


def _grep_notes(
    *,
    reason: DiscoveryTruncationReason | None,
    max_matches: int,
    lines_excerpted: bool,
    skipped_files: int,
    first_skip_error: str | None,
    binary_match_paths: tuple[str, ...],
) -> tuple[list[str], list[str]]:
    """Warnings and retry hints naming every grep limit that applied."""

    warnings: list[str] = []
    hints: list[str] = []
    if reason == "limit":
        warnings.append(
            f"Stopped at max_matches={max_matches} matching lines; more matches exist."
        )
        hints.append(
            "Raise max_matches (up to 500) or narrow base_path, include_glob, or "
            "pattern to see the rest."
        )
    elif reason == "output_bytes":
        warnings.append(
            f"Stopped at the {GREP_MAX_OUTPUT_BYTES // 1024} KB output limit; "
            "more matches may exist."
        )
        hints.append("Narrow base_path, include_glob, or pattern to see the rest.")
    elif reason == "timeout":
        warnings.append(
            f"The search stopped after {RIPGREP_TIMEOUT_SECONDS:g} seconds; files "
            "it had not reached were not searched."
        )
        hints.append("Narrow base_path or include_glob to search the rest.")
    if lines_excerpted:
        warnings.append(
            f"Matching lines longer than {GREP_MAX_LINE_CHARS} characters are shown "
            "as an excerpt around their first match, or from the line's start when "
            f"that match is over {RIPGREP_MAX_COLUMNS // 1024} KB into the line; "
            f"{GREP_OMITTED_TEXT_MARKER} marks omitted text."
        )
        hints.append("To see more of such a line, read the file at offset=line_number.")
    if skipped_files > 0:
        warnings.append(
            f"{skipped_files} path(s) could not be read and were not searched, so "
            f"matches in them are missing. First error: {first_skip_error}."
        )
    if binary_match_paths:
        warnings.append(binary_match_warning(binary_match_paths))
        hints.append("Use read on a binary file that matched, such as a PDF.")
    return warnings, hints
