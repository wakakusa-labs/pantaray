"""memory_sql ツール実行。"""

from __future__ import annotations

import asyncio
from collections.abc import MutableMapping
from typing import cast

from pantaray_agents.agents.action_agent.runtime.handlers.tool_args import (
    ToolArgs,
    require_int_arg,
    require_string_arg,
)
from pantaray_agents.agents.action_agent.services.memory_sql import (
    DEFAULT_MEMORY_SQL_LIMIT,
    run_local_memory_sql,
)
from pantaray_agents.agents.action_agent.tools import ToolDefinition
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.schema.agent.base import JSONValue

from .shared import ToolValidationError, UnprojectedToolExecutionResult
from .validation import validate_tool_args


async def run_memory_sql_tool(
    agent: object,
    step_id: str,
    tool_def: ToolDefinition,
    args: ToolArgs,
    state: MutableMapping[str, object],
) -> UnprojectedToolExecutionResult:
    _ = agent
    validate_tool_args(tool_def, dict(args))
    runtime_cfg = tool_def.runtime_config or {}
    raw_default_limit = runtime_cfg.get("default_limit")
    default_limit = (
        raw_default_limit
        if isinstance(raw_default_limit, int)
        else DEFAULT_MEMORY_SQL_LIMIT
    )
    sql = require_string_arg(args, "sql")
    limit = require_int_arg(args, "limit", default_limit)
    user_id = _current_user_id(state)

    repo_result = await asyncio.to_thread(
        run_local_memory_sql,
        user_id=user_id,
        sql=sql,
        limit=limit,
    )
    if repo_result.error:
        raise ToolValidationError(
            "memory_sql rejected the request.",
            details={
                "code": "MEMORY_SQL_REQUEST_REJECTED",
                "reason": str(repo_result.error),
                "details": [],
                "fix_hint": "Use a single read-only SELECT against allowed memory tables.",
                "path": ["args", "sql"],
                "message": str(repo_result.error),
            },
        )
    payload = repo_result.data or {
        "columns": [],
        "rows": [],
        "row_count": 0,
        "truncated": False,
        "notes": [],
    }
    return UnprojectedToolExecutionResult(
        step_id=step_id,
        tool_id=tool_def.tool_id,
        status="success",
        started_at=now_utc_iso(),
        completed_at=now_utc_iso(),
        output=cast(JSONValue, payload),
    )


def _current_user_id(state: MutableMapping[str, object]) -> str:
    user_id = state.get("user_id")
    if isinstance(user_id, str) and user_id.strip():
        return user_id
    raise ToolValidationError(
        "memory_sql requires a current user.",
        details={
            "code": "MEMORY_SQL_CURRENT_USER_REQUIRED",
            "reason": "memory_sql must be scoped to the current action user.",
            "details": [],
            "fix_hint": "Run memory_sql only inside an action state with user_id.",
            "path": ["state", "user_id"],
            "message": "state.user_id is required.",
        },
    )


__all__ = ["run_memory_sql_tool"]
