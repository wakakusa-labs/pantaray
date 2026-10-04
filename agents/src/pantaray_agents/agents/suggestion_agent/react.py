from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Literal, Protocol

from pantaray_agents.agents.artifact_react import (
    NativeReactCompletion,
    NativeReactRunInput,
    ReactLoopPolicy,
    ReactLoopStep,
    ReactToolResult,
    resolve_react_tool_definitions,
    run_native_react,
)
from pantaray_agents.agents.artifact_react.transcript import (
    build_prompt_with_transcript,
)
from pantaray_agents.agents.core.mixins.llm_tool_use_mixin import LlmToolCallTurn
from pantaray_agents.schema.agent.action_message import (
    ACTION_MESSAGE_CONTENT_MAX_CODEPOINTS,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.schema.agent.suggestion import (
    SuggestionExtraction,
    SuggestionStructuredOutput,
)
from pantaray_llm.contracts.tool_use import (
    LlmToolContinuation,
    LlmToolDefinition,
    LlmToolResult,
)

from .research import SuggestionResearchTools

# Deep research is the point of the run: quality comes before cost or duration.
SUGGESTION_MAX_LLM_TURNS = 300
SUGGESTION_MAX_RESEARCH_TOOL_CALLS = 300
# Design limit: replays measured about 5 bytes per input token over a run and 2.4 at
# worst for Japanese tool output, so 400 kB stays under a 200k-token context with the
# system prompt and tools. Raise it when a supported model's measured ratio allows.
SUGGESTION_MAX_INPUT_BYTES = 400_000
SUBMIT_SUGGESTION_TOOL_NAME = "submit_suggestion"
# Offered only while the user lets commands run without asking.
SUGGESTION_COMMAND_TOOL_ID = "bash"
SUGGESTION_TOOL_IDS: tuple[str, ...] = (
    "memory_search",
    "get_memory_reference",
    "memory_sql",
    "read",
    "list",
    "glob",
    "grep",
    "web_search",
    "web_extract",
    "zanei_timeline",
    "zanei_query",
    SUGGESTION_COMMAND_TOOL_ID,
)

# Raw computer activity reaches the model within this run only. Like the short
# Insight, which stores no step at all, the run steps keep what was read (counts,
# range, cursor state) and never the screen text itself.
ACTIVITY_TOOL_IDS = frozenset({"zanei_timeline", "zanei_query"})
_STORED_ACTIVITY_KEYS = (
    "status",
    "error_code",
    "has_more",
    "reached_latest",
    "page_budget_spent",
    "gap",
    "next_start",
    "redacted",
    "source_truncated",
)

type SuggestionStepRecorder = Callable[[ReactLoopStep], Awaitable[None]]
type SuggestionThoughtDiscarder = Callable[[], str | None]


class SuggestionToolCallGenerator(Protocol):
    async def __call__(
        self,
        *,
        prompt: str,
        tools: tuple[LlmToolDefinition, ...],
        continuation_mode: Literal["disabled", "stateless"],
        continuation: LlmToolContinuation | None,
        tool_result: LlmToolResult | None,
        system_instruction: str,
        stage: str,
    ) -> LlmToolCallTurn: ...


class SuggestionOutputParser(Protocol):
    def __call__(
        self,
        *,
        raw_text: str,
        parsed_output: SuggestionStructuredOutput | None,
    ) -> SuggestionExtraction: ...


def _stored_activity_output(output: JSONValue) -> dict[str, JSONValue]:
    if not isinstance(output, dict):
        return {}
    stored = {key: output[key] for key in _STORED_ACTIVITY_KEYS if key in output}
    events = output.get("events")
    if isinstance(events, list):
        stored["event_count"] = len(events)
        # Rows are [event_id, observed_at, context_index].
        if events and isinstance(events[0], list) and isinstance(events[-1], list):
            stored["first_observed_at"] = events[0][1]
            stored["last_observed_at"] = events[-1][1]
    text = output.get("text")
    if isinstance(text, str):
        stored["text_characters"] = len(text)
    return stored


def _transcript_json(output: JSONValue) -> str:
    # The serialization build_prompt_with_transcript embeds each result with.
    return json.dumps(output, ensure_ascii=False)


def _terminal_tool() -> LlmToolDefinition:
    return LlmToolDefinition(
        name=SUBMIT_SUGGESTION_TOOL_NAME,
        description=(
            "Submit the final evidence-grounded intervention decision. This may be "
            "called on the first turn when the initial context is already sufficient."
        ),
        parameters={
            "type": "object",
            "additionalProperties": False,
            "required": [
                "has_suggestion",
                "interaction_contract",
                "key_point",
                "suggestion_summary",
                "target_context",
                "candidates",
            ],
            "properties": {
                "has_suggestion": {"type": "boolean"},
                "interaction_contract": {
                    "type": ["string", "null"],
                    "enum": ["action_offer", "message_only", None],
                },
                "key_point": {
                    "type": "string",
                    "maxLength": ACTION_MESSAGE_CONTENT_MAX_CODEPOINTS,
                    "description": (
                        "The whole suggestion for the writer, in a few short "
                        "sentences: what it is, why it matters now, the deciding "
                        "facts, what is unconfirmed and, for an offer, what "
                        "Pantaray would make or do. Not the finished message."
                    ),
                },
                "suggestion_summary": {"type": ["string", "null"]},
                "target_context": {
                    "oneOf": [
                        {"type": "null"},
                        {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["organization_name", "project_name"],
                            "properties": {
                                "organization_name": {"type": ["string", "null"]},
                                "project_name": {"type": ["string", "null"]},
                            },
                        },
                    ]
                },
                "candidates": {
                    "type": "array",
                    "description": (
                        "Up to eight candidates you considered, including the one "
                        "you suggest and the best one for each endeavor, each with "
                        "why it was suggested or skipped and the shift it came from. "
                        "Diagnostic only; never shown to the user."
                    ),
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["candidate", "decision", "reason"],
                        "properties": {
                            "candidate": {"type": "string"},
                            "decision": {
                                "type": "string",
                                "enum": ["suggested", "skipped"],
                            },
                            "reason": {"type": "string"},
                        },
                    },
                },
            },
        },
    )


