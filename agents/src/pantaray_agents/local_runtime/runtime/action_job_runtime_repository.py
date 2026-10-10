from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from pantaray_agents.action_status import (
    ACTION_FAILURE_MESSAGE_CANCELED,
    ACTION_STATUS_CANCELED,
    build_finalize_action_terminal_command,
)
from pantaray_agents.local_runtime.storage.migrations import MigrationError
from pantaray_agents.local_runtime.storage.migrations.connection import (
    configure_connection,
)
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction
from pantaray_agents.local_runtime.suggestion_state.event_names import (
    EVENT_PROCESS_STARTED,
)
from pantaray_agents.schema.agent.action import (
    ActionExecutionTarget,
    ActionScratchExecutionTarget,
    ActionUserMessageInput,
)
from pantaray_agents.schema.agent.action_assistant_message import (
    ActionAssistantMessageStep,
)
from pantaray_agents.tasks.action_user_message import (
    parse_action_user_message,
    render_action_user_request_text,
)
from pantaray_agents.tasks.types import (
    ActionJobRuntimePayload,
    ActionToolApprovalContinuationRef,
    ActionUserStepContinuationRef,
)

from .action_assistant_messages import read_preceding_assistant_messages
from .action_checkpoint_retention import load_tool_approval_resume_anchor_in_connection
from .action_job_recovery import is_recovered_user_step_resume
from .action_suggestion_adapter import project_suggestion_action_started
from .job_payload_models import parse_action_job_payload_json
from .process_events import append_process_event_in_connection

ActionStartSkipOutcome = Literal["already_processing", "already_terminal"]

ACTION_START_SKIPPED_EVENT = "action_start_skipped"
ACTION_START_SKIP_ALREADY_PROCESSING_CODE = "ACTION_START_ALREADY_PROCESSING"
ACTION_START_SKIP_ALREADY_TERMINAL_CODE = "ACTION_START_ALREADY_TERMINAL"


@dataclass(frozen=True, slots=True)
class ActionJobExecutionContext:
    suggestion_id: str | None
    user_step_id: str
    user_step_number: int
    user_step_local_step_number: int
    user_step_short_id: str
    user_step_created_at: str
    user_message: ActionUserMessageInput
    execution_target: ActionExecutionTarget
    command_id: str
    accepted_at: str
    preceding_assistant_messages: tuple[ActionAssistantMessageStep, ...] = ()
    approval_session_id: str | None = None
    approval_tool_request_id: str | None = None
    checkpoint_step_id: str | None = None


@dataclass(frozen=True, slots=True)
class ActionJobPreparation:
    context: ActionJobExecutionContext
    skip_outcome: ActionStartSkipOutcome | None = None


@dataclass(frozen=True, slots=True)
class ActionJobStartSkipCommand:
    job_id: str
    process_id: str
    user_id: str
    action_id: str
    suggestion_id: str | None
    command_id: str
    accepted_at: str
    completed_at: str
    failure_stage: str
    outcome: ActionStartSkipOutcome


