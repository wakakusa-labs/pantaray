from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from pantaray_agents.agents.artifact_react import (
    ReactToolCall,
    ReactToolDefinition,
    ReactToolResult,
    react_tool_response_schema,
    tool_error_response,
)
from pantaray_agents.local_runtime.suggestion_state.shared import configure_connection
from pantaray_agents.schema.agent.action_history import history_fetch_refs_schema
from pantaray_agents.schema.agent.base import JSONValue

from .action_history_contract import (
    StoredActionHistoryError,
    history_fetch_step,
    history_fetch_success_schema,
    memory_tool_output_json,
    parse_from_step,
    parse_history_refs,
    parse_page,
    parse_search_request,
    search_match,
    search_success_schema,
    step_list_success_schema,
    step_metadata,
)

LIST_ACTION_STEPS_TOOL_NAME = "list_action_steps"
SEARCH_ACTION_STEPS_TOOL_NAME = "search_action_steps"
HISTORY_FETCH_TOOL_NAME = "history_fetch"


@dataclass(frozen=True, slots=True)
class ActionTurnWindow:
    """The steps of one Action the run may read.

    Listing and search start at the turns the run records; ``from_step`` reaches
    earlier steps, down to step 1, as context. Reading ends with the newest turn
    the run completed, so a turn still in progress stays out.
    """

    action_id: str
    turn_start_step_number: int
    turn_end_step_number: int

    def __post_init__(self) -> None:
        if not self.action_id.strip():
            raise ValueError("Action history identity must not be empty")
        if (
            self.turn_start_step_number < 1
            or self.turn_end_step_number < self.turn_start_step_number
        ):
            raise ValueError("Action history turn step range is invalid")


