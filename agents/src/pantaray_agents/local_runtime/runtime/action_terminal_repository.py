from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from pantaray_agents.action_status import FinalizeActionTerminalCommand
from pantaray_agents.local_runtime.memory_catalog.models import MemoryRevision
from pantaray_agents.local_runtime.storage.migrations import MigrationError
from pantaray_agents.local_runtime.storage.transactions import (
    immediate_transaction,
)
from pantaray_agents.local_runtime.suggestion_state.action_terminal_projection import (
    ActionTerminalProjectionResult,
    finalize_action_terminal_projection,
    map_action_status_to_process_status,
)
from pantaray_agents.local_runtime.suggestion_state.public_process_events import (
    PublicProcessEventActionOwnerError,
    PublicProcessEventConflictError,
    read_public_process_event_sequence,
)
from pantaray_agents.local_runtime.suggestion_state.shared import configure_connection
from pantaray_agents.schema.agent.action import RuntimeStateCheckpointPayload

from .action_subagent_parent_lifecycle import (
    ActionChildSettlementPendingError,
    resolve_action_terminal_cancel_winner_in_connection,
    settle_action_subagent_children_in_connection,
)
from .action_subagent_process_stop import stop_action_subagent_processes
from .action_terminal_handoff import (
    apply_action_terminal_handoff_in_connection,
    validate_action_terminal_handoff_in_connection,
)
from .action_terminal_memory import (
    InvalidActionMemoryDraftError,
    load_completed_action_turn_binding_in_connection,
    publish_action_memory_in_connection,
)
from .memory_agent_triggers import (
    ACTION_TERMINAL_MEMORY_TRIGGER_KIND,
    ActionTerminalTriggerBinding,
    insert_pending_memory_agent_trigger,
)
from .process_events import append_process_event_in_connection


@dataclass(frozen=True)
class ActionTerminalRepository:
    db_path: Path
    busy_timeout_ms: int

    async def finalize_action_job_terminal(
        self,
        *,
        command: FinalizeActionTerminalCommand,
        job_id: str,
        runtime_state_checkpoint: RuntimeStateCheckpointPayload | None,
    ) -> ActionTerminalProjectionResult:
        if self.busy_timeout_ms <= 0:
            raise MigrationError("LOCAL_DB_BUSY_TIMEOUT_MS must be a positive integer")
        if not job_id.strip():
            raise MigrationError("job_id must not be empty")
        # The transaction below settles the children and releases their claims.
        await stop_action_subagent_processes(
            db_path=self.db_path,
            busy_timeout_ms=self.busy_timeout_ms,
            user_id=command.user_id,
            action_id=command.action_id,
            parent_process_id=command.process_id,
        )

        with sqlite3.connect(self.db_path) as connection:
            configure_connection(connection, self.busy_timeout_ms)
            with immediate_transaction(connection):
                # The durable Stop fence decides the winner before the replay
                # branch reads it back, so one event id always resolves to the
                # status this transaction persisted.
                winner = resolve_action_terminal_cancel_winner_in_connection(
                    connection=connection,
                    command=command,
                    job_id=job_id,
                )
                if _internal_stream_end_exists(
                    connection=connection,
                    process_id=winner.process_id,
                    event_id=winner.process_completed_event_id,
                ):
                    return finalize_action_job_terminal_in_connection(
                        connection=connection,
                        command=winner,
                        job_id=job_id,
                    )
                # A live child keeps its claims until its own worker settles it,
                # so the recorded cancel requests are committed without the
                # parent terminal and the caller retries this transaction.
                settled = settle_action_subagent_children_in_connection(
                    connection=connection,
                    command=winner,
                    job_id=job_id,
                )
                if settled:
                    return self._finalize_settled_parent(
                        connection=connection,
                        command=winner,
                        job_id=job_id,
                        runtime_state_checkpoint=runtime_state_checkpoint,
                    )
        raise ActionChildSettlementPendingError(
            f"Action {command.action_id} still owns an unsettled subagent child"
        )

    def _finalize_settled_parent(
        self,
        *,
        connection: sqlite3.Connection,
        command: FinalizeActionTerminalCommand,
        job_id: str,
        runtime_state_checkpoint: RuntimeStateCheckpointPayload | None,
    ) -> ActionTerminalProjectionResult:
        authority = validate_action_terminal_handoff_in_connection(
            connection=connection,
            command=command,
            job_id=job_id,
        )
        result = finalize_action_job_terminal_in_connection(
            connection=connection,
            command=command,
            job_id=job_id,
        )
        apply_action_terminal_handoff_in_connection(
            connection=connection,
            command=command,
            authority=authority,
            # A canceled turn hands off to nothing. The Stop transaction could
            # only mark the USER steps it saw before it recorded the fence, so
            # this transaction settles the ones accepted while a child was
            # still settling instead of leaving them pending forever.
            runtime_state_checkpoint=(
                None
                if command.action_status == "canceled"
                else runtime_state_checkpoint
            ),
        )
        return result


