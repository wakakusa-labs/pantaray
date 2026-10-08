"""The chat's answer: the ``reply`` tool, which ends a turn.

Its arguments are an ``assistant_message`` item's content without ``kind``, so
a stored reply goes back into the conversation as the very call that made it.
"""

from __future__ import annotations

import json

from pydantic import ValidationError

from pantaray_agents.conversation.window import ConversationEntry
from pantaray_agents.local_runtime.chat.store import read_unavailable_reference
from pantaray_agents.schema.agent.action_message import (
    ACTION_MESSAGE_CONTENT_MAX_CODEPOINTS,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.schema.chat import (
    CHAT_CARD_SUMMARY_MAX_CODEPOINTS,
    AssistantMessageContent,
    ChatItem,
)
from pantaray_llm.contracts.conversation import (
    LlmTurnAssistantItem,
    LlmTurnToolResultItem,
)
from pantaray_llm.contracts.tool_use import LlmToolCall, LlmToolDefinition

REPLY_TOOL_NAME = "reply"
_SUMMARY: dict[str, JSONValue] = {
    "type": "string",
    "maxLength": CHAT_CARD_SUMMARY_MAX_CODEPOINTS,
    "description": "One line on what it is about, as the card shows it.",
}


def _card_schema(kind: str) -> dict[str, JSONValue]:
    return {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": [kind]},
            f"{kind}_id": {"type": "string"},
            "summary": _SUMMARY,
        },
        "required": ["kind", f"{kind}_id", "summary"],
        "additionalProperties": False,
    }


REPLY_TOOL = LlmToolDefinition(
    name=REPLY_TOOL_NAME,
    description=(
        "Send your answer to the user. It ends your turn, so call it alone, "
        "once the answer is ready."
    ),
    parameters={
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "maxLength": ACTION_MESSAGE_CONTENT_MAX_CODEPOINTS,
                "description": "What you say to the user.",
            },
            "quote_item_id": {
                "type": ["string", "null"],
                "description": (
                    "The id of the chat item you answer, when showing which one "
                    "helps; otherwise null."
                ),
            },
            "cards": {
                "type": "array",
                "items": {
                    "anyOf": [_card_schema("action"), _card_schema("suggestion")]
                },
                "description": (
                    "Your tasks and suggestions this answer is about; the user "
                    "opens each from its card."
                ),
            },
        },
        "required": ["text", "quote_item_id", "cards"],
        "additionalProperties": False,
    },
)


def check_reply(
    *, user_id: str, arguments: dict[str, JSONValue]
) -> AssistantMessageContent | str:
    """The reply to append, or why it cannot be, in words the model acts on."""

    try:
        content = AssistantMessageContent.model_validate_json(
            json.dumps({"kind": "assistant_message", **arguments})
        )
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors(include_input=False, include_url=False)
        )
        return f"The reply was not sent: {problems}."
    unavailable = read_unavailable_reference(user_id=user_id, content=content)
    if unavailable == "quote_item_id":
        return "The reply was not sent: quote_item_id is not an item of this chat."
    if unavailable == "cards":
        return (
            "The reply was not sent: a card names an Action or Suggestion the user "
            "does not have."
        )
    return content


def render_reply(
    item: ChatItem, content: AssistantMessageContent
) -> list[ConversationEntry]:
    """A stored reply as the call that sent it, and the result it got."""

    call = LlmToolCall(
        call_id=f"reply-{item.item_id}",
        name=REPLY_TOOL_NAME,
        arguments=content.model_dump(mode="json", exclude={"kind"}),
    )
    return [
        ConversationEntry(LlmTurnAssistantItem(type="assistant", calls=[call])),
        ConversationEntry(
            LlmTurnToolResultItem(
                type="tool_result",
                call_id=call.call_id,
                name=REPLY_TOOL_NAME,
                output={"status": "success", "item_id": item.item_id},
            )
        ),
    ]


__all__ = ["REPLY_TOOL", "REPLY_TOOL_NAME", "check_reply", "render_reply"]
