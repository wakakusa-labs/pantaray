from __future__ import annotations

import sqlite3
from pathlib import Path

from pantaray_agents.agents.action_agent.runtime.checkpoint import (
    RUNTIME_STATE_CHECKPOINT_VERSION,
)
from pantaray_agents.agents.action_agent.runtime.models.execution_context import (
    EXECUTION_CONTEXT_STATE_FIELDS,
)
from pantaray_agents.schema.tool_result import build_runtime_tool_error_output

from ..descriptor_access import DescriptorPathError
from ..storage.migrations import MigrationError
from ..storage.transactions import immediate_transaction
from ..tooling.models import StoredToolRuntimeResource
from ..tooling.repository import complete_execution_session
from ..tooling.resources.action_session_temp_authority import (
    ActionSessionTempCleanupReceipt,
    load_action_session_temp_cleanup_receipt,
)
from ..tooling.resources.action_session_temp_cleanup import (
    DescriptorSafeRemovalUnavailableError,
    remove_action_session_temp_leaf,
)
from ..tooling.resources.cancel_cleanup import (
    cleanup_action_runtime_resources,
)
from ..tooling.resources.resource_cleanup import resource_cleanup_attempts_exhausted
from ..tooling.resources.resource_db_support import configure_connection
from ..tooling.resources.resource_recovery import STARTUP_RECOVERY_ERROR_TYPE
from ..tooling.resources.resource_store import load_tool_runtime_resource
from ..tooling.resources.resource_transition_store import (
    ToolRuntimeResourceEventInput,
    ToolRuntimeResourceTransitionStatus,
    persist_tool_runtime_resource_transition_in_connection,
)
from ..tooling.tool_result_finalization import (
    InvocationToolResultOwner,
    ToolResultFinalizationRequest,
    finalize_local_tool_result,
)
from .action_startup_recovery_authority import (
    ActionStartupRecoveryAuthority,
    load_action_startup_recovery_authority_in_connection,
)
from .action_startup_recovery_envelope import (
    ActionStartupRecoveryEnvelope,
    list_action_startup_recovery_envelopes_in_connection,
)
from .action_subagent_startup_recovery import ActionRootIdentity
from .utc_timestamps import now_utc_iso

_RECOVERY_ERROR_MESSAGE = "Worker restart re-queued the in-flight job"
_TOOL_FAILURE_MESSAGE = "tool invocation was interrupted during startup recovery"


def recover_interrupted_action_runs_for_startup(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    preserved_roots: frozenset[ActionRootIdentity] = frozenset(),
) -> int:
    """Clean and re-queue each interrupted Action no subagent child still owns.

    A preserved root keeps its manifest, session, temp root and checkpoint
    execution context; the generic in-flight repair re-queues its job instead.
    """

    recovered_count = 0
    for envelope, authority in _load_recovery_snapshots(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
    ):
        if (envelope.user_id, envelope.action_id) in preserved_roots:
            continue
        if authority is None or authority.pending_approval_outcome == "retain":
            continue
        if authority.session_status == "running":
            complete_execution_session(
                db_path=db_path,
                busy_timeout_ms=busy_timeout_ms,
                execution_session_id=authority.context.execution_session_id,
                status="expired",
                completed_at=now_utc_iso(),
            )
            authority = _reload_exact_authority(
                db_path=db_path,
                busy_timeout_ms=busy_timeout_ms,
                envelope=envelope,
            )
        _settle_session_children(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            authority=authority,
        )
        receipt = load_action_session_temp_cleanup_receipt(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            user_id=envelope.user_id,
            action_id=envelope.action_id,
            execution_session_id=authority.context.execution_session_id,
        )
        if receipt is None:
            raise MigrationError(
                "interrupted Action session has no terminal cleanup authority"
            )
        _require_receipt_matches_authority(receipt=receipt, authority=authority)
        resource = load_tool_runtime_resource(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            resource_id=receipt.root_resource_id,
        )
        try:
            remove_action_session_temp_leaf(resource=resource, receipt=receipt)
        except (
            OSError,
            DescriptorPathError,
            DescriptorSafeRemovalUnavailableError,
        ) as exc:
            _persist_root_cleanup_failure(
                db_path=db_path,
                busy_timeout_ms=busy_timeout_ms,
                authority=authority,
                resource=resource,
                cleanup_error=str(exc) or type(exc).__name__,
            )
            raise MigrationError(
                "interrupted Action session temp cleanup failed"
            ) from exc
        _persist_requeue(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            authority=authority,
            resource=resource,
        )
        recovered_count += 1
    return recovered_count


