from __future__ import annotations

import os
import stat
import uuid
from dataclasses import dataclass
from typing import Literal, TypedDict

from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.schema.tool_result import serialize_json_tool_output
from pantaray_agents.tools.contract import (
    ReactToolCall,
    ReactToolDefinition,
    ReactToolResult,
    react_tool_response_schema,
    tool_error_response,
)

TOOL_RESULT_FETCH_TOOL_NAME: Literal["tool_result_fetch"] = "tool_result_fetch"
MEMORY_TOOL_RESULT_INLINE_CHARACTER_LIMIT = 20_000
RUN_TOOL_RESULT_STORAGE_KIND: Literal["run_tool_result_file"] = "run_tool_result_file"
TOOL_RESULT_JSON_MEDIA_TYPE: Literal["application/json"] = "application/json"
TOOL_RESULT_LIFETIME: Literal["current_run"] = "current_run"

_RESULT_FILE_PREFIX = "output-"
_RESULT_FILE_SUFFIX = ".json"
_RESULT_REF_MAX_LENGTH = 64
_FETCH_SOURCE_BYTE_LIMIT = MEMORY_TOOL_RESULT_INLINE_CHARACTER_LIMIT


class RunToolResultFileMetadata(TypedDict):
    storage: Literal["run_tool_result_file"]
    result_ref: str
    media_type: Literal["application/json"]
    byte_size: int
    character_count: int
    line_count: int
    fetch_tool: Literal["tool_result_fetch"]
    lifetime: Literal["current_run"]


class RunToolResultStoreError(RuntimeError):
    """The run-owned tool-result store could not honor its contract."""


class RunToolResultStoreClosedError(RunToolResultStoreError):
    """The run-owned tool-result store was used after close."""


class RunToolResultStorageError(RunToolResultStoreError):
    """An overflow tool result could not be stored atomically."""


class RunToolResultFetchError(RunToolResultStoreError):
    """A stored tool result could not be read safely."""


@dataclass(frozen=True, slots=True)
class _StoredRunToolResult:
    file_name: str
    byte_size: int
    character_count: int
    line_count: int


