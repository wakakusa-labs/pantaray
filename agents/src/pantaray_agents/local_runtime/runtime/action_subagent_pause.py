from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tasks.types import ActionSubagentJobPayload

from ..storage.migrations.connection import configure_connection
from ..storage.transactions import (
    SQLiteTransactionOwnershipError,
    immediate_transaction,
)
from ..tooling.repository import apply_approval_decision_in_connection
from .action_approval_projection import (
    append_pending_approval_snapshot_in_connection,
)
from .action_subagent_terminal import (
    ActionSubagentTerminalResult,
    build_action_subagent_canceled_result,
    finalize_action_subagent_terminal_in_connection,
)
from .job_envelope import LocalJobEnvelopeIntegrityError
from .job_types import LOCAL_ACTION_SUBAGENT_JOB_TYPE
from .process_events import append_process_event_in_connection
from .utc_timestamps import now_utc_iso

ACTION_SUBAGENT_PAUSE_EVENT = "process_paused"
_CHILD_PROCESS_KIND = "action_subagent"
_APPROVAL_STATUS_PENDING = "pending"
_APPROVAL_STATUS_INTERRUPTED = "interrupted"


class ActionSubagentApprovalPause(BaseException):  # noqa: N818
    """One gated child Tool call waiting for a user approval decision.

    A pause is control flow, not a Tool failure. Deriving from ``BaseException``
    keeps the shared native ReAct runner from recording a Tool error step that
    the child would read back as a real result on its next turn.
    """

    def __init__(
        self,
        *,
        tool_id: str,
        arguments: dict[str, JSONValue],
        tool_request_id: str,
        approval_session_id: str,
        call_id: str,
        intent_class: str,
        command_summary: dict[str, JSONValue],
    ) -> None:
        super().__init__(tool_request_id)
        self.tool_id = tool_id
        self.arguments = arguments
        self.tool_request_id = tool_request_id
        self.approval_session_id = approval_session_id
        self.call_id = call_id
        self.intent_class = intent_class
        self.command_summary = command_summary


@dataclass(frozen=True, slots=True)
class PausedChildAnchor:
    job_id: str
    parent_process_id: str
    anchor: dict[str, JSONValue]


def next_action_subagent_tool_request_id(
    *, db_path: Path, busy_timeout_ms: int, process_id: str, call_id: str
) -> str:
    """Return the child's next durable, never-reused Tool request identity.

    ``<process id>:<next event seq>:<call id>``: the sequence orders it against
    the child's history rows, and the call id names the call a restart answers.
    """

    with sqlite3.connect(db_path) as connection:
        configure_connection(connection, busy_timeout_ms)
        row = connection.execute(
            "SELECT next_event_seq FROM processes WHERE process_id=?",
            (process_id,),
        ).fetchone()
    if row is None:
        raise LocalJobEnvelopeIntegrityError("Subagent Tool request process is missing")
    return f"{process_id}:{int(row[0])}:{call_id}"


def pause_action_subagent_for_approval(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    payload: ActionSubagentJobPayload,
    pause: ActionSubagentApprovalPause,
) -> ActionSubagentTerminalResult | None:
    """Park the running child on its approval anchor, or let a cancel win."""

    paused_at = now_utc_iso()
    with sqlite3.connect(db_path) as connection:
        configure_connection(connection, busy_timeout_ms)
        with immediate_transaction(connection):
            row = connection.execute(
                "SELECT cancel_requested_at FROM jobs WHERE job_id=? AND user_id=?",
                (payload["job_id"], payload["user_id"]),
            ).fetchone()
            if row is None:
                raise LocalJobEnvelopeIntegrityError("Subagent pause job is missing")
            if row[0] is not None:
                _interrupt_approval_session(
                    connection,
                    user_id=payload["user_id"],
                    action_id=payload["action_id"],
                    approval_session_id=pause.approval_session_id,
                    tool_request_id=pause.tool_request_id,
                    interrupted_at=paused_at,
                )
                return finalize_action_subagent_terminal_in_connection(
                    connection=connection,
                    payload=payload,
                    result=build_action_subagent_canceled_result(),
                    completed_at=paused_at,
                )
            _park_running_child(connection, payload=payload, paused_at=paused_at)
            append_process_event_in_connection(
                connection=connection,
                process_id=payload["process_id"],
                event_name=ACTION_SUBAGENT_PAUSE_EVENT,
                payload=_pause_anchor_payload(payload, pause, paused_at),
                created_at=paused_at,
            )
            append_pending_approval_snapshot_in_connection(
                connection,
                user_id=payload["user_id"],
                action_id=payload["action_id"],
                root_process_id=payload["parent_process_id"],
                created_at=paused_at,
            )
    return None


