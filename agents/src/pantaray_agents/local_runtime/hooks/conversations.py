"""Send a task's prompt as a turn of its conversation, and count how turns go.

A turn goes through ``submit_action_message`` as an ordinary next turn. Its
first message id derives from the task, its content and the occurrence (or
the event it answers), and is recorded as the task's latest turn before it is
sent, so a fire repeated after a crash finds its turn, and a later fire finds
its conversation. A task fires only once its latest turn has ended and been
counted, and while its conversation is not running or waiting for the user's
approval. Three failures in a row stop the task.
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
    NewActionTarget,
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
)
from .inbox import consume_in_connection
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
    """Send the turn: to this conversation, or to a new one when None."""

    action_id: str | None


def fire_task(
    fire: Fire,
    task: Task,
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
) -> FireOutcome:
    # On an on trigger the newest event is the one handed; it names the turn.
    occurrence = (
        f"event:{fire.events[-1].delivery_id}"
        if fire.events
        else fire.occurrence.isoformat()
    )
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
                continues=task.run.conversation == "continue",
            )
    if claim == "wait":
        return "retry"
    if isinstance(claim, _Send):
        try:
            sent_to = submit_action_message(
                SubmitActionMessageCommand(
                    user_id=user_id,
                    origin="hook",
                    target=NewActionTarget()
                    if claim.action_id is None
                    else ExistingActionTarget(
                        action_id=claim.action_id, expected_process_id=None
                    ),
                    message=ActionUserMessageInput(
                        message_id=message_id,
                        content=action_hook_request_text(task.run.prompt),
                    ),
                )
            ).action_id
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
        with _connect(db_path, busy_timeout_ms) as connection, connection:
            _bind(connection, user_id=user_id, hook_id=task.hook.id, action_id=sent_to)
    with _connect(db_path, busy_timeout_ms) as connection, connection:
        consume_in_connection(
            connection,
            hook_id=task.hook.id,
            delivery_ids=[
                *(event.delivery_id for event in fire.events),
                *fire.superseded,
            ],
        )
    return "done"


def _claim_turn(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    hook_id: str,
    content_hash: str,
    message_id: str,
    continues: bool,
) -> Literal["wait", "sent"] | _Send:
    """Make ``message_id`` the task's latest turn, once the one before it has
    ended and been counted, while the task is still on and approved as read;
    in the caller's immediate transaction.

    The turn is recorded before it is sent, so a crash after the send leaves
    durable data that binds its conversation before any later fire. The
    approval is read again here because the tick read it earlier: a task
    stopped or deleted with its conversation since must not send a turn, least
    of all to a new conversation.
    """

    ensure_user_row(connection, user_id=user_id)
    if control_of(read_controls(connection, user_id=user_id), hook_id) != (
        HookControl(enabled=True, approved_hash=content_hash)
    ):
        return "wait"
    conversation, unresolved = _settle_in_connection(
        connection, user_id=user_id, hook_id=hook_id
    )
    if conversation.stopped_reason is not None or unresolved:
        return "wait"
    if _turn_of_message(connection, user_id=user_id, message_id=message_id):
        return "sent"
    bound = conversation.action_id
    if bound is not None and _is_busy(connection, user_id=user_id, action_id=bound):
        return "wait"
    connection.execute(
        """INSERT INTO hook_conversations(user_id, hook_id, last_message_id)
           VALUES (?, ?, ?)
           ON CONFLICT(user_id, hook_id) DO UPDATE SET
             last_message_id = excluded.last_message_id, last_result = NULL""",
        (user_id, hook_id, message_id),
    )
    return _Send(action_id=bound if continues else None)


def record_turn_results(connection: sqlite3.Connection, *, user_id: str) -> None:
    """Bind and count each task's latest turn as far as it has gone."""

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
    """The task's state once its latest turn is bound and, if it ended,
    counted (exactly once); and whether that turn is still unresolved.

    A latest turn with no message was never sent: a crash came before the
    send, or the send was refused and counted then.
    """

    conversation = _conversation(connection, user_id=user_id, hook_id=hook_id)
    if conversation.last_message_id is None or conversation.last_result is not None:
        return conversation, False
    turn = _turn_of_message(
        connection, user_id=user_id, message_id=conversation.last_message_id
    )
    if turn is None:
        return conversation, False
    action_id, status = turn
    if conversation.action_id != action_id:
        _bind(connection, user_id=user_id, hook_id=hook_id, action_id=action_id)
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


def _bind(
    connection: sqlite3.Connection, *, user_id: str, hook_id: str, action_id: str
) -> None:
    """The conversation the task's latest turn went to is the task's."""

    connection.execute(
        "UPDATE hook_conversations SET action_id = ? WHERE user_id = ? AND hook_id = ?",
        (action_id, user_id, hook_id),
    )


def _conversation(
    connection: sqlite3.Connection, *, user_id: str, hook_id: str
) -> TaskConversation:
    row = connection.execute(
        """SELECT action_id, last_message_id, last_result, failure_count,
                  stopped_reason
           FROM hook_conversations WHERE user_id = ? AND hook_id = ?""",
        (user_id, hook_id),
    ).fetchone()
    if row is None:
        return NO_CONVERSATION
    return TaskConversation(
        action_id=row[0],
        last_message_id=row[1],
        last_result=row[2],
        failure_count=int(row[3]),
        stopped_reason=row[4],
    )


def _turn_of_message(
    connection: sqlite3.Connection, *, user_id: str, message_id: str
) -> tuple[str, str] | None:
    """The conversation a message went to, and its run's status."""

    row = connection.execute(
        """SELECT step.action_id, turn.status FROM agent_action_steps AS step
           JOIN processes AS turn
             ON turn.user_id = step.user_id
            AND turn.process_id = step.adopted_process_id
           WHERE step.user_id = ? AND step.user_message_id = ? LIMIT 1""",
        (user_id, message_id),
    ).fetchone()
    return None if row is None else (str(row[0]), str(row[1]))


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
