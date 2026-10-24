"""Read-only projection for replaying an existing Action USER submission."""

from __future__ import annotations

import sqlite3
from typing import cast

from pantaray_agents.local_runtime.storage.migrations import MigrationError
from pantaray_agents.local_runtime.tooling.models import ApprovalMode
from pantaray_agents.schema.action_conversation import ActionStatus
from pantaray_agents.tasks.action_user_message import parse_action_user_message

from .action_message_models import (
    DeferredActionMessageResult,
    ExistingActionTarget,
    MessageIdentityConflictError,
    NewActionTarget,
    StartedActionMessageResult,
    SubmitActionMessageCommand,
    SubmitActionMessageResult,
    _ExistingSubmission,
)
from .action_message_process_fence import (
    resolve_action_process_lineage_in_connection,
)
from .job_payload_models import parse_action_job_payload_json

ACTION_MESSAGE_STATUSES = frozenset(
    {"queued", "processing", "success", "error", "canceled"}
)


def _load_existing_submission(
    *,
    connection: sqlite3.Connection,
    command: SubmitActionMessageCommand,
) -> _ExistingSubmission | None:
    row = connection.execute(
        """
        SELECT actions.action_id, actions.suggestion_id, actions.status AS action_status,
               actions.initial_user_message_id, actions.initial_approval_mode,
               steps.step_id, steps.user_message_id, steps.user_message_json,
               steps.step_number, steps.adoption_canceled_at, steps.expected_process_id,
               steps.adopted_process_id,
               assistant.source_suggestion_id AS reply_to_suggestion_id
        FROM agent_action_steps AS steps
        JOIN agent_actions AS actions ON actions.action_id = steps.action_id
        LEFT JOIN agent_action_steps AS assistant
          ON assistant.action_id = actions.action_id AND assistant.user_id = actions.user_id
         AND assistant.step_type = 'assistant_message' AND assistant.short_step_id = 'S-1-ASSISTANT'
        WHERE steps.user_id = ? AND steps.user_message_id = ?
        """,
        (command.user_id, command.message.message_id),
    ).fetchone()
    if row is None:
        return None
    action_created = row["initial_user_message_id"] == command.message.message_id
    user_message_json = row["user_message_json"]
    if not isinstance(user_message_json, str):
        raise MigrationError("Existing Action USER message envelope is incomplete")
    action_status = row["action_status"]
    if action_status not in ACTION_MESSAGE_STATUSES:
        raise MigrationError("Existing Action status is invalid")
    result = _project_existing_submission_result(
        connection=connection,
        user_id=command.user_id,
        action_id=str(row["action_id"]),
        message_id=str(row["user_message_id"]),
        user_step_id=str(row["step_id"]),
        action_status=cast(ActionStatus, action_status),
        has_step_identity=row["step_number"] is not None,
        adoption_canceled=row["adoption_canceled_at"] is not None,
        adopted_process_id=(
            str(row["adopted_process_id"])
            if row["adopted_process_id"] is not None
            else None
        ),
    )
    raw_suggestion_id = row["suggestion_id"]
    raw_reply_to_suggestion_id = row["reply_to_suggestion_id"]
    return _ExistingSubmission(
        result=result,
        action_created=action_created,
        initial_approval_mode=cast(ApprovalMode | None, row["initial_approval_mode"]),
        suggestion_id=str(raw_suggestion_id) if raw_suggestion_id is not None else None,
        reply_to_suggestion_id=(
            str(raw_reply_to_suggestion_id)
            if raw_reply_to_suggestion_id is not None
            else None
        ),
        expected_process_id=(
            str(row["expected_process_id"])
            if row["expected_process_id"] is not None
            else None
        ),
        user_message_json=user_message_json,
    )


def _project_existing_submission_result(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    action_id: str,
    message_id: str,
    user_step_id: str,
    action_status: ActionStatus,
    has_step_identity: bool,
    adoption_canceled: bool,
    adopted_process_id: str | None,
) -> SubmitActionMessageResult:
    common = (action_id, message_id, user_step_id, action_status)
    if adoption_canceled:
        return DeferredActionMessageResult("not_executed", *common, None, None, False)
    if adopted_process_id is not None:
        lineage = resolve_action_process_lineage_in_connection(
            connection=connection,
            user_id=user_id,
            action_id=action_id,
            process_id=adopted_process_id,
        )
        return StartedActionMessageResult(
            "started", *common, lineage.root_process_id, lineage.job_id, False
        )
    if not has_step_identity:
        return DeferredActionMessageResult("pending", *common, None, None, False)

    # Expand-window rows predate durable adoption but retain their originating job.
    rows = connection.execute(
        """
        SELECT jobs.job_id, jobs.process_id, payloads.payload_json
        FROM jobs
        JOIN job_payloads AS payloads ON payloads.job_id = jobs.job_id
        WHERE jobs.job_type = 'execute_action' AND jobs.logical_key = ?
        ORDER BY jobs.scheduled_at ASC, jobs.job_id ASC
        """,
        (action_id,),
    ).fetchall()
    for row in rows:
        payload = parse_action_job_payload_json(str(row["payload_json"]))
        continuation = payload["continuation_ref"]
        if (
            continuation["kind"] == "user_step"
            and continuation["user_step_id"] == user_step_id
        ):
            return StartedActionMessageResult(
                "started", *common, str(row["process_id"]), str(row["job_id"]), False
            )
    raise MigrationError("Existing Action message job envelope is incomplete")


def _validate_idempotent_replay(
    *,
    existing: _ExistingSubmission,
    command: SubmitActionMessageCommand,
    user_message_json: str,
) -> None:
    target = command.target
    if isinstance(target, NewActionTarget) and not existing.action_created:
        raise MessageIdentityConflictError(
            "message_id already belongs to an existing Action turn"
        )
    if isinstance(target, ExistingActionTarget) and (
        existing.action_created or existing.result.action_id != target.action_id
    ):
        raise MessageIdentityConflictError(
            "message_id already belongs to a different Action target"
        )
    if (
        isinstance(target, ExistingActionTarget)
        and existing.expected_process_id != target.expected_process_id
    ):
        raise MessageIdentityConflictError(
            "message_id already belongs to a different expected process"
        )
    if (
        isinstance(target, NewActionTarget)
        and existing.initial_approval_mode != target.approval_mode
    ):
        raise MessageIdentityConflictError(
            "message_id already belongs to a different initial approval mode"
        )
    if (
        isinstance(target, NewActionTarget)
        and existing.reply_to_suggestion_id != target.reply_to_suggestion_id
    ):
        raise MessageIdentityConflictError(
            "message_id already belongs to a different reply origin"
        )
    expected_suggestion_id = (
        target.suggestion_id
        if isinstance(target, NewActionTarget)
        else existing.suggestion_id
    )
    if existing.suggestion_id != expected_suggestion_id:
        raise MessageIdentityConflictError(
            "message_id already belongs to a different suggestion relation"
        )
    stored = parse_action_user_message(existing.user_message_json)
    sent = parse_action_user_message(user_message_json)
    # The Suggestion files are the ones this boundary copied, not what was sent.
    if stored.model_copy(update={"suggestion_files": ()}) != sent:
        raise MessageIdentityConflictError(
            "message_id already belongs to different USER message content"
        )
