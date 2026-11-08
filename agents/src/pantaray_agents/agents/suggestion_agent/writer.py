"""Write the user-facing Suggestion text and its TODO from what the decision run decided.

The writer is one separate LLM call in a clean context. It sees only the decided
content and the answer language, never the run's research, memory or
suggestion_summary, so the text is written from the point alone.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from pantaray_agents import todo_ux_feature
from pantaray_agents.agents.artifact_react import ReactLoopStep
from pantaray_agents.schema.agent.action_message import (
    ACTION_MESSAGE_CONTENT_MAX_CODEPOINTS,
)
from pantaray_agents.schema.agent.suggestion import (
    SuggestionDecidedContent,
    SuggestionInteractionContract,
    SuggestionTodo,
    SuggestionTodoOutcome,
)
from pantaray_agents.utils.prompt_loader import PromptConfig
from pantaray_llm.contracts.tool_use import LlmToolDefinition

from .react import SuggestionStepRecorder, SuggestionToolCallGenerator

SUGGESTION_WRITER_PROMPT_NAME = "suggestion/suggestion_writer"
# Runs on the Suggestion inference profile: Cloud serves only the purposes of its
# pinned pantaray-llm, so a writer-specific purpose would need a Cloud release.
SUGGESTION_WRITER_STAGE = "suggestion_writer"
WRITE_SUGGESTION_TOOL_NAME = "write_suggestion"
_KIND_LABELS = {"action_offer": "offer", "message_only": "advice"}


@dataclass(frozen=True, slots=True)
class WrittenSuggestion:
    answer: str
    todo: SuggestionTodo | None  # None while the TODO list is off


@dataclass(frozen=True, slots=True)
class WriterRequest:
    system: str
    prompt: str
    tool: LlmToolDefinition
    # Whether the writer is asked for the TODO; only while the TODO list is on.
    with_todo: bool


class _WrittenOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str
    todo: str | None = None
    # Only an offer's tool asks for these.
    todo_state: SuggestionTodoOutcome | None = None
    todo_note: str | None = None


def build_writer_request(
    config: PromptConfig,
    decided: SuggestionDecidedContent,
    *,
    answer_language: str,
) -> WriterRequest:
    """The writer's messages and tool, built from the decided content only.

    While the TODO list is off, neither asks for a TODO, so the writer is
    asked for the message alone, as before the TODO list.
    """

    if not config.system_instruction:
        raise ValueError("The Suggestion writer prompt has no system_instruction")
    with_todo = todo_ux_feature.TODO_UX_ENABLED
    prompt = config.prompt.format(
        answer_language=answer_language,
        kind=_KIND_LABELS[decided["interaction_contract"]],
        key_point=decided["key_point"],
    )
    system = config.system_instruction
    placeholder = "{write_rule}\n"
    if placeholder in system:
        rule = config.require_role_rule(
            "write_with_todo" if with_todo else "write_message"
        )
        system = system.replace(placeholder, rule)
    system = system.replace("{answer_language}", answer_language)
    return WriterRequest(
        system=system,
        prompt=prompt,
        tool=writer_tool(decided["interaction_contract"], with_todo=with_todo),
        with_todo=with_todo,
    )


def writer_tool(
    contract: SuggestionInteractionContract, *, with_todo: bool
) -> LlmToolDefinition:
    """The writer's one tool; only an offer says where its work stands."""

    offer = contract == "action_offer"
    properties: dict[str, object] = {
        "message": {"type": "string", "description": "The message to the user."},
    }
    if not with_todo:
        return _tool(properties, "Submit the message.")
    properties |= {
        "todo": {
            "type": "string",
            "description": (
                "The work the message is about, as one line for the user's TODO list."
                if offer
                else "What the message did for the user, as one line for their "
                "TODO list, where it shows as done."
            ),
        },
    }
    if offer:
        properties["todo_state"] = {
            "type": "string",
            "enum": ["done", "partial", "continue"],
            "description": (
                "Where that work stands once the user has the message: done when "
                "nothing is left to do, partial when Pantaray made part of it and "
                "the rest is the user's, continue when Pantaray would carry on "
                "with the rest."
            ),
        }
        properties["todo_note"] = {
            "type": ["string", "null"],
            "description": (
                "For partial or continue, one short line on where the work "
                "stands; null when done."
            ),
        }
    return _tool(properties, "Submit the message and its TODO.")


def _tool(properties: dict[str, object], description: str) -> LlmToolDefinition:
    return LlmToolDefinition(
        name=WRITE_SUGGESTION_TOOL_NAME,
        description=description,
        parameters={
            "type": "object",
            "additionalProperties": False,
            "required": list(properties),
            "properties": properties,
        },
    )


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


def parse_written_suggestion(
    arguments: Mapping[str, object],
    contract: SuggestionInteractionContract,
    *,
    with_todo: bool,
) -> WrittenSuggestion:
    """The answer and the TODO; the TODO list's own rules check its line on save."""

    output = _WrittenOutput.model_validate(arguments)
    answer = check_written_answer(output.message)
    if not with_todo:
        return WrittenSuggestion(answer, None)
    if output.todo is None:
        raise ValueError("todo is required")
    # Delivering a notice is its whole work, so its TODO opens done; the user
    # unchecks it to keep it open.
    state: SuggestionTodoOutcome | None = (
        "done" if contract == "message_only" else output.todo_state
    )
    if state is None:
        raise ValueError("todo_state is required for an offer")
    if state == "done":
        # A finished TODO shows no note, so one the model added is not kept.
        return WrittenSuggestion(
            answer, SuggestionTodo(text=output.todo, outcome="done", note=None)
        )
    note = (output.todo_note or "").strip()
    if not note:
        raise ValueError(f"todo_note is required when todo_state is {state}")
    return WrittenSuggestion(
        answer, SuggestionTodo(text=output.todo, outcome=state, note=note)
    )


async def write_suggestion_answer(
    *,
    run_id: str,
    step_number: int,
    decided: SuggestionDecidedContent,
    answer_language: str,
    config: PromptConfig,
    generate_tool_call: SuggestionToolCallGenerator,
    record_step: SuggestionStepRecorder,
) -> WrittenSuggestion:
    """Write the answer and its TODO, and record the call as the run's next step.

    A failure is recorded on that step and raised, so the run ends through the
    model-error path and nothing is published without text.
    """

    request = build_writer_request(config, decided, answer_language=answer_language)
    prompt = request.prompt
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
        turn = await generate_tool_call(
            prompt=prompt,
            tools=(request.tool,),
            system_instruction=request.system,
            stage=SUGGESTION_WRITER_STAGE,
        )
        arguments = turn.call.arguments
        written = parse_written_suggestion(
            arguments, decided["interaction_contract"], with_todo=request.with_todo
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
            response_text=json.dumps(arguments, ensure_ascii=False),
        )
    )
    return written


__all__ = [
    "SUGGESTION_WRITER_PROMPT_NAME",
    "SUGGESTION_WRITER_STAGE",
    "WRITE_SUGGESTION_TOOL_NAME",
    "WriterRequest",
    "WrittenSuggestion",
    "build_writer_request",
    "check_written_answer",
    "parse_written_suggestion",
    "write_suggestion_answer",
    "writer_tool",
]