def finalize_action_job_terminal_in_connection(
    *,
    connection: sqlite3.Connection,
    command: FinalizeActionTerminalCommand,
    job_id: str,
) -> ActionTerminalProjectionResult:
    if _internal_stream_end_exists(
        connection=connection,
        process_id=command.process_id,
        event_id=command.process_completed_event_id,
    ):
        existing_sequence = read_public_process_event_sequence(
            connection=connection,
            event_id=command.process_completed_event_id,
            suggestion_id=command.suggestion_id,
            action_id=command.action_id,
            event_name="process_completed",
        )
        if existing_sequence is None:
            raise MigrationError(
                "stream_end exists without public process_completed event"
            )
        return _build_projection_result_from_command(
            command,
            process_completed_sequence=existing_sequence,
        )
    _interrupt_pending_approval_sessions(connection=connection, command=command)

    projection_result = finalize_action_terminal_projection(
        connection=connection,
        command=command,
    )
    action_revision = None
    if command.action_status == "success":
        action_revision = publish_action_memory_in_connection(
            connection=connection, command=command
        )
    finalize_action_run_terminal_in_connection(
        connection=connection,
        command=command,
        job_id=job_id,
        persisted_sequence=projection_result.process_completed_sequence,
        physical_run_only=False,
    )
    _trigger_memory_update_for_terminal(
        connection=connection,
        command=command,
        action_revision=action_revision,
    )
    return projection_result


def _trigger_memory_update_for_terminal(
    *,
    connection: sqlite3.Connection,
    command: FinalizeActionTerminalCommand,
    action_revision: MemoryRevision | None,
) -> None:
    """Hand the completed turn to the unified Memory run.

    Success, error and cancel all carry a lesson, so every terminal that
    completed a turn triggers a run; only a successful one published a
    revision the run can reference as evidence.
    """

    binding = load_completed_action_turn_binding_in_connection(
        connection=connection,
        command=command,
        action_revision=action_revision,
    )
    if binding is None:
        return
    insert_pending_memory_agent_trigger(
        connection=connection,
        user_id=command.user_id,
        trigger_kind=ACTION_TERMINAL_MEMORY_TRIGGER_KIND,
        source_id=f"{command.action_id}:{binding.turn_end_step_number}",
        created_at=command.completed_at,
        action_terminal=ActionTerminalTriggerBinding(
            action_id=command.action_id,
            action_completed_at=command.completed_at,
            source_action_revision_id=binding.source_action_revision_id,
            turn_start_step_number=binding.turn_start_step_number,
            turn_end_step_number=binding.turn_end_step_number,
            action_prompt_name=binding.action_prompt_name,
            action_prompt_version=binding.action_prompt_version,
            suggestion_id=command.suggestion_id,
        ),
    )


def finalize_action_run_terminal_in_connection(
    *,
    connection: sqlite3.Connection,
    command: FinalizeActionTerminalCommand,
    job_id: str,
    persisted_sequence: int | None,
    physical_run_only: bool,
) -> None:
    """Persist one physical Action run terminal without projecting the Action."""

    if physical_run_only == (persisted_sequence is not None):
        raise MigrationError("Action run terminal projection mode is inconsistent")
    stream_end_payload: dict[str, object] = dict(
        command.process_completed_payload["data"]
    )
    if persisted_sequence is not None:
        stream_end_payload["persisted_sequence"] = persisted_sequence
    if physical_run_only:
        stream_end_payload["physical_run_only"] = True
    append_process_event_in_connection(
        connection=connection,
        process_id=command.process_id,
        event_id=command.process_completed_event_id,
        event_name="stream_end",
        payload=stream_end_payload,
        created_at=command.completed_at,
    )
    _update_runtime_terminal_rows(
        connection=connection,
        job_id=job_id,
        process_id=command.process_id,
        command=command,
    )


