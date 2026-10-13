"""Reading back tool results the runtime moved out of line into action files."""

from __future__ import annotations

import codecs
import json
import math
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypedDict, cast
from urllib.parse import quote

from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.schema.tool_result import serialize_json_tool_output
from pantaray_agents.tools.files.text_lines import count_text_lines

from .models import ToolOutputStorageKind
from .tool_result_spill import (
    SPILLED_TEXT_KEYS,
    is_json_spill_shape,
    is_spilled_text,
    replace_spilled_texts,
    spilled_texts,
)
from .tool_result_storage import (
    TOOL_RESULT_BINARY_MEDIA_TYPE,
    TOOL_RESULT_JSON_MEDIA_TYPE,
    ActionBinaryToolResultMetadata,
    ActionFileToolResultMetadata,
    validate_invocation_id,
)

_JSON_RESULT_FILE_PATTERN = re.compile(r"^output-[0-9a-f]{32}\.json$")
_TEXT_RESULT_FILE_PATTERN = re.compile(r"^output-[0-9a-f]{32}\.txt$")
_BINARY_RESULT_FILE_PATTERN = re.compile(r"^output-[0-9a-f]{32}\.bin$")
_BINARY_METADATA_KEYS = frozenset({"storage", "path", "media_type", "byte_size"})
_UTF8_MAX_CHARACTER_BYTES = 4


class _TextReference(TypedDict):
    storage: Literal["action_file"]
    path: str
    media_type: Literal["text/plain"]
    byte_size: int
    character_count: int
    line_count: int
    preview: str
    retry_hint: str


@dataclass(frozen=True, slots=True)
class ToolResultTextPrefix:
    content: str
    next_byte_offset: int | None
    total_bytes: int
    unavailable_reason: Literal["no_output", "binary"] | None


class ToolResultLoadError(RuntimeError):
    """A persisted action-file result failed bounded integrity validation."""


class ToolResultReadLimitError(ToolResultLoadError):
    """A valid action-file result exceeds the caller's read budget."""


class ToolResultTextOffsetError(ValueError):
    """A cursor does not point at a server-issued UTF-8 boundary."""


def load_action_file_result(
    *,
    action_tool_results_path: Path,
    invocation_id: str,
    output: JSONValue,
    max_bytes: int,
) -> JSONValue:
    """The whole result an action-file reference stands in for, read from its files.

    Reads without following stored paths or symlinks, and raises
    ``ToolResultReadLimitError`` when the files hold more than ``max_bytes``.
    """
    validate_invocation_id(invocation_id)
    if max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    if _is_json_metadata(output):
        return _load_json_result(
            action_tool_results_path=action_tool_results_path,
            invocation_id=invocation_id,
            metadata=output,
            max_bytes=max_bytes,
        )
    references = [_validate_text_reference(value) for value in spilled_texts(output)]
    if not references:
        raise ToolResultLoadError("action-file result references no stored file")
    if sum(reference["byte_size"] for reference in references) > max_bytes:
        raise ToolResultReadLimitError("action-file result exceeds the read limit")
    return _restore_texts(
        action_tool_results_path=action_tool_results_path,
        invocation_id=invocation_id,
        output=output,
        read_limit=None,
    )


def _load_json_result(
    *,
    action_tool_results_path: Path,
    invocation_id: str,
    metadata: JSONValue,
    max_bytes: int,
) -> JSONValue:
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
    _check_counts(text, validated)
    return cast(JSONValue, parsed)


def _restore_texts(
    *,
    action_tool_results_path: Path,
    invocation_id: str,
    output: JSONValue,
    read_limit: int | None,
) -> JSONValue:
    """``output`` with each moved text read back, or only its first ``read_limit``
    bytes, cut back to a whole character, when it holds more."""

    def text_of(value: dict[str, JSONValue]) -> str:
        reference = _validate_text_reference(value)
        stored_path = _validate_stored_result_path(
            action_tool_results_path=action_tool_results_path,
            invocation_id=invocation_id,
            raw_path=reference["path"],
            file_pattern=_TEXT_RESULT_FILE_PATTERN,
        )
        byte_size = reference["byte_size"]
        whole = read_limit is None or byte_size <= read_limit
        payload = _read_action_file_prefix(
            action_tool_results_path=action_tool_results_path,
            invocation_id=invocation_id,
            stored_path=stored_path,
            expected_byte_size=byte_size,
            start_byte=0,
            read_bytes=byte_size if whole else cast(int, read_limit),
        )
        try:
            text = codecs.getincrementaldecoder("utf-8")().decode(payload, final=whole)
        except UnicodeDecodeError as exc:
            raise ToolResultLoadError("action-file text is not valid UTF-8") from exc
        if whole:
            _check_counts(text, reference)
        return text

    return replace_spilled_texts(output, text_of)