@dataclass(frozen=True, slots=True)
class ActionJobRuntimeRepository:
    db_path: Path
    busy_timeout_ms: int

    def prepare_execution(
        self,
        *,
        payload: ActionJobRuntimePayload,
        started_at: str,
    ) -> ActionJobPreparation:
        self._validate_config()
        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            configure_connection(connection, self.busy_timeout_ms)
            with immediate_transaction(connection):
                action_row = load_and_validate_action_job_envelope_in_connection(
                    connection=connection,
                    payload=payload,
                )
                execution_target = _parse_execution_target(
                    action_row["execution_target_json"]
                )
                continuation_ref = payload["continuation_ref"]
                if continuation_ref["kind"] == "user_step":
                    return _prepare_user_step(
                        connection=connection,
                        payload=payload,
                        action_row=action_row,
                        continuation_ref=continuation_ref,
                        execution_target=execution_target,
                        started_at=started_at,
                    )
                return _prepare_tool_approval(
                    connection=connection,
                    payload=payload,
                    action_row=action_row,
                    continuation_ref=continuation_ref,
                    execution_target=execution_target,
                )

    def finalize_skipped_action_start(
        self, *, command: ActionJobStartSkipCommand
    ) -> None:
        # action_terminal_handoff already imports this repository for USER binding.
        # Delay the inverse terminal helper import until both modules are initialized.
        from .action_terminal_repository import (
            action_run_terminal_is_pending_in_connection,
            finalize_action_run_terminal_in_connection,
        )

        self._validate_config()
        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            configure_connection(connection, self.busy_timeout_ms)
            with immediate_transaction(connection):
                error_code = _skip_outcome_error_code(command.outcome)
                terminal_command = build_finalize_action_terminal_command(
                    process_completed_event_id=(
                        f"{command.job_id}-start-skipped-stream-end"
                    ),
                    suggestion_id=command.suggestion_id,
                    user_id=command.user_id,
                    command_id=command.command_id,
                    process_id=command.process_id,
                    action_id=command.action_id,
                    accepted_at=command.accepted_at,
                    completed_at=command.completed_at,
                    action_status=ACTION_STATUS_CANCELED,
                    failure_code=error_code,
                    failure_stage=command.failure_stage,
                    failure_message_public=ACTION_FAILURE_MESSAGE_CANCELED,
                )
                if not action_run_terminal_is_pending_in_connection(
                    connection=connection,
                    command=terminal_command,
                    job_id=command.job_id,
                    physical_run_only=True,
                ):
                    return
                event_payload: dict[str, object] = {
                    "kind": "action",
                    "action_id": command.action_id,
                    "command_id": command.command_id,
                    "accepted_at": command.accepted_at,
                    "completed_at": terminal_command.completed_at,
                    "outcome": command.outcome,
                }
                if command.suggestion_id is not None:
                    event_payload["suggestion_id"] = command.suggestion_id
                append_process_event_in_connection(
                    connection=connection,
                    process_id=command.process_id,
                    event_name=ACTION_START_SKIPPED_EVENT,
                    payload=event_payload,
                    created_at=terminal_command.completed_at,
                )
                finalize_action_run_terminal_in_connection(
                    connection=connection,
                    command=terminal_command,
                    job_id=command.job_id,
                    persisted_sequence=None,
                    physical_run_only=True,
                )

    def _validate_config(self) -> None:
        if self.busy_timeout_ms <= 0:
            raise MigrationError("LOCAL_DB_BUSY_TIMEOUT_MS must be a positive integer")


