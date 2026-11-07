"""Attribute public Action conversation without exposing private image references."""

from __future__ import annotations

from typing import Literal, TypedDict, get_args

from pantaray_agents.local_runtime.runtime.action_message_models import (
    ACTION_HOST_STEP_NAMES,
)
from pantaray_agents.schema.agent.action import StepType
from pantaray_agents.schema.agent.action_message_codec import (
    parse_stored_action_user_message,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.utils.local_time import describe_utc_timestamp

ConversationKind = Literal[
    "user_message",
    "assistant_message",
    "assistant_commentary",
    "assistant_proposal",
    "suggestion_approval",
    "host_control",
    "legacy_unattributed",
]


class MemoryConversationEntry(TypedDict):
    kind: ConversationKind
    content: str
    image_count: int | None


class MemoryActionConversation(TypedDict):
    origin: ConversationKind
    suggestion_id: str | None
    entries: list[MemoryConversationEntry]


def project_memory_action_conversation(
    *,
    step_type: str,
    step_name: str,
    user_message_id: str | None,
    user_message_json: str | None,
    user_request_text: str | None,
    llm_response_text: str | None,
    source_suggestion_id: str | None,
) -> MemoryActionConversation | None:
    if step_type == StepType.ASSISTANT_MESSAGE.value:
        if llm_response_text is None:
            raise ValueError("Action ASSISTANT message body is missing")
        kind: ConversationKind = (
            "assistant_commentary"
            if step_name == "assistant_commentary"
            else "assistant_message"
        )
        return {
            "origin": kind,
            "suggestion_id": source_suggestion_id,
            "entries": [{"kind": kind, "content": llm_response_text, "image_count": 0}],
        }
    if step_type != StepType.USER_REQUEST.value:
        return None
    if user_request_text is None:
        raise ValueError("Action USER request text is missing")
    message = parse_stored_action_user_message(
        message_id=user_message_id,
        message_json=user_message_json,
        user_request_text=user_request_text,
    )
    if step_name in ACTION_HOST_STEP_NAMES or message is None:
        kind = (
            "host_control"
            if step_name in ACTION_HOST_STEP_NAMES
            else "legacy_unattributed"
        )
        return {
            "origin": kind,
            "suggestion_id": None,
            "entries": [
                {
                    "kind": kind,
                    "content": user_request_text,
                    "image_count": len(message.images) if message is not None else None,
                }
            ],
        }
    approval = message.suggestion_approval
    if approval is None:
        return {
            "origin": "user_message",
            "suggestion_id": None,
            "entries": [
                {
                    "kind": "user_message",
                    "content": message.content,
                    "image_count": len(message.images),
                }
            ],
        }
    entries: list[MemoryConversationEntry] = [
        {"kind": "assistant_proposal", "content": message.content, "image_count": 0},
        {
            "kind": "suggestion_approval",
            "content": f"Approved at {describe_utc_timestamp(approval.approved_at)}",
            "image_count": 0,
        },
    ]
    if message.supplement is not None or message.images:
        entries.append(
            {
                "kind": "user_message",
                "content": message.supplement or "",
                "image_count": len(message.images),
            }
        )
    return {
        "origin": "suggestion_approval",
        "suggestion_id": approval.suggestion_id,
        "entries": entries,
    }


def conversation_kind_schema() -> dict[str, JSONValue]:
    return {"type": "string", "enum": list(get_args(ConversationKind))}


def memory_action_conversation_schema() -> dict[str, JSONValue]:
    return {
        "type": ["object", "null"],
        "additionalProperties": False,
        "required": ["origin", "suggestion_id", "entries"],
        "properties": {
            "origin": conversation_kind_schema(),
            "suggestion_id": {"type": ["string", "null"]},
            "entries": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["kind", "content", "image_count"],
                    "properties": {
                        "kind": conversation_kind_schema(),
                        "content": {"type": "string"},
                        "image_count": {
                            "type": ["integer", "null"],
                            "minimum": 0,
                            "description": "Attachment count only; image content is unavailable. Null means unknown in legacy history.",
                        },
                    },
                },
            },
        },
    }
