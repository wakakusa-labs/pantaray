from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path

from pydantic import ValidationError

from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.schema.agent.base import JSONValue

from ..resources.resource_tracking import (
    register_path_resource,
    register_process_group_resource,
)
from ..sandbox.command_sandbox_client import (
    run_command_via_sandbox,
)
from ..tool_result_finalization import (
    InvocationToolResultOwner,
    ToolResultFinalizationRequest,
    finalize_local_tool_result,
)
from ..tool_result_validation import ToolOutputValidationError
from .broker_command_validation import (
    build_validated_command_request,
    build_validated_python_request,
    verify_outside_workspace_folders_unchanged,
)
from .broker_common import (
    APPLY_PATCH_TOOL_ID,
    BASH_TOOL_ID,
    GLOB_TOOL_ID,
    GREP_TOOL_ID,
    LIST_TOOL_ID,
    READ_TOOL_ID,
    RENDER_PDF_PAGE_TOOL_ID,
    RUN_PYTHON_TOOL_ID,
    ApprovalDecisionError,
    BrokerApprovalDeniedError,
    BrokerApprovalRequiredError,
    BrokerContext,
    BrokerExecutionError,
    BrokerPolicyError,
    FinalizedBrokerPolicyError,
    apply_approval_decision,
    ensure_tool_authorization,
    load_broker_context,
)
from .broker_direct import (
    build_patch_approval_summary,
    resolve_patch_mount_plan,
    run_apply_patch_executor,
)
from .broker_direct_read import run_read_executor
from .broker_direct_render_pdf import run_render_pdf_page_executor
from .broker_discovery import (
    run_glob_executor,
    run_grep_executor,
    run_list_executor,
)
from .broker_failure_finalization import (
    finalize_broker_invocation_error,
    finalize_canceled_broker_invocation,
)
from .broker_outcome import (
    BrokerPreflightOutcome,
    BrokerToolOutcome,
    project_broker_tool_outcome,
)
from .broker_protocol import (
    ApplyPatchToolArgs,
    BashToolArgs,
    BrokerToolRequest,
    GlobToolArgs,
    GrepToolArgs,
    ListToolArgs,
    ReadToolArgs,
    RenderPdfPageToolArgs,
    RunPythonToolArgs,
    ValidatedCommandRequest,
    ValidatedGlobRequest,
    ValidatedGrepRequest,
    ValidatedListRequest,
    ValidatedPatchRequest,
    ValidatedReadRequest,
    ValidatedRenderPdfPageRequest,
)
from .broker_registry import BROKER_TOOL_REGISTRY, validate_broker_registry
from .broker_structured_patch import extract_structured_patch_paths
from .execution_start import claim_broker_execution_start


class BrokerCompletionPersistenceError(RuntimeError):
    """A brokered tool ran, but its terminal result could not be persisted."""

    def __init__(self, *, tool_invocation_id: str) -> None:
        super().__init__("brokered tool completion could not be persisted")
        self.tool_invocation_id = tool_invocation_id


BROKER_TOOL_ARGS_INVALID = "BROKER_TOOL_ARGS_INVALID"

logger = logging.getLogger(__name__)


def _effective_tool_request_id(
    *,
    tool_id: str,
    tool_request_id: str | None,
) -> str:
    if tool_id in {APPLY_PATCH_TOOL_ID, BASH_TOOL_ID, RUN_PYTHON_TOOL_ID}:
        if tool_request_id is None or not tool_request_id.strip():
            raise BrokerPolicyError(
                "tool_request_id is required for approval-gated tools"
            )
        return tool_request_id
    return tool_request_id or str(uuid.uuid4())


def _effective_tool_invocation_id(
    *,
    tool_id: str,
    invocation_id: str | None,
    tool_request_id: str,
    preflight_only: bool,
) -> str | None:
    if invocation_id is not None and invocation_id.strip():
        return invocation_id
    if tool_id in {APPLY_PATCH_TOOL_ID, BASH_TOOL_ID, RUN_PYTHON_TOOL_ID}:
        if preflight_only:
            return None
        return f"{tool_id}:{tool_request_id}"
    if preflight_only:
        return None
    return tool_request_id


