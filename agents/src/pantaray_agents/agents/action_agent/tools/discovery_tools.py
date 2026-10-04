"""Brokered read-only workspace discovery tool definitions."""

from __future__ import annotations

from typing import cast

from pantaray_agents.local_runtime.tooling.brokering.broker_discovery import (
    GREP_MAX_OUTPUT_BYTES,
)
from pantaray_agents.local_runtime.tooling.brokering.broker_discovery_ripgrep import (
    GREP_MAX_LINE_CHARS,
    RIPGREP_TIMEOUT_SECONDS,
)
from pantaray_agents.local_runtime.tooling.brokering.broker_protocol import (
    GlobToolArgs,
    GrepToolArgs,
    ListToolArgs,
)
from pantaray_agents.schema.agent.base import JSONValue

from .base import (
    ToolDefinition,
    ToolExecutionPolicy,
    ToolGuideSpec,
    ToolSpec,
    tool_execution_policy,
)
from .broker_tool_input_schema import (
    BrokerToolFieldPresentation,
    broker_tool_input_spec_from_model,
)

DISCOVERY_TIMEOUT_MS = 5_000
DISCOVERY_RESULT_LIMIT_MAX = 500
LIST_MAX_DEPTH = 6
DISCOVERY_LOCAL_PATH_DESCRIPTION = (
    "Local filesystem path. Use `.` for the current working directory or an "
    "absolute path. Allowed paths follow Read/search access in Workspace Path Rules."
)
DISCOVERY_ENTRY_SCHEMA = cast(
    dict[str, JSONValue],
    {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "kind": {"type": "string", "enum": ["file", "directory"]},
            "name": {"type": "string"},
        },
        "required": ["path", "kind", "name"],
        "additionalProperties": False,
    },
)
DISCOVERY_TRUNCATION_REASON_SCHEMA = cast(
    dict[str, JSONValue],
    {
        "type": ["string", "null"],
        "enum": [
            "limit",
            "scan_budget",
            "timeout",
            "output_bytes",
            "line_length",
            None,
        ],
    },
)
DISCOVERY_RETRY_HINT_SCHEMA = cast(
    dict[str, JSONValue],
    {"type": ["string", "null"]},
)
DISCOVERY_WARNING_SCHEMA = cast(
    dict[str, JSONValue],
    {"type": ["string", "null"]},
)

LIST_TOOL_FIELD_PRESENTATION = (
    BrokerToolFieldPresentation(
        name="path",
        description=DISCOVERY_LOCAL_PATH_DESCRIPTION,
    ),
    BrokerToolFieldPresentation(
        name="max_depth",
        description=(
            f"Maximum directory depth to include from path, 0-{LIST_MAX_DEPTH}. "
            "Prefer 1 for an Explorer/Finder-like directory view."
        ),
    ),
    BrokerToolFieldPresentation(
        name="limit",
        description=(
            f"Maximum number of entries to return, 1-{DISCOVERY_RESULT_LIMIT_MAX}. "
            "This is a hard cap, not a page size; list has no offset."
        ),
    ),
)

GLOB_TOOL_FIELD_PRESENTATION = (
    BrokerToolFieldPresentation(
        name="base_path",
        description=DISCOVERY_LOCAL_PATH_DESCRIPTION,
    ),
    BrokerToolFieldPresentation(
        name="pattern",
        description="Glob pattern relative to base_path, for example `**/*.py`.",
    ),
    BrokerToolFieldPresentation(
        name="limit",
        description="Maximum number of matches to return.",
    ),
)

GREP_TOOL_FIELD_PRESENTATION = (
    BrokerToolFieldPresentation(
        name="base_path",
        description=DISCOVERY_LOCAL_PATH_DESCRIPTION,
    ),
    BrokerToolFieldPresentation(
        name="pattern",
        description="Regular expression to search for.",
    ),
    BrokerToolFieldPresentation(
        name="include_glob",
        description="Optional glob relative to base_path, for example `**/*.py`.",
    ),
    BrokerToolFieldPresentation(
        name="max_matches",
        description=(
            "Maximum number of matching lines to return, "
            f"1-{DISCOVERY_RESULT_LIMIT_MAX}; "
            f"default {GrepToolArgs.model_fields['max_matches'].default}."
        ),
    ),
)


def _discovery_policy() -> ToolExecutionPolicy:
    return tool_execution_policy(
        intent_class="read_only",
        required_capabilities=("scoped_read",),
        default_timeout_ms=DISCOVERY_TIMEOUT_MS,
    )


