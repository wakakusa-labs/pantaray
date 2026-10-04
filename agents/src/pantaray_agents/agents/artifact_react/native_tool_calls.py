"""Running the tool calls of one native ReAct turn.

A turn's calls run one at a time in the order the model gave them, or all at
once when the caller's plan says they may. Either way each result is recorded in
that order, and a call the plan left out is recorded as a result saying it did
not run, so the model sees an answer for everything it asked.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pantaray_agents.schema.agent.base import JSONValue
from pantaray_llm.contracts.tool_use import LlmToolCall

from .runner import record_fatal_tool_error
from .tooling import ReactToolRegistry
from .types import (
    ReactLoopStep,
    ReactStepStatus,
    ReactToolCall,
    ReactToolResult,
    ToolCallEnvelope,
)

if TYPE_CHECKING:  # pragma: no cover
    from .native_runner import NativeReactRunInput


@dataclass(frozen=True, slots=True)
class NativeReactSkippedCall:
    """A call the turn requested that the loop did not run, and why."""

    name: str
    arguments: dict[str, JSONValue]
    reason: str


@dataclass(frozen=True, slots=True)
class NativeReactTurnInterrupt:
    """A tool exception that stops the turn to resume the run later.

    The calls after the one that raised it, and the calls the plan left out,
    are recorded as not run before it propagates, so the resumed run shows the
    model an answer for each of them instead of nothing.
    """

    exception: type[BaseException]
    not_run_reason: str


@dataclass(frozen=True, slots=True)
class NativeReactTurnPlan:
    """Which of one turn's calls run now, in the order the model gave them."""

    calls: tuple[LlmToolCall, ...]
    parallel: bool
    skipped: tuple[NativeReactSkippedCall, ...] = ()


async def execute_planned_turn[T](
    *,
    run_input: NativeReactRunInput[T],
    registry: ReactToolRegistry,
    plan: NativeReactTurnPlan,
    steps: list[ReactLoopStep],
    tool_results: list[ReactToolResult],
) -> None:
    """Run the planned calls, then record each skipped one as a result saying so.

    Results land in the order the model gave the calls, also when they ran at
    once, so the record of a turn does not depend on which call finished first.
    """

    if plan.parallel:
        await run_tools_at_once(
            run_input=run_input,
            registry=registry,
            calls=plan.calls,
            steps=steps,
            tool_results=tool_results,
        )
    else:
        for index, call in enumerate(plan.calls):
            try:
                await run_tool(
                    run_input=run_input,
                    registry=registry,
                    call=call,
                    steps=steps,
                    tool_results=tool_results,
                )
            except BaseException as exc:
                interrupt = run_input.turn_interrupt
                if interrupt is None or not isinstance(exc, interrupt.exception):
                    raise
                not_run = tuple(
                    NativeReactSkippedCall(
                        name=later.name,
                        arguments=later.arguments,
                        reason=interrupt.not_run_reason,
                    )
                    for later in plan.calls[index + 1 :]
                )
                await _record_skipped(
                    run_input=run_input,
                    skipped=(*not_run, *plan.skipped),
                    steps=steps,
                    tool_results=tool_results,
                )
                raise
    await _record_skipped(
        run_input=run_input,
        skipped=plan.skipped,
        steps=steps,
        tool_results=tool_results,
    )


async def _record_skipped[T](
    *,
    run_input: NativeReactRunInput[T],
    skipped: tuple[NativeReactSkippedCall, ...],
    steps: list[ReactLoopStep],
    tool_results: list[ReactToolResult],
) -> None:
    for skipped_call in skipped:
        react_call = react_tool_call(skipped_call.name, skipped_call.arguments)
        output: JSONValue = {
            "status": "error",
            "error_code": "TOOL_CALL_NOT_RUN",
            "message": skipped_call.reason,
        }
        step = tool_step(
            run_input=run_input,
            call=react_call,
            step_number=len(steps) + 1,
            status="error",
            output=output,
            error_message=skipped_call.reason,
        )
        await run_input.record_step(step)
        steps.append(step)
        tool_results.append(
            ReactToolResult(
                tool_name=skipped_call.name,
                status="error",
                output=output,
                error_message=skipped_call.reason,
            )
        )


