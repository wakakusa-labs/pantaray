from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from pydantic import ValidationError

from pantaray_agents.agents.action_agent.runtime.checkpoint import (
    RUNTIME_STATE_CHECKPOINT_VERSION,
)
from pantaray_agents.agents.action_agent.runtime.models.execution_context import (
    EXECUTION_CONTEXT_STATE_FIELDS,
    ExecutionContextModel,
)
from pantaray_agents.local_runtime.storage.migrations import MigrationError
from pantaray_agents.local_runtime.tooling.action_session_temp_paths import (
    resolve_action_session_temp_leaf,
    resolve_action_storage_paths,
)

from .action_startup_recovery_envelope import ActionStartupRecoveryEnvelope
from .utc_timestamps import parse_utc_iso

type ActionStartupRecoverySessionStatus = Literal[
    "running", "expired", "completed", "failed"
]
type ActionStartupRecoveryRootStatus = Literal["active", "cleanup_failed"]
type PendingApprovalRecoveryOutcome = Literal["none", "retain"]


@dataclass(frozen=True, slots=True)
class ActionStartupRecoveryCheckpoint:
    step_id: str | None
    step_number: int | None
    raw_json: str | None


@dataclass(frozen=True, slots=True)
class ActionStartupRecoveryAuthority:
    envelope: ActionStartupRecoveryEnvelope
    scratch_root_path: str
    session_status: ActionStartupRecoverySessionStatus
    session_completed_at: str | None
    context: ExecutionContextModel
    root_resource_id: str
    root_resource_status: ActionStartupRecoveryRootStatus
    root_resource_updated_at: str
    root_cleanup_attempts: int
    checkpoint: ActionStartupRecoveryCheckpoint
    pending_approval_outcome: PendingApprovalRecoveryOutcome


