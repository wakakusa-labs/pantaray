"""Suggestion provenance adapter for canonical Action message submission."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from pantaray_agents.local_runtime.storage.migrations import MigrationError
from pantaray_agents.local_runtime.suggestion_state.event_names import (
    EVENT_PROCESS_STARTED,
    EVENT_SUGGESTION_REACTION_COMMITTED,
)
from pantaray_agents.local_runtime.suggestion_state.public_process_events import (
    append_public_process_event,
)
from pantaray_agents.local_runtime.suggestion_state.public_projection import (
    rebuild_history_projection,
)
from pantaray_agents.schema.agent.action import ActionUserMessageInput

from .action_message_models import ActionMessageConflictError


@dataclass(frozen=True, slots=True)
class _SuggestionContext:
    suggestion_summary: str | None
    organization_name: str | None
    project_name: str | None


def validate_suggestion_action_start(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    suggestion_id: str,
    message: ActionUserMessageInput,
) -> None:
    """Validate that one persisted Suggestion can start exactly one Action."""

    approval = message.suggestion_approval
    if approval is None:
        raise ActionMessageConflictError(
            "suggestion_id requires typed Suggestion approval metadata"
        )
    suggestion_row = connection.execute(
        """
        SELECT
            status,
            has_suggestion,
            interaction_contract,
            user_reaction,
            answer,
            suggestion_summary,
            target_context_json
        FROM agent_suggestions
        WHERE user_id = ? AND suggestion_id = ?
        """,
        (user_id, suggestion_id),
    ).fetchone()
    if suggestion_row is None:
        raise ActionMessageConflictError("Suggestion not found")
    if (
        str(suggestion_row["status"]) != "success"
        or int(suggestion_row["has_suggestion"] or 0) != 1
        or str(suggestion_row["interaction_contract"] or "") != "action_offer"
        or suggestion_row["user_reaction"] is not None
    ):
        raise ActionMessageConflictError(
            "Suggestion is not available for Action creation"
        )
    if approval.suggestion_id != suggestion_id:
        raise ActionMessageConflictError(
            "Suggestion approval relation does not match suggestion_id"
        )
    if str(suggestion_row["answer"] or "") != message.content:
        raise ActionMessageConflictError(
            "Suggestion approval content does not match persisted Suggestion"
        )
    expected_context = _suggestion_context_from_row(suggestion_row)
    if (
        approval.summary != expected_context.suggestion_summary
        or approval.organization_name != expected_context.organization_name
        or approval.project_name != expected_context.project_name
    ):
        raise ActionMessageConflictError(
            "Suggestion approval metadata does not match persisted Suggestion"
        )


def project_suggestion_action_start(
    *,
    connection: sqlite3.Connection,
    event_id: str,
    action_id: str,
    process_id: str,
    user_id: str,
    suggestion_id: str,
    message: ActionUserMessageInput,
    created_at: str,
) -> None:
    """Project a validated Suggestion approval after the Action envelope exists."""

    approval = message.suggestion_approval
    if approval is None or approval.suggestion_id != suggestion_id:
        raise ActionMessageConflictError("Suggestion approval relation changed")
    cursor = connection.execute(
        """
        UPDATE agent_suggestions
        SET user_reaction = 'accepted',
            accepted_at = ?,
            rejected_at = NULL,
            action_status = 'idle',
            action_failure_code = NULL,
            action_failure_stage = NULL,
            action_failure_message_public = NULL,
            action_command_id = ?,
            action_process_id = ?,
            action_started_at = NULL,
            action_request_payload = NULL,
            updated_at = ?
        WHERE user_id = ? AND suggestion_id = ? AND user_reaction IS NULL
          AND EXISTS (
              SELECT 1
              FROM agent_actions AS actions
              WHERE actions.action_id = ?
                AND actions.user_id = agent_suggestions.user_id
                AND actions.suggestion_id = agent_suggestions.suggestion_id
          )
        """,
        (
            approval.approved_at,
            message.message_id,
            process_id,
            created_at,
            user_id,
            suggestion_id,
            action_id,
        ),
    )
    if cursor.rowcount != 1:
        raise ActionMessageConflictError("Suggestion acceptance state changed")

    append_public_process_event(
        connection=connection,
        event_id=event_id,
        suggestion_id=suggestion_id,
        user_id=user_id,
        action_id=None,
        event_name=EVENT_SUGGESTION_REACTION_COMMITTED,
        payload={
            "data": {
                "reaction": "accepted",
                "committed_at": approval.approved_at,
            },
            "meta": {"suggestion_id": suggestion_id},
        },
        created_at=created_at,
    )
    rebuild_history_projection(
        connection=connection,
        user_id=user_id,
        suggestion_id=suggestion_id,
    )


def project_suggestion_action_started(
    *,
    connection: sqlite3.Connection,
    event_id: str,
    action_id: str,
    process_id: str,
    user_id: str,
    suggestion_id: str,
    command_id: str,
    accepted_at: str,
    started_at: str,
) -> int:
    """Project one canonical Action claim into its optional Suggestion view."""

    cursor = connection.execute(
        """
        UPDATE agent_suggestions
        SET action_status = 'processing',
            action_command_id = ?,
            action_process_id = ?,
            action_started_at = ?,
            action_failure_code = NULL,
            action_failure_stage = NULL,
            action_failure_message_public = NULL,
            action_request_payload = NULL,
            updated_at = ?
        WHERE user_id = ? AND suggestion_id = ?
          AND user_reaction = 'accepted'
          AND EXISTS (
              SELECT 1
              FROM agent_actions AS actions
              WHERE actions.action_id = ?
                AND actions.user_id = agent_suggestions.user_id
                AND actions.suggestion_id = agent_suggestions.suggestion_id
          )
        """,
        (
            command_id,
            process_id,
            started_at,
            started_at,
            user_id,
            suggestion_id,
            action_id,
        ),
    )
    if cursor.rowcount != 1:
        raise MigrationError("Suggestion projection does not own the claimed Action")

    sequence = append_public_process_event(
        connection=connection,
        event_id=event_id,
        suggestion_id=suggestion_id,
        user_id=user_id,
        action_id=action_id,
        event_name=EVENT_PROCESS_STARTED,
        payload={
            "data": {
                "kind": "action",
                "process_id": process_id,
                "suggestion_id": suggestion_id,
                "action_id": action_id,
                "command_id": command_id,
                "accepted_at": accepted_at,
                "started_at": started_at,
            },
            "meta": {
                "kind": "action",
                "suggestion_id": suggestion_id,
                "action_id": action_id,
                "command_id": command_id,
                "process_id": process_id,
            },
        },
        created_at=started_at,
    )
    rebuild_history_projection(
        connection=connection,
        user_id=user_id,
        suggestion_id=suggestion_id,
    )
    return sequence


def _suggestion_context_from_row(row: sqlite3.Row) -> _SuggestionContext:
    raw_target_context = row["target_context_json"]
    if raw_target_context is None:
        target_context: object = {}
    elif isinstance(raw_target_context, str):
        try:
            target_context = json.loads(raw_target_context)
        except json.JSONDecodeError as exc:
            raise MigrationError("Suggestion target_context_json is invalid") from exc
    else:
        raise MigrationError("Suggestion target_context_json must be JSON text")
    if not isinstance(target_context, dict):
        raise MigrationError("Suggestion target_context_json must be a JSON object")
    return _SuggestionContext(
        suggestion_summary=_optional_text(row["suggestion_summary"]),
        organization_name=_optional_text(target_context.get("organization_name")),
        project_name=_optional_text(target_context.get("project_name")),
    )


def _optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return value.strip() or None


__all__ = [
    "ActionMessageConflictError",
    "project_suggestion_action_start",
    "project_suggestion_action_started",
    "validate_suggestion_action_start",
]
