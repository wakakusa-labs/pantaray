"""Canonical transactional resume of a paused Action root from its approval.

Both a card decision and a USER message sent instead of answering settle the
blocker and requeue the same runtime here, in the caller's transaction, so the
resumed job restores its checkpoint through one ``tool_approval`` continuation.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Literal

from pantaray_agents.local_runtime.storage.migrations import MigrationError
from pantaray_agents.local_runtime.suggestion_state.event_names import (
    EVENT_ACTION_RESUME_REQUESTED,
)
from pantaray_agents.local_runtime.tooling.repository import (
    apply_approval_decision_in_connection,
)
from pantaray_agents.tasks.action_user_message import parse_action_user_message

from .action_approval_projection import (
    append_pending_approval_snapshot_in_connection,
)
from .action_resume_events import (
    ActionResumeEventIdentity,
    append_action_resume_events_in_connection,
)
from .job_payload_builder import build_action_job_payload
from .job_types import ACTION_PROCESS_KIND, LOCAL_ACTION_JOB_TYPE

# What a resumed blocker's approval session ends as. ``interrupted`` is the user
# sending a new message instead of answering: the call is set aside, not denied.
type ApprovalSessionDecision = Literal["approved_once", "denied", "interrupted"]


class ActionApprovalDecisionError(MigrationError):
    """A tool approval decision cannot be applied to the requested Action."""

    def __init__(self, message: str, *, error_code: str) -> None:
        super().__init__(message)
        self.error_code = error_code


@dataclass(frozen=True, slots=True)
class ActionApprovalTarget:
    """One pending approval blocker on a paused Action root process."""

    user_id: str
    action_id: str
    process_id: str
    tool_request_id: str
    approval_session_id: str


@dataclass(frozen=True, slots=True)
class _ActionResumeContext:
    suggestion_id: str | None
    command_id: str
    accepted_at: str


@dataclass(frozen=True, slots=True)
class _PausedActionPredecessor:
    process_id: str
    job_id: str


def resume_paused_action_approval_in_connection(
    connection: sqlite3.Connection,
    *,
    target: ActionApprovalTarget,
    session_status: ApprovalSessionDecision,
    decided_at: str,
) -> str:
    """Settle one pending blocker of a paused root and requeue that same runtime.

    The caller owns the transaction. Returns the requeued job id; the resumed
    job restores the checkpoint through the ``tool_approval`` continuation.
    """

    approval_session_id = _resolve_pending_approval(
        connection, target=target, session_status=session_status
    )
    predecessor = _resolve_paused_predecessor(
        connection,
        target=target,
        approval_session_id=approval_session_id,
    )
    context = _load_action_resume_context(
        connection,
        target=target,
        approval_session_id=approval_session_id,
    )
    _apply_decision(
        connection,
        target=target,
        approval_session_id=approval_session_id,
        session_status=session_status,
        decided_at=decided_at,
    )
    payload = build_action_job_payload(
        {
            "job_id": predecessor.job_id,
            "process_id": predecessor.process_id,
            "action_id": target.action_id,
            "user_id": target.user_id,
            "continuation_ref": {
                "kind": "tool_approval",
                "approval_session_id": approval_session_id,
                "tool_request_id": target.tool_request_id,
            },
        }
    )
    _resume_paused_action_runtime(
        connection,
        predecessor=predecessor,
        target=target,
        payload_json=json.dumps(payload, ensure_ascii=False),
        resumed_at=decided_at,
    )
    append_action_resume_events_in_connection(
        connection=connection,
        identity=ActionResumeEventIdentity(
            user_id=target.user_id,
            action_id=target.action_id,
            suggestion_id=context.suggestion_id,
            command_id=context.command_id,
            accepted_at=context.accepted_at,
            approval_session_id=approval_session_id,
            approval_tool_request_id=target.tool_request_id,
            predecessor_process_id=predecessor.process_id,
            process_id=predecessor.process_id,
            job_id=predecessor.job_id,
            requested_at=decided_at,
        ),
    )
    append_pending_approval_snapshot_in_connection(
        connection,
        user_id=target.user_id,
        action_id=target.action_id,
        root_process_id=predecessor.process_id,
        created_at=decided_at,
    )
    return predecessor.job_id


def _resolve_paused_predecessor(
    connection: sqlite3.Connection,
    *,
    target: ActionApprovalTarget,
    approval_session_id: str,
) -> _PausedActionPredecessor:
    predecessor_rows = connection.execute(
        """
        SELECT
            process_events.process_id,
            json_extract(process_events.payload_json, '$.action_id') AS event_action_id,
            json_extract(process_events.payload_json, '$.user_id') AS event_user_id,
            processes.kind AS process_kind,
            jobs.job_id,
            jobs.user_id AS job_user_id,
            jobs.job_type,
            jobs.logical_key
        FROM process_events
        JOIN processes ON processes.process_id = process_events.process_id
        JOIN jobs ON jobs.process_id = processes.process_id
        WHERE process_events.event_name = 'process_paused'
          AND processes.user_id = ?
          AND processes.action_id = ?
          AND processes.process_id = ?
          AND processes.status = 'paused'
          AND jobs.status = 'paused'
          AND process_events.event_seq = (
              SELECT MAX(latest_pause.event_seq)
              FROM process_events AS latest_pause
              WHERE latest_pause.process_id = process_events.process_id
                AND latest_pause.event_name = 'process_paused'
          )
          AND EXISTS (
              SELECT 1
              FROM json_each(
                  process_events.payload_json,
                  '$.approval_blockers'
              ) AS blocker
              WHERE json_extract(blocker.value, '$.approval_session_id') = ?
                AND json_extract(blocker.value, '$.tool_request_id') = ?
          )
        LIMIT 2
        """,
        (
            target.user_id,
            target.action_id,
            target.process_id,
            approval_session_id,
            target.tool_request_id,
        ),
    ).fetchall()
    if not predecessor_rows and _action_resume_is_in_progress(
        connection,
        target=target,
    ):
        raise ActionApprovalDecisionError(
            "Another approval continuation is still running",
            error_code="APPROVAL_DECISION_CONFLICT",
        )
    if len(predecessor_rows) != 1:
        raise ActionApprovalDecisionError(
            "Approval pause event does not identify exactly one Action predecessor",
            error_code="APPROVAL_INTERRUPTED",
        )
    predecessor_row = predecessor_rows[0]
    if (
        predecessor_row["event_action_id"] != target.action_id
        or predecessor_row["event_user_id"] != target.user_id
        or predecessor_row["process_kind"] != ACTION_PROCESS_KIND
        or predecessor_row["job_user_id"] != target.user_id
        or predecessor_row["job_type"] != LOCAL_ACTION_JOB_TYPE
        or predecessor_row["logical_key"] != target.action_id
    ):
        raise ActionApprovalDecisionError(
            "Approval predecessor identity or state does not match the requested Action",
            error_code="APPROVAL_INTERRUPTED",
        )
    return _PausedActionPredecessor(
        process_id=str(predecessor_row["process_id"]),
        job_id=str(predecessor_row["job_id"]),
    )


def _action_resume_is_in_progress(
    connection: sqlite3.Connection,
    *,
    target: ActionApprovalTarget,
) -> bool:
    row = connection.execute(
        """
        SELECT 1
        FROM process_events
        JOIN processes ON processes.process_id = process_events.process_id
        JOIN jobs ON jobs.process_id = processes.process_id
        WHERE process_events.event_name = ?
          AND processes.user_id = ?
          AND processes.action_id = ?
          AND processes.process_id = ?
          AND processes.kind = ?
          AND processes.status IN ('enqueued', 'running')
          AND jobs.user_id = ?
          AND jobs.job_type = ?
          AND jobs.logical_key = ?
          AND jobs.status IN ('queued', 'running', 'retryable_error')
        LIMIT 1
        """,
        (
            EVENT_ACTION_RESUME_REQUESTED,
            target.user_id,
            target.action_id,
            target.process_id,
            ACTION_PROCESS_KIND,
            target.user_id,
            LOCAL_ACTION_JOB_TYPE,
            target.action_id,
        ),
    ).fetchone()
    return row is not None


def _resume_paused_action_runtime(
    connection: sqlite3.Connection,
    *,
    predecessor: _PausedActionPredecessor,
    target: ActionApprovalTarget,
    payload_json: str,
    resumed_at: str,
) -> None:
    payload_cursor = connection.execute(
        """
        UPDATE job_payloads
        SET payload_json = ?
        WHERE job_id = ?
        """,
        (payload_json, predecessor.job_id),
    )
    job_cursor = connection.execute(
        """
        UPDATE jobs
        SET
            status = 'queued',
            scheduled_at = ?,
            completed_at = NULL,
            claimed_by = NULL,
            claimed_at = NULL,
            heartbeat_at = NULL,
            error_code = NULL
        WHERE job_id = ? AND process_id = ? AND user_id = ?
          AND job_type = ? AND logical_key = ? AND status = 'paused'
        """,
        (
            resumed_at,
            predecessor.job_id,
            predecessor.process_id,
            target.user_id,
            LOCAL_ACTION_JOB_TYPE,
            target.action_id,
        ),
    )
    process_cursor = connection.execute(
        """
        UPDATE processes
        SET
            status = 'enqueued',
            completed_at = NULL,
            current_job_id = NULL,
            updated_at = ?,
            heartbeat_at = ?
        WHERE process_id = ? AND user_id = ? AND action_id = ?
          AND kind = ? AND status = 'paused'
        """,
        (
            resumed_at,
            resumed_at,
            predecessor.process_id,
            target.user_id,
            target.action_id,
            ACTION_PROCESS_KIND,
        ),
    )
    if (
        int(payload_cursor.rowcount) != 1
        or int(job_cursor.rowcount) != 1
        or int(process_cursor.rowcount) != 1
    ):
        raise ActionApprovalDecisionError(
            "Approval runtime changed before it could be resumed",
            error_code="APPROVAL_INTERRUPTED",
        )


def _load_action_resume_context(
    connection: sqlite3.Connection,
    *,
    target: ActionApprovalTarget,
    approval_session_id: str,
) -> _ActionResumeContext:
    row = connection.execute(
        """
        WITH matching_checkpoint AS (
            SELECT steps.step_number
            FROM agent_action_steps AS steps
            WHERE steps.user_id = ?
              AND steps.action_id = ?
              AND steps.runtime_state_checkpoint IS NOT NULL
              AND (
                (
                  json_extract(
                    steps.runtime_state_checkpoint,
                    '$.pending_approval_request.approval_session_id'
                  ) = ?
                  AND json_extract(
                    steps.runtime_state_checkpoint,
                    '$.pending_approval_request.tool_request_id'
                  ) = ?
                )
                OR EXISTS (
                  SELECT 1
                  FROM json_each(
                    steps.runtime_state_checkpoint,
                    '$.current_approval_blockers'
                  ) AS blocker
                  WHERE json_extract(blocker.value, '$.approval_session_id') = ?
                    AND json_extract(blocker.value, '$.tool_request_id') = ?
                )
              )
            ORDER BY steps.step_number DESC, steps.created_at DESC, steps.step_id DESC
            LIMIT 1
        ),
        owning_user_step AS (
            SELECT steps.user_message_id, steps.user_message_json, steps.created_at
            FROM agent_action_steps AS steps
            JOIN matching_checkpoint
              ON steps.step_number <= matching_checkpoint.step_number
            WHERE steps.user_id = ?
              AND steps.action_id = ?
              AND steps.step_type = 'user_request'
              AND steps.status = 'success'
            ORDER BY steps.step_number DESC, steps.created_at DESC, steps.step_id DESC
            LIMIT 1
        )
        SELECT
            a.suggestion_id,
            owning_user_step.user_message_id,
            owning_user_step.user_message_json,
            owning_user_step.created_at AS user_step_created_at
        FROM agent_actions AS a
        JOIN owning_user_step
        WHERE a.user_id = ?
          AND a.action_id = ?
          AND a.status = 'processing'
        """,
        (
            target.user_id,
            target.action_id,
            approval_session_id,
            target.tool_request_id,
            approval_session_id,
            target.tool_request_id,
            target.user_id,
            target.action_id,
            target.user_id,
            target.action_id,
        ),
    ).fetchone()
    if row is None:
        raise ActionApprovalDecisionError(
            "Approval checkpoint does not match the pending tool request",
            error_code="APPROVAL_INTERRUPTED",
        )
    message_json = row["user_message_json"]
    message_id = row["user_message_id"]
    if not isinstance(message_json, str) or not isinstance(message_id, str):
        raise ActionApprovalDecisionError(
            "Approval checkpoint has no owning typed USER message",
            error_code="APPROVAL_INTERRUPTED",
        )
    user_message = parse_action_user_message(message_json)
    if user_message.message_id != message_id:
        raise ActionApprovalDecisionError(
            "Approval checkpoint USER message identity is inconsistent",
            error_code="APPROVAL_INTERRUPTED",
        )
    approval = user_message.suggestion_approval
    suggestion_id = row["suggestion_id"]
    return _ActionResumeContext(
        suggestion_id=(str(suggestion_id) if suggestion_id is not None else None),
        command_id=message_id,
        accepted_at=(
            approval.approved_at
            if approval is not None
            else str(row["user_step_created_at"])
        ),
    )


def _resolve_pending_approval(
    connection: sqlite3.Connection,
    *,
    target: ActionApprovalTarget,
    session_status: ApprovalSessionDecision,
) -> str:
    row = connection.execute(
        """
        SELECT approval_session_id, status, EXISTS (SELECT 1
                   FROM process_events JOIN processes USING (process_id)
                   WHERE process_events.process_id = ? AND process_events.event_name = ?
                     AND processes.user_id = ? AND processes.action_id = ?
                     AND json_extract(process_events.payload_json, '$.approval_session_id') = ?
                     AND json_extract(process_events.payload_json, '$.approval_tool_request_id') = ?
               ) AS decision_was_applied
        FROM approval_sessions
        WHERE user_id = ? AND tool_request_id = ? AND action_id = ?
        """,
        (
            target.process_id,
            EVENT_ACTION_RESUME_REQUESTED,
            target.user_id,
            target.action_id,
            target.approval_session_id,
            target.tool_request_id,
            target.user_id,
            target.tool_request_id,
            target.action_id,
        ),
    ).fetchone()
    if row is None:
        raise ActionApprovalDecisionError(
            "Approval session not found",
            error_code="APPROVAL_NOT_FOUND",
        )
    approval_session_id = str(row["approval_session_id"] or "").strip()
    if not approval_session_id or target.approval_session_id != approval_session_id:
        raise ActionApprovalDecisionError(
            "Approval request does not match the pending tool request",
            error_code="APPROVAL_REQUEST_MISMATCH",
        )
    approval_status = str(row["status"])
    if approval_status != "pending":
        error_code = "APPROVAL_DECISION_CONFLICT"
        if approval_status == session_status and bool(row["decision_was_applied"]):
            error_code = "APPROVAL_DECISION_ALREADY_APPLIED"
        raise ActionApprovalDecisionError(
            "Approval decision is stale or already applied",
            error_code=error_code,
        )
    return approval_session_id


def _apply_decision(
    connection: sqlite3.Connection,
    *,
    target: ActionApprovalTarget,
    approval_session_id: str,
    session_status: ApprovalSessionDecision,
    decided_at: str,
) -> None:
    result = apply_approval_decision_in_connection(
        connection=connection,
        user_id=target.user_id,
        tool_request_id=target.tool_request_id,
        action_id=target.action_id,
        approval_session_id=approval_session_id,
        expected_current_status="pending",
        next_status=session_status,
        decided_at=decided_at,
    )
    if result.status == "not_found":
        error_code = "APPROVAL_NOT_FOUND"
    elif result.status == "request_mismatch":
        error_code = "APPROVAL_REQUEST_MISMATCH"
    elif result.status == "conflict":
        error_code = "APPROVAL_DECISION_CONFLICT"
    else:
        return
    raise ActionApprovalDecisionError(
        "Approval decision could not be applied",
        error_code=error_code,
    )


__all__ = [
    "ActionApprovalDecisionError",
    "ActionApprovalTarget",
    "ApprovalSessionDecision",
    "resume_paused_action_approval_in_connection",
]
