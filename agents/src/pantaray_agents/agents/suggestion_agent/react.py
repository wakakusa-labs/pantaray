"""One Suggestion research run: a model conversation on the shared loop.

The shared Suggestion prompt is the head every request is sent behind, so the
lens runs of one Suggestion share its cache prefix; the run's lens is its first
message. The run researches with its tools and ends with a single
``submit_suggestion`` call, which ``decide`` accepts or answers with why it was
rejected. A Suggestion is never resumed -- a retried job starts over -- so the
history stays in memory. Each turn and each answered call is a run step.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from pantaray_agents.agents.artifact_react import ReactLoopStep
from pantaray_agents.agents.core import CountingSink
from pantaray_agents.agents.core.mixins.llm_tool_use_mixin import LlmToolCallTurn
from pantaray_agents.config_tunables import load_local_runtime_tunables
from pantaray_agents.conversation.budget import ContextBudget
from pantaray_agents.conversation.loop import (
    Continue,
    ConversationRequest,
    ConversationRun,
    Finish,
    IdleTurn,
    RecordedTurn,
    TurnReply,
    run_conversation,
)
from pantaray_agents.conversation.provider_turns import ProviderTurnStore
from pantaray_agents.conversation.window import ConversationEntry, WindowState
from pantaray_agents.schema.agent.action_message import (
    ACTION_MESSAGE_CONTENT_MAX_CODEPOINTS,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.schema.agent.suggestion import (
    SuggestionExtraction,
    SuggestionStructuredOutput,
)
from pantaray_agents.tools.contract import (
    ReactToolResult,
    ToolCallEnvelope,
    resolve_react_tool_definitions,
)
from pantaray_llm.contracts.conversation import (
    LlmTurnItem,
    LlmTurnToolResultItem,
    LlmTurnUserItem,
)
from pantaray_llm.contracts.input_block import LlmInputTextBlock
from pantaray_llm.contracts.tool_use import (
    LlmToolCall,
    LlmToolContinuation,
    LlmToolDefinition,
    LlmToolResult,
)
from pantaray_llm.errors import LlmProxyExecutionError
from pantaray_llm.profiles import SUGGESTION_PROFILE_ID

from .research import SuggestionResearchTools

# Deep research is the point of the run: quality comes before cost or duration.
SUGGESTION_MAX_LLM_TURNS = 300
SUGGESTION_MAX_RESEARCH_TOOL_CALLS = 300
SUBMIT_SUGGESTION_TOOL_NAME = "submit_suggestion"
_SUBMIT_REQUIRED = (
    "Respond with tool calls. When your research is done, call "
    f"`{SUBMIT_SUGGESTION_TOOL_NAME}` alone with your decision."
)
# What a failed send's step keeps when the failure is not the model's output.
_SEND_FAILED_MESSAGE = "LLM provider request failed."
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
# Sends one research turn, counting its usage on the run's own sink: the lens runs
# of one Suggestion run at once, and each run's budget reads only its own sends.
type SuggestionTurnSender = Callable[
    [ConversationRequest, CountingSink], Awaitable[TurnReply]
]


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
                        "The suggestion in a few concise sentences for the user, "
                        "who has not seen your research: what you suggest, why "
                        "it matters now, the deciding facts, what is unconfirmed "
                        "and, for an offer, what Pantaray would do."
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
    context: str,
    lens: str,
    system_instruction: str,
    research_tools: SuggestionResearchTools,
    send_turn: SuggestionTurnSender,
    parse_output: SuggestionOutputParser,
    record_step: SuggestionStepRecorder,
) -> SuggestionExtraction:
    """Research behind ``context`` for ``lens`` until a submission is accepted.

    Raises what the loop raises: a send, tool or step failure, a cancel, or no
    accepted submission by the last turn.
    """

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
    # What the run is asked, as one text: its first step and its extraction keep it.
    prompt_text = f"{context.rstrip()}\n\n{lens}"
    sink = CountingSink()
    steps = _RunSteps(run_id=suggestion_id, record=record_step, prompt=prompt_text)

    async def send(request: ConversationRequest) -> TurnReply:
        try:
            return await send_turn(request, sink)
        except LlmProxyExecutionError as exc:
            await steps.failed_send(exc)
            raise

    def decide(turn: IdleTurn) -> Finish[SuggestionExtraction] | Continue:
        if turn.ending_call is None:
            return Continue(_SUBMIT_REQUIRED)
        arguments = turn.ending_call.arguments
        raw_text = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
        try:
            parsed = SuggestionStructuredOutput.model_validate(arguments)
            extraction = parse_output(raw_text=raw_text, parsed_output=parsed)
        except ValueError as exc:
            return Continue(
                f"{SUBMIT_SUGGESTION_TOOL_NAME} was rejected: {str(exc)[:1200]}"
            )
        extraction["thinking"] = None
        extraction["prompt_text"] = prompt_text
        extraction["response_text"] = raw_text
        return Finish(extraction)

    # Suggestion runs on the models an Action does, under the same input cap.
    tunables = load_local_runtime_tunables().action_agent
    return await run_conversation(
        ConversationRun(
            prompt=context,
            system_instruction=system_instruction,
            tools=tool_definitions,
            ending_tools=(_terminal_tool(),),
            history=(ConversationEntry(_user_text(lens)),),
            provider_turns=ProviderTurnStore(identity=None),
            inference_profile=SUGGESTION_PROFILE_ID,
            max_turns=SUGGESTION_MAX_LLM_TURNS,
            max_tool_calls=SUGGESTION_MAX_RESEARCH_TOOL_CALLS,
            max_parallel_tool_calls=tunables.max_parallel_tool_calls,
            window=WindowState(
                budget=ContextBudget(
                    window_tokens=tunables.context_window_tokens,
                    baseline=None,
                    reset_pending=False,
                ),
                omit_before=0,
            ),
            usage=lambda: sink.delta,
            send=send,
            before_send=_nothing_arrived,
            on_turn=steps.on_turn,
            on_result=steps.on_result,
            on_notice=_keep_out_of_steps,
            decide=decide,
        )
    )


@dataclass(slots=True)
class _RunSteps:
    """Records the run's steps in order: each turn, answered call and failed send."""

    run_id: str
    record: SuggestionStepRecorder
    prompt: str | None  # until the first turn records it
    count: int = 0

    async def on_turn(self, turn: RecordedTurn) -> None:
        # Every later request extends the first, which this names whole.
        prompt, self.prompt = self.prompt, None
        await self.record(
            ReactLoopStep(
                run_id=self.run_id,
                step_number=self._next(),
                step_kind="llm",
                status="success",
                prompt_text=prompt,
                # The calls only: commentary may quote activity text, which
                # stays within the run like the activity output itself.
                response_text=json.dumps(
                    [
                        {"tool_id": call.name, "args": call.arguments}
                        for call in turn.reply.response.calls
                    ],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
        )

    async def on_result(
        self, call: LlmToolCall, result: ReactToolResult
    ) -> LlmTurnToolResultItem:
        await self.record(
            ReactLoopStep(
                run_id=self.run_id,
                step_number=self._next(),
                step_kind="tool",
                status="success" if result.status == "success" else "error",
                tool_name=call.name,
                tool_call_envelope=ToolCallEnvelope(
                    tool_id=call.name, reason=None, args=call.arguments
                ).to_json(),
                tool_output=_stored_activity_output(result.output)
                if call.name in ACTIVITY_TOOL_IDS
                else result.output,
                error_message=result.error_message,
            )
        )
        return LlmTurnToolResultItem(
            type="tool_result",
            call_id=call.call_id,
            name=call.name,
            output=result.output,
        )

    async def failed_send(self, exc: LlmProxyExecutionError) -> None:
        await self.record(
            ReactLoopStep(
                run_id=self.run_id,
                step_number=self._next(),
                step_kind="llm",
                status="error",
                error_message=exc.error_message
                if exc.recovery == "repair_next_turn"
                else _SEND_FAILED_MESSAGE,
            )
        )

    def _next(self) -> int:
        self.count += 1
        return self.count


async def _nothing_arrived() -> Sequence[LlmTurnItem]:
    return ()


async def _keep_out_of_steps(_notice: LlmTurnUserItem) -> None:
    """A notice is the loop's own text, which the next request carries."""


def _user_text(text: str) -> LlmTurnUserItem:
    return LlmTurnUserItem(
        type="user", content=[LlmInputTextBlock(type="input_text", text=text)]
    )


__all__ = [
    "ACTIVITY_TOOL_IDS",
    "SUBMIT_SUGGESTION_TOOL_NAME",
    "SuggestionStepRecorder",
    "SuggestionTurnSender",
    "SUGGESTION_COMMAND_TOOL_ID",
    "SUGGESTION_TOOL_IDS",
    "SUGGESTION_MAX_LLM_TURNS",
    "SUGGESTION_MAX_RESEARCH_TOOL_CALLS",
    "run_suggestion_react",
]
