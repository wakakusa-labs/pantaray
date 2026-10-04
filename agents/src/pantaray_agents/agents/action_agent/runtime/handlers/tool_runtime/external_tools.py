"""web/plan/history-fetch 系ツール実行。"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, cast

from pantaray_agents.agents.action_agent.runtime.handlers.tool_args import (
    ToolArgs,
    optional_string_arg,
    require_string_list_arg,
)
from pantaray_agents.agents.action_agent.runtime.handlers.web_crawl_runtime import (
    run_web_crawl_tool,
)
from pantaray_agents.agents.action_agent.runtime.handlers.web_extract_runtime import (
    run_web_extract_tool,
)
from pantaray_agents.agents.action_agent.runtime.handlers.web_search_runtime import (
    run_web_search_tool,
)
from pantaray_agents.agents.action_agent.runtime.state import ActionAgentState
from pantaray_agents.agents.action_agent.tools import ToolDefinition
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.local_runtime.tooling.tool_result_storage import (
    ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT,
)
from pantaray_agents.schema.agent.action_history import HistoryFetchStep
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.schema.repositories.repository import DBRow
from pantaray_agents.schema.tool_result import serialize_json_tool_output
from pantaray_agents.utils.local_time import describe_utc_timestamp

from .shared import (
    HistoryFetchPayload,
    ToolValidationError,
    UnprojectedToolExecutionResult,
)

if TYPE_CHECKING:
    from pantaray_agents.agents.action_agent import ActionAgent


async def run_web_search_wrapper(
    agent,
    step_id: str,
    tool_def: ToolDefinition,
    args: ToolArgs,
    state,
) -> UnprojectedToolExecutionResult:
    outcome = await run_web_search_tool(
        agent=agent,
        step_id=step_id,
        tool_def=tool_def,
        args=dict(args),
        state=state,
    )
    return UnprojectedToolExecutionResult(
        step_id=step_id,
        tool_id=tool_def.tool_id,
        status=outcome.status,
        started_at=now_utc_iso(),
        completed_at=now_utc_iso(),
        output=cast(JSONValue, outcome.payload),
        prompt_tokens=outcome.prompt_tokens,
        completion_tokens=outcome.completion_tokens,
    )


async def run_web_extract_wrapper(
    agent,
    step_id: str,
    tool_def: ToolDefinition,
    args: ToolArgs,
    state,
) -> UnprojectedToolExecutionResult:
    outcome = await run_web_extract_tool(
        agent=agent,
        step_id=step_id,
        tool_def=tool_def,
        args=dict(args),
        state=state,
    )
    return UnprojectedToolExecutionResult(
        step_id=step_id,
        tool_id=tool_def.tool_id,
        status=outcome.status,
        started_at=now_utc_iso(),
        completed_at=now_utc_iso(),
        output=cast(JSONValue, outcome.payload),
        prompt_tokens=outcome.prompt_tokens,
        completion_tokens=outcome.completion_tokens,
    )


async def run_web_crawl_wrapper(
    agent,
    step_id: str,
    tool_def: ToolDefinition,
    args: ToolArgs,
    state,
) -> UnprojectedToolExecutionResult:
    outcome = await run_web_crawl_tool(
        agent=agent,
        step_id=step_id,
        tool_def=tool_def,
        args=dict(args),
        state=state,
    )
    return UnprojectedToolExecutionResult(
        step_id=step_id,
        tool_id=tool_def.tool_id,
        status=outcome.status,
        started_at=now_utc_iso(),
        completed_at=now_utc_iso(),
        output=cast(JSONValue, outcome.payload),
        prompt_tokens=outcome.prompt_tokens,
        completion_tokens=outcome.completion_tokens,
    )


async def run_history_fetch_wrapper(
    agent: ActionAgent,
    step_id: str,
    tool_def: ToolDefinition,
    args: ToolArgs,
    state: ActionAgentState,
) -> UnprojectedToolExecutionResult:
    refs = tuple(require_string_list_arg(args, "refs"))

    action_id = state["action_id"]
    user_id = state["user_id"]

    result = await agent.repository.get_action_steps_by_short_step_ids(
        user_id=user_id,
        action_id=action_id,
        short_step_ids=refs,
    )
    if result.error:
        raise RuntimeError(f"history_fetch failed: {result.error}")

    rows_by_ref = {
        str(row.get("short_step_id") or ""): row for row in result.data or []
    }
    missing_refs = [ref for ref in refs if ref not in rows_by_ref]
    if missing_refs:
        missing_refs_text = ", ".join(missing_refs)
        raise ToolValidationError(
            f"history_fetch: refs not found in this Action: {missing_refs_text}",
            details={
                "path": ["refs"],
                "message": "Use only short step IDs shown in this Action history.",
                "metadata": {"missing_refs": list(missing_refs)},
            },
        )

    content = serialize_json_tool_output(
        {
            "steps": [
                cast(JSONValue, _history_fetch_step(rows_by_ref[ref], ref=ref))
                for ref in refs
            ]
        }
    )
    payload = _history_fetch_page(
        content=content,
        refs=refs,
        user_id=user_id,
        action_id=action_id,
        cursor=optional_string_arg(args, "cursor"),
    )
    return UnprojectedToolExecutionResult(
        step_id=step_id,
        tool_id=tool_def.tool_id,
        status="success",
        started_at=now_utc_iso(),
        completed_at=now_utc_iso(),
        output=cast(JSONValue, payload),
    )


def _history_fetch_page(
    *,
    content: str,
    refs: tuple[str, ...],
    user_id: str,
    action_id: str,
    cursor: str | None,
) -> HistoryFetchPayload:
    # A repair can replace the row behind a History Ref between page requests.
    # Bind the cursor to both the authorized Action and the exact selected content.
    digest = hashlib.sha256(
        serialize_json_tool_output([user_id, action_id, content]).encode("utf-8")
    ).hexdigest()
    offset = 0
    if cursor is not None:
        source_digest, raw_offset = cursor.split(":")
        offset = int(raw_offset)
        if source_digest != digest or offset >= len(content):
            raise ToolValidationError(
                "history_fetch: cursor does not match the selected history content",
                details={
                    "path": ["cursor"],
                    "message": "Restart with the same refs and no cursor; the selected history changed or the cursor is out of range.",
                },
            )

    def page(length: int) -> HistoryFetchPayload:
        end = offset + length
        return {
            "refs": list(refs),
            "content": content[offset:end],
            "offset": offset,
            "total_characters": len(content),
            "next_cursor": f"{digest}:{end}" if end < len(content) else None,
        }

    low, high = 0, min(len(content) - offset, ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT)
    while low < high:
        midpoint = (low + high + 1) // 2
        if (
            len(serialize_json_tool_output(cast(JSONValue, page(midpoint))))
            <= ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT
        ):
            low = midpoint
        else:
            high = midpoint - 1
    if low == 0:
        raise ToolValidationError("history_fetch: refs exceed the inline page budget")
    return page(low)


def _history_fetch_step(row: DBRow, *, ref: str) -> HistoryFetchStep:
    step_number = row.get("step_number")
    local_step_number = row.get("local_step_number")
    if not isinstance(step_number, int) or isinstance(step_number, bool):
        raise RuntimeError(f"history_fetch: invalid step_number for ref {ref!r}")
    if not isinstance(local_step_number, int) or isinstance(local_step_number, bool):
        raise RuntimeError(f"history_fetch: invalid local_step_number for ref {ref!r}")
    return {
        "short_step_id": ref,
        "step_number": step_number,
        "local_step_number": local_step_number,
        "step_name": _required_text(row, "step_name", ref=ref),
        "step_type": _required_text(row, "step_type", ref=ref),
        "status": _required_text(row, "status", ref=ref),
        "user_request_text": _optional_text(row.get("user_request_text")),
        "thinking": _optional_text(row.get("thinking")),
        "llm_prompt_text": _optional_text(row.get("llm_prompt_text")),
        "llm_response_text": _optional_text(row.get("llm_response_text")),
        "tool_args": _optional_object(row.get("tool_args")),
        "tool_output": _optional_object(row.get("tool_output")),
        "error": _optional_object(row.get("error")),
        "goal_handle": _optional_text(row.get("goal_handle")),
        "started_at": _optional_local_time(row.get("started_at")),
        "completed_at": _optional_local_time(row.get("completed_at")),
    }


def _required_text(row: DBRow, key: str, *, ref: str) -> str:
    value = _optional_text(row.get(key))
    if value is None:
        raise RuntimeError(f"history_fetch: missing {key} for ref {ref!r}")
    return value


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _optional_local_time(value: object) -> str | None:
    text = _optional_text(value)
    return describe_utc_timestamp(text) if text is not None else None


def _optional_object(value: object) -> dict[str, JSONValue] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise RuntimeError("history_fetch: persisted JSON field must be an object")
    return {str(key): item for key, item in value.items()}
