"""One memory update as a model conversation on the shared loop.

The harness adds what every memory edit run needs around the editing tools: a
run-local store that keeps an oversized tool result out of the conversation and
the `tool_result_fetch` tool that pages it back, and the `completed` ending
tool that reports which memory requests the run applied. A run is never
resumed -- a stopped job prepares a fresh workspace and starts over -- so the
history stays in memory.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from pantaray_agents.agents.artifact_react import COMPLETED_TOOL_NAME
from pantaray_agents.config_tunables import load_local_runtime_tunables
from pantaray_agents.conversation import provider_turns
from pantaray_agents.conversation.budget import ContextBudget, UsageTotals
from pantaray_agents.conversation.loop import (
    Continue,
    ConversationEntry,
    ConversationRun,
    Finish,
    IdleTurn,
    RecordedTurn,
    SendTurn,
    run_conversation,
)
from pantaray_agents.conversation.window import WindowState
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tools.contract import ReactToolDefinition, ReactToolResult
from pantaray_llm.contracts.conversation import (
    LlmTurnItem,
    LlmTurnToolResultItem,
    LlmTurnUserItem,
)
from pantaray_llm.contracts.input_block import LlmInputTextBlock
from pantaray_llm.contracts.tool_use import LlmToolCall, LlmToolDefinition
from pantaray_llm.profiles import MEMORY_UPDATE_PROFILE_ID

from .tool_result_projection import project_tool_result
from .tool_result_store import TOOL_RESULT_FETCH_TOOL_NAME, RunToolResultStore

APPLIED_MEMORY_REQUESTS_ARG = "applied_memory_requests"
_COMPLETE_REQUIRED = (
    "Respond with tool calls. When the memory update is correct and complete, "
    f"call `{COMPLETED_TOOL_NAME}` alone."
)


@dataclass(frozen=True, slots=True)
class MemoryFileEditorRunInput:
    run_id: str
    # The runner borrows this descriptor and owns only its internal duplicate.
    tool_result_directory_fd: int
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


async def run_memory_file_editor(
    run_input: MemoryFileEditorRunInput,
) -> tuple[str, ...]:
    """Run the update to its accepted `completed`; return the applied requests."""

    if any(
        definition.name == TOOL_RESULT_FETCH_TOOL_NAME
        for definition in run_input.tool_definitions
    ):
        raise ValueError(
            f"{TOOL_RESULT_FETCH_TOOL_NAME} is reserved by the memory harness"
        )
    result_store = RunToolResultStore(
        directory_fd=os.dup(run_input.tool_result_directory_fd)
    )

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

    async def on_result(
        call: LlmToolCall, result: ReactToolResult
    ) -> LlmTurnToolResultItem:
        # The loop awaits this to its end through a stop, so the store is
        # never closed under a projection still writing to it.
        projected = await asyncio.to_thread(
            project_tool_result, result=result, store=result_store
        )
        return LlmTurnToolResultItem(
            type="tool_result",
            call_id=call.call_id,
            name=call.name,
            output=projected.output,
        )

    # Memory update runs on the models an Action does, under the same input cap.
    tunables = load_local_runtime_tunables().action_agent
    identity, _ = provider_turns.read_provider_turn_target(
        inference_profile=MEMORY_UPDATE_PROFILE_ID
    )
    try:
        applied = await run_conversation(
            ConversationRun(
                prompt=run_input.prompt,
                system_instruction=run_input.system_instruction,
                tools=(*run_input.tool_definitions, result_store.fetch_definition()),
                ending_tools=(_completed_tool(run_input.memory_request_ids),),
                history=(ConversationEntry(_user_text(run_input.task)),),
                provider_turns=provider_turns.ProviderTurnStore(identity),
                inference_profile=MEMORY_UPDATE_PROFILE_ID,
                max_turns=run_input.max_turns,
                max_tool_calls=run_input.max_tool_calls,
                max_parallel_tool_calls=tunables.max_parallel_tool_calls,
                window=WindowState(
                    budget=ContextBudget(
                        window_tokens=tunables.context_window_tokens,
                        baseline=None,
                        reset_pending=False,
                    ),
                    omit_before=0,
                ),
                usage=run_input.usage,
                send=run_input.send,
                before_send=_nothing_arrives,
                on_turn=_keep_nothing,
                on_result=on_result,
                on_notice=_keep_nothing,
                decide=decide,
            )
        )
    except BaseException as run_error:
        try:
            result_store.close()
        except BaseException as close_error:
            run_error.add_note(
                "run tool-result store close failed: "
                f"{type(close_error).__name__}: {close_error}"
            )
        raise
    result_store.close()
    return applied


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
    "MemoryFileEditorRunInput",
    "run_memory_file_editor",
]
