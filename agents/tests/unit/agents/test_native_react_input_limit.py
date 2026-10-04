from __future__ import annotations

import json

from pantaray_agents.agents.artifact_react import (
    ReactLoopPolicy,
    ReactLoopStep,
    ReactToolCall,
    ReactToolResult,
)
from pantaray_agents.agents.artifact_react.native_runner import (
    OMITTED_TOOL_OUTPUT,
    NativeReactCompletion,
    NativeReactRunInput,
    run_native_react,
)
from pantaray_agents.agents.artifact_react.tooling import (
    ReactToolDefinition,
    react_tool_response_schema,
)
from pantaray_agents.agents.core.mixins.llm_tool_use_mixin import LlmToolCallTurn
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_llm.contracts.tool_use import (
    LlmToolCall,
    LlmToolContinuation,
    LlmToolDefinition,
    LlmToolResult,
    OpenAiToolContinuation,
)

INPUT_LIMIT_BYTES = 60_000
RESEARCH_CALLS = 200
OUTPUT_BYTES = 3_000
REASONING_BYTES = 1_000


def _search_tool() -> ReactToolDefinition:
    calls = 0

    async def execute(_call: ReactToolCall, _step: int) -> ReactToolResult:
        nonlocal calls
        calls += 1
        return ReactToolResult(
            tool_name="search",
            status="success",
            output={
                "status": "success",
                "data": f"result-{calls}:" + "x" * OUTPUT_BYTES,
            },
        )

    return ReactToolDefinition(
        name="search",
        description="search",
        request_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {},
        },
        response_schema=react_tool_response_schema(
            success_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["status", "data"],
                "properties": {
                    "status": {"const": "success"},
                    "data": {"type": "string"},
                },
            }
        ),
        execute=execute,
    )


async def test_a_long_run_keeps_every_request_within_the_input_limit() -> None:
    """Without a bound the resent history grows past the provider's input limit."""
    request_sizes: list[int] = []
    fresh_prompts: list[str] = []
    turn = 0

    async def call_llm(
        prompt: str,
        _tools: tuple[LlmToolDefinition, ...],
        continuation: LlmToolContinuation | None,
        tool_result: LlmToolResult | None,
    ) -> LlmToolCallTurn:
        # Mirrors the stateless provider: the history resent is the previous
        # history plus the tool result, or the prompt alone on a fresh turn.
        nonlocal turn
        turn += 1
        if continuation is None:
            history: list[dict[str, JSONValue]] = [{"role": "user", "content": prompt}]
            request_sizes.append(len(prompt.encode()))
            fresh_prompts.append(prompt)
        else:
            assert isinstance(continuation, OpenAiToolContinuation)
            assert tool_result is not None
            history = [
                *continuation.history_items,
                {
                    "type": "function_call_output",
                    "output": json.dumps(tool_result.output),
                },
            ]
            request_sizes.append(
                len(continuation.model_dump_json().encode())
                + len(tool_result.model_dump_json().encode())
            )
        name = "search" if turn <= RESEARCH_CALLS else "completed"
        history.append(
            {"type": "reasoning", "encrypted_content": "r" * REASONING_BYTES}
        )
        history.append({"type": "function_call", "call_id": f"call-{turn}"})
        return LlmToolCallTurn(
            calls=(LlmToolCall(call_id=f"call-{turn}", name=name, arguments={}),),
            continuation=OpenAiToolContinuation(
                provider="openai", history_items=history
            ),
        )

    def complete(
        _arguments: dict[str, JSONValue], _final_turn: bool
    ) -> NativeReactCompletion[str]:
        return NativeReactCompletion(value="done", final_text="")

    async def record_step(_step: ReactLoopStep) -> None:
        return None

    async def project_result(result: ReactToolResult) -> ReactToolResult:
        return result

    def build_prompt(results: tuple[ReactToolResult, ...], _error: str | None) -> str:
        lines = [json.dumps(result.output, ensure_ascii=False) for result in results]
        return "\n".join(["Research the moment.", *lines])

    result = await run_native_react(
        NativeReactRunInput(
            run_id="run-1",
            tool_definitions=(_search_tool(),),
            terminal_tool=LlmToolDefinition(
                name="completed",
                description="Finish the run.",
                parameters={"type": "object", "properties": {}},
            ),
            complete=complete,
            build_prompt=build_prompt,
            call_llm=call_llm,
            record_step=record_step,
            project_tool_result=project_result,
            policy=ReactLoopPolicy(
                max_llm_turns=RESEARCH_CALLS + 1,
                max_tool_calls=RESEARCH_CALLS,
                max_input_bytes=INPUT_LIMIT_BYTES,
            ),
        )
    )

    assert result.value == "done"
    assert max(request_sizes) <= INPUT_LIMIT_BYTES
    # The run kept researching across resets instead of resetting on every turn.
    assert 2 < len(fresh_prompts) < RESEARCH_CALLS // 4
    last_reset = fresh_prompts[-1]
    omitted = json.dumps(OMITTED_TOOL_OUTPUT, ensure_ascii=False)
    assert last_reset.splitlines()[1] == omitted
    assert last_reset.splitlines()[-1].startswith(
        '{"status": "success", "data": "result-'
    )
