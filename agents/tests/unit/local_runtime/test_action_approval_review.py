from __future__ import annotations

from pantaray_agents.local_runtime.runtime.action_approval_review import (
    ActionRequestRow,
    action_request_words,
)
from pantaray_agents.local_runtime.runtime.action_message_models import (
    ACTION_RESUME_REQUEST_TEXT,
    ACTION_RESUME_STEP_NAME,
    ACTION_TODO_PICKUP_REQUEST_TEXT,
    ACTION_TODO_PICKUP_STEP_NAME,
    ACTION_USER_STEP_NAME,
)
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
    )


def _opening(text: str, *, suggestion_id: str | None) -> ActionRequestRow:
    return ActionRequestRow(
        "assistant_message", "assistant_message", None, None, None, text, suggestion_id
    )


def test_only_the_users_own_words_are_the_users_messages() -> None:
    rows = (
        _opening("Draft the reply to the vendor.", suggestion_id=None),
        _user(ACTION_TODO_PICKUP_REQUEST_TEXT, step_name=ACTION_TODO_PICKUP_STEP_NAME),
        _user("Send it to me first."),
        # The chat wrote this instruction itself, relaying none of the user's words.
        _user(
            "Upload the draft to the shared drive.",
            chat_handoff=ChatHandoffInput(relayed_item_ids=()),
        ),
        _user(ACTION_RESUME_REQUEST_TEXT, step_name=ACTION_RESUME_STEP_NAME),
    )

    assert action_request_words(rows) == (
        ("Send it to me first.",),
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

    assert action_request_words((accepted,)) == (
        ("Keep it to five lines.",),
        RequestTextNotByUser(
            source="suggestion", text="Summarize this week's discussion."
        ),
    )
    assert action_request_words(answered) == (
        ("Yes, file it.",),
        RequestTextNotByUser(
            source="suggestion", text="Shall I file the expense report?"
        ),
    )
