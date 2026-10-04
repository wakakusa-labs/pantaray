from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from pantaray_agents.action_status import build_finalize_action_terminal_command
from pantaray_agents.local_runtime.action_conversation.history_candidates import (
    register_conversation_history_casefold_sqlite,
)
from pantaray_agents.local_runtime.action_conversation.history_repository import (
    read_conversation_history_page_in_connection,
)
from pantaray_agents.local_runtime.runtime.job_payload_builder import (
    build_action_job_payload,
)
from pantaray_agents.schema.agent.action_message import (
    ActionUserMessageInput,
    SuggestionApprovalInput,
)
from pantaray_agents.schema.agent.action_message_codec import (
    render_action_user_request_text,
    serialize_action_user_message,
)
from pantaray_agents.schema.conversation_history import ConversationHistoryPage

from .local_action_repository_support import bootstrap_action_repository_db

_USER_ID = "user-1"
_PAUSED_AT = "2026-08-30T00:03:00.000Z"
_EXTRA_AT = "2026-08-30T00:02:45.000Z"
_TERMINAL_STARTED_AT = "2026-08-30T00:02:00.000Z"
_TERMINAL_RECENCY_AT = "2026-08-30T00:02:30.000Z"
_SUGGESTION_AT = "2026-08-30T00:01:00.000Z"
_ROOT_STARTED_AT = "2026-08-30T00:10:00.000Z"
_LEAF_COMPLETED_AT = "2026-08-30T00:11:00.000Z"


def _read(
    connection: sqlite3.Connection,
    search_text: str = "",
    *,
    cursor: str | None = None,
    limit: int = 10,
) -> tuple[ConversationHistoryPage, int]:
    selects: list[str] = []

    def trace(statement: str) -> None:
        if statement.lstrip().upper().startswith(("SELECT", "WITH")):
            selects.append(statement)

    connection.set_trace_callback(trace)
    try:
        page = read_conversation_history_page_in_connection(
            connection=connection,
            user_id=_USER_ID,
            search_text=search_text,
            cursor=cursor,
            limit=limit,
        )
    finally:
        connection.set_trace_callback(None)
    return page, len(selects)


