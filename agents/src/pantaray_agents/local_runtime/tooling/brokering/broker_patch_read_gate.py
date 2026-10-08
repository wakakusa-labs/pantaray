from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from difflib import SequenceMatcher
from hashlib import sha256
from typing import cast

from pantaray_agents.local_runtime.storage.migrations import MigrationError
from pantaray_agents.local_runtime.tooling.brokering.broker_common import BrokerContext
from pantaray_agents.local_runtime.tooling.brokering.broker_patch_matching import (
    PatchMatchFailure,
    find_patch_edit_location,
)
from pantaray_agents.local_runtime.tooling.brokering.broker_protocol import (
    ApplyPatchChange,
    ApplyPatchDeleteChange,
    ApplyPatchEdit,
    ApplyPatchUpdateChange,
)
from pantaray_agents.local_runtime.tooling.read_windows import (
    READ_WINDOW_MAX_BYTES,
    ReadWindow,
    ReadWindowCandidate,
    ReadWindowTooLargeError,
    build_bounded_read_windows,
)
from pantaray_agents.local_runtime.tooling.repository import _configure_connection
from pantaray_agents.local_runtime.tooling.tool_result_storage import (
    ToolResultLoadError,
    ToolResultReadLimitError,
    load_action_file_json_result,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tools.files.manifest_paths import (
    resolve_tool_results_root,
)

from .broker_structured_patch import (
    PATCH_ERROR_DELETE_REQUIRES_FULL_FILE_READ,
    PATCH_ERROR_READ_WINDOW_TOO_LARGE,
    StructuredPatchError,
)

FULL_READ_MAX_LINES = 800
WINDOW_MARGIN_LINES = 40
MUTATING_TOOL_IDS = ("apply_patch", "bash", "run_python")
JSON_CONTROL_ESCAPE_MAX_EXPANSION = 6
PATCH_READ_SNAPSHOT_ENVELOPE_MAX_BYTES = 16 * 1024
# Design limit: needs_read text is capped at 40 KB. Allow worst-case JSON control
# escaping plus the fixed envelope; raise this only if that bounded schema grows.
PATCH_READ_SNAPSHOT_MAX_JSON_BYTES = (
    READ_WINDOW_MAX_BYTES * JSON_CONTROL_ESCAPE_MAX_EXPANSION
    + PATCH_READ_SNAPSHOT_ENVELOPE_MAX_BYTES
)


@dataclass(frozen=True, slots=True)
class PatchReadSnapshot:
    path: str
    file_sha256: str
    full_file: bool
    visible_texts: tuple[str, ...]


def text_sha256(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def build_needs_read_output(
    *,
    path: str,
    text: str,
    change: ApplyPatchChange,
) -> dict[str, JSONValue]:
    file_sha256 = text_sha256(text)
    if fits_full_file_read(text):
        return {
            "status": "needs_read",
            "applied_paths": [],
            "path": path,
            "patch_applied": False,
            "read_scope": "full_file",
            "file_truncated": False,
            "text": text,
            "windows": [],
            "file_sha256": file_sha256,
            "llm_feedback": (
                "Patch was not applied. Rebuild the apply_patch request using "
                "only this text, then retry."
            ),
        }

    windows = _target_windows(text=text, change=change)
    return {
        "status": "needs_read",
        "applied_paths": [],
        "path": path,
        "patch_applied": False,
        "read_scope": "target_windows",
        "file_truncated": True,
        "text": "",
        "windows": [
            {
                "start_line": window.start_line,
                "end_line": window.end_line,
                "match_reason": window.match_reason,
                "text": window.text,
            }
            for window in windows
        ],
        "file_sha256": file_sha256,
        "llm_feedback": (
            "Patch was not applied. Rebuild old_lines, before_lines, and "
            "after_lines using only the returned window text, then retry."
        ),
    }


def needs_read_output(
    *,
    context: BrokerContext,
    path: str,
    current_text: str,
    change: ApplyPatchChange,
) -> dict[str, JSONValue] | None:
    snapshot = latest_valid_patch_snapshot(
        context=context,
        path=path,
        current_text=current_text,
    )
    if snapshot is not None and change_uses_visible_lines(
        change=change, snapshot=snapshot
    ):
        return None
    if isinstance(change, ApplyPatchDeleteChange) and not fits_full_file_read(
        current_text
    ):
        raise StructuredPatchError(
            f"{PATCH_ERROR_DELETE_REQUIRES_FULL_FILE_READ}: delete requires full-file visibility before removing {change.path}",
            code=PATCH_ERROR_DELETE_REQUIRES_FULL_FILE_READ,
        )
    try:
        return build_needs_read_output(path=path, text=current_text, change=change)
    except ReadWindowTooLargeError as exc:
        raise StructuredPatchError(
            f"{PATCH_ERROR_READ_WINDOW_TOO_LARGE}: {exc}",
            code=PATCH_ERROR_READ_WINDOW_TOO_LARGE,
        ) from exc


def latest_valid_patch_snapshot(
    *,
    context: BrokerContext,
    path: str,
    current_text: str,
) -> PatchReadSnapshot | None:
    latest = _latest_mutating_tool_output(context=context)
    if latest is None:
        return None
    tool_id, output = latest
    if tool_id != "apply_patch":
        return None
    if output.get("status") != "needs_read":
        return None
    if output.get("path") != path:
        return None
    if output.get("file_sha256") != text_sha256(current_text):
        return None
    visible_texts = _visible_texts_from_output(output)
    return PatchReadSnapshot(
        path=path,
        file_sha256=str(output["file_sha256"]),
        full_file=output.get("read_scope") == "full_file",
        visible_texts=visible_texts,
    )


def change_uses_visible_lines(
    *,
    change: ApplyPatchChange,
    snapshot: PatchReadSnapshot,
) -> bool:
    if snapshot.full_file:
        return True
    if isinstance(change, ApplyPatchDeleteChange):
        return False
    if not isinstance(change, ApplyPatchUpdateChange):
        return True
    return all(
        _edit_visible_in_any_text(edit=edit, visible_texts=snapshot.visible_texts)
        for edit in change.edits
    )


def _latest_mutating_tool_output(
    *,
    context: BrokerContext,
) -> tuple[str, dict[str, JSONValue]] | None:
    placeholders = ", ".join("?" for _tool_id in MUTATING_TOOL_IDS)
    with sqlite3.connect(context.db_path) as connection:
        connection.row_factory = sqlite3.Row
        _configure_connection(
            connection=connection,
            busy_timeout_ms=context.busy_timeout_ms,
        )
        row = connection.execute(
            f"""
            SELECT
                ti.invocation_id,
                ti.tool_id,
                out.output_json,
                out.output_storage_kind
            FROM tool_invocations ti
            LEFT JOIN tool_outputs out ON out.invocation_id = ti.invocation_id
            WHERE ti.execution_session_id = ?
              AND ti.tool_id IN ({placeholders})
              AND ti.completed_at IS NOT NULL
            ORDER BY ti.completed_at DESC, ti.started_at DESC, ti.invocation_id DESC
            LIMIT 1
            """,
            (context.execution_session.execution_session_id, *MUTATING_TOOL_IDS),
        ).fetchone()
    if row is None:
        return None
    tool_id = str(row["tool_id"])
    if tool_id != "apply_patch":
        return (tool_id, {})
    storage_kind = row["output_storage_kind"]
    raw_output = row["output_json"]
    if storage_kind is None and raw_output is None:
        return (tool_id, {})
    if not isinstance(raw_output, str) or not raw_output:
        raise MigrationError("mutating tool output has an invalid storage contract")
    try:
        projected = json.loads(raw_output)
    except json.JSONDecodeError as exc:
        raise MigrationError("mutating tool output contains invalid JSON") from exc
    if storage_kind == "action_file":
        action_id = context.execution_session.action_id
        if action_id is None:
            raise MigrationError("execution session is not bound to an action")
        try:
            parsed = load_action_file_json_result(
                action_tool_results_path=resolve_tool_results_root(
                    roots=context.manifest_roots,
                    action_id=action_id,
                ),
                invocation_id=str(row["invocation_id"]),
                metadata=cast(JSONValue, projected),
                max_bytes=PATCH_READ_SNAPSHOT_MAX_JSON_BYTES,
            )
        except ToolResultReadLimitError:
            return (tool_id, {})
        except ToolResultLoadError as exc:
            raise MigrationError("mutating tool action-file output is invalid") from exc
    elif storage_kind == "inline_json":
        parsed = projected
    else:
        raise MigrationError("mutating tool output has an invalid storage contract")
    if not isinstance(parsed, dict):
        return (tool_id, {})
    return (tool_id, parsed)


def _visible_texts_from_output(output: dict[str, JSONValue]) -> tuple[str, ...]:
    if output.get("read_scope") == "full_file":
        text = output.get("text")
        return (text,) if isinstance(text, str) else ()
    windows = output.get("windows")
    if not isinstance(windows, list):
        return ()
    visible_texts: list[str] = []
    for window in windows:
        if not isinstance(window, dict):
            continue
        text = window.get("text")
        if isinstance(text, str):
            visible_texts.append(text)
    return tuple(visible_texts)


def fits_full_file_read(text: str) -> bool:
    return (
        len(text.encode("utf-8")) <= READ_WINDOW_MAX_BYTES
        and len(_line_texts(text)) <= FULL_READ_MAX_LINES
    )


def _target_windows(*, text: str, change: ApplyPatchChange) -> tuple[ReadWindow, ...]:
    line_texts = _line_texts(text)
    if not line_texts:
        return (ReadWindow(1, 1, "empty_file", ""),)
    start, end, reason = _target_candidate(line_texts=line_texts, change=change)
    return build_bounded_read_windows(
        text=text,
        candidates=(ReadWindowCandidate(start, end, reason),),
        margin_lines=WINDOW_MARGIN_LINES,
    )


def _target_candidate(
    *,
    line_texts: tuple[str, ...],
    change: ApplyPatchChange,
) -> tuple[int, int, str]:
    if isinstance(change, ApplyPatchDeleteChange):
        return (0, min(len(line_texts) - 1, WINDOW_MARGIN_LINES), "file_start")
    if not isinstance(change, ApplyPatchUpdateChange):
        return (0, 0, "file_start")
    edit = change.edits[0]
    try:
        location = find_patch_edit_location(
            current_lines=list(line_texts),
            before_lines=edit.before_lines,
            old_lines=edit.old_lines,
            after_lines=edit.after_lines,
            cursor=0,
        )
    except PatchMatchFailure:
        location = None
    if location is not None:
        return (
            location.visible_start,
            max(location.visible_start, location.visible_end - 1),
            "exact_edit_location_match",
        )
    fallback = next(iter(_edit_existing_patterns(edit)), "")
    fuzzy_index = _find_fuzzy_line(line_texts=line_texts, expected=fallback)
    return (fuzzy_index, fuzzy_index, "nearest_patch_line_match")


def _find_fuzzy_line(*, line_texts: tuple[str, ...], expected: str) -> int:
    best_index = 0
    best_ratio = -1.0
    for index, line in enumerate(line_texts):
        ratio = SequenceMatcher(None, expected, line).ratio()
        if ratio > best_ratio:
            best_index = index
            best_ratio = ratio
    return best_index


def _edit_existing_patterns(edit: ApplyPatchEdit) -> tuple[str, ...]:
    return tuple(edit.before_lines + edit.old_lines + edit.after_lines)


def _edit_visible_in_any_text(
    *,
    edit: ApplyPatchEdit,
    visible_texts: tuple[str, ...],
) -> bool:
    return any(
        _edit_visible_in_text(edit=edit, line_texts=_line_texts(visible_text))
        for visible_text in visible_texts
    )


def _edit_visible_in_text(
    *,
    edit: ApplyPatchEdit,
    line_texts: tuple[str, ...],
) -> bool:
    try:
        find_patch_edit_location(
            current_lines=list(line_texts),
            before_lines=edit.before_lines,
            old_lines=edit.old_lines,
            after_lines=edit.after_lines,
            cursor=0,
        )
    except PatchMatchFailure:
        return False
    return True


def _line_texts(text: str) -> tuple[str, ...]:
    return tuple(
        segment.removesuffix("\n").removesuffix("\r")
        for segment in _line_segments(text)
    )


def _line_segments(text: str) -> tuple[str, ...]:
    if text == "":
        return ()
    return tuple(
        text.replace("\r\n", "\n").replace("\r", "\n").splitlines(keepends=True)
    )
