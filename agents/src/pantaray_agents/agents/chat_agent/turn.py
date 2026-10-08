"""One chat turn: read the chat, let the model answer, append what it says.

A turn answers the trigger items past the last turn's end (``plan_chat_turn``).
It runs on the shared conversation loop. Text the model sends beside its calls
is appended at once, before the calls run; ``reply`` appends the answer and ends
the turn, and an answer given as text alone is sent back once to go through
``reply``. Items that arrive mid-turn are read before the next model call. The
end -- the reply, or a ``turn_failure`` -- records the newest item the turn
read, so an item that arrived after that waits for the next turn.

Nothing but the items the turn appends outlives it. A turn that stops before its
end (the app quits, the route changes) runs again under the same ``key`` and
reads what it already said back from the chat.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Final

from pantaray_agents.agents.chat_agent.context import (
    CHAT_HEAD,
    ChatWindow,
    render_item,
    turn_context,
)
from pantaray_agents.agents.chat_agent.reply import (
    REPLY_TOOL,
    REPLY_TOOL_NAME,
    check_reply,
)
from pantaray_agents.agents.core.mixins.llm_tool_use_mixin import (
    ActionTurnReply,
    LlmToolUseMixin,
)
from pantaray_agents.agents.core.mixins.llm_usage import CountingSink, TokenSink
from pantaray_agents.conversation.budget import ContextCapacityExceeded, input_bytes
from pantaray_agents.conversation.loop import (
    Continue,
    ConversationOutputInvalid,
    ConversationRequest,
    ConversationRun,
    ConversationTurnsExhausted,
    Finish,
    IdleTurn,
    RecordedTurn,
    TurnReply,
    run_conversation,
)
from pantaray_agents.conversation.provider_turns import ProviderTurnStore
from pantaray_agents.conversation.window import WindowState
from pantaray_agents.local_runtime.chat.store import (
    CHAT_TRIGGER_KINDS,
    ChatTurnEnd,
    append_chat_item,
    chat_turn_end_message_id,
    chat_turn_message_id,
    read_chat_items_for_turn,
    read_chat_turn_marks,
)
from pantaray_agents.local_runtime.runtime.identity import (
    OwnerMismatchError,
    verify_current_owner,
)
from pantaray_agents.local_runtime.runtime.route_identity import (
    effective_route_identity,
    read_route_inputs,
)
from pantaray_agents.schema.chat import (
    AssistantMessageContent,
    ChatItemContent,
    ChatTurnFailureReason,
    TurnFailureContent,
)
from pantaray_agents.tools.contract import ReactToolDefinition, ReactToolResult
from pantaray_agents.utils.structured_logging import (
    fingerprint_text,
    log_structured_event,
)
from pantaray_llm.contracts.conversation import (
    LlmTurnItem,
    LlmTurnToolResultItem,
    LlmTurnUserItem,
)
from pantaray_llm.contracts.tool_use import LlmToolCall, LlmToolDefinition
from pantaray_llm.errors import (
    PROXY_AUTHENTICATION_FAILED,
    PROXY_CONNECTION_NOT_CONFIGURED,
    PROXY_INSUFFICIENT_BALANCE,
    PROXY_UPSTREAM_FORBIDDEN,
    PROXY_WALLET_CHECK_FAILED,
    LlmProxyExecutionError,
)
from pantaray_llm.profiles import CHAT_PROFILE_ID

logger = logging.getLogger(__name__)

CHAT_SYSTEM_INSTRUCTION: Final[str] = (
    "You are Pantaray, talking with the user in one ongoing chat, the way a "
    "capable colleague does in a messaging app: short, plain sentences in the "
    "user's language.\n"
    "If you need to look something up, first say so in one short line, then "
    "look it up; if you can answer right away, answer."
)
_NUDGE: Final[str] = (
    "Your answer has not reached the user: only what you send with reply does."
)
# Model calls in one turn. A chat turn answers and hands long work to an Action,
# so a turn that needs more has gone wrong and ends as a `step_limit` failure.
CHAT_TURN_MAX_MODEL_CALLS: Final[int] = 8
CHAT_TURN_MAX_TOOL_CALLS: Final[int] = 16
CHAT_TURN_MAX_PARALLEL_TOOL_CALLS: Final[int] = 4
# The user's own LLM connection or account: fixing it is theirs, not a retry's.
_CONNECTION_ERROR_CODES: Final = frozenset(
    {
        PROXY_CONNECTION_NOT_CONFIGURED,
        PROXY_AUTHENTICATION_FAILED,
        PROXY_INSUFFICIENT_BALANCE,
        PROXY_WALLET_CHECK_FAILED,
        PROXY_UPSTREAM_FORBIDDEN,
    }
)

type ChatSend = Callable[[ConversationRequest, TokenSink], Awaitable[TurnReply]]


@dataclass(frozen=True, slots=True)
class ChatTurnPlan:
    """One turn to run.

    ``key`` is the same each time this turn runs again, so what it appends and
    does is keyed by it. Trigger items after ``cursor`` wait for this turn.
    """

    user_id: str
    key: str
    cursor: int


class ChatTurnInterrupted(RuntimeError):
    """The owner or the route changed under the turn, which ended unanswered."""


class ChatModel(LlmToolUseMixin):
    """The chat's model, reached on the user's connection under ``chat``."""

    DEFAULT_SYSTEM_INSTRUCTION = CHAT_SYSTEM_INSTRUCTION
    LLM_INFERENCE_PROFILE_ID = CHAT_PROFILE_ID

    def __init__(self, client: object) -> None:
        self.client = client
        self.llm_config = {}

    async def send(
        self, request: ConversationRequest, sink: TokenSink
    ) -> ActionTurnReply:
        return await self._generate_llm_action_turn(
            sink=sink,
            prompt=request.prompt,
            tools=request.tools,
            max_parallel_tool_calls=request.max_parallel_tool_calls,
            system_instruction=request.system_instruction,
            conversation=request.conversation,
        )


def plan_chat_turn(*, user_id: str, retry_of: str | None) -> ChatTurnPlan | None:
    """The turn the chat waits for, if any.

    ``retry_of`` names a ``turn_failure`` to run again; it counts only while it
    is still how the last turn ended, and then reads every item since the last
    reply. It reads the database, so an async caller runs it off the loop.
    Raises ``OwnerMismatchError`` when the user no longer owns the data.
    """

    verify_current_owner(user_id)
    marks = read_chat_turn_marks(user_id=user_id)
    failure = marks.last_failure
    if failure is not None and failure.item_id == retry_of:
        return ChatTurnPlan(
            user_id=user_id, key=f"r{failure.sequence}", cursor=marks.replied_through
        )
    if not marks.waiting:
        return None
    through = marks.answered_through
    return ChatTurnPlan(user_id=user_id, key=f"a{through}", cursor=through)


async def run_chat_turn(
    plan: ChatTurnPlan,
    *,
    send: ChatSend,
    tools: tuple[ReactToolDefinition, ...],
    window: ChatWindow,
) -> ChatWindow:
    """Run ``plan`` to its end, and return where the window stands after it.

    Its own reads, appends and window fitting run off the event loop. The
    shared loop still lays each request out and runs ``tools`` on the loop it
    is awaited on, so a caller on a loop that must stay responsive gives the
    turn a loop of its own.

    Raises ``ChatTurnInterrupted`` when the turn stopped before its end.
    """

    turn = _ChatTurn(plan=plan, window=window)
    try:
        answer = await turn.run(send, tools)
    except (ChatTurnInterrupted, OwnerMismatchError):
        raise
    except Exception as exc:
        reason = _failure_reason(exc)
        log_structured_event(
            logger,
            level="error" if reason == "internal" else "warning",
            evt="CHAT_TURN_FAILED",
            component="agents.chat_agent",
            exception=exc if reason == "internal" else None,
            reason=reason,
            error_code=getattr(exc, "error_code", None),
            user_id_fp=fingerprint_text(plan.user_id),
        )
        await turn.end(TurnFailureContent(kind="turn_failure", reason=reason))
        return turn.window
    await turn.end(answer)
    return turn.window


@dataclass(slots=True)
class _ChatTurn:
    plan: ChatTurnPlan
    window: ChatWindow
    seen: int = 0  # the newest item read so far
    checked: dict[str, AssistantMessageContent | str] = field(default_factory=dict)
    nudged: bool = False

    async def run(
        self, send: ChatSend, tools: tuple[ReactToolDefinition, ...]
    ) -> AssistantMessageContent:
        started = effective_route_identity(read_route_inputs())
        # The owner may have changed since the plan: nothing of this chat is
        # read for, or sent on, anyone else's route.
        if started.owner_id != self.plan.user_id:
            raise ChatTurnInterrupted("the chat's owner no longer owns the data")
        sink = CountingSink()

        async def send_turn(request: ConversationRequest) -> TurnReply:
            # A turn started for one owner and account never reaches another.
            if effective_route_identity(read_route_inputs()) != started:
                raise ChatTurnInterrupted("the route changed under the turn")
            return await send(request, sink)

        items = await asyncio.to_thread(
            read_chat_items_for_turn, user_id=self.plan.user_id, after=self.window.after
        )
        self.seen = items[-1].item.sequence if items else self.window.after
        waiting = [
            entry.item
            for entry in items
            if entry.item.sequence > self.plan.cursor
            and entry.item.content.kind in CHAT_TRIGGER_KINDS
        ]
        definitions = (REPLY_TOOL, *(_definition(tool) for tool in tools))
        # Rendering and measuring the whole window is CPU work: off the loop.
        self.window, history = await asyncio.to_thread(
            self.window.fit,
            items,
            waiting_from=waiting[0].sequence,
            tail=turn_context(waiting),
            head_bytes=input_bytes(CHAT_HEAD, CHAT_SYSTEM_INSTRUCTION)
            + sum(len(tool.model_dump_json().encode()) for tool in definitions),
        )
        return await run_conversation(
            ConversationRun(
                prompt=CHAT_HEAD,
                system_instruction=CHAT_SYSTEM_INSTRUCTION,
                tools=tools,
                ending_tools=(REPLY_TOOL,),
                history=history,
                provider_turns=ProviderTurnStore(identity=None),
                inference_profile=CHAT_PROFILE_ID,
                max_turns=CHAT_TURN_MAX_MODEL_CALLS,
                max_tool_calls=CHAT_TURN_MAX_TOOL_CALLS,
                max_parallel_tool_calls=CHAT_TURN_MAX_PARALLEL_TOOL_CALLS,
                window=WindowState(budget=self.window.budget, omit_before=0),
                usage=lambda: sink.delta,
                send=send_turn,
                before_send=self.read_arrivals,
                on_turn=self.on_turn,
                on_result=_result_item,
                on_notice=_keep_notice,
                decide=self.decide,
            )
        )

    async def read_arrivals(self) -> list[LlmTurnItem]:
        arrived = await asyncio.to_thread(
            read_chat_items_for_turn, user_id=self.plan.user_id, after=self.seen
        )
        if arrived:
            self.seen = arrived[-1].item.sequence
        # What else arrives is this turn's own: it appends one turn at a time.
        return [
            entry.item
            for item in arrived
            if item.item.content.kind in CHAT_TRIGGER_KINDS
            for entry in render_item(item)
        ]

    async def on_turn(self, turn: RecordedTurn) -> None:
        self.window = replace(self.window, budget=turn.window.budget)
        response = turn.reply.response
        if response.calls:
            # Said beside the calls, so the user reads it before they run.
            for index, message in enumerate(response.messages):
                await self.append(
                    chat_turn_message_id(
                        self.plan.key, f"say/{turn.entry.turn_id}/{index}"
                    ),
                    AssistantMessageContent(
                        kind="assistant_message",
                        text=message.text,
                        quote_item_id=None,
                        cards=(),
                    ),
                )
        for call in response.calls:
            if call.name == REPLY_TOOL_NAME:
                self.checked[call.call_id] = await asyncio.to_thread(
                    check_reply, user_id=self.plan.user_id, arguments=call.arguments
                )

    def decide(self, idle: IdleTurn) -> Finish[AssistantMessageContent] | Continue:
        if idle.ending_call is not None:
            verdict = self.checked[idle.ending_call.call_id]
            return Continue(verdict) if isinstance(verdict, str) else Finish(verdict)
        response = idle.response
        if response.calls:
            # None of them ran, and what was said beside them is already shown.
            return Continue(_NUDGE)
        if self.nudged or idle.final:
            # Sent back once already, or out of calls: the text is the answer.
            return Finish(
                AssistantMessageContent(
                    kind="assistant_message",
                    text="\n\n".join(message.text for message in response.messages),
                    quote_item_id=None,
                    cards=(),
                )
            )
        self.nudged = True
        return Continue(_NUDGE)

    async def end(self, content: ChatItemContent) -> None:
        end: ChatTurnEnd = "failure" if content.kind == "turn_failure" else "reply"
        await self.append(
            chat_turn_end_message_id(self.plan.key, end, self.seen), content
        )

    async def append(self, message_id: str, content: ChatItemContent) -> None:
        await asyncio.to_thread(
            append_chat_item,
            user_id=self.plan.user_id,
            message_id=message_id,
            content=content,
        )


def _definition(tool: ReactToolDefinition) -> LlmToolDefinition:
    return LlmToolDefinition(
        name=tool.name,
        description=tool.description,
        parameters=dict(tool.request_schema),
    )


async def _result_item(
    call: LlmToolCall, result: ReactToolResult
) -> LlmTurnToolResultItem:
    return LlmTurnToolResultItem(
        type="tool_result", call_id=call.call_id, name=call.name, output=result.output
    )


async def _keep_notice(_item: LlmTurnUserItem) -> None:
    # A notice lives in this turn's history only; the next turn starts anew.
    return None


def _failure_reason(exc: Exception) -> ChatTurnFailureReason:
    if isinstance(exc, ConversationTurnsExhausted):
        return "step_limit"
    if isinstance(exc, LlmProxyExecutionError):
        if exc.retryable or exc.error_code in _CONNECTION_ERROR_CODES:
            return "llm_connection"
        return "llm_request"
    if isinstance(exc, ConversationOutputInvalid | ContextCapacityExceeded):
        return "llm_request"
    return "internal"


__all__ = [
    "CHAT_SYSTEM_INSTRUCTION",
    "ChatModel",
    "ChatSend",
    "ChatTurnInterrupted",
    "ChatTurnPlan",
    "plan_chat_turn",
    "run_chat_turn",
]