def test_composes_candidates_and_bulk_authority_into_public_history(
    tmp_path: Path,
) -> None:
    linked_message = ActionUserMessageInput(
        message_id="message-linked",
        content="Linked USER title CAFÉ",
        suggestion_approval=SuggestionApprovalInput(
            suggestion_id="sug-1", approved_at=_PAUSED_AT
        ),
    )
    extra_message = ActionUserMessageInput(
        message_id="message-extra", content="Extra USER title"
    )
    terminal_message = ActionUserMessageInput(
        message_id="message-terminal", content="Terminal USER title"
    )
    terminal = build_finalize_action_terminal_command(
        process_completed_event_id="event-terminal",
        suggestion_id=None,
        user_id=_USER_ID,
        command_id=terminal_message.message_id,
        process_id="run-terminal",
        action_id="action-terminal",
        accepted_at=_TERMINAL_STARTED_AT,
        completed_at=_TERMINAL_RECENCY_AT,
        action_status="success",
        final_output="Terminal Needle",
    )
    terminal_payload = dict(terminal.process_completed_payload["data"])
    terminal_payload["persisted_sequence"] = 7

    with sqlite3.connect(bootstrap_action_repository_db(tmp_path)) as connection:
        connection.row_factory = sqlite3.Row
        register_conversation_history_casefold_sqlite(connection)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(
            f"""
            UPDATE agent_suggestions SET answer='Linked answer is deduplicated',
              user_reaction='accepted',created_at='{_PAUSED_AT}',updated_at='{_PAUSED_AT}'
            WHERE suggestion_id='sug-1';
            INSERT INTO agent_suggestions(suggestion_id,user_id,status,answer,
              has_suggestion,interaction_contract,user_reaction,delivery_state,
              created_at,updated_at)
            VALUES ('suggestion-pending','{_USER_ID}','success','Pending Suggestion ÜBER',
              1,'action_offer',NULL,'released','{_SUGGESTION_AT}','{_SUGGESTION_AT}');
            UPDATE agent_actions SET initial_user_message_id='{linked_message.message_id}',
              status='processing',created_at='{_PAUSED_AT}',updated_at='{_PAUSED_AT}'
            WHERE action_id='act-1';
            INSERT INTO agent_actions(action_id,user_id,suggestion_id,
              initial_user_message_id,execution_target_json,status,final_output,
              prompt_name,prompt_version,created_at,updated_at)
            VALUES ('action-terminal','{_USER_ID}',NULL,'{terminal_message.message_id}',
              '{{"kind":"scratch"}}','success','Terminal Needle','test','1',
              '{_TERMINAL_STARTED_AT}','{_TERMINAL_RECENCY_AT}'),
              ('action-extra','{_USER_ID}',NULL,'{extra_message.message_id}',
               '{{"kind":"scratch"}}','processing','','test','1',
               '{_EXTRA_AT}','{_EXTRA_AT}');
            INSERT INTO processes(process_id,user_id,kind,status,action_id,started_at,
              updated_at,completed_at,heartbeat_at,terminal_event_id,next_event_seq) VALUES
              ('run-paused','{_USER_ID}','action','paused','act-1','{_PAUSED_AT}',
               '{_PAUSED_AT}',NULL,'{_PAUSED_AT}',NULL,1),
              ('run-extra','{_USER_ID}','action','paused','action-extra','{_EXTRA_AT}',
               '{_EXTRA_AT}',NULL,'{_EXTRA_AT}',NULL,1),
              ('run-terminal','{_USER_ID}','action','completed','action-terminal',
               '{_TERMINAL_STARTED_AT}','{terminal.completed_at}',
               '{terminal.completed_at}','{terminal.completed_at}','event-terminal',2);
            INSERT INTO jobs(job_id,user_id,job_type,process_id,status,scheduled_at,
              started_at,completed_at,logical_key) VALUES
              ('job-paused','{_USER_ID}','execute_action','run-paused','paused',
               '{_PAUSED_AT}','{_PAUSED_AT}',NULL,'act-1'),
              ('job-extra','{_USER_ID}','execute_action','run-extra','paused',
               '{_EXTRA_AT}','{_EXTRA_AT}',NULL,'action-extra'),
              ('job-terminal','{_USER_ID}','execute_action','run-terminal','completed',
               '{_TERMINAL_STARTED_AT}','{_TERMINAL_STARTED_AT}',
               '{terminal.completed_at}','action-terminal');
            """
        )
        connection.executemany(
            "INSERT INTO job_payloads(job_id,payload_json) VALUES (?,?)",
            tuple(
                (
                    job_id,
                    json.dumps(
                        build_action_job_payload(
                            {
                                "job_id": job_id,
                                "process_id": run_id,
                                "action_id": action_id,
                                "user_id": _USER_ID,
                                "continuation_ref": {
                                    "kind": "user_step",
                                    "user_step_id": step_id,
                                },
                            }
                        )
                    ),
                )
                for job_id, run_id, action_id, step_id in (
                    ("job-paused", "run-paused", "act-1", "step-linked"),
                    ("job-extra", "run-extra", "action-extra", "step-extra"),
                    (
                        "job-terminal",
                        "run-terminal",
                        "action-terminal",
                        "step-terminal",
                    ),
                )
            ),
        )
        connection.executemany(
            """INSERT INTO agent_action_steps(
                   step_id,action_id,user_id,step_number,local_step_number,short_step_id,
                   step_type,step_name,status,goal_handle,user_message_id,user_message_json,
                   user_request_text,accepted_sequence,adopted_process_id,started_at,
                   completed_at,created_at)
               VALUES (?,?,?,1,1,'S-1-USER','user_request','user_request','success','S',
                       ?,?,?,?,?,?,?,?)""",
            tuple(
                (
                    step_id,
                    action_id,
                    _USER_ID,
                    message.message_id,
                    serialize_action_user_message(message),
                    render_action_user_request_text(message),
                    1,
                    run_id,
                    created_at,
                    created_at,
                    created_at,
                )
                for step_id, action_id, run_id, message, created_at in (
                    (
                        "step-linked",
                        "act-1",
                        "run-paused",
                        linked_message,
                        _PAUSED_AT,
                    ),
                    (
                        "step-extra",
                        "action-extra",
                        "run-extra",
                        extra_message,
                        _EXTRA_AT,
                    ),
                    (
                        "step-terminal",
                        "action-terminal",
                        "run-terminal",
                        terminal_message,
                        _TERMINAL_STARTED_AT,
                    ),
                )
            ),
        )
        connection.execute(
            """INSERT INTO process_events(
                   process_id,event_seq,event_id,event_name,payload_json,created_at)
               VALUES ('run-terminal',1,'event-terminal','stream_end',?,?)""",
            (json.dumps(terminal_payload), terminal.completed_at),
        )
        connection.commit()
        connection.execute("BEGIN")

        cursor_page, _ = _read(connection, limit=2)
        assert cursor_page.next_cursor is not None
        single_action_page, single_action_selects = _read(
            connection,
            cursor=cursor_page.next_cursor,
            limit=1,
        )
        assert [(item.kind, item.title) for item in single_action_page.items] == [
            ("conversation", "Terminal USER title")
        ]

        first_page, first_selects = _read(connection, limit=3)
        assert first_page.next_cursor is not None
        second_page, _ = _read(
            connection,
            cursor=first_page.next_cursor,
            limit=3,
        )
        assert second_page.next_cursor is None
        items = first_page.items + second_page.items
        assert [(item.kind, item.title, item.status) for item in items] == [
            ("conversation", "Linked USER title CAFÉ", "approval_pending"),
            ("conversation", "Extra USER title", "approval_pending"),
            ("conversation", "Terminal USER title", "idle"),
            ("suggestion", "Pending Suggestion ÜBER", "approval_pending"),
        ]
        assert items[2].model_dump()["latest_completion_event_id"] == ("event-terminal")
        assert first_selects == single_action_selects

        for search_text, expected_ids in (
            ("café", ("act-1",)),
            ("terminal needle", ("action-terminal",)),
            ("über", ("suggestion-pending",)),
        ):
            result, _ = _read(connection, search_text)
            assert (
                tuple(
                    item.action_id
                    if item.kind == "conversation"
                    else item.suggestion_id
                    for item in result.items
                )
                == expected_ids
            )