def load_action_startup_recovery_authority_in_connection(
    *,
    connection: sqlite3.Connection,
    db_path: Path,
    envelope: ActionStartupRecoveryEnvelope,
) -> ActionStartupRecoveryAuthority | None:
    """Bind resource authority in the envelope's caller-owned snapshot."""

    if envelope.action_status == "queued":
        return None
    rows: list[sqlite3.Row] = connection.execute(
        """
        SELECT manifest.manifest_id, manifest.user_id AS manifest_user_id,
               manifest.execution_session_id,
               manifest.scratch_root_path, manifest.status AS manifest_status,
               session.user_id AS session_user_id, session.action_id AS session_action_id,
               session.parent_execution_session_id, session.status AS session_status,
               session.started_at AS session_started_at,
               session.completed_at AS session_completed_at, session.cwd_path,
               session.action_temp_dir, session.app_runtime_python,
               session.network_policy, session.read_access_scope,
               root.resource_id, root.execution_session_id AS root_session_id,
               root.action_id AS root_action_id, root.status AS root_status,
               root.pid, root.pgid, root.process_start_signature,
               root.resource_path, root.lock_id, root.updated_at AS root_updated_at,
               root.cleaned_at, root.cleanup_attempts
        FROM workspace_manifests AS manifest
        LEFT JOIN execution_sessions AS session
          ON session.execution_session_id = manifest.execution_session_id
        LEFT JOIN tool_runtime_resources AS root
          ON root.execution_session_id = session.execution_session_id
         AND root.resource_kind = 'temp_dir' AND root.action_id IS NOT NULL
         AND root.tool_invocation_id IS NULL
        WHERE manifest.action_id = ?
        ORDER BY manifest.manifest_id, root.resource_id
        """,
        (envelope.action_id,),
    ).fetchall()
    if not rows:
        return None
    if len(rows) != 1:
        raise MigrationError("interrupted Action manifest or root intent is not unique")
    row = rows[0]
    session_id = _text(row["execution_session_id"], field="session id")
    paths = resolve_action_storage_paths(
        db_path=db_path,
        user_id=envelope.user_id,
        action_id=envelope.action_id,
    )
    canonical_leaf = resolve_action_session_temp_leaf(
        paths=paths,
        execution_session_id=session_id,
    )
    expected = {
        "manifest_user_id": envelope.user_id,
        "manifest_status": "ready",
        "scratch_root_path": str(paths.workspace),
        "session_user_id": envelope.user_id,
        "session_action_id": envelope.action_id,
        "cwd_path": str(paths.workspace),
        "action_temp_dir": str(canonical_leaf),
        "root_session_id": session_id,
        "root_action_id": envelope.action_id,
        "resource_path": str(canonical_leaf),
    }
    root_status = row["root_status"]
    session_status = row["session_status"]
    session_started_at = row["session_started_at"]
    completed_at = row["session_completed_at"]
    cleanup_attempts = row["cleanup_attempts"]
    if (
        any(row[field] != value for field, value in expected.items())
        or row["parent_execution_session_id"] is not None
        or session_status
        not in {"running", "expired", "completed", "failed", "canceled"}
        or (session_status == "running") != (completed_at is None)
        or not isinstance(session_started_at, str)
        or not session_started_at.strip()
        or (
            completed_at is not None
            and (not isinstance(completed_at, str) or not completed_at.strip())
        )
    ):
        raise MigrationError("interrupted Action session authority is inconsistent")
    try:
        context = ExecutionContextModel.model_validate(
            {
                "manifest_id": row["manifest_id"],
                "execution_session_id": session_id,
                "execution_network_policy": row["network_policy"],
                "action_temp_dir": str(canonical_leaf),
                "app_runtime_python": row["app_runtime_python"],
                "read_access_scope": row["read_access_scope"],
            }
        )
    except ValidationError as exc:
        raise MigrationError("interrupted Action execution context is invalid") from exc
    checkpoint, checkpoint_context = _load_checkpoint(
        connection=connection,
        envelope=envelope,
    )
    if checkpoint_context is not None and checkpoint_context != context:
        raise MigrationError("checkpoint execution context is stale or mismatched")
    if not _is_current_run_session(
        envelope=envelope,
        checkpoint_context=checkpoint_context,
        session_status=str(session_status),
        session_started_at=session_started_at,
        session_completed_at=completed_at,
    ):
        return None
    if (
        session_status not in {"running", "expired", "completed", "failed"}
        or root_status not in {"active", "cleanup_failed"}
        or any(
            row[field] is not None
            for field in (
                "pid",
                "pgid",
                "process_start_signature",
                "lock_id",
                "cleaned_at",
            )
        )
        or isinstance(cleanup_attempts, bool)
        or not isinstance(cleanup_attempts, int)
        or cleanup_attempts < 0
    ):
        raise MigrationError(
            "interrupted Action root cleanup authority is inconsistent"
        )
    return ActionStartupRecoveryAuthority(
        envelope=envelope,
        scratch_root_path=_text(row["scratch_root_path"], field="scratch root"),
        session_status=cast(ActionStartupRecoverySessionStatus, session_status),
        session_completed_at=completed_at,
        context=context,
        root_resource_id=_text(row["resource_id"], field="root resource id"),
        root_resource_status=cast(ActionStartupRecoveryRootStatus, root_status),
        root_resource_updated_at=_text(row["root_updated_at"], field="root update"),
        root_cleanup_attempts=cleanup_attempts,
        checkpoint=checkpoint,
        pending_approval_outcome=_load_pending_outcome(
            connection=connection,
            envelope=envelope,
            checkpoint_context=checkpoint_context,
            checkpoint=checkpoint,
        ),
    )


def _load_checkpoint(
    *,
    connection: sqlite3.Connection,
    envelope: ActionStartupRecoveryEnvelope,
) -> tuple[ActionStartupRecoveryCheckpoint, ExecutionContextModel | None]:
    row = connection.execute(
        """SELECT step_id, step_number, runtime_state_checkpoint,
                  runtime_state_checkpoint_version
           FROM agent_action_steps
           WHERE user_id = ? AND action_id = ? AND step_number >= ?
             AND runtime_state_checkpoint IS NOT NULL
           ORDER BY step_number DESC, completed_at IS NULL ASC,
                    completed_at DESC, created_at DESC, step_id DESC LIMIT 1""",
        (envelope.user_id, envelope.action_id, envelope.anchor.step_number),
    ).fetchone()
    if row is None:
        return ActionStartupRecoveryCheckpoint(None, None, None), None
    if row["runtime_state_checkpoint_version"] != RUNTIME_STATE_CHECKPOINT_VERSION:
        raise MigrationError("checkpoint version is not current")
    raw_json = _text(row["runtime_state_checkpoint"], field="checkpoint")
    try:
        decoded = json.loads(raw_json)
        if not isinstance(decoded, dict):
            raise ValueError("checkpoint must be an object")
        context = ExecutionContextModel.from_optional_values(
            **{field: decoded.get(field) for field in EXECUTION_CONTEXT_STATE_FIELDS}
        )
    except (TypeError, ValueError, ValidationError) as exc:
        raise MigrationError(
            "checkpoint execution context is partial or invalid"
        ) from exc
    step_number = row["step_number"]
    if isinstance(step_number, bool) or not isinstance(step_number, int):
        raise MigrationError("checkpoint row identity is invalid")
    return (
        ActionStartupRecoveryCheckpoint(
            step_id=_text(row["step_id"], field="checkpoint step id"),
            step_number=step_number,
            raw_json=raw_json,
        ),
        context,
    )


