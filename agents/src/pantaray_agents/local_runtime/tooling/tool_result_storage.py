from __future__ import annotations

import os
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, NotRequired, TypedDict, cast
from urllib.parse import quote

from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.schema.tool_result import (
    UnprojectedToolOutput,
    largest_fitting_size,
    serialize_json_tool_output,
)
from pantaray_agents.tools.files.text_lines import count_text_lines

from .models import ToolOutputStorageKind
from .tool_result_spill import outline_previewer, spill_long_texts

TOOL_RESULT_JSON_MEDIA_TYPE: Literal["application/json"] = "application/json"
TOOL_RESULT_BINARY_MEDIA_TYPE: Literal["application/octet-stream"] = (
    "application/octet-stream"
)
ACTION_TOOL_RESULTS_DIRNAME = "tool-results"
ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT = 20_000
TOOL_RESULT_FILENAME_PREFIX = "output-"
TOOL_RESULT_JSON_SUFFIX = ".json"
TOOL_RESULT_BINARY_SUFFIX = ".bin"
TOOL_RESULT_TEXT_SUFFIX = ".txt"
_RUN_SPILL_ROOT_MODE = 0o700


class ActionFileToolResultMetadata(TypedDict):
    storage: Literal["action_file"]
    path: str
    media_type: Literal["application/json"]
    byte_size: int
    character_count: int
    line_count: int
    preview: NotRequired[str]
    retry_hint: NotRequired[str]
    status: NotRequired[str]


class ActionBinaryToolResultMetadata(TypedDict):
    storage: Literal["action_file"]
    path: str
    media_type: Literal["application/octet-stream"]
    byte_size: int


@dataclass(frozen=True, slots=True)
class ToolResultStorageResult:
    output_json: JSONValue
    storage_kind: ToolOutputStorageKind
    search_text: str | None
    stdout_text: str | None
    stderr_text: str | None
    storage_directory_fd: int | None = field(default=None, repr=False, compare=False)
    stored_file_names: tuple[str, ...] = field(default=(), repr=False, compare=False)


class ToolResultStorageError(RuntimeError):
    """A large tool result could not be stored atomically."""


def store_tool_result(
    *,
    action_tool_results_path: Path,
    invocation_id: str,
    output: UnprojectedToolOutput,
    search_text: str | None = None,
    stdout_text: str | None = None,
    stderr_text: str | None = None,
) -> ToolResultStorageResult:
    """Project raw output into durable JSON, spilling files when required."""
    validate_invocation_id(invocation_id)
    owner_path = (action_tool_results_path / quote(invocation_id, safe="-_")).absolute()
    if isinstance(output, bytes):
        file_name = _result_file_name(TOOL_RESULT_BINARY_SUFFIX)
        binary_metadata: ActionBinaryToolResultMetadata = {
            "storage": "action_file",
            "path": str(owner_path / file_name),
            "media_type": TOOL_RESULT_BINARY_MEDIA_TYPE,
            "byte_size": len(output),
        }
        return _stored_result(
            action_tool_results_path=action_tool_results_path,
            invocation_id=invocation_id,
            output_json=cast(dict[str, JSONValue], binary_metadata),
            payloads=((file_name, output),),
        )

    history_text = serialize_json_tool_output(output)
    if len(history_text) <= ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT:
        return ToolResultStorageResult(
            output_json=output,
            storage_kind="inline_json",
            search_text=search_text,
            stdout_text=stdout_text,
            stderr_text=stderr_text,
        )

    spilled = spill_long_texts(
        output,
        limit=ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT,
        new_path=lambda: str(owner_path / _result_file_name(TOOL_RESULT_TEXT_SUFFIX)),
    )
    if spilled is not None:
        projected, texts = spilled
        return _stored_result(
            action_tool_results_path=action_tool_results_path,
            invocation_id=invocation_id,
            output_json=projected,
            payloads=tuple(
                (Path(path).name, text.encode("utf-8")) for path, text in texts
            ),
        )

    file_name = _result_file_name(TOOL_RESULT_JSON_SUFFIX)
    payload = history_text.encode("utf-8")
    previewer = outline_previewer(
        output=output, text=history_text, path=str(owner_path / file_name)
    )
    reference: ActionFileToolResultMetadata = {
        "storage": "action_file",
        "path": str(owner_path / file_name),
        "media_type": TOOL_RESULT_JSON_MEDIA_TYPE,
        "byte_size": len(payload),
        "character_count": len(history_text),
        "line_count": count_text_lines(history_text),
    }
    # A result's status says how the call ended, not what it returned. Keeping it beside
    # the reference lets a reader tell, say, a patch that applied nothing from one that
    # did without opening the file.
    if isinstance(output, dict) and isinstance(status := output.get("status"), str):
        reference["status"] = status

    def with_preview(window: int) -> JSONValue:
        preview, retry_hint = previewer(window)
        metadata: ActionFileToolResultMetadata = {
            **reference,
            "preview": preview,
            "retry_hint": retry_hint,
        }
        return cast(dict[str, JSONValue], metadata)

    return _stored_result(
        action_tool_results_path=action_tool_results_path,
        invocation_id=invocation_id,
        output_json=with_preview(
            largest_fitting_size(
                with_preview,
                high=ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT,
                limit=ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT,
            )
        ),
        payloads=((file_name, payload),),
    )