class RunToolResultStore:
    """Run-scoped storage and bounded retrieval for serialized tool results."""

    def __init__(self, *, directory_fd: int) -> None:
        self._directory_fd: int | None = _validate_directory_fd(directory_fd)
        self._stored_results: dict[str, _StoredRunToolResult] = {}
        self._closed = False

    def store(self, serialized_payload: str) -> RunToolResultFileMetadata:
        """Persist one serialized payload and return its opaque retrieval metadata."""

        self._ensure_open()
        stored = self._persist(serialized_payload)
        result_ref = f"tool-result:{uuid.uuid4().hex}"
        self._stored_results[result_ref] = stored
        return {
            "storage": RUN_TOOL_RESULT_STORAGE_KIND,
            "result_ref": result_ref,
            "media_type": TOOL_RESULT_JSON_MEDIA_TYPE,
            "byte_size": stored.byte_size,
            "character_count": stored.character_count,
            "line_count": stored.line_count,
            "fetch_tool": TOOL_RESULT_FETCH_TOOL_NAME,
            "lifetime": TOOL_RESULT_LIFETIME,
        }

    def fetch_definition(self) -> ReactToolDefinition:
        """Build the harness-owned tool for reading one stored result by byte offset."""

        async def execute(call: ReactToolCall, _step_number: int) -> ReactToolResult:
            return self._execute_fetch(call)

        return ReactToolDefinition(
            name=TOOL_RESULT_FETCH_TOOL_NAME,
            description=(
                "Read a bounded page from a tool result that exceeded the inline "
                "limit. Continue with next_offset until it is null."
            ),
            request_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["result_ref"],
                "properties": {
                    "result_ref": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": _RESULT_REF_MAX_LENGTH,
                    },
                    "offset": {"type": "integer", "minimum": 0},
                },
            },
            response_schema=react_tool_response_schema(
                success_schema={
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "status",
                        "result_ref",
                        "content",
                        "offset",
                        "next_offset",
                        "byte_size",
                        "complete",
                    ],
                    "properties": {
                        "status": {"type": "string", "enum": ["success"]},
                        "result_ref": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": _RESULT_REF_MAX_LENGTH,
                        },
                        "content": {"type": "string"},
                        "offset": {"type": "integer", "minimum": 0},
                        "next_offset": {
                            "type": ["integer", "null"],
                            "minimum": 0,
                        },
                        "byte_size": {"type": "integer", "minimum": 0},
                        "complete": {"type": "boolean"},
                    },
                }
            ),
            execute=execute,
        )

    def close(self) -> None:
        """Close this store without owning deletion of the enclosing run tree."""

        if self._closed:
            return
        directory_fd = self._directory_fd
        self._stored_results.clear()
        self._directory_fd = None
        self._closed = True
        if directory_fd is not None:
            os.close(directory_fd)

    def _execute_fetch(self, call: ReactToolCall) -> ReactToolResult:
        if self._closed:
            return tool_error_response(
                tool_name=TOOL_RESULT_FETCH_TOOL_NAME,
                error_code="TOOL_RESULT_STORE_CLOSED",
                message="The run tool-result store is no longer available.",
            )
        if not isinstance(call.tool_args, dict):
            return tool_error_response(
                tool_name=TOOL_RESULT_FETCH_TOOL_NAME,
                error_code="TOOL_RESULT_FETCH_INVALID_REQUEST",
                message="tool_result_fetch requires an object request.",
            )
        result_ref = call.tool_args.get("result_ref")
        offset_value = call.tool_args.get("offset", 0)
        if (
            not isinstance(result_ref, str)
            or not result_ref
            or len(result_ref) > _RESULT_REF_MAX_LENGTH
        ):
            return tool_error_response(
                tool_name=TOOL_RESULT_FETCH_TOOL_NAME,
                error_code="TOOL_RESULT_FETCH_INVALID_REQUEST",
                message="tool_result_fetch requires a non-empty result_ref.",
            )
        if (
            not isinstance(offset_value, int)
            or isinstance(offset_value, bool)
            or offset_value < 0
        ):
            return tool_error_response(
                tool_name=TOOL_RESULT_FETCH_TOOL_NAME,
                error_code="TOOL_RESULT_FETCH_INVALID_REQUEST",
                message="tool_result_fetch offset must be a non-negative integer.",
            )
        stored = self._stored_results.get(result_ref)
        if stored is None:
            return tool_error_response(
                tool_name=TOOL_RESULT_FETCH_TOOL_NAME,
                error_code="TOOL_RESULT_NOT_FOUND",
                message="The requested run tool result does not exist.",
            )
        if offset_value > stored.byte_size:
            return tool_error_response(
                tool_name=TOOL_RESULT_FETCH_TOOL_NAME,
                error_code="TOOL_RESULT_OFFSET_OUT_OF_RANGE",
                message="tool_result_fetch offset exceeds the stored result size.",
                details={"byte_size": stored.byte_size},
            )
        try:
            output = self._fetch_page(
                result_ref=result_ref,
                stored=stored,
                offset=offset_value,
            )
        except RunToolResultFetchError:
            return tool_error_response(
                tool_name=TOOL_RESULT_FETCH_TOOL_NAME,
                error_code="TOOL_RESULT_FETCH_FAILED",
                message="The stored run tool result could not be read safely.",
            )
        return ReactToolResult(
            tool_name=TOOL_RESULT_FETCH_TOOL_NAME,
            status="success",
            output=output,
        )

    def _fetch_page(
        self,
        *,
        result_ref: str,
        stored: _StoredRunToolResult,
        offset: int,
    ) -> dict[str, JSONValue]:
        if offset == stored.byte_size:
            return _build_fetch_output(
                result_ref=result_ref,
                content="",
                offset=offset,
                next_offset=None,
                byte_size=stored.byte_size,
            )
        payload = self._read_page_bytes(stored=stored, offset=offset)
        text = _decode_utf8_page(payload)
        output = _largest_bounded_fetch_output(
            result_ref=result_ref,
            text=text,
            offset=offset,
            byte_size=stored.byte_size,
        )
        if (
            len(serialize_json_tool_output(output))
            > MEMORY_TOOL_RESULT_INLINE_CHARACTER_LIMIT
        ):
            raise RunToolResultFetchError(
                "tool-result fetch response exceeded the inline limit"
            )
        return output

    def _read_page_bytes(self, *, stored: _StoredRunToolResult, offset: int) -> bytes:
        directory_fd = self._directory_fd
        if directory_fd is None:
            raise RunToolResultFetchError("tool-result directory is unavailable")
        file_fd: int | None = None
        try:
            file_fd = os.open(
                stored.file_name,
                os.O_RDONLY | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            file_stat = os.fstat(file_fd)
            if (
                not stat.S_ISREG(file_stat.st_mode)
                or file_stat.st_size != stored.byte_size
            ):
                raise RunToolResultFetchError(
                    "stored tool-result file size or type changed"
                )
            first_byte = os.pread(file_fd, 1, offset)
            if first_byte and first_byte[0] & 0b1100_0000 == 0b1000_0000:
                raise RunToolResultFetchError(
                    "tool-result offset is not a UTF-8 character boundary"
                )
            return os.pread(file_fd, _FETCH_SOURCE_BYTE_LIMIT, offset)
        except OSError as exc:
            raise RunToolResultFetchError(
                "failed to safely read the stored tool result"
            ) from exc
        finally:
            if file_fd is not None:
                os.close(file_fd)

    def _persist(self, serialized_payload: str) -> _StoredRunToolResult:
        directory_fd = self._directory_fd
        if directory_fd is None:
            raise RunToolResultStoreClosedError(
                "run tool-result store has already been closed"
            )
        payload = serialized_payload.encode("utf-8")
        file_name = _write_bytes_atomically(
            directory_fd=directory_fd,
            payload=payload,
        )
        return _StoredRunToolResult(
            file_name=file_name,
            byte_size=len(payload),
            character_count=len(serialized_payload),
            line_count=serialized_payload.count("\n") + 1,
        )

    def _ensure_open(self) -> None:
        if self._closed:
            raise RunToolResultStoreClosedError(
                "run tool-result store has already been closed"
            )


def _validate_directory_fd(directory_fd: int) -> int:
    try:
        directory_mode = os.fstat(directory_fd).st_mode
    except OSError as exc:
        try:
            os.close(directory_fd)
        except OSError:
            pass
        raise RunToolResultStorageError(
            "run tool-result directory descriptor is unavailable"
        ) from exc
    if not stat.S_ISDIR(directory_mode):
        os.close(directory_fd)
        raise RunToolResultStorageError(
            "run tool-result storage descriptor is not a directory"
        )
    return directory_fd


def _write_bytes_atomically(*, directory_fd: int, payload: bytes) -> str:
    result_name = f"{_RESULT_FILE_PREFIX}{uuid.uuid4().hex}{_RESULT_FILE_SUFFIX}"
    temporary_name = f".{_RESULT_FILE_PREFIX}{uuid.uuid4().hex}.tmp"
    temporary_fd: int | None = None
    renamed = False
    try:
        temporary_fd = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        with os.fdopen(temporary_fd, "wb") as handle:
            temporary_fd = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.rename(
            temporary_name,
            result_name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        renamed = True
        os.fsync(directory_fd)
    except OSError as exc:
        cleanup_name = result_name if renamed else temporary_name
        try:
            os.unlink(cleanup_name, dir_fd=directory_fd)
            os.fsync(directory_fd)
        except FileNotFoundError:
            pass
        except OSError as cleanup_error:
            exc.add_note(f"tool-result cleanup failed: {cleanup_error}")
        raise RunToolResultStorageError(
            "failed to atomically store the run tool result"
        ) from exc
    finally:
        if temporary_fd is not None:
            os.close(temporary_fd)
    return result_name


def _decode_utf8_page(payload: bytes) -> str:
    for trailing_bytes in range(4):
        candidate = payload if trailing_bytes == 0 else payload[:-trailing_bytes]
        try:
            return candidate.decode("utf-8")
        except UnicodeDecodeError:
            continue
    raise RunToolResultFetchError("stored tool result is not valid UTF-8")


def _largest_bounded_fetch_output(
    *,
    result_ref: str,
    text: str,
    offset: int,
    byte_size: int,
) -> dict[str, JSONValue]:
    lower = 0
    upper = len(text)
    best = _build_fetch_output(
        result_ref=result_ref,
        content="",
        offset=offset,
        next_offset=offset,
        byte_size=byte_size,
    )
    while lower <= upper:
        midpoint = (lower + upper) // 2
        content = text[:midpoint]
        consumed_bytes = len(content.encode("utf-8"))
        candidate_next = offset + consumed_bytes
        candidate = _build_fetch_output(
            result_ref=result_ref,
            content=content,
            offset=offset,
            next_offset=(candidate_next if candidate_next < byte_size else None),
            byte_size=byte_size,
        )
        if (
            len(serialize_json_tool_output(candidate))
            <= MEMORY_TOOL_RESULT_INLINE_CHARACTER_LIMIT
        ):
            best = candidate
            lower = midpoint + 1
        else:
            upper = midpoint - 1
    if not best["content"] and text:
        raise RunToolResultFetchError(
            "tool-result fetch metadata leaves no room for content"
        )
    return best


def _build_fetch_output(
    *,
    result_ref: str,
    content: str,
    offset: int,
    next_offset: int | None,
    byte_size: int,
) -> dict[str, JSONValue]:
    return {
        "status": "success",
        "result_ref": result_ref,
        "content": content,
        "offset": offset,
        "next_offset": next_offset,
        "byte_size": byte_size,
        "complete": next_offset is None,
    }


__all__ = [
    "MEMORY_TOOL_RESULT_INLINE_CHARACTER_LIMIT",
    "RunToolResultFileMetadata",
    "RunToolResultStore",
    "TOOL_RESULT_FETCH_TOOL_NAME",
]
