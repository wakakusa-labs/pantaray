"""Start a TODO import: open its draft, then hand the user's text, files and
images to the Action that drafts it, through `submit_action_message`; and find
the imports still open by that first message.

The Action's first message id derives from the import id, so
`UNIQUE (user_id, initial_user_message_id)` keeps one Action per import however
often the start is sent. The two steps are two transactions: a start that
failed after the draft opened is sent again with the same import id.
"""

from __future__ import annotations

import json
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
from pantaray_agents.local_runtime.runtime.identity import verify_current_owner
from pantaray_agents.local_runtime.runtime.runtime_env import (
    read_local_runtime_db_config,
)
from pantaray_agents.local_runtime.todos.clock import TodoClock
from pantaray_agents.local_runtime.todos.db import (
    AllScope,
    NoOrganizationScope,
    OrganizationScope,
    TodoScope,
    todo_db_connection,
)
from pantaray_agents.local_runtime.todos.imports.model import (
    DraftIssue,
    OpenTodoImport,
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
from pantaray_agents.schema.agent.image import ImageInput

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
    images: tuple[ImageInput, ...],
    clock: TodoClock,
) -> TodoImportStarted | DraftIssue:
    """Open the import and start its Action with ``text``, ``files`` and
    ``images``, the images in the user's own scope.

    The caller gives at least one of them. With no text, the message names the
    files and counts the images, as the user gave nothing else.
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
    content = (text or "").strip() or _attachments_named(files, images)
    submitted = submit_action_message(
        SubmitActionMessageCommand(
            user_id=user_id,
            target=NewActionTarget(),
            message=ActionUserMessageInput(
                message_id=todo_import_message_id(import_id),
                content=content,
                images=images,
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


def _attachments_named(
    files: tuple[FileAttachmentInput, ...], images: tuple[ImageInput, ...]
) -> str:
    names = [file.name for file in files]
    if images:
        names.append("1 image" if len(images) == 1 else f"{len(images)} images")
    return ", ".join(names)


def read_todo_import_action_id(*, user_id: str, import_id: str) -> str | None:
    """The Action that drafts ``import_id``; None until its start went through."""

    with todo_db_connection() as connection:
        row = connection.execute(
            "SELECT action_id FROM agent_actions "
            "WHERE user_id = ? AND initial_user_message_id = ?",
            (user_id, todo_import_message_id(import_id)),
        ).fetchone()
    return None if row is None else str(row[0])


# The first message of each Action named, by the message id it opened with.
_FIRST_CONTENT_SQL: Final = """
SELECT action.initial_user_message_id,
       json_extract(step.user_message_json, '$.content')
FROM agent_actions AS action
JOIN agent_action_steps AS step
  ON step.user_id = action.user_id AND step.action_id = action.action_id
 AND step.user_message_id = action.initial_user_message_id
WHERE action.user_id = :user_id
  AND action.initial_user_message_id IN (SELECT value FROM json_each(:message_ids))
"""


def list_open_todo_imports(
    *, user_id: str, scope: TodoScope
) -> tuple[OpenTodoImport, ...]:
    """The imports neither registered nor canceled that were started in
    ``scope``, or in any scope for `AllScope`, newest first.

    An import whose Action never started is left out: its start failed, and
    sending the same text again reuses it.
    """

    verify_current_owner(user_id)
    params: dict[str, str] = {"user_id": user_id}
    match scope:
        case AllScope():
            in_scope = "1"
        case NoOrganizationScope():
            in_scope = "scope_kind = 'no_organization'"
        case OrganizationScope(organization_id=organization_id):
            in_scope = "organization_id = :organization_id"
            params["organization_id"] = organization_id
    with todo_db_connection() as connection:
        drafts = connection.execute(
            "SELECT import_id, created_at FROM todo_import_drafts "
            f"WHERE user_id = :user_id AND state = 'open' AND {in_scope} "
            "ORDER BY created_at DESC, import_id",
            params,
        ).fetchall()
        message_ids = [todo_import_message_id(row[0]) for row in drafts]
        contents: dict[str, str] = dict(
            connection.execute(
                _FIRST_CONTENT_SQL,
                {"user_id": user_id, "message_ids": json.dumps(message_ids)},
            ).fetchall()
        )
    return tuple(
        OpenTodoImport(
            import_id=import_id,
            # Never blank: the start strips the text, or names the attachments.
            request=content.splitlines()[0],
            started_at=started_at,
        )
        for (import_id, started_at), message_id in zip(drafts, message_ids, strict=True)
        if (content := contents.get(message_id)) is not None
    )


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


__all__ = [
    "TodoImportStarted",
    "list_open_todo_imports",
    "read_todo_import_action_id",
    "start_todo_import",
    "todo_import_message_id",
]