LIST_TOOL = ToolDefinition.from_spec(
    ToolSpec(
        tool_id="list",
        name="List Local Directory",
        description="List files and directories under a readable local directory.",
        guide=ToolGuideSpec(
            what=(
                "Use this to inspect the immediate structure of a readable local "
                "directory without running a shell command."
            ),
            when="Use before reading files or when you need directory entries.",
            pitfalls=(
                "Allowed paths follow Read/search access in Workspace Path Rules. "
                "Relative paths are resolved from the current working directory. "
                "List is not paginated: do not pass offset, do not expect next_offset, "
                f"and keep limit at {DISCOVERY_RESULT_LIMIT_MAX} or less. "
                "Typical first call: path=., max_depth=1. If truncated=true, "
                "inspect truncation_reason and retry with a narrower path or smaller "
                "max_depth. Use glob for path patterns and grep for text search."
            ),
        ),
        execution_policy=_discovery_policy(),
        input_spec=broker_tool_input_spec_from_model(
            model=ListToolArgs,
            fields=LIST_TOOL_FIELD_PRESENTATION,
            description="List a readable local directory.",
        ),
        output_schema={
            "type": "object",
            "properties": {
                "status": {"type": "string"},
                "entries": {"type": "array", "items": DISCOVERY_ENTRY_SCHEMA},
                "truncated": {"type": "boolean"},
                "truncation_reason": DISCOVERY_TRUNCATION_REASON_SCHEMA,
                "retry_hint": DISCOVERY_RETRY_HINT_SCHEMA,
                "warning": DISCOVERY_WARNING_SCHEMA,
            },
            "required": [
                "status",
                "entries",
                "truncated",
                "truncation_reason",
                "retry_hint",
                "warning",
            ],
            "additionalProperties": False,
        },
    )
)

GLOB_TOOL = ToolDefinition.from_spec(
    ToolSpec(
        tool_id="glob",
        name="Glob Local Files",
        description="Find readable local files by glob pattern under a local base path.",
        guide=ToolGuideSpec(
            what=(
                "Use this to find files by path pattern without shell pipes; "
                "base_path is a local path."
            ),
            when="Use when you know a path pattern such as `**/*.py`.",
            pitfalls=(
                "base_path uses local path semantics. Allowed paths follow Read/search "
                "access in Workspace Path Rules. pattern "
                "is relative to base_path and must not be absolute or contain parent "
                "directory parts. Use ** only when recursive matching is intended. "
                "Typical first call: base_path=., "
                "pattern=**/*.py. If truncated=true, inspect truncation_reason. If "
                "warning or retry_hint is present, retry the same tool with narrower inputs."
            ),
        ),
        execution_policy=_discovery_policy(),
        input_spec=broker_tool_input_spec_from_model(
            model=GlobToolArgs,
            fields=GLOB_TOOL_FIELD_PRESENTATION,
            description="Find readable local paths by glob.",
        ),
        output_schema={
            "type": "object",
            "properties": {
                "status": {"type": "string"},
                "matches": {"type": "array", "items": DISCOVERY_ENTRY_SCHEMA},
                "truncated": {"type": "boolean"},
                "truncation_reason": DISCOVERY_TRUNCATION_REASON_SCHEMA,
                "retry_hint": DISCOVERY_RETRY_HINT_SCHEMA,
                "warning": DISCOVERY_WARNING_SCHEMA,
            },
            "required": [
                "status",
                "matches",
                "truncated",
                "truncation_reason",
                "retry_hint",
                "warning",
            ],
            "additionalProperties": False,
        },
    )
)

GREP_TOOL = ToolDefinition.from_spec(
    ToolSpec(
        tool_id="grep",
        name="Grep Local Text",
        description="Search readable local text files of any size by regular expression.",
        guide=ToolGuideSpec(
            what=(
                "Use this to search text inside readable local files without running a shell "
                "pipeline; base_path is a local path."
            ),
            when="Use when you need lines matching a text or regex pattern.",
            pitfalls=(
                "base_path uses local path semantics. Allowed paths follow Read/search "
                "access in Workspace Path Rules. "
                "include_glob is relative to base_path; use it to narrow file types before searching. "
                "pattern is a ripgrep regular expression; escape regex metacharacters "
                "when searching literal text. Typical first call: base_path=., "
                "pattern=TODO, include_glob=**/*.py. "
                "Limits: symlinks are not followed; a binary file (with NUL bytes) "
                "that matches is named in warning, without lines; output stops at "
                f"max_matches lines or {GREP_MAX_OUTPUT_BYTES // 1024} KB; a line longer "
                f"than {GREP_MAX_LINE_CHARS} characters comes back as an excerpt around "
                f"its first match; the search stops after {RIPGREP_TIMEOUT_SECONDS:g} "
                "seconds. When a limit applies, truncated, warning and retry_hint say "
                "which one and what to do next; skipped_files counts paths that could "
                "not be read."
            ),
        ),
        execution_policy=_discovery_policy(),
        input_spec=broker_tool_input_spec_from_model(
            model=GrepToolArgs,
            fields=GREP_TOOL_FIELD_PRESENTATION,
            description="Search workspace text.",
        ),
        output_schema={
            "type": "object",
            "properties": {
                "status": {"type": "string"},
                "matches": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "line_number": {"type": "integer"},
                            "line": {"type": "string"},
                        },
                        "required": ["path", "line_number", "line"],
                        "additionalProperties": False,
                    },
                },
                "truncated": {"type": "boolean"},
                "truncation_reason": DISCOVERY_TRUNCATION_REASON_SCHEMA,
                "retry_hint": DISCOVERY_RETRY_HINT_SCHEMA,
                "warning": DISCOVERY_WARNING_SCHEMA,
                "skipped_files": {"type": "integer", "minimum": 0},
            },
            "required": [
                "status",
                "matches",
                "truncated",
                "truncation_reason",
                "retry_hint",
                "warning",
                "skipped_files",
            ],
            "additionalProperties": False,
        },
    )
)
