"""Bounded logical-run and page projection for Action conversations."""

from __future__ import annotations

import sqlite3
from typing import cast

from pydantic import ValidationError

from pantaray_agents.local_runtime.runtime.action_logical_run_authority import (
    ActionLogicalRunAuthority,
    ActionLogicalRunSelection,
    select_action_logical_run_authorities_in_connection,
)
from pantaray_agents.local_runtime.runtime.action_queued_message_adoption import (
    next_run_takes_messages_after_in_connection,
)
from pantaray_agents.schema.action_conversation import (
    ActionConversationPage,
    ActionConversationSummary,
    ActionRun,
    AssistantEntry,
    RunStatus,
)

from .entry_projection import (
    action_user_row_is_visible,
    project_action_tool_entry,
    project_action_user_entry,
)
from .history_queries import (
    ActionHistoryAssistantRow,
    ActionHistoryUserRow,
    TimelineHistoryRow,
)
from .repository import (
    ActionConversationIntegrityError,
    read_action_conversation_history_page_in_connection,
)
from .terminal_projection import project_action_run_terminal

_ACTIVE_STATUS_BY_AUTHORITY: dict[tuple[str, str], RunStatus] = {
    ("enqueued", "queued"): "running",
    ("running", "running"): "running",
    ("paused", "paused"): "approval_pending",
}


def read_action_conversation_page_in_connection(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    action_id: str,
    cursor: str | None,
    limit: int,
) -> ActionConversationPage:
    """Read and project one bounded page inside the caller's SQLite snapshot."""

    history = read_action_conversation_history_page_in_connection(
        connection=connection,
        user_id=user_id,
        action_id=action_id,
        cursor=cursor,
        limit=limit,
    )
    selections = (
        frozenset()
        if history.scope == "unadopted"
        else frozenset(
            ActionLogicalRunSelection(history.action.action_id, _row_run_id(row))
            for row in history.rows
        )
    )
    authorities = select_action_logical_run_authorities_in_connection(
        connection=connection,
        user_id=user_id,
        selections=selections,
    )
    returned = frozenset(
        ActionLogicalRunSelection(item.action_id, item.root_process_id)
        for item in authorities
    )
    if returned != selections or len(returned) != len(authorities):
        raise ActionConversationIntegrityError(
            "Action logical-run authorities do not match the bounded page"
        )
    authority_by_run = {item.root_process_id: item for item in authorities}

    try:
        if history.scope == "unadopted":
            runs: tuple[ActionRun, ...] = ()
            user_rows = cast(tuple[ActionHistoryUserRow, ...], history.rows)
            takes_after = next_run_takes_messages_after_in_connection(
                connection=connection, user_id=user_id, action_id=action_id
            )
            unadopted = tuple(
                project_action_user_entry(row, next_run_takes_after=takes_after)
                for row in user_rows
                if action_user_row_is_visible(row)
            )
        else:
            runs = tuple(
                _project_run(
                    user_id=user_id,
                    action=history.action,
                    run_id=run_id,
                    rows=rows,
                    authority=authority_by_run[run_id],
                )
                for run_id, rows in _group_run_rows(history.rows)
            )
            unadopted = ()
        return ActionConversationPage(
            action=history.action,
            runs=runs,
            unadopted_messages=unadopted,
            next_cursor=history.next_cursor,
        )
    except ValidationError as exc:
        raise ActionConversationIntegrityError(
            "Action conversation rows cannot form a public page"
        ) from exc


def _group_run_rows(
    rows: tuple[TimelineHistoryRow, ...],
) -> tuple[tuple[str, tuple[TimelineHistoryRow, ...]], ...]:
    groups: dict[str, list[TimelineHistoryRow]] = {}
    previous: str | None = None
    for row in rows:
        run_id = _row_run_id(row)
        if run_id != previous and run_id in groups:
            raise ActionConversationIntegrityError(
                "Action logical runs are not contiguous in the bounded page"
            )
        groups.setdefault(run_id, []).append(row)
        previous = run_id
    return tuple((run_id, tuple(group)) for run_id, group in groups.items())


def _row_run_id(row: TimelineHistoryRow) -> str:
    run_id = (
        row.adopted_process_id
        if isinstance(row, ActionHistoryUserRow)
        else row.owning_run_id
    )
    if not isinstance(run_id, str) or not run_id.strip():
        raise ActionConversationIntegrityError(
            "Action conversation row has no logical-run identity"
        )
    return run_id


def _project_run(
    *,
    user_id: str,
    action: ActionConversationSummary,
    run_id: str,
    rows: tuple[TimelineHistoryRow, ...],
    authority: ActionLogicalRunAuthority,
) -> ActionRun:
    entries = tuple(
        project_action_user_entry(row)
        if isinstance(row, ActionHistoryUserRow)
        else AssistantEntry(
            step_kind="assistant",
            step_id=row.step_id,
            step_number=row.step_number,
            content=row.content,
        )
        if isinstance(row, ActionHistoryAssistantRow)
        else project_action_tool_entry(row)
        for row in rows
        if not isinstance(row, ActionHistoryUserRow) or action_user_row_is_visible(row)
    )
    status = _ACTIVE_STATUS_BY_AUTHORITY.get(
        (authority.process_status, authority.job_status)
    )
    if status is None:
        terminal = project_action_run_terminal(
            user_id=user_id,
            action=action,
            authority=authority,
        )
        status = terminal.status
        completed_at = terminal.completed_at
        final_output = terminal.final_output
        error = terminal.error
    else:
        completed_at = None
        final_output = None
        error = None
    return ActionRun(
        run_id=run_id,
        status=status,
        started_at=authority.root_started_at,
        completed_at=completed_at,
        completion_event_id=authority.terminal_event_id,
        entries=entries,
        final_output=final_output,
        error=error,
    )


__all__ = ["read_action_conversation_page_in_connection"]
