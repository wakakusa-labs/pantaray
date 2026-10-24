"""Look for a Suggestion through sampled patterns in parallel runs, then pick one.

Each Suggestion samples three distinct patterns of help ("lenses") by weight from
one pool. Every lens gets its own decision run on the shared prompt plus that
lens, shown with a few examples drawn from the lens's pool; a selector call then
picks the single best candidate, or none, and only the chosen run goes on to
make what it suggests.
"""

from __future__ import annotations

import asyncio
import random
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace

from pantaray_agents.agents.artifact_react import ReactLoopStep
from pantaray_agents.schema.agent.suggestion import SuggestionExtraction
from pantaray_agents.utils.prompt_loader import PromptConfig
from pantaray_llm.contracts.tool_use import LlmToolDefinition

from .make import make_suggestion_artifacts
from .nudges import PastSuggestions
from .output import parse_suggestion_output
from .react import (
    LensRunResult,
    SuggestionConversation,
    SuggestionStepRecorder,
    SuggestionToolCallGenerator,
    SuggestionTurnSender,
    run_suggestion_react,
)
from .research import SuggestionResearchTools

SUGGESTION_LENS_PROMPT_NAME = "suggestion/suggestion_lenses"
SUGGESTION_SELECTOR_PROMPT_NAME = "suggestion/suggestion_selector"
SELECT_SUGGESTION_TOOL_NAME = "select_suggestion"
LENS_WEIGHTS = {
    "research_and_tell": 1,
    "external_change": 1,
    "take_over": 1,
    "prepare_next_step": 1,
    "likely_forgotten": 1,
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
LENSES_PER_SUGGESTION = 3
# A lens's text lists its example pool after this line, one "- " item each.
LENS_EXAMPLES_HEADING = "Examples:"
# Two cases from different people show the range of a lens without one anchoring it.
EXAMPLES_PER_LENS_RUN = 2
# Lens runs share one Suggestion's step numbers: run i records from i * stride, and
# a number taken twice overwrites a step. A turn records itself, each call that comes
# back (max_parallel_tool_calls at most) and up to two failed sends per repair.
LENS_STEP_STRIDE = 10_000
_KIND_LABELS = {"action_offer": "offer", "message_only": "advice"}


@dataclass(frozen=True, slots=True)
class SampledLens:
    name: str
    # Draws this run's examples from the lens's pool.
    example_seed: int


@dataclass(frozen=True, slots=True)
class LensCandidate:
    lens: str
    extraction: SuggestionExtraction
    conversation: SuggestionConversation


@dataclass(frozen=True, slots=True)
class LensDecision:
    extraction: SuggestionExtraction
    candidates: tuple[LensCandidate, ...]
    selected_lens: str | None


def sample_lenses(rng: random.Random) -> tuple[SampledLens, ...]:
    remaining = dict(LENS_WEIGHTS)
    names = []
    for _ in range(LENSES_PER_SUGGESTION):
        (name,) = rng.choices(tuple(remaining), weights=tuple(remaining.values()))
        del remaining[name]
        names.append(name)
    return tuple(SampledLens(name, rng.getrandbits(32)) for name in names)


def lens_section(lens_config: PromptConfig, lens: SampledLens) -> str:
    """The lens's definition with a seeded draw of its examples."""
    definition, _, pool_text = lens_config.require_role_rule(lens.name).partition(
        f"\n{LENS_EXAMPLES_HEADING}\n"
    )
    # Unwrap each "- " item; its continuation lines are indented.
    items = re.split(r"^- ", pool_text, flags=re.MULTILINE)[1:]
    pool = [" ".join(item.split()) for item in items]
    chosen = random.Random(lens.example_seed).sample(
        pool, min(EXAMPLES_PER_LENS_RUN, len(pool))
    )
    examples = "".join(f"\n- {example}" for example in chosen)
    text = f"{definition}\n{LENS_EXAMPLES_HEADING}{examples}" if chosen else definition
    return lens_config.prompt.format(lens=text)


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
    past: PastSuggestions,
    lenses: Sequence[SampledLens],
    lens_config: PromptConfig,
    selector_config: PromptConfig,
    nudge_config: PromptConfig,
    make_config: PromptConfig,
    research_tools: SuggestionResearchTools,
    send_turn: SuggestionTurnSender,
    generate_tool_call: SuggestionToolCallGenerator,
    record_step: SuggestionStepRecorder,
) -> LensDecision:
    """Run one decision run per lens in parallel and select among their candidates.

    Any run's failure fails the Suggestion, as a single run's failure does, and
    stops the other runs.
    """

    async def run_lens(index: int, lens: SampledLens) -> LensRunResult:
        offset = index * LENS_STEP_STRIDE

        async def record(step: ReactLoopStep) -> None:
            await record_step(replace(step, step_number=step.step_number + offset))

        return await run_suggestion_react(
            user_id=user_id,
            suggestion_id=suggestion_id,
            context=initial_prompt,
            lens=lens_section(lens_config, lens),
            system_instruction=system_instruction,
            research_tools=research_tools,
            nudge_config=nudge_config,
            send_turn=send_turn,
            parse_output=parse_suggestion_output,
            record_step=record,
            past=past,
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
        LensCandidate(lens.name, result.extraction, result.conversation)
        for lens, result in zip(lenses, results, strict=True)
        if result.extraction["decided"] is not None
    )
    if not candidates:
        return LensDecision(results[0].extraction, (), None)

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
    chosen = None if choice is None else candidates[choice - 1]
    selected_lens = None if chosen is None else chosen.lens
    await record_step(
        ReactLoopStep(
            run_id=suggestion_id,
            step_number=step_number,
            step_kind="llm",
            status="success",
            prompt_text=prompt,
            response_text=f"choice={choice}; {reason}",
            # The recent-Suggestion history reads the shown Suggestion's lens here.
            tool_output={"selected_lens": selected_lens},
        )
    )
    if chosen is None:
        declined = _declined(candidates[0].extraction)
        return LensDecision(declined, candidates, None)

    async def record_make(step: ReactLoopStep) -> None:
        # Its llm steps carry no tool_output, so the selector's record above
        # stays the one the recent-Suggestion history reads the lens from.
        await record_step(replace(step, step_number=step.step_number + step_number))

    made = await make_suggestion_artifacts(
        user_id=user_id,
        suggestion_id=suggestion_id,
        extraction=chosen.extraction,
        conversation=chosen.conversation,
        research_tools=research_tools,
        make_config=make_config,
        record_step=record_make,
    )
    if made is None:
        return LensDecision(_declined(chosen.extraction), candidates, None)
    return LensDecision(made, candidates, selected_lens)


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
    "LENS_WEIGHTS",
    "LensCandidate",
    "LensDecision",
    "SUGGESTION_LENS_PROMPT_NAME",
    "SUGGESTION_SELECTOR_PROMPT_NAME",
    "SampledLens",
    "decide_with_lenses",
    "lens_section",
    "sample_lenses",
]