def load_and_validate_action_job_envelope_in_connection(
    *,
    connection: sqlite3.Connection,
    payload: ActionJobRuntimePayload,
) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT
            jobs.user_id AS job_user_id,
            jobs.job_type,
            jobs.process_id AS job_process_id,
            jobs.status AS job_status,
            jobs.logical_key AS job_logical_key,
            job_payloads.payload_json,
            processes.user_id AS process_user_id,
            processes.kind AS process_kind,
            processes.status AS process_status,
            processes.action_id AS process_action_id,
            processes.suggestion_id AS process_suggestion_id,
            processes.current_job_id,
            actions.user_id AS action_user_id,
            actions.suggestion_id,
            actions.initial_user_message_id,
            actions.status AS action_status,
            actions.execution_target_json
        FROM jobs
        JOIN job_payloads ON job_payloads.job_id = jobs.job_id
        JOIN processes ON processes.process_id = jobs.process_id
        JOIN agent_actions AS actions ON actions.action_id = processes.action_id
        WHERE jobs.job_id = ?
        """,
        (payload["job_id"],),
    ).fetchone()
    if row is None:
        raise MigrationError("Action job execution envelope was not found")
    expected = {
        "job_user_id": payload["user_id"],
        "job_type": "execute_action",
        "job_process_id": payload["process_id"],
        "job_status": "running",
        "job_logical_key": payload["action_id"],
        "process_user_id": payload["user_id"],
        "process_kind": "action",
        "process_status": "running",
        "process_action_id": payload["action_id"],
        "current_job_id": payload["job_id"],
        "action_user_id": payload["user_id"],
    }
    for field_name, expected_value in expected.items():
        if row[field_name] != expected_value:
            raise MigrationError(
                "Action job execution envelope mismatch: "
                f"field={field_name} expected={expected_value!r} "
                f"actual={row[field_name]!r}"
            )
    persisted_payload = parse_action_job_payload_json(str(row["payload_json"]))
    if persisted_payload != payload:
        raise MigrationError("Action job payload does not match its durable envelope")
    if row["process_suggestion_id"] != row["suggestion_id"]:
        raise MigrationError(
            "Action process suggestion provenance does not match Action"
        )
    return cast(sqlite3.Row, row)


def _prepare_user_step(
    *,
    connection: sqlite3.Connection,
    payload: ActionJobRuntimePayload,
    action_row: sqlite3.Row,
    continuation_ref: ActionUserStepContinuationRef,
    execution_target: ActionExecutionTarget,
    started_at: str,
) -> ActionJobPreparation:
    step_row = _load_user_step(
        connection=connection,
        action_id=payload["action_id"],
        user_id=payload["user_id"],
        user_step_id=continuation_ref["user_step_id"],
    )
    context = _build_context(
        action_row=action_row,
        step_row=step_row,
        execution_target=execution_target,
        preceding_assistant_messages=read_preceding_assistant_messages(
            connection,
            user_id=payload["user_id"],
            action_id=payload["action_id"],
            before_step_number=step_row["step_number"],
        ),
    )
    action_status = str(action_row["action_status"])
    if action_status == "processing":
        if is_recovered_user_step_resume(
            connection=connection,
            job_id=payload["job_id"],
            process_id=payload["process_id"],
            action_id=payload["action_id"],
            user_id=payload["user_id"],
            command_id=context.command_id,
            accepted_at=context.accepted_at,
            suggestion_id=context.suggestion_id,
        ):
            return ActionJobPreparation(context=context)
        return ActionJobPreparation(context=context, skip_outcome="already_processing")
    if action_status in {"success", "error", "canceled"}:
        return ActionJobPreparation(context=context, skip_outcome="already_terminal")
    if action_status != "queued":
        raise MigrationError(f"Unsupported Action start status: {action_status!r}")
    cursor = connection.execute(
        """
        UPDATE agent_actions
        SET status = 'processing', updated_at = ?
        WHERE action_id = ? AND user_id = ? AND status = 'queued'
        """,
        (started_at, payload["action_id"], payload["user_id"]),
    )
    if cursor.rowcount != 1:
        raise MigrationError(
            "Action start claim did not update exactly one queued Action"
        )
    event_id = f"{payload['job_id']}-process-started"
    event_payload: dict[str, object] = {
        "kind": "action",
        "process_id": payload["process_id"],
        "action_id": payload["action_id"],
        "command_id": context.command_id,
        "accepted_at": context.accepted_at,
        "started_at": started_at,
    }
    if context.suggestion_id is not None:
        event_payload["suggestion_id"] = context.suggestion_id
        event_payload["persisted_sequence"] = project_suggestion_action_started(
            connection=connection,
            event_id=event_id,
            action_id=payload["action_id"],
            process_id=payload["process_id"],
            user_id=payload["user_id"],
            suggestion_id=context.suggestion_id,
            command_id=context.command_id,
            accepted_at=context.accepted_at,
            started_at=started_at,
        )
    append_process_event_in_connection(
        connection=connection,
        process_id=payload["process_id"],
        event_id=event_id,
        event_name=EVENT_PROCESS_STARTED,
        payload=event_payload,
        created_at=started_at,
    )
    return ActionJobPreparation(context=context)


def _prepare_tool_approval(
    *,
    connection: sqlite3.Connection,
    payload: ActionJobRuntimePayload,
    action_row: sqlite3.Row,
    continuation_ref: ActionToolApprovalContinuationRef,
    execution_target: ActionExecutionTarget,
) -> ActionJobPreparation:
    if action_row["action_status"] != "processing":
        raise MigrationError("Tool approval continuation requires a processing Action")
    approval_row = connection.execute(
        """
        SELECT status
        FROM approval_sessions
        WHERE approval_session_id = ? AND tool_request_id = ?
          AND action_id = ? AND user_id = ?
        """,
        (
            continuation_ref["approval_session_id"],
            continuation_ref["tool_request_id"],
            payload["action_id"],
            payload["user_id"],
        ),
    ).fetchone()
    if approval_row is None:
        raise MigrationError(
            "Tool approval continuation does not own the approval request"
        )
    if approval_row["status"] not in {"approved_once", "denied"}:
        raise MigrationError("Tool approval continuation requires a decided approval")
    anchor = load_tool_approval_resume_anchor_in_connection(
        connection,
        user_id=payload["user_id"],
        action_id=payload["action_id"],
        approval_session_id=continuation_ref["approval_session_id"],
        tool_request_id=continuation_ref["tool_request_id"],
    )
    if anchor is None:
        raise MigrationError("Tool approval continuation checkpoint was not found")
    step_row = connection.execute(
        """
        SELECT step_id, step_number, local_step_number, short_step_id,
               step_type, status, user_message_id, user_message_json,
               user_request_text, created_at
        FROM agent_action_steps
        WHERE action_id = ? AND user_id = ? AND step_type = 'user_request'
          AND step_number <= ?
        ORDER BY step_number DESC, created_at DESC, step_id DESC
        LIMIT 1
        """,
        (payload["action_id"], payload["user_id"], anchor.step_number),
    ).fetchone()
    if step_row is None:
        raise MigrationError("Tool approval checkpoint has no owning USER step")
    context = _build_context(
        action_row=action_row,
        step_row=step_row,
        execution_target=execution_target,
        approval_session_id=continuation_ref["approval_session_id"],
        approval_tool_request_id=continuation_ref["tool_request_id"],
        checkpoint_step_id=anchor.step_id,
    )
    return ActionJobPreparation(context=context)


def _load_user_step(
    *,
    connection: sqlite3.Connection,
    action_id: str,
    user_id: str,
    user_step_id: str,
) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT step_id, step_number, local_step_number, short_step_id,
               step_type, status, user_message_id, user_message_json,
               user_request_text, created_at
        FROM agent_action_steps
        WHERE step_id = ? AND action_id = ? AND user_id = ?
        """,
        (user_step_id, action_id, user_id),
    ).fetchone()
    if row is None:
        raise MigrationError("Action continuation USER step was not found or not owned")
    return cast(sqlite3.Row, row)