@dataclass(frozen=True, slots=True)
class AgentExperienceActionHistoryTools:
    db_path: Path
    busy_timeout_ms: int
    user_id: str
    turns: tuple[ActionTurnWindow, ...]

    def __post_init__(self) -> None:
        if self.busy_timeout_ms <= 0:
            raise ValueError("busy_timeout_ms must be positive")
        if not self.user_id.strip():
            raise ValueError("Action history identity must not be empty")
        if not self.turns:
            raise ValueError("Action history needs at least one completed turn")
        action_ids = tuple(turn.action_id for turn in self.turns)
        if len(set(action_ids)) != len(action_ids):
            raise ValueError("Action history turns must name distinct Actions")

    @property
    def action_ids(self) -> tuple[str, ...]:
        return tuple(turn.action_id for turn in self.turns)

    def _require_turn(self, value: JSONValue) -> ActionTurnWindow:
        args = value if isinstance(value, dict) else {}
        action_id = args.get("action_id")
        for turn in self.turns:
            if turn.action_id == action_id:
                return turn
        raise ValueError(f"action_id is outside this run: {action_id!r}")

    def definitions(self) -> tuple[ReactToolDefinition, ...]:
        return (
            ReactToolDefinition(
                name=LIST_ACTION_STEPS_TOOL_NAME,
                description=(
                    "List latest persisted Action step metadata in execution order. "
                    "Use returned short_step_id values with history_fetch."
                ),
                request_schema=self._page_request_schema(),
                response_schema=react_tool_response_schema(
                    success_schema=step_list_success_schema()
                ),
                execute=self.list_steps,
            ),
            ReactToolDefinition(
                name=SEARCH_ACTION_STEPS_TOOL_NAME,
                description=(
                    "Search latest persisted Action user messages, tool arguments and "
                    "results, responses, and errors. Fetch exact matches by "
                    "short_step_id with history_fetch for attributed conversation. "
                    "Search excerpts are discovery hints, not user-authored statements."
                ),
                request_schema=self._search_request_schema(),
                response_schema=react_tool_response_schema(
                    success_schema=search_success_schema()
                ),
                execute=self.search_steps,
            ),
            ReactToolDefinition(
                name=HISTORY_FETCH_TOOL_NAME,
                description=(
                    "Fetch selected persisted Action history, including attributed conversation, "
                    "without execution prompts or internal reasoning. "
                    "User image and screen-capture attachments expose counts; "
                    "their content and blob paths are unavailable. "
                    "Use displayed short_step_id values; no tool is rerun."
                ),
                request_schema={
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["action_id", "refs"],
                    "properties": {
                        "action_id": self._action_id_schema(),
                        "refs": history_fetch_refs_schema(),
                    },
                },
                response_schema=react_tool_response_schema(
                    success_schema=history_fetch_success_schema()
                ),
                execute=self.fetch_history,
            ),
        )

    async def list_steps(
        self, call: ReactToolCall, _step_number: int
    ) -> ReactToolResult:
        try:
            turn = self._require_turn(call.tool_args)
            offset, limit = parse_page(call.tool_args)
            from_step = parse_from_step(call.tool_args, turn.turn_start_step_number)
            with self._connect() as connection:
                rows = connection.execute(
                    _LIST_LATEST_STEPS_SQL,
                    (
                        self.user_id,
                        turn.action_id,
                        from_step,
                        turn.turn_end_step_number,
                        limit + 1,
                        offset,
                    ),
                ).fetchall()
            has_more = len(rows) > limit
            return _success(
                call.tool_name,
                {
                    "status": "success",
                    "steps": [step_metadata(row) for row in rows[:limit]],
                    "next_offset": offset + limit if has_more else None,
                },
            )
        except ValueError as exc:
            return _invalid(call.tool_name, exc)
        except StoredActionHistoryError as exc:
            return _stored_error(call.tool_name, exc)

    async def search_steps(
        self, call: ReactToolCall, _step_number: int
    ) -> ReactToolResult:
        try:
            turn = self._require_turn(call.tool_args)
            query, offset, limit = parse_search_request(call.tool_args)
            from_step = parse_from_step(call.tool_args, turn.turn_start_step_number)
            with self._connect() as connection:
                rows = connection.execute(
                    _SEARCH_LATEST_STEPS_SQL,
                    (
                        self.user_id,
                        turn.action_id,
                        from_step,
                        turn.turn_end_step_number,
                        query,
                        limit + 1,
                        offset,
                    ),
                ).fetchall()
            has_more = len(rows) > limit
            return _success(
                call.tool_name,
                {
                    "status": "success",
                    "matches": [search_match(row, query=query) for row in rows[:limit]],
                    "next_offset": offset + limit if has_more else None,
                },
            )
        except ValueError as exc:
            return _invalid(call.tool_name, exc)
        except StoredActionHistoryError as exc:
            return _stored_error(call.tool_name, exc)

    async def fetch_history(
        self, call: ReactToolCall, _step_number: int
    ) -> ReactToolResult:
        try:
            turn = self._require_turn(call.tool_args)
            refs = parse_history_refs(call.tool_args)
            placeholders = ", ".join("?" for _ in refs)
            with self._connect() as connection:
                rows = connection.execute(
                    _FETCH_LATEST_STEPS_SQL.format(placeholders=placeholders),
                    (
                        self.user_id,
                        turn.action_id,
                        turn.turn_end_step_number,
                        *refs,
                    ),
                ).fetchall()
            rows_by_ref = {str(row["short_step_id"]): row for row in rows}
            missing_refs = [ref for ref in refs if ref not in rows_by_ref]
            if missing_refs:
                return tool_error_response(
                    tool_name=call.tool_name,
                    error_code="ACTION_HISTORY_NOT_FOUND",
                    message=(
                        "history_fetch: refs not found in this Action: "
                        + ", ".join(missing_refs)
                    ),
                    details={
                        "path": ["refs"],
                        "missing_refs": cast(JSONValue, missing_refs),
                    },
                )
            steps = [history_fetch_step(rows_by_ref[ref], ref=ref) for ref in refs]
            return _success(
                call.tool_name,
                {
                    "status": "success",
                    "steps": cast(JSONValue, steps),
                },
            )
        except ValueError as exc:
            return _invalid(call.tool_name, exc)
        except StoredActionHistoryError as exc:
            return _stored_error(call.tool_name, exc)

    def _action_id_schema(self) -> dict[str, JSONValue]:
        return {"type": "string", "enum": list(self.action_ids)}

    def _page_request_schema(self) -> dict[str, JSONValue]:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["action_id"],
            "properties": {
                "action_id": self._action_id_schema(),
                "from_step": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        "First step to read; defaults to the first new step. "
                        "Lower it, down to 1, to read earlier steps as context."
                    ),
                },
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
        }

    def _search_request_schema(self) -> dict[str, JSONValue]:
        schema = self._page_request_schema()
        schema["required"] = ["action_id", "query"]
        properties = schema["properties"]
        assert isinstance(properties, dict)
        properties["query"] = {"type": "string", "minLength": 1}
        return schema

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        configure_connection(connection, self.busy_timeout_ms)
        connection.row_factory = sqlite3.Row
        connection.create_function(
            "memory_tool_output", 1, memory_tool_output_json, deterministic=True
        )
        return connection


def _success(tool_name: str, output: dict[str, JSONValue]) -> ReactToolResult:
    return ReactToolResult(tool_name=tool_name, status="success", output=output)


