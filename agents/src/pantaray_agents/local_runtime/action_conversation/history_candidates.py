"""Bounded candidate selection for the conversation history list."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import ClassVar, Final, Literal, cast

from pantaray_agents.local_runtime.runtime.action_message_models import (
    ACTION_RESUME_STEP_NAME,
)
from pantaray_agents.local_runtime.storage.migrations import MigrationError
from pantaray_agents.schema.conversation_history import ConversationHistoryStatus

from .cursor_codec import OpaqueCursorError
from .history_cursor import (
    ConversationHistoryCursor,
    decode_conversation_history_cursor,
    encode_conversation_history_cursor,
)
from .history_queries import ActionHistoryUserRow


@dataclass(frozen=True, slots=True)
class ActionHistoryCandidate:
    action_id: str
    suggestion_id: str | None
    action_status: str
    suggestion_user_id: str | None
    suggestion_interaction_contract: str | None
    suggestion_user_reaction: str | None
    initial_message_id: str
    initial_user: ActionHistoryUserRow | None
    latest_run_id: str | None
    status_hint: ConversationHistoryStatus
    latest_completion_event_id: str | None
    matched_user: ActionHistoryUserRow | None
    matched_terminal_root_run_id: str | None
    updated_at: str
    matched_assistant_text: str | None
    kind: ClassVar[Literal["conversation"]] = "conversation"

    @property
    def stable_id(self) -> str:
        return self.action_id


@dataclass(frozen=True, slots=True)
class SuggestionHistoryCandidate:
    suggestion_id: str
    answer: str | None
    interaction_contract: str | None
    user_reaction: str | None
    status_hint: ConversationHistoryStatus
    updated_at: str
    kind: ClassVar[Literal["suggestion"]] = "suggestion"

    @property
    def stable_id(self) -> str:
        return self.suggestion_id


type ConversationHistoryCandidate = ActionHistoryCandidate | SuggestionHistoryCandidate

CONVERSATION_HISTORY_CASEFOLD_SQL_FUNCTION: Final[str] = (
    "pantaray_conversation_history_casefold"
)


def _casefold_text(value: str) -> str:
    return value.casefold()


def register_conversation_history_casefold_sqlite(
    connection: sqlite3.Connection,
) -> None:
    """Register Unicode case folding for this connection's lifetime."""

    connection.create_function(
        CONVERSATION_HISTORY_CASEFOLD_SQL_FUNCTION,
        1,
        _casefold_text,
        deterministic=True,
    )


@dataclass(frozen=True, slots=True)
class ConversationHistoryCandidatePage:
    candidates: tuple[ConversationHistoryCandidate, ...]
    next_cursor: str | None


