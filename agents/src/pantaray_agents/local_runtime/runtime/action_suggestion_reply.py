"""Snapshot a Suggestion the user answers in words as the first assistant message.

A message-only Suggestion is answered this way, and so is an offer the user dismissed:
writing after dismissing it asks for something else. The dismissal stays as recorded.
"""

import sqlite3
import uuid

from .action_message_models import ActionMessageConflictError


def insert_suggestion_reply_message(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    action_id: str,
    process_id: str,
    suggestion_id: str,
) -> None:
    """Join the first USER's transaction after its process exists, before numbering it."""
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
            source["answer"],
            process_id,
            suggestion_id,
            source["created_at"],
            source["created_at"],
            source["created_at"],
        ),
    )

    if inserted.rowcount != 1:
        raise ActionMessageConflictError("Suggestion already has a reply conversation")