def action_run_terminal_is_pending_in_connection(
    *,
    connection: sqlite3.Connection,
    command: FinalizeActionTerminalCommand,
    job_id: str,
    physical_run_only: bool,
) -> bool:
    row = connection.execute(
        """
        SELECT
            CASE
                WHEN process.terminal_event_id IS NULL
                  AND process.status = 'running' AND job.status = 'running'
                  AND attempt.status = 'running' AND process.completed_at IS NULL
                  AND job.completed_at IS NULL AND attempt.completed_at IS NULL
                  AND attempt.error_code IS NULL
                  AND process.current_job_id = job.job_id
                THEN 'pending'
                WHEN process.terminal_event_id IS NOT NULL
                  AND process.current_job_id IS NULL
                  AND process.completed_at IS NOT NULL AND process.completed_at <> ''
                  AND process.completed_at = job.completed_at
                  AND process.completed_at = attempt.completed_at
                  AND process.completed_at = process.updated_at
                  AND process.completed_at = process.heartbeat_at
                  AND process.completed_at = job.heartbeat_at
                  AND process.completed_at = terminal.created_at
                  AND terminal.event_name = 'stream_end'
                  AND process.status = job.status AND job.status = attempt.status
                  AND process.status IN ('completed', 'failed', 'canceled')
                THEN CASE process.status
                    WHEN 'completed' THEN 'success'
                    WHEN 'failed' THEN 'error'
                    ELSE 'canceled'
                END
            END AS authority_status,
            process.terminal_event_id, terminal.payload_json,
            process.completed_at, job.error_code, attempt.error_code
        FROM jobs AS job
        JOIN processes AS process ON process.process_id = job.process_id
        JOIN job_attempts AS attempt
          ON attempt.job_id = job.job_id AND attempt.attempt_number = job.attempt
        LEFT JOIN process_events AS terminal
          ON terminal.process_id = process.process_id
         AND terminal.event_id = process.terminal_event_id
        WHERE job.job_id = ? AND job.process_id = ? AND job.user_id = ?
          AND job.job_type = 'execute_action' AND job.logical_key = ?
          AND process.user_id = ? AND process.kind = 'action'
          AND process.action_id = ? AND process.suggestion_id IS ?
        """,
        (
            job_id,
            command.process_id,
            command.user_id,
            command.action_id,
            command.user_id,
            command.action_id,
            command.suggestion_id,
        ),
    ).fetchone()
    if row is None:
        raise MigrationError("Action run terminal authority is unavailable")
    status, terminal_event_id, terminal_payload_json = row[:3]
    completed_at, job_error_code, attempt_error_code = row[3:]
    if status == "pending":
        return True
    if (
        status not in {"success", "error", "canceled"}
        or not isinstance(terminal_event_id, str)
        or not isinstance(completed_at, str)
        or not isinstance(terminal_payload_json, str)
    ):
        raise MigrationError("Terminal Action run authority is inconsistent")
    try:
        terminal_payload = json.loads(terminal_payload_json)
    except json.JSONDecodeError as exc:
        raise MigrationError("Action run terminal payload is invalid JSON") from exc
    identity: dict[str, object] = {
        "kind": "action",
        "process_id": command.process_id,
        "action_id": command.action_id,
        "command_id": command.command_id,
        "status": status,
        "completed_at": completed_at,
    }
    if command.suggestion_id is not None:
        identity["suggestion_id"] = command.suggestion_id
    if not isinstance(terminal_payload, dict) or any(
        terminal_payload.get(key) != value for key, value in identity.items()
    ):
        raise MigrationError("Action run terminal payload identity is inconsistent")
    if command.suggestion_id is None and "suggestion_id" in terminal_payload:
        raise MigrationError("Action run terminal Suggestion identity is inconsistent")
    if physical_run_only and terminal_event_id == command.process_completed_event_id:
        expected_payload = dict(command.process_completed_payload["data"])
        expected_payload["physical_run_only"] = True
        if (
            terminal_payload != expected_payload
            or completed_at != command.completed_at
            or job_error_code != command.failure_code
            or attempt_error_code != command.failure_code
        ):
            raise MigrationError("Skipped Action run terminal is inconsistent")
        return False

    sequence = terminal_payload.get("persisted_sequence")
    failure_code = terminal_payload.get("failure_code")
    try:
        public_sequence = read_public_process_event_sequence(
            connection=connection,
            event_id=terminal_event_id,
            suggestion_id=command.suggestion_id,
            action_id=command.action_id,
            event_name="process_completed",
        )
    except (PublicProcessEventActionOwnerError, PublicProcessEventConflictError) as exc:
        raise MigrationError("Prior Action public terminal is inconsistent") from exc
    if (
        not isinstance(sequence, int)
        or isinstance(sequence, bool)
        or sequence <= 0
        or public_sequence != sequence
        or "physical_run_only" in terminal_payload
        or (
            status == "success"
            and (job_error_code is not None or attempt_error_code is not None)
        )
        or (
            status != "success"
            and (
                not isinstance(failure_code, str)
                or not failure_code
                or job_error_code != failure_code
                or attempt_error_code != failure_code
            )
        )
    ):
        raise MigrationError("Prior Action run terminal is inconsistent")
    return False


