"""Turn a brokered tool call into the validated request the broker executes."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import cast

from pydantic import ValidationError

from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tools.contract import BrokerPolicyError
from pantaray_agents.tools.files.render_pages import RenderPdfPageToolArgs

from .broker_browser import (
    build_validated_browser_write_request,
    refuse_browser_network_use,
)
from .broker_browser_print import build_validated_browser_print_request
from .broker_command_validation import (
    build_validated_command_request,
    build_validated_python_request,
)
from .broker_common import (
    APPLY_PATCH_TOOL_ID,
    BASH_TOOL_ID,
    BROWSER_TOOL_ID,
    RUN_PYTHON_TOOL_ID,
    BrokerContext,
    load_broker_context,
)
from .broker_direct import build_validated_patch_request
from .broker_protocol import (
    ApplyPatchToolArgs,
    BashToolArgs,
    BrokerExecutionPath,
    BrokerManagedToolDefinition,
    BrokerToolArgs,
    BrokerToolRequest,
    RunPythonToolArgs,
    ValidatedGlobRequest,
    ValidatedGrepRequest,
    ValidatedListRequest,
    ValidatedReadRequest,
    ValidatedRenderPdfPageRequest,
)
from .broker_registry import (
    BROKER_MODEL_REGISTRY,
    BROKER_TOOL_REGISTRY,
    validate_broker_registry,
)
from .broker_user_browser import (
    build_validated_user_browser_read_request,
    refuse_printing,
    user_browser_for_run,
)
from .browser_protocol import (
    BrowserAllowWritesArgs,
    BrowserCloseArgs,
    BrowserNavigateArgs,
    BrowserPrintArgs,
    BrowserToolCall,
    ValidatedBrowserRequest,
)
from .direct_execution_start import ValidatedBrokerRequest
from .process_protocol import (
    ValidatedReadProcessOutputRequest,
    ValidatedStopProcessRequest,
)

BROKER_TOOL_ARGS_INVALID = "BROKER_TOOL_ARGS_INVALID"
# browser asks only for print and allow_writes, but every call carries the
# request id it binds to.
_APPROVAL_GATED_TOOL_IDS = frozenset(
    {APPLY_PATCH_TOOL_ID, BROWSER_TOOL_ID, BASH_TOOL_ID, RUN_PYTHON_TOOL_ID}
)
# Read-only requests that carry their parsed arguments as they are.
_ARGS_REQUESTS: dict[
    BrokerExecutionPath,
    type[ValidatedReadRequest]
    | type[ValidatedListRequest]
    | type[ValidatedGlobRequest]
    | type[ValidatedGrepRequest]
    | type[ValidatedReadProcessOutputRequest]
    | type[ValidatedStopProcessRequest],
] = {
    "broker_direct_read": ValidatedReadRequest,
    "broker_direct_list": ValidatedListRequest,
    "broker_direct_glob": ValidatedGlobRequest,
    "broker_direct_grep": ValidatedGrepRequest,
    "broker_process_read": ValidatedReadProcessOutputRequest,
    "broker_process_stop": ValidatedStopProcessRequest,
}


def _effective_tool_request_id(
    *,
    tool_id: str,
    tool_request_id: str | None,
) -> str:
    if tool_id in _APPROVAL_GATED_TOOL_IDS:
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
    if tool_id in _APPROVAL_GATED_TOOL_IDS:
        if preflight_only:
            return None
        return f"{tool_id}:{tool_request_id}"
    if preflight_only:
        return None
    return tool_request_id


def validate_broker_request(
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
) -> tuple[BrokerContext, ValidatedBrokerRequest]:
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
        args=_parse_args(definition=definition, args=args),
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
    if isinstance(request.args, BrowserToolCall):
        return context, _validated_browser_request(
            context=context,
            call=request.args,
            tool_invocation_id=effective_invocation_id,
            tool_request_id=effective_tool_request_id,
            requested_at=effective_requested_at,
            preflight_only=preflight_only,
        )
    args_request = _ARGS_REQUESTS.get(definition.execution_path)
    if args_request is not None:
        # The model refuses arguments parsed for another tool.
        return context, args_request.model_validate(
            {
                "tool_invocation_id": effective_invocation_id,
                "manifest_id": request.manifest_id,
                "execution_session_id": request.execution_session_id,
                "action_id": context.execution_session.action_id or "unknown",
                "tool_request_id": effective_tool_request_id,
                "requested_at": effective_requested_at,
                "args": request.args,
            }
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
    if definition.execution_path == "broker_direct_patch":
        assert isinstance(request.args, ApplyPatchToolArgs)
        return context, build_validated_patch_request(
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


def _validated_browser_request(
    *,
    context: BrokerContext,
    call: BrowserToolCall,
    tool_invocation_id: str | None,
    tool_request_id: str,
    requested_at: str,
    preflight_only: bool,
) -> ValidatedBrokerRequest:
    refuse_browser_network_use(context=context, call=call)
    args = call.call.root
    user_browser = None
    if call.browser == "user" and not isinstance(args, BrowserCloseArgs):
        refuse_printing(call.call)
        if isinstance(args, BrowserNavigateArgs):
            return build_validated_user_browser_read_request(
                context=context,
                args=args,
                tool_invocation_id=tool_invocation_id,
                tool_request_id=tool_request_id,
                requested_at=requested_at,
                preflight_only=preflight_only,
            )
        user_browser = user_browser_for_run(context.actor_process_id)
    if isinstance(args, BrowserPrintArgs):
        return build_validated_browser_print_request(
            context=context,
            args=args,
            tool_invocation_id=tool_invocation_id,
            tool_request_id=tool_request_id,
            requested_at=requested_at,
            preflight_only=preflight_only,
        )
    if isinstance(args, BrowserAllowWritesArgs):
        return build_validated_browser_write_request(
            context=context,
            args=args,
            user_browser=user_browser,
            tool_invocation_id=tool_invocation_id,
            tool_request_id=tool_request_id,
            requested_at=requested_at,
            preflight_only=preflight_only,
        )
    return ValidatedBrowserRequest(
        tool_invocation_id=tool_invocation_id,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session.execution_session_id,
        action_id=context.execution_session.action_id or "unknown",
        tool_request_id=tool_request_id,
        requested_at=requested_at,
        browser=call.browser,
        user_browser=user_browser,
        args=call.call,
    )


def _parse_args(
    *, definition: BrokerManagedToolDefinition, args: dict[str, JSONValue]
) -> BrokerToolArgs:
    model = BROKER_MODEL_REGISTRY.input_models[definition.input_model_ref]
    try:
        return cast(BrokerToolArgs, model.model_validate(args))
    except ValidationError as exc:
        raise BrokerPolicyError(
            f"{BROKER_TOOL_ARGS_INVALID}: invalid {definition.tool_id} arguments: "
            f"{exc}",
            code=BROKER_TOOL_ARGS_INVALID,
        ) from exc


__all__ = ["BROKER_TOOL_ARGS_INVALID", "validate_broker_request"]
