"""memory_search ツール定義。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import NoReturn

from pantaray_agents.agents.action_agent.tools.base import (
    ToolPolicyValidationError,
    ToolValidationDetails,
)
from pantaray_agents.local_runtime.memory_catalog.search_policy import (
    MEMORY_CATALOG_TO_SEARCH_SOURCE,
    MEMORY_SEARCH_FOCUS_VALUES,
)
from pantaray_agents.repositories.action_support.memory_search_helpers import (
    MEMORY_SEARCH_DEFAULT_LIMIT,
    MEMORY_SEARCH_MAX_LIMIT,
    MEMORY_SEARCH_MAX_TIME_HINT_RADIUS_HOURS,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.schema.memory_embeddings import (
    MEMORY_SEARCH_SEMANTIC_STATUS_VALUES,
)
from pantaray_agents.tools.contract import ToolConcurrency
from pantaray_agents.tools.memory.search_contract import (
    KEYWORDS_DESCRIPTION,
    MEMORY_SEARCH_AVOID,
    MEMORY_SEARCH_DESCRIPTION,
    MEMORY_SEARCH_PURPOSE,
    MEMORY_SEARCH_WHEN,
    QUERY_DESCRIPTION,
    focus_description,
    keywords_schema,
    parse_memory_search_input,
    query_schema,
)
from pantaray_agents.utils.strict_numbers import is_strict_int
from pantaray_agents.utils.timestamps import parse_iso8601_utc

from .base import (
    InputSpec,
    ToolDefinition,
    ToolGuideSpec,
    ToolSpec,
    field_spec,
    tool_execution_policy,
)

ACTION_MEMORY_SEARCH_FOCUS_VALUES = MEMORY_SEARCH_FOCUS_VALUES


def validate_memory_search_args(args: Mapping[str, JSONValue]) -> None:
    try:
        parse_memory_search_input(args)
    except ValueError as exc:
        raise ToolPolicyValidationError(
            str(exc),
            details={
                "code": "INVALID_MEMORY_SEARCH_INPUT",
                "reason": str(exc),
                "path": [],
                "message": str(exc),
            },
        ) from exc

    focus = args.get("focus", "all")
    if focus not in ACTION_MEMORY_SEARCH_FOCUS_VALUES:
        raise ToolPolicyValidationError(
            "focus is invalid.",
            details={
                "code": "INVALID_MEMORY_SEARCH_FOCUS",
                "reason": (
                    "focus must be one of: "
                    f"{', '.join(ACTION_MEMORY_SEARCH_FOCUS_VALUES)}."
                ),
                "path": ["focus"],
                "message": "focus is invalid.",
            },
        )

    time_hint = args.get("time_hint")
    if time_hint is None:
        return
    if not isinstance(time_hint, dict):
        _raise_time_hint_error("time_hint must be an object.", ["time_hint"])

    center = time_hint.get("center")
    radius_hours = time_hint.get("radius_hours")
    if (center is None) != (radius_hours is None):
        _raise_time_hint_error(
            "time_hint.center and time_hint.radius_hours must be provided together.",
            ["time_hint"],
        )
    if center is not None:
        if not isinstance(center, str):
            _raise_time_hint_error(
                "time_hint.center must be an ISO8601 string.", ["time_hint", "center"]
            )
        try:
            parse_iso8601_utc(center)
        except ValueError as exc:
            raise ToolPolicyValidationError(
                "time_hint.center must be an ISO8601 string.",
                details={
                    "code": "INVALID_MEMORY_SEARCH_TIME_HINT",
                    "reason": "time_hint.center could not be parsed as ISO8601.",
                    "path": ["time_hint", "center"],
                    "message": str(exc),
                },
            ) from exc

    if radius_hours is not None and (
        not is_strict_int(radius_hours) or radius_hours <= 0
    ):
        _raise_time_hint_error(
            "time_hint.radius_hours must be a positive integer.",
            ["time_hint", "radius_hours"],
        )
    if (
        is_strict_int(radius_hours)
        and radius_hours > MEMORY_SEARCH_MAX_TIME_HINT_RADIUS_HOURS
    ):
        _raise_time_hint_error(
            "time_hint.radius_hours exceeds the maximum supported radius.",
            ["time_hint", "radius_hours"],
        )


def _raise_time_hint_error(
    message: str,
    path: Sequence[str | int],
) -> NoReturn:
    details: ToolValidationDetails = {
        "code": "INVALID_MEMORY_SEARCH_TIME_HINT",
        "reason": message,
        "path": list(path),
        "message": message,
    }
    raise ToolPolicyValidationError(
        message,
        details=details,
    )


MEMORY_SEARCH_TOOL = ToolDefinition.from_spec(
    ToolSpec(
        tool_id="memory_search",
        name="Memory Search",
        description=MEMORY_SEARCH_DESCRIPTION,
        guide=ToolGuideSpec(
            what=MEMORY_SEARCH_PURPOSE,
            when=MEMORY_SEARCH_WHEN,
            pitfalls=MEMORY_SEARCH_AVOID,
        ),
        # Reads memory_context_epoch and writes the extended epoch back; two at
        # once would lose one extension and leave later handles unresolvable.
        concurrency=ToolConcurrency("parallel", shared_state="memory_context_epoch"),
        execution_policy=tool_execution_policy(
            intent_class="read_only",
            default_timeout_ms=30_000,
        ),
        input_spec=InputSpec(
            fields=(
                field_spec(
                    name="keywords",
                    schema=keywords_schema(),
                    description=KEYWORDS_DESCRIPTION,
                ),
                field_spec(
                    name="query", schema=query_schema(), description=QUERY_DESCRIPTION
                ),
                field_spec(
                    name="focus",
                    schema={
                        "type": "string",
                        "enum": list(ACTION_MEMORY_SEARCH_FOCUS_VALUES),
                    },
                    description=focus_description(
                        ACTION_MEMORY_SEARCH_FOCUS_VALUES, "all"
                    ),
                ),
                field_spec(
                    name="time_hint",
                    schema={
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "center": {"type": "string", "format": "date-time"},
                            "radius_hours": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": MEMORY_SEARCH_MAX_TIME_HINT_RADIUS_HOURS,
                            },
                        },
                    },
                    description=(
                        "Optional ranking hint: results near center rank higher, "
                        "and nothing is filtered out. To keep strictly to a time "
                        "range, use memory_sql."
                    ),
                    children=(
                        field_spec(
                            name="center",
                            schema={"type": "string", "format": "date-time"},
                            required=True,
                            description=(
                                "ISO8601 timestamp to rank nearby memories higher. "
                                "Times shown to you are local with an offset; pass "
                                "the same offset, e.g. 2026-09-27T06:50+09:00."
                            ),
                        ),
                        field_spec(
                            name="radius_hours",
                            schema={
                                "type": "integer",
                                "minimum": 1,
                                "maximum": MEMORY_SEARCH_MAX_TIME_HINT_RADIUS_HOURS,
                            },
                            required=True,
                            description="Ranking radius around center, in hours.",
                        ),
                    ),
                ),
                field_spec(
                    name="limit",
                    schema={
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MEMORY_SEARCH_MAX_LIMIT,
                    },
                    description=f"Optional result limit. Default: {MEMORY_SEARCH_DEFAULT_LIMIT}.",
                ),
            )
        ),
        output_schema={
            "type": "object",
            "properties": {
                "notes": {"type": "array", "items": {"type": "string"}},
                "semantic_status": {
                    "type": "string",
                    "enum": list(MEMORY_SEARCH_SEMANTIC_STATUS_VALUES),
                },
                "semantic_error_code": {"type": ["string", "null"]},
                "results": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "source": {
                                "type": "string",
                                "enum": list(MEMORY_CATALOG_TO_SEARCH_SOURCE.values()),
                            },
                            "record_id": {"type": "string"},
                            "content": {"type": "string"},
                            "source_path": {"type": "string"},
                            "heading_path": {"type": ["string", "null"]},
                            "observed_at": {"type": "string"},
                            "matched_by": {
                                "type": "string",
                                "enum": ["keywords", "query", "both"],
                            },
                            "context_handle": {"type": "string", "minLength": 1},
                        },
                        "required": [
                            "source",
                            "record_id",
                            "content",
                            "source_path",
                            "heading_path",
                            "observed_at",
                            "matched_by",
                            "context_handle",
                        ],
                        "additionalProperties": False,
                    },
                },
            },
            "required": [
                "results",
                "semantic_status",
                "semantic_error_code",
                "notes",
            ],
            "additionalProperties": False,
        },
        runtime_config={
            "default_limit": MEMORY_SEARCH_DEFAULT_LIMIT,
            "max_limit": MEMORY_SEARCH_MAX_LIMIT,
        },
        pre_validate_args=validate_memory_search_args,
    )
)
