from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from pantaray_agents.local_runtime.runtime.action_approval_review import (
    ActionRequestRow,
    action_request_words,
    read_chat_words,
)
from pantaray_agents.local_runtime.runtime.action_message_models import (
    ACTION_RESUME_REQUEST_TEXT,
    ACTION_RESUME_STEP_NAME,
    ACTION_TODO_PICKUP_REQUEST_TEXT,
    ACTION_TODO_PICKUP_STEP_NAME,
    ACTION_USER_STEP_NAME,
)
from pantaray_agents.local_runtime.storage.migrations import load_default_migrations
from pantaray_agents.local_runtime.tooling.approval_review import (
    RequestTextNotByUser,
)
from pantaray_agents.schema.agent.action_message import (
    ActionUserMessageInput,
    ChatHandoffInput,
    SuggestionApprovalInput,
)
from pantaray_agents.schema.agent.action_message_codec import (
    render_action_user_request_text,
)

from .migrated_db import prepare_test_database


def _user(
    content: str, *, step_name: str = ACTION_USER_STEP_NAME, **fields: object
) -> ActionRequestRow:
    message = ActionUserMessageInput(message_id="m", content=content, **fields)  # type: ignore[arg-type]
    return ActionRequestRow(
        "user_request",
        step_name,
        message.message_id,
        message.model_dump_json(),
        render_action_user_request_text(message),
        None,
        None,
        "t",
    )


def _opening(text: str, *, suggestion_id: str | None) -> ActionRequestRow:
    return ActionRequestRow(
        "assistant_message",
        "assistant_message",
        None,
        None,
        None,
        text,
        suggestion_id,
        "t",
    )


def _no_chat(_row: ActionRequestRow) -> tuple[str, ...]:
    raise AssertionError("only a chat instruction reads the chat")


def test_only_the_users_own_words_are_the_users_messages() -> None:
    rows = (
        _opening("Draft the reply to the vendor.", suggestion_id=None),
        _user(ACTION_TODO_PICKUP_REQUEST_TEXT, step_name=ACTION_TODO_PICKUP_STEP_NAME),
        _user("Send it to me first."),
        # The chat wrote this instruction itself: the user's words are in the chat.
        _user(
            "Upload the draft to the shared drive.",
            chat_handoff=ChatHandoffInput(relayed_item_ids=()),
        ),
        _user(ACTION_RESUME_REQUEST_TEXT, step_name=ACTION_RESUME_STEP_NAME),
    )

    assert action_request_words(rows, lambda _row: ("Put it on the drive.",)) == (
        ("Send it to me first.", "Put it on the drive."),
        RequestTextNotByUser(source="todo", text="Draft the reply to the vendor."),
    )


def test_a_suggestion_the_user_accepted_or_answered_is_labelled_as_such() -> None:
    accepted = _user(
        "Summarize this week's discussion.",
        suggestion_approval=SuggestionApprovalInput(
            suggestion_id="s-1", approved_at="2026-11-17T00:00:00Z"
        ),
        supplement="Keep it to five lines.",
    )
    answered = (
        _opening("Shall I file the expense report?", suggestion_id="s-2"),
        _user("Yes, file it."),
    )

    assert action_request_words((accepted,), _no_chat) == (
        ("Keep it to five lines.",),
        RequestTextNotByUser(
            source="suggestion", text="Summarize this week's discussion."
        ),
    )
    assert action_request_words(answered, _no_chat) == (
        ("Yes, file it.",),
        RequestTextNotByUser(
            source="suggestion", text="Shall I file the expense report?"
        ),
    )


def test_a_chat_hand_off_reads_the_users_messages_since_the_last_one(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(
        db_path=db_path, busy_timeout_ms=1_000, migrations=load_default_migrations()
    )
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            "INSERT INTO users(user_id, ui_language, created_at, updated_at) "
            "VALUES ('user-1', 'ja', 't', 't');"
            "INSERT INTO agent_actions(action_id, user_id, initial_user_message_id, "
            "execution_target_json, status, final_output, prompt_name, "
            "prompt_version, created_at, updated_at) VALUES ('action-1', 'user-1', "
            "'m-1', '{\"kind\":\"scratch\"}', 'processing', '', 'p', 'v1', 't', 't');"
        )
        connection.execute(
            "INSERT INTO processes(process_id, user_id, kind, status, action_id, "
            "started_at, updated_at, completed_at, heartbeat_at, next_event_seq) "
            "VALUES ('process-1', 'user-1', 'action', 'completed', 'action-1', "
            "'t', 't', 't', 't', 1)"
        )
        for sequence, kind, text, at in (
            (1, "user_message", "Fix the typo in the README.", "t1"),
            (3, "user_message", "Export the report as a PDF.", "t3"),
            (4, "assistant_message", "I will export it now.", "t4"),
            (5, "user_message", "Put it on my Desktop.", "t5"),
            (7, "user_message", "Thanks.", "t7"),
        ):
            payload = {"kind": kind, "text": text, "quote_item_id": None}
            connection.execute(
                "INSERT INTO chat_items(sequence, item_id, user_id, message_id, kind, "
                "payload, created_at) VALUES (?, ?, 'user-1', ?, ?, ?, ?)",
                (
                    sequence,
                    f"i{sequence}",
                    f"m{sequence}",
                    kind,
                    json.dumps(payload),
                    at,
                ),
            )
        # The earlier turn's hand-off, and this turn's two.
        for sequence, (message_id, at) in enumerate(
            (
                ("chat-turn/a1/start_action/1", "t2"),
                ("chat-turn/a5/start_action/1", "t6"),
                ("chat-turn/a5/start_action/2", "t6b"),
            ),
            start=1,
        ):
            connection.execute(
                "INSERT INTO agent_action_steps(step_id, action_id, user_id, "
                "step_type, step_name, status, goal_handle, retry_count, "
                "prompt_tokens, completion_tokens, user_request_text, "
                "user_message_id, user_message_json, accepted_sequence, "
                "expected_process_id, created_at) VALUES (?, 'action-1', 'user-1', "
                "'user_request', 'user_request', 'success', 'S', 0, 0, 0, 'Go.', ?, "
                "'{}', ?, 'process-1', ?)",
                (message_id, message_id, sequence, at),
            )
        words = [
            read_chat_words(
                connection,
                user_id="user-1",
                row=ActionRequestRow(
                    "user_request",
                    "user_request",
                    message_id,
                    None,
                    None,
                    None,
                    None,
                    at,
                ),
            )
            for message_id, at in (
                ("chat-turn/a5/start_action/1", "t6"),
                ("chat-turn/a5/start_action/2", "t6b"),
            )
        ]

    # Never the chat's own reply, nor what led to the earlier hand-off.
    assert words == [("Export the report as a PDF.", "Put it on my Desktop.")] * 2
