from __future__ import annotations

from collections.abc import Sequence

import pytest

from pantaray_agents.agents.artifact_react import (
    ReactLoopStep,
    ReactToolCall,
    ReactToolDefinition,
    ReactToolResult,
    react_tool_response_schema,
)
from pantaray_agents.agents.core.mixins.llm_tool_use_mixin import LlmToolCallTurn
from pantaray_agents.agents.suggestion_agent import SuggestionAgent
from pantaray_agents.agents.suggestion_agent.output import parse_suggestion_output
from pantaray_agents.agents.suggestion_agent.react import (
    SUBMIT_SUGGESTION_TOOL_NAME,
    SUGGESTION_COMMAND_TOOL_ID,
    SUGGESTION_MAX_LLM_TURNS,
    SUGGESTION_TOOL_IDS,
    _terminal_tool,
    run_suggestion_react,
)
from pantaray_agents.agents.suggestion_agent.research import (
    FixedSuggestionResearchTools,
)
from pantaray_agents.schema.agent.action_message import (
    ACTION_MESSAGE_CONTENT_MAX_CODEPOINTS,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_llm.contracts.tool_use import (
    LlmToolCall,
    LlmToolDefinition,
    OpenAiToolContinuation,
)


def _turn(
    name: str, arguments: dict[str, JSONValue], *, call_id: str
) -> LlmToolCallTurn:
    return LlmToolCallTurn(
        calls=(LlmToolCall(call_id=call_id, name=name, arguments=arguments),),
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


def _terminal_payload() -> dict[str, JSONValue]:
    return {
        "has_suggestion": True,
        "interaction_contract": "action_offer",
        "key_point": "共通契約に反例があり、呼び出し側の対処では直らない。",
        "deliverable": "共通契約の修正と検証",
        "agent_session": False,
        "suggestion_summary": "### Target\nPantaray\n\n### Work Surface\nUnknown\n\n### Why This Suggestion\nEvidence\n\n### Expected Action\nFix and verify\n\n### Source Context\nObserved\n\n### Ambiguity\nNone",
        "target_context": {
            "organization_name": "Wakakusa",
            "project_name": "Pantaray",
        },
    }


async def _execute_probe(
    call: ReactToolCall,
    _step_number: int,
) -> ReactToolResult:
    return ReactToolResult(
        tool_name=call.tool_name,
        status="success",
        output={"status": "success", "evidence": "counterexample checked"},
    )


def _probe_tool(name: str) -> ReactToolDefinition:
    return ReactToolDefinition(
        name=name,
        description="Retrieve one bounded piece of evidence.",
        request_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["query"],
            "properties": {
                "query": {"type": "string", "minLength": 1},
            },
        },
        response_schema=react_tool_response_schema(
            success_schema={
                "type": "object",
                "required": ["status", "evidence"],
                "properties": {
                    "status": {"const": "success"},
                    "evidence": {"type": "string"},
                },
            }
        ),
        execute=_execute_probe,
    )


def _fixed_research_tools() -> FixedSuggestionResearchTools:
    return FixedSuggestionResearchTools(
        tuple(_probe_tool(tool_id) for tool_id in SUGGESTION_TOOL_IDS)
    )


def test_terminal_tool_declares_action_message_content_limit() -> None:
    properties = _terminal_tool().parameters["properties"]

    assert "answer" not in properties
    for field in ("key_point", "deliverable"):
        assert properties[field]["maxLength"] == ACTION_MESSAGE_CONTENT_MAX_CODEPOINTS


@pytest.mark.asyncio
async def test_suggestion_react_can_disconfirm_then_submit(
    suggestion_agent: SuggestionAgent,
) -> None:
    calls: list[tuple[tuple[str, ...], object, object]] = []
    steps: list[ReactLoopStep] = []

    async def generate_tool_call(**kwargs) -> LlmToolCallTurn:  # noqa: ANN003
        tools: Sequence[LlmToolDefinition] = kwargs["tools"]
        calls.append(
            (
                tuple(tool.name for tool in tools),
                kwargs["continuation"],
                kwargs["tool_result"],
            )
        )
        if len(calls) == 1:
            return _turn(
                "memory_search",
                {
                    "query": "independent meanings",
                },
                call_id="memory-search-1",
            )
        return _turn(
            SUBMIT_SUGGESTION_TOOL_NAME,
            _terminal_payload(),
            call_id="submit-1",
        )

    async def record_step(step: ReactLoopStep) -> None:
        steps.append(step)

    result = await run_suggestion_react(
        user_id="user-1",
        suggestion_id="suggestion-1",
        initial_prompt="initial context",
        system_instruction="system",
        research_tools=_fixed_research_tools(),
        generate_tool_call=generate_tool_call,
        parse_output=parse_suggestion_output,
        record_step=record_step,
        discard_llm_thoughts=lambda: "private thought",
    )

    assert result["has_suggestion"] is True
    assert result["thinking"] is None
    assert calls[0][0] == (SUBMIT_SUGGESTION_TOOL_NAME, *SUGGESTION_TOOL_IDS)
    assert calls[1][1] is not None
    assert calls[1][2] is not None
    assert [step.step_kind for step in steps] == ["llm", "tool", "tool", "llm"]
    assert all(step.thinking is None for step in steps)
    assert steps[1].tool_call_envelope == {
        "tool_id": "memory_search",
        "reason": None,
        "args": {
            "query": "independent meanings",
        },
    }


@pytest.mark.asyncio
async def test_suggestion_react_final_turn_exposes_only_submit(
    suggestion_agent: SuggestionAgent,
) -> None:
    tool_sets: list[tuple[str, ...]] = []
    probe_calls = 0

    async def generate_tool_call(**kwargs) -> LlmToolCallTurn:  # noqa: ANN003
        nonlocal probe_calls
        tools: Sequence[LlmToolDefinition] = kwargs["tools"]
        tool_sets.append(tuple(tool.name for tool in tools))
        if "memory_search" in tool_sets[-1]:
            probe_calls += 1
            return _turn(
                "memory_search",
                {
                    "query": f"query {probe_calls}",
                },
                call_id=f"memory-search-{probe_calls}",
            )
        return _turn(
            SUBMIT_SUGGESTION_TOOL_NAME,
            _terminal_payload(),
            call_id="submit-final",
        )

    result = await run_suggestion_react(
        user_id="user-1",
        suggestion_id="suggestion-final",
        initial_prompt="initial context",
        system_instruction="system",
        research_tools=_fixed_research_tools(),
        generate_tool_call=generate_tool_call,
        parse_output=parse_suggestion_output,
        record_step=lambda _step: _completed(),
        discard_llm_thoughts=lambda: None,
    )

    assert result["has_suggestion"] is True
    assert len(tool_sets) == SUGGESTION_MAX_LLM_TURNS
    assert tool_sets[-1] == (SUBMIT_SUGGESTION_TOOL_NAME,)


@pytest.mark.asyncio
async def test_suggestion_react_runs_without_the_command_tool(
    suggestion_agent: SuggestionAgent,
) -> None:
    tool_sets: list[tuple[str, ...]] = []

    async def generate_tool_call(**kwargs) -> LlmToolCallTurn:  # noqa: ANN003
        tool_sets.append(tuple(tool.name for tool in kwargs["tools"]))
        return _turn(
            SUBMIT_SUGGESTION_TOOL_NAME, _terminal_payload(), call_id="submit-1"
        )

    await run_suggestion_react(
        user_id="user-1",
        suggestion_id="suggestion-no-commands",
        initial_prompt="initial context",
        system_instruction="system",
        research_tools=FixedSuggestionResearchTools(
            tuple(
                _probe_tool(tool_id)
                for tool_id in SUGGESTION_TOOL_IDS
                if tool_id != SUGGESTION_COMMAND_TOOL_ID
            )
        ),
        generate_tool_call=generate_tool_call,
        parse_output=parse_suggestion_output,
        record_step=lambda _step: _completed(),
        discard_llm_thoughts=lambda: None,
    )

    assert SUGGESTION_COMMAND_TOOL_ID not in tool_sets[0]
    assert "memory_search" in tool_sets[0]


@pytest.mark.asyncio
async def test_suggestion_react_returns_invalid_submission_for_repair(
    suggestion_agent: SuggestionAgent,
) -> None:
    pending_results: list[object] = []

    async def generate_tool_call(**kwargs) -> LlmToolCallTurn:  # noqa: ANN003
        pending_results.append(kwargs["tool_result"])
        if len(pending_results) == 1:
            invalid = _terminal_payload()
            invalid["key_point"] = ""
            return _turn(
                SUBMIT_SUGGESTION_TOOL_NAME,
                invalid,
                call_id="submit-invalid",
            )
        result = kwargs["tool_result"]
        assert result is not None
        assert result.name == SUBMIT_SUGGESTION_TOOL_NAME
        assert result.output["error_code"] == "COMPLETION_PRECONDITION_FAILED"
        return _turn(
            SUBMIT_SUGGESTION_TOOL_NAME,
            _terminal_payload(),
            call_id="submit-repaired",
        )

    result = await run_suggestion_react(
        user_id="user-1",
        suggestion_id="suggestion-repair",
        initial_prompt="initial context",
        system_instruction="system",
        research_tools=_fixed_research_tools(),
        generate_tool_call=generate_tool_call,
        parse_output=parse_suggestion_output,
        record_step=lambda _step: _completed(),
        discard_llm_thoughts=lambda: None,
    )

    assert result["has_suggestion"] is True
    assert len(pending_results) == 2


async def _completed() -> None:
    return None


SCREEN_TEXT = "text shown on the user's screen"


async def _execute_raw_activity(
    call: ReactToolCall,
    _step_number: int,
) -> ReactToolResult:
    return ReactToolResult(
        tool_name=call.tool_name,
        status="success",
        output={
            "contexts": [{"window_title": SCREEN_TEXT}],
            "events": [["event-1", "2026-09-28T02:56:50+09:00", 0]],
            "has_more": False,
        },
    )


@pytest.mark.asyncio
async def test_raw_activity_reaches_the_model_but_no_stored_step(
    suggestion_agent: SuggestionAgent,
) -> None:
    prompts: list[str] = []
    steps: list[ReactLoopStep] = []
    tools = tuple(
        ReactToolDefinition(
            name=tool_id,
            description="Read activity.",
            request_schema={"type": "object"},
            response_schema={"type": "object"},
            execute=_execute_raw_activity,
        )
        if tool_id == "zanei_timeline"
        else _probe_tool(tool_id)
        for tool_id in SUGGESTION_TOOL_IDS
    )

    async def generate_tool_call(**kwargs) -> LlmToolCallTurn:  # noqa: ANN003
        prompts.append(kwargs["prompt"])
        # No continuation, so every turn's prompt carries the transcript and is
        # recorded with its step.
        name, arguments = (
            ("zanei_timeline", {})
            if len(prompts) == 1
            else (SUBMIT_SUGGESTION_TOOL_NAME, _terminal_payload())
        )
        return LlmToolCallTurn(
            calls=(
                LlmToolCall(
                    call_id=f"call-{len(prompts)}", name=name, arguments=arguments
                ),
            ),
            continuation=None,
        )

    async def record_step(step: ReactLoopStep) -> None:
        steps.append(step)

    await run_suggestion_react(
        user_id="user-1",
        suggestion_id="suggestion-activity",
        initial_prompt="initial context",
        system_instruction="system",
        research_tools=FixedSuggestionResearchTools(tools),
        generate_tool_call=generate_tool_call,
        parse_output=parse_suggestion_output,
        record_step=record_step,
        discard_llm_thoughts=lambda: None,
    )

    assert SCREEN_TEXT in prompts[-1]
    stored = repr([(step.prompt_text, step.tool_output) for step in steps])
    assert SCREEN_TEXT not in stored
    (final_tool_step,) = [
        step
        for step in steps
        if step.tool_name == "zanei_timeline" and step.tool_output
    ]
    assert final_tool_step.tool_output == {
        "has_more": False,
        "event_count": 1,
        "first_observed_at": "2026-09-28T02:56:50+09:00",
        "last_observed_at": "2026-09-28T02:56:50+09:00",
    }
    assert steps[-1].prompt_text is not None
    assert '"event_count": 1' in steps[-1].prompt_text
