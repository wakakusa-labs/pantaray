"""Adopt durable pending USER turns into one canonical Action run."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

from pantaray_agents.agents.action_agent.runtime.checkpoint import (
    RUNTIME_STATE_CHECKPOINT_VERSION,
    build_runtime_state_checkpoint,
)
from pantaray_agents.agents.action_agent.runtime.handlers.nodes.user_request import (
    project_persisted_user_request_step,
)
from pantaray_agents.agents.action_agent.runtime.state import ActionAgentState
from pantaray_agents.agents.action_agent.runtime.user_image_attachments import (
    build_user_image_attachments,
)
from pantaray_agents.local_runtime.storage.migrations import MigrationError
from pantaray_agents.local_runtime.storage.migrations.connection import (
    configure_connection,
)
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction
from pantaray_agents.schema.action_conversation import ActionMessageAdoptedEventData
from pantaray_agents.schema.agent.action import ActionAgentRequest
from pantaray_agents.schema.agent.image import ImageInput
from pantaray_agents.tasks.action_user_message import (
    parse_action_user_message,
    render_action_user_request_text,
)

from .action_checkpoint_retention import prune_action_checkpoints_in_connection
from .action_invalidation_events import append_action_invalidation_event
from .action_message_process_fence import (
    resolve_action_process_lineage_in_connection,
)
from .job_payload_models import parse_action_job_payload_json
from .utc_timestamps import now_utc_iso, parse_utc_iso


@dataclass(frozen=True, slots=True)
class PendingActionUserStep:
    step_id: str
    message_id: str
    request_text: str
    created_at: str
    images: tuple[ImageInput, ...] = ()


@dataclass(frozen=True, slots=True)
class _AdoptedUserStep:
    pending: PendingActionUserStep
    step_number: int
    local_step_number: int
    short_step_id: str


def adopt_pending_action_user_steps_at_parent_think(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    job_id: str,
    process_id: str,
    request: ActionAgentRequest,
    state: ActionAgentState,
) -> ActionAgentState:
    """Validate the current running segment and adopt pending USER turns."""

    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        configure_connection(connection, busy_timeout_ms)
        with immediate_transaction(connection):
            adopted_at = now_utc_iso()
            _validate_current_segment(
                connection=connection,
                job_id=job_id,
                process_id=process_id,
                request=request,
            )
            lineage = resolve_action_process_lineage_in_connection(
                connection=connection,
                user_id=request.user_id,
                action_id=request.action_id,
                process_id=process_id,
            )
            if lineage.job_id != job_id:
                raise MigrationError("current Action segment lineage is inconsistent")
            return adopt_pending_action_user_steps_in_connection(
                connection=connection,
                user_id=request.user_id,
                action_id=request.action_id,
                expected_process_id=lineage.root_process_id,
                adopted_process_id=lineage.root_process_id,
                state=state,
                adopted_at=adopted_at,
            )


def adopt_pending_action_user_steps_in_connection(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    action_id: str,
    expected_process_id: str,
    adopted_process_id: str,
    state: ActionAgentState,
    adopted_at: str,
    pending_steps: tuple[PendingActionUserStep, ...] | None = None,
) -> ActionAgentState:
    """Adopt every pending USER turn and checkpoint the projected state."""

    if not connection.in_transaction:
        raise MigrationError("USER adoption requires a caller-owned transaction")
    if state.get("user_id") != user_id or state.get("action_id") != action_id:
        raise MigrationError("runtime state does not match USER adoption owner")
    phase = state.get("phase")
    if phase not in {"init", "planning", "executing"}:
        raise MigrationError("pending USER turns require an active THINK phase")
    pending = (
        pending_steps
        if pending_steps is not None
        else load_pending_action_user_steps_in_connection(
            connection=connection,
            user_id=user_id,
            action_id=action_id,
            expected_process_id=expected_process_id,
        )
    )
    if not pending:
        return state

    next_step_number, next_local_step_number, parent_step_id = _next_positions(
        connection=connection,
        user_id=user_id,
        action_id=action_id,
    )
    if state.get("step") != next_step_number:
        raise MigrationError("runtime state step does not match durable Action history")
    projected_state = state
    adopted_steps: list[_AdoptedUserStep] = []
    for index, row in enumerate(pending):
        step = _AdoptedUserStep(
            pending=row,
            step_number=next_step_number + index,
            local_step_number=next_local_step_number + index,
            short_step_id=f"S-{next_local_step_number + index}-USER",
        )
        projected_state = project_persisted_user_request_step(
            projected_state,
            step_id=row.step_id,
            step_number=step.step_number,
            local_step_number=step.local_step_number,
            short_step_id=step.short_step_id,
            request_text=row.request_text,
            occurred_at=row.created_at,
            history_phase=phase,
            attachments=build_user_image_attachments(
                user_id=user_id,
                images=row.images,
            ),
        )
        adopted_steps.append(step)
    projected_state["updated_at"] = _latest_timestamp(
        str(state.get("updated_at") or ""), adopted_at
    )
    checkpoint_json = json.dumps(
        build_runtime_state_checkpoint(projected_state),
        ensure_ascii=False,
        separators=(",", ":"),
    )

    previous_step_id = parent_step_id
    for step in adopted_steps:
        row = step.pending
        cursor = connection.execute(
            """
            UPDATE agent_action_steps
            SET parent_step_id = ?, step_number = ?, local_step_number = ?,
                short_step_id = ?, adopted_process_id = ?
            WHERE step_id = ? AND user_id = ? AND action_id = ?
              AND step_number IS NULL AND local_step_number IS NULL
              AND short_step_id IS NULL AND adoption_canceled_at IS NULL
              AND expected_process_id = ? AND adopted_process_id IS NULL
            """,
            (
                previous_step_id,
                step.step_number,
                step.local_step_number,
                step.short_step_id,
                adopted_process_id,
                row.step_id,
                user_id,
                action_id,
                expected_process_id,
            ),
        )
        if cursor.rowcount != 1:
            raise MigrationError("pending USER adoption lost its durable ownership")
        append_action_invalidation_event(
            connection=connection,
            event_id=str(uuid.uuid4()),
            user_id=user_id,
            data=ActionMessageAdoptedEventData(
                action_id=action_id,
                message_id=row.message_id,
                step_id=row.step_id,
                process_id=adopted_process_id,
            ),
            created_at=adopted_at,
        )
        previous_step_id = row.step_id

    checkpoint_cursor = connection.execute(
        """
        UPDATE agent_action_steps
        SET runtime_state_checkpoint = ?, runtime_state_checkpoint_version = ?
        WHERE step_id = ? AND adopted_process_id = ?
        """,
        (
            checkpoint_json,
            RUNTIME_STATE_CHECKPOINT_VERSION,
            pending[-1].step_id,
            adopted_process_id,
        ),
    )
    if checkpoint_cursor.rowcount != 1:
        raise MigrationError("adopted USER checkpoint owner disappeared")
    prune_action_checkpoints_in_connection(
        connection, user_id=user_id, action_id=action_id
    )
    return projected_state


def mark_pending_action_user_steps_not_executed_in_connection(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    action_id: str,
    expected_process_id: str,
    canceled_at: str,
    pending_steps: tuple[PendingActionUserStep, ...],
) -> None:
    if not connection.in_transaction:
        raise MigrationError("USER cancellation requires a caller-owned transaction")
    cursor = connection.executemany(
        """
        UPDATE agent_action_steps
        SET adoption_canceled_at = ?
        WHERE step_id = ? AND user_id = ? AND action_id = ?
          AND step_number IS NULL AND local_step_number IS NULL
          AND short_step_id IS NULL AND adoption_canceled_at IS NULL
          AND expected_process_id = ? AND adopted_process_id IS NULL
        """,
        (
            (
                canceled_at,
                row.step_id,
                user_id,
                action_id,
                expected_process_id,
            )
            for row in pending_steps
        ),
    )
    if cursor.rowcount != len(pending_steps):
        raise MigrationError("pending USER cancellation lost its durable ownership")


def _validate_current_segment(
    *,
    connection: sqlite3.Connection,
    job_id: str,
    process_id: str,
    request: ActionAgentRequest,
) -> None:
    row = connection.execute(
        """
        SELECT jobs.user_id AS job_user_id, jobs.job_type, jobs.process_id,
               jobs.status AS job_status, jobs.logical_key, payloads.payload_json,
               processes.user_id AS process_user_id, processes.kind,
               processes.status AS process_status, processes.action_id,
               processes.current_job_id, actions.user_id AS action_user_id,
               actions.status AS action_status
        FROM jobs
        JOIN job_payloads AS payloads ON payloads.job_id = jobs.job_id
        JOIN processes ON processes.process_id = jobs.process_id
        JOIN agent_actions AS actions ON actions.action_id = processes.action_id
        WHERE jobs.job_id = ? AND processes.process_id = ?
        """,
        (job_id, process_id),
    ).fetchone()
    expected = {
        "job_user_id": request.user_id,
        "job_type": "execute_action",
        "process_id": process_id,
        "job_status": "running",
        "logical_key": request.action_id,
        "process_user_id": request.user_id,
        "kind": "action",
        "process_status": "running",
        "action_id": request.action_id,
        "current_job_id": job_id,
        "action_user_id": request.user_id,
        "action_status": "processing",
    }
    if row is None or any(row[field] != value for field, value in expected.items()):
        raise MigrationError("current Action job/process envelope is inconsistent")
    payload_json = row["payload_json"]
    if not isinstance(payload_json, str):
        raise MigrationError("current Action job payload must be TEXT")
    payload = parse_action_job_payload_json(payload_json)
    if (
        payload["job_id"] != job_id
        or payload["process_id"] != process_id
        or payload["action_id"] != request.action_id
        or payload["user_id"] != request.user_id
    ):
        raise MigrationError("current Action job payload identity is inconsistent")
    continuation = payload["continuation_ref"]
    if continuation["kind"] == "user_step":
        valid_continuation = (
            continuation["user_step_id"] == request.user_step_id
            and request.approval_resume_session_id is None
            and request.approval_resume_tool_request_id is None
        )
    else:
        valid_continuation = (
            continuation["approval_session_id"] == request.approval_resume_session_id
            and continuation["tool_request_id"]
            == request.approval_resume_tool_request_id
        )
    if not valid_continuation:
        raise MigrationError("current Action continuation does not match request")


def load_pending_action_user_steps_in_connection(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    action_id: str,
    expected_process_id: str,
) -> tuple[PendingActionUserStep, ...]:
    rows = connection.execute(
        """
        SELECT step_id, user_message_id, user_message_json, user_request_text,
               created_at, accepted_sequence, parent_step_id, local_step_number,
               short_step_id, status, goal_handle, expected_process_id,
               adopted_process_id
        FROM agent_action_steps
        WHERE user_id = ? AND action_id = ? AND step_type = 'user_request'
          AND step_number IS NULL AND adoption_canceled_at IS NULL
        ORDER BY accepted_sequence, step_id
        """,
        (user_id, action_id),
    ).fetchall()
    pending: list[PendingActionUserStep] = []
    for row in rows:
        accepted_sequence = row["accepted_sequence"]
        if (
            isinstance(accepted_sequence, bool)
            or not isinstance(accepted_sequence, int)
            or accepted_sequence <= 0
            or row["parent_step_id"] is not None
            or row["local_step_number"] is not None
            or row["short_step_id"] is not None
            or row["status"] != "success"
            or row["goal_handle"] != "S"
            or row["expected_process_id"] != expected_process_id
            or row["adopted_process_id"] is not None
        ):
            raise MigrationError("pending USER step ownership is inconsistent")
        step_id = _required_text(row["step_id"], field="step_id")
        message_id = _required_text(row["user_message_id"], field="message_id")
        message_json = _required_text(row["user_message_json"], field="message_json")
        request_text = _required_text(row["user_request_text"], field="request_text")
        message = parse_action_user_message(message_json)
        if (
            message.message_id != message_id
            or render_action_user_request_text(message) != request_text
        ):
            raise MigrationError("pending USER message evidence is inconsistent")
        pending.append(
            PendingActionUserStep(
                step_id=step_id,
                message_id=message_id,
                request_text=request_text,
                created_at=_required_text(row["created_at"], field="created_at"),
                images=message.images,
            )
        )
    return tuple(pending)


def _next_positions(
    *, connection: sqlite3.Connection, user_id: str, action_id: str
) -> tuple[int, int, str | None]:
    maxima = connection.execute(
        """
        SELECT COALESCE(MAX(step_number), 0),
               COALESCE(MAX(CASE WHEN goal_handle = 'S'
                                 THEN local_step_number END), 0)
        FROM agent_action_steps
        WHERE user_id = ? AND action_id = ?
        """,
        (user_id, action_id),
    ).fetchone()
    parent = connection.execute(
        """
        SELECT step_id FROM agent_action_steps
        WHERE user_id = ? AND action_id = ? AND goal_handle = 'S'
          AND step_number IS NOT NULL
        ORDER BY step_number DESC, local_step_number DESC, completed_at DESC,
                 created_at DESC, step_id DESC
        LIMIT 1
        """,
        (user_id, action_id),
    ).fetchone()
    assert maxima is not None
    return int(maxima[0]) + 1, int(maxima[1]) + 1, str(parent[0]) if parent else None


def _required_text(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MigrationError(f"pending USER {field} must be non-empty TEXT")
    return value


def _latest_timestamp(current: str, adopted_at: str) -> str:
    return max((current, adopted_at), key=parse_utc_iso)


__all__ = [
    "PendingActionUserStep",
    "adopt_pending_action_user_steps_at_parent_think",
    "adopt_pending_action_user_steps_in_connection",
    "load_pending_action_user_steps_in_connection",
    "mark_pending_action_user_steps_not_executed_in_connection",
]
