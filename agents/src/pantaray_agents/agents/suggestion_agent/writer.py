"""Write the user-facing Suggestion text from what the decision run decided.

The writer is one separate LLM call in a clean context. It sees only the decided
content and the answer language, never the run's research, memory or
suggestion_summary, so the text is written from the point alone.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from pantaray_agents.agents.artifact_react import ReactLoopStep
from pantaray_agents.schema.agent.action_message import (
    ACTION_MESSAGE_CONTENT_MAX_CODEPOINTS,
)
from pantaray_agents.schema.agent.suggestion import SuggestionDecidedContent
from pantaray_agents.utils.prompt_loader import PromptConfig

SUGGESTION_WRITER_PROMPT_NAME = "suggestion/suggestion_writer"
# Runs on the Suggestion inference profile: Cloud serves only the purposes of its
# pinned pantaray-llm, so a writer-specific purpose would need a Cloud release.
SUGGESTION_WRITER_STAGE = "suggestion_writer"
_KIND_LABELS = {"action_offer": "offer", "message_only": "advice"}

type SuggestionStepRecorder = Callable[[ReactLoopStep], Awaitable[None]]


class SuggestionTextGenerator(Protocol):
    async def __call__(
        self, *, prompt: str, system_instruction: str, stage: str
    ) -> str: ...


def build_writer_messages(
    config: PromptConfig,
    decided: SuggestionDecidedContent,
    *,
    answer_language: str,
) -> tuple[str, str]:
    """Return (system instruction, prompt) built from the decided content only."""

    if not config.system_instruction:
        raise ValueError("The Suggestion writer prompt has no system_instruction")
    prompt = config.prompt.format(
        answer_language=answer_language,
        kind=_KIND_LABELS[decided["interaction_contract"]],
        key_point=decided["key_point"],
    )
    system = config.system_instruction.replace("{answer_language}", answer_language)
    return system, prompt


def check_written_answer(text: str) -> str:
    """Return the trimmed text, or reject text the user could not be shown."""

    answer = text.strip()
    if not answer:
        raise ValueError("The Suggestion writer returned no text")
    if len(answer) > ACTION_MESSAGE_CONTENT_MAX_CODEPOINTS:
        raise ValueError(
            "The Suggestion writer returned more than "
            f"{ACTION_MESSAGE_CONTENT_MAX_CODEPOINTS} characters"
        )
    return answer


async def write_suggestion_answer(
    *,
    run_id: str,
    step_number: int,
    decided: SuggestionDecidedContent,
    answer_language: str,
    config: PromptConfig,
    generate_text: SuggestionTextGenerator,
    record_step: SuggestionStepRecorder,
) -> str:
    """Write the answer and record the call as the run's next step.

    A failure is recorded on that step and raised, so the run ends through the
    model-error path and nothing is published without text.
    """

    system, prompt = build_writer_messages(
        config,
        decided,
        answer_language=answer_language,
    )
    await record_step(
        ReactLoopStep(
            run_id=run_id,
            step_number=step_number,
            step_kind="llm",
            status="processing",
            prompt_text=prompt,
        )
    )
    try:
        answer = check_written_answer(
            await generate_text(
                prompt=prompt,
                system_instruction=system,
                stage=SUGGESTION_WRITER_STAGE,
            )
        )
    except Exception as exc:
        # Recorded for the trace, then raised unchanged to the caller's error path.
        await record_step(
            ReactLoopStep(
                run_id=run_id,
                step_number=step_number,
                step_kind="llm",
                status="error",
                prompt_text=prompt,
                error_message=str(exc)[:1200],
            )
        )
        raise
    await record_step(
        ReactLoopStep(
            run_id=run_id,
            step_number=step_number,
            step_kind="llm",
            status="success",
            prompt_text=prompt,
            response_text=answer,
        )
    )
    return answer


__all__ = [
    "SUGGESTION_WRITER_PROMPT_NAME",
    "SUGGESTION_WRITER_STAGE",
    "SuggestionTextGenerator",
    "build_writer_messages",
    "check_written_answer",
    "write_suggestion_answer",
]
