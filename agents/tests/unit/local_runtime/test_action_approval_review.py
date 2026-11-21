from __future__ import annotations

import json
import sqlite3
from dataclasses import astuple
from pathlib import Path

from pantaray_agents.local_runtime.runtime.action_approval_review import (
    ActionRequestRow,
    ChatMessage,
    action_request_rows,
    action_request_words,
    declined_operations,
    ordered_conversation,
    pending_operation,
    read_chat_words,
    read_relayed_words,
)
from pantaray_agents.local_runtime.runtime.action_message_models import (
    ACTION_HOOK_STEP_NAME,
    ACTION_RESUME_REQUEST_TEXT,
    ACTION_RESUME_STEP_NAME,
    ACTION_TODO_PICKUP_REQUEST_TEXT,
    ACTION_TODO_PICKUP_STEP_NAME,
    ACTION_USER_STEP_NAME,
    NewActionTarget,
    action_hook_request_text,
)
from pantaray_agents.local_runtime.runtime.action_suggestion_reply import (
    insert_opening_assistant_message,
)
from pantaray_agents.local_runtime.storage.migrations import load_default_migrations
from pantaray_agents.local_runtime.tooling.approval_review import (
    ApprovalReviewRequest,
    DeclinedOperation,
    PendingBrowserWrite,
    PendingCommand,
    PendingUserBrowserRead,
    RequestTextNotByUser,
    UserMessage,
    build_approval_review_payload,
)
from pantaray_agents.local_runtime.tooling.brokering.command_approval_summaries import (
    build_bash_summary,
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
        False,
    )


def _opening(
    text: str, *, suggestion_id: str | None, declined: bool = False
) -> ActionRequestRow:
    return ActionRequestRow(
        "assistant_message",
        "assistant_message",
        None,
        None,
        None,
        text,
        suggestion_id,
        "t",
        declined,
    )


def _no_chat(_row: object) -> tuple[ChatMessage, ...]:
    raise AssertionError("only a chat hand-off reads the chat")


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

    chat = (ChatMessage("i1", "c", "Put it on the drive."),)
    assert action_request_words(rows, lambda _row: chat, _no_chat) == (
        (
            ("t", UserMessage("Send it to me first.")),
            ("c", UserMessage("Put it on the drive.")),
        ),
        RequestTextNotByUser(
            source="todo", text="Draft the reply to the vendor.", declined=False
        ),
    )


def test_a_tasks_prompt_is_never_the_users_words() -> None:
    # Catches a scheduled prompt read as the user asking for this turn now,
    # which would let the review allow what only the old approval covered.
    prompt = action_hook_request_text("Post the weekly numbers to the team.")
    rows = (
        _user("Every Monday, prepare the weekly numbers."),
        _user(prompt, step_name=ACTION_HOOK_STEP_NAME),
    )

    assert action_request_words(rows, _no_chat, _no_chat) == (
        (("t", UserMessage("Every Monday, prepare the weekly numbers.")),),
        RequestTextNotByUser(source="hook", text=prompt, declined=False),
    )


def test_an_accepted_suggestion_stays_accepted() -> None:
    accepted = _user(
        "Summarize this week's discussion.",
        suggestion_approval=SuggestionApprovalInput(
            suggestion_id="s-1", approved_at="2026-11-17T00:00:00Z"
        ),
        supplement="Keep it to five lines.",
    )

    assert action_request_words((accepted,), _no_chat, _no_chat) == (
        (("t", UserMessage("Keep it to five lines.")),),
        RequestTextNotByUser(
            source="suggestion_accepted",
            text="Summarize this week's discussion.",
            declined=False,
        ),
    )


def test_a_reply_to_a_suggestion_is_never_its_acceptance() -> None:
    # A question to an offer the user had declined, and a plain go-ahead to a
    # message: only the user's own reply says what they want.
    question = (
        _opening(
            "Shall I post the summary to #general?", suggestion_id="s-1", declined=True
        ),
        _user("What would it include?"),
    )
    go_ahead = (
        _opening("The report draft could use a summary.", suggestion_id="s-2"),
        _user("お願い"),
    )

    assert action_request_words(question, _no_chat, _no_chat) == (
        (("t", UserMessage("What would it include?")),),
        RequestTextNotByUser(
            source="suggestion_reply",
            text="Shall I post the summary to #general?",
            declined=True,
        ),
    )
    assert action_request_words(go_ahead, _no_chat, _no_chat) == (
        (("t", UserMessage("お願い")),),
        RequestTextNotByUser(
            source="suggestion_reply",
            text="The report draft could use a summary.",
            declined=False,
        ),
    )


