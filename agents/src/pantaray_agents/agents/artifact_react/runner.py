from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tools.contract import (
    ReactToolCall,
    ReactToolExecutor,
    ReactToolResult,
)
from pantaray_agents.utils.structured_logging import (
    fingerprint_text,
    log_structured_event,
)
from pantaray_agents.utils.trace_context import get_trace_context
from pantaray_llm.errors import LlmProxyExecutionError

from .transcript import build_prompt_with_transcript
from .types import (
    LlmUpstreamError,
    ReactFinish,
    ReactLoopPolicy,
    ReactLoopResult,
    ReactLoopStep,
    ReactParseError,
    stringify_llm_output,
)

type ReactParsedOutput = ReactFinish | ReactToolCall
type ReactLlmOutput = str | object
type ReactLlmCaller = Callable[[str], Awaitable[ReactLlmOutput]]
type ReactOutputParser = Callable[[ReactLlmOutput], ReactParsedOutput]
type ReactStepRecorder = Callable[[ReactLoopStep], Awaitable[None]]
type ReactPromptBuilder = Callable[[tuple[ReactToolResult, ...], str | None], str]
type ReactThoughtConsumer = Callable[[], str | None]

logger = logging.getLogger(__name__)

FATAL_TOOL_ERROR_MESSAGE = "fatal tool error"
LLM_PARSE_ERROR_MESSAGE = "LLM response could not be parsed."
LLM_PARSE_FEEDBACK_MESSAGE = (
    "Previous response did not match the required tool schema. Return exactly one "
    "documented JSON tool call."
)
LLM_PROXY_ERROR_MESSAGE = "LLM provider request failed."
LLM_PROXY_FEEDBACK_MESSAGE = "LLM provider request failed after retries."
LLM_UPSTREAM_ERROR_MESSAGE = "LLM upstream request failed."
LLM_UPSTREAM_FEEDBACK_MESSAGE = "LLM upstream request failed after retries."


