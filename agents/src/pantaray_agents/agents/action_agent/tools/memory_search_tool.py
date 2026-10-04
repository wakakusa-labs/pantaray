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
    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ToolPolicyValidationError(
            "query is required.",
            details={
                "code": "INVALID_MEMORY_SEARCH_QUERY",
                "reason": "memory_search requires a non-empty natural-language query.",
                "path": ["query"],
                "message": "query is required.",
            },
        )

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
        description=(
            "Search current active Memory Catalog fragments with exact, lexical, and "
            "semantic retrieval. "
            "Use this for recall by words and meaning across stock knowledge, "
            "flow knowledge, and prior agent work, including a topic tied to a "
            "time, which goes in time_hint. Use memory_sql only to strictly "
            "filter, order, or count rows of its tables."
        ),
        guide=ToolGuideSpec(
            what=(
                "Broad deterministic recall over current local memory. It can retrieve "
                "organized stock knowledge (long-term insights and structured facts), "
                "timestamped flow knowledge (activity descriptions, activity summaries, "
                "and short-term insights), and prior agent work records. The "
                "implementation preserves exact anchors, corroborates lexical and "
                "semantic matches, and uses optional time-hint ranking for activity "
                "memory. Tombstoned, corrupt, and historical revisions are excluded."
            ),
            when=(
                "Use before answering when prior user, project, workspace, activity, "
                "or agent-work context may materially affect the answer or action. "
                "Search by shared anchors such as project names, repo paths, file "
                "paths, issue IDs, people, and domain terms. Use "
                "focus='agent_work' for actions and their evidence, "
                "focus='stable_knowledge' "
                "for organized stock context, focus='activity' for recent flow "
                "context, and focus='all' when the same topic may span both. If "
                "a result contains a note-bearing ref, use get_memory_reference with "
                "that result's context_handle to follow the exact immutable link. "
                "When a question names both a topic and a time, such as the "
                "estimate document seen yesterday, search here with time_hint to "
                "find candidates. When it is bounded only by time, such as what "
                "happened in a window, or rows of memory_sql's tables must be "
                "ordered or counted, use memory_sql. For the exact time something "
                "was observed, use the Observed: line of each quote in source_records "
                "results, or memory_sql's source_records table; the result's "
                "observed_at is the latest time in its run."
            ),
            pitfalls=(
                "Do not treat one source as complete by itself. Stock knowledge can be "
                "stale; flow knowledge is fresher but noisier and less organized. "
                "When context matters, reconcile stock and flow evidence. time_hint "
                "only ranks results near a time and does not filter them; to "
                "strictly restrict rows of memory_sql's tables to a time range or "
                "type, use memory_sql. Write the query with concrete names, IDs, "
                "paths, errors, and both Japanese/English terms when useful. Words "
                "are matched from three characters; below that, single characters "
                "are not matched, and two-letter ASCII words only whole and when "
                "written in capitals (PR, UI) or with a digit (#7, v2). notes in "
                "the result name any query term that was not matched and say when "
                "the result list was full, so more may match."
            ),
        ),
        execution_policy=tool_execution_policy(
            intent_class="read_only",
            default_timeout_ms=30_000,
        ),
        input_spec=InputSpec(
            fields=(
                field_spec(
                    name="query",
                    schema={"type": "string", "minLength": 1},
                    required=True,
                    description="Natural-language or keyword query for memory recall.",
                ),
                field_spec(
                    name="focus",
                    schema={
                        "type": "string",
                        "enum": list(ACTION_MEMORY_SEARCH_FOCUS_VALUES),
                    },
                    description=(
                        "Optional broad search focus. Default all. This is a source "
                        "selection hint, not a table name."
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
                        "Optional ranking hint. Results near center are boosted, but "
                        "other relevant results can still appear."
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
                            "match_kind": {
                                "type": "string",
                                "enum": [
                                    "exact",
                                    "corroborated",
                                    "lexical",
                                    "semantic",
                                ],
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
                            "match_kind",
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