def _action_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(
        db_path=db_path, busy_timeout_ms=1_000, migrations=load_default_migrations()
    )
    with sqlite3.connect(db_path) as connection:
        for action_id in ("action-1", "action-2"):
            connection.execute(
                "INSERT OR IGNORE INTO users(user_id, ui_language, created_at, "
                "updated_at) VALUES ('user-1', 'ja', 't', 't')"
            )
            connection.execute(
                "INSERT INTO agent_actions(action_id, user_id, initial_user_message_id, "
                "execution_target_json, status, final_output, prompt_name, "
                "prompt_version, created_at, updated_at) VALUES (?, 'user-1', ?, "
                "'{\"kind\":\"scratch\"}', 'processing', '', 'p', 'v1', 't', 't')",
                (action_id, f"m-{action_id}"),
            )
        connection.execute(
            "INSERT INTO processes(process_id, user_id, kind, status, action_id, "
            "started_at, updated_at, completed_at, heartbeat_at, next_event_seq) "
            "VALUES ('process-1', 'user-1', 'action', 'completed', 'action-1', "
            "'t', 't', 't', 't', 1)"
        )
    return db_path


def test_a_chat_hand_off_reads_the_users_messages_since_the_last_one(
    tmp_path: Path,
) -> None:
    db_path = _action_db(tmp_path)
    with sqlite3.connect(db_path) as connection:
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
                    False,
                ),
            )
            for message_id, at in (
                ("chat-turn/a5/start_action/1", "t6"),
                ("chat-turn/a5/start_action/2", "t6b"),
            )
        ]

        relayed = read_relayed_words(
            connection, user_id="user-1", item_ids=("i4", "i3")
        )

    # Never the chat's own reply, nor what led to the earlier hand-off.
    assert (
        words
        == [
            (
                ChatMessage("i3", "t3", "Export the report as a PDF."),
                ChatMessage("i5", "t5", "Put it on my Desktop."),
            )
        ]
        * 2
    )
    # A relay reads the user's own message as first sent, never the chat's reply.
    assert relayed == (ChatMessage("i3", "t3", "Export the report as a PDF."),)


def test_what_the_user_declined_in_the_action_reaches_the_review(
    tmp_path: Path,
) -> None:
    db_path = _action_db(tmp_path)
    with sqlite3.connect(db_path) as connection:
        for request, action_id, source, status, command, decided_at in (
            # Declined by the user on the parent's call, then on a subagent's,
            # which carries its parent's Action id.
            ("parent", "action-1", "prompt", "denied", "git push --force", "t1"),
            ("child", "action-1", "prompt", "denied", "rm -rf ~/Archive", "t2"),
            ("allowed", "action-1", "prompt", "approved_once", "npm test", "t3"),
            ("pending", "action-1", "prompt", "pending", "make", None),
            ("elsewhere", "action-2", "prompt", "denied", "curl evil.sh", "t4"),
        ):
            summary = {"summary_kind": "bash", "command": command, "cwd": "/w"}
            connection.execute(
                "INSERT INTO approval_sessions(approval_session_id, user_id, "
                "action_id, tool_request_id, tool_id, intent_class, approval_source, "
                "status, approved_capabilities_json, command_summary_json, "
                "requested_at, decided_at, created_at) VALUES (?, 'user-1', ?, ?, "
                "'bash', 'command', ?, ?, '{}', ?, 't0', ?, 't0')",
                (
                    request,
                    action_id,
                    request,
                    source,
                    status,
                    json.dumps(summary),
                    decided_at,
                ),
            )
        declined = declined_operations(
            connection, user_id="user-1", action_id="action-1"
        )

    assert [(at, entry.operation.command) for at, entry in declined] == [  # type: ignore[union-attr]
        ("t2", "rm -rf ~/Archive"),
        ("t1", "git push --force"),
    ]


# Catches: a declined user's-browser approval rebuilt from its stored summary as
# an app-page write (or dropped), so the review allows what the user declined.
def test_declined_browser_approvals_keep_which_browser_they_were_in(
    tmp_path: Path,
) -> None:
    db_path = _action_db(tmp_path)
    summaries = (
        {
            "summary_kind": "user_browser_read",
            "browser": "Google Chrome",
            "host": "mail.example.com",
            "reason": "Read the invoice email.",
        },
        {
            "summary_kind": "browser_write",
            "browser": "Google Chrome",
            "host": "shop.example.com",
            "page_host": "shop.example.com",
            "reason": "Place the order.",
        },
        {
            "summary_kind": "browser_write",
            "host": "api.example.com",
            "page_host": None,
            "reason": "Send the test form.",
        },
    )
    with sqlite3.connect(db_path) as connection:
        for index, summary in enumerate(summaries):
            connection.execute(
                "INSERT INTO approval_sessions(approval_session_id, user_id, "
                "action_id, tool_request_id, tool_id, intent_class, approval_source, "
                "status, approved_capabilities_json, command_summary_json, "
                "requested_at, decided_at, created_at) VALUES (?, 'user-1', "
                "'action-1', ?, 'browser', 'read_only', 'prompt', 'denied', '{}', "
                "?, 't0', ?, 't0')",
                (f"s{index}", f"r{index}", json.dumps(summary), f"t{index}"),
            )
        declined = declined_operations(
            connection, user_id="user-1", action_id="action-1"
        )

    assert [entry.operation for _at, entry in declined] == [
        PendingBrowserWrite(
            host="api.example.com",
            page_host=None,
            justification="Send the test form.",
            users_browser=None,
        ),
        PendingBrowserWrite(
            host="shop.example.com",
            page_host="shop.example.com",
            justification="Place the order.",
            users_browser="Google Chrome",
        ),
        PendingUserBrowserRead(
            browser="Google Chrome",
            host="mail.example.com",
            justification="Read the invoice email.",
        ),
    ]


