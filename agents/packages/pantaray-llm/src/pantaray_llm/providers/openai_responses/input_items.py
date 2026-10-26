"""Responses ``input`` items: the request's own message, then its conversation.

A request's single user message becomes one item, as it always has. When the
caller also sends an Action conversation, its items follow that message in the
shape the Responses API reads a conversation from -- ``reasoning``,
``function_call``, ``function_call_output``, and assistant messages -- so the
prompt prefix stays byte-identical from turn to turn and the prompt cache reads.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from typing import cast

from openai.types.responses.response_input_item_param import ResponseInputItemParam

from pantaray_llm.contracts.conversation import (
    LlmConversation,
    LlmTurnAssistantItem,
    LlmTurnToolResultItem,
    OpenAiProviderTurn,
)
from pantaray_llm.contracts.input_block import LlmInputBlock, LlmInputTextBlock
from pantaray_llm.contracts.tool_use import LlmToolCall, OpenAiContinuationMediaSlot
from pantaray_llm.contracts.uploaded_blob import UploadedBlob
from pantaray_llm.errors import PROXY_INVALID_INPUT, ProviderError
from pantaray_llm.providers.media_projection import original_media_projection
from pantaray_llm.providers.openai_responses.settings import OpenAiLlmProfile
from pantaray_llm.providers.openai_responses.tool_use import build_openai_blob_data_url
from pantaray_llm.providers.uploaded_content import require_uploaded_blob

# Text without an OpenAI provider turn goes back as commentary: the API asks for
# a phase on every assistant message, and dropping it degrades the newer models.
_COMMENTARY_PHASE = "commentary"


def is_replayable_output_item(item: Mapping[str, object]) -> bool:
    """Every request sends `store: false`, and such a request may only replay a
    reasoning item that carries its encrypted state. Replaying one without it is
    rejected as an unknown item, so drop it instead of sending a dead reference.
    """

    if item.get("type") != "reasoning":
        return True
    return isinstance(item.get("encrypted_content"), str)


def build_openai_message_content(
    *,
    blocks: list[LlmInputBlock],
    profile: OpenAiLlmProfile,
    uploaded_blobs: Mapping[str, UploadedBlob],
    item_index: int,
) -> tuple[list[dict[str, object]], list[OpenAiContinuationMediaSlot]]:
    """Render one message's blocks, and the media slots that index them."""

    content: list[dict[str, object]] = []
    media_slots: list[OpenAiContinuationMediaSlot] = []
    for block in blocks:
        if isinstance(block, LlmInputTextBlock):
            content.append({"type": "input_text", "text": block.text})
            continue
        if profile.image_detail is None:
            raise ProviderError(
                status_code=400,
                code=PROXY_INVALID_INPUT,
                message=(
                    f"OpenAI profile does not accept image inputs: "
                    f"{profile.profile_id}."
                ),
            )
        descriptor = block.image
        blob = require_uploaded_blob(
            descriptor=descriptor,
            uploaded_blobs=uploaded_blobs,
        )
        content.append(
            {
                "type": "input_image",
                "image_url": build_openai_blob_data_url(blob),
                "detail": profile.image_detail,
            }
        )
        media_slots.append(
            OpenAiContinuationMediaSlot(
                item_index=item_index,
                content_index=len(content) - 1,
                data_field="image_url",
                projection=original_media_projection(
                    descriptor=descriptor,
                    source_blob=blob,
                ),
            )
        )
    return content, media_slots


def build_openai_conversation_items(
    *,
    conversation: LlmConversation,
    profile: OpenAiLlmProfile,
    uploaded_blobs: Mapping[str, UploadedBlob],
    first_item_index: int,
) -> tuple[list[ResponseInputItemParam], list[OpenAiContinuationMediaSlot]]:
    """Lay the conversation out as input items, starting at ``first_item_index``."""

    items: list[ResponseInputItemParam] = []
    media_slots: list[OpenAiContinuationMediaSlot] = []

    def append_user_message(blocks: list[LlmInputBlock]) -> None:
        content, slots = build_openai_message_content(
            blocks=blocks,
            profile=profile,
            uploaded_blobs=uploaded_blobs,
            item_index=first_item_index + len(items),
        )
        items.append(cast(ResponseInputItemParam, {"role": "user", "content": content}))
        media_slots.extend(slots)

    for item in conversation:
        if isinstance(item, LlmTurnAssistantItem):
            items.extend(_assistant_items(item))
        elif isinstance(item, LlmTurnToolResultItem):
            items.append(
                cast(
                    ResponseInputItemParam,
                    {
                        "type": "function_call_output",
                        "call_id": item.call_id,
                        "output": json.dumps(item.output, ensure_ascii=False),
                    },
                )
            )
            # A function_call_output carries no media, so whatever the tool
            # attached follows it as the user's own message.
            if item.content:
                append_user_message(item.content)
        else:
            append_user_message(item.content)
    return items, media_slots


def _assistant_items(item: LlmTurnAssistantItem) -> list[ResponseInputItemParam]:
    turn = item.provider_turn
    if isinstance(turn, OpenAiProviderTurn):
        return _replay_openai_turn(turn=turn, calls=item.calls)
    # Anything else is another provider's opaque turn, which this adapter has no
    # claim on. The structure is what earns the cache read, so send that alone.
    return [
        *(
            cast(
                ResponseInputItemParam,
                {"role": "assistant", "content": text, "phase": _COMMENTARY_PHASE},
            )
            for text in item.text
        ),
        *(
            cast(
                ResponseInputItemParam,
                {
                    "type": "function_call",
                    "call_id": call.call_id,
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, ensure_ascii=False),
                },
            )
            for call in item.calls
        ),
    ]


def _replay_openai_turn(
    *,
    turn: OpenAiProviderTurn,
    calls: list[LlmToolCall],
) -> list[ResponseInputItemParam]:
    items: list[dict[str, object]] = [
        copy.deepcopy(cast(dict[str, object], item))
        for item in turn.items
        if is_replayable_output_item(item)
    ]
    replayed_calls = [item for item in items if item.get("type") == "function_call"]
    if [(item.get("call_id"), item.get("name")) for item in replayed_calls] != [
        (call.call_id, call.name) for call in calls
    ]:
        raise ProviderError(
            status_code=400,
            code=PROXY_INVALID_INPUT,
            message=(
                "OpenAI provider_turn function calls do not match the assistant "
                "item's calls."
            ),
        )
    if len(items) != len(turn.items):
        # The API pairs a reasoning item with the function_call item IDs it
        # produced, and rejects a call whose partner is gone. The call itself
        # still replays; only the identity that carries the pairing goes.
        for item in replayed_calls:
            item.pop("id", None)
    return cast(list[ResponseInputItemParam], items)


__all__ = [
    "build_openai_conversation_items",
    "build_openai_message_content",
    "is_replayable_output_item",
]