def _candidate_query(*, cursor_bound: bool) -> str:
    cursor_predicate = (
        """AND (raw_updated_at<:before_updated_at
      OR (raw_updated_at=:before_updated_at
        AND (kind,stable_id)>(:before_kind,:before_id)))"""
        if cursor_bound
        else ""
    )
    return rf"""
WITH action_source AS (
  SELECT action.action_id,action.suggestion_id,action.status,
    action.initial_user_message_id,
    action.updated_at AS action_updated_at,
    suggestion.user_id,suggestion.interaction_contract,suggestion.user_reaction,
    initial.step_id,initial.step_number,initial.accepted_sequence,initial.user_message_id,
    initial.user_message_json,initial.user_request_text,initial.adopted_process_id,
    initial.expected_process_id,initial.adoption_canceled_at,initial.step_name,
    latest_run.adopted_process_id AS latest_run_id,process.status AS process_status,
    job.status AS job_status,process.terminal_event_id,
    matched_user.step_id AS matched_user_step_id,
    matched_user.step_number AS matched_user_step_number,
    matched_user.accepted_sequence AS matched_user_accepted_sequence,
    matched_user.user_message_id AS matched_user_message_id,
    matched_user.user_message_json AS matched_user_message_json,
    matched_user.user_request_text AS matched_user_request_text,
    matched_user.adopted_process_id AS matched_user_adopted_process_id,
    matched_user.expected_process_id AS matched_user_expected_process_id,
    matched_user.adoption_canceled_at AS matched_user_adoption_canceled_at,
    matched_user.step_name AS matched_user_step_name,
    (SELECT terminal_user.adopted_process_id
      FROM agent_action_steps AS terminal_user
      JOIN processes AS terminal_process
        ON terminal_process.process_id=terminal_user.adopted_process_id
       AND terminal_process.user_id=terminal_user.user_id AND terminal_process.kind='action'
       AND terminal_process.action_id=terminal_user.action_id
      JOIN jobs AS terminal_job INDEXED BY idx_jobs_process_id
        ON terminal_job.process_id=terminal_process.process_id
       AND terminal_job.user_id=terminal_process.user_id
       AND terminal_job.job_type='execute_action'
      JOIN process_events AS terminal_event
        ON terminal_event.process_id=terminal_process.process_id
       AND terminal_event.event_id=terminal_process.terminal_event_id
       AND terminal_event.event_name='stream_end'
      WHERE :search_text<>'' AND matched_user.step_id IS NULL
        AND terminal_user.user_id=action.user_id
        AND terminal_user.action_id=action.action_id
        AND terminal_user.step_type='user_request' AND terminal_user.status='success'
        AND terminal_user.adopted_process_id IS NOT NULL
        AND terminal_user.accepted_sequence IS NOT NULL
        AND ((terminal_process.status='completed' AND terminal_job.status='completed')
          OR (terminal_process.status='failed' AND terminal_job.status='failed')
          OR (terminal_process.status='canceled' AND terminal_job.status='canceled'))
        AND json_type(terminal_event.payload_json,'$.final_output')='text'
        AND {CONVERSATION_HISTORY_CASEFOLD_SQL_FUNCTION}(
              json_extract(terminal_event.payload_json,'$.final_output'))
            LIKE :search_pattern ESCAPE '\'
      ORDER BY terminal_user.accepted_sequence DESC,terminal_user.step_id DESC
      LIMIT 1) AS matched_terminal_root_run_id,
    (SELECT assistant.llm_response_text FROM agent_action_steps AS assistant
      WHERE :search_text<>'' AND assistant.user_id=action.user_id
        AND assistant.action_id=action.action_id AND assistant.step_type='assistant_message'
        AND {CONVERSATION_HISTORY_CASEFOLD_SQL_FUNCTION}(assistant.llm_response_text)
          LIKE :search_pattern ESCAPE '\'
      ORDER BY assistant.step_number DESC LIMIT 1) AS matched_assistant_text
  FROM agent_actions AS action INDEXED BY idx_agent_actions_user_history_recency
  LEFT JOIN agent_suggestions AS suggestion
    ON suggestion.suggestion_id=action.suggestion_id
  LEFT JOIN agent_action_steps AS initial ON initial.step_id=(
    SELECT step_id FROM agent_action_steps WHERE user_id=action.user_id
      AND action_id=action.action_id AND user_message_id=action.initial_user_message_id
      AND step_type='user_request' AND status='success'
    ORDER BY step_id LIMIT 1)
  LEFT JOIN agent_action_steps AS latest_run ON latest_run.step_id=(
    SELECT step_id FROM agent_action_steps WHERE user_id=action.user_id
      AND action_id=action.action_id AND step_type='user_request' AND status='success'
      AND adopted_process_id IS NOT NULL AND step_number IS NOT NULL
    ORDER BY step_number DESC,step_id DESC LIMIT 1)
  LEFT JOIN processes AS process ON process.process_id=latest_run.adopted_process_id
  LEFT JOIN jobs AS job ON job.job_id=(SELECT job_id
    FROM jobs INDEXED BY idx_jobs_process_id
    WHERE process_id=process.process_id AND job_type='execute_action'
    ORDER BY job_id LIMIT 1)
  LEFT JOIN agent_action_steps AS matched_user ON matched_user.step_id=(
    SELECT step_id FROM agent_action_steps AS searched_user
    WHERE :search_text<>'' AND searched_user.user_id=action.user_id
      AND searched_user.action_id=action.action_id
      AND searched_user.step_type='user_request' AND searched_user.status='success'
      AND searched_user.step_name<>:hidden_step_name
      AND searched_user.accepted_sequence IS NOT NULL
      AND ((searched_user.user_message_id IS NULL AND searched_user.user_message_json IS NULL
            AND {CONVERSATION_HISTORY_CASEFOLD_SQL_FUNCTION}(
                  searched_user.user_request_text) LIKE :search_pattern ESCAPE '\')
        OR (searched_user.user_message_id IS NOT NULL
            AND searched_user.user_message_json IS NOT NULL
            AND ({CONVERSATION_HISTORY_CASEFOLD_SQL_FUNCTION}(CASE WHEN json_type(searched_user.user_message_json,'$.content')='text'
                       THEN json_extract(searched_user.user_message_json,'$.content') ELSE '' END)
                   LIKE :search_pattern ESCAPE '\'
              OR {CONVERSATION_HISTORY_CASEFOLD_SQL_FUNCTION}(CASE WHEN json_type(searched_user.user_message_json,'$.supplement')='text'
                       THEN json_extract(searched_user.user_message_json,'$.supplement') ELSE '' END)
                   LIKE :search_pattern ESCAPE '\')))
    ORDER BY searched_user.accepted_sequence DESC,searched_user.step_id DESC LIMIT 1)
  WHERE action.user_id=:user_id
), action_evidence AS (
  SELECT 'conversation' AS kind,action_id AS stable_id,
    action_updated_at AS raw_updated_at,
    CASE WHEN (process_status='enqueued' AND job_status='queued')
              OR (process_status='running' AND job_status='running')
         THEN 'running' WHEN process_status='paused' AND job_status='paused'
         THEN 'approval_pending' ELSE 'idle' END AS status_hint,
    action_id,suggestion_id,status,initial_user_message_id,user_id,
    interaction_contract,user_reaction,step_id,step_number,accepted_sequence,
    user_message_id,user_message_json,user_request_text,adopted_process_id,
    expected_process_id,adoption_canceled_at,latest_run_id,terminal_event_id,
    NULL AS answer,
    matched_user_step_id,matched_user_step_number,matched_user_accepted_sequence,
    matched_user_message_id,matched_user_message_json,matched_user_request_text,
    matched_user_adopted_process_id,matched_user_expected_process_id,
    matched_user_adoption_canceled_at,matched_terminal_root_run_id,
    matched_user_step_id IS NOT NULL OR matched_terminal_root_run_id IS NOT NULL
      OR matched_assistant_text IS NOT NULL AS search_match,
    step_name,matched_user_step_name,
    CASE WHEN matched_user_step_id IS NULL AND matched_terminal_root_run_id IS NULL
      THEN matched_assistant_text END AS matched_assistant_text
  FROM action_source
), suggestion_evidence AS (
  SELECT 'suggestion' AS kind,suggestion.suggestion_id AS stable_id,
    suggestion.updated_at AS raw_updated_at,
    CASE WHEN suggestion.interaction_contract='action_offer'
              AND suggestion.user_reaction IS NULL
         THEN 'approval_pending' ELSE 'idle' END AS status_hint,
    NULL AS action_id,suggestion.suggestion_id,suggestion.status,
    NULL AS initial_user_message_id,suggestion.user_id,
    suggestion.interaction_contract,suggestion.user_reaction,
    NULL AS step_id,NULL AS step_number,NULL AS accepted_sequence,
    NULL AS user_message_id,NULL AS user_message_json,NULL AS user_request_text,
    NULL AS adopted_process_id,NULL AS expected_process_id,
    NULL AS adoption_canceled_at,NULL AS latest_run_id,NULL AS terminal_event_id,
    suggestion.answer,
    NULL AS matched_user_step_id,NULL AS matched_user_step_number,
    NULL AS matched_user_accepted_sequence,NULL AS matched_user_message_id,
    NULL AS matched_user_message_json,NULL AS matched_user_request_text,
    NULL AS matched_user_adopted_process_id,NULL AS matched_user_expected_process_id,
    NULL AS matched_user_adoption_canceled_at,NULL AS matched_terminal_root_run_id,
    typeof(suggestion.answer)='text'
      AND {CONVERSATION_HISTORY_CASEFOLD_SQL_FUNCTION}(
        CASE WHEN typeof(suggestion.answer)='text' THEN suggestion.answer ELSE '' END)
        LIKE :search_pattern ESCAPE '\' AS search_match,
    NULL AS step_name,NULL AS matched_user_step_name,NULL AS matched_assistant_text
  FROM agent_suggestions AS suggestion
    INDEXED BY idx_agent_suggestions_user_history_recency
  LEFT JOIN agent_actions AS linked
    ON linked.suggestion_id=suggestion.suggestion_id
  WHERE suggestion.user_id=:user_id AND suggestion.delivery_state='released'
    AND linked.action_id IS NULL
    AND NOT EXISTS (SELECT 1 FROM agent_action_steps AS reply
      WHERE reply.user_id=suggestion.user_id
        AND reply.source_suggestion_id=suggestion.suggestion_id)
), action_lane AS (
  SELECT * FROM action_evidence
  WHERE (:search_text='' OR search_match)
    {cursor_predicate}
  ORDER BY raw_updated_at DESC,stable_id LIMIT :fetch_limit
), suggestion_lane AS (
  SELECT * FROM suggestion_evidence
  WHERE (:search_text='' OR search_match)
    {cursor_predicate}
  ORDER BY raw_updated_at DESC,stable_id LIMIT :fetch_limit
), candidates AS (
  SELECT * FROM action_lane UNION ALL SELECT * FROM suggestion_lane
)
SELECT kind,stable_id,raw_updated_at,status_hint,action_id,suggestion_id,status,
 initial_user_message_id,user_id,interaction_contract,user_reaction,step_id,step_number,
 accepted_sequence,user_message_id,user_message_json,user_request_text,
 adopted_process_id,expected_process_id,adoption_canceled_at,latest_run_id,
 terminal_event_id,answer,
 matched_user_step_id,matched_user_step_number,matched_user_accepted_sequence,
 matched_user_message_id,matched_user_message_json,matched_user_request_text,
 matched_user_adopted_process_id,matched_user_expected_process_id,
 matched_user_adoption_canceled_at,matched_terminal_root_run_id,
 step_name,matched_user_step_name,matched_assistant_text
FROM candidates
ORDER BY raw_updated_at DESC,kind,stable_id LIMIT :fetch_limit
"""


