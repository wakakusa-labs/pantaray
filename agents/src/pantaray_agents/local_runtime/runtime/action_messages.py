"""Canonical transactional boundary for submitting USER messages to Actions."""

from __future__ import annotations

import sqlite3
import uuid
from typing import Literal, cast

from pantaray_agents.local_runtime.storage.migrations import MigrationError
from pantaray_agents.local_runtime.storage.migrations.connection import (
    configure_connection,
)
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction
from pantaray_agents.local_runtime.tooling.models import ApprovalMode
from pantaray_agents.local_runtime.tooling.repository.action_approval_modes import (
    set_action_approval_mode_in_connection,
)
from pantaray_agents.schema.action_conversation import (
    ActionMessageAcceptedEventData,
    ActionMessageAdoptedEventData,
    ActionStatus,
)
from pantaray_agents.tasks.action_job_support import (
    ACTION_HEADER_PROMPT_NAME,
    ACTION_HEADER_PROMPT_VERSION,
)
from pantaray_agents.tasks.action_user_message import (
    render_action_user_request_text,
    serialize_action_user_message,
)

from .action_file_attachments import ActionFileAttachmentLinks
from .action_invalidation_events import append_action_invalidation_event
from .action_message_models import (
    ACTION_RESUME_STEP_NAME,
    ACTION_USER_STEP_NAME,
    ActionMessageConflictError,
    ActionMessageSubmissionError,
    ActionNotFoundError,
    DeferredActionMessageResult,
    ExistingActionTarget,
    ExpectedProcessConflictError,
    MessageIdentityConflictError,
    NewActionTarget,
    StartedActionMessageResult,
    SubmitActionMessageCommand,
    SubmitActionMessageResult,
    _StepPosition,
)
from .action_message_process_disposition import (
    ActionProcessDispositionResult,
    classify_action_process_disposition_in_connection,
)
from .action_message_replay import (
    ACTION_MESSAGE_STATUSES,
    _load_existing_submission,
    _validate_idempotent_replay,
)
from .action_prepare_failure_followup import (
    require_prepare_failure_followup_authority,
)
from .action_queue import build_local_action_enqueue_request
from .action_resumability import action_latest_run_has_restorable_checkpoint
from .action_suggestion_adapter import (
    project_suggestion_action_start,
    validate_suggestion_action_start,
)
from .action_suggestion_reply import insert_suggestion_reply_message
from .identity import verify_current_owner
from .job_enqueue import enqueue_local_job
from .job_payload_builder import build_action_job_payload
from .runtime_env import (
    read_local_runtime_artifact_root,
    read_local_runtime_db_config,
)
from .utc_timestamps import now_utc_iso

ACTION_QUEUED_STATUS = "queued"
ACTION_USER_STEP_TYPE = "user_request"
ACTION_USER_STEP_STATUS = "success"
ACTION_SUPERVISOR_SCOPE = "S"
ACTION_SCRATCH_EXECUTION_TARGET_JSON = '{"kind":"scratch"}'


