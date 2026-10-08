"""One Suggestion research run on the shared conversation loop."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass

import pytest

from pantaray_agents.agents.artifact_react import ReactLoopStep
from pantaray_agents.agents.core import CountingSink
from pantaray_agents.agents.suggestion_agent import react
from pantaray_agents.agents.suggestion_agent.output import parse_suggestion_output
from pantaray_agents.agents.suggestion_agent.react import (
    SUBMIT_SUGGESTION_TOOL_NAME,
    SUGGESTION_COMMAND_TOOL_ID,
    SUGGESTION_TOOL_IDS,
    _terminal_tool,
    run_suggestion_react,
)
from pantaray_agents.agents.suggestion_agent.research import (
    FixedSuggestionResearchTools,
)
from pantaray_agents.config_tunables import load_local_runtime_tunables
from pantaray_agents.conversation.loop import ConversationRequest
from pantaray_agents.schema.agent.action_message import (
    ACTION_MESSAGE_CONTENT_MAX_CODEPOINTS,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.schema.agent.suggestion import SuggestionExtraction
from pantaray_agents.tools.contract import (
    ReactToolCall,
    ReactToolDefinition,
    ReactToolExecutor,
    ReactToolResult,
    ToolConcurrency,
)
from pantaray_llm.contracts.action_turn import LlmActionTurnResponse, LlmCommentary
from pantaray_llm.contracts.conversation import (
    LlmProviderTurn,
    LlmTurnItem,
    LlmTurnToolResultItem,
    LlmTurnUserItem,
)
from pantaray_llm.contracts.input_block import LlmInputTextBlock
from pantaray_llm.contracts.tool_use import LlmToolCall


@dataclass(frozen=True, slots=True)
class _Reply:
    response: LlmActionTurnResponse
    provider_turn: LlmProviderTurn | None = None


def _reply(*calls: tuple[str, str, dict[str, JSONValue]], text: str = "") -> _Reply:
    return _Reply(
        LlmActionTurnResponse(
            mode="action_turn",
            messages=[
                LlmCommentary(phase="commentary", source_message_id="m", text=text)
            ]
            if text
            else [],
            calls=[
                LlmToolCall(call_id=call_id, name=name, arguments=arguments)
                for call_id, name, arguments in calls
            ],
        )
    )


def _submit(call_id: str, payload: dict[str, JSONValue] | None = None) -> _Reply:
    return _reply(
        (call_id, SUBMIT_SUGGESTION_TOOL_NAME, payload or _terminal_payload())
    )


def _terminal_payload() -> dict[str, JSONValue]:
    return {
        "has_suggestion": True,
        "interaction_contract": "action_offer",
        "key_point": "共通契約に反例があり、呼び出し側の対処では直らない。",
        "suggestion_summary": "### Target\nPantaray\n\n### Work Surface\nUnknown\n\n### Why This Suggestion\nEvidence\n\n### Expected Action\nFix and verify\n\n### Source Context\nObserved\n\n### Ambiguity\nNone",
        "target_context": {
            "organization_name": "Wakakusa",
            "project_name": "Pantaray",
        },
    }


async def _evidence(call: ReactToolCall, _step_number: int) -> ReactToolResult:
    return ReactToolResult(
        tool_name=call.tool_name,
        status="success",
        output={"status": "success", "evidence": f"checked {call.tool_args}"},
    )


def _tool(
    name: str,
    execute: ReactToolExecutor = _evidence,
    concurrency: ToolConcurrency = ToolConcurrency("sequential"),
) -> ReactToolDefinition:
    return ReactToolDefinition(
        name=name,
        description="Retrieve one bounded piece of evidence.",
        request_schema={"type": "object"},
        response_schema={"type": "object"},
        execute=execute,
        concurrency=concurrency,
    )


def _tools(**overrides: ReactToolDefinition) -> FixedSuggestionResearchTools:
    return FixedSuggestionResearchTools(
        tuple(overrides.get(tool_id, _tool(tool_id)) for tool_id in SUGGESTION_TOOL_IDS)
    )


class _Model:
    """Answers each research turn with the next reply ``reply`` gives."""

    def __init__(self, reply: Callable[[ConversationRequest], _Reply]) -> None:
        self.reply = reply
        self.requests: list[ConversationRequest] = []

    async def __call__(
        self, request: ConversationRequest, sink: CountingSink
    ) -> _Reply:
        del sink
        self.requests.append(request)
        return self.reply(request)


def _scripted(*replies: _Reply) -> _Model:
    queue = list(replies)
    return _Model(lambda _request: queue.pop(0))


async def _run(
    model: _Model,
    *,
    tools: FixedSuggestionResearchTools | None = None,
    steps: list[ReactLoopStep] | None = None,
) -> SuggestionExtraction:
    async def record_step(step: ReactLoopStep) -> None:
        if steps is not None:
            steps.append(step)

    return await run_suggestion_react(
        user_id="user-1",
        suggestion_id="suggestion-1",
        context="initial context",
        lens="## The kind of suggestion for this run",
        system_instruction="system",
        research_tools=tools or _tools(),
        send_turn=model,
        parse_output=parse_suggestion_output,
        record_step=record_step,
    )


def _results(request: ConversationRequest) -> dict[str, JSONValue]:
    return {
        item.call_id: item.output
        for item in request.conversation
        if isinstance(item, LlmTurnToolResultItem)
    }


def _text(item: LlmTurnItem) -> str:
    assert isinstance(item, LlmTurnUserItem)
    block = item.content[0]
    assert isinstance(block, LlmInputTextBlock)
    return block.text


def test_terminal_tool_declares_action_message_content_limit() -> None:
    properties = _terminal_tool().parameters["properties"]

    assert "answer" not in properties
    assert properties["key_point"]["maxLength"] == ACTION_MESSAGE_CONTENT_MAX_CODEPOINTS


@pytest.mark.asyncio
async def test_a_run_researches_and_ends_through_submit_suggestion() -> None:
    steps: list[ReactLoopStep] = []
    model = _scripted(
        _reply(("search-1", "memory_search", {"query": "independent meanings"})),
        _submit("submit-1"),
    )

    result = await _run(model, steps=steps)

    assert result["has_suggestion"] is True
    assert result["thinking"] is None
    prompt = "initial context\n\n## The kind of suggestion for this run"
    assert result["prompt_text"] == prompt
    # The shared context is the head of every request; the lens leads the history.
    assert [request.prompt for request in model.requests] == ["initial context"] * 2
    assert _text(model.requests[0].conversation[0]).startswith("## The kind")
    assert "evidence" in str(_results(model.requests[1])["search-1"])
    assert [step.step_kind for step in steps] == ["llm", "tool", "llm"]
    assert [step.step_number for step in steps] == [1, 2, 3]
    assert steps[0].prompt_text == prompt
    assert steps[2].prompt_text is None
    assert steps[1].tool_call_envelope == {
        "tool_id": "memory_search",
        "reason": None,
        "args": {"query": "independent meanings"},
    }


@pytest.mark.asyncio
async def test_parallel_research_calls_run_together_in_one_turn() -> None:
    both_running = asyncio.Barrier(2)

    async def meet(call: ReactToolCall, step_number: int) -> ReactToolResult:
        # Run one after the other, the first call would wait here alone.
        await asyncio.wait_for(both_running.wait(), timeout=2)
        return await _evidence(call, step_number)

    parallel = ToolConcurrency("parallel")
    model = _scripted(
        _reply(
            ("read-1", "read", {"path": "/a"}),
            ("search-1", "web_search", {"query": "b"}),
        ),
        _submit("submit-1"),
    )

    await _run(
        model,
        tools=_tools(
            read=_tool("read", meet, parallel),
            web_search=_tool("web_search", meet, parallel),
        ),
    )

    assert model.requests[0].max_parallel_tool_calls == (
        load_local_runtime_tunables().action_agent.max_parallel_tool_calls
    )
    assert set(_results(model.requests[1])) == {"read-1", "search-1"}


@pytest.mark.asyncio
async def test_the_last_turn_keeps_every_tool_and_ends_through_submit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(react, "SUGGESTION_MAX_LLM_TURNS", 3)

    def keep_searching(request: ConversationRequest) -> _Reply:
        last = request.conversation[-1]
        if isinstance(last, LlmTurnUserItem) and "last turn" in _text(last):
            return _submit("submit-last")
        number = len(model.requests)
        return _reply((f"search-{number}", "memory_search", {"query": str(number)}))

    model = _Model(keep_searching)

    result = await _run(model)

    assert result["has_suggestion"] is True
    assert len(model.requests) == 3
    first, last = model.requests[0], model.requests[-1]
    assert last.tools == first.tools
    assert [tool.name for tool in first.tools] == [
        SUBMIT_SUGGESTION_TOOL_NAME,
        *SUGGESTION_TOOL_IDS,
    ]
    assert last.max_parallel_tool_calls == 1


@pytest.mark.asyncio
async def test_a_turn_that_does_not_submit_is_answered_and_the_run_goes_on() -> None:
    steps: list[ReactLoopStep] = []
    model = _scripted(
        _reply(text="I think the evidence is enough."),
        _submit("submit-invalid", {**_terminal_payload(), "key_point": ""}),
        _submit("submit-repaired"),
    )

    result = await _run(model, steps=steps)

    assert result["has_suggestion"] is True
    asked = _text(model.requests[1].conversation[-1])
    assert "call `submit_suggestion` alone" in asked
    rejected = _results(model.requests[2])["submit-invalid"]
    assert isinstance(rejected, dict)
    assert rejected["error_code"] == "COMPLETION_PRECONDITION_FAILED"
    assert "submit_suggestion was rejected" in str(rejected["message"])
    assert [(step.step_kind, step.status) for step in steps] == [
        ("llm", "success"),
        ("llm", "success"),
        ("tool", "error"),
        ("llm", "success"),
    ]


@pytest.mark.asyncio
async def test_a_run_without_the_command_tool_offers_the_rest() -> None:
    model = _scripted(_submit("submit-1"))

    await _run(
        model,
        tools=FixedSuggestionResearchTools(
            tuple(
                _tool(tool_id)
                for tool_id in SUGGESTION_TOOL_IDS
                if tool_id != SUGGESTION_COMMAND_TOOL_ID
            )
        ),
    )

    names = [tool.name for tool in model.requests[0].tools]
    assert SUGGESTION_COMMAND_TOOL_ID not in names
    assert "memory_search" in names


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
async def test_raw_activity_reaches_the_model_but_no_stored_step() -> None:
    steps: list[ReactLoopStep] = []
    model = _scripted(
        _reply(("activity-1", "zanei_timeline", {})),
        _reply(text=f"The window said {SCREEN_TEXT}."),
        _submit("submit-1"),
    )

    await _run(
        model,
        tools=_tools(zanei_timeline=_tool("zanei_timeline", _execute_raw_activity)),
        steps=steps,
    )

    assert SCREEN_TEXT in str(_results(model.requests[1])["activity-1"])
    stored = [
        (step.prompt_text, step.response_text, json.dumps(step.tool_output))
        for step in steps
    ]
    assert SCREEN_TEXT not in json.dumps(stored, ensure_ascii=False)
    (activity_step,) = [step for step in steps if step.tool_name == "zanei_timeline"]
    assert activity_step.tool_output == {
        "has_more": False,
        "event_count": 1,
        "first_observed_at": "2026-09-28T02:56:50+09:00",
        "last_observed_at": "2026-09-28T02:56:50+09:00",
    }
