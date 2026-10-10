"""Canonical Action-owned cancellation transaction.

An external Stop settles in two phases. The first transaction records the
durable Stop fence on the parent job and applies the canonical single-child
cancel core to every child the parent still owns. Queued and approval-paused
children settle inside that transaction, so an Action without a live child
still reaches its canceled terminal in one transaction. A running child keeps
its claims until its own worker reaches a safe boundary, so the fence and the
child cancel requests are committed without the parent terminal, and the
parent's own worker writes the terminal once the barrier clears: the fence
makes that late write canceled regardless of what the worker computed.

A parent parked on its own approval, or re-queued by the in-flight repair after
a worker restart, still owns those children but has no worker of its own. This
Stop fences it and settles every child it can reach; when a running child
outlives the transaction, the parent terminal follows from whichever
transaction next owns that run — a repeated Stop once the child settles, or the
approval decision that resumes the parked run into the fence. The parked
approval therefore stays pending, because interrupting it here would close the
only convergence the user can drive from the client.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from pantaray_agents.action_status import (
    ACTION_STATUS_CANCELED,
    ACTION_STATUS_PROCESSING,
    JOB_STATUS_QUEUED,
    PROCESS_STATUS_ENQUEUED,
    ActionTerminalStatus,
    build_finalize_action_terminal_command,
    derive_action_terminal_failure,
    is_action_terminal_status,
)
from pantaray_agents.local_runtime.storage.migrations import MigrationError
from pantaray_agents.local_runtime.storage.migrations.connection import (
    configure_connection,
)
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction
from pantaray_agents.tasks.action_user_message import (
    parse_action_user_message,
    render_action_user_request_text,
)

from .action_message_process_fence import (
    resolve_action_process_lineage_in_connection,
)
from .action_subagent_parent_lifecycle import (
    settle_action_subagent_children_in_connection,
)
from .action_terminal_repository import (
    finalize_action_job_terminal_in_connection,
)
from .action_user_adoption import (
    load_pending_action_user_steps_in_connection,
    mark_pending_action_user_steps_not_executed_in_connection,
)
from .job_payload_models import parse_action_job_payload_json

_ACTIVE_ACTION_JOB_STATUSES = (
    "queued",
    "running",
    "paused",
    "retryable_error",
)
_ACTIVE_ACTION_PROCESS_STATUSES = ("enqueued", "running", "paused")


class ActionCancelNotFoundError(MigrationError):
    """The requested Action does not belong to the caller."""


class ActionCancelStateConflictError(MigrationError):
    """The requested Action cannot be canceled from its current state."""


@dataclass(frozen=True, slots=True)
class ActionCancelResult:
    """Outcome of one Stop transaction.

    ``action_status`` is ``None`` when the Stop fence is durable but a running
    child still holds mutation authority, so the Action stays ``processing``
    until the barrier clears.
    """

    changed: bool
    action_status: ActionTerminalStatus | None
    suggestion_id: str | None
    process_id: str | None
    execution_session_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _ActiveActionRuntime:
    job_id: str
    process_id: str


@dataclass(frozen=True, slots=True)
class ActionCancelRepository:
    db_path: Path
    busy_timeout_ms: int

    async def cancel_processing_action(
        self,
        *,
        user_id: str,
        action_id: str,
        expected_process_id: str | None = None,
        completed_at: str,
        process_completed_event_id: str,
    ) -> ActionCancelResult:
        """Cancel the Action's current runtime turn in one transaction."""

        if self.busy_timeout_ms <= 0:
            raise MigrationError("LOCAL_DB_BUSY_TIMEOUT_MS must be a positive integer")
        if not user_id.strip() or not action_id.strip():
            raise ValueError("user_id and action_id must not be empty")

        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            configure_connection(connection, self.busy_timeout_ms)
            with immediate_transaction(connection):
                action_row = connection.execute(
                    """
                    SELECT user_id, suggestion_id, status
                    FROM agent_actions
                    WHERE action_id = ?
                    """,
                    (action_id,),
                ).fetchone()
                if action_row is None or str(action_row["user_id"]) != user_id:
                    raise ActionCancelNotFoundError("Action not found")

                action_status = str(action_row["status"]).strip().lower()
                raw_suggestion_id = action_row["suggestion_id"]
                suggestion_id = (
                    str(raw_suggestion_id) if raw_suggestion_id is not None else None
                )
                if is_action_terminal_status(action_status):
                    execution_session_ids = (
                        _load_cleanup_session_ids(
                            connection=connection,
                            user_id=user_id,
                            action_id=action_id,
                        )
                        if action_status == ACTION_STATUS_CANCELED
                        else ()
                    )
                    return ActionCancelResult(
                        changed=False,
                        action_status=action_status,
                        suggestion_id=suggestion_id,
                        process_id=None,
                        execution_session_ids=execution_session_ids,
                    )
                if action_status not in {"queued", ACTION_STATUS_PROCESSING}:
                    raise ActionCancelStateConflictError(
                        f"Action cannot be canceled while status={action_status!r}"
                    )

                runtime = _load_active_action_runtime(
                    connection=connection,
                    user_id=user_id,
                    action_id=action_id,
                    suggestion_id=suggestion_id,
                    queued=action_status == "queued",
                )
                if (
                    expected_process_id is not None
                    and runtime.process_id != expected_process_id
                ):
                    raise ActionCancelStateConflictError(
                        "Action active process does not match the cancellation fence"
                    )
                lineage = resolve_action_process_lineage_in_connection(
                    connection=connection,
                    user_id=user_id,
                    action_id=action_id,
                    process_id=runtime.process_id,
                )
                command_id, accepted_at = load_current_action_user_turn_in_connection(
                    connection=connection,
                    user_id=user_id,
                    action_id=action_id,
                    suggestion_id=suggestion_id,
                    root_process_id=lineage.root_process_id,
                )
                pending_steps = load_pending_action_user_steps_in_connection(
                    connection=connection,
                    user_id=user_id,
                    action_id=action_id,
                    expected_process_id=lineage.root_process_id,
                )
                if action_status == "queued":
                    cursor = connection.execute(
                        """
                        UPDATE agent_actions
                        SET status = 'processing', updated_at = ?
                        WHERE action_id = ? AND user_id = ? AND status = 'queued'
                        """,
                        (completed_at, action_id, user_id),
                    )
                    if cursor.rowcount != 1:
                        raise MigrationError(
                            "Queued Action cancel lost its active state"
                        )
                failure = derive_action_terminal_failure(ACTION_STATUS_CANCELED)
                if failure is None:
                    raise MigrationError("Action cancel failure reason is missing")
                command = build_finalize_action_terminal_command(
                    process_completed_event_id=process_completed_event_id,
                    suggestion_id=suggestion_id,
                    user_id=user_id,
                    command_id=command_id,
                    process_id=runtime.process_id,
                    action_id=action_id,
                    accepted_at=accepted_at,
                    completed_at=completed_at,
                    action_status=ACTION_STATUS_CANCELED,
                    failure_code=failure.failure_code,
                    failure_stage=failure.failure_stage,
                    failure_message_public=failure.failure_message_public,
                )
                _record_action_stop_fence(
                    connection=connection,
                    job_id=runtime.job_id,
                    requested_at=completed_at,
                )
                mark_pending_action_user_steps_not_executed_in_connection(
                    connection=connection,
                    user_id=user_id,
                    action_id=action_id,
                    expected_process_id=lineage.root_process_id,
                    canceled_at=completed_at,
                    pending_steps=pending_steps,
                )
                if not settle_action_subagent_children_in_connection(
                    connection=connection,
                    command=command,
                    job_id=runtime.job_id,
                ):
                    # A running child still derives its broker authority from
                    # the root execution session, so this Stop leaves the root
                    # runtime intact and commits only the fence and the child
                    # cancel requests.
                    return ActionCancelResult(
                        changed=True,
                        action_status=None,
                        suggestion_id=suggestion_id,
                        process_id=runtime.process_id,
                        execution_session_ids=(),
                    )
                _cancel_running_execution_sessions(
                    connection=connection,
                    user_id=user_id,
                    action_id=action_id,
                    completed_at=completed_at,
                )
                execution_session_ids = _load_cleanup_session_ids(
                    connection=connection,
                    user_id=user_id,
                    action_id=action_id,
                )
                # The terminal settles every row this Action still owns: the
                # unique active-process index leaves no second Action process
                # to sweep, and a subagent child is owned by the child terminal
                # transaction that also releases its claims.
                result = finalize_action_job_terminal_in_connection(
                    connection=connection,
                    command=command,
                    job_id=runtime.job_id,
                )
                return ActionCancelResult(
                    changed=True,
                    action_status=result.action_status,
                    suggestion_id=suggestion_id,
                    process_id=runtime.process_id,
                    execution_session_ids=execution_session_ids,
                )


