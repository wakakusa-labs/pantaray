"""Canonical raw-to-durable boundary for local tool results."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.schema.tool_result import (
    UnprojectedToolOutput,
    build_runtime_tool_error_output,
)
from pantaray_agents.tools.files.manifest_paths import (
    load_tool_results_root,
)

from .action_file_read_memory import build_action_file_read_memory_input
from .models import (
    TOOL_INVOCATION_TERMINAL_STATUSES,
    ToolInvocationCompletionInput,
    ToolInvocationFileReferenceInput,
    ToolInvocationReadMemoryInput,
    ToolInvocationTerminalStatus,
    ToolOutputOwnerKind,
    ToolOutputStorageKind,
)
from .repository import load_tool_definition
from .repository.executions import (
    load_tool_invocation_completion_context,
    record_tool_invocation_completion,
)
from .resources.resource_repository import (
    ToolRuntimeResourceReconciliationError,
    finalize_tool_runtime_resources_for_invocation,
)
from .tool_result_storage import (
    ToolResultStorageResult,
    discard_stored_tool_result,
    release_stored_tool_result,
    store_tool_result,
)
from .tool_result_validation import (
    ToolOutputValidationError,
    validate_successful_tool_output,
    validate_tool_output_representation,
)

ToolResultCompletionScope = Literal["execution", "invocation"]
TIMEOUT_TOOL_OUTPUT_ERROR_TYPE = "ToolTimeoutError"
TIMEOUT_CLEANUP_EVENT_TYPE = "timeout_cleanup_completed"
TIMEOUT_CLEANUP_MESSAGE_SUFFIX = "timeout cleanup completed"

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class InvocationToolResultOwner:
    invocation_id: str
    completed_at: str
    status: ToolInvocationTerminalStatus
    completion_scope: ToolResultCompletionScope


@dataclass(frozen=True, slots=True)
class ActionStepToolResultOwner:
    manifest_id: str
    action_id: str
    user_id: str
    step_id: str


@dataclass(frozen=True, slots=True)
class ApprovalCommandSummaryToolResultOwner:
    manifest_id: str
    action_id: str
    user_id: str
    tool_request_id: str


type ToolResultOwner = (
    InvocationToolResultOwner
    | ActionStepToolResultOwner
    | ApprovalCommandSummaryToolResultOwner
)


@dataclass(frozen=True, slots=True)
class ToolResultFinalizationRequest:
    owner: ToolResultOwner
    output: object
    search_text: str | None = None
    stdout_text: str | None = None
    stderr_text: str | None = None
    file_reference_paths: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FinalizedToolOutput:
    output: JSONValue
    storage_kind: ToolOutputStorageKind
    owner_kind: ToolOutputOwnerKind
    search_text: str | None
    stdout_text: str | None
    stderr_text: str | None


def finalize_local_tool_result(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    request: ToolResultFinalizationRequest,
) -> FinalizedToolOutput:
    """Validate and durably project one result through the only local I/O path."""

    if isinstance(
        request.owner,
        (ActionStepToolResultOwner, ApprovalCommandSummaryToolResultOwner),
    ):
        validated_output = validate_tool_output_representation(request.output)
        return _finalize_action_step_result(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            manifest_id=request.owner.manifest_id,
            action_id=request.owner.action_id,
            user_id=request.owner.user_id,
            storage_owner_id=_action_artifact_owner_id(request.owner),
            output=validated_output,
            search_text=request.search_text,
            stdout_text=request.stdout_text,
            stderr_text=request.stderr_text,
        )
    return _finalize_invocation_result(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        request=request,
        owner=request.owner,
        output=request.output,
    )


def _action_artifact_owner_id(
    owner: ActionStepToolResultOwner | ApprovalCommandSummaryToolResultOwner,
) -> str:
    if isinstance(owner, ActionStepToolResultOwner):
        return owner.step_id
    return owner.tool_request_id


def _finalize_action_step_result(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    manifest_id: str,
    action_id: str,
    user_id: str,
    storage_owner_id: str,
    output: UnprojectedToolOutput,
    search_text: str | None,
    stdout_text: str | None,
    stderr_text: str | None,
) -> FinalizedToolOutput:
    tool_results_path = _load_tool_results_path(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        manifest_id=manifest_id,
        action_id=action_id,
        user_id=user_id,
    )
    stored = store_tool_result(
        action_tool_results_path=tool_results_path,
        invocation_id=storage_owner_id,
        output=output,
        search_text=search_text,
        stdout_text=stdout_text,
        stderr_text=stderr_text,
    )
    _release_durable_result(stored, owner_id=storage_owner_id)
    return _to_finalized_output(stored, owner_kind="action_step")


def _finalize_invocation_result(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    request: ToolResultFinalizationRequest,
    owner: InvocationToolResultOwner,
    output: object,
) -> FinalizedToolOutput:
    if owner.status not in TOOL_INVOCATION_TERMINAL_STATUSES:
        raise ValueError(f"tool invocation status is not terminal: {owner.status}")
    context = load_tool_invocation_completion_context(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        invocation_id=owner.invocation_id,
    )
    tool_results_path = _load_tool_results_path(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        manifest_id=context.manifest_id,
        action_id=context.action_id,
        user_id=context.user_id,
    )
    try:
        validated_output = validate_tool_output_representation(output)
        if owner.status == "completed":
            definition = load_tool_definition(
                db_path=db_path,
                busy_timeout_ms=busy_timeout_ms,
                tool_id=context.tool_id,
            )
            validate_successful_tool_output(
                tool_id=context.tool_id,
                output=validated_output,
                output_schema=definition.output_schema_json,
            )
    except ToolOutputValidationError as error:
        failure_output = build_runtime_tool_error_output(
            error_type=error.__class__.__name__,
            message=str(error),
        )
        stored_failure = _store_terminal_result(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            tool_results_path=tool_results_path,
            owner=InvocationToolResultOwner(
                invocation_id=owner.invocation_id,
                completed_at=owner.completed_at,
                status="failed",
                completion_scope=owner.completion_scope,
            ),
            output=failure_output,
            search_text=None,
            stdout_text=None,
            stderr_text=None,
        )
        _finalize_execution_resources_if_owned(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            owner=owner,
            output=failure_output,
        )
        error.tool_invocation_id = owner.invocation_id
        error.finalized_output = _to_finalized_output(
            stored_failure,
            owner_kind="tool_invocation",
        )
        raise

    stored = _store_terminal_result(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        tool_results_path=tool_results_path,
        owner=owner,
        output=validated_output,
        search_text=request.search_text,
        stdout_text=request.stdout_text,
        stderr_text=request.stderr_text,
        file_reference_paths=request.file_reference_paths,
        read_memory=build_action_file_read_memory_input(
            tool_id=context.tool_id,
            status=owner.status,
            output=validated_output,
        ),
    )
    _finalize_execution_resources_if_owned(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        owner=owner,
        output=validated_output,
    )
    return _to_finalized_output(stored, owner_kind="tool_invocation")


def _store_terminal_result(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    tool_results_path: Path,
    owner: InvocationToolResultOwner,
    output: UnprojectedToolOutput,
    search_text: str | None,
    stdout_text: str | None,
    stderr_text: str | None,
    file_reference_paths: tuple[str, ...] = (),
    read_memory: ToolInvocationReadMemoryInput | None = None,
) -> ToolResultStorageResult:
    stored = store_tool_result(
        action_tool_results_path=tool_results_path,
        invocation_id=owner.invocation_id,
        output=output,
        search_text=search_text,
        stdout_text=stdout_text,
        stderr_text=stderr_text,
    )
    try:
        record_tool_invocation_completion(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            completion=ToolInvocationCompletionInput(
                invocation_id=owner.invocation_id,
                status=owner.status,
                completed_at=owner.completed_at,
                output_json=stored.output_json,
                output_storage_kind=stored.storage_kind,
                search_text=stored.search_text,
                stdout_text=stored.stdout_text,
                stderr_text=stored.stderr_text,
                redaction_applied=False,
                file_references=tuple(
                    ToolInvocationFileReferenceInput(
                        file_reference_id=str(uuid.uuid4()),
                        path=path,
                    )
                    for path in file_reference_paths
                ),
                read_memory=read_memory,
            ),
        )
    except Exception as completion_error:
        try:
            discard_stored_tool_result(stored)
        except OSError as cleanup_error:
            completion_error.add_note(f"tool result rollback failed: {cleanup_error}")
        raise
    _release_durable_result(stored, owner_id=owner.invocation_id)
    return stored


def _release_durable_result(
    stored: ToolResultStorageResult,
    *,
    owner_id: str,
) -> None:
    release_error = release_stored_tool_result(stored)
    if release_error is None:
        return
    logger.warning(
        "Tool-result descriptor release failed after durable write: "
        "owner_id=%s exception_type=%s",
        owner_id,
        release_error.__class__.__name__,
    )


def _finalize_execution_resources_if_owned(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    owner: InvocationToolResultOwner,
    output: UnprojectedToolOutput,
) -> None:
    if owner.completion_scope != "execution":
        return
    cleanup_event_type, cleanup_message_suffix = _cleanup_metadata(output)
    try:
        finalize_tool_runtime_resources_for_invocation(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            invocation_id=owner.invocation_id,
            finalized_at=owner.completed_at,
            cleanup_event_type=cleanup_event_type,
            cleanup_message_suffix=cleanup_message_suffix,
        )
    except ToolRuntimeResourceReconciliationError:
        logger.exception(
            "Post-commit tool resource reconciliation failed; startup recovery "
            "will retry: invocation_id=%s",
            owner.invocation_id,
        )


def _cleanup_metadata(output: UnprojectedToolOutput) -> tuple[str | None, str | None]:
    if not isinstance(output, dict):
        return None, None
    error = output.get("error")
    if (
        not isinstance(error, dict)
        or error.get("type") != TIMEOUT_TOOL_OUTPUT_ERROR_TYPE
    ):
        return None, None
    return TIMEOUT_CLEANUP_EVENT_TYPE, TIMEOUT_CLEANUP_MESSAGE_SUFFIX


def _load_tool_results_path(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    manifest_id: str,
    action_id: str,
    user_id: str,
) -> Path:
    return load_tool_results_root(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        user_id=user_id,
        manifest_id=manifest_id,
        action_id=action_id,
    )


def _to_finalized_output(
    stored: ToolResultStorageResult,
    *,
    owner_kind: ToolOutputOwnerKind,
) -> FinalizedToolOutput:
    return FinalizedToolOutput(
        output=stored.output_json,
        storage_kind=stored.storage_kind,
        owner_kind=owner_kind,
        search_text=stored.search_text,
        stdout_text=stored.stdout_text,
        stderr_text=stored.stderr_text,
    )


__all__ = [
    "ActionStepToolResultOwner",
    "ApprovalCommandSummaryToolResultOwner",
    "FinalizedToolOutput",
    "InvocationToolResultOwner",
    "TIMEOUT_CLEANUP_EVENT_TYPE",
    "TIMEOUT_TOOL_OUTPUT_ERROR_TYPE",
    "ToolResultCompletionScope",
    "ToolResultFinalizationRequest",
    "finalize_local_tool_result",
]
