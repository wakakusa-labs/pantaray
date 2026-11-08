"""Start the work of every TODO that is ready for it, through `submit_action_message`.

Nothing is written on the TODO: the Action's first message id derives from the
TODO id, so `UNIQUE (user_id, initial_user_message_id)` keeps one Action per
TODO however often, or however concurrently, a pass runs. A submit that fails
leaves the TODO without an Action, so the next pass tries it again.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import assert_never

from pydantic import ValidationError

from pantaray_agents.local_runtime.runtime.action_message_models import (
    ACTION_TODO_PICKUP_REQUEST_TEXT,
    ActionMessageSubmissionError,
    NewActionTarget,
    SubmitActionMessageCommand,
)
from pantaray_agents.local_runtime.runtime.action_messages import (
    submit_action_message,
)
from pantaray_agents.local_runtime.runtime.runtime_env import (
    read_local_runtime_db_config,
)
from pantaray_agents.local_runtime.todos.clock import (
    read_todo_clock,
)
from pantaray_agents.local_runtime.todos.conversation import todo_message_id
from pantaray_agents.local_runtime.todos.db import todo_db_connection
from pantaray_agents.local_runtime.todos.display import (
    ContinueAiTodo,
    PickupTarget,
    StartWritten,
    pickup_target,
)
from pantaray_agents.local_runtime.todos.model import SuggestionOrigin, Todo
from pantaray_agents.local_runtime.todos.reads import (
    read_pickup_candidates,
)
from pantaray_agents.local_runtime.tooling.repository.workspace_settings import (
    list_workspace_settings,
)
from pantaray_agents.local_runtime.tooling.repository.workspace_settings_models import (
    WorkspaceSettings,
)
from pantaray_agents.schema.agent.action_message import (
    ActionUserMessageInput,
    TodoHandoffInput,
    TodoHandoffProject,
    TodoHandoffSuggestion,
)
from pantaray_agents.schema.agent.suggestion import SuggestionTargetContext

logger = logging.getLogger(__name__)


def pick_up_todos(*, user_id: str) -> None:
    """Start an Action for each TODO `pickup_target` names."""

    current_week_start = read_todo_clock().current_week_start
    targets = [
        (entry.todo, target)
        for entry in read_pickup_candidates(
            user_id=user_id, current_week_start=current_week_start
        )
        if (target := pickup_target(entry.todo, entry.conversation, current_week_start))
        is not None
    ]
    if not targets:
        return
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    settings = list_workspace_settings(
        db_path=db_path, busy_timeout_ms=busy_timeout_ms, user_id=user_id
    )
    for todo, target in targets:
        try:
            handoff = _handoff(todo, settings, _suggestion(user_id, todo))
            command = _command(user_id, todo, target, handoff)
            submit_action_message(command)
        # A TODO the Action message cannot carry fails alone, so it never holds
        # back the TODOs after it.
        except (ValidationError, ActionMessageSubmissionError, sqlite3.Error) as exc:
            logger.warning(
                "TODO pickup failed: todo_id=%s error=%s",
                todo.todo_id,
                type(exc).__name__,
            )
            continue
        logger.info("TODO picked up: todo_id=%s target=%s", todo.todo_id, target.kind)


def _command(
    user_id: str, todo: Todo, target: PickupTarget, handoff: TodoHandoffInput
) -> SubmitActionMessageCommand:
    message_id = todo_message_id(todo.todo_id)
    match target:
        case StartWritten():
            # The user wrote this TODO, so its text is their message.
            return SubmitActionMessageCommand(
                user_id=user_id,
                origin="user",
                target=NewActionTarget(),
                message=ActionUserMessageInput(
                    message_id=message_id, content=todo.text, todo_handoff=handoff
                ),
            )
        case ContinueAiTodo():
            # A Suggestion's TODO opens with the Suggestion's message, linked to
            # it, so the Suggestion keeps one conversation.
            origin = todo.origin
            return SubmitActionMessageCommand(
                user_id=user_id,
                origin="todo_pickup",
                target=NewActionTarget(
                    approval_mode="prompt_each_time",
                    reply_to_suggestion_id=origin.suggestion_id,
                )
                if isinstance(origin, SuggestionOrigin)
                else NewActionTarget(
                    approval_mode="prompt_each_time", todo_text=todo.text
                ),
                message=ActionUserMessageInput(
                    message_id=message_id,
                    content=ACTION_TODO_PICKUP_REQUEST_TEXT,
                    todo_handoff=handoff,
                ),
            )
        case _:
            assert_never(target)


def _suggestion(user_id: str, todo: Todo) -> TodoHandoffSuggestion | None:
    """What the Suggestion a TODO came from left for its work, as an approval passes it.

    Its summary holds what the shown message leaves out, such as which target
    or "draft only, do not send", so the Action must read it too.
    """

    if not isinstance(todo.origin, SuggestionOrigin):
        return None
    suggestion_id = todo.origin.suggestion_id
    with todo_db_connection() as connection:
        row = connection.execute(
            "SELECT suggestion_summary, target_context_json FROM agent_suggestions "
            "WHERE user_id = ? AND suggestion_id = ?",
            (user_id, suggestion_id),
        ).fetchone()
    # A Suggestion deleted meanwhile cannot open the Action; the submit refuses it.
    if row is None:
        return None
    raw_target = row["target_context_json"]
    target = (
        None
        if raw_target is None
        else SuggestionTargetContext.model_validate_json(raw_target)
    )
    return TodoHandoffSuggestion(
        suggestion_id=suggestion_id,
        summary=row["suggestion_summary"],
        organization_name=None if target is None else target.organization_name,
        project_name=None if target is None else target.project_name,
    )


def _handoff(
    todo: Todo, settings: WorkspaceSettings, suggestion: TodoHandoffSuggestion | None
) -> TodoHandoffInput:
    """The TODO's project with the folders the Action composer would send for it."""

    project = next(
        (p for p in settings.projects if p.project_id == todo.project_id), None
    )
    return TodoHandoffInput(
        todo_id=todo.todo_id,
        suggestion=suggestion,
        project=None
        if project is None
        else TodoHandoffProject(
            display_name=project.display_name,
            paths=tuple(
                folder.real_path
                for folder in settings.folders
                if project.project_id in folder.project_ids
            ),
        ),
    )


__all__ = ["pick_up_todos"]
