"""End an interrupted Action that startup recovery cannot resume.

The Action ends as an error the user can see and the rest of startup proceeds.
Nothing is re-queued, so no side effect of the interrupted run can repeat, and
its waiting USER messages are marked not executed, as Stop does. A run whose
Action cannot even be finalized is quarantined so no worker claims it again.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from pantaray_agents.action_status import (
    ACTION_STATUS_ERROR,
    build_finalize_action_terminal_command,
    derive_action_terminal_failure,
)
from pantaray_agents.local_runtime.storage.migrations import MigrationError
from pantaray_agents.local_runtime.storage.migrations.connection import (
    configure_connection,
)
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction

from .action_cancel_repository import load_current_action_user_turn_in_connection
from .action_message_process_fence import (
    resolve_action_process_lineage_in_connection,
)
from .action_startup_recovery_envelope import InterruptedActionJob
from .action_subagent_parent_lifecycle import (
    settle_action_subagent_children_in_connection,
)
from .action_terminal_repository import finalize_action_job_terminal_in_connection
from .action_user_adoption import (
    load_pending_action_user_steps_in_connection,
    mark_pending_action_user_steps_not_executed_in_connection,
)
from .job_claim import block_invalid_local_job
from .utc_timestamps import now_utc_iso

logger = logging.getLogger(__name__)


def fail_unrecoverable_interrupted_action(
    *, db_path: Path, busy_timeout_ms: int, job: InterruptedActionJob
) -> None:
    try:
        with sqlite3.connect(db_path) as connection:
            connection.row_factory = sqlite3.Row
            configure_connection(connection, busy_timeout_ms)
            with immediate_transaction(connection):
                _finalize_as_error_in_connection(
                    connection, job=job, completed_at=now_utc_iso()
                )
    except MigrationError:
        logger.exception(
            "Interrupted Action cannot be ended either; blocking its job: "
            "job_id=%s action_id=%s",
            job.job_id,
            job.action_id,
        )
        with sqlite3.connect(db_path) as connection:
            configure_connection(connection, busy_timeout_ms)
            with immediate_transaction(connection):
                block_invalid_local_job(connection=connection, job_id=job.job_id)


def _finalize_as_error_in_connection(
    connection: sqlite3.Connection, *, job: InterruptedActionJob, completed_at: str
) -> None:
    job_id, user_id, action_id = job.job_id, job.user_id, job.action_id
    row = connection.execute(
        """SELECT job.process_id, action.suggestion_id, action.status
           FROM jobs AS job
           JOIN agent_actions AS action ON action.action_id = job.logical_key
           WHERE job.job_id = ? AND job.user_id = ? AND job.logical_key = ?
             AND job.job_type = 'execute_action' AND job.status = 'running'
             AND action.user_id = ?
             AND action.status IN ('queued', 'processing')""",
        (job_id, user_id, action_id, user_id),
    ).fetchone()
    if row is None:
        raise MigrationError("unrecoverable Action run is no longer running")
    process_id = str(row["process_id"])
    # The terminal projection settles a processing Action only.
    if row["status"] == "queued":
        connection.execute(
            "UPDATE agent_actions SET status = 'processing', updated_at = ? "
            "WHERE action_id = ? AND user_id = ? AND status = 'queued'",
            (completed_at, action_id, user_id),
        )
    suggestion_id = row["suggestion_id"]
    lineage = resolve_action_process_lineage_in_connection(
        connection=connection,
        user_id=user_id,
        action_id=action_id,
        process_id=process_id,
    )
    command_id, accepted_at = load_current_action_user_turn_in_connection(
        connection=connection,
        user_id=user_id,
        action_id=action_id,
        suggestion_id=suggestion_id,
        root_process_id=lineage.root_process_id,
    )
    failure = derive_action_terminal_failure(ACTION_STATUS_ERROR)
    assert failure is not None
    command = build_finalize_action_terminal_command(
        process_completed_event_id=f"{job_id}:startup-recovery-failed",
        suggestion_id=suggestion_id,
        user_id=user_id,
        command_id=command_id,
        process_id=process_id,
        action_id=action_id,
        accepted_at=accepted_at,
        completed_at=completed_at,
        action_status=ACTION_STATUS_ERROR,
        failure_code=failure.failure_code,
        failure_stage=failure.failure_stage,
        failure_message_public=failure.failure_message_public,
    )
    if not settle_action_subagent_children_in_connection(
        connection=connection, command=command, job_id=job_id
    ):
        raise MigrationError("unrecoverable Action still owns a running child")
    mark_pending_action_user_steps_not_executed_in_connection(
        connection=connection,
        user_id=user_id,
        action_id=action_id,
        expected_process_id=lineage.root_process_id,
        canceled_at=completed_at,
        pending_steps=load_pending_action_user_steps_in_connection(
            connection=connection,
            user_id=user_id,
            action_id=action_id,
            expected_process_id=lineage.root_process_id,
        ),
    )
    connection.execute(
        "UPDATE execution_sessions SET status = 'failed', completed_at = ? "
        "WHERE user_id = ? AND action_id = ? AND status = 'running'",
        (completed_at, user_id, action_id),
    )
    finalize_action_job_terminal_in_connection(
        connection=connection, command=command, job_id=job_id
    )


__all__ = ["fail_unrecoverable_interrupted_action"]
