"""Keep only the Action runtime checkpoints a reader can still select.

Every checkpoint is a full snapshot of the run, so keeping one per step grows
the Action's rows quadratically. Readers select either the newest checkpoint
(resume, startup recovery, resumability, prepare-failure follow-up) or one that
names a given approval (approval decision, continuation, and its recovery
anchor). Every other checkpoint is unreachable, so the writer that supersedes
it clears it in the same transaction.
"""

from __future__ import annotations

import sqlite3

# Design limit: a pause row whose approval never settles (Stop while it is
# pending, or a crash after the approval was claimed) keeps its snapshot, one
# per such approval. Revisit if those rows show up in database size reports.
_PRUNE_SUPERSEDED_CHECKPOINTS_SQL = """
UPDATE agent_action_steps
SET runtime_state_checkpoint = NULL, runtime_state_checkpoint_version = NULL
WHERE user_id = :user_id AND action_id = :action_id
  AND runtime_state_checkpoint IS NOT NULL
  AND step_id <> (
      SELECT step_id FROM agent_action_steps
      WHERE user_id = :user_id AND action_id = :action_id
        AND runtime_state_checkpoint IS NOT NULL
      ORDER BY step_number DESC, completed_at IS NULL ASC, completed_at DESC,
               created_at DESC, step_id DESC
      LIMIT 1
  )
  AND json_extract(runtime_state_checkpoint, '$.pending_approval_request') IS NULL
  AND COALESCE(
      json_array_length(runtime_state_checkpoint, '$.current_approval_blockers'), 0
  ) = 0
"""


def prune_action_checkpoints_in_connection(
    connection: sqlite3.Connection, *, user_id: str, action_id: str
) -> None:
    """Clear the Action's unreachable checkpoints inside the caller's transaction."""

    connection.execute(
        _PRUNE_SUPERSEDED_CHECKPOINTS_SQL,
        {"user_id": user_id, "action_id": action_id},
    )


__all__ = ["prune_action_checkpoints_in_connection"]
