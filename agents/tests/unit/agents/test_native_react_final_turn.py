from __future__ import annotations

from pantaray_agents.agents.artifact_react import ReactLoopPolicy, ReactLoopStep
from pantaray_agents.agents.artifact_react.native_runner import (
    NativeReactCompletion,
    NativeReactRunInput,
    run_native_react,
)
from pantaray_agents.agents.core.mixins.llm_tool_use_mixin import LlmToolCallTurn
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tools.contract import ReactToolResult
from pantaray_llm.contracts.tool_use import (
    LlmToolCall,
    LlmToolContinuation,
    LlmToolDefinition,
    LlmToolResult,
    OpenAiToolContinuation,
)


def _terminal_tool() -> LlmToolDefinition:
    return LlmToolDefinition(
        name="completed",
        description="Finish the run.",
        parameters={"type": "object", "properties": {}},
    )


async def test_the_completion_handler_learns_when_the_terminal_tool_is_forced() -> None:
    """A handler that gates completion must not reject a turn it cannot recover from."""
    final_turns: list[bool] = []

    async def call_llm(
        _prompt: str,
        tools: tuple[LlmToolDefinition, ...],
        _continuation: LlmToolContinuation | None,
        _tool_result: LlmToolResult | None,
    ) -> LlmToolCallTurn:
        assert [tool.name for tool in tools] == ["completed"]
        return LlmToolCallTurn(
            calls=(LlmToolCall(call_id="call-1", name="completed", arguments={}),),
            continuation=OpenAiToolContinuation(
                provider="openai",
                history_items=[
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": "prompt"}],
                    }
                ],
            ),
        )

    def complete(
        _arguments: dict[str, JSONValue], final_turn: bool
    ) -> NativeReactCompletion[str]:
        final_turns.append(final_turn)
        if final_turn:
            return NativeReactCompletion(value="finished", final_text="")
        return NativeReactCompletion(
            value=None, final_text="", error_message="keep reading"
        )

    async def record_step(_step: ReactLoopStep) -> None:
        return None

    async def project_result(result: ReactToolResult) -> ReactToolResult:
        return result

    result = await run_native_react(
        NativeReactRunInput(
            run_id="run-1",
            tool_definitions=(),
            terminal_tool=_terminal_tool(),
            complete=complete,
            build_prompt=lambda _results, _error: "prompt",
            call_llm=call_llm,
            record_step=record_step,
            project_tool_result=project_result,
            policy=ReactLoopPolicy(max_llm_turns=5, max_tool_calls=1),
        )
    )

    assert final_turns == [False, True]
    assert result.value == "finished"


async def test_the_forced_turn_after_a_rejected_completion_is_sent_fresh() -> None:
    """A replayed continuation would drop the final-turn prompt and, on Anthropic,
    fail with 400 because its thinking was produced under the full tool set."""
    sent: list[tuple[str, LlmToolContinuation | None, LlmToolResult | None]] = []
    continuation = OpenAiToolContinuation(
        provider="openai",
        history_items=[
            {"role": "user", "content": [{"type": "input_text", "text": "prompt"}]}
        ],
    )

    async def call_llm(
        prompt: str,
        _tools: tuple[LlmToolDefinition, ...],
        call_continuation: LlmToolContinuation | None,
        tool_result: LlmToolResult | None,
    ) -> LlmToolCallTurn:
        sent.append((prompt, call_continuation, tool_result))
        return LlmToolCallTurn(
            calls=(LlmToolCall(call_id="call-1", name="completed", arguments={}),),
            continuation=continuation,
        )

    def complete(
        _arguments: dict[str, JSONValue], final_turn: bool
    ) -> NativeReactCompletion[str]:
        if final_turn:
            return NativeReactCompletion(value="finished", final_text="")
        return NativeReactCompletion(
            value=None, final_text="", error_message="keep reading"
        )

    async def record_step(_step: ReactLoopStep) -> None:
        return None

    async def project_result(result: ReactToolResult) -> ReactToolResult:
        return result

    await run_native_react(
        NativeReactRunInput(
            run_id="run-1",
            tool_definitions=(),
            terminal_tool=_terminal_tool(),
            complete=complete,
            build_prompt=lambda results, error: (
                f"prompt|{[result.error_message for result in results]}|{error}"
            ),
            call_llm=call_llm,
            record_step=record_step,
            project_tool_result=project_result,
            policy=ReactLoopPolicy(max_llm_turns=5, max_tool_calls=1),
            final_turn_prompt="Finish now.",
        )
    )

    assert sent[-1] == (
        "prompt|['keep reading']|keep reading\n\nFinish now.",
        None,
        None,
    )
