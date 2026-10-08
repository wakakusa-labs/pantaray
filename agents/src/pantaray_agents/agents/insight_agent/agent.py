"""Read one Zanei delta and produce activity and short Insight together."""

import logging
from dataclasses import replace
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from pantaray_agents.agents.core.mixins.llm_tool_use_mixin import (
    ActionTurnReply,
    LlmToolUseMixin,
)
from pantaray_agents.agents.core.mixins.llm_usage import CountingSink
from pantaray_agents.agents.core.tool_llm_runner import ToolLlmRunner
from pantaray_agents.config_tunables import (
    ActionAgentTunables,
    load_local_runtime_tunables,
)
from pantaray_agents.conversation import provider_turns
from pantaray_agents.conversation.budget import ContextBudget
from pantaray_agents.conversation.loop import (
    Continue,
    ConversationEntry,
    ConversationRequest,
    ConversationRun,
    Finish,
    IdleTurn,
    RecordedTurn,
    run_conversation,
)
from pantaray_agents.conversation.window import WindowState
from pantaray_agents.tools.contract import ReactToolResult
from pantaray_agents.tools.memory.retrieval import (
    MEMORY_EDITOR_RETRIEVAL_POLICY,
    MemoryContextSession,
    MemoryRetrievalSession,
)
from pantaray_agents.tools.zanei import ZaneiTools
from pantaray_agents.utils.prompt_loader import prompt_loader
from pantaray_llm.contracts.conversation import (
    LlmTurnItem,
    LlmTurnToolResultItem,
    LlmTurnUserItem,
)
from pantaray_llm.contracts.input_block import LlmInputTextBlock
from pantaray_llm.contracts.tool_use import LlmToolCall, LlmToolDefinition
from pantaray_llm.profiles import INSIGHT_PROFILE_ID

from .record_verification import SourceRecordClaim

# The short Insight only needs memory previews for continuity; the memory file
# editor's untruncated policy would let 6 searches x 8 documents grow without
# bound. These caps put the memory share of a run at 6 x 5,343 = ~32k tokens,
# one of the fixed terms the Zanei drain budgets are derived against (see
# MAX_TIMELINE_CHARS_PER_RUN).
SHORT_INSIGHT_RETRIEVAL_POLICY = replace(
    MEMORY_EDITOR_RETRIEVAL_POLICY,
    max_results=4,
    default_limit=4,
    search_content_max_chars=1200,
    reference_content_max_chars=4000,
)

logger = logging.getLogger(__name__)

# The bounds the run had on the generic ReAct loop.
_MAX_TURNS = 40
_MAX_TOOL_CALLS = 36
_COMPLETED_TOOL_NAME = "completed"
_COMPLETE_REQUIRED = (
    "Respond with tool calls. Read the Zanei range with zanei_timeline, then "
    f"save this run's outputs with {_COMPLETED_TOOL_NAME}."
)
_INVALID_COMPLETION = (
    "Provide nonempty activity and insight Markdown and a nullable "
    "reconsideration_reason."
)


class ShortInsightOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    activity: str = Field(
        min_length=1, description="Objective chronological activity, Markdown."
    )
    insight: str = Field(
        min_length=1, description="Current user context and short Insight, Markdown."
    )
    reconsideration_reason: str | None = Field(
        description="Concrete reason to reconsider suggestions, or null."
    )
    # Optional so an omitted list completes the run: the host verifies these
    # against the text the run fetched and keeps what it can prove, and the
    # prose outputs must not depend on getting them.
    records: list[SourceRecordClaim] = Field(
        default_factory=list,
        description=(
            "Verbatim work-relevant text read on screen, including non-conversational "
            "evidence, each tied to its event. Empty when this run read none."
        ),
    )

    @field_validator("records", mode="before")
    @classmethod
    def discard_malformed_records(cls, value: object) -> list[SourceRecordClaim]:
        # This is untrusted completion input. Invalid secondary records must not
        # prevent the activity, Insight and read cursor from being committed.
        records: list[SourceRecordClaim] = []
        invalid = 0
        if isinstance(value, list):
            for item in value:
                try:
                    records.append(SourceRecordClaim.model_validate(item))
                except ValidationError:
                    invalid += 1
        else:
            invalid = 1
        if invalid:
            logger.warning(
                "Malformed source records discarded", extra={"invalid_records": invalid}
            )
        return records