async def run_tool[T](
    *,
    run_input: NativeReactRunInput[T],
    registry: ReactToolRegistry,
    call: LlmToolCall,
    steps: list[ReactLoopStep],
    tool_results: list[ReactToolResult],
) -> ReactToolResult:
    react_call = react_tool_call(call.name, call.arguments)
    tool_step_number = len(steps) + 1
    await run_input.record_step(
        tool_step(
            run_input=run_input,
            call=react_call,
            step_number=tool_step_number,
            status="processing",
        )
    )
    try:
        raw_result = await registry.execute(react_call, tool_step_number)
    except Exception as exc:
        await record_fatal_tool_error(
            run_id=run_input.run_id,
            tool_step_number=tool_step_number,
            parsed=react_call,
            error=exc,
            record_step=run_input.record_step,
        )
        raise
    return await settle_tool(
        run_input=run_input,
        call=react_call,
        step_number=tool_step_number,
        raw_result=raw_result,
        steps=steps,
        tool_results=tool_results,
    )


async def run_tools_at_once[T](
    *,
    run_input: NativeReactRunInput[T],
    registry: ReactToolRegistry,
    calls: tuple[LlmToolCall, ...],
    steps: list[ReactLoopStep],
    tool_results: list[ReactToolResult],
) -> None:
    numbered = [
        (react_tool_call(call.name, call.arguments), len(steps) + 1 + index)
        for index, call in enumerate(calls)
    ]
    for react_call, step_number in numbered:
        await run_input.record_step(
            tool_step(
                run_input=run_input,
                call=react_call,
                step_number=step_number,
                status="processing",
            )
        )
    outcomes = await asyncio.gather(
        *(registry.execute(call, number) for call, number in numbered),
        return_exceptions=True,
    )
    failure: BaseException | None = None
    for (react_call, step_number), outcome in zip(numbered, outcomes, strict=True):
        if isinstance(outcome, BaseException):
            if isinstance(outcome, Exception):
                await record_fatal_tool_error(
                    run_id=run_input.run_id,
                    tool_step_number=step_number,
                    parsed=react_call,
                    error=outcome,
                    record_step=run_input.record_step,
                )
            failure = failure or outcome
            continue
        await settle_tool(
            run_input=run_input,
            call=react_call,
            step_number=step_number,
            raw_result=outcome,
            steps=steps,
            tool_results=tool_results,
        )
    if failure is not None:
        raise failure


async def settle_tool[T](
    *,
    run_input: NativeReactRunInput[T],
    call: ReactToolCall,
    step_number: int,
    raw_result: ReactToolResult,
    steps: list[ReactLoopStep],
    tool_results: list[ReactToolResult],
) -> ReactToolResult:
    final_step = tool_step(
        run_input=run_input,
        call=call,
        step_number=step_number,
        status=raw_result.status,
        output=raw_result.output,
        error_message=raw_result.error_message,
    )
    if not raw_result.final_step_recorded:
        await run_input.record_step(final_step)
    steps.append(final_step)
    projected_result = await project_tool_result(
        run_input=run_input,
        result=raw_result,
    )
    tool_results.append(projected_result)
    return projected_result


async def project_tool_result[T](
    *,
    run_input: NativeReactRunInput[T],
    result: ReactToolResult,
) -> ReactToolResult:
    projected = await run_input.project_tool_result(result)
    if projected.tool_name != result.tool_name or projected.status != result.status:
        raise RuntimeError("Tool result projection must preserve tool name and status")
    if projected.final_step_recorded != result.final_step_recorded:
        raise RuntimeError(
            "Tool result projection must preserve final-step recording ownership"
        )
    return projected


def react_tool_call(name: str, arguments: dict[str, JSONValue]) -> ReactToolCall:
    return ReactToolCall(
        tool_name=name,
        tool_args=arguments,
        tool_call_envelope=ToolCallEnvelope(tool_id=name, reason=None, args=arguments),
    )


def tool_step[T](
    *,
    run_input: NativeReactRunInput[T],
    call: ReactToolCall,
    step_number: int,
    status: ReactStepStatus,
    output: JSONValue = None,
    error_message: str | None = None,
) -> ReactLoopStep:
    return ReactLoopStep(
        run_id=run_input.run_id,
        step_number=step_number,
        step_kind="tool",
        status=status,
        tool_name=call.tool_name,
        tool_args=call.tool_args,
        tool_call_envelope=call.tool_call_envelope.to_json(),
        tool_output=output,
        error_message=error_message,
    )


__all__ = [
    "NativeReactSkippedCall",
    "NativeReactTurnInterrupt",
    "NativeReactTurnPlan",
    "execute_planned_turn",
    "project_tool_result",
    "react_tool_call",
    "run_tool",
    "tool_step",
]