def read_conversation_history_candidates_in_connection(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    search_text: str,
    cursor: str | None,
    limit: int,
) -> ConversationHistoryCandidatePage:
    """Select a page after the caller registers this module's casefold function."""

    if not connection.in_transaction:
        raise MigrationError("Conversation history requires a caller-owned transaction")
    if limit <= 0:
        raise ValueError("Conversation history limit must be positive")
    normalized_search = search_text.strip().casefold()
    anchor = decode_conversation_history_cursor(cursor) if cursor is not None else None
    if anchor is not None and (
        anchor.user_id != user_id or anchor.search_text != normalized_search
    ):
        raise OpaqueCursorError("cursor is bound to another history query")
    escaped_search = (
        normalized_search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
    rows = connection.execute(
        _candidate_query(cursor_bound=anchor is not None),
        {
            "user_id": user_id,
            "search_text": normalized_search,
            "search_pattern": f"%{escaped_search}%",
            "hidden_step_name": ACTION_RESUME_STEP_NAME,
            "before_updated_at": anchor.updated_at if anchor else None,
            "before_kind": anchor.kind if anchor else None,
            "before_id": anchor.stable_id if anchor else None,
            "fetch_limit": limit + 1,
        },
    ).fetchall()
    candidates = tuple(_candidate(tuple(row)) for row in rows[:limit])
    next_cursor = None
    if len(rows) > limit:
        last = candidates[-1]
        next_cursor = encode_conversation_history_cursor(
            ConversationHistoryCursor(
                user_id=user_id,
                search_text=normalized_search,
                updated_at=last.updated_at,
                kind=last.kind,
                stable_id=last.stable_id,
            )
        )
    return ConversationHistoryCandidatePage(candidates, next_cursor)


def _candidate(row: tuple[object, ...]) -> ConversationHistoryCandidate:
    if row[0] == "suggestion":
        return SuggestionHistoryCandidate(
            suggestion_id=cast("str", row[1]),
            answer=cast("str | None", row[22]),
            interaction_contract=cast("str | None", row[9]),
            user_reaction=cast("str | None", row[10]),
            status_hint=cast("ConversationHistoryStatus", row[3]),
            updated_at=cast("str", row[2]),
        )
    initial_user = None
    if row[11] is not None:
        initial_user = ActionHistoryUserRow(
            step_id=cast("str", row[11]),
            step_number=cast("int | None", row[12]),
            accepted_sequence=cast("int", row[13]),
            user_message_id=cast("str | None", row[14]),
            user_message_json=cast("str | None", row[15]),
            user_request_text=cast("str", row[16]),
            adopted_process_id=cast("str | None", row[17]),
            expected_process_id=cast("str | None", row[18]),
            adoption_canceled_at=cast("str | None", row[19]),
            step_name=cast("str", row[33]),
        )
    matched_user = None
    if row[23] is not None:
        matched_user = ActionHistoryUserRow(
            step_id=cast("str", row[23]),
            step_number=cast("int | None", row[24]),
            accepted_sequence=cast("int", row[25]),
            user_message_id=cast("str | None", row[26]),
            user_message_json=cast("str | None", row[27]),
            user_request_text=cast("str", row[28]),
            adopted_process_id=cast("str | None", row[29]),
            expected_process_id=cast("str | None", row[30]),
            adoption_canceled_at=cast("str | None", row[31]),
            step_name=cast("str", row[34]),
        )
    return ActionHistoryCandidate(
        action_id=cast("str", row[4]),
        suggestion_id=cast("str | None", row[5]),
        action_status=cast("str", row[6]),
        suggestion_user_id=cast("str | None", row[8]),
        suggestion_interaction_contract=cast("str | None", row[9]),
        suggestion_user_reaction=cast("str | None", row[10]),
        initial_message_id=cast("str", row[7]),
        initial_user=initial_user,
        latest_run_id=cast("str | None", row[20]),
        status_hint=cast("ConversationHistoryStatus", row[3]),
        latest_completion_event_id=cast("str | None", row[21]),
        matched_user=matched_user,
        matched_terminal_root_run_id=cast("str | None", row[32]),
        updated_at=cast("str", row[2]),
        matched_assistant_text=cast("str | None", row[35]),
    )


__all__ = [
    "ActionHistoryCandidate",
    "ConversationHistoryCandidate",
    "ConversationHistoryCandidatePage",
    "SuggestionHistoryCandidate",
    "read_conversation_history_candidates_in_connection",
    "register_conversation_history_casefold_sqlite",
]
