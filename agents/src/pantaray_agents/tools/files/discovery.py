"""The list, glob and grep tools over one checked directory."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from pantaray_agents.local_runtime.tooling.documents.page_render_sandbox import (
    interpreter_read_roots,
)
from pantaray_agents.local_runtime.tooling.sandbox.seatbelt_profiles import (
    render_ripgrep_seatbelt_profile,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.schema.read_access import READ_ACCESS_SCOPE_FULL_ACCESS
from pantaray_agents.tools.contract import BrokerPolicyError

from .discovery_paths import (
    LIST_IGNORE_WORKER,
    entry_for_discovery_path,
    list_discovery_paths,
)
from .grep_lines import (
    GREP_MAX_LINE_CHARS,
    GREP_OMITTED_TEXT_MARKER,
    RIPGREP_MAX_COLUMNS,
    RipgrepGrepMatch,
    binary_match_warning,
)
from .manifest_paths import ResolvedManifestPath
from .read import open_read_target
from .read_contract import (
    DISCOVERY_RESULT_LIMIT_MAX,
    LIST_MAX_DEPTH,
    DiscoveryTruncationReason,
    GlobToolArgs,
    GrepToolArgs,
    ListToolArgs,
    ReadToolResult,
)
from .read_paths import resolve_read_path
from .read_scope import ReadScope
from .read_target import resolve_read_target
from .ripgrep import (
    RIPGREP_TIMEOUT_SECONDS,
    RipgrepGrepResult,
    ancestor_rule_files,
    run_ripgrep_files,
    run_ripgrep_grep,
)
from .workspace_descriptor_access import scan_skip_notes

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


def _resolve_directory(*, scope: ReadScope, raw_path: str) -> ResolvedManifestPath:
    return resolve_read_path(
        scope=scope,
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


def run_list(*, scope: ReadScope, request: ListToolArgs) -> ReadToolResult:
    base = _resolve_directory(scope=scope, raw_path=request.path)
    bounded = list_discovery_paths(
        base=base,
        max_depth=request.max_depth,
        limit=request.limit,
        include_path=lambda path: not scope.hides(path),
        exclude_subtree=scope.private_storage.prunes,
        sandbox_profile=_discovery_sandbox_profile(
            scope=scope,
            base=base,
            runtime_roots=(
                *interpreter_read_roots(Path(sys.executable)),
                str(LIST_IGNORE_WORKER),
            ),
        ),
    )
    entries = [entry_for_discovery_path(path) for path in bounded.selected]
    entry_values: list[JSONValue] = [_discovery_entry_json(entry) for entry in entries]
    search_text = "\n".join(str(entry["path"]) for entry in entries)
    warnings, hints = scan_skip_notes(
        bounded.skips, max_depth=request.max_depth, depth_limit=LIST_MAX_DEPTH
    )
    if bounded.truncation_reason == "limit":
        warnings.append(f"Stopped at limit={request.limit} entries; more exist.")
        hints.append(
            f"Raise limit (up to {DISCOVERY_RESULT_LIMIT_MAX}), list a narrower "
            "path, or page through one directory with read and offset."
        )
    return ReadToolResult(
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
            if base.root in scope.manifest_roots
            else ()
        ),
    )


def run_glob(*, scope: ReadScope, request: GlobToolArgs) -> ReadToolResult:
    base = _resolve_directory(scope=scope, raw_path=request.base_path)
    _reject_unsafe_glob_pattern(request.pattern, field_name="pattern")
    search_scope = scope.private_storage.search_scope(base.path)
    backend_result = run_ripgrep_files(
        cwd=base.path,
        sandbox_profile=_discovery_sandbox_profile(scope=scope, base=base),
        glob_pattern=request.pattern,
        limit=request.limit,
        follow_symlinks=False,
        pruned_relative_paths=search_scope.pruned,
        extra_search_paths=search_scope.own_roots,
        include_path=lambda path: not scope.hides(path),
    )
    relative_paths = sorted(
        _relative_path(relative_path) for relative_path in backend_result.relative_paths
    )
    absolute_paths = tuple(
        str(_backend_path(base, relative_path)) for relative_path in relative_paths
    )
    match_values: list[JSONValue] = list(relative_paths)
    warnings, hints = _glob_notes(
        reason=backend_result.truncation_reason,
        limit=request.limit,
        skipped_files=backend_result.skipped_files,
        first_skip_error=backend_result.first_skip_error,
    )
    return ReadToolResult(
        output={
            "status": "success",
            "base_path": str(base.path),
            "matches": match_values,
            "truncated": backend_result.truncated,
            "truncation_reason": backend_result.truncation_reason,
            "retry_hint": " ".join(hints) or None,
            "warning": " ".join(warnings) or None,
            "skipped_files": backend_result.skipped_files,
        },
        search_text="\n".join(absolute_paths),
        file_paths=absolute_paths,
        file_reference_paths=(
            absolute_paths if base.root in scope.manifest_roots else ()
        ),
    )


def _discovery_sandbox_profile(
    *,
    scope: ReadScope,
    base: ResolvedManifestPath,
    runtime_roots: tuple[str, ...] = (),
) -> str:
    # ripgrep and the list worker reopen what they walk by name, so only the
    # kernel's check on what they really open holds.
    full_access = scope.read_access_scope == READ_ACCESS_SCOPE_FULL_ACCESS
    storage = scope.private_storage
    folder = base.root.canonical_real_path
    return render_ripgrep_seatbelt_profile(
        read_roots=("/",) if full_access else (str(folder),),
        read_files=() if full_access else ancestor_rule_files(folder),
        runtime_roots=runtime_roots,
        private_storage_roots=tuple(str(root) for root in storage.storage_roots),
        readable_private_roots=tuple(str(root) for root in storage.readable_roots),
    )


def _backend_path(base: ResolvedManifestPath, relative_path: str) -> Path:
    return base.path / PurePosixPath(relative_path)


def _relative_path(backend_path: str) -> str:
    """A path the search backend printed, relative to base_path, without ./."""
    return PurePosixPath(backend_path).as_posix()


def _discovery_entry_json(entry: dict[str, object]) -> dict[str, JSONValue]:
    return {
        "path": str(entry["path"]),
        "kind": str(entry["kind"]),
        "name": str(entry["name"]),
    }


def _append_grep_match(
    *,
    files: dict[str, list[tuple[int, str]]],
    output_bytes: int,
    match: RipgrepGrepMatch,
) -> GrepAppendResult:
    path = _relative_path(match.relative_path)
    # A file's path is shown once, above its matching lines.
    byte_size = len(
        ("" if path in files else f"{path}\n").encode("utf-8", errors="replace")
        + f"{match.line_number}:{match.line}\n".encode("utf-8", errors="replace")
    )
    if output_bytes + byte_size > GREP_MAX_OUTPUT_BYTES:
        return GrepAppendResult(
            output_bytes=output_bytes,
            truncation_reason="output_bytes",
        )
    files.setdefault(path, []).append((match.line_number, match.line))
    return GrepAppendResult(
        output_bytes=output_bytes + byte_size,
        truncation_reason="line_length" if match.line_truncated else None,
    )


def _build_grep_outcome(
    *,
    files: dict[str, list[tuple[int, str]]],
    base: ResolvedManifestPath,
    backend_result: RipgrepGrepResult,
    max_matches: int,
    lines_excerpted: bool,
    truncation_reason: DiscoveryTruncationReason | None,
    include_file_references: bool,
) -> ReadToolResult:
    file_values: list[JSONValue] = []
    search_lines: list[str] = []
    for path in sorted(files):
        lines = sorted(files[path])
        # "line_number:line", as ripgrep prints it, which the byte budget counts.
        file_values.append(
            {"path": path, "matches": [f"{number}:{line}" for number, line in lines]}
        )
        absolute_path = _backend_path(base, path)
        search_lines.extend(
            f"{absolute_path}:{number}:{line}" for number, line in lines
        )
    absolute_paths = tuple(str(_backend_path(base, path)) for path in sorted(files))
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
    return ReadToolResult(
        output={
            "status": "success",
            "base_path": str(base.path),
            "files": file_values,
            "truncated": truncation_reason is not None,
            "truncation_reason": truncation_reason,
            "retry_hint": " ".join(retry_hints) or None,
            "warning": " ".join(warnings) or None,
            "skipped_files": backend_result.skipped_files,
        },
        search_text="\n".join(search_lines),
        file_paths=absolute_paths,
        file_reference_paths=absolute_paths if include_file_references else (),
    )


def run_grep(*, scope: ReadScope, request: GrepToolArgs) -> ReadToolResult:
    base = _resolve_directory(scope=scope, raw_path=request.base_path)
    if request.include_glob is not None:
        _reject_unsafe_glob_pattern(request.include_glob, field_name="include_glob")
    search_scope = scope.private_storage.search_scope(base.path)
    backend_result = run_ripgrep_grep(
        cwd=base.path,
        sandbox_profile=_discovery_sandbox_profile(scope=scope, base=base),
        pattern=request.pattern,
        include_glob=request.include_glob,
        max_matches=request.max_matches,
        follow_symlinks=False,
        pruned_relative_paths=search_scope.pruned,
        extra_search_paths=search_scope.own_roots,
        open_matched_file=lambda relative_path: open_read_target(
            resolve_read_target(
                scope=scope, raw_path=str(_backend_path(base, relative_path))
            )
        ),
        include_path=lambda path: not scope.hides(path),
    )
    files: dict[str, list[tuple[int, str]]] = {}
    truncation_reason: DiscoveryTruncationReason | None = (
        backend_result.truncation_reason
    )
    output_bytes = 0
    lines_excerpted = False
    for backend_match in backend_result.matches:
        append_result = _append_grep_match(
            files=files,
            output_bytes=output_bytes,
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
        files=files,
        base=base,
        backend_result=backend_result,
        max_matches=request.max_matches,
        lines_excerpted=lines_excerpted,
        truncation_reason=truncation_reason,
        include_file_references=base.root in scope.manifest_roots,
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
        hints.append(
            "To see more of such a line, read base_path/path at offset=line_number."
        )
    if skipped_files > 0:
        warnings.append(
            f"{skipped_files} path(s) could not be read and were not searched, so "
            f"matches in them are missing. First error: {first_skip_error}."
        )
    if binary_match_paths:
        warnings.append(binary_match_warning(binary_match_paths))
        hints.append("Use read on a binary file that matched, such as a PDF.")
    return warnings, hints