async def run_suggestion_react(
    *,
    user_id: str,
    suggestion_id: str,
    initial_prompt: str,
    system_instruction: str,
    research_tools: SuggestionResearchTools,
    generate_tool_call: SuggestionToolCallGenerator,
    parse_output: SuggestionOutputParser,
    record_step: SuggestionStepRecorder,
    discard_llm_thoughts: SuggestionThoughtDiscarder,
) -> SuggestionExtraction:
    definitions = research_tools.build_tool_definitions(
        user_id=user_id,
        run_id=suggestion_id,
    )
    offered = {definition.name for definition in definitions}
    tool_definitions = resolve_react_tool_definitions(
        definitions=definitions,
        tool_ids=tuple(
            tool_id
            for tool_id in SUGGESTION_TOOL_IDS
            if tool_id != SUGGESTION_COMMAND_TOOL_ID or tool_id in offered
        ),
    )

    async def call_llm(
        prompt: str,
        tools: tuple[LlmToolDefinition, ...],
        continuation: LlmToolContinuation | None,
        tool_result: LlmToolResult | None,
    ) -> LlmToolCallTurn:
        return await generate_tool_call(
            prompt=prompt,
            tools=tools,
            continuation_mode="stateless",
            continuation=continuation,
            tool_result=tool_result,
            system_instruction=system_instruction,
            stage="suggestion",
        )

    # (raw, stored) transcript entries, applied to any prompt a step records.
    activity_replacements: list[tuple[str, str]] = []

    async def persist_step(step: ReactLoopStep) -> None:
        if step.tool_name in ACTIVITY_TOOL_IDS and step.tool_output is not None:
            stored = _stored_activity_output(step.tool_output)
            activity_replacements.append(
                (_transcript_json(step.tool_output), _transcript_json(stored))
            )
            step = replace(step, tool_output=stored)
        elif step.prompt_text is not None and activity_replacements:
            prompt = step.prompt_text
            for raw, stored_text in activity_replacements:
                prompt = prompt.replace(raw, stored_text)
            step = replace(step, prompt_text=prompt)
        await record_step(step)

    async def project_tool_result(result: ReactToolResult) -> ReactToolResult:
        return result

    def complete(
        arguments: dict[str, JSONValue],
        _final_turn: bool,
    ) -> NativeReactCompletion[SuggestionExtraction]:
        raw_text = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
        try:
            parsed = SuggestionStructuredOutput.model_validate(arguments)
            extraction = parse_output(raw_text=raw_text, parsed_output=parsed)
        except ValueError as exc:
            return NativeReactCompletion(
                value=None,
                final_text=raw_text,
                error_message=f"submit_suggestion was rejected: {str(exc)[:1200]}",
            )
        extraction["thinking"] = None
        extraction["prompt_text"] = initial_prompt
        extraction["response_text"] = raw_text
        return NativeReactCompletion(value=extraction, final_text=raw_text)

    def discard_thoughts() -> None:
        discard_llm_thoughts()

    result = await run_native_react(
        NativeReactRunInput(
            run_id=suggestion_id,
            tool_definitions=tool_definitions,
            terminal_tool=_terminal_tool(),
            complete=complete,
            build_prompt=lambda tool_results, last_error: build_prompt_with_transcript(
                initial_prompt=initial_prompt,
                tool_results=tool_results,
                last_error=last_error,
            ),
            call_llm=call_llm,
            record_step=persist_step,
            project_tool_result=project_tool_result,
            policy=ReactLoopPolicy(
                max_llm_turns=SUGGESTION_MAX_LLM_TURNS,
                max_tool_calls=SUGGESTION_MAX_RESEARCH_TOOL_CALLS,
                max_input_bytes=SUGGESTION_MAX_INPUT_BYTES,
            ),
            consume_llm_thoughts=discard_thoughts,
            final_turn_prompt=(
                "Research is now closed. Call submit_suggestion with the best "
                "evidence-grounded decision. If evidence is insufficient or the "
                "hard gates are not met, submit has_suggestion=false."
            ),
        )
    )
    if result.loop_result.status != "success" or result.value is None:
        raise RuntimeError(
            result.loop_result.last_error or "Suggestion ReAct loop failed"
        )
    return result.value


__all__ = [
    "ACTIVITY_TOOL_IDS",
    "SUBMIT_SUGGESTION_TOOL_NAME",
    "SUGGESTION_COMMAND_TOOL_ID",
    "SUGGESTION_TOOL_IDS",
    "SUGGESTION_MAX_LLM_TURNS",
    "SUGGESTION_MAX_RESEARCH_TOOL_CALLS",
    "run_suggestion_react",
]
