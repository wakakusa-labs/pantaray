"""Start the work of every TODO that is ready for it, through `submit_action_message`.

The TODO does not record its Action: the Action's first message id derives
from the TODO id, so `UNIQUE (user_id, initial_user_message_id)` keeps one
Action per TODO however often, or however concurrently, a pass runs. A submit
that fails leaves the TODO without an Action, and the pass says so, so that the
hook that ran it runs the next pass.
The only write on the TODO lets go of the attachments its Action took.
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
from pantaray_agents.local_runtime.todos.display import (
    ContinueAiTodo,
    PickupTarget,
    StartWritten,
    pickup_target,
)
from pantaray_agents.local_runtime.todos.model import Todo, WrittenOrigin
from pantaray_agents.local_runtime.todos.reads import (
    read_pickup_candidates,
)
from pantaray_agents.local_runtime.todos.writes import release_todo_attachments
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
)

logger = logging.getLogger(__name__)


def pick_up_todos(*, user_id: str) -> bool:
    """Start an Action for each TODO `pickup_target` names; whether every
    submit went through."""

    today = read_todo_clock().today
    targets = [
        (entry.todo, target)
        for entry in read_pickup_candidates(user_id=user_id, today=today)
        if (target := pickup_target(entry.todo, entry.conversation, today)) is not None
    ]
    if not targets:
        return True
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    settings = list_workspace_settings(
        db_path=db_path, busy_timeout_ms=busy_timeout_ms, user_id=user_id
    )
    submitted = True
    for todo, target in targets:
        try:
            command = _command(user_id, todo, target, todo_handoff(todo, settings))
            submit_action_message(command)
        # A TODO the Action message cannot carry fails alone, so it never holds
        # back the TODOs after it.
        except (ValidationError, ActionMessageSubmissionError, sqlite3.Error) as exc:
            logger.warning(
                "TODO pickup failed: todo_id=%s error=%s",
                todo.todo_id,
                type(exc).__name__,
            )
            submitted = False
            continue
        logger.info("TODO picked up: todo_id=%s target=%s", todo.todo_id, target.kind)
        if command.message.files or command.message.images:
            _release_attachments(user_id, todo)
    return submitted


def _release_attachments(user_id: str, todo: Todo) -> None:
    """The Action owns the attachments from its first message on, so the TODO
    lets them go: started again, after that conversation was deleted with the
    TODO and the TODO restored, it sends its text alone.

    Released only after the submit commits, so a failed submit keeps them for
    the next pass. A release that fails leaves a TODO whose Action exists, so
    nothing sends them again unless that conversation is deleted.
    """

    try:
        release_todo_attachments(user_id=user_id, todo_id=todo.todo_id)
    except sqlite3.Error as exc:
        logger.warning(
            "TODO attachments not released: todo_id=%s error=%s",
            todo.todo_id,
            type(exc).__name__,
        )


def _command(
    user_id: str, todo: Todo, target: PickupTarget, handoff: TodoHandoffInput
) -> SubmitActionMessageCommand:
    message_id = todo_message_id(todo.todo_id)
    match target:
        case StartWritten():
            # The user wrote this TODO, so its text and attachments are their
            # message. A weekly instance of it has none.
            written = (
                todo.origin
                if isinstance(todo.origin, WrittenOrigin)
                else WrittenOrigin()
            )
            return SubmitActionMessageCommand(
                user_id=user_id,
                origin="user",
                target=NewActionTarget(),
                message=ActionUserMessageInput(
                    message_id=message_id,
                    content=todo.text,
                    images=written.images,
                    files=written.files,
                    todo_handoff=handoff,
                ),
            )
        case ContinueAiTodo():
            return SubmitActionMessageCommand(
                user_id=user_id,
                origin="todo_pickup",
                target=NewActionTarget(todo_text=todo.text),
                message=ActionUserMessageInput(
                    message_id=message_id,
                    content=ACTION_TODO_PICKUP_REQUEST_TEXT,
                    todo_handoff=handoff,
                ),
            )
        case _:
            assert_never(target)


def todo_handoff(todo: Todo, settings: WorkspaceSettings) -> TodoHandoffInput:
    """The TODO's project with the folders the Action composer would send for it."""

    project = next(
        (p for p in settings.projects if p.project_id == todo.project_id), None
    )
    return TodoHandoffInput(
        todo_id=todo.todo_id,
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


__all__ = ["pick_up_todos", "todo_handoff"]
