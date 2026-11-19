from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from pantaray_agents.local_runtime.runtime.browser_relay import close_browser_session
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tools.contract import BrokerPolicyError
from pantaray_agents.tools.files.discovery import run_glob, run_grep, run_list
from pantaray_agents.tools.files.read_scope import ReadScope

from ..resources.resource_tracking import (
    register_path_resource,
    register_process_group_resource,
)
from ..sandbox.command_egress import run_command_via_sandbox
from ..tool_result_finalization import (
    InvocationToolResultOwner,
    ToolInvocationCanceledFirstError,
    ToolResultFinalizationRequest,
    finalize_local_tool_result,
)
from ..tool_result_validation import ToolOutputValidationError
from .action_path_policy import read_scope
from .broker_browser import BrowserNetworkOffError, run_browser_executor
from .broker_command_validation import verify_outside_workspace_folders_unchanged
from .broker_common import (
    ApprovalDecisionError,
    BrokerApprovalDeniedError,
    BrokerApprovalRequiredError,
    BrokerContext,
    BrokerExecutionError,
    FinalizedBrokerPolicyError,
    apply_approval_decision,
    ensure_session_capabilities,
)
from .broker_direct import run_apply_patch_executor
from .broker_direct_read import run_read_executor
from .broker_direct_render_pdf import run_render_pdf_page_executor
from .broker_failure_finalization import (
    canceled_broker_tool_outcome,
    finalize_broker_invocation_error,
    finalize_canceled_broker_invocation,
)
from .broker_outcome import (
    BrokerPreflightOutcome,
    BrokerToolOutcome,
    project_broker_tool_outcome,
    read_tool_outcome,
)
from .broker_process import run_process_control, start_background_command
from .broker_protocol import (
    ValidatedCommandRequest,
    ValidatedGlobRequest,
    ValidatedGrepRequest,
    ValidatedListRequest,
    ValidatedPatchRequest,
    ValidatedReadRequest,
    ValidatedRenderPdfPageRequest,
)
from .broker_request_validation import validate_broker_request
from .browser_protocol import (
    ValidatedBrowserPrintRequest,
    ValidatedBrowserRequest,
    ValidatedBrowserWriteRequest,
    ValidatedUserBrowserReadRequest,
)
from .execution_start import claim_broker_execution_start
from .process_protocol import (
    ValidatedReadProcessOutputRequest,
    ValidatedStopProcessRequest,
)


class BrokerCompletionPersistenceError(RuntimeError):
    """A brokered tool ran, but its terminal result could not be persisted."""

    def __init__(self, *, tool_invocation_id: str) -> None:
        super().__init__("brokered tool completion could not be persisted")
        self.tool_invocation_id = tool_invocation_id


logger = logging.getLogger(__name__)

_BROWSER_REQUESTS = (
    ValidatedBrowserRequest,
    ValidatedBrowserPrintRequest,
    ValidatedBrowserWriteRequest,
    ValidatedUserBrowserReadRequest,
)


def _superseded_by_cancel(
    canceled_first: ToolInvocationCanceledFirstError,
) -> BrokerToolOutcome:
    """The result of a call that finished after a cancel had closed its row."""

    logger.info(
        "tool invocation %s finished after it was canceled; the cancel stands",
        canceled_first.invocation_id,
    )
    return canceled_broker_tool_outcome(
        invocation_id=canceled_first.invocation_id,
        canceled_result=canceled_first.canceled_result,
    )


def _checked_read_scope(context: BrokerContext) -> ReadScope:
    """The scope a read/search call runs in, once the Action's own checks pass."""

    ensure_session_capabilities(context=context)
    return read_scope(context)


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
    try:
        context, validated = validate_broker_request(
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
    except BrowserNetworkOffError as refused:
        # A page opened while the network was on keeps it until Electron is
        # sent the setting, which this refusal never does: the page ends.
        await close_browser_session(
            owner_process_id=actor_process_id, browser=refused.browser
        )
        raise
    if isinstance(validated, ValidatedPatchRequest) and validated.preflight_only:
        return BrokerPreflightOutcome(
            status="success",
            output={
                "status": "success",
                "applied_paths": list(validated.patch_paths),
            },
        )
    if preflight_only and isinstance(validated, _BROWSER_REQUESTS):
        return BrokerPreflightOutcome(status="success", output={"status": "success"})
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
                scope=_checked_read_scope(context),
                request=validated.args,
                user_id=context.execution_session.user_id,
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
            outcome = read_tool_outcome(
                await asyncio.to_thread(
                    run_list,
                    scope=_checked_read_scope(context),
                    request=validated.args,
                )
            )
        elif isinstance(validated, ValidatedGlobRequest):
            outcome = read_tool_outcome(
                await asyncio.to_thread(
                    run_glob,
                    scope=_checked_read_scope(context),
                    request=validated.args,
                )
            )
        elif isinstance(validated, ValidatedGrepRequest):
            outcome = read_tool_outcome(
                await asyncio.to_thread(
                    run_grep,
                    scope=_checked_read_scope(context),
                    request=validated.args,
                )
            )
        elif isinstance(
            validated, (ValidatedReadProcessOutputRequest, ValidatedStopProcessRequest)
        ):
            outcome = await run_process_control(context=context, request=validated)
        elif isinstance(validated, _BROWSER_REQUESTS):
            outcome = await run_browser_executor(context=context, request=validated)
        else:
            verify_outside_workspace_folders_unchanged(
                context=context, request=validated
            )
            outcome = await (
                start_background_command(context=context, request=validated)
                if validated.run_in_background
                else run_command_via_sandbox(
                    context=context,
                    request=validated,
                    create_subprocess_exec=asyncio.create_subprocess_exec,
                    register_process_group_resource_fn=register_process_group_resource,
                    register_path_resource_fn=register_path_resource,
                )
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
        except ToolInvocationCanceledFirstError:
            # A cancel recorded first already wrote the terminal this would have.
            pass
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
        except ToolInvocationCanceledFirstError as canceled_first:
            return _superseded_by_cancel(canceled_first)
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
    except ToolInvocationCanceledFirstError as canceled_first:
        return _superseded_by_cancel(canceled_first)
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
    "BrokerPreflightOutcome",
    "BrokerToolOutcome",
    "FinalizedBrokerPolicyError",
    "apply_approval_decision",
    "execute_broker_tool",
]