def action_stop_fence_is_recorded(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    action_id: str,
    job_id: str,
) -> bool:
    """Report whether an external Stop already fenced this Action job."""

    if busy_timeout_ms <= 0:
        raise MigrationError("LOCAL_DB_BUSY_TIMEOUT_MS must be a positive integer")
    with sqlite3.connect(db_path) as connection:
        configure_connection(connection, busy_timeout_ms)
        row = connection.execute(
            """
            SELECT 1
            FROM jobs
            WHERE job_id = ? AND user_id = ? AND job_type = 'execute_action'
              AND logical_key = ? AND cancel_requested_at IS NOT NULL
            """,
            (job_id, user_id, action_id),
        ).fetchone()
    return row is not None


def _record_action_stop_fence(
    *,
    connection: sqlite3.Connection,
    job_id: str,
    requested_at: str,
) -> None:
    """Mark the parent job canceled-by-request without terminalizing it.

    The fence rejects later spawns, tells the running worker to stop, and turns
    whatever terminal that worker computes into ``canceled``. A repeated Stop
    keeps the first request time.
    """

    connection.execute(
        """
        UPDATE jobs
        SET cancel_requested_at = ?
        WHERE job_id = ? AND cancel_requested_at IS NULL
        """,
        (requested_at, job_id),
    )


