"""The user's single chat: its append-only items and their HTTP / WS shapes.

An item's content is a closed union on ``kind``. Cards and events refer to an
Action or a Suggestion by id only; their status is read from that Action or
Suggestion when the item is drawn, never stored here.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PositiveInt,
    StringConstraints,
    ValidationInfo,
    field_validator,
)

from pantaray_agents.schema.action_conversation import ActionConversationIdentity
from pantaray_agents.schema.agent.action_message import (
    ACTION_MESSAGE_CONTENT_MAX_CODEPOINTS,
    ACTION_MESSAGE_MAX_FILES,
    ACTION_MESSAGE_MAX_IMAGES,
    ActionMessageId,
    ActionProjectRef,
    FileAttachmentInput,
    bounded_project_refs,
    require_project_ref_spans,
)
from pantaray_agents.schema.agent.image import ImageInput
from pantaray_agents.schema.conversation_history import ConversationHistoryTimestamp

# A card is drawn with one or two lines under its title.
CHAT_CARD_SUMMARY_MAX_CODEPOINTS = 500


def _bounded_text(limit: int) -> StringConstraints:
    return StringConstraints(strip_whitespace=True, min_length=1, max_length=limit)


# Chat text shares the Action message's bound: a chat message may be handed to
# an Action as it is.
type ChatText = Annotated[str, _bounded_text(ACTION_MESSAGE_CONTENT_MAX_CODEPOINTS)]
type ChatCardSummary = Annotated[str, _bounded_text(CHAT_CARD_SUMMARY_MAX_CODEPOINTS)]
# A task's answer reaches the chat whole; the chat's window budget fits it.
type ChatActionEventAnswer = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1)
]
# The same attachment contract and limits as an Action USER message.
type ChatImages = Annotated[
    tuple[ImageInput, ...], Field(max_length=ACTION_MESSAGE_MAX_IMAGES)
]
type ChatFiles = Annotated[
    tuple[FileAttachmentInput, ...], Field(max_length=ACTION_MESSAGE_MAX_FILES)
]
type ChatActionEventKind = Literal[
    "completed", "failed", "canceled", "approval_pending"
]
# ``internal`` is a failure of Pantaray's own, which the user can still retry.
type ChatTurnFailureReason = Literal[
    "llm_connection", "llm_request", "step_limit", "internal"
]


class _ChatModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, hide_input_in_errors=True
    )


class ChatActionCard(_ChatModel):
    kind: Literal["action"]
    action_id: ActionMessageId
    summary: ChatCardSummary


class ChatSuggestionCard(_ChatModel):
    kind: Literal["suggestion"]
    suggestion_id: ActionMessageId
    summary: ChatCardSummary


type ChatCard = Annotated[
    ChatActionCard | ChatSuggestionCard, Field(discriminator="kind")
]


def _project_refs_in_text(
    refs: tuple[ActionProjectRef, ...], info: ValidationInfo
) -> tuple[ActionProjectRef, ...]:
    return require_project_ref_spans(refs, info, text_field="text")


class UserMessageContent(_ChatModel):
    kind: Literal["user_message"]
    text: ChatText
    quote_item_id: ActionMessageId | None
    images: ChatImages
    files: ChatFiles
    # Workspace projects named with @, as an Action message names them; after
    # ``text``, which their spans point into.
    project_refs: tuple[ActionProjectRef, ...] = ()

    _bound_project_refs = field_validator("project_refs", mode="before")(
        bounded_project_refs
    )
    _span_project_refs = field_validator("project_refs")(_project_refs_in_text)


class AssistantMessageContent(_ChatModel):
    kind: Literal["assistant_message"]
    text: ChatText
    quote_item_id: ActionMessageId | None
    cards: tuple[ChatCard, ...]


class ActionEventContent(_ChatModel):
    kind: Literal["action_event"]
    action_id: ActionMessageId
    event: ChatActionEventKind
    # Only a run that answered has one; a cancel or an approval wait may not.
    # The whole answer, despite the name, which rows already stored carry.
    final_answer_excerpt: ChatActionEventAnswer | None


class TurnFailureContent(_ChatModel):
    kind: Literal["turn_failure"]
    reason: ChatTurnFailureReason


type ChatItemContent = Annotated[
    UserMessageContent
    | AssistantMessageContent
    | ActionEventContent
    | TurnFailureContent,
    Field(discriminator="kind"),
]


class ChatItem(_ChatModel):
    """One appended item. ``sequence`` orders the chat and is the page cursor."""

    sequence: PositiveInt
    item_id: ActionConversationIdentity
    created_at: ConversationHistoryTimestamp
    content: ChatItemContent


class ChatItemPage(_ChatModel):
    """Newest first. ``next_cursor`` is the ``before`` of the next older page."""

    items: tuple[ChatItem, ...]
    next_cursor: PositiveInt | None


class ChatMessageHttpRequest(_ChatModel):
    """``message_id`` is a user-wide idempotency key for this one message."""

    message_id: ActionMessageId
    text: ChatText
    quote_item_id: ActionMessageId | None = None
    images: ChatImages = ()
    files: ChatFiles = ()
    project_refs: tuple[ActionProjectRef, ...] = ()

    _bound_project_refs = field_validator("project_refs", mode="before")(
        bounded_project_refs
    )
    _span_project_refs = field_validator("project_refs")(_project_refs_in_text)


type ChatMessageRejectedField = Literal[
    "body", "text", "quote_item_id", "images", "files", "project_refs"
]


class ChatMessageRejected(_ChatModel):
    type: Literal["ChatMessageRejected"] = "ChatMessageRejected"
    field: ChatMessageRejectedField


class ChatMessageConflict(_ChatModel):
    """The ``message_id`` was already used for a different message."""

    type: Literal["MessageIdentityConflict"] = "MessageIdentityConflict"


class ChatItemAppendedMessage(_ChatModel):
    """WS ``chat_item_appended``: an item was appended to the owner's chat."""

    item: ChatItem


class ChatTurnStateMessage(_ChatModel):
    """WS ``chat_turn_state``: whether a turn is answering the owner's chat."""

    running: bool


class ChatTurnRetryHttpRequest(_ChatModel):
    """Run the turn a ``turn_failure`` ended again, reading the same items."""

    failure_item_id: ActionMessageId


class ChatTurnRetryStale(_ChatModel):
    """The failure is no longer how the chat's last turn ended."""

    type: Literal["ChatTurnRetryStale"] = "ChatTurnRetryStale"


__all__ = [
    "CHAT_CARD_SUMMARY_MAX_CODEPOINTS",
    "ActionEventContent",
    "AssistantMessageContent",
    "ChatActionCard",
    "ChatActionEventKind",
    "ChatCard",
    "ChatItem",
    "ChatItemAppendedMessage",
    "ChatItemContent",
    "ChatItemPage",
    "ChatMessageConflict",
    "ChatMessageHttpRequest",
    "ChatMessageRejected",
    "ChatMessageRejectedField",
    "ChatSuggestionCard",
    "ChatTurnFailureReason",
    "ChatTurnRetryHttpRequest",
    "ChatTurnRetryStale",
    "ChatTurnStateMessage",
    "TurnFailureContent",
    "UserMessageContent",
]