def _is_current_run_session(
    *,
    envelope: ActionStartupRecoveryEnvelope,
    checkpoint_context: ExecutionContextModel | None,
    session_status: str,
    session_started_at: str,
    session_completed_at: str | None,
) -> bool:
    # A previous recovery strips the context, so any continuation, approval
    # included, binds the session by attempt ownership until the next checkpoint.
    if checkpoint_context is not None:
        return True
    attempt_started = parse_utc_iso(envelope.attempt_started_at)
    session_started = parse_utc_iso(session_started_at)
    session_completed = (
        parse_utc_iso(session_completed_at)
        if session_completed_at is not None
        else None
    )
    if session_completed is not None and session_completed < session_started:
        raise MigrationError("Action session completion precedes its start")
    if session_started >= attempt_started:
        if session_status in {"running", "expired", "completed", "failed"}:
            return True
        raise MigrationError("current Action session lifecycle is inconsistent")
    if session_status in {"expired", "completed", "failed", "canceled"}:
        assert session_completed is not None
        if session_completed < attempt_started:
            return False
    raise MigrationError("manifest session is not bound to the interrupted attempt")


def _load_pending_outcome(
    *,
    connection: sqlite3.Connection,
    envelope: ActionStartupRecoveryEnvelope,
    checkpoint_context: ExecutionContextModel | None,
    checkpoint: ActionStartupRecoveryCheckpoint,
) -> PendingApprovalRecoveryOutcome:
    pending = int(
        connection.execute(
            "SELECT COUNT(*) FROM approval_sessions "
            "WHERE user_id = ? AND action_id = ? AND status = 'pending'",
            (envelope.user_id, envelope.action_id),
        ).fetchone()[0]
    )
    if pending == 0:
        return "none"
    if checkpoint.raw_json is None or checkpoint_context is None:
        raise MigrationError("pending approval has no bound current-run checkpoint")
    matched = int(
        connection.execute(
            """
            SELECT COUNT(*) FROM approval_sessions AS approval
            WHERE approval.user_id = ? AND approval.action_id = ?
              AND approval.manifest_id = ? AND approval.status = 'pending'
              AND ((approval.approval_session_id = json_extract(?,
                    '$.pending_approval_request.approval_session_id')
                AND approval.tool_request_id = json_extract(?,
                    '$.pending_approval_request.tool_request_id'))
                OR EXISTS (SELECT 1 FROM json_each(?,
                    '$.current_approval_blockers') AS blocker
                    WHERE json_extract(blocker.value, '$.approval_session_id')
                            = approval.approval_session_id
                      AND json_extract(blocker.value, '$.tool_request_id')
                            = approval.tool_request_id))
            """,
            (
                envelope.user_id,
                envelope.action_id,
                checkpoint_context.manifest_id,
                checkpoint.raw_json,
                checkpoint.raw_json,
                checkpoint.raw_json,
            ),
        ).fetchone()[0]
    )
    if matched != pending:
        raise MigrationError("pending approval is not bound to the current checkpoint")
    return "retain"


def _text(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MigrationError(f"interrupted Action {field} is incomplete")
    return value


__all__ = [
    "ActionStartupRecoveryAuthority",
    "ActionStartupRecoveryCheckpoint",
    "load_action_startup_recovery_authority_in_connection",
]