def _load_cleanup_session_ids(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    action_id: str,
) -> tuple[str, ...]:
    rows = connection.execute(
        """
        WITH RECURSIVE action_sessions(
            execution_session_id, user_id, action_id, parent_execution_session_id,
            status, started_at, depth
        ) AS (
            SELECT execution_session_id, user_id, action_id,
                   parent_execution_session_id, status, started_at, 0
            FROM execution_sessions
            WHERE user_id = ? AND action_id = ?
              AND parent_execution_session_id IS NULL
            UNION ALL
            SELECT child.execution_session_id, child.user_id, child.action_id,
                   child.parent_execution_session_id, child.status,
                   child.started_at, parent.depth + 1
            FROM execution_sessions AS child
            JOIN action_sessions AS parent
              ON child.parent_execution_session_id = parent.execution_session_id
            WHERE child.user_id = ? AND child.action_id = ?
        )
        SELECT session.execution_session_id
        FROM action_sessions AS session
        WHERE session.status IN ('completed', 'failed', 'canceled', 'expired')
          AND (
              EXISTS (
                  SELECT 1
                  FROM tool_runtime_resources AS resource
                  WHERE resource.execution_session_id = session.execution_session_id
                    AND resource.status IN ('active', 'cleanup_failed', 'abandoned')
              )
              OR EXISTS (
                  SELECT 1
                  FROM tool_invocations AS invocation
                  WHERE invocation.user_id = session.user_id
                    AND invocation.action_id = session.action_id
                    AND invocation.execution_session_id = session.execution_session_id
                    AND invocation.status IN ('queued', 'running')
              )
          )
        ORDER BY session.depth DESC, session.started_at, session.execution_session_id
        """,
        (user_id, action_id, user_id, action_id),
    ).fetchall()
    return tuple(str(row["execution_session_id"]) for row in rows)


def _cancel_running_execution_sessions(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    action_id: str,
    completed_at: str,
) -> None:
    connection.execute(
        """
        UPDATE execution_sessions
        SET status = 'canceled', completed_at = ?
        WHERE user_id = ? AND action_id = ? AND status = 'running'
        """,
        (completed_at, user_id, action_id),
    )