def _load_recovery_snapshots(
    *, db_path: Path, busy_timeout_ms: int
) -> tuple[
    tuple[ActionStartupRecoveryEnvelope, ActionStartupRecoveryAuthority | None], ...
]:
    database_uri = f"{db_path.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(database_uri, uri=True) as connection:
        configure_connection(connection, busy_timeout_ms)
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        envelopes = list_action_startup_recovery_envelopes_in_connection(
            connection=connection
        )
        return tuple(
            (
                envelope,
                load_action_startup_recovery_authority_in_connection(
                    connection=connection,
                    db_path=db_path,
                    envelope=envelope,
                ),
            )
            for envelope in envelopes
        )


def _reload_exact_authority(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    envelope: ActionStartupRecoveryEnvelope,
) -> ActionStartupRecoveryAuthority:
    snapshots = _load_recovery_snapshots(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
    )
    matches = [item for item in snapshots if item[0].job_id == envelope.job_id]
    if len(matches) != 1 or matches[0][0] != envelope or matches[0][1] is None:
        raise MigrationError("interrupted Action recovery authority changed")
    return matches[0][1]


def _settle_session_children(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    authority: ActionStartupRecoveryAuthority,
) -> None:
    envelope = authority.envelope
    result = cleanup_action_runtime_resources(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        action_id=envelope.action_id,
        execution_session_id=authority.context.execution_session_id,
    )
    if result.failure_count:
        raise MigrationError("interrupted Action child resource cleanup failed")
    completed_at = now_utc_iso()
    database_uri = f"{db_path.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(database_uri, uri=True) as connection:
        configure_connection(connection, busy_timeout_ms)
        rows = connection.execute(
            """SELECT invocation_id FROM tool_invocations
               WHERE execution_session_id=? AND user_id=? AND action_id=?
                 AND status IN ('queued','running')
               ORDER BY started_at, invocation_id""",
            (
                authority.context.execution_session_id,
                envelope.user_id,
                envelope.action_id,
            ),
        ).fetchall()
    for row in rows:
        finalize_local_tool_result(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            request=ToolResultFinalizationRequest(
                owner=InvocationToolResultOwner(
                    invocation_id=str(row["invocation_id"]),
                    completed_at=completed_at,
                    status="failed",
                    completion_scope="invocation",
                ),
                output=build_runtime_tool_error_output(
                    error_type=STARTUP_RECOVERY_ERROR_TYPE,
                    message=_TOOL_FAILURE_MESSAGE,
                ),
            ),
        )


def _require_receipt_matches_authority(
    *,
    receipt: ActionSessionTempCleanupReceipt,
    authority: ActionStartupRecoveryAuthority,
) -> None:
    envelope = authority.envelope
    if (
        receipt.user_id != envelope.user_id
        or receipt.action_id != envelope.action_id
        or receipt.execution_session_id != authority.context.execution_session_id
        or receipt.session_status != authority.session_status
        or receipt.canonical_leaf != Path(authority.context.action_temp_dir)
        or receipt.manifest_id != authority.context.manifest_id
        or receipt.root_resource_id != authority.root_resource_id
        or receipt.root_resource_status != authority.root_resource_status
        or receipt.root_resource_updated_at != authority.root_resource_updated_at
        or receipt.root_cleanup_attempts != authority.root_cleanup_attempts
    ):
        raise MigrationError("interrupted Action cleanup receipt changed")


def _persist_requeue(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    authority: ActionStartupRecoveryAuthority,
    resource: StoredToolRuntimeResource,
) -> None:
    timestamp = now_utc_iso()
    with sqlite3.connect(db_path) as connection:
        configure_connection(connection, busy_timeout_ms)
        with immediate_transaction(connection):
            _require_unchanged_authority(
                connection=connection,
                db_path=db_path,
                authority=authority,
            )
            transition = persist_tool_runtime_resource_transition_in_connection(
                connection=connection,
                resource=resource,
                status="cleaned",
                timestamp=timestamp,
                cleanup_error=None,
                event=ToolRuntimeResourceEventInput(
                    event_type="action_session_temp_cleaned",
                    message="session temp cleanup completed",
                    tool_invocation_id=None,
                ),
            )
            if not transition.applied:
                raise MigrationError("interrupted Action root cleanup CAS failed")
            _remove_checkpoint_execution_context(
                connection=connection,
                authority=authority,
            )
            _requeue_job_in_connection(
                connection=connection,
                authority=authority,
                timestamp=timestamp,
            )