def _invalid(tool_name: str, exc: ValueError) -> ReactToolResult:
    return tool_error_response(
        tool_name=tool_name,
        error_code="ACTION_HISTORY_INVALID",
        message=str(exc),
    )


def _stored_error(tool_name: str, exc: StoredActionHistoryError) -> ReactToolResult:
    return tool_error_response(
        tool_name=tool_name,
        error_code="ACTION_HISTORY_CORRUPT",
        message=str(exc),
    )


_LIST_LATEST_STEPS_SQL = """
WITH ranked_steps AS (
    SELECT
        short_step_id,
        step_number,
        local_step_number,
        step_type,
        step_name,
        status,
        goal_handle,
        retry_count,
        created_at,
        completed_at,
        ROW_NUMBER() OVER (
            PARTITION BY short_step_id
            ORDER BY
                completed_at IS NULL ASC,
                completed_at DESC,
                created_at DESC,
                step_id DESC
        ) AS resolution_rank
    FROM agent_action_steps
    WHERE user_id = ?
      AND action_id = ?
      AND step_number BETWEEN ? AND ?
      AND short_step_id IS NOT NULL
)
SELECT
    short_step_id,
    step_number,
    local_step_number,
    step_type,
    step_name,
    status,
    goal_handle,
    retry_count,
    created_at,
    completed_at
FROM ranked_steps
WHERE resolution_rank = 1
ORDER BY step_number, local_step_number, created_at, short_step_id
LIMIT ? OFFSET ?
"""


_SEARCH_LATEST_STEPS_SQL = """
WITH ranked_steps AS (
    SELECT
        short_step_id,
        step_number,
        step_type,
        step_name,
        status,
        user_request_text,
        user_message_id,
        user_message_json,
        source_suggestion_id,
        llm_response_text,
        tool_args,
        memory_tool_output(tool_output) AS tool_output,
        error,
        created_at,
        completed_at,
        ROW_NUMBER() OVER (
            PARTITION BY short_step_id
            ORDER BY
                completed_at IS NULL ASC,
                completed_at DESC,
                created_at DESC,
                step_id DESC
        ) AS resolution_rank
    FROM agent_action_steps
    WHERE user_id = ?
      AND action_id = ?
      AND step_number BETWEEN ? AND ?
      AND short_step_id IS NOT NULL
)
SELECT
    short_step_id,
    step_number,
    step_type,
    step_name,
    status,
    user_request_text,
    user_message_id,
    user_message_json,
    source_suggestion_id,
    llm_response_text,
    tool_args,
    tool_output,
    error,
    created_at
FROM ranked_steps
WHERE resolution_rank = 1
  AND instr(lower(
        COALESCE(step_name, '') || char(10) ||
        COALESCE(user_request_text, '') || char(10) ||
        COALESCE(llm_response_text, '') || char(10) ||
        COALESCE(tool_args, '') || char(10) ||
        COALESCE(tool_output, '') || char(10) ||
        COALESCE(error, '')
      ), lower(?)) > 0
ORDER BY step_number, created_at, short_step_id
LIMIT ? OFFSET ?
"""


_FETCH_LATEST_STEPS_SQL = """
WITH ranked_steps AS (
    SELECT
        short_step_id,
        step_number,
        local_step_number,
        step_name,
        step_type,
        status,
        user_request_text,
        user_message_id,
        user_message_json,
        source_suggestion_id,
        llm_response_text,
        tool_args,
        tool_output,
        error,
        goal_handle,
        started_at,
        completed_at,
        created_at,
        ROW_NUMBER() OVER (
            PARTITION BY short_step_id
            ORDER BY
                completed_at IS NULL ASC,
                completed_at DESC,
                created_at DESC,
                step_id DESC
        ) AS resolution_rank
    FROM agent_action_steps
    WHERE user_id = ?
      AND action_id = ?
      AND step_number <= ?
      AND short_step_id IN ({placeholders})
)
SELECT
    short_step_id,
    step_number,
    local_step_number,
    step_name,
    step_type,
    status,
    user_request_text,
    user_message_id,
    user_message_json,
    source_suggestion_id,
    llm_response_text,
    tool_args,
    tool_output,
    error,
    goal_handle,
    started_at,
    completed_at
FROM ranked_steps
WHERE resolution_rank = 1
"""


__all__ = [
    "HISTORY_FETCH_TOOL_NAME",
    "LIST_ACTION_STEPS_TOOL_NAME",
    "SEARCH_ACTION_STEPS_TOOL_NAME",
    "AgentExperienceActionHistoryTools",
]