def store_run_tool_result(*, spill_root: Path, output: JSONValue) -> JSONValue:
    """What the model reads of one result of a run with no Action: the result
    itself, or, when too large, what storage leaves in its place, the rest saved
    under ``spill_root`` (the run's own folder, created here) for read to page."""

    spill_root.mkdir(mode=_RUN_SPILL_ROOT_MODE, parents=True, exist_ok=True)
    stored = store_tool_result(
        # Named by the path read checks them against.
        action_tool_results_path=spill_root.resolve(strict=True),
        invocation_id=uuid.uuid4().hex,
        output=output,
    )
    release_error = release_stored_tool_result(stored)
    if release_error is not None:
        raise release_error
    return stored.output_json


def _stored_result(
    *,
    action_tool_results_path: Path,
    invocation_id: str,
    output_json: JSONValue,
    payloads: Sequence[tuple[str, bytes]],
) -> ToolResultStorageResult:
    return ToolResultStorageResult(
        output_json=output_json,
        storage_kind="action_file",
        search_text=None,
        stdout_text=None,
        stderr_text=None,
        storage_directory_fd=_store_payloads(
            action_tool_results_path=action_tool_results_path,
            invocation_id=invocation_id,
            payloads=payloads,
        ),
        stored_file_names=tuple(file_name for file_name, _payload in payloads),
    )


def discard_stored_tool_result(result: ToolResultStorageResult) -> None:
    """Remove a spill whose database record could not be committed."""
    directory_fd = result.storage_directory_fd
    if directory_fd is None:
        return
    try:
        _unlink_all(directory_fd=directory_fd, file_names=result.stored_file_names)
    finally:
        os.close(directory_fd)


def _unlink_all(*, directory_fd: int, file_names: Sequence[str]) -> None:
    for file_name in file_names:
        try:
            os.unlink(file_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
    os.fsync(directory_fd)


def release_stored_tool_result(result: ToolResultStorageResult) -> OSError | None:
    """Release a rollback descriptor without changing an already-durable result."""
    directory_fd = result.storage_directory_fd
    if directory_fd is None:
        return None
    try:
        os.close(directory_fd)
    except OSError as exc:
        return exc
    return None


def validate_invocation_id(invocation_id: str) -> None:
    if (
        not invocation_id
        or invocation_id in {".", ".."}
        or "/" in invocation_id
        or "\\" in invocation_id
        or "\0" in invocation_id
    ):
        raise ValueError("invocation_id must be a non-path identifier")


def _open_storage_directory(
    *, action_tool_results_path: Path, storage_directory_name: str
) -> int:
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    current_fd = os.open(action_tool_results_path, directory_flags)
    keep_open = False
    try:
        try:
            os.mkdir(storage_directory_name, mode=0o700, dir_fd=current_fd)
        except FileExistsError:
            pass
        os.fsync(current_fd)
        child_fd = os.open(storage_directory_name, directory_flags, dir_fd=current_fd)
        os.close(current_fd)
        current_fd = child_fd
        keep_open = True
        return current_fd
    finally:
        if not keep_open:
            os.close(current_fd)


def _result_file_name(file_suffix: str) -> str:
    return f"{TOOL_RESULT_FILENAME_PREFIX}{uuid.uuid4().hex}{file_suffix}"


def _store_payloads(
    *,
    action_tool_results_path: Path,
    invocation_id: str,
    payloads: Sequence[tuple[str, bytes]],
) -> int:
    """Write every file of one result, or none of them; the owner's directory fd."""
    storage_directory_fd: int | None = None
    written: list[str] = []
    try:
        storage_directory_fd = _open_storage_directory(
            action_tool_results_path=action_tool_results_path,
            storage_directory_name=quote(invocation_id, safe="-_"),
        )
        for file_name, payload in payloads:
            _write_bytes_atomically(
                directory_fd=storage_directory_fd,
                payload=payload,
                result_file_name=file_name,
            )
            written.append(file_name)
    except OSError as exc:
        if storage_directory_fd is not None:
            try:
                _unlink_all(directory_fd=storage_directory_fd, file_names=written)
            except OSError as cleanup_error:
                exc.add_note(f"tool result cleanup failed: {cleanup_error}")
            os.close(storage_directory_fd)
        raise ToolResultStorageError(
            "failed to atomically store tool result in the action tool result directory"
        ) from exc
    return storage_directory_fd


def _write_bytes_atomically(
    *, directory_fd: int, payload: bytes, result_file_name: str
) -> None:
    temp_file_name = f".{TOOL_RESULT_FILENAME_PREFIX}{uuid.uuid4().hex}.tmp"
    temp_fd: int | None = None
    renamed = False
    try:
        temp_fd = os.open(
            temp_file_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        with os.fdopen(temp_fd, "wb") as handle:
            temp_fd = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.rename(
            temp_file_name,
            result_file_name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        renamed = True
        os.fsync(directory_fd)
    except OSError as exc:
        cleanup_name = result_file_name if renamed else temp_file_name
        try:
            os.unlink(cleanup_name, dir_fd=directory_fd)
            os.fsync(directory_fd)
        except FileNotFoundError:
            pass
        except OSError as cleanup_error:
            exc.add_note(f"tool result cleanup failed: {cleanup_error}")
        raise
    finally:
        if temp_fd is not None:
            os.close(temp_fd)


__all__ = [
    "ACTION_TOOL_RESULTS_DIRNAME",
    "ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT",
    "ActionBinaryToolResultMetadata",
    "ActionFileToolResultMetadata",
    "TOOL_RESULT_BINARY_MEDIA_TYPE",
    "TOOL_RESULT_JSON_MEDIA_TYPE",
    "ToolResultStorageError",
    "ToolResultStorageResult",
    "discard_stored_tool_result",
    "release_stored_tool_result",
    "store_run_tool_result",
    "store_tool_result",
    "validate_invocation_id",
]
