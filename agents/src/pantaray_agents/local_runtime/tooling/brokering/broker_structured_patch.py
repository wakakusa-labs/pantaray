from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from difflib import unified_diff
from pathlib import Path
from typing import Literal

from pantaray_agents.local_runtime.descriptor_access import (
    DescriptorPathError,
    open_regular_file_at_descriptor,
)
from pantaray_agents.local_runtime.descriptor_write import (
    CommittedFileWriteError,
    open_writable_parent_descriptor,
    write_text_at_descriptor,
)
from pantaray_agents.local_runtime.tooling.brokering.broker_patch_matching import (
    PatchEditMatch,
    PatchMatchFailure,
    PatchMatchFailureReason,
    find_patch_edit_match,
)
from pantaray_agents.local_runtime.tooling.brokering.broker_protocol import (
    ApplyPatchAddChange,
    ApplyPatchChange,
    ApplyPatchDeleteChange,
    ApplyPatchEdit,
    ApplyPatchUpdateChange,
)
from pantaray_agents.tools.contract import BrokerPolicyError
from pantaray_agents.tools.files.workspace_descriptor_access import (
    WorkspacePathMissingError,
    open_workspace_file_descriptor,
)

PATCH_ERROR_PATH_CONFLICT = "PATCH_PATH_CONFLICT"
PATCH_ERROR_PATH_INVALID = "PATCH_PATH_INVALID"
PATCH_ERROR_TARGET_EXISTS = "PATCH_TARGET_EXISTS"
PATCH_ERROR_TARGET_MISSING = "PATCH_TARGET_MISSING"
PATCH_ERROR_TARGET_NOT_FILE = "PATCH_TARGET_NOT_FILE"
PATCH_ERROR_CONTEXT_NOT_FOUND = "PATCH_CONTEXT_NOT_FOUND"
PATCH_ERROR_CONTEXT_AMBIGUOUS = "PATCH_CONTEXT_AMBIGUOUS"
PATCH_ERROR_EDIT_ORDER_INVALID = "PATCH_EDIT_ORDER_INVALID"
PATCH_ERROR_LINE_PATTERN_INVALID = "PATCH_LINE_PATTERN_INVALID"
PATCH_ERROR_DELETE_REQUIRES_FULL_FILE_READ = "PATCH_DELETE_REQUIRES_FULL_FILE_READ"
PATCH_ERROR_READ_WINDOW_TOO_LARGE = "PATCH_READ_WINDOW_TOO_LARGE"
PATCH_ERROR_READ_FAILED = "PATCH_READ_FAILED"
PATCH_ERROR_WRITE_FAILED = "PATCH_WRITE_FAILED"


class StructuredPatchError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        applied_paths: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.applied_paths = applied_paths


@dataclass(frozen=True, slots=True)
class StructuredPatchFileChange:
    operation: Literal["add", "update", "delete"]
    path: str
    old_text: str
    new_text: str


def extract_structured_patch_paths(
    changes: list[ApplyPatchChange],
) -> tuple[str, ...]:
    paths: list[str] = []
    seen_raw_paths: set[str] = set()
    for change in changes:
        path = _validate_structured_patch_path(change.path)
        if path in seen_raw_paths:
            raise BrokerPolicyError(
                f"{PATCH_ERROR_PATH_CONFLICT}: duplicate apply_patch path: {path}",
                code=PATCH_ERROR_PATH_CONFLICT,
            )
        seen_raw_paths.add(path)
        paths.append(path)
    return tuple(paths)


def apply_structured_workspace_patch(
    *,
    patch_root: Path,
    path_rewrites: dict[str, str],
    changes: list[ApplyPatchChange],
) -> str:
    file_changes = _derive_file_changes(
        patch_root=patch_root,
        path_rewrites=path_rewrites,
        changes=changes,
    )
    return _apply_file_changes(
        patch_root=patch_root,
        path_rewrites=path_rewrites,
        changes=file_changes,
    )


def read_structured_patch_target_text(
    *,
    patch_root: Path,
    path_rewrites: dict[str, str],
    path: str,
) -> str:
    return _read_existing_file(
        patch_root=patch_root,
        path_rewrites=path_rewrites,
        path=path,
    )


