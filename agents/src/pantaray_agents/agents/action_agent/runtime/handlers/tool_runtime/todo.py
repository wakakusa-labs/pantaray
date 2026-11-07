"""update_todo and request_todo_deletion execution, on the Action's own TODO."""

from __future__ import annotations

import uuid
from typing import cast

from pantaray_agents.agents.action_agent.runtime.handlers.tool_args import (
    ToolArgs,
    require_string_arg,
)
from pantaray_agents.agents.action_agent.runtime.state import ActionAgentState
from pantaray_agents.agents.action_agent.runtime.state.context import ensure_context
from pantaray_agents.agents.action_agent.tools import (
    UPDATE_TODO_TOOL_ID,
    ToolDefinition,
)
from pantaray_agents.agents.action_agent.tools.todo_tools import (
    TODO_REFUSALS,
    TodoStatus,
)
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.local_runtime.todos import core
from pantaray_agents.local_runtime.todos.core import Rejected
from pantaray_agents.local_runtime.todos.model import AiReport, TodoId
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.utils.trace_context import get_trace_context

from .shared import UnprojectedToolExecutionResult


def run_todo_tool(
    *,
    step_id: str,
    tool_def: ToolDefinition,
    args: ToolArgs,
    state: ActionAgentState,
) -> UnprojectedToolExecutionResult:
    todo_id = ensure_context(state).get("served_todo_id")
    if todo_id is None:
        # Only the Supervisor of a TODO's Action is offered these tools.
        raise RuntimeError(f"{tool_def.tool_id} is offered only in a TODO's Action")
    user_id = state["user_id"]
    if tool_def.tool_id == UPDATE_TODO_TOOL_ID:
        output = _update_todo(
            user_id=user_id,
            todo_id=todo_id,
            status=cast("TodoStatus", require_string_arg(args, "status")),
            note=require_string_arg(args, "note"),
        )
    else:
        output = _request_deletion(
            user_id=user_id, todo_id=todo_id, reason=require_string_arg(args, "reason")
        )
    timestamp = now_utc_iso()
    return UnprojectedToolExecutionResult(
        step_id=step_id,
        tool_id=tool_def.tool_id,
        status="success",
        started_at=timestamp,
        completed_at=timestamp,
        output=output,
    )


def _update_todo(
    *, user_id: str, todo_id: TodoId, status: TodoStatus, note: str
) -> dict[str, JSONValue]:
    # The TODO store reads Action conversations through modules that import the
    # Action agent, so importing it when this module loads closes a cycle.
    from pantaray_agents.local_runtime.todos.store import (
        read_todo_clock,
        set_todo_check,
        update_todo,
    )

    clock = read_todo_clock()
    if status == "done":
        checked = set_todo_check(
            user_id=user_id, todo_id=todo_id, by="ai", checked=True, clock=clock
        )
        if checked is None or isinstance(checked, Rejected):
            return _refused(checked)
        return {"status": "checked"}
    report = AiReport(
        kind=status, note=_one_line(note), run_id=_current_run_id(), at=clock.now
    )
    reported = update_todo(
        user_id=user_id,
        todo_id=todo_id,
        step=lambda todo: core.report_progress(todo, report),
    )
    if reported is None or isinstance(reported, Rejected):
        return _refused(reported)
    return {"status": "reported"}


def _request_deletion(
    *, user_id: str, todo_id: TodoId, reason: str
) -> dict[str, JSONValue]:
    # Deferred for the same import cycle as in `_update_todo`.
    from pantaray_agents.local_runtime.todos.store import read_todo_clock, update_todo

    clock = read_todo_clock()
    request_id = str(uuid.uuid4())
    requested = update_todo(
        user_id=user_id,
        todo_id=todo_id,
        step=lambda todo: core.request_deletion(
            todo, request_id=request_id, reason=_one_line(reason), now=clock.now
        ),
    )
    if requested is None or isinstance(requested, Rejected):
        return _refused(requested)
    return {"status": "requested"}


def _refused[R: str](rejection: Rejected[R] | None) -> dict[str, JSONValue]:
    reason = "not_found" if rejection is None else rejection.reason
    return {"status": "refused", "reason": reason, "message": TODO_REFUSALS[reason]}


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _current_run_id() -> str:
    """The process id of the run making the call, which the TODO row compares."""

    trace = get_trace_context()
    process_id = trace.extra.get("process_id") if trace is not None else None
    if not isinstance(process_id, str) or not process_id:
        raise RuntimeError("update_todo requires the active Action job trace")
    return process_id


__all__ = ["run_todo_tool"]
