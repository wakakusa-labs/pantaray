"""Withdraw a USER message that no run has adopted yet.

An unadopted message is a USER step with ``step_number IS NULL``: sent while a
run was busy, or left behind when its run ended or was stopped. Adoption claims
such a step inside an IMMEDIATE transaction, so withdrawing it in one too means
that whichever commits first wins and the other sees the result.

The step row goes; a tombstone keeps its ``message_id``, so the submit boundary
answers a retry of that same send as withdrawn and never registers it again.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from functools import partial
from pathlib import Path

from pantaray_agents.local_runtime.storage.migrations.connection import (
    configure_connection,
)
from pantaray_agents.local_runtime.storage.transactions import (
    immediate_transaction,
    register_after_commit,
)
from pantaray_agents.local_runtime.tooling.action_session_temp_paths import (
    resolve_action_storage_paths,
)
from pantaray_agents.local_runtime.tooling.resources.action_session_temp_cleanup import (
    remove_descriptor_confined_directory_child,
)
from pantaray_agents.schema.agent.action_message import ACTION_ATTACHMENTS_DIRNAME
from pantaray_agents.tasks.action_user_message import parse_action_user_message

from .action_message_models import (
    ExistingActionTarget,
    MessageIdentityConflictError,
    MessageWithdrawnError,
    SubmitActionMessageCommand,
)
from .identity import verify_current_owner
from .runtime_env import read_local_runtime_db_config
from .utc_timestamps import now_utc_iso


class ActionMessageAdoptedError(RuntimeError):
    """A run adopted the message before it could be withdrawn."""


def withdraw_action_message(*, user_id: str, action_id: str, step_id: str) -> None:
    """Delete the caller's unadopted USER step and the files it brought.

    A step that is not the caller's, not in that Action, or already gone is
    left as it is: the message is withdrawn either way. Attached images stay:
    the image store also holds what users attach elsewhere.

    Raises:
        ActionMessageAdoptedError: a run adopted the step first.
    """

    verify_current_owner(user_id)
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    with closing(sqlite3.connect(db_path)) as connection:
        configure_connection(connection, busy_timeout_ms)
        with immediate_transaction(connection):
            row = connection.execute(
                """SELECT step_number, user_message_id, user_message_json
                FROM agent_action_steps
                WHERE user_id = ? AND action_id = ? AND step_id = ?
                  AND step_type = 'user_request'""",
                (user_id, action_id, step_id),
            ).fetchone()
            if row is None:
                return
            step_number, message_id, message_json = row
            if step_number is not None:
                raise ActionMessageAdoptedError("the message was adopted by a run")
            connection.execute(
                """DELETE FROM agent_action_steps
                WHERE step_id = ? AND step_number IS NULL
                  AND adopted_process_id IS NULL""",
                (step_id,),
            )
            connection.execute(
                """INSERT INTO action_withdrawn_messages(
                    user_id, message_id, action_id, withdrawn_at
                ) VALUES (?, ?, ?, ?)""",
                (user_id, message_id, action_id, now_utc_iso()),
            )
            files = parse_action_user_message(str(message_json)).files
            if files:
                # Submit linked each attachment id for this message alone (a
                # second link of the id is refused, and the staged copy is gone),
                # so nothing else can reach its directory.
                register_after_commit(
                    connection=connection,
                    callback=partial(
                        _remove_attachments,
                        attachments_path=resolve_action_storage_paths(
                            db_path=db_path, user_id=user_id, action_id=action_id
                        ).workspace
                        / ACTION_ATTACHMENTS_DIRNAME,
                        attachment_ids=tuple(file.attachment_id for file in files),
                    ),
                )


def refuse_withdrawn_message_in_connection(
    *, connection: sqlite3.Connection, command: SubmitActionMessageCommand
) -> None:
    """Refuse a send whose ``message_id`` the user withdrew.

    Raises:
        MessageWithdrawnError: it is that withdrawn message again.
        MessageIdentityConflictError: the id was withdrawn from another Action.
    """

    row = connection.execute(
        """SELECT action_id FROM action_withdrawn_messages
        WHERE user_id = ? AND message_id = ?""",
        (command.user_id, command.message.message_id),
    ).fetchone()
    if row is None:
        return
    target = command.target
    if not isinstance(target, ExistingActionTarget) or target.action_id != row[0]:
        raise MessageIdentityConflictError(
            "message_id belongs to a message withdrawn from another Action"
        )
    raise MessageWithdrawnError("the user withdrew this message")


def _remove_attachments(
    *, attachments_path: Path, attachment_ids: tuple[str, ...]
) -> None:
    for attachment_id in attachment_ids:
        remove_descriptor_confined_directory_child(
            parent_path=attachments_path, child_name=attachment_id
        )


__all__ = [
    "ActionMessageAdoptedError",
    "refuse_withdrawn_message_in_connection",
    "withdraw_action_message",
]
