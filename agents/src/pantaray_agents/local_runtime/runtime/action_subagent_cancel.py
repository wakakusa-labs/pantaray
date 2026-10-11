from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path

from pantaray_agents.tasks.types import ActionSubagentJobPayload

from ..storage.migrations import MigrationError
from ..storage.migrations.connection import configure_connection
from ..storage.transactions import (
    SQLiteTransactionOwnershipError,
    immediate_transaction,
)
from .action_approval_projection import (
    append_pending_approval_snapshot_in_connection,
)
from .action_subagent_ownership import owned_child_sql
from .action_subagent_pause import cancel_paused_action_subagent_in_connection
from .action_subagent_terminal import (
    build_action_subagent_canceled_result,
    finalize_action_subagent_terminal_in_connection,
)
from .job_envelope import (
    LocalJobEnvelopeIntegrityError,
    load_local_job_envelope,
    require_valid_local_job_envelope,
)
from .job_payload_models import (
    parse_action_subagent_job_payload_json,
    serialize_action_subagent_job_payload,
)
from .job_types import LOCAL_ACTION_JOB_TYPE, LOCAL_ACTION_SUBAGENT_JOB_TYPE
from .utc_timestamps import now_utc_iso

_TERMINAL_JOB_STATUSES = frozenset({"completed", "failed", "canceled"})


class ActionSubagentCancelAuthorityError(RuntimeError):
    pass


class ActionSubagentCancelInputError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ActionSubagentCancelRequest:
    user_id: str
    action_id: str
    parent_process_id: str
    parent_job_id: str
    child_process_id: str


@dataclass(frozen=True, slots=True)
class _ChildAuthority:
    payload: ActionSubagentJobPayload
    job_status: str
    process_status: str
    attempt: int
    current_job_id: str | None
    terminal_event_id: str | None
    process_completed_at: str | None
    cancel_requested_at: str | None
    job_completed_at: str | None


def request_action_subagent_cancellation(
    *, db_path: Path, busy_timeout_ms: int, request: ActionSubagentCancelRequest
) -> None:
    if busy_timeout_ms <= 0:
        raise MigrationError("LOCAL_DB_BUSY_TIMEOUT_MS must be a positive integer")
    with sqlite3.connect(db_path) as connection:
        configure_connection(connection, busy_timeout_ms)
        with immediate_transaction(connection):
            request_action_subagent_cancellation_in_connection(
                connection=connection, request=request
            )


def request_action_subagent_cancellation_in_connection(
    *, connection: sqlite3.Connection, request: ActionSubagentCancelRequest
) -> None:
    """Settle one child cancel request inside the caller's write transaction."""

    if not connection.in_transaction:
        raise SQLiteTransactionOwnershipError(
            "Action subagent cancel requires a caller-owned transaction"
        )
    if any(
        not value.strip()
        for value in (
            request.user_id,
            request.action_id,
            request.parent_process_id,
            request.parent_job_id,
            request.child_process_id,
        )
    ):
        raise ActionSubagentCancelInputError("cancel request identity is invalid")

    authority = _require_parent_child_authority(connection, request)
    requested_at = now_utc_iso()
    if authority.job_status in _TERMINAL_JOB_STATUSES:
        finalize_action_subagent_terminal_in_connection(
            connection=connection,
            payload=authority.payload,
            result=build_action_subagent_canceled_result(),
            completed_at=requested_at,
        )
        return
    if authority.job_status == "running":
        _require_running_authority(connection, authority)
        _set_cancel_requested(
            connection, authority=authority, requested_at=requested_at
        )
        return
    released_blocker = authority.job_status == "paused"
    if released_blocker:
        cancel_paused_action_subagent_in_connection(
            connection=connection,
            payload=authority.payload,
            canceled_at=requested_at,
        )
        authority = replace(authority, job_status="queued")
    elif authority.job_status != "queued":
        raise LocalJobEnvelopeIntegrityError(
            "Subagent cancel job status is unsupported"
        )
    _set_cancel_requested(connection, authority=authority, requested_at=requested_at)
    finalize_action_subagent_terminal_in_connection(
        connection=connection,
        payload=authority.payload,
        result=build_action_subagent_canceled_result(),
        completed_at=requested_at,
    )
    if released_blocker:
        append_pending_approval_snapshot_in_connection(
            connection,
            user_id=request.user_id,
            action_id=request.action_id,
            root_process_id=request.parent_process_id,
            created_at=requested_at,
        )


def action_subagent_cancellation_requested(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    payload: ActionSubagentJobPayload,
) -> bool:
    if busy_timeout_ms <= 0:
        raise MigrationError("LOCAL_DB_BUSY_TIMEOUT_MS must be a positive integer")
    with sqlite3.connect(db_path) as connection:
        configure_connection(connection, busy_timeout_ms)
        authority = _load_child_authority(
            connection,
            job_id=payload["job_id"],
            expected_payload=payload,
        )
        _require_running_authority(connection, authority)
        return authority.cancel_requested_at is not None


