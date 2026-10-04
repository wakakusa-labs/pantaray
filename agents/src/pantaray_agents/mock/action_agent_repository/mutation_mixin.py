"""MockActionAgentRepository の mutation 系責務。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from pydantic import ValidationError

from pantaray_agents.action_status import (
    ACTION_STATUS_PROCESSING,
    ActionRuntimeStatus,
    ActionTerminalStatus,
)
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.repositories.budget_validation import (
    validate_optional_positive_int,
)
from pantaray_agents.schema.action_tool_call import ActionToolCallOrigin
from pantaray_agents.schema.agent.action import ActionProviderTurnRecord, StepType
from pantaray_agents.schema.agent.action_assistant_message import ActionLlmTurnCommit
from pantaray_agents.schema.agent.action_subagent import (
    ActionSubagentCollectionReceipt,
)
from pantaray_agents.schema.agent.base import StepStatusType
from pantaray_agents.schema.repositories.repository import (
    DBRow,
    JSONValue,
    RepositoryErrorKind,
    RepositoryResult,
)

from ..mock_action_agent_repository_types import (
    ActionResponseLike,
    validate_mock_action_row,
    validate_mock_action_step_row,
)

if TYPE_CHECKING:  # pragma: no cover
    from ..mock_action_agent_repository import MockActionAgentRepository


class MockActionAgentMutationMixin:
    @staticmethod
    def _repository_failure(
        *,
        message: str,
        error_kind: RepositoryErrorKind,
        retryable: bool,
    ) -> RepositoryResult[DBRow]:
        return RepositoryResult(
            error=message,
            error_kind=error_kind,
            retryable=retryable,
        )

    def set_next_save_action_error(
        self: MockActionAgentRepository,
        error_message: str,
    ) -> None:
        self._next_save_action_error = error_message

    async def upsert_action_header(
        self: MockActionAgentRepository,
        *,
        action_id: str,
        user_id: str,
        suggestion_id: str,
        prompt_name: str,
        prompt_version: str,
        status: ActionRuntimeStatus = ACTION_STATUS_PROCESSING,
        final_output: str = "",
        final_prompt_text: str | None = None,
        created_at: str | None = None,
        updated_at: str | None = None,
        steps_budget: int | None = None,
        llm_steps_budget: int | None = None,
        tool_steps_budget: int | None = None,
        token_budget: int | None = None,
    ) -> RepositoryResult[DBRow]:
        budget_error = validate_optional_positive_int(
            field_name="token_budget",
            value=token_budget,
        )
        if budget_error is not None:
            return RepositoryResult(error=budget_error)

        try:
            record = validate_mock_action_row(
                {
                    "action_id": action_id,
                    "user_id": user_id,
                    "suggestion_id": suggestion_id,
                    "final_output": final_output,
                    "final_prompt_text": final_prompt_text,
                    "prompt_name": prompt_name,
                    "prompt_version": prompt_version,
                    "status": status,
                    "error": None,
                    "created_at": created_at or now_utc_iso(),
                    "updated_at": updated_at or now_utc_iso(),
                    "steps_budget": steps_budget,
                    "llm_steps_budget": llm_steps_budget,
                    "tool_steps_budget": tool_steps_budget,
                    "token_budget": token_budget,
                    "total_steps": None,
                    "total_llm_steps": None,
                    "total_tool_steps": None,
                    "total_prompt_tokens": None,
                    "total_completion_tokens": None,
                }
            )
        except (ValidationError, ValueError) as exc:
            return RepositoryResult(error=str(exc))
        await self.save_data("actions", record)
        return RepositoryResult(data=record)

    async def save_action(
        self: MockActionAgentRepository,
        response: ActionResponseLike | Mapping[str, object],
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
    ) -> RepositoryResult[DBRow]:
        budget_error = validate_optional_positive_int(
            field_name="token_budget",
            value=token_budget,
        )
        if budget_error is not None:
            return RepositoryResult(error=budget_error)
        if self._next_save_action_error:
            error_msg = self._next_save_action_error
            self._next_save_action_error = None
            return RepositoryResult(error=error_msg)

        base_dict = (
            response.model_dump(exclude_none=False)
            if hasattr(response, "model_dump")
            else dict(response)
        )
        try:
            record = validate_mock_action_row(
                {
                    **base_dict,
                    "final_prompt_text": final_prompt_text,
                    "prompt_name": prompt_name,
                    "prompt_version": prompt_version,
                    "steps_budget": steps_budget,
                    "llm_steps_budget": llm_steps_budget,
                    "tool_steps_budget": tool_steps_budget,
                    "token_budget": token_budget,
                    "total_steps": total_steps,
                    "total_llm_steps": total_llm_steps,
                    "total_tool_steps": total_tool_steps,
                    "total_prompt_tokens": total_prompt_tokens,
                    "total_completion_tokens": total_completion_tokens,
                }
            )
        except (ValidationError, ValueError) as exc:
            return RepositoryResult(error=str(exc))
        await self.save_data("actions", record)
        return RepositoryResult(data=record)

    async def save_action_step(
        self: MockActionAgentRepository,
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
    ) -> RepositoryResult[DBRow]:
        _ = tool_invocation_ids, subagent_collection_receipt
        if not isinstance(user_id, str) or not user_id:
            return self._repository_failure(
                message="save_action_step: user_id is required",
                error_kind=RepositoryErrorKind.VALIDATION,
                retryable=False,
            )

        action_result = await self.get_action(user_id=user_id, action_id=action_id)
        if action_result.error:
            return self._repository_failure(
                message=f"save_action_step: {action_result.error}",
                error_kind=RepositoryErrorKind.NOT_FOUND,
                retryable=False,
            )
        if not action_result.data:
            return self._repository_failure(
                message="save_action_step: Action not found",
                error_kind=RepositoryErrorKind.NOT_FOUND,
                retryable=False,
            )

        record: DBRow = {
            "step_id": step_id,
            "action_id": action_id,
            "step_number": step_number,
            "step_name": step_name,
            "step_type": step_type,
            "user_request_text": user_request_text,
            "llm_prompt_text": llm_prompt_text,
            "llm_response_text": llm_response_text,
            "tool_args": tool_args,
            "tool_output": tool_output,
            "thinking": thinking,
            "call_id": origin.call_id if origin is not None else None,
            "llm_step_id": origin.llm_step_id if origin is not None else None,
            "provider_turn": None if provider_turn is None else provider_turn.turn,
            "provider_turn_identity": (
                None if provider_turn is None else provider_turn.identity
            ),
            "runtime_state_checkpoint": runtime_state_checkpoint,
            "runtime_state_checkpoint_version": runtime_state_checkpoint_version,
            "status": status,
            "error": error,
            "execution_time_ms": execution_time_ms,
            "parent_step_id": parent_step_id,
            "goal_handle": goal_handle,
            "short_step_id": short_step_id,
            "local_step_number": local_step_number,
            "retry_count": retry_count,
            "started_at": started_at,
            "completed_at": completed_at,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "created_at": now_utc_iso(),
        }

        try:
            normalized_record = validate_mock_action_step_row(record)
        except ValidationError as exc:
            return self._repository_failure(
                message=str(exc),
                error_kind=RepositoryErrorKind.VALIDATION,
                retryable=False,
            )
        await self.save_data("action_steps", normalized_record)
        if llm_turn is not None:
            for message in llm_turn.messages:
                await self.save_data(
                    "action_steps",
                    validate_mock_action_step_row(
                        {
                            "step_id": message.step_id,
                            "action_id": action_id,
                            "user_id": user_id,
                            "parent_step_id": step_id,
                            "step_number": message.step_number,
                            "local_step_number": message.local_step_number,
                            "short_step_id": message.short_step_id,
                            "step_type": StepType.ASSISTANT_MESSAGE,
                            "step_name": "assistant_commentary",
                            "status": StepStatusType.SUCCESS,
                            "goal_handle": "S",
                            "llm_response_text": message.content,
                            "started_at": message.created_at,
                            "completed_at": message.created_at,
                            "created_at": message.created_at,
                        }
                    ),
                )
        return RepositoryResult(data=normalized_record)

    async def update_action_status_if_processing(
        self: MockActionAgentRepository,
        *,
        user_id: str,
        action_id: str,
        status: ActionTerminalStatus,
    ) -> RepositoryResult[bool]:
        if not user_id or not action_id:
            return RepositoryResult(
                error="update_action_status_if_processing: user_id/action_id is required"
            )
        res = await self.get_action(user_id=user_id, action_id=action_id)
        row = res.data if isinstance(res.data, dict) else None
        if row is None:
            return RepositoryResult(data=False)

        raw_status = row.get("status")
        current_status = str(raw_status or "")
        if current_status != ACTION_STATUS_PROCESSING:
            return RepositoryResult(data=False)

        updated = dict(row)
        updated["status"] = status
        updated["updated_at"] = now_utc_iso()
        await self.save_data("actions", updated)
        return RepositoryResult(data=True)

    async def update_suggestion_action_status_if_processing(
        self: MockActionAgentRepository,
        *,
        user_id: str,
        suggestion_id: str,
        status: ActionTerminalStatus,
    ) -> RepositoryResult[bool]:
        if not user_id or not suggestion_id:
            return RepositoryResult(
                error=(
                    "update_suggestion_action_status_if_processing: "
                    "user_id/suggestion_id is required"
                )
            )
        res = await self.get_suggestion(user_id=user_id, suggestion_id=suggestion_id)
        row = res.data if isinstance(res.data, dict) else None
        if row is None:
            return RepositoryResult(data=False)
        current_status = str(row.get("action_status") or "").strip().lower()
        if current_status != ACTION_STATUS_PROCESSING:
            return RepositoryResult(data=False)
        updated = dict(row)
        updated["action_status"] = status
        updated["updated_at"] = now_utc_iso()
        await self.save_data("suggestions", updated)
        return RepositoryResult(data=True)
