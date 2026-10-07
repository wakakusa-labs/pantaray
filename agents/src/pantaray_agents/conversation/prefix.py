"""What a request is sent behind, and where a provider turn may go back.

A thinking block is bound to the system instruction, the tools and every item
before it, and a provider refuses one whose prefix has changed since -- a body
omitted, a retry notice that was never recorded, a row a repair replaced, a
system instruction in another language. So each turn is recorded with the
fingerprint of the prefix it was produced behind, and goes back only behind the
same one. Where a rewrite breaks the match, that turn and every turn produced
behind it stay off; a turn produced after the rewrite matches again.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field

from pantaray_agents.schema.agent.action import ActionProviderTurnRecord
from pantaray_llm.contracts.conversation import (
    LlmProviderTurn,
    LlmTurnItem,
    OpenAiProviderTurn,
)
from pantaray_llm.contracts.tool_use import LlmToolCall, LlmToolDefinition


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


__all__ = ["ConversationLayout", "fingerprint_request", "replayable_turn"]
