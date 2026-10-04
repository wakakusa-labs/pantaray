"""Raw computer-activity reads for the user-initiated Action conversation.

The reads themselves belong to ``insight_agent.zanei_tools.ZaneiTools``: this
adapter only resolves the signed-in user's live permit, keeps one reader session
per Action run in memory, and projects the shared result into an Action tool
result. The session is never written to Action state, so it does not enter the
runtime checkpoint and a resumed run starts a fresh read.
"""

from __future__ import annotations

import asyncio
from typing import cast

from pantaray_agents.agents.action_agent.runtime.handlers.tool_args import ToolArgs
from pantaray_agents.agents.action_agent.runtime.state import ActionAgentState
from pantaray_agents.agents.action_agent.tools import ToolDefinition
from pantaray_agents.agents.action_agent.tools.zanei_tools import (
    RECORDING_UNAVAILABLE_STATUS,
)
from pantaray_agents.agents.artifact_react import (
    ReactToolCall,
    ReactToolResult,
    ToolCallEnvelope,
)
from pantaray_agents.agents.insight_agent.zanei_tools import (
    EVENT_TOOL,
    PAGE_TOOL,
    ZaneiTools,
)
from pantaray_agents.local_runtime.context import store
from pantaray_agents.local_runtime.context.source_control import context_source_control
from pantaray_agents.local_runtime.context.source_gate import SourceInvalidated
from pantaray_agents.local_runtime.context.source_reader import SourceReader
from pantaray_agents.local_runtime.memory_catalog.connection import (
    open_memory_catalog_connection,
)
from pantaray_agents.local_runtime.runtime.bootstrap import read_local_runtime_db_config
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.schema.context_source import SourceBinding
from pantaray_agents.schema.tool_result import (
    build_runtime_tool_error_output,
    serialize_json_tool_output,
)

from .shared import UnprojectedToolExecutionResult

RECORDING_UNAVAILABLE_MESSAGE = (
    "Computer activity recording is not available, so recent activity cannot be "
    "read. Ask the user for the page, file or text instead of guessing."
)
ZANEI_READ_FAILED_ERROR_TYPE = "ZaneiReadError"
# A timeline page is sized for the short Insight run's whole transcript, not for
# one Action tool result: 128 rows serialize to ~41,600 characters of real
# recorder metadata and 85,300 at the projection caps. A result above the durable
# store's ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT (20,000) is replaced by an
# `action_file` reference before the next THINK, which would leave the model a
# path instead of events and no event_id to pass to zanei_query. So an oversized
# page is cut to the newest events that fit, with the dropped count reported:
# the Action asks what the user was just doing, and the newest end of the page
# is that answer. The margin under the limit covers the transcript wrapper the
# result is rendered into. The dropped events are not re-read: the session's
# cursor has already moved past them.
ACTION_TIMELINE_RESULT_CHARACTER_BUDGET = 16_000


async def run_zanei_timeline_tool(
    *,
    step_id: str,
    tool_def: ToolDefinition,
    args: ToolArgs,
    state: ActionAgentState,
    runtime,
) -> UnprojectedToolExecutionResult:
    return await _run_zanei_tool(
        step_id=step_id,
        tool_def=tool_def,
        args=args,
        state=state,
        runtime=runtime,
        shared_tool_name=PAGE_TOOL,
    )


async def run_zanei_query_tool(
    *,
    step_id: str,
    tool_def: ToolDefinition,
    args: ToolArgs,
    state: ActionAgentState,
    runtime,
) -> UnprojectedToolExecutionResult:
    return await _run_zanei_tool(
        step_id=step_id,
        tool_def=tool_def,
        args=args,
        state=state,
        runtime=runtime,
        shared_tool_name=EVENT_TOOL,
    )


