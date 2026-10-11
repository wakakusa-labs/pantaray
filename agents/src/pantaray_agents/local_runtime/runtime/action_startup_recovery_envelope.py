from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal, cast

from pantaray_agents.local_runtime.storage.migrations import MigrationError
from pantaray_agents.tasks.types import ActionContinuationRef

from .action_checkpoint_retention import load_tool_approval_resume_anchor_in_connection
from .action_message_process_fence import (
    ActionLogicalRunLineage,
    resolve_action_process_lineage_in_connection,
)
from .job_payload_models import parse_action_job_payload_json

type ActionStartupRecoveryActionStatus = Literal["queued", "processing"]


@dataclass(frozen=True, slots=True)
class ActionStartupRecoveryAnchor:
    step_id: str
    step_number: int


@dataclass(frozen=True, slots=True)
class ActionStartupRecoveryEnvelope:
    job_id: str
    process_id: str
    user_id: str
    action_id: str
    action_status: ActionStartupRecoveryActionStatus
    attempt: int
    attempt_id: str
    attempt_started_at: str
    claimed_by: str
    claimed_at: str
    job_heartbeat_at: str
    process_updated_at: str
    process_heartbeat_at: str
    payload_json: str
    lineage: ActionLogicalRunLineage
    anchor: ActionStartupRecoveryAnchor


@dataclass(frozen=True, slots=True)
class InterruptedActionJob:
    job_id: str
    user_id: str
    action_id: str


def list_interrupted_action_jobs_in_connection(
    *, connection: sqlite3.Connection
) -> tuple[InterruptedActionJob, ...]:
    """List the running Action jobs a dead worker left, without binding them."""

    rows = connection.execute(
        "SELECT job_id, user_id, logical_key FROM jobs "
        "WHERE job_type = 'execute_action' AND status = 'running' "
        "ORDER BY scheduled_at, job_id"
    ).fetchall()
    return tuple(
        InterruptedActionJob(
            job_id=str(row[0]), user_id=str(row[1]), action_id=str(row[2])
        )
        for row in rows
    )