def _persist_root_cleanup_failure(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    authority: ActionStartupRecoveryAuthority,
    resource: StoredToolRuntimeResource,
    cleanup_error: str,
) -> None:
    timestamp = now_utc_iso()
    abandoned = resource_cleanup_attempts_exhausted(resource)
    status: ToolRuntimeResourceTransitionStatus = (
        "abandoned" if abandoned else "cleanup_failed"
    )
    message = (
        f"session temp cleanup abandoned after retry budget: {cleanup_error}"
        if abandoned
        else f"session temp cleanup failed: {cleanup_error}"
    )
    with sqlite3.connect(db_path) as connection:
        configure_connection(connection, busy_timeout_ms)
        with immediate_transaction(connection):
            _require_unchanged_authority(
                connection=connection,
                db_path=db_path,
                authority=authority,
            )
            transition = persist_tool_runtime_resource_transition_in_connection(
                connection=connection,
                resource=resource,
                status=status,
                timestamp=timestamp,
                cleanup_error=cleanup_error,
                event=ToolRuntimeResourceEventInput(
                    event_type=f"action_session_temp_{status}",
                    message=message,
                    tool_invocation_id=None,
                ),
            )
            if not transition.applied:
                raise MigrationError(
                    "interrupted Action root cleanup failure CAS failed"
                )


def _require_unchanged_authority(
    *,
    connection: sqlite3.Connection,
    db_path: Path,
    authority: ActionStartupRecoveryAuthority,
) -> None:
    envelopes = list_action_startup_recovery_envelopes_in_connection(
        connection=connection
    )
    matches = [item for item in envelopes if item.job_id == authority.envelope.job_id]
    if len(matches) != 1 or matches[0] != authority.envelope:
        raise MigrationError("interrupted Action recovery envelope changed")
    current = load_action_startup_recovery_authority_in_connection(
        connection=connection,
        db_path=db_path,
        envelope=matches[0],
    )
    if current != authority:
        raise MigrationError("interrupted Action recovery authority changed")


def _remove_checkpoint_execution_context(
    *,
    connection: sqlite3.Connection,
    authority: ActionStartupRecoveryAuthority,
) -> None:
    checkpoint = authority.checkpoint
    if checkpoint.raw_json is None:
        return
    assert checkpoint.step_id is not None
    assert checkpoint.step_number is not None
    json_paths = tuple(f"$.{field}" for field in EXECUTION_CONTEXT_STATE_FIELDS)
    placeholders = ", ".join("?" for _ in json_paths)
    cursor = connection.execute(
        f"""UPDATE agent_action_steps
            SET runtime_state_checkpoint=json_remove(runtime_state_checkpoint,{placeholders})
            WHERE step_id = ? AND step_number = ? AND user_id = ? AND action_id = ?
              AND runtime_state_checkpoint = ?
              AND runtime_state_checkpoint_version = ?""",
        (
            *json_paths,
            checkpoint.step_id,
            checkpoint.step_number,
            authority.envelope.user_id,
            authority.envelope.action_id,
            checkpoint.raw_json,
            RUNTIME_STATE_CHECKPOINT_VERSION,
        ),
    )
    if cursor.rowcount != 1:
        raise MigrationError("interrupted Action checkpoint CAS failed")


def _requeue_job_in_connection(
    *,
    connection: sqlite3.Connection,
    authority: ActionStartupRecoveryAuthority,
    timestamp: str,
) -> None:
    envelope = authority.envelope
    attempt = connection.execute(
        """UPDATE job_attempts
           SET status='failed', completed_at=?, error_code='RECOVERY_REQUEUED',
               error_message=?
           WHERE attempt_id=? AND job_id=? AND status='running'""",
        (
            timestamp,
            _RECOVERY_ERROR_MESSAGE,
            envelope.attempt_id,
            envelope.job_id,
        ),
    )
    process = connection.execute(
        """UPDATE processes
           SET status='enqueued', current_job_id=NULL, heartbeat_at=?, updated_at=?
           WHERE process_id=? AND user_id=? AND kind='action' AND status='running'
             AND action_id=? AND current_job_id=?""",
        (
            timestamp,
            timestamp,
            envelope.process_id,
            envelope.user_id,
            envelope.action_id,
            envelope.job_id,
        ),
    )
    job = connection.execute(
        """UPDATE jobs
           SET status='queued', claimed_by=NULL, claimed_at=NULL, heartbeat_at=NULL
           WHERE job_id=? AND user_id=? AND job_type='execute_action'
             AND process_id=? AND logical_key=? AND status='running'""",
        (
            envelope.job_id,
            envelope.user_id,
            envelope.process_id,
            envelope.action_id,
        ),
    )
    if (attempt.rowcount, process.rowcount, job.rowcount) != (1, 1, 1):
        raise MigrationError("interrupted Action job requeue CAS failed")


__all__ = ["recover_interrupted_action_runs_for_startup"]
