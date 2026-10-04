from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace

from pantaray_agents.agents.core.mixins.llm_tool_use_mixin import LlmToolCallTurn
from pantaray_agents.agents.core.tool_call_repair import (
    build_tool_call_repair_feedback,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_llm.contracts.tool_use import (
    LlmToolContinuation,
    LlmToolDefinition,
    LlmToolResult,
)
from pantaray_llm.errors import LlmProxyExecutionError

from .native_tool_calls import (
    NativeReactSkippedCall,
    NativeReactTurnInterrupt,
    NativeReactTurnPlan,
    execute_planned_turn,
    project_tool_result,
    react_tool_call,
    run_tool,
    tool_step,
)
from .tooling import ReactToolDefinition, ReactToolRegistry
from .types import (
    ReactLoopPolicy,
    ReactLoopResult,
    ReactLoopStep,
    ReactToolResult,
)

type NativeReactLlmCaller = Callable[
    [
        str,
        tuple[LlmToolDefinition, ...],
        LlmToolContinuation | None,
        LlmToolResult | None,
    ],
    Awaitable[LlmToolCallTurn],
]
type NativeReactStepRecorder = Callable[[ReactLoopStep], Awaitable[None]]
type NativeReactPromptBuilder = Callable[[tuple[ReactToolResult, ...], str | None], str]
type NativeReactThoughtConsumer = Callable[[], str | None]
type NativeReactToolResultProjector = Callable[
    [ReactToolResult], Awaitable[ReactToolResult]
]

LLM_PROXY_ERROR_MESSAGE = "LLM provider request failed."
OMITTED_TOOL_OUTPUT: JSONValue = {
    "omitted": (
        "Output omitted to keep the request within the model's input limit; "
        "call the tool again if you still need it."
    )
}


@dataclass(frozen=True, slots=True)
class NativeReactCompletion[T]:
    value: T | None
    final_text: str
    error_message: str | None = None

    def __post_init__(self) -> None:
        if self.error_message is None and self.value is None:
            raise ValueError("successful completion requires a value")
        if self.error_message is not None and self.value is not None:
            raise ValueError("rejected completion must not contain a value")


# The bool is `final_turn`: the loop can no longer offer a research tool, so a
# rejected completion fails the whole run. Handlers that gate the terminal tool on
# work still outstanding decide there whether to reject or to finish degraded.
type NativeReactCompletionHandler[T] = Callable[
    [dict[str, JSONValue], bool], NativeReactCompletion[T]
]


# The int is how many tool calls the run may still make.
type NativeReactTurnPlanner = Callable[[LlmToolCallTurn, int], NativeReactTurnPlan]


@dataclass(frozen=True, slots=True)
class NativeReactRunInput[T]:
    run_id: str
    tool_definitions: tuple[ReactToolDefinition, ...]
    terminal_tool: LlmToolDefinition
    complete: NativeReactCompletionHandler[T]
    build_prompt: NativeReactPromptBuilder
    call_llm: NativeReactLlmCaller
    record_step: NativeReactStepRecorder
    project_tool_result: NativeReactToolResultProjector
    policy: ReactLoopPolicy
    consume_llm_thoughts: NativeReactThoughtConsumer | None = None
    final_turn_prompt: str | None = None
    # None runs only a turn's first call. A planner lets a turn run several;
    # its caller sends no continuation, which carries one result per turn.
    plan_turn: NativeReactTurnPlanner | None = None
    turn_interrupt: NativeReactTurnInterrupt | None = None

    def __post_init__(self) -> None:
        if not self.run_id.strip():
            raise ValueError("run_id must not be empty")


@dataclass(frozen=True, slots=True)
class NativeReactRunResult[T]:
    loop_result: ReactLoopResult
    value: T | None


def build_native_tool_definitions(
    *,
    definitions: tuple[ReactToolDefinition, ...],
    terminal_tool: LlmToolDefinition,
) -> tuple[LlmToolDefinition, ...]:
    return (
        terminal_tool,
        *(
            LlmToolDefinition(
                name=tool.name,
                description=tool.description,
                parameters=dict(tool.request_schema),
            )
            for tool in definitions
        ),
    )


def _response_text(turn: LlmToolCallTurn) -> str:
    return json.dumps(
        {"tool_id": turn.call.name, "args": turn.call.arguments},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _utf8_size(value: str) -> int:
    return len(value.encode())


def _exceeds_input_limit(
    policy: ReactLoopPolicy,
    continuation: LlmToolContinuation | None,
    pending_result: LlmToolResult | None,
) -> bool:
    if policy.max_input_bytes is None or continuation is None:
        return False
    size = _utf8_size(continuation.model_dump_json())
    if pending_result is not None:
        size += _utf8_size(pending_result.model_dump_json())
    return size > policy.max_input_bytes


def _build_bounded_prompt[T](
    run_input: NativeReactRunInput[T],
    tool_results: list[ReactToolResult],
    last_error: str | None,
    *,
    compact: bool,
) -> str:
    """Omit the oldest tool outputs until the prompt fits the input limit.

    A reset past the limit compacts to half of it, so the continuation can grow
    for many turns before the next reset instead of resetting on every turn.
    """
    prompt = run_input.build_prompt(tuple(tool_results), last_error)
    limit = run_input.policy.max_input_bytes
    if limit is None:
        return prompt
    size = _utf8_size(prompt)
    if not compact and size <= limit:
        return prompt
    excess = size - limit // 2
    if excess <= 0:
        return prompt
    omitted_size = _utf8_size(json.dumps(OMITTED_TOOL_OUTPUT, ensure_ascii=False))
    kept = list(tool_results)
    for index, result in enumerate(kept):
        if excess <= 0:
            break
        saved = _utf8_size(json.dumps(result.output, ensure_ascii=False)) - omitted_size
        if saved > 0:
            kept[index] = replace(result, output=OMITTED_TOOL_OUTPUT)
            excess -= saved
    return run_input.build_prompt(tuple(kept), last_error)


def _must_force_terminal[T](
    *,
    turn_index: int,
    tool_calls: int,
    run_input: NativeReactRunInput[T],
) -> bool:
    return (
        turn_index == run_input.policy.max_llm_turns - 1
        or tool_calls >= run_input.policy.max_tool_calls
    )


async def run_native_react[T](
    run_input: NativeReactRunInput[T],
) -> NativeReactRunResult[T]:
    registry = ReactToolRegistry(run_input.tool_definitions)
    all_native_tools = build_native_tool_definitions(
        definitions=run_input.tool_definitions,
        terminal_tool=run_input.terminal_tool,
    )
    terminal_tools = (run_input.terminal_tool,)
    steps: list[ReactLoopStep] = []
    tool_results: list[ReactToolResult] = []
    continuation: LlmToolContinuation | None = None
    pending_result: LlmToolResult | None = None
    tool_calls = 0
    consecutive_llm_errors = 0
    last_error: str | None = None

    for turn_index in range(run_input.policy.max_llm_turns):
        force_terminal = _must_force_terminal(
            turn_index=turn_index,
            tool_calls=tool_calls,
            run_input=run_input,
        )
        native_tools = terminal_tools if force_terminal else all_native_tools
        llm_step_number = len(steps) + 1
        call_continuation = continuation
        # A caller on continuation_mode="disabled" gets no continuation back, and
        # the shared contract rejects a tool_result without one.
        call_pending_result = pending_result if continuation is not None else None
        over_input_limit = _exceeds_input_limit(
            run_input.policy, call_continuation, call_pending_result
        )
        if over_input_limit or (
            force_terminal
            and pending_result is not None
            and pending_result.name != run_input.terminal_tool.name
        ):
            # A continuation request must redeclare the prior tool, and one past
            # the input limit would be rejected. A fresh turn keeps the results in
            # the prompt transcript instead.
            call_continuation = None
            call_pending_result = None
        prompt = _build_bounded_prompt(
            run_input, tool_results, last_error, compact=over_input_limit
        )
        if force_terminal and run_input.final_turn_prompt:
            prompt = f"{prompt}\n\n{run_input.final_turn_prompt}"
        try:
            turn = await run_input.call_llm(
                prompt,
                native_tools,
                call_continuation,
                call_pending_result,
            )
        except LlmProxyExecutionError as exc:
            if exc.recovery != "repair_next_turn":
                step = ReactLoopStep(
                    run_id=run_input.run_id,
                    step_number=llm_step_number,
                    step_kind="llm",
                    status="error",
                    prompt_text=prompt,
                    error_message=LLM_PROXY_ERROR_MESSAGE,
                )
                await run_input.record_step(step)
                raise
            consecutive_llm_errors += 1
            last_error = build_tool_call_repair_feedback(exc)
            step = ReactLoopStep(
                run_id=run_input.run_id,
                step_number=llm_step_number,
                step_kind="llm",
                status="error",
                prompt_text=prompt,
                error_message=exc.error_message,
            )
            await run_input.record_step(step)
            steps.append(step)
            if consecutive_llm_errors >= run_input.policy.max_consecutive_llm_errors:
                raise
            continuation = None
            pending_result = None
            continue

        consecutive_llm_errors = 0
        last_error = None
        thinking = (
            run_input.consume_llm_thoughts()
            if run_input.consume_llm_thoughts is not None
            else None
        )
        llm_step = ReactLoopStep(
            run_id=run_input.run_id,
            step_number=llm_step_number,
            step_kind="llm",
            status="success",
            prompt_text=prompt if call_continuation is None else None,
            response_text=_response_text(turn),
            thinking=thinking,
        )
        await run_input.record_step(llm_step)
        steps.append(llm_step)

        # With a planner, the terminal tool ends the run only as the turn's
        # single call; otherwise the planner holds it back.
        single_call = len(turn.calls) == 1 and not turn.dropped_call_names
        if turn.call.name == run_input.terminal_tool.name and (
            run_input.plan_turn is None or single_call
        ):
            completion = run_input.complete(turn.call.arguments, force_terminal)
            if completion.error_message is None:
                return NativeReactRunResult(
                    loop_result=ReactLoopResult(
                        status="success",
                        final_text=completion.final_text,
                        steps=tuple(steps),
                        completion_reason=run_input.terminal_tool.name,
                    ),
                    value=completion.value,
                )
            rejected = await _record_rejected_completion(
                run_input=run_input,
                turn=turn,
                steps=steps,
                tool_results=tool_results,
                tool_calls=tool_calls,
                error_message=completion.error_message,
            )
            if rejected is None:
                return _error_result(
                    steps=steps,
                    last_error="react loop reached max_tool_calls",
                )
            continuation, pending_result, tool_calls, last_error = rejected
            continue

        if force_terminal or tool_calls >= run_input.policy.max_tool_calls:
            return _error_result(
                steps=steps,
                last_error="react loop returned a non-terminal tool after the tool budget",
            )
        if run_input.plan_turn is None:
            tool_calls += 1
            continuation, pending_result = await _execute_tool(
                run_input=run_input,
                registry=registry,
                turn=turn,
                steps=steps,
                tool_results=tool_results,
            )
            continue
        if turn.continuation is not None:
            raise RuntimeError("A planned turn cannot carry a provider continuation")
        plan = run_input.plan_turn(turn, run_input.policy.max_tool_calls - tool_calls)
        tool_calls += len(plan.calls)
        await execute_planned_turn(
            run_input=run_input,
            registry=registry,
            plan=plan,
            steps=steps,
            tool_results=tool_results,
        )

    return _error_result(
        steps=steps,
        last_error=last_error or "react loop reached max_llm_turns",
    )


async def _record_rejected_completion[T](
    *,
    run_input: NativeReactRunInput[T],
    turn: LlmToolCallTurn,
    steps: list[ReactLoopStep],
    tool_results: list[ReactToolResult],
    tool_calls: int,
    error_message: str,
) -> tuple[LlmToolContinuation | None, LlmToolResult, int, str | None] | None:
    if tool_calls >= run_input.policy.max_tool_calls:
        return None
    tool_calls += 1
    call = react_tool_call(turn.call.name, turn.call.arguments)
    tool_step_number = len(steps) + 1
    await run_input.record_step(
        tool_step(
            run_input=run_input,
            call=call,
            step_number=tool_step_number,
            status="processing",
        )
    )
    raw_result = ReactToolResult(
        tool_name=turn.call.name,
        status="error",
        output={
            "status": "error",
            "error_code": "COMPLETION_PRECONDITION_FAILED",
            "message": error_message,
        },
        error_message=error_message,
    )
    final_step = tool_step(
        run_input=run_input,
        call=call,
        step_number=tool_step_number,
        status="error",
        output=raw_result.output,
        error_message=raw_result.error_message,
    )
    await run_input.record_step(final_step)
    steps.append(final_step)
    projected_result = await project_tool_result(
        run_input=run_input,
        result=raw_result,
    )
    tool_results.append(projected_result)
    return (
        turn.continuation,
        LlmToolResult(
            call_id=turn.call.call_id,
            name=turn.call.name,
            output=projected_result.output,
        ),
        tool_calls,
        projected_result.error_message,
    )


async def _execute_tool[T](
    *,
    run_input: NativeReactRunInput[T],
    registry: ReactToolRegistry,
    turn: LlmToolCallTurn,
    steps: list[ReactLoopStep],
    tool_results: list[ReactToolResult],
) -> tuple[LlmToolContinuation | None, LlmToolResult]:
    result = await run_tool(
        run_input=run_input,
        registry=registry,
        call=turn.call,
        steps=steps,
        tool_results=tool_results,
    )
    return (
        turn.continuation,
        LlmToolResult(
            call_id=turn.call.call_id, name=turn.call.name, output=result.output
        ),
    )


def _error_result[T](
    *,
    steps: list[ReactLoopStep],
    last_error: str,
) -> NativeReactRunResult[T]:
    return NativeReactRunResult(
        loop_result=ReactLoopResult(
            status="error",
            final_text="",
            steps=tuple(steps),
            last_error=last_error,
        ),
        value=None,
    )


__all__ = [
    "NativeReactCompletion",
    "NativeReactRunInput",
    "NativeReactRunResult",
    "NativeReactSkippedCall",
    "NativeReactTurnInterrupt",
    "NativeReactTurnPlan",
    "NativeReactTurnPlanner",
    "build_native_tool_definitions",
    "run_native_react",
]