def _interrupt_pending_approval_sessions(
    *,
    connection: sqlite3.Connection,
    command: FinalizeActionTerminalCommand,
) -> None:
    connection.execute(
        """
        UPDATE approval_sessions
        SET status = 'interrupted', decided_at = ?
        WHERE user_id = ? AND action_id = ? AND status = 'pending'
        """,
        (command.completed_at, command.user_id, command.action_id),
    )


def _build_projection_result_from_command(
    command: FinalizeActionTerminalCommand,
    *,
    process_completed_sequence: int,
) -> ActionTerminalProjectionResult:
    return ActionTerminalProjectionResult(
        process_completed_sequence=process_completed_sequence,
        action_status=command.action_status,
        action_failure_code=command.failure_code,
        final_output=command.final_output,
        failure_stage=command.failure_stage,
        failure_message_public=command.failure_message_public,
    )


def _internal_stream_end_exists(
    *,
    connection: sqlite3.Connection,
    process_id: str,
    event_id: str,
) -> bool:
    row = connection.execute(
        """
        SELECT 1
        FROM process_events
        WHERE process_id = ? AND event_id = ? AND event_name = 'stream_end'
        LIMIT 1
        """,
        (process_id, event_id),
    ).fetchone()
    return row is not None


def _update_runtime_terminal_rows(
    *,
    connection: sqlite3.Connection,
    job_id: str,
    process_id: str,
    command: FinalizeActionTerminalCommand,
) -> None:
    job_status = _map_action_status_to_job_status(command.action_status)
    process_status = map_action_status_to_process_status(command.action_status)
    attempt_status = _map_job_status_to_attempt_status(job_status)
    connection.execute(
        """
        UPDATE jobs
        SET
            status = ?,
            completed_at = ?,
            heartbeat_at = ?,
            error_code = ?
        WHERE job_id = ?
        """,
        (
            job_status,
            command.completed_at,
            command.completed_at,
            command.failure_code,
            job_id,
        ),
    )
    connection.execute(
        """
        UPDATE processes
        SET
            status = ?,
            completed_at = ?,
            current_job_id = NULL,
            terminal_event_id = ?,
            updated_at = ?,
            heartbeat_at = ?
        WHERE process_id = ?
        """,
        (
            process_status,
            command.completed_at,
            command.process_completed_event_id,
            command.completed_at,
            command.completed_at,
            process_id,
        ),
    )
    connection.execute(
        """
        UPDATE job_attempts
        SET
            status = ?,
            completed_at = ?,
            error_code = ?
        WHERE job_id = ? AND status = 'running'
        """,
        (
            attempt_status,
            command.completed_at,
            command.failure_code,
            job_id,
        ),
    )
    if (
        connection.execute(
            "SELECT 1 FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        is None
    ):
        raise MigrationError(f"job not found: {job_id}")


def _map_action_status_to_job_status(action_status: str) -> str:
    if action_status == "success":
        return "completed"
    if action_status == "canceled":
        return "canceled"
    if action_status == "error":
        return "failed"
    raise MigrationError(f"unsupported action terminal status: {action_status}")


def _map_job_status_to_attempt_status(job_status: str) -> str:
    if job_status == "completed":
        return "completed"
    if job_status == "canceled":
        return "canceled"
    return "failed"


__all__ = [
    "ActionTerminalRepository",
    "InvalidActionMemoryDraftError",
    "finalize_action_job_terminal_in_connection",
    "finalize_action_run_terminal_in_connection",
]
