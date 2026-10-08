"""What a chat turn reads back from the chat, and lays out within its window."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.unit.local_runtime.action_seed import insert_agent_action

from pantaray_agents.agents.chat_agent.context import (
    ChatWindow,
    render_item,
    turn_context,
)
from pantaray_agents.agents.chat_agent.media import NO_MEDIA
from pantaray_agents.agents.chat_agent.reply import REPLY_TOOL, check_reply
from pantaray_agents.conversation.budget import ContextBudget, ContextCapacityExceeded
from pantaray_agents.conversation.window import ConversationEntry
from pantaray_agents.local_runtime.chat.store import (
    ChatTurnEnd,
    append_chat_item,
    chat_turn_end_message_id,
    chat_turn_message_id,
    read_chat_items_after,
    read_chat_items_for_turn,
    read_chat_turn_marks,
)
from pantaray_agents.local_runtime.chat.work_list import ChatWorkList
from pantaray_agents.local_runtime.runtime.identity import (
    register_logged_out_owner,
    reset_logged_out_owner,
)
from pantaray_agents.schema.chat import (
    AssistantMessageContent,
    ChatActionCard,
    ChatItem,
    TurnFailureContent,
    UserMessageContent,
)
from pantaray_llm.contracts.action_turn import LlmActionTurnRequest
from pantaray_llm.contracts.conversation import (
    LlmTurnAssistantItem,
    LlmTurnToolResultItem,
)

USER = "user-1"
_NO_WORK = ChatWorkList(tasks=(), suggestions=())


@pytest.fixture(autouse=True)
def owner() -> Iterator[None]:
    register_logged_out_owner(USER)
    yield
    reset_logged_out_owner()


def _say(message_id: str, text: str = "Hello") -> ChatItem:
    return append_chat_item(
        user_id=USER,
        message_id=message_id,
        content=UserMessageContent(
            kind="user_message", text=text, quote_item_id=None, images=(), files=()
        ),
    )


def _assistant(
    message_id: str, text: str = "Answered.", *, quote: str | None = None
) -> ChatItem:
    return append_chat_item(
        user_id=USER,
        message_id=message_id,
        content=AssistantMessageContent(
            kind="assistant_message", text=text, quote_item_id=quote, cards=()
        ),
    )


def _end(key: str, end: ChatTurnEnd, through: int) -> ChatItem:
    if end == "reply":
        return _assistant(chat_turn_end_message_id(key, end, through))
    return append_chat_item(
        user_id=USER,
        message_id=chat_turn_end_message_id(key, end, through),
        content=TurnFailureContent(kind="turn_failure", reason="llm_connection"),
    )


def _window(window_tokens: int = 1_000_000, after: int = 0) -> ChatWindow:
    budget = ContextBudget(
        window_tokens=window_tokens, baseline=None, reset_pending=False
    )
    return ChatWindow(budget=budget, after=after)


def _fit(window: ChatWindow, waiting: list[ChatItem]) -> tuple[ChatWindow, list[str]]:
    """Fit every item after ``window.after``, with ``waiting`` the newest."""

    items = read_chat_items_for_turn(user_id=USER, after=window.after)
    fitted, entries = window.fit(
        items,
        waiting_from=waiting[0].sequence,
        tail=turn_context(waiting, _NO_WORK),
        head_bytes=2_000,
        media=NO_MEDIA,
    )
    return fitted, [_shown(entry) for entry in entries]


def _shown(entry: ConversationEntry) -> str:
    return entry.item.model_dump_json()


def test_the_marks_follow_the_last_reply_and_failure() -> None:
    assert read_chat_turn_marks(user_id=USER).waiting is False
    _say("m-1")
    # A client may pick any key: a user message is never a turn's end.
    _say(chat_turn_end_message_id("a0", "reply", 99))
    _assistant(chat_turn_message_id("a0", "say/t/0"))
    _end("a0", "reply", 2)
    _say("m-3")
    failure = _end("a2", "failure", 5)

    marks = read_chat_turn_marks(user_id=USER)

    assert (marks.answered_through, marks.replied_through) == (5, 2)
    assert marks.last_failure == failure
    assert marks.waiting is False
    _say("m-4")
    assert read_chat_turn_marks(user_id=USER).waiting is True
    reply_flags = [
        entry.is_reply for entry in read_chat_items_for_turn(user_id=USER, after=0)
    ]
    assert reply_flags == [False, False, False, True, False, False, False]


def test_a_stored_reply_goes_back_as_the_call_that_sent_it() -> None:
    asked = _say("m-1", "Is the report done?")
    insert_agent_action(
        db_path=Path(os.environ["LOCAL_DB_PATH"]), user_id=USER, action_id="act-1"
    )
    sent = AssistantMessageContent(
        kind="assistant_message",
        text="Yes.",
        quote_item_id=asked.item_id,
        cards=(ChatActionCard(kind="action", action_id="act-1", summary="Q3"),),
    )
    append_chat_item(
        user_id=USER,
        message_id=chat_turn_end_message_id("a0", "reply", 1),
        content=sent,
    )
    _assistant(chat_turn_message_id("a1", "say/t/0"), "Checking.")
    _end("a1", "failure", 1)

    said, call, result, aside = [
        entry.item
        for item in read_chat_items_for_turn(user_id=USER, after=0)
        for entry in render_item(item, NO_MEDIA)
    ]

    assert asked.item_id in str(said)
    assert isinstance(call, LlmTurnAssistantItem)
    assert isinstance(result, LlmTurnToolResultItem)
    assert result.call_id == call.calls[0].call_id
    # The arguments it shows are a reply the turn would send again.
    assert check_reply(user_id=USER, arguments=call.calls[0].arguments) == sent
    assert aside == LlmTurnAssistantItem(type="assistant", text=["Checking."])
    LlmActionTurnRequest(  # every call shown is answered
        mode="action_turn",
        tools=[REPLY_TOOL],
        conversation=[said, call, result, aside, turn_context([asked], _NO_WORK)],
    )


@pytest.mark.parametrize(
    ("arguments", "refusal"),
    [
        ({"quote_item_id": None, "cards": []}, "text: Field required"),
        ({"text": "Hi", "quote_item_id": "missing", "cards": []}, "quote_item_id"),
        (
            {
                "text": "Hi",
                "quote_item_id": None,
                "cards": [{"kind": "suggestion", "suggestion_id": "s", "summary": "x"}],
            },
            "a card names an Action or Suggestion",
        ),
    ],
)
def test_a_reply_the_chat_cannot_show_is_refused_in_words(
    arguments: dict[str, object], refusal: str
) -> None:
    verdict = check_reply(user_id=USER, arguments=arguments)  # type: ignore[arg-type]

    assert isinstance(verdict, str)
    assert verdict.startswith("The reply was not sent:")
    assert refusal in verdict


def test_a_later_turn_repeats_what_the_previous_one_sent_before_its_context() -> None:
    first = _say("m-1")
    _, earlier = _fit(_window(), [first])
    _end("a0", "reply", first.sequence)
    later = _say("m-2")

    _, sent = _fit(_window(), [later])

    assert sent[: len(earlier) - 1] == earlier[:-1]


def test_the_window_drops_the_oldest_answered_items_and_keeps_the_rows() -> None:
    for n in range(10):
        asked = _say(f"m-{n}", f"Question {n}: " + "x" * 400)
        _end(f"a{n}", "reply", asked.sequence)
    waiting = [_say("m-new", "Newest"), _say("m-also", "Also " + "y" * 400)]

    window, sent = _fit(_window(window_tokens=2_000), waiting)

    assert window.after > 0
    assert not any("Question 0:" in text for text in sent)
    assert all(any(item.item_id in text for text in sent) for item in waiting)
    assert len(read_chat_items_after(user_id=USER, after=0)) == 22


def test_waiting_items_are_never_dropped_to_fit() -> None:
    _end("a0", "reply", 0)
    waiting = [_say(f"m-{n}", "z" * 2_000) for n in range(4)]

    with pytest.raises(ContextCapacityExceeded):
        _fit(_window(window_tokens=2_000), waiting)