def _validate_request(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    tool_id: str,
    user_id: str,
    actor_process_id: str,
    manifest_id: str,
    execution_session_id: str,
    args: dict[str, JSONValue],
    invocation_id: str | None,
    tool_request_id: str | None,
    requested_at: str | None,
    preflight_only: bool,
) -> tuple[
    BrokerContext,
    ValidatedReadRequest
    | ValidatedRenderPdfPageRequest
    | ValidatedListRequest
    | ValidatedGlobRequest
    | ValidatedGrepRequest
    | ValidatedPatchRequest
    | ValidatedCommandRequest,
]:
    validate_broker_registry()
    definition = BROKER_TOOL_REGISTRY.definitions.get(tool_id)
    if definition is None:
        raise BrokerPolicyError(f"unsupported broker tool: {tool_id}")
    context = load_broker_context(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        tool_id=tool_id,
        path_access_kind=definition.path_access_kind,
        user_id=user_id,
        actor_process_id=actor_process_id,
        manifest_id=manifest_id,
        execution_session_id=execution_session_id,
    )
    request = BrokerToolRequest(
        tool_id=tool_id,
        user_id=user_id,
        manifest_id=manifest_id,
        execution_session_id=execution_session_id,
        args=_parse_args(tool_id=tool_id, args=args),
        invocation_id=invocation_id,
        tool_request_id=tool_request_id,
        requested_at=requested_at,
        preflight_only=preflight_only,
    )
    effective_tool_request_id = _effective_tool_request_id(
        tool_id=request.tool_id,
        tool_request_id=request.tool_request_id,
    )
    effective_invocation_id = _effective_tool_invocation_id(
        tool_id=request.tool_id,
        invocation_id=request.invocation_id,
        tool_request_id=effective_tool_request_id,
        preflight_only=preflight_only,
    )
    effective_requested_at = request.requested_at or "unknown"
    if definition.execution_path == "broker_direct_read":
        assert isinstance(request.args, ReadToolArgs)
        return context, ValidatedReadRequest(
            tool_invocation_id=effective_invocation_id,
            manifest_id=request.manifest_id,
            execution_session_id=request.execution_session_id,
            action_id=context.execution_session.action_id or "unknown",
            tool_request_id=effective_tool_request_id,
            requested_at=effective_requested_at,
            path=request.args.path,
            offset=request.args.offset,
            column=request.args.column,
            limit=request.args.limit,
            start_unit=request.args.start_unit,
        )
    if definition.execution_path == "broker_direct_render_pdf":
        assert isinstance(request.args, RenderPdfPageToolArgs)
        return context, ValidatedRenderPdfPageRequest(
            tool_invocation_id=effective_invocation_id,
            manifest_id=request.manifest_id,
            execution_session_id=request.execution_session_id,
            action_id=context.execution_session.action_id or "unknown",
            tool_request_id=effective_tool_request_id,
            requested_at=effective_requested_at,
            path=request.args.path,
            pages=request.args.pages,
        )
    if definition.execution_path == "broker_direct_list":
        assert isinstance(request.args, ListToolArgs)
        return context, ValidatedListRequest(
            tool_invocation_id=effective_invocation_id,
            manifest_id=request.manifest_id,
            execution_session_id=request.execution_session_id,
            action_id=context.execution_session.action_id or "unknown",
            tool_request_id=effective_tool_request_id,
            requested_at=effective_requested_at,
            path=request.args.path,
            max_depth=request.args.max_depth,
            limit=request.args.limit,
        )
    if definition.execution_path == "broker_direct_glob":
        assert isinstance(request.args, GlobToolArgs)
        return context, ValidatedGlobRequest(
            tool_invocation_id=effective_invocation_id,
            manifest_id=request.manifest_id,
            execution_session_id=request.execution_session_id,
            action_id=context.execution_session.action_id or "unknown",
            tool_request_id=effective_tool_request_id,
            requested_at=effective_requested_at,
            base_path=request.args.base_path,
            pattern=request.args.pattern,
            limit=request.args.limit,
        )
    if definition.execution_path == "broker_direct_grep":
        assert isinstance(request.args, GrepToolArgs)
        return context, ValidatedGrepRequest(
            tool_invocation_id=effective_invocation_id,
            manifest_id=request.manifest_id,
            execution_session_id=request.execution_session_id,
            action_id=context.execution_session.action_id or "unknown",
            tool_request_id=effective_tool_request_id,
            requested_at=effective_requested_at,
            base_path=request.args.base_path,
            pattern=request.args.pattern,
            include_glob=request.args.include_glob,
            max_matches=request.args.max_matches,
        )
    if definition.execution_path == "broker_direct_patch":
        assert isinstance(request.args, ApplyPatchToolArgs)
        return context, _build_validated_patch_request(
            context=context,
            args=request.args,
            tool_invocation_id=effective_invocation_id,
            tool_request_id=effective_tool_request_id,
            requested_at=effective_requested_at,
            preflight_only=preflight_only,
        )
    if definition.execution_path == "broker_sandbox_command":
        assert isinstance(request.args, BashToolArgs)
        return context, build_validated_command_request(
            context=context,
            args=request.args,
            tool_invocation_id=effective_invocation_id,
            tool_request_id=effective_tool_request_id,
            requested_at=effective_requested_at,
            preflight_only=preflight_only,
        )
    if definition.execution_path == "broker_sandbox_python":
        assert isinstance(request.args, RunPythonToolArgs)
        return context, build_validated_python_request(
            context=context,
            args=request.args,
            tool_invocation_id=effective_invocation_id,
            tool_request_id=effective_tool_request_id,
            requested_at=effective_requested_at,
            preflight_only=preflight_only,
        )
    raise BrokerPolicyError(
        f"unsupported broker execution path: {definition.execution_path}"
    )


