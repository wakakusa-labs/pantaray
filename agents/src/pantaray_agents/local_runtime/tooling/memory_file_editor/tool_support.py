from __future__ import annotations

import sqlite3

from pantaray_agents.local_runtime.descriptor_access import (
    DescriptorPathError,
    DescriptorPathPolicyError,
)
from pantaray_agents.local_runtime.memory_catalog.editable_files import (
    MemoryFileConflictError,
)
from pantaray_agents.local_runtime.memory_catalog.errors import (
    MemoryCatalogError,
    MemoryCatalogIntegrityError,
    MemoryLinkValidationError,
    MemoryPublicationConflictError,
)
from pantaray_agents.local_runtime.tooling.fs_sandbox import (
    PatchChunk,
    PatchLine,
    SandboxPathError,
    TextFileError,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tools.contract import (
    TOOL_ERROR_RESPONSE_SCHEMA,
    JsonSchema,
    ReactToolDefinition,
    ReactToolResult,
)

from .bounded_workspace_io import MAX_READ_LINE_LIMIT

FILE_TOOL_ERRORS = (
    DescriptorPathError,
    OSError,
    sqlite3.Error,
    MemoryFileConflictError,
    MemoryCatalogError,
    SandboxPathError,
    TextFileError,
)


def tool(
    *,
    name: str,
    description: str,
    request_schema: JsonSchema,
    response_schema: JsonSchema,
    execute: object,
) -> ReactToolDefinition:
    return ReactToolDefinition(
        name=name,
        description=description,
        request_schema=request_schema,
        response_schema=response_schema,
        execute=execute,  # type: ignore[arg-type]
    )


def response_schema(
    *, required: tuple[str, ...], properties: dict[str, JSONValue]
) -> JsonSchema:
    success_properties: dict[str, JSONValue] = {
        "status": {"type": "string", "enum": ["success"]},
        **properties,
    }
    return {
        "oneOf": [
            {
                "type": "object",
                "additionalProperties": False,
                "required": list(required),
                "properties": success_properties,
            },
            dict(TOOL_ERROR_RESPONSE_SCHEMA),
        ]
    }


def path_changed_response_schema() -> JsonSchema:
    return response_schema(
        required=("status", "path", "changed"),
        properties={
            "path": {"type": "string"},
            "changed": {"type": "boolean"},
            "draft_revision": {"type": "string", "pattern": "^sha256:"},
        },
    )


def read_file_response_schema() -> JsonSchema:
    return response_schema(
        required=(
            "status",
            "path",
            "text",
            "offset",
            "column",
            "end_line",
            "end_column",
            "total_lines",
            "next_offset",
            "next_column",
            "truncated",
            "truncation_reason",
            "retry_hint",
        ),
        properties={
            "path": {"type": "string"},
            "text": {"type": "string"},
            "offset": {"type": "integer"},
            "column": {"type": "integer"},
            "end_line": {"type": "integer"},
            "end_column": {"type": "integer"},
            "total_lines": {"type": ["integer", "null"]},
            "next_offset": {"type": ["integer", "null"]},
            "next_column": {"type": ["integer", "null"]},
            "truncated": {"type": "boolean"},
            "truncation_reason": {"type": ["string", "null"]},
            "retry_hint": {"type": ["string", "null"]},
            "draft_revision": {"type": "string", "pattern": "^sha256:"},
        },
    )


def search_files_response_schema() -> JsonSchema:
    return response_schema(
        required=(
            "status",
            "matches",
            "truncated",
            "truncation_reason",
            "retry_hint",
            "skipped_files",
        ),
        properties={
            "matches": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["path", "line_number", "line"],
                    "properties": {
                        "path": {"type": "string"},
                        "line_number": {"type": "integer"},
                        "line": {"type": "string"},
                    },
                },
            },
            "truncated": {"type": "boolean"},
            "truncation_reason": {"type": ["string", "null"]},
            "retry_hint": {"type": ["string", "null"]},
            "skipped_files": {"type": "integer", "minimum": 0},
        },
    )


