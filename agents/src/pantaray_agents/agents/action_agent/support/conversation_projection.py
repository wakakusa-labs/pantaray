"""Project the Supervisor's Action history into neutral conversation items.

An Action turn has always reached its provider as one rendered string, so every
turn was a different prompt prefix and no prompt cache ever read it: measured
over 171 live turns, ``cached_tokens`` was zero on every one. The history rows
already carry what a conversation needs -- who spoke, which call produced which
result -- so this module lays them out as items instead. The string rendering in
``formatter_parts`` stays: it is what the step record keeps and what the routes
that cannot receive a conversation still send.

What earns the cache read is that one turn's items are the previous turn's items
with nothing but new items after them. Measured against the live API, a request
whose input merely *appends* to the previous one reads 81-87% of its input from
the cache, while rewriting a single item it already sent -- even the last one --
drops the read back to the first message. So the per-turn context (the time,
the pending draft) is not a trailing item that each turn replaces: each turn
appends its own, and the ones it sent before stay where they were, read back
from the row that recorded them.

These properties keep that true, and they belong to this projection rather than
to the contract it builds:

* a row is final the moment it is appended, and the turn context is recorded
  with it, so nothing an earlier turn sent is ever recomputed;
* the body-omission boundary only ever moves forward, so omitting old results
  rewrites those items and leaves every later item alone.

A THINK's provider turn is the one thing that may not ride along a rewrite. A
thinking block is bound to the system instruction, the tools and every item
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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from pantaray_agents.agents.action_agent.runtime.state import HistoryEntry
from pantaray_agents.agents.action_agent.runtime.tool_attachments import (
    ToolAttachment,
    coerce_tool_attachments,
    collect_prompt_file_inputs,
)
from pantaray_agents.agents.action_agent.support.formatter_parts.history import (
    # The one renderer of a stored result body. A conversation sends the same
    # body as the string rendering, so it must scrub the same data URLs.
    omit_attachment_data_urls,
)
from pantaray_agents.agents.action_agent.tools import STEP_NOTE_ARG
from pantaray_agents.agents.core.llm_file_inputs import LlmFileInput
from pantaray_agents.schema.agent.action import ActionProviderTurnRecord, StepType
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_llm.contracts.conversation import (
    LlmConversation,
    LlmProviderTurn,
    LlmTurnAssistantItem,
    LlmTurnItem,
    LlmTurnToolResultItem,
    LlmTurnUserItem,
    OpenAiProviderTurn,
)
from pantaray_llm.contracts.input_block import (
    LlmImageDescriptor,
    LlmInputBlock,
    LlmInputImageBlock,
    LlmInputTextBlock,
)
from pantaray_llm.contracts.tool_use import LlmToolCall, LlmToolDefinition

# The same mark the string rendering uses for a body past the omission boundary.
OMITTED_OUTPUT_MARK = "…"
# One fixed line, so every turn context begins the same way and the model can
# tell a recorded one from the current one by position alone.
TURN_CONTEXT_HEADING = (
    "# Turn Context\nThe state as of this turn. Anything below it is newer.\n\n"
)
# Sent when nothing changed but the items would otherwise end on the assistant,
# which a provider reads as a turn to continue rather than one to answer.
UNCHANGED_TURN_CONTEXT = "# Turn Context\nNothing has changed since the last one."
_NOTICE_PREFIX = "System Notice: "


@dataclass(frozen=True, slots=True)
class ActionConversationProjection:
    conversation: LlmConversation
    # The context message this turn appended, if any; the caller records it.
    turn_context: str | None
    # The media the items reference, which the request uploads alongside them.
    file_inputs: tuple[LlmFileInput, ...]
    # The request up to its last item: what a turn this request produces is
    # recorded with, and handed back only behind.
    fingerprint: str


@dataclass(slots=True)
class _Layout:
    """The items laid out so far, and the fingerprint of the request up to them."""

    fingerprint: str
    items: list[LlmTurnItem] = field(default_factory=list)

    def add(self, item: LlmTurnItem, *, turn_of: str | None = None) -> None:
        # A placed turn enters as the THINK that produced it rather than as its
        # bytes: a recorded turn never changes, and its step id reads back the
        # same after a restart whatever the stored JSON looks like.
        text = item.model_dump_json(exclude={"provider_turn"})
        if turn_of is not None:
            text += f"\0{turn_of}"
        self.fingerprint = _chain(self.fingerprint, text)
        self.items.append(item)


def fingerprint_request(
    *, head: str, system_instruction: str, tools: Sequence[LlmToolDefinition]
) -> str:
    """What every item of one Action's requests is sent behind."""

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


