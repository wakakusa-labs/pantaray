"""Lens runs look for one pattern each; the selector picks one candidate or none."""

import asyncio
import random

import pytest
from tests.unit.agents.suggestion_agent.prompt_support import NO_SUGGESTION

from pantaray_agents.agents.core.mixins.llm_tool_use_mixin import (
    ActionTurnReply,
    LlmToolCallTurn,
)
from pantaray_agents.agents.suggestion_agent.lenses import (
    LENS_STEP_STRIDE,
    LENS_WEIGHTS,
    LENSES_PER_SUGGESTION,
    SUGGESTION_LENS_PROMPT_NAME,
    SUGGESTION_SELECTOR_PROMPT_NAME,
    decide_with_lenses,
    sample_lenses,
)
from pantaray_agents.agents.suggestion_agent.react import SUGGESTION_MAX_LLM_TURNS
from pantaray_agents.config_tunables import load_local_runtime_tunables
from pantaray_agents.conversation.loop import REPAIR_MAX_ATTEMPTS, ConversationRequest
from pantaray_agents.mock.suggestion_research import (
    build_mock_suggestion_research_tools,
)
from pantaray_agents.utils.prompt_loader import PromptLoader
from pantaray_llm.contracts.action_turn import LlmActionTurnResponse
from pantaray_llm.contracts.conversation import LlmTurnUserItem
from pantaray_llm.contracts.input_block import LlmInputTextBlock
from pantaray_llm.contracts.tool_use import LlmToolCall

LENSES = ("take_over", "likely_forgotten", "perspective")


def _suggestion(point: str) -> dict[str, object]:
    return {
        "has_suggestion": True,
        "interaction_contract": "message_only",
        "key_point": point,
        "suggestion_summary": f"### Target\n{point}",
        "target_context": {"organization_name": None, "project_name": None},
        "candidates": [],
    }


def _lens_of(request: ConversationRequest) -> str:
    # Each run's lens leads its history.
    lens_text = request.conversation[0]
    assert isinstance(lens_text, LlmTurnUserItem)
    block = lens_text.content[0]
    assert isinstance(block, LlmInputTextBlock)
    return block.text


def _model(choice: int | None, submissions: dict[str, dict[str, object]]):
    selector_prompts: list[str] = []

    async def send(request: ConversationRequest, _sink: object) -> ActionTurnReply:
        lens = next(
            lens for lens in LENSES if f"### {lens.title()}" in _lens_of(request)
        )
        call = LlmToolCall(
            call_id="c",
            name="submit_suggestion",
            arguments=submissions.get(lens, NO_SUGGESTION),
        )
        return ActionTurnReply(
            response=LlmActionTurnResponse(
                mode="action_turn", messages=[], calls=[call]
            ),
            provider_turn=None,
        )

    async def generate(*, prompt, tools, **_kwargs):
        assert tools[0].name == "select_suggestion"
        selector_prompts.append(prompt)
        call = LlmToolCall(
            call_id="c",
            name="select_suggestion",
            arguments={"choice": choice, "reason": "why"},
        )
        return LlmToolCallTurn(calls=(call,))

    return (send, generate), selector_prompts


async def _decide(model, steps: list[int]):
    send, generate = model
    loader = PromptLoader()
    lens_config = loader.load_config(SUGGESTION_LENS_PROMPT_NAME)
    # Title each lens by its key so the fake model can tell the runs apart.
    lens_config.role_rules = {lens: f"### {lens.title()}" for lens in LENSES}

    async def record(step) -> None:
        steps.append(step.step_number)

    return await decide_with_lenses(
        user_id="user",
        suggestion_id="sug",
        initial_prompt="shared context",
        system_instruction="rules",
        current_time="now",
        recent_suggestions="earlier ideas",
        lenses=LENSES,
        lens_config=lens_config,
        selector_config=loader.load_config(SUGGESTION_SELECTOR_PROMPT_NAME),
        research_tools=build_mock_suggestion_research_tools(),
        send_turn=send,
        generate_tool_call=generate,
        record_step=record,
    )


def test_each_suggestion_samples_three_distinct_lenses_from_the_whole_pool() -> None:
    rng = random.Random(1)
    seen: set[str] = set()
    for _ in range(200):
        lenses = sample_lenses(rng)
        assert len(set(lenses)) == LENSES_PER_SUGGESTION
        seen.update(lenses)
    assert seen == set(LENS_WEIGHTS)


def test_every_sampled_lens_has_a_text() -> None:
    config = PromptLoader().load_config(SUGGESTION_LENS_PROMPT_NAME)
    assert set(config.role_rules) == set(LENS_WEIGHTS)


@pytest.mark.asyncio
async def test_the_selector_choice_maps_to_that_candidate() -> None:
    generate, selector_prompts = _model(
        2,
        {
            "take_over": _suggestion("first idea"),
            "perspective": _suggestion("third idea"),
        },
    )
    steps: list[int] = []

    decision = await _decide(generate, steps)

    assert decision.selected_lens == "perspective"
    assert decision.extraction["decided"]["key_point"] == "third idea"
    assert [c.lens for c in decision.candidates] == ["take_over", "perspective"]
    # The selector compares candidates, not patterns.
    assert "first idea" in selector_prompts[0]
    # It sees what the user was already shown.
    assert "earlier ideas" in selector_prompts[0]
    assert "take_over" not in selector_prompts[0].lower().replace(" ", "_")
    # Each run records in its own step range; the selector after all of them.
    assert {1, 10_001, 20_001, 30_001} <= set(steps)


def test_a_lens_run_cannot_record_into_the_next_runs_step_range() -> None:
    parallel = load_local_runtime_tunables().action_agent.max_parallel_tool_calls
    per_turn = 1 + parallel + 2 * REPAIR_MAX_ATTEMPTS

    assert SUGGESTION_MAX_LLM_TURNS * per_turn < LENS_STEP_STRIDE


@pytest.mark.asyncio
async def test_the_selector_can_decline_every_candidate() -> None:
    generate, _ = _model(None, {"likely_forgotten": _suggestion("only idea")})

    decision = await _decide(generate, [])

    assert decision.selected_lens is None
    assert decision.extraction["has_suggestion"] is False
    assert decision.extraction["decided"] is None


@pytest.mark.asyncio
async def test_no_candidate_skips_the_selector() -> None:
    generate, selector_prompts = _model(1, {})

    decision = await _decide(generate, [])

    assert selector_prompts == []
    assert decision.extraction["has_suggestion"] is False


@pytest.mark.asyncio
async def test_a_failed_lens_run_stops_the_other_runs() -> None:
    stopped: list[str] = []

    async def send(request: ConversationRequest, _sink: object) -> ActionTurnReply:
        lens = _lens_of(request)
        if "### Take_Over" in lens:
            raise RuntimeError("model unavailable")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            stopped.append(lens)
            raise
        raise AssertionError("unreachable")

    async def generate(**_kwargs):
        raise AssertionError("no selector after a failed run")

    with pytest.raises(RuntimeError, match="model unavailable"):
        await _decide((send, generate), [])

    assert len(stopped) == 2