def submit_action_message(
    command: SubmitActionMessageCommand,
) -> SubmitActionMessageResult:
    """Persist one USER turn and start or defer execution transactionally."""

    verify_current_owner(command.user_id)
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    created_at = now_utc_iso()
    user_message_json = serialize_action_user_message(command.message)

    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        configure_connection(connection, busy_timeout_ms)
        with (
            ActionFileAttachmentLinks() as attachment_links,
            immediate_transaction(connection),
        ):
            existing = _load_existing_submission(
                connection=connection,
                command=command,
            )
            if existing is not None:
                _validate_idempotent_replay(
                    existing=existing,
                    command=command,
                    user_message_json=user_message_json,
                )
                return existing.result

            action_id, suggestion_id, action_status, process_disposition = (
                _resolve_action_target(
                    connection=connection,
                    command=command,
                    created_at=created_at,
                )
            )
            starts_run = process_disposition is None or (
                process_disposition.disposition in {"idle", "normal_terminal"}
            )
            process_id = str(uuid.uuid4()) if starts_run else None
            job_id = str(uuid.uuid4()) if starts_run else None
            user_step_id = str(uuid.uuid4())
            if starts_run:
                assert process_id is not None and job_id is not None
                _enqueue_action_message_job(
                    connection=connection,
                    action_id=action_id,
                    process_id=process_id,
                    job_id=job_id,
                    user_step_id=user_step_id,
                    user_id=command.user_id,
                    suggestion_id=suggestion_id,
                    created_at=created_at,
                )
                if (
                    isinstance(command.target, NewActionTarget)
                    and command.target.reply_to_suggestion_id is not None
                ):
                    insert_suggestion_reply_message(
                        connection=connection,
                        user_id=command.user_id,
                        action_id=action_id,
                        process_id=process_id,
                        suggestion_id=command.target.reply_to_suggestion_id,
                    )

            position = (
                _next_user_step_position(connection=connection, action_id=action_id)
                if starts_run
                else None
            )

            expected_process_id = (
                command.target.expected_process_id
                if isinstance(command.target, ExistingActionTarget)
                else None
            )
            adoption_canceled_at = (
                created_at
                if process_disposition is not None
                and process_disposition.disposition == "canceled"
                else None
            )
            accepted_sequence = _next_action_acceptance_sequence(
                connection=connection,
                action_id=action_id,
            )
            _insert_user_step(
                connection=connection,
                action_id=action_id,
                user_step_id=user_step_id,
                command=command,
                accepted_sequence=accepted_sequence,
                position=position,
                adoption_canceled_at=adoption_canceled_at,
                expected_process_id=expected_process_id,
                adopted_process_id=process_id,
                user_message_json=user_message_json,
                created_at=created_at,
            )
            connection.execute(
                """UPDATE agent_actions SET updated_at = ?
                WHERE user_id = ? AND action_id = ? AND updated_at < ?""",
                (created_at, command.user_id, action_id, created_at),
            )
            append_action_invalidation_event(
                connection=connection,
                event_id=str(uuid.uuid4()),
                user_id=command.user_id,
                data=ActionMessageAcceptedEventData(
                    action_id=action_id,
                    message_id=command.message.message_id,
                    step_id=user_step_id,
                ),
                created_at=created_at,
            )
            if starts_run:
                assert process_id is not None
                append_action_invalidation_event(
                    connection=connection,
                    event_id=str(uuid.uuid4()),
                    user_id=command.user_id,
                    data=ActionMessageAdoptedEventData(
                        action_id=action_id,
                        message_id=command.message.message_id,
                        step_id=user_step_id,
                        process_id=process_id,
                    ),
                    created_at=created_at,
                )
            if isinstance(command.target, NewActionTarget):
                if suggestion_id is not None:
                    assert process_id is not None
                    project_suggestion_action_start(
                        connection=connection,
                        event_id=str(uuid.uuid4()),
                        action_id=action_id,
                        process_id=process_id,
                        user_id=command.user_id,
                        suggestion_id=suggestion_id,
                        message=command.message,
                        created_at=created_at,
                    )
            if command.message.files:
                attachment_links.link(
                    connection=connection,
                    db_path=db_path,
                    artifact_root=read_local_runtime_artifact_root(),
                    user_id=command.user_id,
                    action_id=action_id,
                    files=command.message.files,
                )

    if starts_run:
        assert process_id is not None and job_id is not None
        return StartedActionMessageResult(
            "started",
            action_id,
            command.message.message_id,
            user_step_id,
            action_status,
            process_id,
            job_id,
            True,
        )
    disposition: Literal["pending", "not_executed"] = (
        "not_executed"
        if process_disposition is not None
        and process_disposition.disposition == "canceled"
        else "pending"
    )
    return DeferredActionMessageResult(
        disposition,
        action_id,
        command.message.message_id,
        user_step_id,
        action_status,
        None,
        None,
        True,
    )


