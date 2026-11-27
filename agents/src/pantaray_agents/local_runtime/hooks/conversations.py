"""Send a task's message as a turn of its target conversation, and count how
turns go.

A turn goes through ``submit_action_message`` as an ordinary next turn. Its
message id derives from the task, its version and the occurrence, and is
recorded as the task's latest turn before it is sent, so a fire repeated after
a crash finds its turn. A task fires only once its latest turn has ended and
been counted, and while its conversation is not running or waiting for the
user's approval. Three failures in a row stop the task.
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
from pantaray_agents.schema.agent.action_message import ActionUserMessageInput

from .controls import RunState, StoppedReason, TaskRow, TurnResult

logger = logging.getLogger(__name__)

# Failures in a row that stop a task, so it does not keep failing unseen.
TASK_FAILURE_LIMIT: Final = 3
# A task deleted since its row was read.
_NO_RUN: Final = RunState(
    last_message_id=None, last_result=None, failure_count=0, stopped_reason=None
)
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
    task: TaskRow,
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
) -> FireOutcome:
    hook_id = task.task.hook_id
    occurrence = fire.occurrence.isoformat()
    message_id = str(uuid.uuid5(_NAMESPACE, f"{hook_id}\n{task.version}\n{occurrence}"))
    with _connect(db_path, busy_timeout_ms) as connection:
        with immediate_transaction(connection):
            claim = _claim_turn(
                connection,
                user_id=user_id,
                hook_id=hook_id,
                version=task.version,
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
                        content=action_hook_request_text(task.task.message),
                    ),
                )
            )
        # Started by the user since the claim: wait for it.
        except ExpectedProcessConflictError:
            return "retry"
        except (ValidationError, ActionMessageSubmissionError, sqlite3.Error) as exc:
            logger.warning(
                "task turn not started: hook=%s error=%s",
                hook_id,
                type(exc).__name__,
            )
            _record_result(
                db_path,
                busy_timeout_ms,
                user_id=user_id,
                hook_id=hook_id,
                result="not_started",
            )
            return "retry"
    return "done"


def _claim_turn(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    hook_id: str,
    version: int,
    message_id: str,
) -> Literal["wait", "sent"] | _Send:
    """Make ``message_id`` the task's latest turn, once the one before it has
    ended and been counted, while the task is still on and the version read;
    in the caller's immediate transaction.

    The turn is recorded before it is sent, so a crash after the send finds
    it on the next fire. The task is read again here because the tick read it
    earlier: a task stopped or deleted with its conversation since must not
    send a turn, least of all to a new conversation.
    """

    # The target's reference keeps that conversation while the task exists.
    row = connection.execute(
        """SELECT target_action_id FROM user_hooks
           WHERE user_id = ? AND hook_id = ? AND version = ? AND enabled = 1""",
        (user_id, hook_id, version),
    ).fetchone()
    if row is None:
        return "wait"
    target = str(row[0])
    conversation, unresolved = _settle_in_connection(
        connection, user_id=user_id, hook_id=hook_id
    )
    if conversation.stopped_reason is not None or unresolved:
        return "wait"
    if _turn_of_message(connection, user_id=user_id, message_id=message_id) is not None:
        return "sent"
    if _is_busy(connection, user_id=user_id, action_id=target):
        return "wait"
    connection.execute(
        """UPDATE user_hooks SET last_message_id = ?, last_result = NULL
           WHERE user_id = ? AND hook_id = ?""",
        (message_id, user_id, hook_id),
    )
    return _Send(action_id=target)


def record_turn_results(connection: sqlite3.Connection, *, user_id: str) -> None:
    """Count each task's latest turn once it has ended."""

    for (hook_id,) in connection.execute(
        """SELECT hook_id FROM user_hooks
           WHERE user_id = ? AND last_message_id IS NOT NULL
             AND last_result IS NULL""",
        (user_id,),
    ).fetchall():
        with immediate_transaction(connection):
            _settle_in_connection(connection, user_id=user_id, hook_id=str(hook_id))


def _settle_in_connection(
    connection: sqlite3.Connection, *, user_id: str, hook_id: str
) -> tuple[RunState, bool]:
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
        "SELECT failure_count FROM user_hooks WHERE user_id = ? AND hook_id = ?",
        (user_id, hook_id),
    ).fetchone()
    # Deleted since: nothing is left to count on.
    if row is None:
        return
    failure_count = _failures_after(int(row[0]), result)
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
        """UPDATE user_hooks SET last_result = ?, failure_count = ?,
             stopped_reason = ? WHERE user_id = ? AND hook_id = ?""",
        (result, failure_count, reason, user_id, hook_id),
    )


def _conversation(
    connection: sqlite3.Connection, *, user_id: str, hook_id: str
) -> RunState:
    row = connection.execute(
        """SELECT last_message_id, last_result, failure_count, stopped_reason
           FROM user_hooks WHERE user_id = ? AND hook_id = ?""",
        (user_id, hook_id),
    ).fetchone()
    if row is None:
        return _NO_RUN
    return RunState(
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
