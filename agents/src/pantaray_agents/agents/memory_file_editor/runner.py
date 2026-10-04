from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pantaray_agents.agents.artifact_react import (
    COMPLETED_TOOL_NAME,
    ReactLoopPolicy,
    ReactLoopResult,
    ReactLoopStep,
    ReactToolDefinition,
    ReactToolResult,
)
from pantaray_agents.agents.artifact_react.native_runner import (
    NativeReactCompletion,
    NativeReactRunInput,
    run_native_react,
)
from pantaray_agents.agents.core.mixins.llm_tool_use_mixin import LlmToolCallTurn
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_llm.contracts.tool_use import (
    LlmToolContinuation,
    LlmToolDefinition,
    LlmToolResult,
)

from .tool_result_projection import project_tool_result
from .tool_result_store import TOOL_RESULT_FETCH_TOOL_NAME, RunToolResultStore

type MemoryLlmCaller = Callable[
    [
        str,
        tuple[LlmToolDefinition, ...],
        LlmToolContinuation | None,
        LlmToolResult | None,
    ],
    Awaitable[LlmToolCallTurn],
]
type MemoryStepRecorder = Callable[[ReactLoopStep], Awaitable[None]]
type MemoryPromptBuilder = Callable[[tuple[ReactToolResult, ...], str | None], str]
type MemoryThoughtConsumer = Callable[[], str | None]
type MemoryCompletionValidator = Callable[[], str | None]
type SyncOperation[T] = Callable[[], T]

APPLIED_MEMORY_REQUESTS_ARG = "applied_memory_requests"


@dataclass(frozen=True, slots=True)
class MemoryFileEditorRunInput:
    run_id: str
    # The runner borrows this descriptor and owns only its internal duplicate.
    tool_result_directory_fd: int
    tool_definitions: tuple[ReactToolDefinition, ...]
    build_prompt: MemoryPromptBuilder
    call_llm: MemoryLlmCaller
    record_step: MemoryStepRecorder
    policy: ReactLoopPolicy
    consume_llm_thoughts: MemoryThoughtConsumer | None = None
    validate_completion: MemoryCompletionValidator | None = None
    # The memory requests this run renders; `completed` reports which it applied.
    memory_request_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MemoryFileEditorRunResult:
    loop_result: ReactLoopResult
    applied_memory_request_ids: tuple[str, ...]


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
) -> MemoryFileEditorRunResult:
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

    def complete(
        arguments: dict[str, JSONValue],
        _final_turn: bool,
    ) -> NativeReactCompletion[tuple[str, ...]]:
        applied = arguments.get(APPLIED_MEMORY_REQUESTS_ARG, [])
        applied_ids = (
            tuple(item for item in applied if isinstance(item, str))
            if isinstance(applied, list)
            else ()
        )
        if not isinstance(applied, list) or len(applied_ids) != len(applied):
            return NativeReactCompletion(
                value=None,
                final_text="",
                error_message=(
                    f"{APPLIED_MEMORY_REQUESTS_ARG} must be a list of request_id "
                    "strings."
                ),
            )
        completion_error = (
            run_input.validate_completion()
            if run_input.validate_completion is not None
            else None
        )
        if completion_error is not None:
            return NativeReactCompletion(
                value=None,
                final_text="",
                error_message=completion_error,
            )
        return NativeReactCompletion(value=applied_ids, final_text="")

    async def project_result(result: ReactToolResult) -> ReactToolResult:
        return await _run_sync_to_completion(
            lambda: project_tool_result(result=result, store=result_store)
        )

    try:
        result = await run_native_react(
            NativeReactRunInput(
                run_id=run_input.run_id,
                tool_definitions=(
                    *run_input.tool_definitions,
                    result_store.fetch_definition(),
                ),
                terminal_tool=_completed_tool(run_input.memory_request_ids),
                complete=complete,
                build_prompt=run_input.build_prompt,
                call_llm=run_input.call_llm,
                record_step=run_input.record_step,
                project_tool_result=project_result,
                policy=run_input.policy,
                consume_llm_thoughts=run_input.consume_llm_thoughts,
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
    else:
        result_store.close()
        return MemoryFileEditorRunResult(
            loop_result=result.loop_result,
            applied_memory_request_ids=result.value or (),
        )


async def _run_sync_to_completion[T](operation: SyncOperation[T]) -> T:
    worker = asyncio.create_task(asyncio.to_thread(operation))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError as cancellation:
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        try:
            worker.result()
        except BaseException as worker_error:
            cancellation.add_note(
                "background operation also failed after cancellation: "
                f"{type(worker_error).__name__}: {worker_error}"
            )
        raise


__all__ = [
    "APPLIED_MEMORY_REQUESTS_ARG",
    "MemoryFileEditorRunInput",
    "MemoryFileEditorRunResult",
    "run_memory_file_editor",
]
