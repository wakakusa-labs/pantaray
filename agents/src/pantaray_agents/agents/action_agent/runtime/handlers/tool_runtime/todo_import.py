"""write_todo_draft execution, on the Action's own TODO import."""

from __future__ import annotations

from pydantic import ValidationError

from pantaray_agents.agents.action_agent.runtime.handlers.tool_args import (
    ToolArgs,
    require_int_arg,
)
from pantaray_agents.agents.action_agent.runtime.state import ActionAgentState
from pantaray_agents.agents.action_agent.runtime.state.context import ensure_context
from pantaray_agents.agents.action_agent.tools import ToolDefinition
from pantaray_agents.agents.action_agent.tools.todo_import_tool import (
    TODO_DRAFT_REFUSALS,
    TodoDraftRefusal,
)
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.local_runtime.todos.clock import read_todo_clock
from pantaray_agents.local_runtime.todos.imports.model import (
    DraftIssue,
    ImportClosed,
    NewOrganization,
    NewProject,
    ResolvedGroup,
    StaleRevision,
    TodoImportDraft,
    TodoImportView,
)
from pantaray_agents.local_runtime.todos.imports.store import (
    write_todo_import_draft,
)
from pantaray_agents.schema.agent.base import JSONValue

from .shared import UnprojectedToolExecutionResult


def run_todo_import_tool(
    *,
    step_id: str,
    tool_def: ToolDefinition,
    args: ToolArgs,
    state: ActionAgentState,
) -> UnprojectedToolExecutionResult:
    import_id = ensure_context(state).get("served_todo_import_id")
    if import_id is None:
        # Only the Supervisor of an import's Action is offered this tool.
        raise RuntimeError(f"{tool_def.tool_id} is offered only in an import's Action")
    output = _write(
        user_id=state["user_id"],
        import_id=import_id,
        base_revision=require_int_arg(args, "base_revision", 0),
        draft=args.get("draft"),
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


def _write(
    *, user_id: str, import_id: str, base_revision: int, draft: JSONValue
) -> dict[str, JSONValue]:
    # The schema cannot say a date is a real one or a name is not blank after
    # trimming; the draft's own model does.
    try:
        parsed = TodoImportDraft.model_validate(draft)
    except ValidationError as exc:
        first = exc.errors(include_url=False, include_input=False)[0]
        detail = ".".join(str(part) for part in first["loc"]) + ": " + first["msg"]
        return _refused("invalid_draft", detail=detail)
    written = write_todo_import_draft(
        user_id=user_id,
        import_id=import_id,
        draft=parsed,
        expected_revision=base_revision,
        clock=read_todo_clock(),
    )
    match written:
        case TodoImportView():
            return {"status": "written", **_view(written)}
        case StaleRevision(current=current):
            return {**_refused("stale_revision"), "current": _view(current)}
        case ImportClosed(state=closed):
            return _refused(closed)
        case DraftIssue(reason=reason, group_index=group, todo_index=todo):
            output = _refused(reason)
            if group is not None:
                output["group_index"] = group
            if todo is not None:
                output["todo_index"] = todo
            return output
        case None:
            # The import's Action is found through its draft row, and no path
            # deletes that row but the user's own deletion.
            raise RuntimeError("the import this Action drafts no longer exists")


def _view(view: TodoImportView) -> dict[str, JSONValue]:
    return {
        "revision": view.revision,
        "groups": [_group(group) for group in view.groups],
    }


def _group(group: ResolvedGroup) -> JSONValue:
    return {
        "organization": None
        if group.organization is None
        else {
            "name": group.organization.name,
            "new": isinstance(group.organization, NewOrganization),
        },
        "project": None
        if group.project is None
        else {"name": group.project.name, "new": isinstance(group.project, NewProject)},
        "todos": [
            {
                "text": line.todo.text,
                "due": None if line.todo.due is None else line.todo.due.isoformat(),
                "starts_on": None
                if line.todo.starts_on is None
                else line.todo.starts_on.isoformat(),
                "duplicate": line.duplicate,
                "keep_duplicate": line.todo.keep_duplicate,
            }
            for line in group.todos
        ],
    }


def _refused(
    reason: TodoDraftRefusal, *, detail: str | None = None
) -> dict[str, JSONValue]:
    message = TODO_DRAFT_REFUSALS[reason]
    return {
        "status": "refused",
        "reason": reason,
        "message": message if detail is None else f"{message} {detail}",
    }


__all__ = ["run_todo_import_tool"]