def structured_patch_llm_feedback(code: str) -> str:
    if code == PATCH_ERROR_CONTEXT_NOT_FOUND:
        return (
            "PATCH_CONTEXT_NOT_FOUND: re-read the target file and retry with "
            "old_lines copied from the current file. Use before_lines/after_lines "
            "only as nearby location hints when old_lines is ambiguous."
        )
    if code == PATCH_ERROR_CONTEXT_AMBIGUOUS:
        return (
            "PATCH_CONTEXT_AMBIGUOUS: old_lines matched multiple locations. "
            "Retry with before_lines or after_lines copied from nearby current "
            "file lines to select exactly one place."
        )
    if code == PATCH_ERROR_EDIT_ORDER_INVALID:
        return (
            "PATCH_EDIT_ORDER_INVALID: edits must be ordered from top to bottom "
            "as they apply to the evolving file content."
        )
    if code == PATCH_ERROR_LINE_PATTERN_INVALID:
        return (
            "PATCH_LINE_PATTERN_INVALID: use <OMITTED> at most once per existing "
            "line, with non-empty prefix and suffix copied from the same line."
        )
    if code == PATCH_ERROR_DELETE_REQUIRES_FULL_FILE_READ:
        return (
            "PATCH_DELETE_REQUIRES_FULL_FILE_READ: deleting a large file requires "
            "full-file visibility, which apply_patch cannot return for this target."
        )
    if code == PATCH_ERROR_READ_WINDOW_TOO_LARGE:
        return (
            "PATCH_READ_WINDOW_TOO_LARGE: the required patch line exceeds the "
            "bounded read limit. Split the target line before retrying."
        )
    if code == PATCH_ERROR_TARGET_EXISTS:
        return "PATCH_TARGET_EXISTS: use update for existing files."
    if code == PATCH_ERROR_TARGET_MISSING:
        return (
            "PATCH_TARGET_MISSING: re-read the workspace and target an existing file."
        )
    if code == PATCH_ERROR_TARGET_NOT_FILE:
        return "PATCH_TARGET_NOT_FILE: apply_patch can only update or delete files."
    if code == PATCH_ERROR_READ_FAILED:
        return "PATCH_READ_FAILED: re-read the target paths after they are readable."
    if code == PATCH_ERROR_WRITE_FAILED:
        return "PATCH_WRITE_FAILED: read the target file to confirm its current content before retrying."
    return "Regenerate the apply_patch request using the current file content."


def _validate_structured_patch_path(path: str) -> str:
    normalized = path.strip()
    if not normalized:
        raise BrokerPolicyError(
            f"{PATCH_ERROR_PATH_INVALID}: apply_patch path must not be empty",
            code=PATCH_ERROR_PATH_INVALID,
        )
    if normalized != path:
        raise BrokerPolicyError(
            f"{PATCH_ERROR_PATH_INVALID}: apply_patch path must not have leading or trailing whitespace",
            code=PATCH_ERROR_PATH_INVALID,
        )
    if normalized.startswith("~"):
        raise BrokerPolicyError(
            f"{PATCH_ERROR_PATH_INVALID}: apply_patch path must not use '~'",
            code=PATCH_ERROR_PATH_INVALID,
        )
    return normalized


def _derive_file_changes(
    *,
    patch_root: Path,
    path_rewrites: dict[str, str],
    changes: list[ApplyPatchChange],
) -> tuple[StructuredPatchFileChange, ...]:
    derived: list[StructuredPatchFileChange] = []
    for change in changes:
        if isinstance(change, ApplyPatchAddChange):
            derived.append(
                _derive_add_change(
                    patch_root=patch_root,
                    path_rewrites=path_rewrites,
                    change=change,
                )
            )
            continue
        if isinstance(change, ApplyPatchUpdateChange):
            derived.append(
                _derive_update_change(
                    patch_root=patch_root,
                    path_rewrites=path_rewrites,
                    change=change,
                )
            )
            continue
        if isinstance(change, ApplyPatchDeleteChange):
            derived.append(
                _derive_delete_change(
                    patch_root=patch_root,
                    path_rewrites=path_rewrites,
                    change=change,
                )
            )
            continue
    return tuple(derived)