async def _run_zanei_tool(
    *,
    step_id: str,
    tool_def: ToolDefinition,
    args: ToolArgs,
    state: ActionAgentState,
    runtime,
    shared_tool_name: str,
) -> UnprojectedToolExecutionResult:
    # run_tool validated these args against tool_def before dispatching here.
    user_id = state["user_id"]
    gate = context_source_control.gate
    session = runtime.zanei_session
    if session is None:
        session = await _open_session(user_id)
        if session is None:
            return _unavailable(step_id, tool_def)
        runtime.zanei_session = session
    if shared_tool_name == PAGE_TOOL:
        _reopen_drained_range(session)
    tool_args: dict[str, JSONValue] = dict(args)
    call = ReactToolCall(
        tool_name=shared_tool_name,
        tool_args=tool_args,
        tool_call_envelope=ToolCallEnvelope(
            tool_id=shared_tool_name, reason=None, args=tool_args
        ),
    )
    execute = session.timeline if shared_tool_name == PAGE_TOOL else session.query
    try:
        result = await execute(call, 0)
    except asyncio.CancelledError:
        # `SourceGate.revoke` cancels the in-flight read when recording stops or
        # the source is replaced. It drops the permit before scheduling the
        # cancel, so a permit that no longer matches proves the cancel came from
        # invalidation and must become a result the model can act on, not a
        # cancelled conversation.
        if gate.current(user_id) == session.source:
            raise
        task = asyncio.current_task()
        assert task is not None  # A coroutine driven by asyncio.run runs in a Task.
        task.uncancel()
        runtime.zanei_session = None
        return _unavailable(step_id, tool_def)
    except SourceInvalidated:
        # The permit was revoked or replaced between two tool calls, so the
        # cached session holds a stale source and the reader rejects the read
        # before it starts. Drop the session so the next call re-opens one and
        # picks up a fresh permit if recording has been re-enabled.
        runtime.zanei_session = None
        return _unavailable(step_id, tool_def)
    except RuntimeError as exc:
        # ZaneiTools raises this when the local reader answers with a transport
        # or protocol failure. Report it as a bounded tool error instead of
        # failing the whole Action run.
        return UnprojectedToolExecutionResult(
            step_id=step_id,
            tool_id=tool_def.tool_id,
            status="error",
            started_at=now_utc_iso(),
            completed_at=now_utc_iso(),
            output=build_runtime_tool_error_output(
                error_type=ZANEI_READ_FAILED_ERROR_TYPE,
                message=str(exc),
            ),
        )
    return _projected(step_id, tool_def, result)


def _reopen_drained_range(session: ZaneiTools) -> None:
    """Let a later timeline call see activity recorded after the range ran out.

    ``ZaneiTools`` stops at ``has_more=False`` because the short Insight run
    completes there. An Action run outlives that point: the user keeps working,
    and can send a follow-up about something they did after the last page, while
    the run is still going. A timeline call on a drained session therefore
    re-opens the range from the session's own cursor and drops the snapshot's
    upper bound, so the host answers with whatever it has appended since. The
    stored stream cursor is not touched; only this in-memory session advances.

    The run's page budget still applies, so a model that keeps polling an idle
    recorder stops at ``page_budget_spent`` like any other reader.
    """
    if session.has_more or session.page_budget_spent:
        return
    session.has_more = True
    session.upper_bound = None


async def _open_session(user_id: str) -> ZaneiTools | None:
    gate = context_source_control.gate
    async with gate.turn():
        source = gate.current(user_id)
    if source is None:
        return None
    return ZaneiTools(
        reader=SourceReader(gate),
        source=source,
        # The stream cursor is the short Insight's committed read position and
        # stays owned by that run: this session only starts there and never
        # advances it, so "recent" means everything not summarized yet.
        cursor=load_stream_cursor(source.binding),
        upper_bound=None,
    )


def load_stream_cursor(binding: SourceBinding) -> str | None:
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    with open_memory_catalog_connection(
        db_path=db_path, busy_timeout_ms=busy_timeout_ms
    ) as connection:
        return store.get_cursor(connection, binding)


