"""Canonical persistence boundary for finalized Action tool steps."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, TypedDict, cast

from pantaray_agents.agents.action_agent.runtime.checkpoint import (
    RUNTIME_STATE_CHECKPOINT_VERSION,
    build_runtime_state_checkpoint,
)
from pantaray_agents.agents.action_agent.runtime.state import ActionAgentState
from pantaray_agents.agents.action_agent.runtime.steps.persistence import (
    ActionStepPersistenceError,
    persist_action_step,
)
from pantaray_agents.agents.action_agent.support.severity import NON_FATAL_SEVERITIES
from pantaray_agents.application.action.ports import (
    ActionStepEmitter,
    ActionToolStepEmission,
)
from pantaray_agents.local_runtime.tooling.tool_result_finalization import (
    FinalizedToolOutput,
)
from pantaray_agents.schema.action_tool_call import ActionToolCallOrigin
from pantaray_agents.schema.agent.action import StepType
from pantaray_agents.schema.agent.action_subagent import (
    ActionSubagentCollectionReceipt,
)
from pantaray_agents.schema.agent.base import AgentError, JSONValue, StepStatusType
from pantaray_agents.schema.repositories.repository import DBRow, RepositoryResult
from pantaray_agents.schema.tool_result import FormalToolStepOutput

if TYPE_CHECKING:  # pragma: no cover
    from pantaray_agents.agents.action_agent import ActionAgent

type ToolStepPayload = dict[str, JSONValue]
ToolStepStatus = Literal["success", "error", "processing"]

# Full diagnostics belong to finalized output and therefore obey the 20,000-character
# spill contract. The separate error column is only a bounded public identity.
TOOL_STEP_ERROR_MESSAGE_MAX_CHARS = 2_000


class ToolStepError(TypedDict):
    error_type: str
    error_code: str
    error_message: str | None
    severity: str


@dataclass(frozen=True, slots=True)
class FinalizedToolStepResult:
    status: ToolStepStatus
    output: FinalizedToolOutput
    error: ToolStepError | None = None

    def __post_init__(self) -> None:
        if self.status == "processing" and self.error is not None:
            raise ValueError("processing tool steps cannot contain an error")
        if self.status == "error" and self.error is None:
            raise ValueError("error tool steps require a bounded error identity")
        if (
            self.status == "success"
            and self.error is not None
            and self.error["severity"].lower() not in NON_FATAL_SEVERITIES
        ):
            raise ValueError("successful tool steps only accept non-fatal errors")


@dataclass(frozen=True, slots=True)
class ToolStepPersistenceProjection:
    output_json: dict[str, JSONValue]
    status: StepStatusType
    error: dict[str, JSONValue] | None


@dataclass(frozen=True, slots=True)
class TerminalStepEmission:
    emit: ActionStepEmitter
    label: str


def build_finalized_tool_step(
    *,
    status: ToolStepStatus,
    output: FinalizedToolOutput,
    error: AgentError | None = None,
) -> FinalizedToolStepResult:
    """Build the only formal-step input from an already durable output."""

    return FinalizedToolStepResult(
        status=status,
        output=output,
        error=project_tool_step_error(error) if error is not None else None,
    )


def project_tool_step_error(error: AgentError) -> ToolStepError:
    message = error.error_message
    if message is not None and len(message) > TOOL_STEP_ERROR_MESSAGE_MAX_CHARS:
        message = message[:TOOL_STEP_ERROR_MESSAGE_MAX_CHARS]
    return ToolStepError(
        error_type=error.error_type,
        error_code=error.error_code,
        error_message=message,
        severity=error.severity,
    )


def project_tool_step_for_persistence(
    result: FinalizedToolStepResult,
) -> ToolStepPersistenceProjection:
    """Derive every persisted field from one finalized formal-step result."""

    envelope = FormalToolStepOutput(
        schema_version=1,
        status=result.status,
        output=result.output.output,
        output_storage_kind=result.output.storage_kind,
        output_owner_kind=result.output.owner_kind,
    )
    error = cast(
        "dict[str, JSONValue] | None",
        dict(result.error) if result.error is not None else None,
    )
    return ToolStepPersistenceProjection(
        output_json=cast("dict[str, JSONValue]", envelope.model_dump(mode="json")),
        status=StepStatusType(result.status),
        error=error,
    )


class ToolStepPersistenceError(ActionStepPersistenceError):
    """Tool execution step persistence failed."""


async def record_tool_step(
    agent: ActionAgent,
    state: ActionAgentState,
    *,
    step_id: str,
    action_id: str,
    step_number: int,
    step_name: str,
    tool_args: ToolStepPayload,
    result: FinalizedToolStepResult,
    started_at: str,
    completed_at: str,
    goal_handle: str,
    user_id: str,
    short_step_id: str,
    local_step_number: int,
    parent_step_id: str | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    execution_time_ms: int | None = None,
    retry_count: int = 0,
    tool_invocation_ids: tuple[str, ...] = (),
    subagent_collection_receipt: ActionSubagentCollectionReceipt | None = None,
    terminal_emission: TerminalStepEmission | None = None,
    origin: ActionToolCallOrigin | None = None,
) -> None:
    """Persist one formal tool step and link its audit invocations.

    ``origin`` is absent only where no adopted call declared the step: a
    checkpoint whose ``next_action`` predates batches, and the rows written for a
    call the tool contract rejected before a slot existed.
    """

    persistence = project_tool_step_for_persistence(result)

    async def _save() -> RepositoryResult[DBRow]:
        return await agent.repository.save_action_step(
            step_id=step_id,
            action_id=action_id,
            step_number=step_number,
            step_name=step_name,
            step_type=StepType.TOOL_EXECUTION,
            tool_args=tool_args,
            tool_output=persistence.output_json,
            thinking=None,
            runtime_state_checkpoint=build_runtime_state_checkpoint(state),
            runtime_state_checkpoint_version=RUNTIME_STATE_CHECKPOINT_VERSION,
            status=persistence.status,
            error=persistence.error,
            started_at=started_at,
            completed_at=completed_at,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            execution_time_ms=execution_time_ms,
            retry_count=retry_count,
            parent_step_id=parent_step_id,
            goal_handle=goal_handle,
            user_id=user_id,
            short_step_id=short_step_id,
            local_step_number=local_step_number,
            tool_invocation_ids=tool_invocation_ids,
            subagent_collection_receipt=subagent_collection_receipt,
            origin=origin,
        )

    try:
        await persist_action_step(
            save=_save,
            step_name=step_name,
            step_id=step_id,
        )
    except ActionStepPersistenceError as exc:
        raise ToolStepPersistenceError(str(exc)) from exc
    if terminal_emission is not None:
        await terminal_emission.emit(
            ActionToolStepEmission(
                step_id=step_id,
                step_number=step_number,
                step_name=step_name,
                label=terminal_emission.label,
                tool_args=tool_args,
                status=result.status,
                started_at=started_at,
                completed_at=completed_at,
            )
        )


__all__ = [
    "FinalizedToolStepResult",
    "TerminalStepEmission",
    "TOOL_STEP_ERROR_MESSAGE_MAX_CHARS",
    "ToolStepError",
    "ToolStepPersistenceError",
    "ToolStepPersistenceProjection",
    "ToolStepStatus",
    "build_finalized_tool_step",
    "project_tool_step_error",
    "project_tool_step_for_persistence",
    "record_tool_step",
]