async def run_react_loop(
    *,
    run_id: str,
    initial_prompt: str,
    call_llm: ReactLlmCaller,
    parse_output: ReactOutputParser,
    execute_tool: ReactToolExecutor,
    record_step: ReactStepRecorder,
    policy: ReactLoopPolicy | None = None,
    build_prompt: ReactPromptBuilder | None = None,
    consume_llm_thoughts: ReactThoughtConsumer | None = None,
) -> ReactLoopResult:
    resolved_policy = policy or ReactLoopPolicy()
    steps: list[ReactLoopStep] = []
    tool_results: list[ReactToolResult] = []
    last_error: str | None = None
    llm_turns = 0
    tool_calls = 0

    while llm_turns < resolved_policy.max_llm_turns:
        llm_turns += 1
        prompt = (
            build_prompt(tuple(tool_results), _sanitize_error_for_llm(last_error))
            if build_prompt is not None
            else build_prompt_with_transcript(
                initial_prompt=initial_prompt,
                tool_results=tuple(tool_results),
                last_error=_sanitize_error_for_llm(last_error),
            )
        )
        step_number = _next_step_number(steps)
        last_error = None
        thinking: str | None = None
        try:
            raw_response = await call_llm(prompt)
            if consume_llm_thoughts is not None:
                thinking = consume_llm_thoughts()
            response_text = stringify_llm_output(raw_response)
            parsed = parse_output(raw_response)
        except ReactParseError:
            last_error = LLM_PARSE_FEEDBACK_MESSAGE
            await _record_and_append(
                steps,
                record_step,
                ReactLoopStep(
                    run_id=run_id,
                    step_number=step_number,
                    step_kind="llm",
                    status="error",
                    prompt_text=prompt,
                    response_text=locals().get("response_text"),
                    thinking=thinking,
                    error_message=LLM_PARSE_ERROR_MESSAGE,
                ),
            )
            continue
        except LlmProxyExecutionError:
            last_error = LLM_PROXY_FEEDBACK_MESSAGE
            await _record_and_append(
                steps,
                record_step,
                ReactLoopStep(
                    run_id=run_id,
                    step_number=step_number,
                    step_kind="llm",
                    status="error",
                    prompt_text=prompt,
                    error_message=LLM_PROXY_ERROR_MESSAGE,
                ),
            )
            return ReactLoopResult(
                status="error",
                final_text="",
                steps=tuple(steps),
                last_error=last_error,
            )
        except (ConnectionError, TimeoutError, LlmUpstreamError):
            last_error = LLM_UPSTREAM_FEEDBACK_MESSAGE
            await _record_and_append(
                steps,
                record_step,
                ReactLoopStep(
                    run_id=run_id,
                    step_number=step_number,
                    step_kind="llm",
                    status="error",
                    prompt_text=prompt,
                    error_message=LLM_UPSTREAM_ERROR_MESSAGE,
                ),
            )
            return ReactLoopResult(
                status="error",
                final_text="",
                steps=tuple(steps),
                last_error=last_error,
            )

        await _record_and_append(
            steps,
            record_step,
            ReactLoopStep(
                run_id=run_id,
                step_number=step_number,
                step_kind="llm",
                status="success",
                prompt_text=prompt,
                response_text=response_text,
                thinking=thinking,
            ),
        )
        if isinstance(parsed, ReactFinish):
            return ReactLoopResult(
                status="success",
                final_text=parsed.final_text,
                steps=tuple(steps),
                completion_reason=parsed.reason,
            )

        if tool_calls >= resolved_policy.max_tool_calls:
            return ReactLoopResult(
                status="error",
                final_text="",
                steps=tuple(steps),
                last_error="react loop reached max_tool_calls",
            )
        tool_calls += 1
        tool_step_number = _next_step_number(steps)
        processing_tool_step = ReactLoopStep(
            run_id=run_id,
            step_number=tool_step_number,
            step_kind="tool",
            status="processing",
            tool_name=parsed.tool_name,
            tool_args=parsed.tool_args,
            tool_call_envelope=parsed.tool_call_envelope.to_json(),
        )
        await record_step(processing_tool_step)
        try:
            tool_result = await execute_tool(parsed, tool_step_number)
        except Exception as exc:
            await record_fatal_tool_error(
                run_id=run_id,
                tool_step_number=tool_step_number,
                parsed=parsed,
                error=exc,
                record_step=record_step,
            )
            raise
        tool_step = ReactLoopStep(
            run_id=run_id,
            step_number=tool_step_number,
            step_kind="tool",
            status=tool_result.status,
            tool_name=parsed.tool_name,
            tool_args=parsed.tool_args,
            tool_call_envelope=parsed.tool_call_envelope.to_json(),
            tool_output=tool_result.output,
            error_message=tool_result.error_message,
        )
        if tool_result.final_step_recorded:
            steps.append(tool_step)
        else:
            await _record_and_append(steps, record_step, tool_step)
        tool_results.append(tool_result)

    return ReactLoopResult(
        status="error",
        final_text="",
        steps=tuple(steps),
        last_error=last_error or "react loop reached max_llm_turns",
    )


def _next_step_number(steps: list[ReactLoopStep]) -> int:
    return len(steps) + 1


def _sanitize_error_for_llm(raw_error: str | None) -> str | None:
    if raw_error is None:
        return None
    lowered = raw_error.lower()
    if "http://" in lowered or "https://" in lowered or "token=" in lowered:
        return "The previous attempt failed. Retry with the same task instructions."
    return raw_error


async def _record_and_append(
    steps: list[ReactLoopStep],
    record_step: ReactStepRecorder,
    step: ReactLoopStep,
) -> None:
    await record_step(step)
    steps.append(step)


def _fatal_tool_error_output(exc: Exception) -> JSONValue:
    return {"exception_type": type(exc).__name__}


async def record_fatal_tool_error(
    *,
    run_id: str,
    tool_step_number: int,
    parsed: ReactToolCall,
    error: Exception,
    record_step: ReactStepRecorder,
) -> None:
    trace_context = get_trace_context()
    log_structured_event(
        logger,
        level="error",
        evt="REACT_TOOL_FAILED",
        component="artifact_react",
        tool_name=parsed.tool_name,
        run_id_fp=fingerprint_text(run_id),
        job_id_fp=fingerprint_text(
            trace_context.local_job_id if trace_context is not None else None
        ),
        step_number=tool_step_number,
        exception=error,
    )
    try:
        await record_step(
            ReactLoopStep(
                run_id=run_id,
                step_number=tool_step_number,
                step_kind="tool",
                status="error",
                tool_name=parsed.tool_name,
                tool_args=parsed.tool_args,
                tool_call_envelope=parsed.tool_call_envelope.to_json(),
                tool_output=_fatal_tool_error_output(error),
                error_message=FATAL_TOOL_ERROR_MESSAGE,
            )
        )
    except Exception as record_error:
        error.add_note(
            f"failed to record fatal tool error step: {type(record_error).__name__}"
        )