def _resolve_action_target(
    *,
    connection: sqlite3.Connection,
    command: SubmitActionMessageCommand,
    created_at: str,
) -> tuple[
    str,
    str | None,
    ActionStatus,
    ActionProcessDispositionResult | None,
]:
    target = command.target
    if isinstance(target, NewActionTarget):
        if target.suggestion_id is None:
            if command.message.suggestion_approval is not None:
                raise ActionMessageConflictError(
                    "Suggestion approval metadata requires suggestion_id"
                )
        else:
            validate_suggestion_action_start(
                connection=connection,
                user_id=command.user_id,
                suggestion_id=target.suggestion_id,
                message=command.message,
            )
        action_id = str(uuid.uuid4())
        _insert_action(
            connection=connection,
            action_id=action_id,
            user_id=command.user_id,
            suggestion_id=target.suggestion_id,
            initial_message_id=command.message.message_id,
            initial_approval_mode=target.approval_mode,
            created_at=created_at,
        )
        if target.approval_mode is not None:
            set_action_approval_mode_in_connection(
                connection=connection,
                user_id=command.user_id,
                action_id=action_id,
                approval_mode=target.approval_mode,
                updated_at=created_at,
            )
        return action_id, target.suggestion_id, "queued", None

    row = connection.execute(
        """
        SELECT actions.suggestion_id, actions.status, actions.error
        FROM agent_actions AS actions
        WHERE actions.user_id = ? AND actions.action_id = ?
        """,
        (command.user_id, target.action_id),
    ).fetchone()
    if row is None:
        raise ActionNotFoundError("Action not found")
    if command.message.suggestion_approval is not None:
        raise ActionMessageConflictError(
            "Suggestion approval metadata is valid only for a new Action"
        )
    raw_status = row["status"]
    if raw_status not in ACTION_MESSAGE_STATUSES:
        raise MigrationError("Existing Action status is invalid")
    action_status = cast(ActionStatus, raw_status)
    raw_suggestion_id = row["suggestion_id"]
    suggestion_id = str(raw_suggestion_id) if raw_suggestion_id is not None else None
    process_disposition = classify_action_process_disposition_in_connection(
        connection=connection,
        user_id=command.user_id,
        action_id=target.action_id,
        expected_process_id=target.expected_process_id,
    )
    if process_disposition.disposition == "stale":
        raise ExpectedProcessConflictError("Expected Action process is stale")
    required_status = process_disposition.required_action_status
    if required_status is not None and action_status != required_status:
        raise MigrationError("Action status does not match its canonical process leaf")
    if command.origin == "resume" and action_status != "canceled":
        raise ActionMessageConflictError("Only a stopped Action can be resumed")
    if (
        process_disposition.disposition in {"idle", "normal_terminal"}
        and action_status in {"success", "error", "canceled"}
        and not action_latest_run_has_restorable_checkpoint(
            connection=connection,
            user_id=command.user_id,
            action_id=target.action_id,
        )
    ):
        if action_status != "error":
            raise ActionMessageConflictError(
                "Action cannot accept a USER message without a resumable checkpoint"
            )
        require_prepare_failure_followup_authority(
            connection=connection,
            user_id=command.user_id,
            action_id=target.action_id,
            suggestion_id=suggestion_id,
            action_error_json=row["error"],
        )
    if process_disposition.disposition in {"idle", "normal_terminal"}:
        connection.execute(
            """
            UPDATE agent_actions
            SET status = 'queued', final_output = '', error = NULL,
                final_prompt_text = NULL, updated_at = ?
            WHERE user_id = ? AND action_id = ?
            """,
            (created_at, command.user_id, target.action_id),
        )
        action_status = "queued"
    return target.action_id, suggestion_id, action_status, process_disposition


def _next_user_step_position(
    *, connection: sqlite3.Connection, action_id: str
) -> _StepPosition:
    counters = connection.execute(
        """
        SELECT
            COALESCE(MAX(step_number), 0) AS max_step_number,
            COALESCE(MAX(CASE WHEN goal_handle = 'S' THEN local_step_number END), 0)
                AS max_local_step_number
        FROM agent_action_steps
        WHERE action_id = ?
        """,
        (action_id,),
    ).fetchone()
    if counters is None:
        raise MigrationError("Failed to load Action step counters")
    step_number = int(counters["max_step_number"]) + 1
    local_step_number = int(counters["max_local_step_number"]) + 1
    parent = connection.execute(
        """
        SELECT step_id
        FROM agent_action_steps
        WHERE action_id = ? AND goal_handle = 'S'
        ORDER BY step_number DESC, local_step_number DESC,
                 completed_at DESC, created_at DESC, step_id DESC
        LIMIT 1
        """,
        (action_id,),
    ).fetchone()
    return _StepPosition(
        step_number=step_number,
        local_step_number=local_step_number,
        short_step_id=f"S-{local_step_number}-USER",
        parent_step_id=str(parent["step_id"]) if parent is not None else None,
    )


