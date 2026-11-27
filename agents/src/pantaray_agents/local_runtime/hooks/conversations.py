"""Send a task's prompt as a turn of its conversation, and count how turns go.

A task's conversation is the one it was written from. A turn goes through
``submit_action_message`` as an ordinary next turn. Its first message id
derives from the task, its content and the occurrence, and is recorded as the
task's latest turn before it is sent, so a fire repeated after a crash finds
its turn. A task fires only once its latest turn has ended and been counted,
and while its conversation is not running or waiting for the user's approval.
Three failures in a row stop the task.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from pydantic import ValidationError

from pantaray_agents.hooks.engine import Fire, FireOutcome
from pantaray_agents.local_runtime.runtime.action_message_models import (
    ActionMessageSubmissionError,
    ExistingActionTarget,
    ExpectedProcessConflictError,
    SubmitActionMessageCommand,
    action_hook_request_text,
)
from pantaray_agents.local_runtime.runtime.action_message_process_disposition import (
    classify_action_process_disposition_in_connection,
)
from pantaray_agents.local_runtime.runtime.action_messages import (
    submit_action_message,
)
from pantaray_agents.local_runtime.storage.migrations.connection import (
    configure_connection,
)
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction
from pantaray_agents.local_runtime.storage.users import ensure_user_row
from pantaray_agents.schema.agent.action_message import ActionUserMessageInput

from .controls import (
    NO_CONVERSATION,
    HookControl,
    StoppedReason,
    TaskConversation,
    TurnResult,
    control_of,
    read_controls,
    task_origin_in_connection,
)
from .tasks import Task

logger = logging.getLogger(__name__)

# Failures in a row that stop a task, so it does not keep failing unseen.
TASK_FAILURE_LIMIT: Final = 3
_NAMESPACE: Final = uuid.uuid5(uuid.NAMESPACE_URL, "pantaray:hook_turn_message:v1")
# A run's terminal status, as a turn result.
_ENDED: Final[dict[str, TurnResult]] = {
    "completed": "completed",
    "failed": "failed",
    "canceled": "canceled",
}


def _connect(db_path: Path, busy_timeout_ms: int) -> closing[sqlite3.Connection]:
    connection = sqlite3.connect(db_path)
    # The Action process classifier reads its rows by column name.
    connection.row_factory = sqlite3.Row
    configure_connection(connection, busy_timeout_ms)
    return closing(connection)


@dataclass(frozen=True, slots=True)
class _Send:
    """Send the turn to this conversation."""

    action_id: str


def fire_task(
    fire: Fire,
    task: Task,
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
) -> FireOutcome:
    occurrence = fire.occurrence.isoformat()
    message_id = str(
        uuid.uuid5(_NAMESPACE, f"{task.hook.id}\n{task.content_hash}\n{occurrence}")
    )
    with _connect(db_path, busy_timeout_ms) as connection:
        with immediate_transaction(connection):
            claim = _claim_turn(
                connection,
                user_id=user_id,
                hook_id=task.hook.id,
                content_hash=task.content_hash,
                message_id=message_id,
            )
    if claim == "wait":
        return "retry"
    if isinstance(claim, _Send):
        try:
            submit_action_message(
                SubmitActionMessageCommand(
                    user_id=user_id,
                    origin="hook",
                    target=ExistingActionTarget(
                        action_id=claim.action_id, expected_process_id=None
                    ),
                    message=ActionUserMessageInput(
                        message_id=message_id,
                        content=action_hook_request_text(task.run.prompt),
                    ),
                )
            )
        # Started by the user since the claim: wait for it.
        except ExpectedProcessConflictError:
            return "retry"
        except (ValidationError, ActionMessageSubmissionError, sqlite3.Error) as exc:
            logger.warning(
                "task turn not started: hook=%s error=%s",
                task.hook.id,
                type(exc).__name__,
            )
            _record_result(
                db_path,
                busy_timeout_ms,
                user_id=user_id,
                hook_id=task.hook.id,
                result="not_started",
            )
            return "retry"
    return "done"


def _claim_turn(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    hook_id: str,
    content_hash: str,
    message_id: str,
) -> Literal["wait", "sent"] | _Send:
    """Make ``message_id`` the task's latest turn, once the one before it has
    ended and been counted, while the task is still on and approved as read;
    in the caller's immediate transaction.

    The turn is recorded before it is sent, so a crash after the send finds
    it on the next fire. The task is read again here because the tick read it
    earlier: a task stopped or deleted with its conversation since must not
    send a turn, least of all to a new conversation.
    """

    ensure_user_row(connection, user_id=user_id)
    # The origin's reference keeps that conversation while the task exists.
    origin = task_origin_in_connection(connection, user_id=user_id, hook_id=hook_id)
    if origin is None or control_of(
        read_controls(connection, user_id=user_id), hook_id
    ) != HookControl(enabled=True, approved_hash=content_hash):
        return "wait"
    conversation, unresolved = _settle_in_connection(
        connection, user_id=user_id, hook_id=hook_id
    )
    if conversation.stopped_reason is not None or unresolved:
        return "wait"
    if _turn_of_message(connection, user_id=user_id, message_id=message_id) is not None:
        return "sent"
    if _is_busy(connection, user_id=user_id, action_id=origin):
        return "wait"
    connection.execute(
        """INSERT INTO hook_conversations(user_id, hook_id, last_message_id)
           VALUES (?, ?, ?)
           ON CONFLICT(user_id, hook_id) DO UPDATE SET
             last_message_id = excluded.last_message_id, last_result = NULL""",
        (user_id, hook_id, message_id),
    )
    return _Send(action_id=origin)


def record_turn_results(connection: sqlite3.Connection, *, user_id: str) -> None:
    """Count each task's latest turn once it has ended."""

    for (hook_id,) in connection.execute(
        """SELECT hook_id FROM hook_conversations
           WHERE user_id = ? AND last_message_id IS NOT NULL
             AND last_result IS NULL""",
        (user_id,),
    ).fetchall():
        with immediate_transaction(connection):
            _settle_in_connection(connection, user_id=user_id, hook_id=str(hook_id))