def cancel_paused_action_subagent_in_connection(
    *,
    connection: sqlite3.Connection,
    payload: ActionSubagentJobPayload,
    canceled_at: str,
) -> None:
    """Release one paused child's own blocker inside the cancel transaction.

    The child returns to the queued shape the terminal transaction settles.
    """

    if not connection.in_transaction:
        raise SQLiteTransactionOwnershipError(
            "Paused Action subagent cancel requires a caller-owned transaction"
        )
    paused = load_paused_child(
        connection,
        user_id=payload["user_id"],
        action_id=payload["action_id"],
        child_process_id=payload["process_id"],
    )
    if paused is None:
        raise LocalJobEnvelopeIntegrityError("Paused subagent cancel anchor is missing")
    blocker = anchor_blocker(paused.anchor)
    _interrupt_approval_session(
        connection,
        user_id=payload["user_id"],
        action_id=payload["action_id"],
        approval_session_id=str(blocker["approval_session_id"]),
        tool_request_id=str(blocker["tool_request_id"]),
        interrupted_at=canceled_at,
    )
    requeue_paused_child(
        connection,
        job_id=paused.job_id,
        user_id=payload["user_id"],
        process_id=payload["process_id"],
        resumed_at=canceled_at,
    )


def _pause_anchor_payload(
    payload: ActionSubagentJobPayload,
    pause: ActionSubagentApprovalPause,
    paused_at: str,
) -> dict[str, object]:
    return {
        "action_id": payload["action_id"],
        "user_id": payload["user_id"],
        "status": "processing",
        "completed_at": paused_at,
        "reason": "approval_pending",
        "approval_blockers": [
            {
                "action_id": payload["action_id"],
                "approval_session_id": pause.approval_session_id,
                "tool_request_id": pause.tool_request_id,
                "tool_id": pause.tool_id,
                "intent_class": pause.intent_class,
                "command_summary": pause.command_summary,
            }
        ],
        "tool_arguments": pause.arguments,
        "call_id": pause.call_id,
    }


def _park_running_child(
    connection: sqlite3.Connection,
    *,
    payload: ActionSubagentJobPayload,
    paused_at: str,
) -> None:
    job, user, process = (
        payload["job_id"],
        payload["user_id"],
        payload["process_id"],
    )
    _update_one(
        connection,
        "UPDATE jobs SET status='paused',claimed_by=NULL,claimed_at=NULL,"
        "heartbeat_at=?,completed_at=NULL,error_code=NULL "
        "WHERE job_id=? AND user_id=? AND job_type=? AND process_id=? "
        "AND status='running' AND cancel_requested_at IS NULL",
        (paused_at, job, user, LOCAL_ACTION_SUBAGENT_JOB_TYPE, process),
        "job",
    )
    _update_one(
        connection,
        "UPDATE processes SET status='paused',completed_at=NULL,"
        "current_job_id=NULL,updated_at=?,heartbeat_at=? "
        "WHERE process_id=? AND user_id=? AND kind=? AND action_id=? "
        "AND parent_process_id=? AND status='running' AND current_job_id=? "
        "AND terminal_event_id IS NULL",
        (
            paused_at,
            paused_at,
            process,
            user,
            _CHILD_PROCESS_KIND,
            payload["action_id"],
            payload["parent_process_id"],
            job,
        ),
        "process",
    )
    _update_one(
        connection,
        "UPDATE job_attempts SET status='completed',completed_at=?,"
        "error_code=NULL,error_message=NULL WHERE job_id=? AND status='running'",
        (paused_at, job),
        "attempt",
    )


def requeue_paused_child(
    connection: sqlite3.Connection,
    *,
    job_id: str,
    user_id: str,
    process_id: str,
    resumed_at: str,
) -> None:
    _update_one(
        connection,
        "UPDATE jobs SET status='queued',scheduled_at=?,completed_at=NULL,"
        "claimed_by=NULL,claimed_at=NULL,heartbeat_at=NULL,error_code=NULL "
        "WHERE job_id=? AND user_id=? AND job_type=? AND process_id=? "
        "AND status='paused'",
        (resumed_at, job_id, user_id, LOCAL_ACTION_SUBAGENT_JOB_TYPE, process_id),
        "job",
    )
    _update_one(
        connection,
        "UPDATE processes SET status='enqueued',completed_at=NULL,"
        "current_job_id=NULL,updated_at=?,heartbeat_at=? "
        "WHERE process_id=? AND user_id=? AND kind=? AND status='paused'",
        (resumed_at, resumed_at, process_id, user_id, _CHILD_PROCESS_KIND),
        "process",
    )


