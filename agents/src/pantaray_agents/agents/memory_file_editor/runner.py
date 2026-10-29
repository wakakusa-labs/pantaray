"""One memory update as a model conversation on the shared loop.

The harness adds what every memory edit run needs around the editing tools: an
oversized tool result is saved out of the conversation by the caller's
`save_result`, the caller's `read` pages through what was saved, and the
`completed` ending tool reports which memory requests the run applied. A run is never
resumed -- a stopped job prepares a fresh workspace and starts over -- so the
history stays in memory, where a later stage can continue it behind the same head.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from pantaray_agents.agents.artifact_react import COMPLETED_TOOL_NAME
from pantaray_agents.config_tunables import load_local_runtime_tunables
from pantaray_agents.conversation.budget import ContextBudget, UsageTotals
from pantaray_agents.conversation.loop import (
    Continue,
    ConversationEntry,
    ConversationRun,
    Ended,
    Finish,
    IdleTurn,
    RecordedTurn,
    SendTurn,
    run_conversation_to_end,
)
from pantaray_agents.conversation.provider_turns import (
    ProviderTurnStore,
    read_provider_turn_target,
)
from pantaray_agents.conversation.window import WindowState
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tools.contract import (
    ReactToolDefinition,
    ReactToolResult,
    ToolImage,
)
from pantaray_llm.contracts.conversation import (
    LlmTurnItem,
    LlmTurnToolResultItem,
    LlmTurnUserItem,
)
from pantaray_llm.contracts.input_block import LlmInputTextBlock
from pantaray_llm.contracts.tool_use import LlmToolCall, LlmToolDefinition
from pantaray_llm.profiles import MEMORY_UPDATE_PROFILE_ID

APPLIED_MEMORY_REQUESTS_ARG = "applied_memory_requests"
_COMPLETE_REQUIRED = (
    "Respond with tool calls. When the memory update is correct and complete, "
    f"call `{COMPLETED_TOOL_NAME}` alone."
)


@dataclass(frozen=True, slots=True)
class MemoryFileEditorRunInput:
    run_id: str
    # What the model reads in place of a tool's output: an oversized one with its
    # long texts saved to files, and `read_saved_results` to read those back.
    save_result: Callable[[JSONValue], JSONValue]
    read_saved_results: ReactToolDefinition
    tool_definitions: tuple[ReactToolDefinition, ...]
    # The request's own message every turn is sent behind, and the run's task,
    # which leads the history.
    prompt: str
    task: str
    system_instruction: str
    send: SendTurn
    usage: Callable[[], UsageTotals]
    max_turns: int
    max_tool_calls: int
    # The memory requests this run renders; `completed` reports which it applied.
    memory_request_ids: tuple[str, ...] = ()


def _completed_tool(memory_request_ids: tuple[str, ...]) -> LlmToolDefinition:
    properties: dict[str, JSONValue] = {}
    if memory_request_ids:
        properties[APPLIED_MEMORY_REQUESTS_ARG] = {
            "type": "array",
            "items": {"type": "string", "enum": list(memory_request_ids)},
            "uniqueItems": True,
            "description": (
                "request_id of every memory request now reflected in memory. "
                "Leave out a request you did not apply."
            ),
        }
    return LlmToolDefinition(
        name=COMPLETED_TOOL_NAME,
        description=(
            "Finish only when the evidence-scoped memory update is correct and "
            "no further tool calls are needed."
        ),
        parameters={
            "type": "object",
            "additionalProperties": False,
            "required": list(properties),
            "properties": properties,
        },
    )


@dataclass(frozen=True, slots=True)
class MemoryEditorConversation:
    """An ended run, which a later stage continues behind the same head.

    ``history`` ends with the accepted ``completed`` call, which nothing has
    answered: the stage that continues answers it first.
    """

    run_input: MemoryFileEditorRunInput
    applied_memory_request_ids: tuple[str, ...]
    ending_call: LlmToolCall
    history: tuple[ConversationEntry, ...]
    images: Mapping[str, ToolImage]
    window: WindowState
    turn_store: ProviderTurnStore

    async def continue_with(
        self,
        *,
        answer: JSONValue,
        message: str,
        tools: tuple[ReactToolDefinition, ...],
        max_turns: int,
        max_tool_calls: int,
    ) -> None:
        """Answer ``completed`` with ``answer``, then go on from ``message`` to
        the next accepted ``completed``; the tools edit the same draft.

        The window omits older tool outputs but never a user item, so what the
        stage must keep in view goes in ``message``.
        """

        opening = (
            ConversationEntry(
                LlmTurnToolResultItem(
                    type="tool_result",
                    call_id=self.ending_call.call_id,
                    name=self.ending_call.name,
                    output=answer,
                )
            ),
            ConversationEntry(_user_text(message)),
        )
        await _converse(
            self.run_input,
            tools=tools,
            ending_tool=_completed_tool(()),
            history=(*self.history, *opening),
            images=self.images,
            window=self.window,
            store=self.turn_store,
            decide=_completed_alone,
            max_turns=max_turns,
            max_tool_calls=max_tool_calls,
        )


async def run_memory_file_editor(
    run_input: MemoryFileEditorRunInput,
) -> MemoryEditorConversation:
    """Run the update to its accepted `completed`; keep the conversation."""

    def decide(turn: IdleTurn) -> Finish[tuple[str, ...]] | Continue:
        if turn.ending_call is None:
            return Continue(_COMPLETE_REQUIRED)
        applied = turn.ending_call.arguments.get(APPLIED_MEMORY_REQUESTS_ARG, [])
        if not isinstance(applied, list) or not all(
            isinstance(item, str) for item in applied
        ):
            return Continue(
                f"{APPLIED_MEMORY_REQUESTS_ARG} must be a list of request_id strings."
            )
        return Finish(tuple(item for item in applied if isinstance(item, str)))

    # Memory update runs on the models an Action does, under the same input cap.
    tunables = load_local_runtime_tunables().action_agent
    identity, _ = read_provider_turn_target(inference_profile=MEMORY_UPDATE_PROFILE_ID)
    store = ProviderTurnStore(identity)
    ended, ending_call = await _converse(
        run_input,
        tools=run_input.tool_definitions,
        ending_tool=_completed_tool(run_input.memory_request_ids),
        history=(ConversationEntry(_user_text(run_input.task)),),
        images={},
        window=WindowState(
            budget=ContextBudget(
                window_tokens=tunables.context_window_tokens,
                baseline=None,
                reset_pending=False,
            ),
            omit_before=0,
        ),
        store=store,
        decide=decide,
        max_turns=run_input.max_turns,
        max_tool_calls=run_input.max_tool_calls,
    )
    return MemoryEditorConversation(
        run_input=run_input,
        applied_memory_request_ids=ended.value,
        ending_call=ending_call,
        history=ended.history,
        images=ended.images,
        window=ended.window,
        turn_store=store,
    )


async def _converse[T](
    run_input: MemoryFileEditorRunInput,
    *,
    tools: tuple[ReactToolDefinition, ...],
    ending_tool: LlmToolDefinition,
    history: Sequence[ConversationEntry],
    images: Mapping[str, ToolImage],
    window: WindowState,
    store: ProviderTurnStore,
    decide: Callable[[IdleTurn], Finish[T] | Continue],
    max_turns: int,
    max_tool_calls: int,
) -> tuple[Ended[T], LlmToolCall]:
    """One stage behind the run's head; returns its end and the accepted call."""

    ending: list[LlmToolCall] = []

    def judge(turn: IdleTurn) -> Finish[T] | Continue:
        decision = decide(turn)
        if isinstance(decision, Finish) and turn.ending_call is not None:
            ending.append(turn.ending_call)
        return decision

    async def on_result(
        call: LlmToolCall, result: ReactToolResult
    ) -> LlmTurnToolResultItem:
        return LlmTurnToolResultItem(
            type="tool_result",
            call_id=call.call_id,
            name=call.name,
            output=await asyncio.to_thread(run_input.save_result, result.output),
        )

    ended = await run_conversation_to_end(
        ConversationRun(
            prompt=run_input.prompt,
            system_instruction=run_input.system_instruction,
            tools=(*tools, run_input.read_saved_results),
            ending_tools=(ending_tool,),
            history=history,
            images=images,
            provider_turns=store,
            inference_profile=MEMORY_UPDATE_PROFILE_ID,
            max_turns=max_turns,
            max_tool_calls=max_tool_calls,
            max_parallel_tool_calls=(
                load_local_runtime_tunables().action_agent.max_parallel_tool_calls
            ),
            window=window,
            usage=run_input.usage,
            send=run_input.send,
            before_send=_nothing_arrives,
            on_turn=_keep_nothing,
            on_result=on_result,
            on_notice=_keep_nothing,
            decide=judge,
        )
    )
    return ended, ending[-1]


def _completed_alone(turn: IdleTurn) -> Finish[None] | Continue:
    if turn.ending_call is None:
        return Continue(_COMPLETE_REQUIRED)
    return Finish(None)


async def _nothing_arrives() -> Sequence[LlmTurnItem]:
    return ()


async def _keep_nothing(_item: RecordedTurn | LlmTurnUserItem) -> None:
    return None


def _user_text(text: str) -> LlmTurnUserItem:
    return LlmTurnUserItem(
        type="user", content=[LlmInputTextBlock(type="input_text", text=text)]
    )


__all__ = [
    "APPLIED_MEMORY_REQUESTS_ARG",
    "MemoryEditorConversation",
    "MemoryFileEditorRunInput",
    "run_memory_file_editor",
]