def _check_counts(
    text: str, metadata: ActionFileToolResultMetadata | _TextReference
) -> None:
    if (
        len(text) != metadata["character_count"]
        or count_text_lines(text) != metadata["line_count"]
    ):
        raise ToolResultLoadError("action-file result metadata does not match content")


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
        return _page(
            complete=serialize_json_tool_output(output).encode("utf-8"),
            start_byte=start_byte,
            page_bytes=page_bytes,
            max_read_bytes=max_read_bytes,
        )
    if action_tool_results_path is None:
        raise ToolResultLoadError("action-file result has no storage root")
    validate_invocation_id(output_owner_id)
    if (
        isinstance(output, dict)
        and output.get("storage") == "action_file"
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
    if not _is_json_metadata(output):
        if not spilled_texts(output):
            raise ToolResultLoadError("action-file result references no stored file")
        # A text longer than the readable bytes is read only a little past them: the
        # serialization up to max_read_bytes is unchanged, and the cut text, even
        # after a partial character is dropped, keeps total_bytes over that limit,
        # so the last page still says truncated.
        restored = _restore_texts(
            action_tool_results_path=action_tool_results_path,
            invocation_id=output_owner_id,
            output=output,
            read_limit=max_read_bytes + _UTF8_MAX_CHARACTER_BYTES,
        )
        return _page(
            complete=serialize_json_tool_output(restored).encode("utf-8"),
            start_byte=start_byte,
            page_bytes=page_bytes,
            max_read_bytes=max_read_bytes,
        )
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


def _page(
    *, complete: bytes, start_byte: int, page_bytes: int, max_read_bytes: int
) -> ToolResultTextPrefix:
    total_bytes = len(complete)
    return _decode_text_prefix(
        payload=complete[
            start_byte : min(total_bytes, max_read_bytes, start_byte + page_bytes)
        ],
        start_byte=start_byte,
        total_bytes=total_bytes,
        max_read_bytes=max_read_bytes,
    )


def _is_json_metadata(output: JSONValue) -> bool:
    return (
        isinstance(output, dict)
        and output.get("storage") == "action_file"
        and output.get("media_type") == TOOL_RESULT_JSON_MEDIA_TYPE
    )


def _validate_text_reference(value: JSONValue) -> _TextReference:
    if (
        not isinstance(value, dict)
        or set(value) != SPILLED_TEXT_KEYS
        or not is_spilled_text(value)
        or not all(
            isinstance(value[field], str) for field in ("path", "preview", "retry_hint")
        )
        or any(
            not isinstance(value[field], int)
            or isinstance(value[field], bool)
            or cast(int, value[field]) < 0
            for field in ("byte_size", "character_count", "line_count")
        )
    ):
        raise ToolResultLoadError("invalid action-file text reference")
    return cast(_TextReference, value)


def _validate_json_metadata(metadata: JSONValue) -> ActionFileToolResultMetadata:
    if not isinstance(metadata, dict) or not is_json_spill_shape(set(metadata)):
        raise ToolResultLoadError("invalid action-file JSON metadata shape")
    if (
        metadata.get("storage") != "action_file"
        or metadata.get("media_type") != TOOL_RESULT_JSON_MEDIA_TYPE
        or not isinstance(metadata.get("path"), str)
        or not isinstance(metadata.get("preview", ""), str)
        or not isinstance(metadata.get("retry_hint", ""), str)
        or not isinstance(metadata.get("status", ""), str)
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


__all__ = [
    "ToolResultLoadError",
    "ToolResultReadLimitError",
    "ToolResultTextOffsetError",
    "ToolResultTextPrefix",
    "load_action_file_result",
    "read_tool_result_text_prefix",
]