def _derive_add_change(
    *,
    patch_root: Path,
    path_rewrites: dict[str, str],
    change: ApplyPatchAddChange,
) -> StructuredPatchFileChange:
    destination = _destination_for_change(
        patch_root=patch_root,
        path_rewrites=path_rewrites,
        path=change.path,
    )
    if destination.exists():
        raise StructuredPatchError(
            f"{PATCH_ERROR_TARGET_EXISTS}: target already exists: {change.path}",
            code=PATCH_ERROR_TARGET_EXISTS,
        )
    return StructuredPatchFileChange(
        operation="add",
        path=change.path,
        old_text="",
        new_text=join_patch_new_file_text(
            lines=change.new_lines,
            trailing_newline=change.trailing_newline,
        ),
    )


def _derive_update_change(
    *,
    patch_root: Path,
    path_rewrites: dict[str, str],
    change: ApplyPatchUpdateChange,
) -> StructuredPatchFileChange:
    old_text = _read_existing_file(
        patch_root=patch_root,
        path_rewrites=path_rewrites,
        path=change.path,
    )
    return StructuredPatchFileChange(
        operation="update",
        path=change.path,
        old_text=old_text,
        new_text=apply_patch_edits(old_text=old_text, edits=change.edits),
    )


def _derive_delete_change(
    *,
    patch_root: Path,
    path_rewrites: dict[str, str],
    change: ApplyPatchDeleteChange,
) -> StructuredPatchFileChange:
    old_text = _read_existing_file(
        patch_root=patch_root,
        path_rewrites=path_rewrites,
        path=change.path,
    )
    return StructuredPatchFileChange(
        operation="delete",
        path=change.path,
        old_text=old_text,
        new_text="",
    )


def _destination_for_change(
    *,
    patch_root: Path,
    path_rewrites: dict[str, str],
    path: str,
) -> Path:
    return patch_root / path_rewrites[path]


