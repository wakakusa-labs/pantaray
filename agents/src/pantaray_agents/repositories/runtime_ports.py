from __future__ import annotations

from typing import Literal, Protocol

from pantaray_agents.repositories.action_runtime_resume_contract import (
    ActionRuntimeResumeContext,
)
from pantaray_agents.repositories.action_support.initial_memory_context_contract import (
    InitialMemoryContext,
)
from pantaray_agents.schema.action_tool_call import ActionToolCallOrigin
from pantaray_agents.schema.agent.action import (
    ActionAgentResponse,
    ActionProviderTurnRecord,
    StepType,
)
from pantaray_agents.schema.agent.action_assistant_message import ActionLlmTurnCommit
from pantaray_agents.schema.agent.action_subagent import (
    ActionSubagentCollectionReceipt,
)
from pantaray_agents.schema.agent.base import JSONValue, StepStatusType
from pantaray_agents.schema.agent.suggestion import SuggestionAgentResponse
from pantaray_agents.schema.repositories.repository import DBRow, RepositoryResult
from pantaray_agents.utils.memory_source_policy import MemorySourceCoverageSnapshot
from pantaray_llm.contracts.conversation import LlmProviderTurn

type PatchRunStepKind = Literal["llm", "tool"]
type PatchRunStepStatus = Literal["processing", "success", "error"]


class ActivityRepositoryPort(Protocol):
    async def get_source_data_for_summary(
        self,
        user_id: str,
        summary_type: str,
        period_start: str,
        period_end: str,
        *,
        prompt_name: str | None = None,
        prompt_version: str | None = None,
    ) -> RepositoryResult[list[DBRow]]: ...

    async def get_recent_activity_logs(
        self,
        user_id: str,
        limit: int = 3,
    ) -> RepositoryResult[list[DBRow]]: ...

    async def get_recent_activity_summary(
        self,
        user_id: str,
        summary_type: str,
        limit: int = 1,
    ) -> RepositoryResult[list[DBRow]]: ...

    async def get_activity_logs_by_period_end_range(
        self,
        *,
        user_id: str,
        period_end_from: str,
        period_end_to: str,
        limit: int = 2,
    ) -> RepositoryResult[list[DBRow]]: ...

    async def list_active_user_ids_since(
        self,
        *,
        created_at_from_iso: str,
    ) -> RepositoryResult[list[str]]: ...


class ActionRepositoryPort(Protocol):
    async def get_action(
        self,
        *,
        user_id: str,
        action_id: str,
    ) -> RepositoryResult[DBRow]: ...

    async def get_suggestion(
        self,
        *,
        user_id: str,
        suggestion_id: str | None,
    ) -> RepositoryResult[DBRow]: ...

    async def save_action(
        self,
        response: ActionAgentResponse,
        prompt_name: str,
        prompt_version: str,
        final_prompt_text: str | None = None,
        *,
        steps_budget: int | None = None,
        llm_steps_budget: int | None = None,
        tool_steps_budget: int | None = None,
        token_budget: int | None = None,
        total_steps: int | None = None,
        total_llm_steps: int | None = None,
        total_tool_steps: int | None = None,
        total_prompt_tokens: int | None = None,
        total_completion_tokens: int | None = None,
    ) -> RepositoryResult[DBRow]: ...

    async def save_action_step(
        self,
        step_id: str,
        action_id: str,
        step_number: int,
        step_name: str,
        step_type: StepType,
        user_request_text: str | None = None,
        llm_prompt_text: str | None = None,
        llm_response_text: str | None = None,
        tool_args: dict[str, JSONValue] | None = None,
        tool_output: dict[str, JSONValue] | None = None,
        thinking: str | None = None,
        runtime_state_checkpoint: dict[str, JSONValue] | None = None,
        runtime_state_checkpoint_version: int | None = None,
        status: StepStatusType = StepStatusType.PROCESSING,
        error: dict[str, JSONValue] | None = None,
        execution_time_ms: int | None = None,
        retry_count: int = 0,
        *,
        parent_step_id: str | None = None,
        started_at: str | None = None,
        completed_at: str | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        goal_handle: str,
        user_id: str | None = None,
        short_step_id: str,
        local_step_number: int,
        tool_invocation_ids: tuple[str, ...] = (),
        subagent_collection_receipt: ActionSubagentCollectionReceipt | None = None,
        llm_turn: ActionLlmTurnCommit | None = None,
        origin: ActionToolCallOrigin | None = None,
        provider_turn: ActionProviderTurnRecord | None = None,
    ) -> RepositoryResult[DBRow]: ...

    async def get_action_provider_turns(
        self,
        *,
        user_id: str,
        action_id: str,
        identity: str,
    ) -> RepositoryResult[dict[str, LlmProviderTurn]]: ...

    async def update_action_status_if_processing(
        self,
        *,
        user_id: str,
        action_id: str,
        status: str,
    ) -> RepositoryResult[bool]: ...

    async def increment_action_generation(
        self,
        *,
        user_id: str,
        action_id: str,
    ) -> RepositoryResult[int]: ...

    async def get_action_steps_by_short_step_ids(
        self,
        *,
        user_id: str,
        action_id: str,
        short_step_ids: tuple[str, ...],
    ) -> RepositoryResult[list[DBRow]]: ...

    async def get_runtime_resume_context_for_user_step(
        self,
        *,
        user_id: str,
        action_id: str,
        current_user_step_number: int,
    ) -> RepositoryResult[ActionRuntimeResumeContext]: ...

    async def get_runtime_checkpoint_for_approval_resume(
        self,
        *,
        user_id: str,
        action_id: str,
        approval_session_id: str,
        tool_request_id: str,
    ) -> RepositoryResult[DBRow]: ...

    async def get_initial_memory_context(
        self,
        user_id: str,
        *,
        action_id: str,
        suggestion_id: str | None,
    ) -> RepositoryResult[InitialMemoryContext]: ...

    async def get_memory_source_coverage_snapshot(
        self,
        *,
        user_id: str,
        suggestion_created_at: str | None,
        max_parallel_queries: int,
    ) -> RepositoryResult[MemorySourceCoverageSnapshot]: ...


