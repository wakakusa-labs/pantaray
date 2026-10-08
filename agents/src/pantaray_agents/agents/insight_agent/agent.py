"""Read one Zanei delta and produce activity and short Insight together."""

import logging
from dataclasses import replace
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from pantaray_agents.agents.artifact_react import ReactLoopPolicy, ReactLoopStep
from pantaray_agents.agents.artifact_react.native_runner import (
    NativeReactCompletion,
    NativeReactRunInput,
    run_native_react,
)
from pantaray_agents.agents.artifact_react.transcript import (
    build_prompt_with_transcript,
)
from pantaray_agents.agents.core.mixins.llm_tool_use_mixin import (
    LlmToolCallTurn,
    LlmToolUseMixin,
)
from pantaray_agents.agents.core.mixins.llm_usage import CountingSink
from pantaray_agents.agents.core.tool_llm_runner import ToolLlmRunner
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tools.contract import ReactToolResult
from pantaray_agents.tools.memory.retrieval import (
    MEMORY_EDITOR_RETRIEVAL_POLICY,
    MemoryContextSession,
    MemoryRetrievalSession,
)
from pantaray_agents.tools.zanei import ZaneiTools
from pantaray_agents.utils.prompt_loader import prompt_loader
from pantaray_llm.contracts.tool_use import (
    LlmToolContinuation,
    LlmToolDefinition,
    LlmToolResult,
)
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
        prompt = self._prompt_config.prompt.format(
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

        async def call_llm(
            current_prompt: str,
            tools: tuple[LlmToolDefinition, ...],
            continuation: LlmToolContinuation | None,
            tool_result: LlmToolResult | None,
        ) -> LlmToolCallTurn:
            return await self._generate_llm_tool_call(
                sink=sink,
                prompt=current_prompt,
                tools=tools,
                continuation_mode="stateless",
                continuation=continuation,
                tool_result=tool_result,
                system_instruction=self._prompt_config.system_instruction,
                stage="insight_generation",
            )

        def complete(
            arguments: dict[str, JSONValue],
            final_turn: bool,
        ) -> NativeReactCompletion[ShortInsightOutput]:
            rejection = _completion_rejection(zanei=zanei, final_turn=final_turn)
            if rejection is not None:
                return NativeReactCompletion(
                    value=None, final_text="", error_message=rejection
                )
            try:
                output = ShortInsightOutput.model_validate(arguments)
            except ValidationError:
                return NativeReactCompletion(
                    value=None,
                    final_text="",
                    error_message="Provide nonempty activity and insight Markdown and a nullable reconsideration_reason.",
                )
            _warn_if_range_unread(run_id=run_id, zanei=zanei)
            return NativeReactCompletion(value=output, final_text="")

        async def record_step(step: ReactLoopStep) -> None:
            # Raw evidence and transcripts live only in this run, not the audit log.
            logger.info(
                "Short Insight step",
                extra={
                    "run_id": run_id,
                    "step_number": step.step_number,
                    "step_kind": step.step_kind,
                    "tool_name": step.tool_name,
                    "status": step.status,
                },
            )

        async def project_result(result: ReactToolResult) -> ReactToolResult:
            return result

        result = await run_native_react(
            NativeReactRunInput(
                run_id=run_id,
                tool_definitions=(*zanei.definitions(), *memory.definitions()),
                terminal_tool=LlmToolDefinition(
                    name="completed",
                    description="Save the objective activity and short Insight from this run.",
                    parameters=ShortInsightOutput.model_json_schema(),
                ),
                complete=complete,
                build_prompt=lambda results, error: build_prompt_with_transcript(
                    initial_prompt=prompt, tool_results=results, last_error=error
                ),
                call_llm=call_llm,
                record_step=record_step,
                project_tool_result=project_result,
                policy=ReactLoopPolicy(max_llm_turns=40, max_tool_calls=36),
                final_turn_prompt="This run's tool budget is spent. Finish using the events already read.",
                consume_llm_thoughts=self._consume_llm_thoughts,
            )
        )
        if result.value is None:
            raise RuntimeError(
                result.loop_result.last_error or "Short Insight generation failed"
            )
        return result.value


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