def _read_existing_file(
    *,
    patch_root: Path,
    path_rewrites: dict[str, str],
    path: str,
) -> str:
    descriptor: int | None = None
    try:
        descriptor = open_workspace_file_descriptor(
            root_path=patch_root,
            relative_path=path_rewrites[path],
        )
        with open(descriptor, encoding="utf-8", closefd=False) as handle:
            return handle.read()
    except WorkspacePathMissingError as exc:
        raise StructuredPatchError(
            f"{PATCH_ERROR_TARGET_MISSING}: target file does not exist: {path}",
            code=PATCH_ERROR_TARGET_MISSING,
        ) from exc
    except BrokerPolicyError as exc:
        raise StructuredPatchError(
            f"{PATCH_ERROR_TARGET_NOT_FILE}: target is not a file: {path}",
            code=PATCH_ERROR_TARGET_NOT_FILE,
        ) from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise StructuredPatchError(
            f"{PATCH_ERROR_READ_FAILED}: failed to read target {path}: {exc}",
            code=PATCH_ERROR_READ_FAILED,
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def apply_patch_edits(*, old_text: str, edits: list[ApplyPatchEdit]) -> str:
    current_lines, trailing_newline = _split_text(old_text)
    cursor = 0
    for edit in edits:
        match = _find_edit_match(current_lines=current_lines, edit=edit, cursor=cursor)
        current_lines = (
            current_lines[: match.old_start]
            + edit.new_lines
            + current_lines[match.old_end :]
        )
        cursor = match.old_start + len(edit.new_lines)
    return _join_existing_file_text(current_lines, trailing_newline=trailing_newline)


def _split_text(text: str) -> tuple[list[str], bool]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if normalized == "":
        return [], False
    trailing_newline = normalized.endswith("\n")
    if trailing_newline:
        normalized = normalized[:-1]
    return normalized.split("\n") if normalized else [], trailing_newline


def _join_existing_file_text(lines: list[str], *, trailing_newline: bool) -> str:
    if not lines:
        return ""
    return "\n".join(lines) + ("\n" if trailing_newline else "")


def join_patch_new_file_text(*, lines: list[str], trailing_newline: bool) -> str:
    if not lines:
        return "\n" if trailing_newline else ""
    return "\n".join(lines) + ("\n" if trailing_newline else "")


def _find_edit_match(
    *,
    current_lines: list[str],
    edit: ApplyPatchEdit,
    cursor: int,
) -> PatchEditMatch:
    try:
        return find_patch_edit_match(
            current_lines=current_lines,
            before_lines=edit.before_lines,
            old_lines=edit.old_lines,
            after_lines=edit.after_lines,
            cursor=cursor,
        )
    except PatchMatchFailure as exc:
        raise StructuredPatchError(
            _format_patch_match_failure(exc),
            code=_patch_match_failure_code(exc.reason),
        ) from exc


def _patch_match_failure_code(reason: PatchMatchFailureReason) -> str:
    if reason == PatchMatchFailureReason.CONTEXT_NOT_FOUND:
        return PATCH_ERROR_CONTEXT_NOT_FOUND
    if reason == PatchMatchFailureReason.CONTEXT_AMBIGUOUS:
        return PATCH_ERROR_CONTEXT_AMBIGUOUS
    if reason == PatchMatchFailureReason.EDIT_ORDER_INVALID:
        return PATCH_ERROR_EDIT_ORDER_INVALID
    if reason == PatchMatchFailureReason.LINE_PATTERN_INVALID:
        return PATCH_ERROR_LINE_PATTERN_INVALID


def _format_patch_match_failure(error: PatchMatchFailure) -> str:
    code = _patch_match_failure_code(error.reason)
    return f"{code}: {error}"


def _apply_file_changes(
    *,
    patch_root: Path,
    path_rewrites: dict[str, str],
    changes: tuple[StructuredPatchFileChange, ...],
) -> str:
    if len(changes) != 1:
        raise StructuredPatchError(
            f"{PATCH_ERROR_PATH_CONFLICT}: apply_patch accepts exactly one file change",
            code=PATCH_ERROR_PATH_CONFLICT,
        )
    diff_text = "".join(create_patch_diff(change) for change in changes)
    change = changes[0]
    try:
        _apply_single_change(
            patch_root=patch_root,
            path_rewrites=path_rewrites,
            change=change,
        )
    except (DescriptorPathError, OSError) as exc:
        committed = isinstance(exc, CommittedFileWriteError)
        effect = (
            "The file changed; read it before retrying."
            if committed
            else "No file changes were kept."
        )
        raise StructuredPatchError(
            f"{PATCH_ERROR_WRITE_FAILED}: failed to apply change at path {change.path}: {exc}. {effect}",
            code=PATCH_ERROR_WRITE_FAILED,
            applied_paths=(change.path,) if committed else (),
        ) from exc
    return diff_text


def create_patch_diff(change: StructuredPatchFileChange) -> str:
    return "".join(
        unified_diff(
            change.old_text.splitlines(keepends=True),
            change.new_text.splitlines(keepends=True),
            fromfile=change.path,
            tofile=change.path,
        )
    )


def _apply_single_change(
    *,
    patch_root: Path,
    path_rewrites: dict[str, str],
    change: StructuredPatchFileChange,
) -> None:
    parent_descriptor, destination_name = open_writable_parent_descriptor(
        root_path=patch_root,
        relative_path=path_rewrites[change.path],
        create_missing=change.operation == "add",
    )
    try:
        if change.operation == "delete":
            os.unlink(destination_name, dir_fd=parent_descriptor)
            return
        replacement_mode = (
            _read_current_file_mode(
                parent_descriptor=parent_descriptor,
                destination_name=destination_name,
            )
            if change.operation == "update"
            else None
        )
        write_text_at_descriptor(
            parent_descriptor=parent_descriptor,
            destination_name=destination_name,
            text=change.new_text,
            replacement_mode=replacement_mode,
            exclusive=change.operation == "add",
        )
    finally:
        os.close(parent_descriptor)


def _read_current_file_mode(*, parent_descriptor: int, destination_name: str) -> int:
    descriptor = open_regular_file_at_descriptor(
        parent_descriptor=parent_descriptor,
        relative_path=destination_name,
    )
    try:
        return stat.S_IMODE(os.fstat(descriptor).st_mode)
    finally:
        os.close(descriptor)
