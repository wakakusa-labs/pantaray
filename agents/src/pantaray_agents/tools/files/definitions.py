from __future__ import annotations

from pantaray_agents.local_runtime.tooling.brokering.broker_grep_lines import (
    GREP_MAX_LINE_CHARS,
)
from pantaray_agents.local_runtime.tooling.brokering.workspace_descriptor_access import (
    SEARCH_TIMEOUT_SECONDS,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tools.contract import (
    ReactToolDefinition,
    ReactToolExecutor,
    react_tool_response_schema,
)

from .access import LIST_MAX_DEPTH

READ_MAX_LINES = 200
DISCOVERY_MAX_RESULTS = 100

_NULLABLE_STRING: dict[str, JSONValue] = {"type": ["string", "null"]}
_PAGE_PROPERTIES: dict[str, JSONValue] = {
    "next_offset": {"type": ["integer", "null"], "minimum": 1},
    "truncated": {"type": "boolean"},
    "truncation_reason": _NULLABLE_STRING,
}
_SEARCH_NOTE_PROPERTIES: dict[str, JSONValue] = {
    "warning": _NULLABLE_STRING,
    "retry_hint": _NULLABLE_STRING,
}
_SEARCH_LIMITS = (
    "Results come in path order. Symlinks are not followed, listed or "
    "searched; a directory at max_depth is listed without its contents; glob "
    f"and grep stop after {SEARCH_TIMEOUT_SECONDS:g} seconds. When something "
    "is left out, warning and retry_hint say what and how to reach it."
)


def _success_schema(
    *,
    properties: dict[str, JSONValue],
    required: tuple[str, ...],
) -> dict[str, JSONValue]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["status", *required],
        "properties": {"status": {"const": "success"}, **properties},
    }


def _definition(
    *,
    name: str,
    description: str,
    properties: dict[str, JSONValue],
    required: tuple[str, ...],
    success_schema: dict[str, JSONValue],
    execute: ReactToolExecutor,
) -> ReactToolDefinition:
    return ReactToolDefinition(
        name=name,
        description=description,
        request_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": properties,
            "required": list(required),
        },
        response_schema=react_tool_response_schema(success_schema=success_schema),
        execute=execute,
    )


def build_read_only_file_definitions(
    *,
    read: ReactToolExecutor,
    list_paths: ReactToolExecutor,
    glob: ReactToolExecutor,
    grep: ReactToolExecutor,
) -> tuple[ReactToolDefinition, ...]:
    root: dict[str, JSONValue] = {
        "type": "string",
        "minLength": 1,
        "pattern": r"\S",
    }
    path: dict[str, JSONValue] = {
        "type": "string",
        "minLength": 1,
        "pattern": r"\S",
    }
    offset: dict[str, JSONValue] = {"type": "integer", "minimum": 1}
    return (
        _definition(
            name="read",
            description=(
                "Read bounded text from a file or context handle in an explicit "
                "readable root. Continue with both next_offset and next_column."
            ),
            properties={
                "root": root,
                "path": path,
                "offset": offset,
                "column": offset,
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": READ_MAX_LINES,
                },
            },
            required=("root", "path", "offset", "column", "limit"),
            success_schema=_success_schema(
                properties={
                    "root": {"type": "string"},
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "offset": {"type": "integer", "minimum": 1},
                    "column": {"type": "integer", "minimum": 1},
                    "end_line": {"type": "integer", "minimum": 0},
                    "end_column": {"type": "integer", "minimum": 0},
                    "total_lines": {"type": ["integer", "null"], "minimum": 0},
                    **_PAGE_PROPERTIES,
                    "next_column": {"type": ["integer", "null"], "minimum": 1},
                    "retry_hint": _NULLABLE_STRING,
                },
                required=(
                    "root",
                    "path",
                    "content",
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
            ),
            execute=read,
        ),
        _definition(
            name="list",
            description="List paths in an explicit readable root. " + _SEARCH_LIMITS,
            properties={
                "root": root,
                "path": path,
                "max_depth": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": LIST_MAX_DEPTH,
                },
                "offset": offset,
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": DISCOVERY_MAX_RESULTS,
                },
            },
            required=("root", "path", "max_depth", "offset", "limit"),
            success_schema=_discovery_schema(
                item_schema={
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["path", "kind", "name"],
                    "properties": {
                        "path": {"type": "string"},
                        "kind": {"type": "string", "enum": ["file", "directory"]},
                        "name": {"type": "string"},
                    },
                },
                collection_name="entries",
                include_path=True,
            ),
            execute=list_paths,
        ),
        _definition(
            name="glob",
            description="Find files by glob in an explicit readable root. "
            + _SEARCH_LIMITS,
            properties={
                "root": root,
                "base_path": path,
                "pattern": {"type": "string", "minLength": 1},
                "offset": offset,
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": DISCOVERY_MAX_RESULTS,
                },
            },
            required=("root", "base_path", "pattern", "offset", "limit"),
            success_schema=_discovery_schema(
                item_schema={"type": "string"},
                collection_name="matches",
                include_path=False,
            ),
            execute=glob,
        ),
        _definition(
            name="grep",
            description=(
                "Search text files of any size in an explicit readable root. A "
                f"line over {GREP_MAX_LINE_CHARS} characters comes back as an "
                "excerpt around its first match; a matching binary file is named "
                "in warning; skipped_files counts unreadable files. Skipped "
                "symlinks are not counted or named. " + _SEARCH_LIMITS
            ),
            properties={
                "root": root,
                "base_path": path,
                "pattern": {"type": "string", "minLength": 1},
                "include_glob": {"type": ["string", "null"]},
                "offset": offset,
                "max_matches": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": DISCOVERY_MAX_RESULTS,
                },
            },
            required=(
                "root",
                "base_path",
                "pattern",
                "include_glob",
                "offset",
                "max_matches",
            ),
            success_schema=_grep_schema(),
            execute=grep,
        ),
    )


def _discovery_schema(
    *, item_schema: dict[str, JSONValue], collection_name: str, include_path: bool
) -> dict[str, JSONValue]:
    path_key = "path" if include_path else "base_path"
    return _success_schema(
        properties={
            "root": {"type": "string"},
            path_key: {"type": "string"},
            collection_name: {"type": "array", "items": item_schema},
            **_PAGE_PROPERTIES,
            **_SEARCH_NOTE_PROPERTIES,
        },
        required=(
            "root",
            path_key,
            collection_name,
            "next_offset",
            "truncated",
            "truncation_reason",
            "warning",
            "retry_hint",
        ),
    )


def _grep_schema() -> dict[str, JSONValue]:
    return _success_schema(
        properties={
            "root": {"type": "string"},
            "base_path": {"type": "string"},
            "matches": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["path", "line_number", "line"],
                    "properties": {
                        "path": {"type": "string"},
                        "line_number": {"type": "integer", "minimum": 1},
                        "line": {"type": "string"},
                    },
                },
            },
            **_PAGE_PROPERTIES,
            **_SEARCH_NOTE_PROPERTIES,
            "skipped_files": {"type": "integer", "minimum": 0},
        },
        required=(
            "root",
            "base_path",
            "matches",
            "next_offset",
            "truncated",
            "truncation_reason",
            "warning",
            "retry_hint",
            "skipped_files",
        ),
    )


__all__ = [
    "DISCOVERY_MAX_RESULTS",
    "READ_MAX_LINES",
    "build_read_only_file_definitions",
]
