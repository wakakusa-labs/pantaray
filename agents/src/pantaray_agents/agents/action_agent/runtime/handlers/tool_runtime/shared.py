"""Action tool runtime の共有型。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, TypedDict

from pantaray_agents.agents.action_agent.runtime.tool_attachments import ToolAttachment
from pantaray_agents.local_runtime.tooling.tool_result_finalization import (
    FinalizedToolOutput,
)
from pantaray_agents.schema.agent.action_subagent import (
    ActionSubagentCollectionReceipt,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.schema.memory_embeddings import MemorySearchSemanticStatus
from pantaray_agents.schema.repositories.repository import DBRow
from pantaray_agents.schema.tool_result import UnprojectedToolOutput

APPROVAL_REQUIRED_OUTPUT_KIND = "approval_required"
APPROVAL_DENIED_OUTPUT_KIND = "approval_denied"
NOT_EXECUTED_OUTPUT_KIND = "not_executed"
"""Body marker of a call a user Stop reached before it was ever issued."""


class ToolValidationError(RuntimeError):
    """ツール引数がスキーマに適合しない場合の例外。"""

    details: dict[str, JSONValue] | None
    tool_invocation_id: str | None
    finalized_output: FinalizedToolOutput | None

    def __init__(
        self,
        message: str,
        *,
        details: Mapping[str, JSONValue] | None = None,
        tool_invocation_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.details = dict(details) if details is not None else None
        self.tool_invocation_id = tool_invocation_id
        self.finalized_output = None


class ToolCompletionAuditPersistenceError(RuntimeError):
    """A completed tool could not persist its terminal audit record."""

    tool_id: str
    tool_invocation_id: str | None

    def __init__(self, *, tool_id: str, tool_invocation_id: str | None) -> None:
        super().__init__("Tool completed, but its audit record could not be persisted.")
        self.tool_id = tool_id
        self.tool_invocation_id = tool_invocation_id


class FinalizedToolExecutionError(RuntimeError):
    """A tool failed after its error output was durably finalized."""

    def __init__(
        self,
        *,
        cause: Exception,
        finalized_output: FinalizedToolOutput,
        tool_invocation_id: str,
    ) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.finalized_output = finalized_output
        self.tool_invocation_id = tool_invocation_id
        self.failure = tool_failure_identity_from_exception(cause)


ToolExecutionActor = Literal["goal_worker", "supervisor"]
ToolExecutionStatus = Literal["success", "error", "processing"]
ValidatedToolArgs = dict[str, JSONValue]


@dataclass(frozen=True, slots=True)
class ToolFailureIdentity:
    error_type: str
    message: str


@dataclass(frozen=True, slots=True)
class CompletedToolControl:
    kind: Literal["completed"] = "completed"


@dataclass(frozen=True, slots=True)
class FailedToolControl:
    failure: ToolFailureIdentity
    kind: Literal["failed"] = "failed"


@dataclass(frozen=True, slots=True)
class ApprovalRequiredToolControl:
    approval_session_id: str
    tool_request_id: str
    intent_class: str
    command_summary: dict[str, JSONValue]
    kind: Literal["approval_required"] = "approval_required"


@dataclass(frozen=True, slots=True)
class ApprovalDeniedToolControl:
    approval_session_id: str
    tool_request_id: str
    kind: Literal["approval_denied"] = "approval_denied"


type ToolExecutionControl = (
    CompletedToolControl
    | FailedToolControl
    | ApprovalRequiredToolControl
    | ApprovalDeniedToolControl
)


@dataclass(frozen=True, slots=True)
class ToolRuntimeContext:
    """Resolved runtime limits/settings needed by tool implementations."""

    max_parallel_memory_queries: int


@dataclass(slots=True)
class UnprojectedToolExecutionResult:
    """Tool implementation result before durable JSON projection."""

    step_id: str
    tool_id: str
    status: ToolExecutionStatus
    started_at: str
    completed_at: str
    output: UnprojectedToolOutput
    attachments: list[ToolAttachment] | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    execution_time_ms: int | None = None
    tool_request_id: str | None = None
    tool_invocation_id: str | None = None
    subagent_collection_receipt: ActionSubagentCollectionReceipt | None = None
    # AGENTS.md blocks this call is the first to reach; rendered with its result.
    agents_md: str | None = None


@dataclass(frozen=True, slots=True)
class ToolExecutionPreparation:
    result: UnprojectedToolExecutionResult
    control: ToolExecutionControl
    finalized_output: FinalizedToolOutput | None = None

    def __post_init__(self) -> None:
        _validate_tool_execution_control(
            status=self.result.status,
            control=self.control,
        )


@dataclass(slots=True)
class ToolExecutionResult:
    """Durably projected result safe for state, history, and checkpoints."""

    step_id: str
    tool_id: str
    status: ToolExecutionStatus
    started_at: str
    completed_at: str
    finalized_output: FinalizedToolOutput
    control: ToolExecutionControl
    attachments: list[ToolAttachment] | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    execution_time_ms: int | None = None
    tool_request_id: str | None = None
    tool_invocation_id: str | None = None
    subagent_collection_receipt: ActionSubagentCollectionReceipt | None = None
    agents_md: str | None = None

    def __post_init__(self) -> None:
        _validate_tool_execution_control(status=self.status, control=self.control)


def _validate_tool_execution_control(
    *,
    status: ToolExecutionStatus,
    control: ToolExecutionControl,
) -> None:
    valid = (
        (
            status == "success"
            and isinstance(control, (CompletedToolControl, ApprovalDeniedToolControl))
        )
        or (status == "error" and isinstance(control, FailedToolControl))
        or (status == "processing" and isinstance(control, ApprovalRequiredToolControl))
    )
    if not valid:
        raise ValueError(
            f"tool result status/control mismatch: status={status} "
            f"control={control.kind}"
        )


def build_tool_execution_result(
    result: UnprojectedToolExecutionResult,
    *,
    finalized_output: FinalizedToolOutput,
    control: ToolExecutionControl,
) -> ToolExecutionResult:
    """Construct the only Action result shape from a finalized output."""

    return ToolExecutionResult(
        step_id=result.step_id,
        tool_id=result.tool_id,
        status=result.status,
        started_at=result.started_at,
        completed_at=result.completed_at,
        finalized_output=finalized_output,
        control=control,
        attachments=result.attachments,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        execution_time_ms=result.execution_time_ms,
        tool_request_id=result.tool_request_id,
        tool_invocation_id=result.tool_invocation_id,
        subagent_collection_receipt=result.subagent_collection_receipt,
        agents_md=result.agents_md,
    )


def build_tool_execution_control(
    *,
    status: ToolExecutionStatus,
    output: UnprojectedToolOutput,
) -> ToolExecutionControl:
    if status == "success":
        return CompletedToolControl()
    if status == "error":
        return FailedToolControl(failure=tool_failure_identity_from_output(output))
    raise ValueError("processing tool results require explicit approval control")


def tool_failure_identity_from_exception(error: BaseException) -> ToolFailureIdentity:
    return ToolFailureIdentity(error_type=error.__class__.__name__, message=str(error))


def tool_failure_identity_from_output(
    output: UnprojectedToolOutput,
) -> ToolFailureIdentity:
    if isinstance(output, Mapping):
        error = output.get("error")
        if isinstance(error, Mapping):
            error_type = _first_non_empty_string(error, ("type", "error_type", "code"))
            message = _first_non_empty_string(error, ("message", "error_message"))
            if error_type and message:
                return ToolFailureIdentity(error_type=error_type, message=message)
    return ToolFailureIdentity(
        error_type="UnknownToolError",
        message="Tool returned an unstructured error.",
    )


def _first_non_empty_string(values: Mapping[str, object], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = values.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


class ThinkingPayload(TypedDict):
    """thinking ツールの出力。"""

    text: str


class MemorySearchPayload(TypedDict):
    """memory_search の出力。"""

    results: list[DBRow]
    semantic_status: MemorySearchSemanticStatus
    semantic_error_code: str | None
    notes: list[str]


class HistoryFetchPayload(TypedDict):
    """One inline page of the selected persisted Action steps, serialized as JSON."""

    refs: list[str]
    content: str
    offset: int
    total_characters: int
    next_cursor: str | None