class SuggestionRepositoryPort(Protocol):
    async def create_processing_suggestion_row(
        self,
        *,
        user_id: str,
        suggestion_id: str,
        created_at: str | None = None,
    ) -> RepositoryResult[DBRow]: ...

    async def get_suggestion(
        self,
        *,
        user_id: str,
        suggestion_id: str,
    ) -> RepositoryResult[DBRow]: ...

    async def save_suggestion(
        self,
        suggestion: SuggestionAgentResponse,
        prompt_name: str,
        prompt_version: str,
        prompt_text: str | None = None,
        response_text: str | None = None,
        request_images_count: int = 0,
        used_images_count: int = 0,
    ) -> RepositoryResult[DBRow]: ...

    async def save_suggestion_run_step(
        self,
        *,
        suggestion_id: str,
        step_number: int,
        step_kind: PatchRunStepKind,
        status: PatchRunStepStatus,
        llm_prompt_text: str | None = None,
        llm_response_text: str | None = None,
        tool_name: str | None = None,
        tool_call_envelope: JSONValue = None,
        tool_output: JSONValue = None,
        error_code: str | None = None,
        error_message: str | None = None,
        created_at: str | None = None,
    ) -> RepositoryResult[DBRow]: ...

    async def finalize_suggestion_start_error_if_processing(
        self,
        *,
        user_id: str,
        suggestion_id: str,
        error_code: str,
        error_message: str,
        error_details: dict[str, object] | None = None,
        metadata: dict[str, object] | None = None,
    ) -> RepositoryResult[DBRow]: ...

    async def discard_suggestion_if_processing(
        self,
        *,
        user_id: str,
        suggestion_id: str,
    ) -> RepositoryResult[DBRow]: ...

    async def mark_user_reaction(
        self,
        *,
        user_id: str,
        suggestion_id: str,
        user_reaction: str,
        action_status: str | None = None,
        action_failure_code: str | None = None,
        update_action_failure_code: bool = False,
        accepted_at: str | None = None,
        rejected_at: str | None = None,
    ) -> RepositoryResult[DBRow]: ...

    async def update_action_status(
        self,
        *,
        user_id: str,
        suggestion_id: str,
        action_status: str,
        action_failure_code: str | None = None,
        update_action_failure_code: bool = False,
    ) -> RepositoryResult[DBRow]: ...

    async def get_recent_suggestions(
        self,
        user_id: str,
        days: int = 7,
        limit: int = 20,
    ) -> RepositoryResult[list[DBRow]]: ...

    async def get_recent_activity_descriptions(
        self,
        user_id: str,
        limit: int = 3,
    ) -> RepositoryResult[list[DBRow]]: ...

    async def get_recent_activity_summary_1h(
        self,
        user_id: str,
        limit: int = 1,
    ) -> RepositoryResult[list[DBRow]]: ...

    async def get_recent_activity_summary(
        self,
        user_id: str,
        *,
        summary_type: str,
        limit: int = 1,
    ) -> RepositoryResult[list[DBRow]]: ...