class InsightAgent(LlmToolUseMixin, ToolLlmRunner):
    PROMPT_NAME = "insight"
    PROMPT_VERSION = "2.4"

    def __init__(
        self,
        *,
        client: object,
        llm_config: dict[str, object],
    ) -> None:
        self._prompt_config = prompt_loader.load_config(self.PROMPT_NAME)
        super().__init__(
            client=client,
            llm_config=llm_config,
            default_system_instruction=self._prompt_config.system_instruction or "",
            error_code_prefix="INSIGHT",
            llm_inference_profile_id=INSIGHT_PROFILE_ID,
        )

    async def generate(
        self,
        *,
        run_id: str,
        user_id: str,
        zanei: ZaneiTools,
        workspace_context: str,
        previous_insight: str,
        db_path: Path,
        busy_timeout_ms: int,
    ) -> ShortInsightOutput:
        # The prompt's last paragraph asks for this run's outputs. It leads the
        # conversation, which a request needs at least one item of; the rest,
        # the context it is read against, is the request's own message.
        head, _, ask = self._prompt_config.prompt.rstrip().rpartition("\n\n")
        prompt = head.format(
            workspace_context=workspace_context,
            previous_insight=previous_insight,
        )
        memory = MemoryRetrievalSession(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            context=MemoryContextSession(user_id=user_id, run_id=run_id),
            policy=SHORT_INSIGHT_RETRIEVAL_POLICY,
        )
        sink = CountingSink()
        identity, _ = provider_turns.read_provider_turn_target(
            inference_profile=INSIGHT_PROFILE_ID
        )
        system_instruction = self._prompt_config.system_instruction or ""

        async def send(request: ConversationRequest) -> ActionTurnReply:
            return await self._generate_llm_action_turn(
                sink=sink,
                prompt=request.prompt,
                tools=request.tools,
                max_parallel_tool_calls=request.max_parallel_tool_calls,
                system_instruction=request.system_instruction,
                conversation=request.conversation,
                stage="insight_generation",
            )

        def decide(turn: IdleTurn) -> Finish[ShortInsightOutput] | Continue:
            if turn.ending_call is None:
                return Continue(_COMPLETE_REQUIRED)
            rejection = _completion_rejection(zanei=zanei, final_turn=turn.final)
            if rejection is not None:
                return Continue(rejection)
            try:
                output = ShortInsightOutput.model_validate(turn.ending_call.arguments)
            except ValidationError:
                return Continue(_INVALID_COMPLETION)
            _warn_if_range_unread(run_id=run_id, zanei=zanei)
            return Finish(output)

        async def on_result(
            call: LlmToolCall, result: ReactToolResult
        ) -> LlmTurnToolResultItem:
            # Raw evidence and transcripts live only in this run, not the audit log.
            logger.info(
                "Short Insight step",
                extra={
                    "run_id": run_id,
                    "tool_name": call.name,
                    "status": result.status,
                },
            )
            return LlmTurnToolResultItem(
                type="tool_result",
                call_id=call.call_id,
                name=call.name,
                output=result.output,
            )

        # The history lives only in this run: a stopped run is never resumed, its
        # job starts over from the committed cursor, so nothing is stored.
        return await run_conversation(
            ConversationRun(
                prompt=prompt,
                system_instruction=system_instruction,
                tools=(*zanei.definitions(), *memory.definitions()),
                ending_tools=(
                    LlmToolDefinition(
                        name=_COMPLETED_TOOL_NAME,
                        description="Save the objective activity and short Insight from this run.",
                        parameters=ShortInsightOutput.model_json_schema(),
                    ),
                ),
                history=(
                    ConversationEntry(
                        LlmTurnUserItem(
                            type="user",
                            content=[LlmInputTextBlock(type="input_text", text=ask)],
                        )
                    ),
                ),
                provider_turns=provider_turns.ProviderTurnStore(identity),
                inference_profile=INSIGHT_PROFILE_ID,
                max_turns=_MAX_TURNS,
                max_tool_calls=_MAX_TOOL_CALLS,
                max_parallel_tool_calls=_tunables().max_parallel_tool_calls,
                window=WindowState(
                    budget=ContextBudget(
                        window_tokens=_tunables().context_window_tokens,
                        baseline=None,
                        reset_pending=False,
                    ),
                    omit_before=0,
                ),
                usage=lambda: sink.delta,
                send=send,
                before_send=_nothing_arrives,
                on_turn=_keep_nothing,
                on_result=on_result,
                on_notice=_keep_nothing,
                decide=decide,
            )
        )


def _tunables() -> ActionAgentTunables:
    # The Action's model and window: Insight runs on the same model family, and
    # its own Zanei and memory budgets keep a run far below the window.
    return load_local_runtime_tunables().action_agent


async def _nothing_arrives() -> list[LlmTurnItem]:
    return []


async def _keep_nothing(_item: RecordedTurn | LlmTurnUserItem) -> None:
    return None


def _completion_rejection(*, zanei: ZaneiTools, final_turn: bool) -> str | None:
    """Hold `completed` until this run's fixed Zanei range is read.

    The run's upper bound is pinned when it starts, so a run that stops early
    leaves its own window unread and every later run inherits the backlog. Two
    limits end the drain before that: the forced final turn, where the loop can no
    longer offer `zanei_timeline`, and the run's page and character budgets, which
    keep the accumulated transcript under the proxy's input limit. Rejecting under
    either would fail the run and strand the cursor, so complete and report the
    shortfall.
    """
    if not zanei.read_started:
        return "Read zanei_timeline before completing."
    if not zanei.has_more or final_turn or zanei.page_budget_spent:
        return None
    return (
        "This run's Zanei range is not fully read. Call zanei_timeline until it "
        "reports has_more false, then complete."
    )


def _warn_if_range_unread(*, run_id: str, zanei: ZaneiTools) -> None:
    """Report a run that completed with pages of its own Zanei range unread.

    Called only once the completion is accepted; a rejected final turn never
    completes, so it must not claim to. `pages_read` and `timeline_chars` say
    which of the two drain budgets stopped the run.
    """
    if not zanei.has_more:
        return
    logger.warning(
        "Short Insight completed before reading its whole Zanei range",
        extra={
            "run_id": run_id,
            "events_read": zanei.events_read,
            "pages_read": zanei.pages_read,
            "timeline_chars": zanei.timeline_chars,
            "last_append_sequence": zanei.last_append_sequence,
        },
    )
