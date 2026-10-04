from __future__ import annotations

import codecs
import json
import math
import os
import re
import stat
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, NotRequired, TypedDict, cast
from urllib.parse import quote

from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.schema.tool_result import (
    UnprojectedToolOutput,
    serialize_json_tool_output,
)

from .models import ToolOutputStorageKind
from .tool_result_spill import is_json_spill_shape, spill_preview

TOOL_RESULT_JSON_MEDIA_TYPE: Literal["application/json"] = "application/json"
TOOL_RESULT_BINARY_MEDIA_TYPE: Literal["application/octet-stream"] = (
    "application/octet-stream"
)
ACTION_TOOL_RESULTS_DIRNAME = "tool-results"
ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT = 20_000
TOOL_RESULT_FILENAME_PREFIX = "output-"
TOOL_RESULT_JSON_SUFFIX = ".json"
TOOL_RESULT_BINARY_SUFFIX = ".bin"
_JSON_RESULT_FILE_PATTERN = re.compile(r"^output-[0-9a-f]{32}\.json$")
_BINARY_RESULT_FILE_PATTERN = re.compile(r"^output-[0-9a-f]{32}\.bin$")
_BINARY_METADATA_KEYS = frozenset({"storage", "path", "media_type", "byte_size"})


class ActionFileToolResultMetadata(TypedDict):
    storage: Literal["action_file"]
    path: str
    media_type: Literal["application/json"]
    byte_size: int
    character_count: int
    line_count: int
    preview: NotRequired[str]
    retry_hint: NotRequired[str]


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
    stored_file_name: str | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class ToolResultTextPrefix:
    content: str
    next_byte_offset: int | None
    total_bytes: int
    unavailable_reason: Literal["no_output", "binary"] | None


class ToolResultStorageError(RuntimeError):
    """A large tool result could not be stored atomically."""


class ToolResultLoadError(RuntimeError):
    """A persisted action-file result failed bounded integrity validation."""


class ToolResultReadLimitError(ToolResultLoadError):
    """A valid action-file JSON result exceeds the caller's read budget."""


