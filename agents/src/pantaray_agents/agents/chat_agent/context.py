"""What a chat turn sends: the chat's items as a conversation, within a window.

Every turn rebuilds the conversation from the items, each rendered the same way
every time, so a request repeats the previous turn's up to where that turn's own
context began, and the prompt cache reads it again. What a turn added past its
items -- its context, its calls and their results -- is not kept: the items it
appended are what later turns see of it. The turn context goes last, after the
newest items, because it changes on every turn.

The window drops the oldest items and summarizes nothing (design 6.5). Its
boundary only moves forward, past items the chat keeps showing; it lives as long
as the process does, and a restart lays it out again from the first item.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from pantaray_agents.agents.chat_agent.media import ItemMedia
from pantaray_agents.agents.chat_agent.reply import render_reply
from pantaray_agents.config_tunables import load_local_runtime_tunables
from pantaray_agents.conversation.budget import ContextBudget
from pantaray_agents.conversation.prefix import TURN_CONTEXT_HEADING
from pantaray_agents.conversation.window import (
    ConversationEntry,
    LaidOutWindow,
    lay_out,
)
from pantaray_agents.local_runtime.chat.store import TurnChatItem
from pantaray_agents.local_runtime.chat.work_list import ChatWorkList
from pantaray_agents.schema.action_conversation import ActionStatus
from pantaray_agents.schema.agent.action_message_codec import render_project_refs
from pantaray_agents.schema.chat import (
    ActionEventContent,
    AssistantMessageContent,
    ChatItem,
    SuggestionEventContent,
    TurnFailureContent,
    UserMessageContent,
)
from pantaray_llm.contracts.conversation import (
    LlmTurnAssistantItem,
    LlmTurnItem,
    LlmTurnUserItem,
)
from pantaray_llm.contracts.input_block import LlmInputTextBlock

_TASK_STATES: dict[ActionStatus, str] = {
    "queued": "starting",
    "processing": "in progress",
    "success": "done",
    "error": "failed",
    "canceled": "stopped",
}

CHAT_HEAD = (
    "Your chat with the user follows, oldest first. Every item but yours "
    "starts with its id and time in brackets."
)


def render_item(entry: TurnChatItem, media: ItemMedia) -> list[ConversationEntry]:
    """One item as the model reads it; a failure notice is not part of it."""

    item, content = entry.item, entry.item.content
    if isinstance(content, AssistantMessageContent):
        if entry.is_reply:
            return render_reply(item, content)
        return [
            ConversationEntry(
                LlmTurnAssistantItem(type="assistant", text=[content.text])
            )
        ]
    if isinstance(content, TurnFailureContent):
        return []
    text = f"{_header(item)}\n{_body(content, media)}"
    images = (
        [
            media.images[i.storage_path][0]
            for i in content.images
            if i.storage_path in media.images
        ]
        if isinstance(content, UserMessageContent)
        else []
    )
    return [
        ConversationEntry(
            LlmTurnUserItem(
                type="user",
                content=[LlmInputTextBlock(type="input_text", text=text), *images],
            )
        )
    ]


def turn_context(waiting: Sequence[ChatItem], work: ChatWorkList) -> LlmTurnUserItem:
    """The work list and the items waiting for this turn, behind the chat."""

    tasks = [
        f"- {task.action_id} ({_TASK_STATES[task.status]}): {task.title}"
        + ("" if task.latest is None else f" / {task.latest}")
        for task in work.tasks
    ]
    suggestions = [
        f"- {suggestion.suggestion_id}: {suggestion.title}"
        for suggestion in work.suggestions
    ]
    ids = ", ".join(item.item_id for item in waiting)
    return _user_item(
        f"{TURN_CONTEXT_HEADING}"
        + "\n".join(["Your tasks, newest first:", *(tasks or ["(none)"])])
        + "\n"
        + "\n".join(["Your open suggestions:", *(suggestions or ["(none)"])])
        + f"\nWaiting for your reply: {ids}."
    )


# Design limit: nothing past the boundary is summarized. When answers start to
# miss what an older part of the chat said (the requester's reports), add a stage
# that summarizes what the boundary has passed.
@dataclass(frozen=True, slots=True)
class ChatWindow:
    """Items after ``after`` are sent; ``budget`` is what the turns measured."""

    budget: ContextBudget
    after: int

    @classmethod
    def fresh(cls) -> ChatWindow:
        # The chat runs on the models an Action does, under the same input cap.
        window_tokens = load_local_runtime_tunables().action_agent.context_window_tokens
        return cls(
            budget=ContextBudget(
                window_tokens=window_tokens, baseline=None, reset_pending=False
            ),
            after=0,
        )

    def fit(
        self,
        items: Sequence[TurnChatItem],
        *,
        waiting_from: int,
        tail: LlmTurnItem,
        head_bytes: int,
        media: ItemMedia,
    ) -> tuple[ChatWindow, list[ConversationEntry]]:
        """The history to send, past a later boundary when the budget says so.

        ``items`` are those after ``after``; the ones from ``waiting_from`` on
        are never dropped. Raises ``ContextCapacityExceeded`` when what is left
        still does not fit.
        """

        rendered = [(entry.item.sequence, render_item(entry, media)) for entry in items]

        def entries(after: int) -> list[ConversationEntry]:
            kept = [
                entry for sequence, its in rendered if sequence > after for entry in its
            ]
            return [*kept, ConversationEntry(tail)]

        def lay(after: int) -> LaidOutWindow:
            return _lay(entries(after), head_bytes=head_bytes)

        def resolve(byte_budget: int) -> int:
            remaining = lay(self.after).history_bytes
            boundary = self.after
            for sequence, its in rendered:
                if remaining <= byte_budget or sequence >= waiting_from:
                    break
                remaining -= _lay(its, head_bytes=0).history_bytes
                boundary = sequence
            return boundary

        fitted = self.budget.fit(lay, omit_before=self.after, resolve_boundary=resolve)
        if fitted.rebuilt_at is None:
            return self, entries(self.after)
        window = ChatWindow(
            budget=replace(self.budget, reset_pending=False), after=fitted.rebuilt_at
        )
        return window, entries(window.after)


def _lay(entries: Sequence[ConversationEntry], *, head_bytes: int) -> LaidOutWindow:
    return lay_out(
        entries,
        omit_before=0,
        fingerprint="",
        turns={},
        notices=(),
        head_bytes=head_bytes,
    )


def _header(item: ChatItem) -> str:
    return f"[{item.item_id} {item.created_at}]"


def _body(
    content: UserMessageContent | SuggestionEventContent | ActionEventContent,
    media: ItemMedia,
) -> str:
    if isinstance(content, SuggestionEventContent):
        said = media.suggestions.get(content.suggestion_id, "")
        return f"You made a suggestion: {content.suggestion_id}.\n{said}".rstrip()
    if isinstance(content, ActionEventContent):
        event = f"Your task {content.action_id}: {content.event}."
        excerpt = content.final_answer_excerpt
        return event if excerpt is None else f"{event}\n{excerpt}"
    lines = []
    if content.quote_item_id is not None:
        lines.append(f"Quoting {content.quote_item_id}.")
    gone = sum(image.storage_path not in media.images for image in content.images)
    if gone:
        lines.append(f"Attached images no longer available: {gone}.")
    if content.files:
        lines.append(
            f"Attached files: {', '.join(file.name for file in content.files)}."
        )
    body = "\n".join([*lines, content.text])
    # The projects the user named with @, as the task they start is told them,
    # and that the chat can open those folders itself.
    if content.project_refs:
        body = (
            f"{body}\n\n{render_project_refs(content.project_refs)}\n"
            "Your own read tools can open these folders."
        )
    return body


def _user_item(text: str) -> LlmTurnUserItem:
    return LlmTurnUserItem(
        type="user", content=[LlmInputTextBlock(type="input_text", text=text)]
    )


__all__ = ["CHAT_HEAD", "ChatWindow", "render_item", "turn_context"]
