"""Look for a Suggestion through sampled patterns in parallel runs, then pick one.

Each Suggestion samples three patterns of help ("lenses"): two from the
time-sensitive pool and one from the exploration pool. Every lens gets its own
decision run on the shared prompt plus that lens; a selector call then picks the
single best candidate, or none.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Sequence
from dataclasses import dataclass, replace

from pantaray_agents.agents.artifact_react import ReactLoopStep
from pantaray_agents.schema.agent.suggestion import SuggestionExtraction
from pantaray_agents.utils.prompt_loader import PromptConfig
from pantaray_llm.contracts.tool_use import LlmToolDefinition

from .output import parse_suggestion_output
from .react import (
    SuggestionStepRecorder,
    SuggestionThoughtDiscarder,
    SuggestionToolCallGenerator,
    run_suggestion_react,
)
from .research import SuggestionResearchTools

SUGGESTION_LENS_PROMPT_NAME = "suggestion/suggestion_lenses"
SUGGESTION_SELECTOR_PROMPT_NAME = "suggestion/suggestion_selector"
SELECT_SUGGESTION_TOOL_NAME = "select_suggestion"
URGENT_LENSES = (
    "research_and_tell",
    "external_change",
    "take_over",
    "prepare_next_step",
    "likely_forgotten",
)
URGENT_LENSES_PER_SUGGESTION = 2
EXPLORATION_LENS_WEIGHTS = {
    "easier_way": 1,
    "worth_building": 2,
    "step_toward_aim": 2,
    "seize_opportunity": 1,
    "reuse_asset": 1,
    "connect_people": 1,
    "decision_material": 1,
    "perspective": 1,
    "new_approach": 1,
}
# Lens runs share one Suggestion's step numbers: run i records from i * stride.
# A run takes at most 300 LLM turns and 300 tool calls, one step each.
LENS_STEP_STRIDE = 1000
_KIND_LABELS = {"action_offer": "offer", "message_only": "advice"}


@dataclass(frozen=True, slots=True)
class LensCandidate:
    lens: str
    extraction: SuggestionExtraction


@dataclass(frozen=True, slots=True)
class LensDecision:
    extraction: SuggestionExtraction
    candidates: tuple[LensCandidate, ...]
    selected_lens: str | None


def sample_lenses(rng: random.Random) -> tuple[str, ...]:
    urgent = rng.sample(URGENT_LENSES, URGENT_LENSES_PER_SUGGESTION)
    (exploration,) = rng.choices(
        tuple(EXPLORATION_LENS_WEIGHTS),
        weights=tuple(EXPLORATION_LENS_WEIGHTS.values()),
    )
    return (*urgent, exploration)


def _lens_prompt(initial_prompt: str, lens_config: PromptConfig, lens: str) -> str:
    section = lens_config.prompt.format(lens=lens_config.require_role_rule(lens))
    return f"{initial_prompt.rstrip()}\n\n{section}"


def _selector_tool(count: int) -> LlmToolDefinition:
    return LlmToolDefinition(
        name=SELECT_SUGGESTION_TOOL_NAME,
        description="Choose the candidate to show the user now, or none.",
        parameters={
            "type": "object",
            "additionalProperties": False,
            "required": ["choice", "reason"],
            "properties": {
                "choice": {"type": ["integer", "null"], "minimum": 1, "maximum": count},
                "reason": {"type": "string"},
            },
        },
    )


def _format_candidates(candidates: Sequence[LensCandidate]) -> str:
    blocks = []
    for number, candidate in enumerate(candidates, 1):
        decided = candidate.extraction["decided"]
        if decided is None:
            raise ValueError("A selector candidate needs decided content")
        blocks.append(
            f"### Candidate {number}\n"
            f"Kind: {_KIND_LABELS[decided['interaction_contract']]}\n"
            f"Point: {decided['key_point']}\n"
            f"Deliverable: {decided['deliverable'] or '(none)'}\n"
            f"Context:\n{candidate.extraction['suggestion_summary']}"
        )
    return "\n\n".join(blocks)


async def decide_with_lenses(
    *,
    user_id: str,
    suggestion_id: str,
    initial_prompt: str,
    system_instruction: str,
    current_time: str,
    recent_suggestions: str,
    lenses: Sequence[str],
    lens_config: PromptConfig,
    selector_config: PromptConfig,
    research_tools: SuggestionResearchTools,
    generate_tool_call: SuggestionToolCallGenerator,
    record_step: SuggestionStepRecorder,
    discard_llm_thoughts: SuggestionThoughtDiscarder,
) -> LensDecision:
    """Run one decision run per lens in parallel and select among their candidates.

    Any run's failure fails the Suggestion, as a single run's failure does, and
    stops the other runs.
    """

    async def run_lens(index: int, lens: str) -> SuggestionExtraction:
        offset = index * LENS_STEP_STRIDE

        async def record(step: ReactLoopStep) -> None:
            await record_step(replace(step, step_number=step.step_number + offset))

        return await run_suggestion_react(
            user_id=user_id,
            suggestion_id=suggestion_id,
            initial_prompt=_lens_prompt(initial_prompt, lens_config, lens),
            system_instruction=system_instruction,
            research_tools=research_tools,
            generate_tool_call=generate_tool_call,
            parse_output=parse_suggestion_output,
            record_step=record,
            discard_llm_thoughts=discard_llm_thoughts,
        )

    tasks = [
        asyncio.create_task(run_lens(index, lens)) for index, lens in enumerate(lenses)
    ]
    try:
        results = await asyncio.gather(*tasks)
    except BaseException:
        # No lens run may keep calling the model or recording steps once the
        # Suggestion has failed or been cancelled.
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    candidates = tuple(
        LensCandidate(lens=lens, extraction=result)
        for lens, result in zip(lenses, results, strict=True)
        if result["decided"] is not None
    )
    if not candidates:
        return LensDecision(results[0], (), None)

    if selector_config.system_instruction is None:
        raise ValueError("The Suggestion selector prompt has no system_instruction")
    prompt = selector_config.prompt.format(
        current_time=current_time,
        recent_suggestions=recent_suggestions,
        candidates=_format_candidates(candidates),
    )
    step_number = len(lenses) * LENS_STEP_STRIDE + 1
    await record_step(
        ReactLoopStep(
            run_id=suggestion_id,
            step_number=step_number,
            step_kind="llm",
            status="processing",
            prompt_text=prompt,
        )
    )
    turn = await generate_tool_call(
        prompt=prompt,
        tools=(_selector_tool(len(candidates)),),
        continuation_mode="disabled",
        continuation=None,
        tool_result=None,
        system_instruction=selector_config.system_instruction,
        stage="suggestion_selector",
    )
    arguments = turn.call.arguments
    choice = arguments.get("choice")
    reason = str(arguments.get("reason") or "")
    if choice is not None and (
        not isinstance(choice, int) or not 1 <= choice <= len(candidates)
    ):
        raise ValueError(
            f"The Suggestion selector chose an unknown candidate: {choice}"
        )
    await record_step(
        ReactLoopStep(
            run_id=suggestion_id,
            step_number=step_number,
            step_kind="llm",
            status="success",
            prompt_text=prompt,
            response_text=f"choice={choice}; {reason}",
        )
    )
    if choice is None:
        declined = _declined(candidates[0].extraction)
        return LensDecision(declined, candidates, None)
    chosen = candidates[choice - 1]
    return LensDecision(chosen.extraction, candidates, chosen.lens)


def _declined(extraction: SuggestionExtraction) -> SuggestionExtraction:
    return {
        **extraction,
        "answer": "",
        "decided": None,
        "suggestion_summary": None,
        "target_context": None,
        "has_suggestion": False,
        "interaction_contract": None,
    }


__all__ = [
    "EXPLORATION_LENS_WEIGHTS",
    "LensCandidate",
    "LensDecision",
    "SUGGESTION_LENS_PROMPT_NAME",
    "SUGGESTION_SELECTOR_PROMPT_NAME",
    "URGENT_LENSES",
    "decide_with_lenses",
    "sample_lenses",
]
