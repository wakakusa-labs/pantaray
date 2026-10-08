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
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Final

from pantaray_agents.agents.chat_agent.context import (
    CHAT_HEAD,
    ChatWindow,
    render_item,
    turn_context,
)
from pantaray_agents.agents.chat_agent.media import ItemMedia, load_item_media
from pantaray_agents.agents.chat_agent.reply import (
    REPLY_TOOL,
    REPLY_TOOL_NAME,
    check_reply,
)
from pantaray_agents.agents.core.llm_file_inputs import (
    LlmFileInput,
    tool_image_file_input,
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
    TurnChatItem,
    append_chat_item,
    chat_turn_end_message_id,
    chat_turn_message_id,
    read_chat_items_for_turn,
    read_chat_turn_marks,
)
from pantaray_agents.local_runtime.chat.work_list import read_chat_work_list
from pantaray_agents.local_runtime.runtime.identity import (
    OwnerMismatchError,
    verify_current_owner,
)
from pantaray_agents.local_runtime.runtime.route_identity import (
    EffectiveRouteIdentity,
    effective_route_identity,
    read_route_inputs,
)
from pantaray_agents.schema.chat import (
    AssistantMessageContent,
    ChatItemContent,
    ChatTurnFailureReason,
    TurnFailureContent,
)
from pantaray_agents.tools.contract import (
    ReactToolCall,
    ReactToolDefinition,
    ReactToolResult,
)
from pantaray_agents.utils.local_time import local_now_for_model
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
    "You are Pantaray. You are not a general-purpose assistant waiting for "
    "questions: you work alongside this one person in two directions. When "
    "they hand you something, you take it on as your own task and see it "
    "through. And without being asked, you notice from their work what they "
    "will need -- a next step, a task you could take off their hands -- and "
    "bring it to them as a suggestion, which you carry out when they agree. "
    "That is who you are when they ask.\n"
    "This chat is where the two of you talk, one ongoing conversation, the way "
    "a capable colleague does in a messaging app: short, plain sentences in "
    "the language of their latest message. What you send appears in a chat "
    "bubble exactly as typed, so Markdown never renders there; what would be a "
    "list or a heading reads as ordinary sentences. "
    "The tasks in your work list are the user's tasks that you are working on "
    "yourself, and the suggestions there are yours.\n"
    "Your tasks move only through your tools: a task starts or hears from you "
    'when a call says it did, and a result that begins with "Not done" '
    "means nothing happened, which is what you then tell the user.\n"
    "Text you write beside a tool call reaches the user while the call runs, "
    "so a look-up can open with one short line on what you are checking. "
    "reply carries the answer itself and ends your turn."
)
_NUDGE: Final[str] = (
    "Your answer has not reached the user: only what you send with reply does."
)
_REPEATED: Final[str] = (
    "Not sent: the user already sees these words, which you wrote beside a call "
    "earlier in this turn. Send with reply only what they have not read yet."
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

# Sends one request; ``before_attempt`` runs before every attempt, a transport
# retry included, and raises to stop it.
# Sends one request; ``before_attempt`` runs before every attempt, a transport
# retry included, and raises to stop it. The user's images the request shows
# are handed in by their ref.
type ChatSend = Callable[
    [ConversationRequest, TokenSink, Callable[[], None], Mapping[str, LlmFileInput]],
    Awaitable[TurnReply],
]


@dataclass(frozen=True, slots=True)
class ChatTurnPlan:
    """One turn to run.

    ``key`` is the same each time this turn runs again -- after a crash, or as
    a retry of its failure -- so what it does is keyed by it, and a task it
    started is never started twice. ``attempt`` tells a retry's own items
    apart from the failed run's. Trigger items after ``cursor`` wait for it.
    """

    user_id: str
    key: str
    cursor: int
    attempt: str = ""

    @property
    def item_key(self) -> str:
        return self.key if not self.attempt else f"{self.key}.{self.attempt}"


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
        self,
        request: ConversationRequest,
        sink: TokenSink,
        before_attempt: Callable[[], None],
        user_images: Mapping[str, LlmFileInput],
    ) -> ActionTurnReply:
        tool_images = {
            image.ref: tool_image_file_input(image) for image in request.images
        }
        files = {**user_images, **tool_images}
        return await self._generate_llm_action_turn(
            sink=sink,
            before_attempt=before_attempt,
            # The items place each image; the request uploads what they show.
            file_inputs=[files[ref] for ref in request.media_refs if ref in files],
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
        assert marks.failed_turn_key is not None  # a failure names its turn
        return ChatTurnPlan(
            user_id=user_id,
            key=marks.failed_turn_key,
            cursor=marks.replied_through,
            attempt=f"r{failure.sequence}",
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
        # A send that failed after the route changed leaves its items waiting
        # for the turn that runs again on the new route.
        turn.require_route()
        reason = _failure_reason(exc)
        log_structured_event(
            logger,
            level="error" if reason == "internal" else "warning",
            evt="CHAT_TURN_FAILED",
            component="agents.chat_agent",
            exception=exc if reason == "internal" else None,
            reason=reason,
            error_class=type(exc).__name__,
            error_code=getattr(exc, "error_code", None),
            # What each refused output broke, without the details, which may
            # quote the model's output.
            violations=(
                [str(cause).partition(":")[0] for cause in exc.args]
                if isinstance(exc, ConversationOutputInvalid)
                else None
            ),
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
    said: set[str] = field(default_factory=set)  # shown beside calls this turn
    repeated: bool = False
    route: EffectiveRouteIdentity | None = None  # what the turn started on
    # The user's images the turn has shown, by the ref the items carry.
    user_images: dict[str, LlmFileInput] = field(default_factory=dict)

    async def run(
        self, send: ChatSend, tools: tuple[ReactToolDefinition, ...]
    ) -> AssistantMessageContent:
        self.route = effective_route_identity(read_route_inputs())
        # The owner may have changed since the plan: nothing of this chat is
        # read for, or sent on, anyone else's route.
        if self.route.owner_id != self.plan.user_id:
            raise ChatTurnInterrupted("the chat's owner no longer owns the data")
        sink = CountingSink()

        async def send_turn(request: ConversationRequest) -> TurnReply:
            return await send(request, sink, self.require_route, self.user_images)

        # A retry answers items from before the window's boundary too, and a
        # waiting item is never left out of the turn that answers it.
        self.window = replace(
            self.window, after=min(self.window.after, self.plan.cursor)
        )
        items = await asyncio.to_thread(
            read_chat_items_for_turn, user_id=self.plan.user_id, after=self.window.after
        )
        self.seen = items[-1].item.sequence if items else self.window.after
        work = await asyncio.to_thread(read_chat_work_list, user_id=self.plan.user_id)
        media = await self.load_media(items)
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
            tail=turn_context(waiting, work, now=local_now_for_model()),
            head_bytes=input_bytes(CHAT_HEAD, CHAT_SYSTEM_INSTRUCTION)
            + sum(len(tool.model_dump_json().encode()) for tool in definitions),
            media=media,
        )
        return await run_conversation(
            ConversationRun(
                prompt=CHAT_HEAD,
                system_instruction=CHAT_SYSTEM_INSTRUCTION,
                tools=tuple(self.guarded(tool) for tool in tools),
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
        triggers = [
            item for item in arrived if item.item.content.kind in CHAT_TRIGGER_KINDS
        ]
        media = await self.load_media(triggers)
        return [entry.item for item in triggers for entry in render_item(item, media)]

    async def load_media(self, items: Sequence[TurnChatItem]) -> ItemMedia:
        media = await asyncio.to_thread(
            load_item_media, user_id=self.plan.user_id, items=items
        )
        self.user_images.update(
            (file_input["ref"], file_input) for _, file_input in media.images.values()
        )
        return media

    def guarded(self, tool: ReactToolDefinition) -> ReactToolDefinition:
        """``tool``, refusing to start once the route has changed.

        The loop runs a turn's calls one after another, so a change during one
        call must stop the next before it starts.
        """

        async def execute(call: ReactToolCall, step: int) -> ReactToolResult:
            self.require_route()
            return await tool.execute(call, step)

        return replace(tool, execute=execute)

    def require_route(self) -> None:
        """A turn started for one owner and account never reaches another."""

        if effective_route_identity(read_route_inputs()) != self.route:
            raise ChatTurnInterrupted("the route changed under the turn")

    async def on_turn(self, turn: RecordedTurn) -> None:
        self.window = replace(self.window, budget=turn.window.budget)
        # A reply that came back after the route changed runs none of its calls.
        self.require_route()
        response = turn.reply.response
        # Said beside calls that run, so the user reads it before they do.
        # Beside a reply alone nothing runs, and the reply says it.
        if any(call.name != REPLY_TOOL_NAME for call in response.calls):
            for index, message in enumerate(response.messages):
                self.said.add(message.text.strip())
                await self.append(
                    chat_turn_message_id(
                        self.plan.item_key, f"say/{turn.entry.turn_id}/{index}"
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
        # Checked again once its own awaits are done: the calls run right after.
        self.require_route()

    def decide(self, idle: IdleTurn) -> Finish[AssistantMessageContent] | Continue:
        if idle.ending_call is not None:
            verdict = self.checked[idle.ending_call.call_id]
            if isinstance(verdict, str):
                return Continue(verdict)
            if verdict.text in self.said and not self.repeated and not idle.final:
                # Appended, it would show the user the same words twice.
                self.repeated = True
                return Continue(_REPEATED)
            return Finish(verdict)
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
            chat_turn_end_message_id(self.plan.item_key, end, self.seen), content
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
