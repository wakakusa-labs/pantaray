"""Decide whether one Action can still open a follow-up turn, and what it replays.

A follow-up turn continues from the Action's newest checkpoint, or starts fresh
when the Action has none. The runtime refuses any checkpoint version but its own,
so an Action whose newest checkpoint is another version cannot continue, and a
turn accepted for it would fail on its first step. A run that ends before writing
a checkpoint of its own (the user stops it during its first model call) leaves its
USER step after the newest checkpoint unanswered; the next run restores that
checkpoint, or starts fresh, and replays every such step, in order, before its own
message. This module owns both rules: the USER message boundary enforces the
first, the conversation read model reports it as ``resumable`` once the Action's
latest intent is the user's Stop, and the runtime and queued-message adoption
replay the same steps.
"""

from __future__ import annotations

import sqlite3

from pantaray_agents.agents.action_agent.runtime.checkpoint import (
    RUNTIME_STATE_CHECKPOINT_VERSION,
)
from pantaray_agents.repositories.action_runtime_resume_contract import (
    ActionResumeUserStep,
    parse_action_resume_user_step,
)


def action_accepts_followup_turn(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    action_id: str,
) -> bool:
    """Say whether the next run can start: fresh, or on a checkpoint it restores."""

    row = _newest_checkpoint_version(connection, user_id=user_id, action_id=action_id)
    return row is None or row[0] == RUNTIME_STATE_CHECKPOINT_VERSION


def action_has_restorable_checkpoint(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    action_id: str,
) -> bool:
    """Say whether the Action's newest checkpoint is one this build restores."""

    row = _newest_checkpoint_version(connection, user_id=user_id, action_id=action_id)
    return row is not None and row[0] == RUNTIME_STATE_CHECKPOINT_VERSION


def _newest_checkpoint_version(
    connection: sqlite3.Connection, *, user_id: str, action_id: str
) -> tuple[object] | None:
    return connection.execute(
        """
        SELECT runtime_state_checkpoint_version
        FROM agent_action_steps
        WHERE user_id = ? AND action_id = ? AND runtime_state_checkpoint IS NOT NULL
        ORDER BY step_number DESC, completed_at IS NULL ASC, completed_at DESC,
                 created_at DESC, step_id DESC
        LIMIT 1
        """,
        (user_id, action_id),
    ).fetchone()


def load_unanswered_user_steps_in_connection(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    action_id: str,
    after_step_number: int,
) -> tuple[ActionResumeUserStep, ...]:
    """The adopted USER steps after the checkpoint at ``after_step_number`` (0 when
    the Action has none), oldest first."""

    rows = connection.execute(
        """
        SELECT step_id, user_id, action_id, accepted_sequence, step_number,
               local_step_number, short_step_id, step_type, status,
               user_message_id, user_message_json, user_request_text,
               adopted_process_id, created_at
        FROM agent_action_steps
        WHERE user_id = ? AND action_id = ?
          AND step_type = 'user_request' AND status = 'success'
          AND adopted_process_id IS NOT NULL
          AND step_number > ?
        ORDER BY step_number, accepted_sequence, step_id
        """,
        (user_id, action_id, after_step_number),
    ).fetchall()
    return tuple(
        parse_action_resume_user_step(
            dict(row), expected_user_id=user_id, expected_action_id=action_id
        )
        for row in rows
    )


__all__ = [
    "action_accepts_followup_turn",
    "action_has_restorable_checkpoint",
    "load_unanswered_user_steps_in_connection",
]
