"""memory_sql: one read-only SELECT over the memory tables, as a shared tool.

The words and schemas the model sees live here, once: the Action builds its
catalog entry from them, and Suggestion and the chat use ``MemorySqlSession``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast, get_args

from pantaray_agents.local_runtime.activity_summary_schedule import SummaryType
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tools.contract import (
    ReactToolCall,
    ReactToolDefinition,
    ReactToolResult,
    ToolConcurrency,
    react_tool_response_schema,
    tool_error_response,
)

from .sql import (
    DEFAULT_MEMORY_SQL_LIMIT,
    MAX_MEMORY_SQL_CELL_CHARS,
    MAX_MEMORY_SQL_LIMIT,
    MAX_MEMORY_SQL_OUTPUT_CHARS,
    MEMORY_SQL_CELL_CUT_MARKER,
    execute_memory_sql,
)

MEMORY_SQL_TOOL_NAME: Final[str] = "memory_sql"

_TABLE_GUIDE = "\n".join(
    [
        "Readable memory tables:",
        "- agent_suggestions: suggestion records; key columns suggestion_id, user_id, answer, status, created_at, updated_at.",
        "- agent_actions: executed action results; key columns action_id, suggestion_id, user_id, final_output, status, created_at, updated_at.",
        "- agent_insights: short-term insight rows; key columns insight_id, source_activity_summary_id, short_term_insight_data, status, created_at, updated_at.",
        "- agent_facts: structured fact generation rows; key columns fact_id, user_id, facts_profile_brief, structured_fact_sha256, status, created_at, updated_at.",
        "- activity_logs: time-series activity descriptions; key columns log_id, user_id, period_start, period_end, description, status. "
        "period_start/period_end label the 15-minute job window that wrote the row, not the time the activity was observed; the activity "
        "is usually observed before period_start, and further before when recording stopped or the job was delayed. When a question "
        "needs the exact time something was observed, use source_records.",
        "- activity_summaries: aggregated activity summaries; key columns summary_id, summary_type, period_start, period_end, summary, status. "
        f"summary_type is one of {', '.join(get_args(SummaryType))}.",
        "- source_records: verbatim screen quotes checked against the recording; key columns record_id, run_id "
        "(the activity_logs.log_id that wrote it), observed_at (when the screen showed it), app_name, window_title, source "
        "(the displayed document, page, or conversation header), speaker, shown_time (the time displayed with the quote, "
        "or empty), quote. It has no status column.",
        "status is the state of the job that wrote the row (processing, success, error, canceled, timeout; agent_actions also queued). "
        "success marks rows that finished normally; error and canceled rows have also finished, so when counting finished work, decide "
        "from each table's statuses which ones count.",
        "Time columns hold UTC ISO8601 strings ending in Z. For the user's local date use date(col, 'localtime'); to filter by local times, convert the boundaries to UTC first.",
    ]
)

MEMORY_SQL_DESCRIPTION: Final[str] = (
    "Run a read-only SQLite SELECT against memory-related local tables. "
    "Use this to strictly filter, order, or count rows of the memory "
    "tables by time range, source type, IDs, or neighboring rows."
)
MEMORY_SQL_WHAT: Final[str] = (
    "Use this to inspect memory with SQL when precise filtering, "
    "ordering, aggregation, joins, or neighboring-row lookup is useful.\n\n"
    "Accepted query shape:\n"
    "- A single read-only SELECT statement.\n"
    "- WITH clauses are allowed only when the final statement is SELECT.\n"
    "- Write values directly in the SQL as literals, for example "
    "period_start >= '2026-09-27T00:00:00Z'.\n"
    "- The executor applies the current action's memory scope automatically.\n\n"
    "Result limits: at most limit rows (default "
    f"{DEFAULT_MEMORY_SQL_LIMIT}, maximum {MAX_MEMORY_SQL_LIMIT}) and "
    f"about {MAX_MEMORY_SQL_OUTPUT_CHARS:,} characters in total; a text "
    f"cell longer than {MAX_MEMORY_SQL_CELL_CHARS:,} characters is cut "
    f"and ends with a {MEMORY_SQL_CELL_CUT_MARKER} ...] marker. row_count "
    "counts the rows returned, not every row that matched. When "
    "anything was cut, truncated is true and notes say which limit "
    "applied and how to read the rest.\n\n" + _TABLE_GUIDE
)
MEMORY_SQL_WHEN: Final[str] = (
    "Use when rows of the readable memory tables must be strictly "
    "filtered by time range or type, ordered, counted or totaled, joined, "
    "or looked up by ID, status, or neighboring rows, for example every "
    "activity description in a time range or the project that took "
    "the most time last week. When the question also names a topic, find "
    "candidates with memory_search and its time_hint first. It can "
    "also follow up on IDs or timestamps that memory_search returned."
)
MEMORY_SQL_PITFALLS: Final[str] = (
    "Only SELECT / WITH ... SELECT is accepted. Do not use PRAGMA, "
    "INSERT, UPDATE, DELETE, DDL, ATTACH, temp tables, multiple "
    "statements, or tables outside the documented memory allowlist. "
    "Do not add account-scope authorization filters; the executor "
    "applies the action's memory scope automatically. These tables do "
    "not include agent experience or long-term insight "
    "fragments, so an empty result does not mean nothing is remembered. "
    "Use memory_search to find memories by words or meaning when no "
    "structural filter "
    "applies, and get_memory_reference for explicit fragment links."
)
MEMORY_SQL_SQL_FIELD: Final[dict[str, JSONValue]] = {"type": "string", "minLength": 1}
MEMORY_SQL_SQL_FIELD_DESCRIPTION: Final[str] = (
    "Single read-only SELECT statement.\n"
    "- WITH ... SELECT is allowed.\n"
    "- Write values directly in the SQL as literals.\n"
    "- Do not include multiple statements, PRAGMA, writes, DDL, "
    "ATTACH, or manual account-scope authorization filters."
)
MEMORY_SQL_LIMIT_FIELD: Final[dict[str, JSONValue]] = {
    "type": "integer",
    "minimum": 1,
    "maximum": MAX_MEMORY_SQL_LIMIT,
}
MEMORY_SQL_LIMIT_FIELD_DESCRIPTION: Final[str] = (
    "Optional max rows to return.\n"
    f"- Default: {DEFAULT_MEMORY_SQL_LIMIT}.\n"
    f"- Maximum: {MAX_MEMORY_SQL_LIMIT}."
)
MEMORY_SQL_RESULT_SCHEMA: Final[dict[str, JSONValue]] = {
    "type": "object",
    "properties": {
        "columns": {"type": "array", "items": {"type": "string"}},
        "rows": {"type": "array", "items": {"type": "object"}},
        "row_count": {"type": "integer"},
        "truncated": {"type": "boolean"},
        "notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["columns", "rows", "row_count", "truncated", "notes"],
    "additionalProperties": False,
}
# Keys sorted at every level: a reordered schema is a different tool prefix to
# the provider's prompt cache.
MEMORY_SQL_REQUEST_SCHEMA: Final[dict[str, JSONValue]] = {
    "additionalProperties": False,
    "properties": {
        "limit": dict(
            sorted(
                {
                    "description": MEMORY_SQL_LIMIT_FIELD_DESCRIPTION,
                    **MEMORY_SQL_LIMIT_FIELD,
                }.items()
            )
        ),
        "sql": dict(
            sorted(
                {
                    "description": MEMORY_SQL_SQL_FIELD_DESCRIPTION,
                    **MEMORY_SQL_SQL_FIELD,
                }.items()
            )
        ),
    },
    "required": ["sql"],
    "type": "object",
}

# The "when" part points at memory_search's time_hint, which the memory_search of
# Suggestion and the chat does not take, so only the other parts are shown.
_SESSION_DESCRIPTION = "\n\n".join(
    (MEMORY_SQL_DESCRIPTION, MEMORY_SQL_WHAT, MEMORY_SQL_PITFALLS)
)


@dataclass(frozen=True, slots=True)
class MemorySqlSession:
    """memory_sql bound to one database and user."""

    db_path: Path
    busy_timeout_ms: int
    user_id: str

    def definition(self) -> ReactToolDefinition:
        return ReactToolDefinition(
            name=MEMORY_SQL_TOOL_NAME,
            description=_SESSION_DESCRIPTION,
            request_schema=MEMORY_SQL_REQUEST_SCHEMA,
            response_schema=react_tool_response_schema(
                success_schema=MEMORY_SQL_RESULT_SCHEMA
            ),
            execute=self._execute,
            concurrency=ToolConcurrency("parallel"),
        )

    async def _execute(self, call: ReactToolCall, _step: int) -> ReactToolResult:
        args = call.tool_args
        assert isinstance(args, dict)  # the registry validated the request schema
        sql = args["sql"]
        limit = args.get("limit", DEFAULT_MEMORY_SQL_LIMIT)
        assert isinstance(sql, str) and isinstance(limit, int)
        result = await asyncio.to_thread(
            execute_memory_sql,
            db_path=str(self.db_path),
            busy_timeout_ms=self.busy_timeout_ms,
            user_id=self.user_id,
            sql=sql,
            limit=limit,
        )
        if result.error or result.data is None:
            return tool_error_response(
                tool_name=call.tool_name,
                error_code="MEMORY_SQL_REQUEST_REJECTED",
                message=str(result.error),
            )
        return ReactToolResult(
            tool_name=call.tool_name,
            status="success",
            output=cast(JSONValue, result.data),
        )


__all__ = [
    "MEMORY_SQL_DESCRIPTION",
    "MEMORY_SQL_LIMIT_FIELD",
    "MEMORY_SQL_LIMIT_FIELD_DESCRIPTION",
    "MEMORY_SQL_PITFALLS",
    "MEMORY_SQL_REQUEST_SCHEMA",
    "MEMORY_SQL_RESULT_SCHEMA",
    "MEMORY_SQL_SQL_FIELD",
    "MEMORY_SQL_SQL_FIELD_DESCRIPTION",
    "MEMORY_SQL_TOOL_NAME",
    "MEMORY_SQL_WHAT",
    "MEMORY_SQL_WHEN",
    "MemorySqlSession",
]