def load_action_startup_recovery_envelope_in_connection(
    *, connection: sqlite3.Connection, job_id: str
) -> ActionStartupRecoveryEnvelope:
    """Bind one interrupted Action envelope in the caller's read snapshot."""

    row = connection.execute(
        """
        SELECT job.user_id AS job_user_id, job.job_type, job.process_id,
               job.status AS job_status, job.logical_key, job.attempt,
               job.claimed_by, job.claimed_at,
               job.heartbeat_at AS job_heartbeat_at, payload.payload_json,
               process.user_id AS process_user_id, process.kind AS process_kind,
               process.status AS process_status, process.action_id,
               process.current_job_id, process.suggestion_id AS process_suggestion_id,
               process.updated_at AS process_updated_at,
               process.heartbeat_at AS process_heartbeat_at,
               action.user_id AS action_user_id, action.status AS action_status,
               action.suggestion_id AS action_suggestion_id,
               attempt.attempt_id, attempt.started_at AS attempt_started_at,
               attempt.status AS attempt_status, attempt.completed_at,
               attempt.error_code, attempt.error_message,
               (SELECT COUNT(*) FROM job_attempts AS running
                WHERE running.job_id = job.job_id AND running.status = 'running')
                    AS running_attempt_count
        FROM jobs AS job
        LEFT JOIN job_payloads AS payload ON payload.job_id = job.job_id
        LEFT JOIN processes AS process ON process.process_id = job.process_id
        LEFT JOIN agent_actions AS action ON action.action_id = process.action_id
        LEFT JOIN job_attempts AS attempt ON attempt.job_id = job.job_id
          AND attempt.attempt_number = job.attempt
        WHERE job.job_id = ?
        """,
        (job_id,),
    ).fetchone()
    if row is None:
        raise MigrationError("running Action job disappeared during recovery binding")
    payload_json = _text(row["payload_json"], field="payload")
    payload = parse_action_job_payload_json(payload_json)
    expected = {
        "job_user_id": payload["user_id"],
        "job_type": "execute_action",
        "process_id": payload["process_id"],
        "job_status": "running",
        "logical_key": payload["action_id"],
        "process_user_id": payload["user_id"],
        "process_kind": "action",
        "process_status": "running",
        "action_id": payload["action_id"],
        "current_job_id": job_id,
        "action_user_id": payload["user_id"],
        "attempt_status": "running",
        "running_attempt_count": 1,
    }
    attempt = row["attempt"]
    required = (
        "claimed_by",
        "claimed_at",
        "job_heartbeat_at",
        "process_updated_at",
        "process_heartbeat_at",
        "attempt_id",
        "attempt_started_at",
    )
    action_status = row["action_status"]
    if (
        payload["job_id"] != job_id
        or any(row[field] != value for field, value in expected.items())
        or row["process_suggestion_id"] != row["action_suggestion_id"]
        or action_status not in {"queued", "processing"}
        or any(
            row[field] is not None
            for field in ("completed_at", "error_code", "error_message")
        )
        or isinstance(attempt, bool)
        or not isinstance(attempt, int)
        or attempt < 1
        or any(
            not isinstance(row[field], str) or not row[field].strip()
            for field in required
        )
    ):
        raise MigrationError("interrupted Action job envelope is inconsistent")
    lineage = resolve_action_process_lineage_in_connection(
        connection=connection,
        user_id=payload["user_id"],
        action_id=payload["action_id"],
        process_id=payload["process_id"],
    )
    if lineage.job_id != job_id:
        raise MigrationError("interrupted Action lineage changed physical job")
    return ActionStartupRecoveryEnvelope(
        job_id=job_id,
        process_id=payload["process_id"],
        user_id=payload["user_id"],
        action_id=payload["action_id"],
        action_status=cast(ActionStartupRecoveryActionStatus, action_status),
        attempt=attempt,
        attempt_id=str(row["attempt_id"]),
        attempt_started_at=str(row["attempt_started_at"]),
        claimed_by=str(row["claimed_by"]),
        claimed_at=str(row["claimed_at"]),
        job_heartbeat_at=str(row["job_heartbeat_at"]),
        process_updated_at=str(row["process_updated_at"]),
        process_heartbeat_at=str(row["process_heartbeat_at"]),
        payload_json=payload_json,
        lineage=lineage,
        anchor=_load_anchor(
            connection=connection,
            user_id=payload["user_id"],
            action_id=payload["action_id"],
            continuation=payload["continuation_ref"],
            lineage=lineage,
        ),
    )


def _load_anchor(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    action_id: str,
    continuation: ActionContinuationRef,
    lineage: ActionLogicalRunLineage,
) -> ActionStartupRecoveryAnchor:
    if continuation["kind"] == "user_step":
        row = connection.execute(
            """SELECT step_id, step_number FROM agent_action_steps
               WHERE step_id = ? AND user_id = ? AND action_id = ?
                 AND step_type = 'user_request' AND status = 'success'
                 AND accepted_sequence = ? AND adopted_process_id = ?""",
            (
                continuation["user_step_id"],
                user_id,
                action_id,
                lineage.root_accepted_sequence,
                lineage.root_process_id,
            ),
        ).fetchone()
    else:
        anchor = load_tool_approval_resume_anchor_in_connection(
            connection,
            user_id=user_id,
            action_id=action_id,
            approval_session_id=continuation["approval_session_id"],
            tool_request_id=continuation["tool_request_id"],
        )
        if anchor is None:
            raise MigrationError("interrupted Action recovery anchor was not found")
        return ActionStartupRecoveryAnchor(
            step_id=anchor.step_id, step_number=anchor.step_number
        )
    if row is None:
        raise MigrationError("interrupted Action recovery anchor was not found")
    step_id = _text(row["step_id"], field="anchor step_id")
    step_number = row["step_number"]
    if isinstance(step_number, bool) or not isinstance(step_number, int):
        raise MigrationError("interrupted Action recovery anchor is invalid")
    return ActionStartupRecoveryAnchor(step_id=step_id, step_number=step_number)


def _text(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MigrationError(f"interrupted Action {field} is incomplete")
    return value


__all__ = [
    "ActionStartupRecoveryActionStatus",
    "ActionStartupRecoveryAnchor",
    "ActionStartupRecoveryEnvelope",
    "InterruptedActionJob",
    "list_interrupted_action_jobs_in_connection",
    "load_action_startup_recovery_envelope_in_connection",
]
