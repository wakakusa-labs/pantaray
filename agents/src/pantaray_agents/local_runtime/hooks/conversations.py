"""Send a task's prompt as a turn of its conversation, and count how turns go.

A turn goes through ``submit_action_message`` as an ordinary next turn. Its
first message id derives from the task, its content and the occurrence (or
the event it answers), so a fire repeated after a crash finds its turn instead
of starting another. A conversation that runs or waits for the user's approval
holds the task due. Three failures in a row stop the task.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path
from typing import Final

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
from pantaray_agents.local_runtime.storage.users import ensure_user_row
from pantaray_agents.schema.agent.action_message import ActionUserMessageInput

from .controls import StoppedReason, TaskConversation, TurnResult
from .inbox import consume_in_connection
from .tasks import Task

logger = logging.getLogger(__name__)

# Failures in a row that stop a task, so it does not keep failing unseen.
TASK_FAILURE_LIMIT: Final = 3
_NAMESPACE: Final = uuid.uuid5(uuid.NAMESPACE_URL, "pantaray:hook_turn_message:v1")


def _connect(db_path: Path, busy_timeout_ms: int) -> closing[sqlite3.Connection]:
    connection = sqlite3.connect(db_path)
    # The Action process classifier reads its rows by column name.
    connection.row_factory = sqlite3.Row
    configure_connection(connection, busy_timeout_ms)
    return closing(connection)


def fire_task(
    fire: Fire,
    task: Task,
    conversation: TaskConversation,
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
        sent_to = _action_of_message(connection, user_id=user_id, message_id=message_id)
        bound = conversation.action_id
        if (
            sent_to is None
            and bound is not None
            and _is_busy(connection, user_id=user_id, action_id=bound)
        ):
            return "retry"
    if sent_to is None:
        target = (
            ExistingActionTarget(action_id=bound, expected_process_id=None)
            if task.run.conversation == "continue" and bound is not None
            else NewActionTarget()
        )
        try:
            sent_to = submit_action_message(
                SubmitActionMessageCommand(
                    user_id=user_id,
                    origin="hook",
                    target=target,
                    message=ActionUserMessageInput(
                        message_id=message_id,
                        content=action_hook_request_text(task.run.prompt),
                    ),
                )
            ).action_id
        # Started by the user or by Pantaray since the check: wait for it.
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
        _bind(
            connection,
            user_id=user_id,
            hook_id=task.hook.id,
            action_id=sent_to,
            message_id=message_id,
        )
        consume_in_connection(
            connection,
            hook_id=task.hook.id,
            delivery_ids=[
                *(event.delivery_id for event in fire.events),
                *fire.superseded,
            ],
        )
    return "done"


def record_turn_results(connection: sqlite3.Connection, *, user_id: str) -> None:
    """Record the result of each task's latest turn that has ended since."""

    for hook_id, status in connection.execute(
        """SELECT task.hook_id, turn.status FROM hook_conversations AS task
           JOIN agent_action_steps AS step
             ON step.user_id = task.user_id
            AND step.user_message_id = task.last_message_id
           JOIN processes AS turn
             ON turn.user_id = step.user_id
            AND turn.process_id = step.adopted_process_id
           WHERE task.user_id = ? AND task.last_result IS NULL
             AND turn.status IN ('completed', 'failed', 'canceled')""",
        (user_id,),
    ).fetchall():
        _record_in_connection(
            connection, user_id=user_id, hook_id=str(hook_id), result=status
        )


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
    connection: sqlite3.Connection,
    *,
    user_id: str,
    hook_id: str,
    action_id: str,
    message_id: str,
) -> None:
    """The turn just sent is the task's latest; one already recorded stays."""

    ensure_user_row(connection, user_id=user_id)
    connection.execute(
        """INSERT INTO hook_conversations(user_id, hook_id, action_id,
             last_message_id) VALUES (?, ?, ?, ?)
           ON CONFLICT(user_id, hook_id) DO UPDATE SET
             action_id = excluded.action_id,
             last_message_id = excluded.last_message_id,
             last_result = NULL
           WHERE last_message_id IS NOT excluded.last_message_id""",
        (user_id, hook_id, action_id, message_id),
    )


def _action_of_message(
    connection: sqlite3.Connection, *, user_id: str, message_id: str
) -> str | None:
    row = connection.execute(
        """SELECT action_id FROM agent_action_steps
           WHERE user_id = ? AND user_message_id = ? LIMIT 1""",
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
