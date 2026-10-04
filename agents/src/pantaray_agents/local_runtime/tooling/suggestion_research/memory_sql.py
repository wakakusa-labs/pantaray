from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from pantaray_agents.agents.action_agent.services.memory_sql import (
    DEFAULT_MEMORY_SQL_LIMIT,
    execute_memory_sql,
)
from pantaray_agents.agents.action_agent.tools.memory_sql_tool import MEMORY_SQL_TOOL
from pantaray_agents.agents.artifact_react import (
    ReactToolCall,
    ReactToolDefinition,
    ReactToolResult,
)
from pantaray_agents.agents.artifact_react.tooling import (
    react_tool_response_schema,
    tool_error_response,
)
from pantaray_agents.schema.agent.base import JSONValue

# The Action guide's "when" part points at memory_search's time_hint, which the
# Suggestion run's memory_search does not take, so only the other parts are shown.
_DESCRIPTION = "\n\n".join(
    (
        MEMORY_SQL_TOOL.description,
        MEMORY_SQL_TOOL.guide.what,
        MEMORY_SQL_TOOL.guide.pitfalls,
    )
)


@dataclass(frozen=True, slots=True)
class SuggestionMemorySqlSession:
    """The Action's memory_sql, bound to the Suggestion run's database and user."""

    db_path: Path
    busy_timeout_ms: int
    user_id: str

    def definition(self) -> ReactToolDefinition:
        return ReactToolDefinition(
            name=MEMORY_SQL_TOOL.tool_id,
            description=_DESCRIPTION,
            request_schema=MEMORY_SQL_TOOL.build_validation_input_schema(),
            response_schema=react_tool_response_schema(
                success_schema=dict(MEMORY_SQL_TOOL.output_schema)
            ),
            execute=self._execute,
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


__all__ = ["SuggestionMemorySqlSession"]
