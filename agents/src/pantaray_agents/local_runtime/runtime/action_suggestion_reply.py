"""Snapshot the assistant message a new Action opens with, before its first USER.

A Suggestion the user answers in words opens this way. A message-only Suggestion is
answered so, and so is an offer the user dismissed: writing after dismissing it asks
for something else. The dismissal stays as recorded. A TODO the AI wrote opens this
way too when Pantaray picks it up: the TODO is the AI's words, and the USER row that
follows is Pantaray's, not the user's.
"""

import sqlite3
import uuid

from .action_message_models import (
    ACTION_HOST_STEP_NAMES_SQL,
    ActionMessageConflictError,
    NewActionTarget,
)


def opening_assistant_text_sql(action: str) -> str:
    """SQL for the assistant message the Action aliased ``action`` opens with, or NULL.

    Only an opening written here is ever an Action's first numbered step.
    """

    return f"""(SELECT CASE WHEN opening.step_type='assistant_message'
                 THEN opening.llm_response_text END
      FROM agent_action_steps AS opening
        INDEXED BY idx_agent_action_steps_action_timeline
      WHERE opening.action_id={action}.action_id AND opening.user_id={action}.user_id
        AND opening.step_number IS NOT NULL
        AND opening.step_type IN ('user_request','assistant_message','tool_execution')
      ORDER BY opening.step_number,opening.step_id LIMIT 1)"""


def action_request_text_sql(*, action: str, request: str) -> str:
    """SQL for what the Action aliased ``action`` was asked, in words.

    That is the text of its initial USER row, aliased ``request``, unless
    Pantaray wrote that row: then it is the TODO the Action opens with.
    """

    return f"""(CASE WHEN {request}.step_name IN {ACTION_HOST_STEP_NAMES_SQL}
      THEN {opening_assistant_text_sql(action)}
      ELSE {request}.user_request_text END)"""


def insert_opening_assistant_message(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    action_id: str,
    process_id: str,
    target: NewActionTarget,
    created_at: str,
) -> None:
    """Join the first USER's transaction after its process exists, before numbering it."""
    if target.reply_to_suggestion_id is not None:
        _insert_suggestion_reply_message(
            connection=connection,
            user_id=user_id,
            action_id=action_id,
            process_id=process_id,
            suggestion_id=target.reply_to_suggestion_id,
        )
    elif target.todo_text is not None:
        _insert_opening_step(
            connection=connection,
            user_id=user_id,
            action_id=action_id,
            process_id=process_id,
            text=target.todo_text,
            source_suggestion_id=None,
            written_at=created_at,
        )


def _insert_suggestion_reply_message(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    action_id: str,
    process_id: str,
    suggestion_id: str,
) -> None:
    source = connection.execute(
        """SELECT answer,created_at FROM agent_suggestions
        WHERE user_id=? AND suggestion_id=? AND status='success' AND has_suggestion=1
          AND (interaction_contract='message_only'
               OR (interaction_contract='action_offer' AND user_reaction='rejected'))
          AND LENGTH(TRIM(answer))>0""",
        (user_id, suggestion_id),
    ).fetchone()
    if source is None:
        raise ActionMessageConflictError("Suggestion is not available for reply")
    if not _insert_opening_step(
        connection=connection,
        user_id=user_id,
        action_id=action_id,
        process_id=process_id,
        text=source["answer"],
        source_suggestion_id=suggestion_id,
        written_at=source["created_at"],
    ):
        raise ActionMessageConflictError("Suggestion already has a reply conversation")


def _insert_opening_step(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    action_id: str,
    process_id: str,
    text: str,
    source_suggestion_id: str | None,
    written_at: str,
) -> bool:
    inserted = connection.execute(
        """INSERT INTO agent_action_steps(
            step_id,action_id,user_id,step_number,local_step_number,short_step_id,
            step_type,step_name,status,goal_handle,llm_response_text,
            adopted_process_id,source_suggestion_id,retry_count,prompt_tokens,
            completion_tokens,started_at,completed_at,created_at)
        VALUES (?,?,?,1,1,'S-1-ASSISTANT','assistant_message','assistant_message',
                'success','S',?,?,?,0,0,0,?,?,?)
        ON CONFLICT(user_id,source_suggestion_id) WHERE source_suggestion_id IS NOT NULL
        DO NOTHING""",
        (
            str(uuid.uuid4()),
            action_id,
            user_id,
            text,
            process_id,
            source_suggestion_id,
            written_at,
            written_at,
            written_at,
        ),
    )
    return inserted.rowcount == 1