def _next_action_acceptance_sequence(
    *, connection: sqlite3.Connection, action_id: str
) -> int:
    row = connection.execute(
        """
        SELECT COALESCE(MAX(candidate.sequence), 0) AS max_sequence
        FROM (
            SELECT accepted_sequence AS sequence
            FROM agent_action_steps
            WHERE action_id = ? AND accepted_sequence IS NOT NULL
            UNION ALL
            SELECT sequence
            FROM agent_process_events
            WHERE action_id = ?
        ) AS candidate
        """,
        (action_id, action_id),
    ).fetchone()
    if row is None:
        raise MigrationError("Failed to allocate Action acceptance sequence")
    return int(row["max_sequence"]) + 1


def _insert_action(
    *,
    connection: sqlite3.Connection,
    action_id: str,
    user_id: str,
    suggestion_id: str | None,
    initial_message_id: str,
    initial_approval_mode: ApprovalMode | None,
    created_at: str,
) -> None:
    connection.execute(
        """
        INSERT INTO agent_actions(
            action_id, user_id, suggestion_id, initial_user_message_id,
            execution_target_json, status, final_output, prompt_name,
            prompt_version, created_at, updated_at, initial_approval_mode
        ) VALUES (?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?)
        """,
        (
            action_id,
            user_id,
            suggestion_id,
            initial_message_id,
            ACTION_SCRATCH_EXECUTION_TARGET_JSON,
            ACTION_QUEUED_STATUS,
            ACTION_HEADER_PROMPT_NAME,
            ACTION_HEADER_PROMPT_VERSION,
            created_at,
            created_at,
            initial_approval_mode,
        ),
    )


def _insert_user_step(
    *,
    connection: sqlite3.Connection,
    action_id: str,
    user_step_id: str,
    command: SubmitActionMessageCommand,
    accepted_sequence: int,
    position: _StepPosition | None,
    adoption_canceled_at: str | None,
    expected_process_id: str | None,
    adopted_process_id: str | None,
    user_message_json: str,
    created_at: str,
) -> None:
    connection.execute(
        """
        INSERT INTO agent_action_steps(
            step_id, action_id, user_id, parent_step_id, step_number,
            local_step_number, short_step_id, step_type, step_name, status,
            goal_handle, retry_count, prompt_tokens, completion_tokens,
            user_message_id, user_message_json, user_request_text, accepted_sequence,
            adoption_canceled_at, expected_process_id, adopted_process_id,
            started_at, completed_at, created_at
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        """,
        (
            user_step_id,
            action_id,
            command.user_id,
            position.parent_step_id if position is not None else None,
            position.step_number if position is not None else None,
            position.local_step_number if position is not None else None,
            position.short_step_id if position is not None else None,
            ACTION_USER_STEP_TYPE,
            ACTION_RESUME_STEP_NAME
            if command.origin == "resume"
            else ACTION_USER_STEP_NAME,
            ACTION_USER_STEP_STATUS,
            ACTION_SUPERVISOR_SCOPE,
            command.message.message_id,
            user_message_json,
            render_action_user_request_text(command.message),
            accepted_sequence,
            adoption_canceled_at,
            expected_process_id,
            adopted_process_id,
            created_at,
            created_at,
            created_at,
        ),
    )


def _enqueue_action_message_job(
    *,
    connection: sqlite3.Connection,
    action_id: str,
    process_id: str,
    job_id: str,
    user_step_id: str,
    user_id: str,
    suggestion_id: str | None,
    created_at: str,
) -> None:
    payload = build_action_job_payload(
        {
            "job_id": job_id,
            "process_id": process_id,
            "action_id": action_id,
            "user_id": user_id,
            "continuation_ref": {
                "kind": "user_step",
                "user_step_id": user_step_id,
            },
        }
    )
    enqueue_result = enqueue_local_job(
        connection=connection,
        request=build_local_action_enqueue_request(
            payload,
            scheduled_at=created_at,
            suggestion_id=suggestion_id,
        ),
    )
    if not enqueue_result["inserted_new"]:
        raise ActionMessageConflictError(
            "Action already has an active USER message execution"
        )


__all__ = [
    "ActionMessageConflictError",
    "ActionMessageSubmissionError",
    "ActionNotFoundError",
    "DeferredActionMessageResult",
    "ExistingActionTarget",
    "ExpectedProcessConflictError",
    "MessageIdentityConflictError",
    "NewActionTarget",
    "StartedActionMessageResult",
    "SubmitActionMessageCommand",
    "SubmitActionMessageResult",
    "submit_action_message",
]