def load_paused_child(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    action_id: str,
    child_process_id: str,
) -> PausedChildAnchor | None:
    row = connection.execute(
        """
        SELECT job.job_id,process.parent_process_id,paused.payload_json
        FROM processes AS process
        JOIN jobs AS job ON job.process_id=process.process_id
          AND job.user_id=process.user_id AND job.job_type=?
        JOIN process_events AS paused ON paused.process_id=process.process_id
          AND paused.event_name=?
        WHERE process.process_id=? AND process.user_id=? AND process.action_id=?
          AND process.kind=? AND process.status='paused'
          AND process.current_job_id IS NULL AND process.terminal_event_id IS NULL
          AND job.status='paused'
          AND paused.event_seq=(
              SELECT MAX(latest.event_seq) FROM process_events AS latest
              WHERE latest.process_id=process.process_id AND latest.event_name=?)
        """,
        (
            LOCAL_ACTION_SUBAGENT_JOB_TYPE,
            ACTION_SUBAGENT_PAUSE_EVENT,
            child_process_id,
            user_id,
            action_id,
            _CHILD_PROCESS_KIND,
            ACTION_SUBAGENT_PAUSE_EVENT,
        ),
    ).fetchall()
    if not row:
        return None
    if len(row) != 1:
        raise LocalJobEnvelopeIntegrityError("Paused subagent job inventory is invalid")
    return PausedChildAnchor(
        job_id=str(row[0][0]),
        parent_process_id=str(row[0][1]),
        anchor=anchor_object(row[0][2]),
    )


def _interrupt_approval_session(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    action_id: str,
    approval_session_id: str,
    tool_request_id: str,
    interrupted_at: str,
) -> None:
    status = decide_approval_session(
        connection,
        user_id=user_id,
        action_id=action_id,
        approval_session_id=approval_session_id,
        tool_request_id=tool_request_id,
        next_status=_APPROVAL_STATUS_INTERRUPTED,
        decided_at=interrupted_at,
    )
    if status != "updated":
        raise LocalJobEnvelopeIntegrityError(
            "Subagent approval session could not be interrupted"
        )


def decide_approval_session(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    action_id: str,
    approval_session_id: str,
    tool_request_id: str,
    next_status: str,
    decided_at: str,
) -> str:
    # The shared decision writer reads sessions by column name, so the caller's
    # connection keeps whatever row factory it already uses.
    previous_row_factory = connection.row_factory
    connection.row_factory = sqlite3.Row
    try:
        return apply_approval_decision_in_connection(
            connection=connection,
            user_id=user_id,
            tool_request_id=tool_request_id,
            action_id=action_id,
            approval_session_id=approval_session_id,
            expected_current_status=_APPROVAL_STATUS_PENDING,
            next_status=next_status,
            decided_at=decided_at,
        ).status
    finally:
        connection.row_factory = previous_row_factory


def anchor_blocker(anchor: dict[str, JSONValue]) -> dict[str, JSONValue]:
    blockers = anchor.get("approval_blockers")
    if not isinstance(blockers, list) or len(blockers) != 1:
        raise LocalJobEnvelopeIntegrityError(
            "Subagent pause anchor requires exactly one blocker"
        )
    blocker = blockers[0]
    if not isinstance(blocker, dict) or not all(
        isinstance(blocker.get(field), str) and blocker.get(field)
        for field in ("approval_session_id", "tool_request_id", "tool_id")
    ):
        raise LocalJobEnvelopeIntegrityError("Subagent pause anchor blocker is invalid")
    return cast(dict[str, JSONValue], blocker)


def anchor_object(raw: object) -> dict[str, JSONValue]:
    value = json.loads(raw) if isinstance(raw, str) else None
    if not isinstance(value, dict):
        raise LocalJobEnvelopeIntegrityError("Subagent pause anchor must be an object")
    return cast(dict[str, JSONValue], value)


def _update_one(
    connection: sqlite3.Connection,
    sql: str,
    params: tuple[object, ...],
    row_name: str,
) -> None:
    if int(connection.execute(sql, params).rowcount) != 1:
        raise LocalJobEnvelopeIntegrityError(
            f"Subagent approval pause {row_name} changed during the transition"
        )