def _parse_args(
    *,
    tool_id: str,
    args: dict[str, JSONValue],
) -> (
    ReadToolArgs
    | RenderPdfPageToolArgs
    | ListToolArgs
    | GlobToolArgs
    | GrepToolArgs
    | ApplyPatchToolArgs
    | BashToolArgs
    | RunPythonToolArgs
):
    try:
        if tool_id == READ_TOOL_ID:
            return ReadToolArgs.model_validate(args)
        if tool_id == RENDER_PDF_PAGE_TOOL_ID:
            return RenderPdfPageToolArgs.model_validate(args)
        if tool_id == LIST_TOOL_ID:
            return ListToolArgs.model_validate(args)
        if tool_id == GLOB_TOOL_ID:
            return GlobToolArgs.model_validate(args)
        if tool_id == GREP_TOOL_ID:
            return GrepToolArgs.model_validate(args)
        if tool_id == APPLY_PATCH_TOOL_ID:
            return ApplyPatchToolArgs.model_validate(args)
        if tool_id == BASH_TOOL_ID:
            return BashToolArgs.model_validate(args)
        if tool_id == RUN_PYTHON_TOOL_ID:
            return RunPythonToolArgs.model_validate(args)
    except ValidationError as exc:
        raise BrokerPolicyError(
            f"{BROKER_TOOL_ARGS_INVALID}: invalid {tool_id} arguments: {exc}",
            code=BROKER_TOOL_ARGS_INVALID,
        ) from exc
    raise BrokerPolicyError(f"unsupported broker tool: {tool_id}")


def _build_validated_patch_request(
    *,
    context: BrokerContext,
    args: ApplyPatchToolArgs,
    tool_invocation_id: str | None,
    tool_request_id: str,
    requested_at: str,
    preflight_only: bool,
) -> ValidatedPatchRequest:
    patch_paths = tuple(extract_structured_patch_paths(args.changes))
    plan = resolve_patch_mount_plan(context=context, patch_paths=patch_paths)
    if not preflight_only and (
        tool_invocation_id is None or not tool_invocation_id.strip()
    ):
        raise BrokerPolicyError("tool_invocation_id is required for apply_patch")
    command_summary = build_patch_approval_summary(
        context=context, patch_paths=patch_paths, plan=plan
    )
    approval_session_id, approval_source = ensure_tool_authorization(
        context=context,
        tool_invocation_id=tool_invocation_id,
        tool_request_id=tool_request_id,
        command_summary=command_summary,
        requested_at=requested_at,
        require_user_prompt=plan.outside_workspace_folder is not None,
    )
    return ValidatedPatchRequest(
        tool_invocation_id=tool_invocation_id or "",
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session.execution_session_id,
        action_id=context.execution_session.action_id or "unknown",
        approval_session_id=approval_session_id,
        approval_source=approval_source,
        changes=args.changes,
        patch_paths=list(patch_paths),
        command_summary_json=command_summary,
        tool_request_id=tool_request_id,
        requested_at=requested_at,
        preflight_only=preflight_only,
    )


