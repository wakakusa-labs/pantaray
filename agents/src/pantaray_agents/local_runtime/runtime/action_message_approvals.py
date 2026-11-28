"""A USER message sent while approvals wait answers them as set aside.

The user wrote instead of answering, so each paused process's pending approval
ends ``interrupted`` (set aside, not denied) and that process resumes at once,
exactly as a card decision resumes it: the root settles its paused call as not
run and reads the message at its next THINK; a child settles its own and goes
on, and the root reads the message once the child returns. As with a card, one
answer releases a whole process, so only its first blocker is settled.
"""

from __future__ import annotations

import sqlite3

from .action_approval import (
    ActionApprovalTarget,
    resume_paused_action_approval_in_connection,
)
from .action_approval_projection import (
    PendingApprovalBlocker,
    load_pending_approval_blockers_in_connection,
)
from .action_subagent_approval import settle_paused_child_approval_in_connection


def set_aside_pending_approvals_in_connection(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    action_id: str,
    root_process_id: str,
    decided_at: str,
) -> str | None:
    """Resume every process waiting on an approval; the root's id if it was one."""

    first_blockers: dict[str, PendingApprovalBlocker] = {}
    for blocker in load_pending_approval_blockers_in_connection(
        connection,
        user_id=user_id,
        action_id=action_id,
        root_process_id=root_process_id,
    ):
        first_blockers.setdefault(blocker.process_id, blocker)
    for blocker in first_blockers.values():
        if blocker.process_id == root_process_id:
            resume_paused_action_approval_in_connection(
                connection,
                target=ActionApprovalTarget(
                    user_id=user_id,
                    action_id=action_id,
                    process_id=root_process_id,
                    tool_request_id=blocker.tool_request_id,
                    approval_session_id=blocker.approval_session_id,
                ),
                session_status="interrupted",
                decided_at=decided_at,
            )
        else:
            settle_paused_child_approval_in_connection(
                connection,
                user_id=user_id,
                action_id=action_id,
                child_process_id=blocker.process_id,
                approval_session_id=blocker.approval_session_id,
                tool_request_id=blocker.tool_request_id,
                decision="interrupted",
                decided_at=decided_at,
            )
    return root_process_id if root_process_id in first_blockers else None


__all__ = ["set_aside_pending_approvals_in_connection"]
