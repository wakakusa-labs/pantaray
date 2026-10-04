"""The recent Suggestions a new Suggestion run reads, with what became of them."""

from __future__ import annotations

import json
import sqlite3

from pydantic import ValidationError

from pantaray_agents.schema.agent.action_message_codec import (
    parse_action_user_message,
)
from pantaray_agents.schema.repositories.repository import DBRow

_RECENT_SUGGESTION_HISTORY_SQL = """
        WITH recent AS (
            SELECT suggestions.user_id, suggestions.answer,
                   suggestions.created_at, suggestions.user_reaction,
                   suggestions.delivery_state,
                   (
                       SELECT actions.action_id
                       FROM agent_actions AS actions
                       WHERE actions.user_id = suggestions.user_id
                         AND (
                             actions.suggestion_id = suggestions.suggestion_id
                             OR EXISTS (
                                 SELECT 1 FROM agent_action_steps AS reply
                                 WHERE reply.user_id = actions.user_id
                                   AND reply.action_id = actions.action_id
                                   AND reply.source_suggestion_id
                                       = suggestions.suggestion_id
                             )
                         )
                       ORDER BY actions.created_at DESC LIMIT 1
                   ) AS action_id
            FROM agent_suggestions AS suggestions
            WHERE suggestions.user_id = ?
              -- The held one is still waiting to be shown; one that ended
              -- unshown was never seen, so a newer run may propose it again.
              AND suggestions.delivery_state IN ('held', 'released')
              -- The welcome greets; it proposed nothing to build on.
              AND suggestions.prompt_name IS NOT ?
              AND suggestions.created_at >= ?
            ORDER BY suggestions.created_at DESC
            LIMIT ?
        )
        SELECT recent.answer, recent.created_at, recent.user_reaction,
               recent.delivery_state,
               actions.status AS action_status,
               initial.user_message_json AS user_reply_json,
               -- Later turns the user sent in the same Action, such as
               -- narrowing its scope. A reply to another Suggestion
               -- belongs to that Suggestion's entry.
               (
                   SELECT json_group_array(later.user_message_json)
                   FROM (
                       SELECT turn.user_message_json
                       FROM agent_action_steps AS turn
                       WHERE turn.user_id = actions.user_id
                         AND turn.action_id = actions.action_id
                         AND turn.step_type = 'user_request'
                         AND turn.step_number IS NOT NULL
                         AND turn.user_message_json IS NOT NULL
                         AND turn.source_suggestion_id IS NULL
                         AND turn.user_message_id
                             IS NOT actions.initial_user_message_id
                       ORDER BY turn.step_number
                   ) AS later
               ) AS followups_json,
               json_extract(result_event.payload_json, '$.final_output')
                   AS action_result
        FROM recent
        LEFT JOIN agent_actions AS actions
          ON actions.user_id = recent.user_id
         AND actions.action_id = recent.action_id
        LEFT JOIN agent_action_steps AS initial
          ON initial.user_id = actions.user_id
         AND initial.action_id = actions.action_id
         AND initial.user_message_id = actions.initial_user_message_id
        -- A follow-up turn resets agent_actions.final_output, so the latest
        -- successful run's public terminal is the durable result.
        LEFT JOIN processes AS result_process
          ON result_process.process_id = (
              SELECT turn.adopted_process_id
              FROM agent_action_steps AS turn
              JOIN processes AS process
                ON process.process_id = turn.adopted_process_id
               AND process.user_id = turn.user_id
               AND process.kind = 'action'
               AND process.status = 'completed'
              JOIN process_events AS event
                ON event.process_id = process.process_id
               AND event.event_id = process.terminal_event_id
               AND event.event_name = 'stream_end'
              WHERE turn.user_id = actions.user_id
                AND turn.action_id = actions.action_id
                AND turn.step_type = 'user_request'
                AND turn.status = 'success'
                AND turn.step_number IS NOT NULL
                AND turn.adopted_process_id IS NOT NULL
                AND json_type(event.payload_json, '$.final_output') = 'text'
                AND json_type(event.payload_json, '$.physical_run_only') IS NULL
              ORDER BY turn.step_number DESC, turn.step_id DESC
              LIMIT 1
          )
        LEFT JOIN process_events AS result_event
          ON result_event.process_id = result_process.process_id
         AND result_event.event_id = result_process.terminal_event_id
        ORDER BY recent.created_at DESC
"""


def load_recent_suggestion_history(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    excluded_prompt_name: str,
    cutoff_iso: str,
    limit: int,
) -> list[DBRow]:
    rows = connection.execute(
        _RECENT_SUGGESTION_HISTORY_SQL,
        (user_id, excluded_prompt_name, cutoff_iso, limit),
    ).fetchall()
    history: list[DBRow] = []
    for row in rows:
        entry: DBRow = dict(row)
        raw_reply = entry.pop("user_reply_json")
        try:
            message = (
                parse_action_user_message(str(raw_reply))
                if raw_reply is not None
                else None
            )
        except ValidationError:
            # Do not include private reply content in the worker's error log.
            raise ValueError(
                "Stored Suggestion reply has an invalid message schema"
            ) from None
        entry["action_followups"] = _action_followups(
            str(entry.pop("followups_json") or "[]")
        )
        if message is None:
            entry["user_reply"] = None
        elif message.suggestion_approval is not None:
            # An approval repeats the Suggestion answer as its content; only
            # the supplement is what the user wrote.
            entry["user_reply"] = message.supplement
        else:
            entry["user_reply"] = message.content
        history.append(entry)
    return history


def _action_followups(raw: str) -> list[str]:
    """Contents of the user's later Action turns, repeated sends collapsed."""
    followups: list[str] = []
    for item in json.loads(raw):
        try:
            content = parse_action_user_message(str(item)).content.strip()
        except ValidationError:
            # Do not include private message content in the worker's error log.
            raise ValueError(
                "Stored Action turn has an invalid message schema"
            ) from None
        if content and (not followups or followups[-1] != content):
            followups.append(content)
    return followups