def _projected(
    step_id: str,
    tool_def: ToolDefinition,
    result: ReactToolResult,
) -> UnprojectedToolExecutionResult:
    success = result.status == "success"
    return UnprojectedToolExecutionResult(
        step_id=step_id,
        tool_id=tool_def.tool_id,
        status="success" if success else "error",
        started_at=now_utc_iso(),
        completed_at=now_utc_iso(),
        output=_fit_inline(result.output) if success else _error_output(result),
    )


def _error_output(result: ReactToolResult) -> dict[str, JSONValue]:
    """Rebuild a rejected read as the Action's own error envelope.

    ``ZaneiTools`` reports a rejected read the React way, as a flat
    ``{status, error_code, message}`` -- an event id from before a resume, or a
    field the reader could not decode. ``build_tool_execution_control`` reads
    only a nested ``error`` object, so the flat shape reaches the formal step
    and the user as ``UnknownToolError``. Convert it so the step and the error
    the user sees keep the Zanei code and message.
    """
    output = result.output
    error_code = output.get("error_code") if isinstance(output, dict) else None
    message = result.error_message
    assert message is not None  # ReactToolResult requires it on an error result.
    return build_runtime_tool_error_output(
        error_type=error_code
        if isinstance(error_code, str)
        else ZANEI_READ_FAILED_ERROR_TYPE,
        message=message,
    )


def _fit_inline(output: JSONValue) -> JSONValue:
    """Cut a timeline page to the newest events that stay inline."""
    if not isinstance(output, dict):
        return output
    if "contexts" in output:
        # The shared reader compacts repeated metadata for Insight's long
        # transcript; Action's existing tool schema exposes complete event rows.
        contexts = cast(list[dict[str, JSONValue]], output["contexts"])
        rows = cast(list[list[JSONValue]], output["events"])
        output = {
            key: value
            for key, value in output.items()
            if key not in {"contexts", "event_columns", "events"}
        }
        output["events"] = [
            {
                **contexts[cast(int, row[2])],
                "event_id": row[0],
                "observed_at": row[1],
            }
            for row in rows
        ]
    events = output.get("events")
    if not isinstance(events, list):
        return output
    if _serialized_length(output) <= ACTION_TIMELINE_RESULT_CHARACTER_BUDGET:
        return output
    kept, dropped = 0, len(events)
    while kept < dropped:
        candidate = (kept + dropped + 1) // 2
        if (
            _serialized_length(_newest_events(output, events, candidate))
            <= ACTION_TIMELINE_RESULT_CHARACTER_BUDGET
        ):
            kept = candidate
        else:
            dropped = candidate - 1
    return _newest_events(output, events, kept)


def _newest_events(
    output: dict[str, JSONValue], events: list[JSONValue], kept: int
) -> dict[str, JSONValue]:
    trimmed = dict(output)
    trimmed["events"] = events[len(events) - kept :]
    omitted = len(events) - kept
    if omitted:
        trimmed["older_events_omitted"] = omitted
    return trimmed


def _serialized_length(output: JSONValue) -> int:
    return len(serialize_json_tool_output(output))


def _unavailable(
    step_id: str,
    tool_def: ToolDefinition,
) -> UnprojectedToolExecutionResult:
    output: dict[str, JSONValue] = {
        "status": RECORDING_UNAVAILABLE_STATUS,
        "message": RECORDING_UNAVAILABLE_MESSAGE,
    }
    return UnprojectedToolExecutionResult(
        step_id=step_id,
        tool_id=tool_def.tool_id,
        status="success",
        started_at=now_utc_iso(),
        completed_at=now_utc_iso(),
        output=output,
    )


__all__ = [
    "RECORDING_UNAVAILABLE_MESSAGE",
    "load_stream_cursor",
    "run_zanei_query_tool",
    "run_zanei_timeline_tool",
]