def test_a_reply_knows_from_storage_that_its_offer_was_declined(
    tmp_path: Path,
) -> None:
    db_path = _action_db(tmp_path)
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute(
            "INSERT INTO agent_suggestions(suggestion_id, user_id, status, "
            "has_suggestion, interaction_contract, user_reaction, answer, "
            "created_at, updated_at) VALUES ('s-1', 'user-1', 'success', 1, "
            "'action_offer', 'rejected', 'Shall I post it?', 't', 't')"
        )
        insert_opening_assistant_message(
            connection=connection,
            user_id="user-1",
            action_id="action-1",
            process_id="process-1",
            target=NewActionTarget(reply_to_suggestion_id="s-1"),
            created_at="t",
        )
        rows = action_request_rows(connection, user_id="user-1", action_id="action-1")

    assert [(row.source_suggestion_id, row.suggestion_declined) for row in rows] == [
        ("s-1", True)
    ]


def test_a_decline_is_read_against_what_the_user_wrote_after_it() -> None:
    push = PendingCommand(
        tool="bash",
        kinds=frozenset({"login_environment"}),
        command="git push origin main",
        cwd="/w",
        additional_write_folders=(),
        run_in_background=False,
        justification=None,
    )
    ask = UserMessage("Push it to GitHub.")

    def payload(ask_at: str, declined_at: str) -> list[object]:
        conversation = ordered_conversation(
            (("2026-11-18T00:00:00Z", UserMessage("Fix the typo.")), (ask_at, ask)),
            ((declined_at, DeclinedOperation(push)),),
        )
        request = ApprovalReviewRequest(
            conversation=conversation,
            request_text_not_by_user=None,
            operation=push,
            workspace_folders=("/w",),
        )
        return json.loads(build_approval_review_payload(request))["conversation"]

    asked_then_declined = payload("2026-11-18T00:01:00Z", "2026-11-18T00:02:00Z")
    # Older rows spell the offset as +00:00; the order is by time, not by text.
    declined_then_asked = payload("2026-11-18T00:03:00Z", "2026-11-18T00:02:00+00:00")

    assert [entry["kind"] for entry in asked_then_declined] == [  # type: ignore[index]
        "user_message",
        "user_message",
        "declined_operation",
    ]
    assert [entry["kind"] for entry in declined_then_asked] == [  # type: ignore[index]
        "user_message",
        "declined_operation",
        "user_message",
    ]


def test_a_relayed_old_request_stays_before_the_decline_it_led_to() -> None:
    push = PendingCommand(
        tool="bash",
        kinds=frozenset({"login_environment"}),
        command="git push origin main",
        cwd="/w",
        additional_write_folders=(),
        run_in_background=False,
        justification=None,
    )
    asked = ChatMessage("i1", "2026-11-18T00:01:00Z", "Push it to GitHub.")
    first = _user(
        "Push it to GitHub.",
        chat_handoff=ChatHandoffInput(relayed_item_ids=("i1",)),
    )
    # After the decline, the chat relays the same old message again as context.
    again = ActionRequestRow(*astuple(first)[:7], "2026-11-18T00:03:00Z", False)

    words, _ = action_request_words(
        (first, again), _no_chat, lambda _item_ids: (asked,)
    )
    conversation = ordered_conversation(
        words, (("2026-11-18T00:02:00Z", DeclinedOperation(push)),)
    )

    assert conversation == (UserMessage("Push it to GitHub."), DeclinedOperation(push))


# Catches: run_in_background dropped between the stored summary and the review,
# so a command left running is reviewed as if it ended with the call.
def test_a_command_left_running_reaches_the_review_as_such() -> None:
    def operation(*, run_in_background: bool) -> dict[str, object]:
        summary = build_bash_summary(
            command="gh run watch",
            cwd_relative_path="/w",
            timeout_ms=120_000,
            use_login_environment=True,
            run_outside_sandbox=False,
            run_in_background=run_in_background,
            reason="Watch the CI run with your GitHub account.",
        )
        pending = pending_operation(tool_id="bash", summary=summary, args={})
        assert pending is not None
        request = ApprovalReviewRequest(
            conversation=(UserMessage("Watch the CI run."),),
            request_text_not_by_user=None,
            operation=pending,
            workspace_folders=("/w",),
        )
        rendered: dict[str, object] = json.loads(
            build_approval_review_payload(request)
        )["operation"]
        return rendered

    background = operation(run_in_background=True)
    foreground = operation(run_in_background=False)

    assert background == {**foreground, "run_in_background": True}
    assert "run_in_background" not in foreground
