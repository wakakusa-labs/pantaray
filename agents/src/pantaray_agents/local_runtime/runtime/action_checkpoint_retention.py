"""Keep only the Action runtime checkpoints a reader can still select.

Every checkpoint is a full snapshot of the run, so keeping one per step grows
the Action's rows quadratically. Readers select either the newest checkpoint
(resume, startup recovery, resumability, prepare-failure follow-up) or one that
names a given approval (approval decision, continuation, and its recovery
anchor). Every other checkpoint is unreachable, so the writer that supersedes
it clears it in the same transaction.

A resumed run settles its paused step in place, so once the approval has been
consumed no checkpoint names it any more while the job's continuation still
does. That continuation resumes from the newest checkpoint, which the settled
step or a later one wrote.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

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

_APPROVAL_PAUSE_CHECKPOINT_SQL = """
SELECT step_id, step_number FROM agent_action_steps AS step
WHERE step.user_id = :user_id AND step.action_id = :action_id
  AND step.runtime_state_checkpoint IS NOT NULL
  AND ((json_extract(step.runtime_state_checkpoint,
        '$.pending_approval_request.approval_session_id') = :approval_session_id
    AND json_extract(step.runtime_state_checkpoint,
        '$.pending_approval_request.tool_request_id') = :tool_request_id)
    OR EXISTS (SELECT 1 FROM json_each(step.runtime_state_checkpoint,
        '$.current_approval_blockers') AS blocker
        WHERE json_extract(blocker.value, '$.approval_session_id')
                = :approval_session_id
          AND json_extract(blocker.value, '$.tool_request_id') = :tool_request_id))
ORDER BY step.step_number DESC, step.completed_at IS NULL ASC,
         step.completed_at DESC, step.created_at DESC, step.step_id DESC
LIMIT 1
"""

# Only a resumed run consumes a decided approval: a denial is recorded on the
# paused step, and an approved tool claims the session before it runs.
_CONSUMED_APPROVAL_NEWEST_CHECKPOINT_SQL = """
SELECT step_id, step_number FROM agent_action_steps AS step
WHERE step.user_id = :user_id AND step.action_id = :action_id
  AND step.runtime_state_checkpoint IS NOT NULL
  AND EXISTS (SELECT 1 FROM approval_sessions AS approval
      WHERE approval.approval_session_id = :approval_session_id
        AND approval.tool_request_id = :tool_request_id
        AND approval.user_id = :user_id AND approval.action_id = :action_id
        AND (approval.status = 'denied'
             OR (approval.status = 'approved_once'
                 AND approval.claimed_at IS NOT NULL)))
ORDER BY step.step_number DESC, step.completed_at IS NULL ASC,
         step.completed_at DESC, step.created_at DESC, step.step_id DESC
LIMIT 1
"""


@dataclass(frozen=True, slots=True)
class ToolApprovalResumeAnchor:
    step_id: str
    step_number: int
    # False once the resumed run consumed the approval: it is not applied again.
    names_approval: bool


def prune_action_checkpoints_in_connection(
    connection: sqlite3.Connection, *, user_id: str, action_id: str
) -> None:
    """Clear the Action's unreachable checkpoints inside the caller's transaction."""

    connection.execute(
        _PRUNE_SUPERSEDED_CHECKPOINTS_SQL,
        {"user_id": user_id, "action_id": action_id},
    )


def load_tool_approval_resume_anchor_in_connection(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    action_id: str,
    approval_session_id: str,
    tool_request_id: str,
) -> ToolApprovalResumeAnchor | None:
    """Resolve a tool approval continuation in the caller's snapshot."""

    parameters = {
        "user_id": user_id,
        "action_id": action_id,
        "approval_session_id": approval_session_id,
        "tool_request_id": tool_request_id,
    }
    for sql, names_approval in (
        (_APPROVAL_PAUSE_CHECKPOINT_SQL, True),
        (_CONSUMED_APPROVAL_NEWEST_CHECKPOINT_SQL, False),
    ):
        row = connection.execute(sql, parameters).fetchone()
        if row is not None:
            return ToolApprovalResumeAnchor(
                step_id=str(row[0]),
                step_number=int(row[1]),
                names_approval=names_approval,
            )
    return None


__all__ = [
    "ToolApprovalResumeAnchor",
    "load_tool_approval_resume_anchor_in_connection",
    "prune_action_checkpoints_in_connection",
]