def load_and_validate_action_command_user_step_in_connection(
    *,
    connection: sqlite3.Connection,
    action_row: sqlite3.Row,
    user_id: str,
    action_id: str,
    process_id: str,
    command_id: str,
) -> ActionJobExecutionContext:
    rows = connection.execute(
        """
        SELECT step_id
        FROM agent_action_steps
        WHERE user_id = ? AND action_id = ? AND adopted_process_id = ?
          AND user_message_id = ?
        LIMIT 2
        """,
        (user_id, action_id, process_id, command_id),
    ).fetchall()
    if len(rows) != 1:
        raise MigrationError("Action command USER step is not unique")
    return _build_context(
        action_row=action_row,
        step_row=_load_user_step(
            connection=connection,
            action_id=action_id,
            user_id=user_id,
            user_step_id=str(rows[0]["step_id"]),
        ),
        execution_target=_parse_execution_target(action_row["execution_target_json"]),
    )


def _build_context(
    *,
    action_row: sqlite3.Row,
    step_row: sqlite3.Row,
    execution_target: ActionExecutionTarget,
    preceding_assistant_messages: tuple[ActionAssistantMessageStep, ...] = (),
    approval_session_id: str | None = None,
    approval_tool_request_id: str | None = None,
    checkpoint_step_id: str | None = None,
) -> ActionJobExecutionContext:
    if step_row["step_type"] != "user_request" or step_row["status"] != "success":
        raise MigrationError(
            "Action continuation must reference a successful USER step"
        )
    message_json = step_row["user_message_json"]
    message_id = step_row["user_message_id"]
    if not isinstance(message_json, str) or not isinstance(message_id, str):
        raise MigrationError("Action USER step is missing its typed message")
    user_message = parse_action_user_message(message_json)
    if user_message.message_id != message_id:
        raise MigrationError("Action USER step message identity is inconsistent")
    if step_row["user_request_text"] != render_action_user_request_text(user_message):
        raise MigrationError("Action USER step rendered text is inconsistent")
    step_number = step_row["step_number"]
    local_step_number = step_row["local_step_number"]
    short_step_id = step_row["short_step_id"]
    if (
        isinstance(step_number, bool)
        or not isinstance(step_number, int)
        or step_number < 1
        or isinstance(local_step_number, bool)
        or not isinstance(local_step_number, int)
        or local_step_number < 1
        or short_step_id != f"S-{local_step_number}-USER"
    ):
        raise MigrationError("Action USER step position is inconsistent")
    raw_suggestion_id = action_row["suggestion_id"]
    suggestion_id = str(raw_suggestion_id) if raw_suggestion_id is not None else None
    approval = user_message.suggestion_approval
    is_initial_message = action_row["initial_user_message_id"] == message_id
    if is_initial_message and suggestion_id is not None and approval is None:
        raise MigrationError(
            "Suggestion-linked initial USER message lacks approval metadata"
        )
    if approval is not None and approval.suggestion_id != suggestion_id:
        raise MigrationError("Action suggestion provenance does not match USER message")
    created_at = str(step_row["created_at"])
    accepted_at = approval.approved_at if approval is not None else created_at
    return ActionJobExecutionContext(
        suggestion_id=suggestion_id,
        user_step_id=str(step_row["step_id"]),
        user_step_number=step_number,
        user_step_local_step_number=local_step_number,
        user_step_short_id=str(short_step_id),
        user_step_created_at=created_at,
        user_message=user_message,
        execution_target=execution_target,
        command_id=user_message.message_id,
        accepted_at=accepted_at,
        preceding_assistant_messages=preceding_assistant_messages,
        approval_session_id=approval_session_id,
        approval_tool_request_id=approval_tool_request_id,
        checkpoint_step_id=checkpoint_step_id,
    )


def _parse_execution_target(raw_json: object) -> ActionExecutionTarget:
    if not isinstance(raw_json, str):
        raise MigrationError("Action execution target is not durable JSON")
    try:
        decoded = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise MigrationError("Action execution target is invalid JSON") from exc
    if decoded != {"kind": "scratch"}:
        raise MigrationError(
            "Action execution target does not match the supported schema"
        )
    return ActionScratchExecutionTarget(kind="scratch")


def _skip_outcome_error_code(outcome: ActionStartSkipOutcome) -> str:
    if outcome == "already_processing":
        return ACTION_START_SKIP_ALREADY_PROCESSING_CODE
    return ACTION_START_SKIP_ALREADY_TERMINAL_CODE


__all__ = [
    "ACTION_START_SKIPPED_EVENT",
    "ActionJobExecutionContext",
    "ActionJobPreparation",
    "ActionJobRuntimeRepository",
    "ActionJobStartSkipCommand",
    "ActionStartSkipOutcome",
    "load_and_validate_action_command_user_step_in_connection",
    "load_and_validate_action_job_envelope_in_connection",
]