def _settle_in_connection(
    connection: sqlite3.Connection, *, user_id: str, hook_id: str
) -> tuple[TaskConversation, bool]:
    """The task's state once its latest turn, if it ended, is counted
    (exactly once); and whether that turn is still unresolved.

    A latest turn with no message was never sent: a crash came before the
    send, or the send was refused and counted then.
    """

    conversation = _conversation(connection, user_id=user_id, hook_id=hook_id)
    if conversation.last_message_id is None or conversation.last_result is not None:
        return conversation, False
    status = _turn_of_message(
        connection, user_id=user_id, message_id=conversation.last_message_id
    )
    if status is None:
        return conversation, False
    result = _ENDED.get(status)
    if result is None:
        return _conversation(connection, user_id=user_id, hook_id=hook_id), True
    _record_in_connection(connection, user_id=user_id, hook_id=hook_id, result=result)
    return _conversation(connection, user_id=user_id, hook_id=hook_id), False


def _record_result(
    db_path: Path,
    busy_timeout_ms: int,
    *,
    user_id: str,
    hook_id: str,
    result: TurnResult,
) -> None:
    with _connect(db_path, busy_timeout_ms) as connection, connection:
        ensure_user_row(connection, user_id=user_id)
        _record_in_connection(
            connection, user_id=user_id, hook_id=hook_id, result=result
        )


def _failures_after(failure_count: int, result: TurnResult) -> int:
    """A success clears the failures; a turn the user canceled leaves them."""

    if result == "completed":
        return 0
    if result == "canceled":
        return failure_count
    return failure_count + 1


def _record_in_connection(
    connection: sqlite3.Connection, *, user_id: str, hook_id: str, result: TurnResult
) -> None:
    row = connection.execute(
        "SELECT failure_count FROM hook_conversations WHERE user_id = ? AND hook_id = ?",
        (user_id, hook_id),
    ).fetchone()
    failure_count = _failures_after(0 if row is None else int(row[0]), result)
    reason: StoppedReason | None = None
    if failure_count >= TASK_FAILURE_LIMIT:
        reason = "start_failed" if result == "not_started" else "turn_failed"
        logger.warning(
            "task stopped after %d failures: hook=%s reason=%s",
            failure_count,
            hook_id,
            reason,
        )
    connection.execute(
        """INSERT INTO hook_conversations(user_id, hook_id, last_result,
             failure_count, stopped_reason) VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(user_id, hook_id) DO UPDATE SET
             last_result = excluded.last_result,
             failure_count = excluded.failure_count,
             stopped_reason = excluded.stopped_reason""",
        (user_id, hook_id, result, failure_count, reason),
    )


def _conversation(
    connection: sqlite3.Connection, *, user_id: str, hook_id: str
) -> TaskConversation:
    row = connection.execute(
        """SELECT last_message_id, last_result, failure_count, stopped_reason
           FROM hook_conversations WHERE user_id = ? AND hook_id = ?""",
        (user_id, hook_id),
    ).fetchone()
    if row is None:
        return NO_CONVERSATION
    return TaskConversation(
        last_message_id=row[0],
        last_result=row[1],
        failure_count=int(row[2]),
        stopped_reason=row[3],
    )


def _turn_of_message(
    connection: sqlite3.Connection, *, user_id: str, message_id: str
) -> str | None:
    """The status of the run a message started; None when it was never sent."""

    row = connection.execute(
        """SELECT turn.status FROM agent_action_steps AS step
           JOIN processes AS turn
             ON turn.user_id = step.user_id
            AND turn.process_id = step.adopted_process_id
           WHERE step.user_id = ? AND step.user_message_id = ? LIMIT 1""",
        (user_id, message_id),
    ).fetchone()
    return None if row is None else str(row[0])


def _is_busy(connection: sqlite3.Connection, *, user_id: str, action_id: str) -> bool:
    """Whether a run of the Action is queued, running or waiting for approval."""

    return (
        classify_action_process_disposition_in_connection(
            connection=connection,
            user_id=user_id,
            action_id=action_id,
            expected_process_id=None,
        ).disposition
        == "stale"
    )
