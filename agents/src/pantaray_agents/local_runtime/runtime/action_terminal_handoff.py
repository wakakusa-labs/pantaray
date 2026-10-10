from __future__ import annotations

import sqlite3
import uuid
from datetime import timedelta

from pantaray_agents.action_status import FinalizeActionTerminalCommand
from pantaray_agents.agents.action_agent.runtime.checkpoint import (
    restore_runtime_state_checkpoint,
)
from pantaray_agents.application.action.new_user_turn import (
    reset_state_for_new_user_turn,
)
from pantaray_agents.local_runtime.storage.migrations import MigrationError
from pantaray_agents.schema.agent.action import RuntimeStateCheckpointPayload

from .action_job_runtime_repository import (
    load_and_validate_action_command_user_step_in_connection,
    load_and_validate_action_job_envelope_in_connection,
)
from .action_message_process_fence import (
    resolve_action_process_lineage_in_connection,
)
from .action_queue import build_local_action_enqueue_request
from .action_user_adoption import (
    PendingActionUserStep,
    adopt_pending_action_user_steps_in_connection,
    load_pending_action_user_steps_in_connection,
    mark_pending_action_user_steps_not_executed_in_connection,
)
from .job_enqueue import enqueue_local_job
from .job_payload_builder import build_action_job_payload
from .job_payload_models import parse_action_job_payload_json
from .utc_timestamps import format_utc_iso, now_utc_iso, parse_utc_iso


def validate_action_terminal_handoff_in_connection(
    *,
    connection: sqlite3.Connection,
    command: FinalizeActionTerminalCommand,
    job_id: str,
) -> tuple[str, tuple[PendingActionUserStep, ...]]:
    if not connection.in_transaction:
        raise MigrationError(
            "Action terminal handoff requires a caller-owned transaction"
        )
    payload_row = connection.execute(
        "SELECT payload_json FROM job_payloads WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    if payload_row is None or not isinstance(payload_row["payload_json"], str):
        raise MigrationError("Action terminal handoff envelope is inconsistent")
    payload = parse_action_job_payload_json(payload_row["payload_json"])
    if (
        payload["job_id"] != job_id
        or payload["process_id"] != command.process_id
        or payload["action_id"] != command.action_id
        or payload["user_id"] != command.user_id
    ):
        raise MigrationError("Action terminal handoff envelope is inconsistent")
    row = load_and_validate_action_job_envelope_in_connection(
        connection=connection,
        payload=payload,
    )
    if (
        row["action_status"] != "processing"
        or row["suggestion_id"] != command.suggestion_id
    ):
        raise MigrationError("Action terminal handoff envelope is inconsistent")

    lineage = resolve_action_process_lineage_in_connection(
        connection=connection,
        user_id=command.user_id,
        action_id=command.action_id,
        process_id=command.process_id,
    )
    if lineage.job_id != job_id:
        raise MigrationError("Action terminal handoff lineage is inconsistent")
    context = load_and_validate_action_command_user_step_in_connection(
        connection=connection,
        action_row=row,
        user_id=command.user_id,
        action_id=command.action_id,
        process_id=lineage.root_process_id,
        command_id=command.command_id,
    )
    if parse_utc_iso(context.accepted_at) != parse_utc_iso(command.accepted_at):
        raise MigrationError("Action terminal command acceptance time is inconsistent")
    continuation = payload["continuation_ref"]
    if continuation.get("user_step_id") not in (None, context.user_step_id):
        raise MigrationError("Action terminal USER continuation is inconsistent")
    pending_steps = load_pending_action_user_steps_in_connection(
        connection=connection,
        user_id=command.user_id,
        action_id=command.action_id,
        expected_process_id=lineage.root_process_id,
    )
    return lineage.root_process_id, pending_steps


def apply_action_terminal_handoff_in_connection(
    *,
    connection: sqlite3.Connection,
    command: FinalizeActionTerminalCommand,
    authority: tuple[str, tuple[PendingActionUserStep, ...]],
    runtime_state_checkpoint: RuntimeStateCheckpointPayload | None,
) -> None:
    root_process_id, pending_steps = authority
    if not pending_steps:
        return
    successor_at = _successor_timestamp(command.completed_at)
    if runtime_state_checkpoint is None:
        mark_pending_action_user_steps_not_executed_in_connection(
            connection=connection,
            user_id=command.user_id,
            action_id=command.action_id,
            expected_process_id=root_process_id,
            canceled_at=successor_at,
            pending_steps=pending_steps,
        )
        return
    restored = restore_runtime_state_checkpoint(
        runtime_state_checkpoint,
        expected_action_id=command.action_id,
        expected_suggestion_id=command.suggestion_id,
        expected_user_id=command.user_id,
    )
    successor_state = reset_state_for_new_user_turn(
        restored,
        started_at=successor_at,
    )
    successor_process_id = str(uuid.uuid4())
    successor_job_id = str(uuid.uuid4())
    payload = build_action_job_payload(
        {
            "job_id": successor_job_id,
            "process_id": successor_process_id,
            "action_id": command.action_id,
            "user_id": command.user_id,
            "continuation_ref": {
                "kind": "user_step",
                "user_step_id": pending_steps[0].step_id,
            },
        }
    )
    enqueue_result = enqueue_local_job(
        connection=connection,
        request=build_local_action_enqueue_request(
            payload,
            scheduled_at=successor_at,
            suggestion_id=command.suggestion_id,
        ),
    )
    if not enqueue_result["inserted_new"]:
        raise MigrationError("Action terminal handoff did not create its successor")
    adopt_pending_action_user_steps_in_connection(
        connection=connection,
        user_id=command.user_id,
        action_id=command.action_id,
        adopted_process_id=successor_process_id,
        state=successor_state,
        adopted_at=successor_at,
        pending_steps=pending_steps,
    )
    cursor = connection.execute(
        """
        UPDATE agent_actions
        SET status = 'queued', final_output = '', error = NULL,
            final_prompt_text = NULL, updated_at = ?
        WHERE user_id = ? AND action_id = ? AND status = ?
          AND updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', ?)
        """,
        (
            successor_at,
            command.user_id,
            command.action_id,
            command.action_status,
            command.completed_at,
        ),
    )
    if cursor.rowcount != 1:
        raise MigrationError("Action terminal handoff lost its terminal projection")


def _successor_timestamp(completed_at: str) -> str:
    after_completion = parse_utc_iso(completed_at) + timedelta(milliseconds=1)
    return format_utc_iso(max(parse_utc_iso(now_utc_iso()), after_completion))
