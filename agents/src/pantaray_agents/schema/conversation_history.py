"""Strict public list models for Action and Suggestion history."""

from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field
from pydantic_core import PydanticCustomError

from pantaray_agents.schema.action_conversation import (
    ActionConversationIdentity,
    NonBlankText,
)
from pantaray_agents.utils.timestamps import normalize_iso8601_utc_z_milliseconds

type ConversationHistoryStatus = Literal["running", "approval_pending", "idle"]

# A row's status line is one line of the sidebar, cut with an ellipsis to fit.
CONVERSATION_HISTORY_STATUS_LINE_MAX_CODEPOINTS = 120

# What a row shows under its title: one plain line of where the task stands.
type ConversationHistoryStatusLine = Annotated[
    NonBlankText, Field(max_length=CONVERSATION_HISTORY_STATUS_LINE_MAX_CODEPOINTS)
]


def _require_canonical_history_timestamp(value: str) -> str:
    try:
        canonical = normalize_iso8601_utc_z_milliseconds(value)
    except ValueError as exc:
        raise PydanticCustomError(
            "conversation_history_timestamp_invalid",
            "timestamp must use canonical UTC milliseconds",
        ) from exc
    if value != canonical:
        raise PydanticCustomError(
            "conversation_history_timestamp_invalid",
            "timestamp must use canonical UTC milliseconds",
        )
    return value


type ConversationHistoryTimestamp = Annotated[
    str, AfterValidator(_require_canonical_history_timestamp)
]


class _ConversationHistoryModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )


class ConversationHistoryItem(_ConversationHistoryModel):
    kind: Literal["conversation"]
    action_id: ActionConversationIdentity
    title: NonBlankText
    updated_at: ConversationHistoryTimestamp
    status: ConversationHistoryStatus
    latest_completion_event_id: ActionConversationIdentity | None
    # The latest answer's first line; None while running, or with no answer yet.
    status_line: ConversationHistoryStatusLine | None
    # The names of the files its answers link, in the order they first appear.
    artifact_names: tuple[NonBlankText, ...]


class SuggestionHistoryItem(_ConversationHistoryModel):
    kind: Literal["suggestion"]
    suggestion_id: ActionConversationIdentity
    title: NonBlankText
    updated_at: ConversationHistoryTimestamp
    status: Literal["approval_pending", "idle"]
    # The names of the files the suggestion made.
    artifact_names: tuple[NonBlankText, ...]


type ConversationHistoryListItem = Annotated[
    ConversationHistoryItem | SuggestionHistoryItem,
    Field(discriminator="kind"),
]


class ConversationHistoryPage(_ConversationHistoryModel):
    items: tuple[ConversationHistoryListItem, ...]
    next_cursor: NonBlankText | None


__all__ = [
    "CONVERSATION_HISTORY_STATUS_LINE_MAX_CODEPOINTS",
    "ConversationHistoryItem",
    "ConversationHistoryListItem",
    "ConversationHistoryPage",
    "ConversationHistoryStatus",
    "ConversationHistoryTimestamp",
    "SuggestionHistoryItem",
]
