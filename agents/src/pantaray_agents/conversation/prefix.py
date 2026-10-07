"""What a request is sent behind, and where a provider turn may go back.

A thinking block is bound to the system instruction, the tools and every item
before it, and a provider refuses one whose prefix has changed since -- a body
omitted, a retry notice that was never recorded, a row a repair replaced, a
system instruction in another language. So each turn is recorded with the
fingerprint of the prefix it was produced behind, and goes back only behind the
same one. Where a rewrite breaks the match, that turn and every turn produced
behind it stay off; a turn produced after the rewrite matches again.

The cache read rests on the same property. Measured against the live API, a
request whose input merely *appends* to the previous one reads 81-87% of its
input from the cache, while rewriting a single item it already sent -- even the
last one -- drops the read back to the first message. So whatever a turn adds
after its history (its own context, a retry's notice) is appended once and
never replaced by the next turn.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field

from pantaray_agents.schema.agent.action import ActionProviderTurnRecord
from pantaray_llm.contracts.conversation import (
    LlmProviderTurn,
    LlmTurnAssistantItem,
    LlmTurnItem,
    LlmTurnUserItem,
    OpenAiProviderTurn,
)
from pantaray_llm.contracts.input_block import LlmInputImageBlock, LlmInputTextBlock
from pantaray_llm.contracts.tool_use import LlmToolCall, LlmToolDefinition

# One fixed line, so every turn context begins the same way and the model can
# tell a recorded one from the current one by position alone.
TURN_CONTEXT_HEADING = (
    "# Turn Context\nThe state as of this turn. Anything below it is newer.\n\n"
)
# Sent when nothing changed but the items would otherwise end on the assistant,
# which a provider reads as a turn to continue rather than one to answer.
UNCHANGED_TURN_CONTEXT = "# Turn Context\nNothing has changed since the last one."


def fingerprint_request(
    *, head: str, system_instruction: str, tools: Sequence[LlmToolDefinition]
) -> str:
    """What every item of one conversation's requests is sent behind."""

    return _chain(
        "",
        json.dumps(
            [
                head,
                system_instruction,
                [tool.model_dump(mode="json") for tool in tools],
            ],
            ensure_ascii=False,
            sort_keys=True,
        ),
    )


@dataclass(slots=True)
class ConversationLayout:
    """The items laid out so far, and the fingerprint of the request up to them."""

    fingerprint: str
    items: list[LlmTurnItem] = field(default_factory=list)

    def add(self, item: LlmTurnItem, *, turn_of: str | None = None) -> None:
        # A placed turn enters as the id of what produced it rather than as its
        # bytes: a recorded turn never changes, and that id reads back the same
        # after a restart whatever the stored JSON looks like.
        text = item.model_dump_json(exclude={"provider_turn"})
        if turn_of is not None:
            text += f"\0{turn_of}"
        self.fingerprint = _chain(self.fingerprint, text)
        self.items.append(item)

    def add_text(self, text: str) -> None:
        self.add(
            LlmTurnUserItem(
                type="user", content=[LlmInputTextBlock(type="input_text", text=text)]
            )
        )

    def end_turn(self, turn_context: str | None, *, repair_notice: str) -> str | None:
        """Close this turn's request on a user item, and say what context it sent.

        ``turn_context`` is this turn's own context, or None when nothing
        changed; the caller records what this returns, so a later turn sends it
        again where it stood. ``repair_notice`` is a retry's feedback, which
        goes last and is never recorded, so that a retry is itself an append to
        the request that preceded it.
        """

        if turn_context is None and (
            not self.items or isinstance(self.items[-1], LlmTurnAssistantItem)
        ):
            turn_context = UNCHANGED_TURN_CONTEXT
        if turn_context is not None:
            self.add_text(turn_context)
        if repair_notice:
            self.add_text(repair_notice)
        return turn_context

    def media_refs(self) -> list[str]:
        """The application refs of the media the items carry, in item order."""

        return [
            block.image.application_ref
            for item in self.items
            if not isinstance(item, LlmTurnAssistantItem)
            for block in item.content
            if isinstance(block, LlmInputImageBlock)
            and block.image.application_ref is not None
        ]


def _chain(fingerprint: str, text: str) -> str:
    return hashlib.sha256(f"{fingerprint}\n{text}".encode()).hexdigest()


def replayable_turn(
    record: ActionProviderTurnRecord | None,
    calls: Sequence[LlmToolCall],
    *,
    prefix: str,
) -> LlmProviderTurn | None:
    """The turn to hand back on this item, when it still stands where it was made.

    It must follow the same prefix it was produced behind, and it must describe
    what ran. A turn names every call the model made; the item names the calls
    that ran. They part company when a caller held a call back, or when a run
    stopped mid-batch, and both providers reject a request showing a call whose
    result never arrives. The adapters check the same pairing, so an unusable
    turn is left behind here rather than paid for upstream.
    """

    if record is None or record.fingerprint != prefix:
        return None
    executed = [(call.call_id, call.name) for call in calls]
    return record.turn if _declared_calls(record.turn) == executed else None


def _declared_calls(turn: LlmProviderTurn) -> list[tuple[str, str]]:
    """The calls a provider turn shows, in the order it shows them."""
    if isinstance(turn, OpenAiProviderTurn):
        entries, call_type, id_key = turn.items, "function_call", "call_id"
    else:
        entries, call_type, id_key = turn.blocks, "tool_use", "id"
    return [
        (str(entry.get(id_key)), str(entry.get("name")))
        for entry in entries
        if entry.get("type") == call_type
    ]


__all__ = [
    "TURN_CONTEXT_HEADING",
    "UNCHANGED_TURN_CONTEXT",
    "ConversationLayout",
    "fingerprint_request",
    "replayable_turn",
]