def read_file_request_schema(root_ids: tuple[str, ...]) -> JsonSchema:
    properties: dict[str, JSONValue] = {
        "path": {"type": "string", "minLength": 1},
        "offset": {"type": "integer", "minimum": 1},
        "column": {"type": "integer", "minimum": 1},
        "limit": {"type": "integer", "minimum": 1, "maximum": MAX_READ_LINE_LIMIT},
    }
    required: list[JSONValue] = ["path"]
    if root_ids:
        properties["root"] = {"type": "string", "enum": list(root_ids)}
        required.insert(0, "root")
    return {
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": properties,
    }


def search_files_request_schema(root_ids: tuple[str, ...]) -> JsonSchema:
    properties: dict[str, JSONValue] = {
        "query": {"type": "string", "minLength": 1},
        "limit": {"type": "integer", "minimum": 1, "maximum": 50},
    }
    required: list[JSONValue] = ["query"]
    if root_ids:
        properties["root"] = {"type": "string", "enum": list(root_ids)}
        required.insert(0, "root")
    return {
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": properties,
    }


def write_request_schema() -> JsonSchema:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["path", "text"],
        "properties": {
            "path": {"type": "string", "minLength": 1},
            "text": {"type": "string"},
        },
    }


def parse_patch_chunks(args: dict[str, JSONValue]) -> tuple[PatchChunk, ...]:
    raw_chunks = args.get("chunks")
    if not isinstance(raw_chunks, list):
        raise ValueError("chunks must be a list")
    chunks: list[PatchChunk] = []
    for raw_chunk in raw_chunks:
        if not isinstance(raw_chunk, dict):
            raise ValueError("chunk must be an object")
        raw_lines = raw_chunk.get("lines")
        if not isinstance(raw_lines, list):
            raise ValueError("chunk lines must be a list")
        lines = tuple(_parse_patch_line(raw_line) for raw_line in raw_lines)
        chunks.append(PatchChunk(lines=lines))
    return tuple(chunks)


def required_string_arg(value: JSONValue, key: str) -> str:
    return required_string(args_object(value), key)


def required_string(args: dict[str, JSONValue], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value


def optional_int(args: dict[str, JSONValue], key: str) -> int | None:
    value = args.get(key)
    return value if isinstance(value, int) else None


def args_object(value: JSONValue) -> dict[str, JSONValue]:
    if not isinstance(value, dict):
        raise ValueError("tool args must be an object")
    return value


def success(tool_name: str, output: dict[str, JSONValue]) -> ReactToolResult:
    return ReactToolResult(tool_name=tool_name, status="success", output=output)


def error_code(exc: Exception) -> str:
    if isinstance(exc, (MemoryFileConflictError, MemoryPublicationConflictError)):
        return "MEMORY_DRAFT_REVISION_STALE"
    if isinstance(exc, MemoryLinkValidationError):
        return "MEMORY_LINK_INVALID"
    if isinstance(exc, MemoryCatalogIntegrityError):
        return "MEMORY_CATALOG_INTEGRITY"
    if isinstance(exc, sqlite3.Error):
        return "DATABASE_FAILED"
    code = getattr(exc, "code", None)
    if isinstance(code, str):
        return code
    if isinstance(exc, DescriptorPathPolicyError):
        return "PATH_DENIED"
    if isinstance(exc, FileExistsError):
        return "PATH_DENIED"
    if isinstance(exc, (OSError, DescriptorPathError)):
        return "IO_FAILED"
    return "UNSUPPORTED_FILE"


def _parse_patch_line(raw_line: object) -> PatchLine:
    if not isinstance(raw_line, dict):
        raise ValueError("patch line must be an object")
    op = raw_line.get("op")
    text = raw_line.get("text")
    if op not in ("context", "remove", "add") or not isinstance(text, str):
        raise ValueError("patch line requires op and text")
    return PatchLine(op=op, text=text)