class ToolResultTextOffsetError(ValueError):
    """A cursor does not point at a server-issued UTF-8 boundary."""


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
    _validate_invocation_id(invocation_id)
    if isinstance(output, bytes):
        storage_directory_fd, file_name, stored_path = _store_payload(
            action_tool_results_path=action_tool_results_path,
            invocation_id=invocation_id,
            payload=output,
            file_suffix=TOOL_RESULT_BINARY_SUFFIX,
        )
        binary_metadata: ActionBinaryToolResultMetadata = {
            "storage": "action_file",
            "path": str(stored_path),
            "media_type": TOOL_RESULT_BINARY_MEDIA_TYPE,
            "byte_size": len(output),
        }
        return ToolResultStorageResult(
            output_json=cast(dict[str, JSONValue], binary_metadata),
            storage_kind="action_file",
            search_text=None,
            stdout_text=None,
            stderr_text=None,
            storage_directory_fd=storage_directory_fd,
            stored_file_name=file_name,
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

    payload = history_text.encode("utf-8")
    storage_directory_fd, file_name, stored_path = _store_payload(
        action_tool_results_path=action_tool_results_path,
        invocation_id=invocation_id,
        payload=payload,
        file_suffix=TOOL_RESULT_JSON_SUFFIX,
    )

    preview, retry_hint = spill_preview(text=history_text, path=str(stored_path))
    metadata: ActionFileToolResultMetadata = {
        "storage": "action_file",
        "path": str(stored_path),
        "media_type": TOOL_RESULT_JSON_MEDIA_TYPE,
        "byte_size": len(payload),
        "character_count": len(history_text),
        "line_count": history_text.count("\n") + 1,
        "preview": preview,
        "retry_hint": retry_hint,
    }
    return ToolResultStorageResult(
        output_json=cast(dict[str, JSONValue], metadata),
        storage_kind="action_file",
        search_text=None,
        stdout_text=None,
        stderr_text=None,
        storage_directory_fd=storage_directory_fd,
        stored_file_name=file_name,
    )


def discard_stored_tool_result(result: ToolResultStorageResult) -> None:
    """Remove a spill whose database record could not be committed."""
    directory_fd = result.storage_directory_fd
    file_name = result.stored_file_name
    if directory_fd is None or file_name is None:
        return
    try:
        try:
            os.unlink(file_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


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


def load_action_file_json_result(
    *,
    action_tool_results_path: Path,
    invocation_id: str,
    metadata: JSONValue,
    max_bytes: int,
) -> JSONValue:
    """Load one host-owned JSON spill without following stored paths or symlinks."""
    _validate_invocation_id(invocation_id)
    if max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    validated = _validate_json_metadata(metadata)
    stored_path = _validate_stored_result_path(
        action_tool_results_path=action_tool_results_path,
        invocation_id=invocation_id,
        raw_path=validated["path"],
        file_pattern=_JSON_RESULT_FILE_PATTERN,
    )
    if validated["byte_size"] > max_bytes:
        raise ToolResultReadLimitError("action-file JSON result exceeds the read limit")

    payload = _read_action_file_prefix(
        action_tool_results_path=action_tool_results_path,
        invocation_id=invocation_id,
        stored_path=stored_path,
        expected_byte_size=validated["byte_size"],
        start_byte=0,
        read_bytes=validated["byte_size"],
    )

    try:
        text = payload.decode("utf-8")
        parsed = json.loads(
            text,
            parse_constant=_reject_non_json_number,
            parse_float=_parse_finite_float,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ToolResultLoadError(
            "action-file JSON result is not valid UTF-8 JSON"
        ) from exc
    if (
        len(text) != validated["character_count"]
        or text.count("\n") + 1 != validated["line_count"]
    ):
        raise ToolResultLoadError(
            "action-file JSON result metadata does not match content"
        )
    return cast(JSONValue, parsed)


def read_tool_result_text_prefix(
    *,
    action_tool_results_path: Path | None,
    output_owner_id: str,
    output: JSONValue,
    storage_kind: ToolOutputStorageKind,
    start_byte: int,
    page_bytes: int,
    max_read_bytes: int,
) -> ToolResultTextPrefix:
    """Read one bounded UTF-8 page after the caller proves output authority."""

    if output is None:
        if storage_kind != "inline_json":
            raise ToolResultLoadError("action-file result metadata is missing")
        if start_byte:
            raise ToolResultTextOffsetError("cursor exceeds empty tool output")
        return ToolResultTextPrefix("", None, 0, "no_output")
    if storage_kind == "inline_json":
        complete = serialize_json_tool_output(output).encode("utf-8")
        total_bytes = len(complete)
        payload = complete[
            start_byte : min(total_bytes, max_read_bytes, start_byte + page_bytes)
        ]
    else:
        if action_tool_results_path is None:
            raise ToolResultLoadError("action-file result has no storage root")
        _validate_invocation_id(output_owner_id)
        if (
            isinstance(output, dict)
            and output.get("media_type") == TOOL_RESULT_BINARY_MEDIA_TYPE
        ):
            binary = _validate_binary_metadata(output)
            stored_path = _validate_stored_result_path(
                action_tool_results_path=action_tool_results_path,
                invocation_id=output_owner_id,
                raw_path=binary["path"],
                file_pattern=_BINARY_RESULT_FILE_PATTERN,
            )
            _read_action_file_prefix(
                action_tool_results_path=action_tool_results_path,
                invocation_id=output_owner_id,
                stored_path=stored_path,
                expected_byte_size=binary["byte_size"],
                start_byte=0,
                read_bytes=0,
            )
            if start_byte:
                raise ToolResultTextOffsetError("cursor is invalid for binary output")
            return ToolResultTextPrefix("", None, binary["byte_size"], "binary")
        metadata = _validate_json_metadata(output)
        total_bytes = metadata["byte_size"]
        readable_bytes = min(total_bytes, max_read_bytes)
        if start_byte >= readable_bytes:
            raise ToolResultTextOffsetError("cursor exceeds readable tool output")
        stored_path = _validate_stored_result_path(
            action_tool_results_path=action_tool_results_path,
            invocation_id=output_owner_id,
            raw_path=metadata["path"],
            file_pattern=_JSON_RESULT_FILE_PATTERN,
        )
        payload = _read_action_file_prefix(
            action_tool_results_path=action_tool_results_path,
            invocation_id=output_owner_id,
            stored_path=stored_path,
            expected_byte_size=total_bytes,
            start_byte=start_byte,
            read_bytes=min(page_bytes, readable_bytes - start_byte),
        )
    return _decode_text_prefix(
        payload=payload,
        start_byte=start_byte,
        total_bytes=total_bytes,
        max_read_bytes=max_read_bytes,
    )


def _validate_json_metadata(metadata: JSONValue) -> ActionFileToolResultMetadata:
    if not isinstance(metadata, dict) or not is_json_spill_shape(set(metadata)):
        raise ToolResultLoadError("invalid action-file JSON metadata shape")
    if (
        metadata.get("storage") != "action_file"
        or metadata.get("media_type") != TOOL_RESULT_JSON_MEDIA_TYPE
        or not isinstance(metadata.get("path"), str)
        or not isinstance(metadata.get("preview", ""), str)
        or not isinstance(metadata.get("retry_hint", ""), str)
        or any(
            not isinstance(metadata.get(field), int)
            or isinstance(metadata.get(field), bool)
            or cast(int, metadata[field]) < 0
            for field in ("byte_size", "character_count", "line_count")
        )
    ):
        raise ToolResultLoadError("invalid action-file JSON metadata values")
    return cast(ActionFileToolResultMetadata, metadata)


def _validate_binary_metadata(metadata: JSONValue) -> ActionBinaryToolResultMetadata:
    if (
        not isinstance(metadata, dict)
        or set(metadata) != _BINARY_METADATA_KEYS
        or metadata.get("storage") != "action_file"
        or metadata.get("media_type") != TOOL_RESULT_BINARY_MEDIA_TYPE
        or not isinstance(metadata.get("path"), str)
        or not isinstance(metadata.get("byte_size"), int)
        or isinstance(metadata.get("byte_size"), bool)
        or cast(int, metadata["byte_size"]) < 0
    ):
        raise ToolResultLoadError("invalid action-file binary metadata")
    return cast(ActionBinaryToolResultMetadata, metadata)


def _validate_stored_result_path(
    *,
    action_tool_results_path: Path,
    invocation_id: str,
    raw_path: str,
    file_pattern: re.Pattern[str],
) -> Path:
    expected_root = action_tool_results_path.absolute()
    stored_path = Path(raw_path)
    owner_name = quote(invocation_id, safe="-_")
    if (
        not stored_path.is_absolute()
        or stored_path.parent != expected_root / owner_name
        or file_pattern.fullmatch(stored_path.name) is None
    ):
        raise ToolResultLoadError("action-file result path is outside its owner")
    return stored_path


def _read_action_file_prefix(
    *,
    action_tool_results_path: Path,
    invocation_id: str,
    stored_path: Path,
    expected_byte_size: int,
    start_byte: int,
    read_bytes: int,
) -> bytes:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    read_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        root_fd = os.open(action_tool_results_path.absolute(), flags)
        try:
            owner_fd = os.open(quote(invocation_id, safe="-_"), flags, dir_fd=root_fd)
            try:
                file_fd = os.open(stored_path.name, read_flags, dir_fd=owner_fd)
                try:
                    file_stat = os.fstat(file_fd)
                    if (
                        not stat.S_ISREG(file_stat.st_mode)
                        or file_stat.st_size != expected_byte_size
                    ):
                        raise ToolResultLoadError(
                            "action-file JSON result size or type changed"
                        )
                    os.lseek(file_fd, start_byte, os.SEEK_SET)
                    chunks: list[bytes] = []
                    while read_bytes:
                        chunk = os.read(file_fd, min(read_bytes, 64 * 1024))
                        if not chunk:
                            raise ToolResultLoadError(
                                "action-file JSON result ended before its byte_size"
                            )
                        chunks.append(chunk)
                        read_bytes -= len(chunk)
                    if os.fstat(file_fd).st_size != expected_byte_size:
                        raise ToolResultLoadError("action-file result size changed")
                    return b"".join(chunks)
                finally:
                    os.close(file_fd)
            finally:
                os.close(owner_fd)
        finally:
            os.close(root_fd)
    except OSError as exc:
        raise ToolResultLoadError(
            "failed to safely open action-file JSON result"
        ) from exc


def _decode_text_prefix(
    *, payload: bytes, start_byte: int, total_bytes: int, max_read_bytes: int
) -> ToolResultTextPrefix:
    readable_bytes = min(total_bytes, max_read_bytes)
    if start_byte >= readable_bytes:
        raise ToolResultTextOffsetError("cursor exceeds readable tool output")
    if payload and payload[0] & 0b1100_0000 == 0b1000_0000:
        if not start_byte:
            raise ToolResultLoadError("action-file JSON result is not valid UTF-8")
        raise ToolResultTextOffsetError("cursor splits a UTF-8 code point")
    try:
        content = codecs.getincrementaldecoder("utf-8")().decode(payload, final=False)
    except UnicodeDecodeError as exc:
        raise ToolResultLoadError("action-file JSON result is not valid UTF-8") from exc
    consumed = len(content.encode("utf-8"))
    if not consumed:
        if start_byte + len(payload) == readable_bytes < total_bytes:
            return ToolResultTextPrefix("", None, total_bytes, None)
        raise ToolResultLoadError("tool result page has no complete UTF-8 text")
    next_byte = start_byte + consumed
    payload_end = start_byte + len(payload)
    if payload_end == total_bytes and consumed != len(payload):
        raise ToolResultLoadError("action-file JSON result is not valid UTF-8")
    return ToolResultTextPrefix(
        content=content,
        next_byte_offset=(
            next_byte
            if next_byte < readable_bytes and payload_end < readable_bytes
            else None
        ),
        total_bytes=total_bytes,
        unavailable_reason=None,
    )


def _reject_non_json_number(value: str) -> None:
    raise ValueError(f"invalid JSON number: {value}")


def _parse_finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"JSON number is outside the finite range: {value}")
    return parsed


def _validate_invocation_id(invocation_id: str) -> None:
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


def _store_payload(
    *,
    action_tool_results_path: Path,
    invocation_id: str,
    payload: bytes,
    file_suffix: str,
) -> tuple[int, str, Path]:
    storage_directory_name = quote(invocation_id, safe="-_")
    storage_directory_fd: int | None = None
    try:
        storage_directory_fd = _open_storage_directory(
            action_tool_results_path=action_tool_results_path,
            storage_directory_name=storage_directory_name,
        )
        file_name = _write_bytes_atomically(
            directory_fd=storage_directory_fd,
            payload=payload,
            file_suffix=file_suffix,
        )
    except OSError as exc:
        if storage_directory_fd is not None:
            os.close(storage_directory_fd)
        raise ToolResultStorageError(
            "failed to atomically store tool result in the action tool result directory"
        ) from exc
    stored_path = (
        action_tool_results_path / storage_directory_name / file_name
    ).absolute()
    return storage_directory_fd, file_name, stored_path


def _write_bytes_atomically(
    *, directory_fd: int, payload: bytes, file_suffix: str
) -> str:
    result_file_name = f"{TOOL_RESULT_FILENAME_PREFIX}{uuid.uuid4().hex}{file_suffix}"
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
    return result_file_name


__all__ = [
    "ACTION_TOOL_RESULTS_DIRNAME",
    "ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT",
    "ActionBinaryToolResultMetadata",
    "ActionFileToolResultMetadata",
    "TOOL_RESULT_BINARY_MEDIA_TYPE",
    "ToolResultLoadError",
    "ToolResultReadLimitError",
    "ToolResultStorageError",
    "ToolResultStorageResult",
    "ToolResultTextOffsetError",
    "ToolResultTextPrefix",
    "discard_stored_tool_result",
    "load_action_file_json_result",
    "read_tool_result_text_prefix",
    "release_stored_tool_result",
    "store_tool_result",
]
