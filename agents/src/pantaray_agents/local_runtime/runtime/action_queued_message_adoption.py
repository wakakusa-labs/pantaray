"""Give the next run the messages a stopped or failed run never took.

A message sent while a run is busy waits for that run's next THINK. When the run
ends first, a Stop (or a run that ends without a checkpoint) records the message
as not executed instead of starting another run. The next run the user starts on
the Action takes those messages first, in the order they were sent, the way a
finished run hands its waiting messages to its successor.
"""

from __future__ import annotations

import json
import sqlite3

from pantaray_agents.agents.action_agent.runtime.checkpoint import (
    RUNTIME_STATE_CHECKPOINT_VERSION,
    restore_runtime_state_checkpoint,
    validate_runtime_checkpoint_payload,
)
from pantaray_agents.application.action.new_user_turn import (
    reset_state_for_new_user_turn,
)
from pantaray_agents.local_runtime.storage.migrations import MigrationError

from .action_resumability import action_latest_run_has_restorable_checkpoint
from .action_user_adoption import (
    adopt_pending_action_user_steps_in_connection,
    load_not_executed_action_user_steps_in_connection,
)


def adopt_not_executed_messages_into_new_run_in_connection(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    action_id: str,
    suggestion_id: str | None,
    process_id: str,
    adopted_at: str,
) -> None:
    """Adopt every not-executed USER turn into the run ``process_id`` opens.

    Call it before the new message takes its position, so the waiting turns come
    first. They join the latest checkpoint, which the new message then continues
    from like any follow-up.
    """

    waiting = load_not_executed_action_user_steps_in_connection(
        connection=connection, user_id=user_id, action_id=action_id
    )
    # Only the follow-up to a run that failed before its first checkpoint has no
    # state to add them to; they stay not executed then.
    if not waiting or not action_latest_run_has_restorable_checkpoint(
        connection=connection, user_id=user_id, action_id=action_id
    ):
        return
    row = connection.execute(
        """SELECT runtime_state_checkpoint, runtime_state_checkpoint_version
        FROM agent_action_steps
        WHERE user_id = ? AND action_id = ? AND runtime_state_checkpoint IS NOT NULL
        ORDER BY step_number DESC, completed_at IS NULL ASC, completed_at DESC,
                 created_at DESC, step_id DESC
        LIMIT 1""",
        (user_id, action_id),
    ).fetchone()
    checkpoint_json, version = row
    if version != RUNTIME_STATE_CHECKPOINT_VERSION:
        raise MigrationError("Action checkpoint cannot take the waiting messages")
    checkpoint = json.loads(checkpoint_json)
    validate_runtime_checkpoint_payload(checkpoint)
    state = reset_state_for_new_user_turn(
        restore_runtime_state_checkpoint(
            checkpoint,
            expected_action_id=action_id,
            expected_suggestion_id=suggestion_id,
            expected_user_id=user_id,
        ),
        started_at=adopted_at,
    )
    adopt_pending_action_user_steps_in_connection(
        connection=connection,
        user_id=user_id,
        action_id=action_id,
        adopted_process_id=process_id,
        state=state,
        adopted_at=adopted_at,
        pending_steps=waiting,
    )


__all__ = ["adopt_not_executed_messages_into_new_run_in_connection"]
