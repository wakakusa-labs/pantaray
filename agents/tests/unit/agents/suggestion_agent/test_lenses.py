"""Lens runs look for one pattern each; the selector picks one candidate or none."""

import asyncio
import random

import pytest
from tests.unit.agents.suggestion_agent.prompt_support import NO_SUGGESTION

from pantaray_agents.agents.core.mixins.llm_tool_use_mixin import LlmToolCallTurn
from pantaray_agents.agents.suggestion_agent.lenses import (
    EXPLORATION_LENS_WEIGHTS,
    SUGGESTION_LENS_PROMPT_NAME,
    SUGGESTION_SELECTOR_PROMPT_NAME,
    URGENT_LENSES,
    decide_with_lenses,
    sample_lenses,
)
from pantaray_agents.mock.suggestion_research import (
    build_mock_suggestion_research_tools,
)
from pantaray_agents.utils.prompt_loader import PromptLoader
from pantaray_llm.contracts.tool_use import LlmToolCall

LENSES = ("take_over", "likely_forgotten", "perspective")


def _suggestion(point: str) -> dict[str, object]:
    return {
        "has_suggestion": True,
        "interaction_contract": "message_only",
        "key_point": point,
        "details": None,
        "suggestion_summary": f"### Target\n{point}",
        "target_context": {"organization_name": None, "project_name": None},
        "candidates": [],
    }


def _model(choice: int | None, submissions: dict[str, dict[str, object]]):
    selector_prompts: list[str] = []

    async def generate(*, prompt, tools, continuation_mode, **_kwargs):
        if tools[0].name == "select_suggestion":
            assert continuation_mode == "disabled"
            selector_prompts.append(prompt)
            arguments: dict[str, object] = {"choice": choice, "reason": "why"}
            name = "select_suggestion"
        else:
            lens = next(lens for lens in LENSES if f"### {lens.title()}" in prompt)
            arguments = submissions.get(lens, NO_SUGGESTION)
            name = "submit_suggestion"
        call = LlmToolCall(call_id="c", name=name, arguments=arguments)
        return LlmToolCallTurn(calls=(call,), continuation=None)

    return generate, selector_prompts


async def _decide(generate, steps: list[int]):
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
        generate_tool_call=generate,
        record_step=record,
        discard_llm_thoughts=lambda: None,
    )


def test_each_suggestion_samples_two_urgent_lenses_and_one_exploration_lens() -> None:
    rng = random.Random(1)
    for _ in range(200):
        first, second, third = sample_lenses(rng)
        assert {first, second} <= set(URGENT_LENSES) and first != second
        assert third in EXPLORATION_LENS_WEIGHTS


def test_every_sampled_lens_has_a_text() -> None:
    config = PromptLoader().load_config(SUGGESTION_LENS_PROMPT_NAME)
    assert set(config.role_rules) == {*URGENT_LENSES, *EXPLORATION_LENS_WEIGHTS}


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
    assert {1, 1001, 2001, 3001} <= set(steps)


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

    async def generate(*, prompt, **_kwargs):
        if "### Take_Over" in prompt:
            raise RuntimeError("model unavailable")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            stopped.append(prompt)
            raise
        raise AssertionError("unreachable")

    with pytest.raises(RuntimeError, match="model unavailable"):
        await _decide(generate, [])

    assert len(stopped) == 2