async def execute_broker_tool(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    tool_id: str,
    user_id: str,
    actor_process_id: str,
    manifest_id: str,
    execution_session_id: str,
    args: dict[str, JSONValue],
    invocation_id: str | None = None,
    tool_request_id: str | None = None,
    requested_at: str | None = None,
    preflight_only: bool = False,
) -> BrokerPreflightOutcome | BrokerToolOutcome:
    context, validated = _validate_request(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        tool_id=tool_id,
        user_id=user_id,
        actor_process_id=actor_process_id,
        manifest_id=manifest_id,
        execution_session_id=execution_session_id,
        args=args,
        invocation_id=invocation_id,
        tool_request_id=tool_request_id,
        requested_at=requested_at,
        preflight_only=preflight_only,
    )
    if isinstance(validated, ValidatedPatchRequest) and validated.preflight_only:
        return BrokerPreflightOutcome(
            status="success",
            output={
                "status": "success",
                "applied_paths": list(validated.patch_paths),
            },
        )
    if isinstance(validated, ValidatedCommandRequest) and validated.preflight_only:
        return BrokerPreflightOutcome(
            status="success",
            output={
                "status": "success",
                "exit_code": 0,
                "stdout": "",
                "stderr": "",
            },
        )
    completion_invocation_id = claim_broker_execution_start(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        context=context,
        validated=validated,
        args=args,
    )
    if completion_invocation_id is None:
        raise BrokerPolicyError(
            "non-preflight broker execution requires an invocation audit"
        )
    try:
        if isinstance(validated, ValidatedPatchRequest):
            outcome = await asyncio.to_thread(
                run_apply_patch_executor,
                context=context,
                request=validated,
            )
        elif isinstance(validated, ValidatedReadRequest):
            outcome = await asyncio.to_thread(
                run_read_executor,
                context=context,
                request=validated,
            )
        elif isinstance(validated, ValidatedRenderPdfPageRequest):
            # Drawing already waits on a child process, so it is awaited here
            # rather than handed to a worker thread that would only wait beside
            # it.
            outcome = await run_render_pdf_page_executor(
                context=context,
                request=validated,
            )
        elif isinstance(validated, ValidatedListRequest):
            outcome = await asyncio.to_thread(
                run_list_executor,
                context=context,
                request=validated,
            )
        elif isinstance(validated, ValidatedGlobRequest):
            outcome = await asyncio.to_thread(
                run_glob_executor,
                context=context,
                request=validated,
            )
        elif isinstance(validated, ValidatedGrepRequest):
            outcome = await asyncio.to_thread(
                run_grep_executor,
                context=context,
                request=validated,
            )
        else:
            verify_outside_workspace_folders_unchanged(
                context=context, request=validated
            )
            outcome = await run_command_via_sandbox(
                context=context,
                request=validated,
                create_subprocess_exec=asyncio.create_subprocess_exec,
                register_process_group_resource_fn=register_process_group_resource,
                register_path_resource_fn=register_path_resource,
            )
    except asyncio.CancelledError as exc:
        # Cancellation (a user Stop, or the parallel batch timeout) does not reach
        # `except Exception`, yet the broker owns this invocation's audit row and
        # must still close it. The cancellation itself always propagates.
        try:
            finalize_canceled_broker_invocation(
                db_path=db_path,
                busy_timeout_ms=busy_timeout_ms,
                invocation_id=completion_invocation_id,
                error=exc,
            )
        except Exception:
            logger.exception(
                "failed to finalize canceled tool invocation %s",
                completion_invocation_id,
            )
        raise
    except Exception as exc:
        if isinstance(exc, (BrokerApprovalDeniedError, BrokerApprovalRequiredError)):
            raise
        try:
            finalized_error = finalize_broker_invocation_error(
                db_path=db_path,
                busy_timeout_ms=busy_timeout_ms,
                invocation_id=completion_invocation_id,
                error=exc,
            )
        except Exception as persistence_error:
            raise BrokerCompletionPersistenceError(
                tool_invocation_id=completion_invocation_id
            ) from persistence_error
        if isinstance(exc, BrokerPolicyError):
            raise FinalizedBrokerPolicyError(
                cause=exc,
                tool_invocation_id=completion_invocation_id,
                finalized_output=finalized_error.output,
                output_storage_kind=finalized_error.storage_kind,
            ) from exc
        raise BrokerExecutionError(
            str(exc),
            tool_invocation_id=completion_invocation_id,
            finalized_output=finalized_error.output,
            output_storage_kind=finalized_error.storage_kind,
        ) from exc
    try:
        finalized_result = finalize_local_tool_result(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            request=ToolResultFinalizationRequest(
                owner=InvocationToolResultOwner(
                    invocation_id=completion_invocation_id,
                    completed_at=now_utc_iso(),
                    status=("completed" if outcome.status == "success" else "failed"),
                    completion_scope="execution",
                ),
                output=outcome.output,
                search_text=outcome.search_text,
                stdout_text=outcome.stdout_text,
                stderr_text=outcome.stderr_text,
                file_reference_paths=outcome.file_reference_paths,
            ),
        )
    except ToolOutputValidationError as output_error:
        output_error.tool_invocation_id = completion_invocation_id
        raise
    except Exception as persistence_error:
        raise BrokerCompletionPersistenceError(
            tool_invocation_id=completion_invocation_id
        ) from persistence_error
    return project_broker_tool_outcome(
        outcome,
        output=finalized_result.output,
        tool_invocation_id=completion_invocation_id,
        search_text=finalized_result.search_text,
        stdout_text=finalized_result.stdout_text,
        stderr_text=finalized_result.stderr_text,
        output_storage_kind=finalized_result.storage_kind,
    )


__all__ = [
    "ApprovalDecisionError",
    "BrokerApprovalDeniedError",
    "BrokerApprovalRequiredError",
    "BrokerCompletionPersistenceError",
    "BrokerExecutionError",
    "BrokerPolicyError",
    "BrokerPreflightOutcome",
    "BrokerToolOutcome",
    "FinalizedBrokerPolicyError",
    "apply_approval_decision",
    "execute_broker_tool",
]