def _load_active_action_runtime(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    action_id: str,
    suggestion_id: str | None,
    queued: bool,
) -> _ActiveActionRuntime:
    job_placeholders = ",".join("?" for _ in _ACTIVE_ACTION_JOB_STATUSES)
    process_placeholders = ",".join("?" for _ in _ACTIVE_ACTION_PROCESS_STATUSES)
    rows = connection.execute(
        f"""
        SELECT
            jobs.job_id,
            jobs.process_id,
            jobs.scheduled_at,
            jobs.status AS job_status,
            payloads.payload_json,
            processes.suggestion_id AS process_suggestion_id,
            processes.status AS process_status
        FROM jobs
        JOIN job_payloads AS payloads ON payloads.job_id = jobs.job_id
        JOIN processes ON processes.process_id = jobs.process_id
        WHERE jobs.user_id = ?
          AND jobs.job_type = 'execute_action'
          AND jobs.logical_key = ?
          AND jobs.status IN ({job_placeholders})
          AND processes.user_id = ?
          AND processes.kind = 'action'
          AND processes.action_id = ?
          AND processes.status IN ({process_placeholders})
        ORDER BY jobs.scheduled_at DESC, jobs.job_id DESC
        """,
        (
            user_id,
            action_id,
            *_ACTIVE_ACTION_JOB_STATUSES,
            user_id,
            action_id,
            *_ACTIVE_ACTION_PROCESS_STATUSES,
        ),
    ).fetchall()
    if not rows:
        raise ActionCancelStateConflictError(
            "Active Action has no cancelable runtime job"
        )
    if queued and (
        len(rows) != 1
        or rows[0]["job_status"] != JOB_STATUS_QUEUED
        or rows[0]["process_status"] != PROCESS_STATUS_ENQUEUED
    ):
        raise ActionCancelStateConflictError(
            "Queued Action runtime is no longer awaiting worker claim"
        )
    row = rows[0]
    process_id = str(row["process_id"] or "").strip()
    job_id = str(row["job_id"] or "").strip()
    if not process_id or not job_id:
        raise MigrationError("Active Action runtime identity is incomplete")
    process_suggestion_id = row["process_suggestion_id"]
    normalized_process_suggestion_id = (
        str(process_suggestion_id) if process_suggestion_id is not None else None
    )
    if normalized_process_suggestion_id != suggestion_id:
        raise MigrationError("Action process suggestion provenance is inconsistent")
    payload = parse_action_job_payload_json(str(row["payload_json"]))
    if (
        payload["job_id"] != job_id
        or payload["process_id"] != process_id
        or payload["action_id"] != action_id
        or payload["user_id"] != user_id
    ):
        raise MigrationError("Active Action job payload identity is inconsistent")
    return _ActiveActionRuntime(job_id=job_id, process_id=process_id)


def load_current_action_user_turn_in_connection(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    action_id: str,
    suggestion_id: str | None,
    root_process_id: str,
) -> tuple[str, str]:
    row = connection.execute(
        """
        SELECT
            actions.initial_user_message_id,
            steps.user_message_id,
            steps.user_message_json,
            steps.user_request_text,
            steps.created_at
        FROM agent_actions AS actions
        JOIN agent_action_steps AS steps ON steps.action_id = actions.action_id
        WHERE actions.user_id = ?
          AND actions.action_id = ?
          AND steps.user_id = ?
          AND steps.step_type = 'user_request'
          AND steps.status = 'success'
          AND steps.step_number IS NOT NULL
          AND steps.adopted_process_id = ?
        ORDER BY steps.step_number DESC, steps.created_at DESC, steps.step_id DESC
        LIMIT 1
        """,
        (user_id, action_id, user_id, root_process_id),
    ).fetchone()
    if row is None:
        raise MigrationError("Processing Action has no durable USER turn")
    message_id = row["user_message_id"]
    message_json = row["user_message_json"]
    if not isinstance(message_id, str) or not isinstance(message_json, str):
        raise MigrationError("Current Action USER turn is incomplete")
    message = parse_action_user_message(message_json)
    if message.message_id != message_id:
        raise MigrationError("Current Action USER message identity is inconsistent")
    if row["user_request_text"] != render_action_user_request_text(message):
        raise MigrationError("Current Action USER rendered text is inconsistent")
    approval = message.suggestion_approval
    is_initial_turn = row["initial_user_message_id"] == message_id
    if suggestion_id is not None and is_initial_turn and approval is None:
        raise MigrationError(
            "Suggestion-linked initial USER turn lacks approval metadata"
        )
    if approval is not None:
        if not is_initial_turn or approval.suggestion_id != suggestion_id:
            raise MigrationError("Action USER suggestion provenance is inconsistent")
        accepted_at = approval.approved_at
    else:
        accepted_at = str(row["created_at"])
    return message_id, accepted_at


__all__ = [
    "ActionCancelNotFoundError",
    "ActionCancelRepository",
    "ActionCancelResult",
    "ActionCancelStateConflictError",
    "action_stop_fence_is_recorded",
    "load_current_action_user_turn_in_connection",
]