def _require_parent_child_authority(
    connection: sqlite3.Connection,
    request: ActionSubagentCancelRequest,
) -> _ChildAuthority:
    """Bind one child to the exact nonterminal parent run that owns it.

    The parent proves its authority through the run it still owns, not through a
    live worker: an external Stop also settles the children of a parent that is
    parked on its own approval or waiting to be claimed, and requiring a claimed
    running job there rolled the whole Stop transaction back. Each accepted shape
    pins the job status and the claim column together, so a half-written parent
    transition never passes as a live run.
    """

    row = connection.execute(
        f"""
        SELECT child.process_id,
               {owned_child_sql("parent.process_id")}
               AND child.action_id=parent.action_id
               AND child.user_id=parent.user_id
               AND child.kind='action_subagent' AS owned_child,
               (SELECT COUNT(*) FROM jobs AS child_job
                WHERE child_job.process_id=child.process_id
                  AND child_job.job_type=?),
               (SELECT MIN(child_job.job_id) FROM jobs AS child_job
                WHERE child_job.process_id=child.process_id
                  AND child_job.job_type=?)
        FROM agent_actions AS action
        JOIN processes AS parent ON parent.action_id=action.action_id
          AND parent.user_id=action.user_id
        JOIN jobs AS parent_job ON parent_job.job_id=?
          AND parent_job.process_id=parent.process_id
          AND parent_job.user_id=parent.user_id
        LEFT JOIN processes AS child ON child.process_id=?
        WHERE action.action_id=? AND action.user_id=? AND action.status='processing'
          AND parent.process_id=? AND parent.kind='action'
          AND parent.terminal_event_id IS NULL
          AND parent_job.job_type=? AND parent_job.logical_key=action.action_id
          AND (
            (parent.status='running' AND parent_job.status='running'
             AND parent.current_job_id=parent_job.job_id)
            OR (parent.status='paused' AND parent_job.status='paused'
                AND parent.current_job_id IS NULL)
            OR (parent.status='enqueued' AND parent_job.status='queued'
                AND parent.current_job_id IS NULL)
          )
        """,
        (
            LOCAL_ACTION_SUBAGENT_JOB_TYPE,
            LOCAL_ACTION_SUBAGENT_JOB_TYPE,
            request.parent_job_id,
            request.child_process_id,
            request.action_id,
            request.user_id,
            request.parent_process_id,
            LOCAL_ACTION_JOB_TYPE,
        ),
    ).fetchone()
    if row is None:
        raise ActionSubagentCancelAuthorityError("active parent trace is not available")
    if row[0] is None or row[1] != 1:
        raise ActionSubagentCancelInputError(
            "child_process_id is not owned by the active parent"
        )
    if int(row[2]) != 1 or row[3] is None:
        raise LocalJobEnvelopeIntegrityError(
            "Subagent cancel child job inventory is invalid"
        )
    return _load_child_authority(
        connection,
        job_id=str(row[3]),
        expected_payload=None,
    )


def _load_child_authority(
    connection: sqlite3.Connection,
    *,
    job_id: str,
    expected_payload: ActionSubagentJobPayload | None,
) -> _ChildAuthority:
    envelope = load_local_job_envelope(connection=connection, job_id=job_id)
    if envelope is None:
        raise LocalJobEnvelopeIntegrityError("Subagent cancel job does not exist")
    require_valid_local_job_envelope(
        envelope,
        expected_job_type=LOCAL_ACTION_SUBAGENT_JOB_TYPE,
        expected_user_id=(
            expected_payload["user_id"] if expected_payload is not None else None
        ),
    )
    assert envelope.payload_json is not None
    payload = parse_action_subagent_job_payload_json(envelope.payload_json)
    if (
        envelope.job_id != payload["job_id"]
        or envelope.job_user_id != payload["user_id"]
        or envelope.process_id != payload["process_id"]
        or envelope.logical_key != payload["job_id"]
        or envelope.payload_json != serialize_action_subagent_job_payload(payload)
        or (expected_payload is not None and payload != expected_payload)
    ):
        raise LocalJobEnvelopeIntegrityError("Subagent cancel payload is invalid")
    row = connection.execute(
        """
        SELECT process.action_id,process.parent_process_id,
               process.current_job_id,process.terminal_event_id,
               process.completed_at,job.cancel_requested_at,job.completed_at
        FROM jobs AS job JOIN processes AS process
          ON process.process_id=job.process_id
        WHERE job.job_id=?
        """,
        (job_id,),
    ).fetchone()
    if row is None or (
        row[0] != payload["action_id"] or row[1] != payload["parent_process_id"]
    ):
        raise LocalJobEnvelopeIntegrityError("Subagent cancel authority is invalid")
    return _ChildAuthority(
        payload=payload,
        job_status=envelope.job_status,
        process_status=str(envelope.process_status),
        attempt=envelope.attempt,
        current_job_id=str(row[2]) if row[2] is not None else None,
        terminal_event_id=str(row[3]) if row[3] is not None else None,
        process_completed_at=str(row[4]) if row[4] is not None else None,
        cancel_requested_at=str(row[5]) if row[5] is not None else None,
        job_completed_at=str(row[6]) if row[6] is not None else None,
    )


def _require_running_authority(
    connection: sqlite3.Connection,
    authority: _ChildAuthority,
) -> None:
    attempt = connection.execute(
        "SELECT status FROM job_attempts WHERE job_id=? AND attempt_number=?",
        (authority.payload["job_id"], authority.attempt),
    ).fetchone()
    if (
        authority.job_status != "running"
        or authority.process_status != "running"
        or authority.current_job_id != authority.payload["job_id"]
        or authority.terminal_event_id is not None
        or authority.process_completed_at is not None
        or authority.job_completed_at is not None
        or authority.attempt < 1
        or attempt is None
        or tuple(attempt) != ("running",)
    ):
        raise LocalJobEnvelopeIntegrityError(
            "Running subagent cancel authority is invalid"
        )


def _set_cancel_requested(
    connection: sqlite3.Connection,
    *,
    authority: _ChildAuthority,
    requested_at: str,
) -> None:
    if authority.cancel_requested_at is not None:
        return
    cursor = connection.execute(
        "UPDATE jobs SET cancel_requested_at=? "
        "WHERE job_id=? AND status=? AND cancel_requested_at IS NULL",
        (requested_at, authority.payload["job_id"], authority.job_status),
    )
    if int(cursor.rowcount) != 1:
        raise LocalJobEnvelopeIntegrityError(
            "Subagent cancel request changed during update"
        )