def _chain(fingerprint: str, text: str) -> str:
    return hashlib.sha256(f"{fingerprint}\n{text}".encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class _ToolRow:
    """One executed call: the row, plus the identity that pairs it with a call."""

    entry: HistoryEntry
    call_id: str
    name: str


def project_action_conversation(
    entries: Sequence[HistoryEntry],
    *,
    request_fingerprint: str,
    omit_before_step_number: int,
    turn_context: str | None,
    repair_notice: str,
    provider_turns: Mapping[str, ActionProviderTurnRecord],
) -> ActionConversationProjection | None:
    """Lay one scope's history out as conversation items, or decline to.

    ``turn_context`` is this turn's own context message, or None when nothing
    changed; the caller records what was appended on the THINK row it is about
    to write. ``repair_notice`` is the
    retry feedback, which follows it as its own item and is never recorded, so
    that a retry is itself an append to the request that preceded it.

    ``provider_turns`` holds the turns this run may hand back, by the
    ``step_id`` of the THINK that produced each one. The caller decides whose
    account issued them; this decides whether a turn still stands behind the
    prefix it was produced behind (``request_fingerprint`` -- the head, system
    instruction and tools -- then every item) and still describes the calls its
    THINK ran.

    ``None`` means this window cannot be sent structurally: a tool row in it
    predates the call identity that pairs a result with the call that asked for
    it, so the turn falls back to the string rendering whole rather than sending
    part of the history in each shape.
    """

    tool_rows = _tool_rows_by_llm_step(entries)
    if tool_rows is None:
        return None
    layout = _Layout(fingerprint=request_fingerprint)
    # An utterance is appended together with the THINK that produced it, so the
    # texts collected here always belong to the next THINK row.
    commentary: list[str] = []
    for entry in entries:
        match entry["step_type"]:
            case StepType.ASSISTANT_MESSAGE:
                commentary.append(entry["assistant_message_text"])
            case StepType.USER_REQUEST:
                # A restored run lists the last turn's answer and then the
                # follow-up that replied to it, with no THINK row between them.
                _add_spoken(layout, commentary)
                commentary.clear()
                layout.add(_user_item(entry))
            case StepType.LLM_OUTPUT:
                _add_think(
                    layout,
                    entry,
                    commentary=tuple(commentary),
                    rows=tool_rows.get(entry["step_id"], ()),
                    omit_before_step_number=omit_before_step_number,
                    record=provider_turns.get(entry["step_id"]),
                )
                commentary.clear()
            case StepType.TOOL_EXECUTION:
                pass  # emitted with the THINK row that declared it
    _add_spoken(layout, commentary)
    items = layout.items
    if turn_context is None and (
        not items or isinstance(items[-1], LlmTurnAssistantItem)
    ):
        turn_context = UNCHANGED_TURN_CONTEXT
    if turn_context is not None:
        layout.add(_text_item(turn_context))
    if repair_notice:
        layout.add(_text_item(repair_notice))
    return ActionConversationProjection(
        conversation=items,
        turn_context=turn_context,
        # ``collect_prompt_file_inputs`` resolves the refs that appear in a text
        # against the rows that own them. The text here is the refs the items
        # placed, so the request uploads exactly that media and nothing else.
        file_inputs=tuple(
            collect_prompt_file_inputs(
                prompt="\n".join(_placed_media_refs(items)), owners=entries
            )
        ),
        fingerprint=layout.fingerprint,
    )


def _add_spoken(layout: _Layout, commentary: Sequence[str]) -> None:
    """What the assistant said with no THINK row after it to carry the words."""

    if commentary:
        layout.add(
            LlmTurnAssistantItem(
                type="assistant", text=list(commentary), calls=[], provider_turn=None
            )
        )


def _placed_media_refs(items: Sequence[LlmTurnItem]) -> list[str]:
    return [
        ref
        for item in items
        if not isinstance(item, LlmTurnAssistantItem)
        for block in item.content
        if not isinstance(block, LlmInputTextBlock)
        for ref in (
            block.image.application_ref
            if isinstance(block, LlmInputImageBlock)
            else block.file.application_ref,
        )
        if ref is not None
    ]


def _tool_rows_by_llm_step(
    entries: Sequence[HistoryEntry],
) -> dict[str, list[_ToolRow]] | None:
    """Group executed calls under the THINK row that declared them."""

    think_ids = {
        entry["step_id"]
        for entry in entries
        if entry["step_type"] == StepType.LLM_OUTPUT
    }
    rows: dict[str, list[_ToolRow]] = {}
    seen_call_ids: set[str] = set()
    for entry in entries:
        if entry["step_type"] != StepType.TOOL_EXECUTION:
            continue
        call_id = entry.get("call_id")
        llm_step_id = entry.get("llm_step_id")
        name = entry["tool_id"]
        if call_id is None or name is None or llm_step_id not in think_ids:
            return None
        # A provider's ids are unique within one response, which is all the
        # runtime checks. One that reuses an id on a later turn cannot be paired,
        # and an unpairable window would fail this Action on every turn after.
        if call_id in seen_call_ids:
            return None
        seen_call_ids.add(call_id)
        rows.setdefault(llm_step_id, []).append(
            _ToolRow(entry=entry, call_id=call_id, name=name)
        )
    # A batch's rows take consecutive step numbers in the order the model
    # declared the calls, which is the order both providers read them back in.
    return {
        step_id: sorted(group, key=lambda row: row.entry["step_number"])
        for step_id, group in rows.items()
    }


def _add_think(
    layout: _Layout,
    entry: HistoryEntry,
    *,
    commentary: tuple[str, ...],
    rows: Sequence[_ToolRow],
    omit_before_step_number: int,
    record: ActionProviderTurnRecord | None,
) -> None:
    """One THINK: the context it was sent with, what it said, what came back."""

    recorded_context = entry.get("turn_context")
    if recorded_context and replays_turn_context(entry, omit_before_step_number):
        layout.add(_text_item(recorded_context))
    if commentary or rows:
        calls = [_tool_call(row) for row in rows]
        turn = _replayable_turn(record, calls, prefix=layout.fingerprint)
        layout.add(
            LlmTurnAssistantItem(
                type="assistant",
                text=list(commentary),
                calls=calls,
                provider_turn=turn,
            ),
            turn_of=None if turn is None else entry["step_id"],
        )
    for row in rows:
        layout.add(
            _tool_result_item(
                row, omit=row.entry["step_number"] < omit_before_step_number
            )
        )
    notice = entry.get("result_line")
    if notice:
        # A THINK's result line reports what the runtime did with the turn --
        # calls it deferred or dropped, a rebuilt window, an invalid output.
        # Those calls have no ``call_id`` to answer, so the line reaches the
        # model as its own message behind the results it belongs with.
        layout.add(_text_item(_NOTICE_PREFIX + notice))


def replays_turn_context(entry: HistoryEntry, omit_before_step_number: int) -> bool:
    """Whether the conversation still sends the context this THINK recorded."""

    # Past the boundary the window was rebuilt, and old context does not come
    # back into it. A THINK from before the field existed has none to replay.
    return (
        entry["step_type"] == StepType.LLM_OUTPUT
        and bool(entry.get("turn_context"))
        and entry["step_number"] >= omit_before_step_number
    )


def _replayable_turn(
    record: ActionProviderTurnRecord | None,
    calls: Sequence[LlmToolCall],
    *,
    prefix: str,
) -> LlmProviderTurn | None:
    """The turn to hand back on this item, when it still stands where it was made.

    It must follow the same prefix it was produced behind, and it must describe
    what ran. A turn names every call the model made; the item names the calls
    that ran. They part company when the batch plan deferred one, or when a run
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


def _tool_call(row: _ToolRow) -> LlmToolCall:
    # Adoption split the note out of the call's arguments and stored it as the
    # row's summary; a replayed call carries the arguments the model wrote.
    arguments: dict[str, JSONValue] = dict(row.entry.get("args") or {})
    arguments[STEP_NOTE_ARG] = row.entry["summary"]
    return LlmToolCall(call_id=row.call_id, name=row.name, arguments=arguments)


def _tool_result_item(row: _ToolRow, *, omit: bool) -> LlmTurnToolResultItem:
    entry = row.entry
    # Past the boundary the body and its media both go: the History Ref below
    # is how the model retrieves either one.
    attachments = () if omit else coerce_tool_attachments(entry.get("attachments"))
    body: dict[str, JSONValue] = {}
    result_line = entry.get("result_line")
    if result_line:
        body["result"] = result_line
    if "output" in entry:
        body["output"] = (
            OMITTED_OUTPUT_MARK if omit else omit_attachment_data_urls(entry["output"])
        )
    agents_md = entry.get("agents_md")
    if agents_md:
        # Each file is attached once per Action, so omission keeps it.
        body["agents_md"] = agents_md
    history_ref = entry.get("short_step_id")
    if history_ref:
        body["history_ref"] = history_ref
    if attachments:
        body["attached_files"] = [attachment["ref"] for attachment in attachments]
    return LlmTurnToolResultItem(
        type="tool_result",
        call_id=row.call_id,
        name=row.name,
        output=body,
        content=[_media_block(attachment) for attachment in attachments],
    )


def _user_item(entry: HistoryEntry) -> LlmTurnUserItem:
    content: list[LlmInputBlock] = [
        LlmInputTextBlock(type="input_text", text=entry["user_request_text"])
    ]
    content.extend(
        _media_block(attachment)
        for attachment in coerce_tool_attachments(entry.get("attachments"))
    )
    return LlmTurnUserItem(type="user", content=content)


def _text_item(text: str) -> LlmTurnUserItem:
    return LlmTurnUserItem(
        type="user", content=[LlmInputTextBlock(type="input_text", text=text)]
    )


def _media_block(attachment: ToolAttachment) -> LlmInputBlock:
    # The model reads a document as the text `read` returns, so an attachment is
    # only ever an image. A non-image one can still reach here out of history a
    # pre-#1395 `read` wrote, and sending it as an image would misdescribe its
    # bytes to the provider, so refuse it instead.
    if not attachment["mime_type"].startswith("image/"):
        raise RuntimeError(
            f"Action attachment is not an image: {attachment['display_path']} "
            f"({attachment['mime_type']})"
        )
    return LlmInputImageBlock(
        type="input_image",
        image=LlmImageDescriptor.model_validate(
            {
                "blob_ref": attachment["blob_ref"],
                "mime_type": attachment["mime_type"],
                "byte_size": attachment["byte_size"],
                "sha256": attachment["sha256"],
                "application_ref": attachment["ref"],
            }
        ),
    )


__all__ = [
    "OMITTED_OUTPUT_MARK",
    "TURN_CONTEXT_HEADING",
    "UNCHANGED_TURN_CONTEXT",
    "ActionConversationProjection",
    "project_action_conversation",
    "replays_turn_context",
    "fingerprint_request",
]
