"""Start a TODO import: open its draft, then hand the user's text and files to
the Action that drafts it, through `submit_action_message`.

The Action's first message id derives from the import id, so
`UNIQUE (user_id, initial_user_message_id)` keeps one Action per import however
often the start is sent. The two steps are two transactions: a start that
failed after the draft opened is sent again with the same import id.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final

from pantaray_agents.local_runtime.runtime.action_message_models import (
    NewActionTarget,
    SubmitActionMessageCommand,
)
from pantaray_agents.local_runtime.runtime.action_messages import (
    submit_action_message,
)
from pantaray_agents.local_runtime.runtime.runtime_env import (
    read_local_runtime_db_config,
)
from pantaray_agents.local_runtime.todos.clock import TodoClock
from pantaray_agents.local_runtime.todos.db import OrganizationScope, TodoScope
from pantaray_agents.local_runtime.todos.imports.model import (
    DraftIssue,
    TodoImportView,
)
from pantaray_agents.local_runtime.todos.imports.store import open_todo_import
from pantaray_agents.local_runtime.tooling.repository.workspace_settings import (
    list_workspace_settings,
)
from pantaray_agents.schema.agent.action_message import (
    ActionUserMessageInput,
    FileAttachmentInput,
    TodoImportHandoffInput,
)

_NAMESPACE_TODO_IMPORT_MESSAGE: Final = uuid.uuid5(
    uuid.NAMESPACE_URL, "pantaray:todo_import_message:v1"
)


def todo_import_message_id(import_id: str) -> str:
    """The id of the first message of the Action that drafts ``import_id``."""

    return str(uuid.uuid5(_NAMESPACE_TODO_IMPORT_MESSAGE, import_id))


@dataclass(frozen=True, slots=True)
class TodoImportStarted:
    view: TodoImportView
    action_id: str


def start_todo_import(
    *,
    user_id: str,
    import_id: str,
    scope: TodoScope,
    text: str | None,
    files: tuple[FileAttachmentInput, ...],
    clock: TodoClock,
) -> TodoImportStarted | DraftIssue:
    """Open the import and start its Action with ``text`` and ``files``.

    The caller gives text, files or both. With no text, the message names the
    files, as the user gave nothing else.
    """

    opened = open_todo_import(
        user_id=user_id, import_id=import_id, scope=scope, clock=clock
    )
    if isinstance(opened, DraftIssue):
        return opened
    # The import's own scope: an import opened earlier keeps the one it has.
    scope = opened.scope
    organization_name = None
    if isinstance(scope, OrganizationScope):
        organization_name = _organization_name(user_id, scope.organization_id)
        # Deleted since an earlier start opened the import.
        if organization_name is None:
            return DraftIssue("organization_not_found")
    content = (text or "").strip() or ", ".join(file.name for file in files)
    submitted = submit_action_message(
        SubmitActionMessageCommand(
            user_id=user_id,
            target=NewActionTarget(),
            message=ActionUserMessageInput(
                message_id=todo_import_message_id(import_id),
                content=content,
                files=files,
                todo_import_handoff=TodoImportHandoffInput(
                    import_id=import_id,
                    scope=scope.kind,
                    organization_name=organization_name,
                ),
            ),
        )
    )
    return TodoImportStarted(view=opened, action_id=submitted.action_id)


def _organization_name(user_id: str, organization_id: str) -> str | None:
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    settings = list_workspace_settings(
        db_path=db_path, busy_timeout_ms=busy_timeout_ms, user_id=user_id
    )
    return next(
        (
            organization.display_name
            for organization in settings.organizations
            if organization.organization_id == organization_id
        ),
        None,
    )


__all__ = ["TodoImportStarted", "start_todo_import", "todo_import_message_id"]
