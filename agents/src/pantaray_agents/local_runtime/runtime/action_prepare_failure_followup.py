"""Exact admission proof for checkpoint-free Action prepare failures."""

from __future__ import annotations

import sqlite3

from pydantic import ValidationError

from pantaray_agents.local_runtime.storage.migrations import MigrationError
from pantaray_agents.repositories.action_runtime_resume_contract import (
    ActionRuntimeResumeContractError,
    parse_action_resume_user_step,
)
from pantaray_agents.schema.agent.base import AgentError

from .action_message_models import ActionMessageConflictError
from .action_message_process_fence import (
    resolve_action_process_lineage_in_connection,
)


def require_prepare_failure_followup_authority(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    action_id: str,
    suggestion_id: str | None,
    action_error_json: object,
) -> None:
    row = connection.execute(
        """
        SELECT steps.step_id, steps.user_id, steps.action_id, steps.accepted_sequence,
               steps.step_number, steps.local_step_number, steps.short_step_id,
               steps.step_type, steps.status, steps.user_message_id, steps.user_message_json,
               steps.user_request_text, steps.adopted_process_id, steps.created_at,
               processes.status AS process_status, jobs.status AS job_status, jobs.job_id
        FROM agent_action_steps AS steps
        JOIN processes ON processes.process_id = steps.adopted_process_id
        JOIN jobs ON jobs.process_id = processes.process_id
        WHERE steps.user_id = ? AND steps.action_id = ?
          AND steps.step_type = 'user_request' AND steps.status = 'success'
        ORDER BY steps.accepted_sequence DESC, steps.step_number DESC, steps.step_id DESC
        LIMIT 1
        """,
        (user_id, action_id),
    ).fetchone()
    if row is None:
        raise MigrationError("Action prepare failure root USER is missing")
    try:
        parse_action_resume_user_step(
            dict(row), expected_user_id=user_id, expected_action_id=action_id
        )
    except ActionRuntimeResumeContractError as exc:
        raise MigrationError(str(exc)) from exc
    adopted_process_id = row["adopted_process_id"]
    accepted_sequence = row["accepted_sequence"]
    if not isinstance(adopted_process_id, str) or not isinstance(
        accepted_sequence, int
    ):
        raise MigrationError("Action prepare failure root identity is invalid")

    lineage = resolve_action_process_lineage_in_connection(
        connection=connection,
        user_id=user_id,
        action_id=action_id,
        process_id=adopted_process_id,
    )
    if (
        lineage.root_accepted_sequence != accepted_sequence
        or row["job_id"] != lineage.job_id
    ):
        raise MigrationError("Action prepare failure process is not a root run")
    if (row["process_status"], row["job_status"]) != ("failed", "failed"):
        raise ActionMessageConflictError(
            "Action terminal run is not an exact prepare failure"
        )

    if not isinstance(action_error_json, str):
        raise MigrationError("Action prepare failure error payload is missing")
    try:
        error = AgentError.model_validate_json(action_error_json)
    except ValidationError as exc:
        raise MigrationError("Action prepare failure error payload is invalid") from exc
    details = error.error_details
    metadata = error.metadata
    if not isinstance(details, dict) or details.get("stage") != "prepare":
        raise ActionMessageConflictError(
            "Action terminal failure was not prepare-stage"
        )
    if not isinstance(metadata, dict) or (
        metadata.get("user_id"),
        metadata.get("action_id"),
        metadata.get("suggestion_id"),
    ) != (user_id, action_id, suggestion_id):
        raise MigrationError("Action prepare failure identity is inconsistent")
    # Reached only without a checkpoint this build restores (the predicate in
    # action_resumability admits every other follow-up), so the next run starts
    # from nothing and replays this one USER turn.
    user_count, has_checkpoint = connection.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM agent_action_steps
             WHERE user_id = :user_id AND action_id = :action_id
               AND step_type = 'user_request' AND status = 'success'
               AND step_number IS NOT NULL),
            EXISTS (SELECT 1 FROM agent_action_steps
                    WHERE user_id = :user_id AND action_id = :action_id
                      AND runtime_state_checkpoint IS NOT NULL)
        """,
        {"user_id": user_id, "action_id": action_id},
    ).fetchone()
    if user_count != 1 or has_checkpoint:
        raise ActionMessageConflictError(
            "Action prepare failure has unreconstructable USER history"
        )
