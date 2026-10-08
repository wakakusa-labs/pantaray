"""The child's conversation, as its private process events store and rebuild it.

Every item the shared loop appends reaches one hook, and each hook appends one
row, so the rows in order rebuild the history a stopped run left: the turns
with their provider turns and where the window stood, the answers, the
notices, and the parent's messages where they were delivered. The assigned
task leads the history and is the job payload's, never a row.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict

from pantaray_agents.conversation.budget import ContextBudget, InputBaseline
from pantaray_agents.conversation.loop import NOT_RUN_ERROR_CODE, RecordedTurn
from pantaray_agents.conversation.window import ConversationEntry, WindowState
from pantaray_agents.local_runtime.runtime.action_subagent_messages import (
    ACTION_SUBAGENT_DELIVERED_EVENT,
    ACTION_SUBAGENT_MESSAGE_EVENT,
    ACTION_SUBAGENT_TOOL_EVENT,
    action_subagent_message_content,
    append_action_subagent_event,
    deliver_action_subagent_messages,
    load_action_subagent_events,
)
from pantaray_agents.local_runtime.storage.migrations import MigrationError
from pantaray_agents.schema.agent.action import ActionProviderTurnRecord
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tasks.types import ActionSubagentJobPayload
from pantaray_agents.tools.contract import ReactToolResult
from pantaray_llm.contracts.conversation import (
    LlmTurnAssistantItem,
    LlmTurnItem,
    LlmTurnToolResultItem,
    LlmTurnUserItem,
)
from pantaray_llm.contracts.input_block import LlmInputTextBlock
from pantaray_llm.contracts.tool_use import LlmToolCall

ACTION_SUBAGENT_TURN_EVENT = "action_subagent_turn"
ACTION_SUBAGENT_NOTICE_EVENT = "action_subagent_notice"
_HISTORY_EVENTS = (
    ACTION_SUBAGENT_MESSAGE_EVENT,
    ACTION_SUBAGENT_DELIVERED_EVENT,
    ACTION_SUBAGENT_TURN_EVENT,
    ACTION_SUBAGENT_TOOL_EVENT,
    ACTION_SUBAGENT_NOTICE_EVENT,
)
_INTERRUPTED = (
    "Not run: this call was interrupted before it was answered. "
    "Request it again in a later turn if it is still needed."
)


class _TurnRow(BaseModel):
    """One answered turn, and where the window stood after it."""

    model_config = ConfigDict(extra="forbid")

    turn_id: str
    item: LlmTurnAssistantItem
    provider_turn: ActionProviderTurnRecord | None
    omit_before: int
    baseline: InputBaseline | None
    reset_pending: bool


class _AnswerRow(BaseModel):
    """The answer to one call.

    An answer written outside the loop -- the approval a resumed child settles,
    the claimed invocation startup recovery writes back -- cannot name its call
    and leaves ``call_id`` out. It answers the first unanswered call of its
    tool: calls run in the model's order, so that is the one that was running.
    Recovery's rows also carry the arguments, which the turn already holds.
    """

    model_config = ConfigDict(extra="ignore")

    call_id: str | None = None
    tool_name: str
    status: Literal["completed", "error"]
    output: JSONValue
    error_message: str | None


@dataclass(frozen=True, slots=True)
class SubagentHistory:
    """What the rows rebuild, and the calls a stopped run left unanswered."""

    entries: tuple[ConversationEntry, ...]
    turns: dict[str, ActionProviderTurnRecord]
    window: WindowState
    unanswered: tuple[LlmToolCall, ...]


def load_subagent_history(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    process_id: str,
    assigned_task: str,
    window_tokens: int,
) -> SubagentHistory:
    entries = [ConversationEntry(_text_item(assigned_task))]
    turns: dict[str, ActionProviderTurnRecord] = {}
    window = WindowState(
        budget=ContextBudget(
            window_tokens=window_tokens, baseline=None, reset_pending=False
        ),
        omit_before=0,
    )
    waiting: list[str] = []
    unanswered: list[LlmToolCall] = []
    for event in load_action_subagent_events(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        process_id=process_id,
        event_names=_HISTORY_EVENTS,
    ):
        name, payload = event.event_name, event.payload
        if name == ACTION_SUBAGENT_MESSAGE_EVENT:
            waiting.append(action_subagent_message_content(payload))
        elif name == ACTION_SUBAGENT_DELIVERED_EVENT:
            entries.extend(ConversationEntry(_text_item(text)) for text in waiting)
            waiting.clear()
        elif name == ACTION_SUBAGENT_TURN_EVENT:
            turn = _TurnRow.model_validate(payload)
            entries.append(ConversationEntry(turn.item, turn_id=turn.turn_id))
            if turn.provider_turn is not None:
                turns[turn.turn_id] = turn.provider_turn
            window = WindowState(
                budget=ContextBudget(
                    window_tokens=window_tokens,
                    baseline=turn.baseline,
                    reset_pending=turn.reset_pending,
                ),
                omit_before=turn.omit_before,
            )
            unanswered.extend(turn.item.calls)
        elif name == ACTION_SUBAGENT_TOOL_EVENT:
            answer = _AnswerRow.model_validate(payload)
            call = _answered_call(unanswered, answer)
            unanswered.remove(call)
            entries.append(ConversationEntry(_answer_item(call.call_id, answer)))
        else:
            entries.append(ConversationEntry(LlmTurnUserItem.model_validate(payload)))
    return SubagentHistory(
        entries=tuple(entries),
        turns=turns,
        window=window,
        unanswered=tuple(unanswered),
    )


@dataclass(frozen=True, slots=True)
class SubagentHistoryWriter:
    """The loop's hooks for one child's job: each appends the row it stands for."""

    db_path: Path
    busy_timeout_ms: int
    payload: ActionSubagentJobPayload

    async def before_send(self) -> list[LlmTurnItem]:
        return [
            _text_item(text)
            for text in deliver_action_subagent_messages(
                db_path=self.db_path,
                busy_timeout_ms=self.busy_timeout_ms,
                payload=self.payload,
            )
        ]

    async def on_turn(self, turn: RecordedTurn) -> None:
        # The loop records every turn as an assistant item under its turn id.
        budget = turn.window.budget
        self._append(
            ACTION_SUBAGENT_TURN_EVENT,
            _TurnRow(
                turn_id=cast(str, turn.entry.turn_id),
                item=cast(LlmTurnAssistantItem, turn.entry.item),
                provider_turn=turn.provider_turn,
                omit_before=turn.window.omit_before,
                baseline=budget.baseline,
                reset_pending=budget.reset_pending,
            ),
        )

    async def on_result(
        self, call: LlmToolCall, result: ReactToolResult
    ) -> LlmTurnToolResultItem:
        answer = _answer_row(call.name, result, call_id=call.call_id)
        self._append(ACTION_SUBAGENT_TOOL_EVENT, answer)
        return _answer_item(call.call_id, answer)

    async def on_notice(self, item: LlmTurnUserItem) -> None:
        self._append(ACTION_SUBAGENT_NOTICE_EVENT, item)

    def answer_waiting_call(self, tool_name: str, result: ReactToolResult) -> None:
        """Answer the call of this tool a paused run left waiting on approval.

        Written before the history is loaded, so the rebuild pairs it with that
        call and ``answer_unanswered`` never reaches it.
        """

        self._append(ACTION_SUBAGENT_TOOL_EVENT, _answer_row(tool_name, result))

    async def answer_unanswered(
        self, history: SubagentHistory
    ) -> list[ConversationEntry]:
        """The history to resume, with every call it left unanswered answered.

        However the last run stopped -- a crash, a failed save -- a call it made
        and never answered is answered as not run before the conversation goes
        on: it may or may not have run, and the model asks again if it must.
        """

        entries = list(history.entries)
        for call in history.unanswered:
            answer = await self.on_result(call, _interrupted(call.name))
            entries.append(ConversationEntry(answer))
        return entries

    def _append(self, event_name: str, row: BaseModel) -> None:
        append_action_subagent_event(
            db_path=self.db_path,
            busy_timeout_ms=self.busy_timeout_ms,
            payload=self.payload,
            event_name=event_name,
            event_payload=row.model_dump(mode="json"),
        )


def _interrupted(tool_name: str) -> ReactToolResult:
    return ReactToolResult(
        tool_name=tool_name,
        status="error",
        output={
            "status": "error",
            "error_code": NOT_RUN_ERROR_CODE,
            "message": _INTERRUPTED,
        },
        error_message=_INTERRUPTED,
    )


def _answer_row(
    tool_name: str, result: ReactToolResult, *, call_id: str | None = None
) -> _AnswerRow:
    return _AnswerRow(
        call_id=call_id,
        tool_name=tool_name,
        status="completed" if result.status == "success" else "error",
        output=result.output,
        error_message=result.error_message,
    )


def _answer_item(call_id: str, answer: _AnswerRow) -> LlmTurnToolResultItem:
    """What the child reads back for one call: status, result, error."""

    body: dict[str, JSONValue] = {"status": answer.status, "result": answer.output}
    if answer.error_message:
        body["error"] = answer.error_message
    return LlmTurnToolResultItem(
        type="tool_result", call_id=call_id, name=answer.tool_name, output=body
    )


def _answered_call(unanswered: list[LlmToolCall], answer: _AnswerRow) -> LlmToolCall:
    for call in unanswered:
        if (
            call.call_id == answer.call_id
            if answer.call_id is not None
            else call.name == answer.tool_name
        ):
            return call
    raise MigrationError("An Action subagent answer names no call waiting for one")


def _text_item(text: str) -> LlmTurnUserItem:
    return LlmTurnUserItem(
        type="user", content=[LlmInputTextBlock(type="input_text", text=text)]
    )
