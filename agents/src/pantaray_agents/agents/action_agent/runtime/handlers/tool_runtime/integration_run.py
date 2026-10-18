"""integration_run_tool at call time: resolve, settle consent, execute, wrap.

Nothing from the Action's start is trusted: the slug, the account and its
permission are resolved again on every call, and the arguments are validated
against the tool's input_schema before the user is asked. A write is approved
through the same preflight and claim as ``capture_screen``, so one answer runs
exactly one write.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from functools import partial
from typing import Literal

from jsonschema import Draft7Validator, SchemaError, ValidationError
from referencing import Registry
from referencing.exceptions import Unresolvable

from pantaray_agents.agents.action_agent.runtime.handlers.tool_args import (
    ToolArgs,
    json_object_copy,
    optional_string_arg,
    require_string_arg,
)
from pantaray_agents.agents.action_agent.tools import (
    INTEGRATION_RUN_TOOL_TOOL_ID,
    ToolDefinition,
)
from pantaray_agents.local_runtime.integrations.contracts import (
    IntegrationAccount,
    IntegrationError,
    IntegrationErrorKind,
    IntegrationGateway,
    IntegrationTool,
    ToolFailed,
    ToolOutcome,
)
from pantaray_agents.local_runtime.integrations.registry import (
    get_integration_gateway,
    get_integration_tool_index,
)
from pantaray_agents.local_runtime.runtime.bootstrap import read_local_runtime_db_config
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.local_runtime.storage.migrations import MigrationError
from pantaray_agents.local_runtime.tooling.brokering.broker_common import (
    BrokerApprovalDeniedError,
    BrokerApprovalRequiredError,
    ensure_tool_authorization,
    load_broker_context,
)
from pantaray_agents.local_runtime.tooling.brokering.execution_start import (
    claim_approved_execution_start,
)
from pantaray_agents.local_runtime.tooling.repository.integration_permissions import (
    integration_tool_scope,
    load_integration_account_access,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.schema.tool_result import build_runtime_tool_error_output
from pantaray_agents.tools.contract import BrokerPolicyError
from pantaray_agents.utils.trace_context import get_trace_context

from .approval_preparation import (
    build_approval_denied_preparation,
    build_approval_required_preparation,
)
from .integration_search import (
    UPSTREAM_FAILED_MESSAGE,
    IntegrationGatewayError,
    gateway_await,
    gateway_call,
    usable_accounts,
)
from .shared import (
    CompletedToolControl,
    FailedToolControl,
    ToolExecutionPreparation,
    UnprojectedToolExecutionResult,
    tool_failure_identity_from_exception,
)

logger = logging.getLogger(__name__)

APPROVAL_STATE_CHANGED = "approval_state_changed"

# Fixed text per kind: a connection method's own error text never reaches the model.
_MESSAGES: dict[IntegrationErrorKind, str] = {
    "connection_missing": "The account is not connected; the user can connect it in Settings.",
    "reauthorization_required": "The account must be signed in again in Settings.",
    "scope_missing": "The connection lacks a permission this tool needs.",
    "invalid_arguments": "The service rejected the arguments.",
    "rate_limited": "The service is limiting requests; wait before trying again.",
    "usage_limit_reached": "The connection's monthly usage limit is used up; do not retry.",
    "upstream_failed": UPSTREAM_FAILED_MESSAGE,
    "outcome_unknown": "The write may or may not have run. Do not run it again; ask the user to check.",
    "permission_denied": "The current permission does not allow this call.",
    "account_required": "Several accounts can run this tool; pass account as one of the candidates.",
}


class IntegrationIdentityError(RuntimeError):
    """The Action state does not identify who asks for an integration write."""


@dataclass(frozen=True, slots=True)
class _Refusal:
    error: IntegrationError
    message: str
    # The validator's text quotes the external schema and the model's arguments.
    untrusted_detail: str | None = None


@dataclass(frozen=True, slots=True)
class _Resolved:
    gateway: IntegrationGateway
    tool: IntegrationTool
    toolkit_name: str
    account: IntegrationAccount
    arguments: dict[str, JSONValue]


@dataclass(frozen=True, slots=True)
class _Identity:
    action_id: str
    execution_session_id: str
    manifest_id: str
    process_id: str
    user_id: str


async def run_integration_run_preflight(
    *,
    step_id: str,
    tool_def: ToolDefinition,
    state,
    args: ToolArgs,
    tool_request_id: str,
    requested_at: str,
) -> ToolExecutionPreparation:
    """Settle consent before a write. The call itself reports any refusal."""

    try:
        identity = _resolve_identity(state)
    except IntegrationIdentityError as exc:
        return _failed_preparation(step_id, tool_def, requested_at, error=exc)
    resolved = await _resolve(user_id=identity.user_id, args=args)
    if isinstance(resolved, _Resolved) and resolved.tool.tool_class == "write":
        try:
            _authorize_write(identity, resolved, tool_request_id, requested_at)
        except BrokerApprovalRequiredError as exc:
            db_path, busy_timeout_ms = read_local_runtime_db_config()
            try:
                return build_approval_required_preparation(
                    db_path=db_path,
                    busy_timeout_ms=busy_timeout_ms,
                    step_id=step_id,
                    tool_def=tool_def,
                    tool_request_id=tool_request_id,
                    approval_session_id=exc.approval_session_id,
                    user_id=identity.user_id,
                    manifest_id=identity.manifest_id,
                    action_id=identity.action_id,
                )
            except BrokerPolicyError as missing:
                return _failed_preparation(
                    step_id, tool_def, requested_at, error=missing
                )
        except BrokerApprovalDeniedError as exc:
            return build_approval_denied_preparation(
                step_id=step_id,
                tool_def=tool_def,
                tool_request_id=tool_request_id,
                approval_session_id=exc.approval_session_id,
                requested_at=requested_at,
            )
        except (BrokerPolicyError, MigrationError) as exc:
            return _failed_preparation(step_id, tool_def, requested_at, error=exc)
    return ToolExecutionPreparation(
        result=UnprojectedToolExecutionResult(
            step_id=step_id,
            tool_id=tool_def.tool_id,
            status="success",
            started_at=requested_at,
            completed_at=requested_at,
            output={"status": "authorized"},
        ),
        control=CompletedToolControl(),
    )


async def run_integration_run_tool(
    *,
    step_id: str,
    tool_def: ToolDefinition,
    state,
    args: ToolArgs,
    tool_request_id: str,
    tool_invocation_id: str,
    requested_at: str,
) -> UnprojectedToolExecutionResult:
    identity = _resolve_identity(state)
    resolved = await _resolve(user_id=identity.user_id, args=args)
    if isinstance(resolved, _Refusal):
        return _refusal_result(step_id, tool_def, requested_at, resolved)
    is_write = resolved.tool.tool_class == "write"
    if is_write:
        try:
            _claim_write(
                identity, resolved, tool_request_id, tool_invocation_id, requested_at
            )
        except (
            BrokerApprovalRequiredError,
            BrokerApprovalDeniedError,
            BrokerPolicyError,
        ) as exc:
            # The approval moved underneath the call: withdrawn, already spent by
            # a replay, or the tool became a write after the preflight.
            message = (
                f"Approval for this write changed before it ran ({type(exc).__name__}),"
                " so nothing ran. Request it again."
            )
            return _error_result(
                step_id,
                tool_def,
                requested_at,
                error_type=APPROVAL_STATE_CHANGED,
                message=message,
            )
    try:
        outcome: ToolOutcome = await resolved.gateway.execute(
            slug=resolved.tool.slug,
            account=resolved.account.account,
            arguments=resolved.arguments,
            tool_request_id=tool_request_id,
        )
    except Exception as exc:  # noqa: BLE001
        # The gateway reports failures as outcomes. An exception still must not
        # hand its text to the model, and a write it interrupted may have run.
        logger.warning(
            "integration tool raised: slug=%s exception_type=%s",
            resolved.tool.slug,
            type(exc).__name__,
        )
        kind: IntegrationErrorKind = (
            "outcome_unknown" if is_write else "upstream_failed"
        )
        outcome = ToolFailed(error=IntegrationError(kind=kind))
    if isinstance(outcome, ToolFailed):
        refusal = _Refusal(error=outcome.error, message=_MESSAGES[outcome.error.kind])
        return _refusal_result(step_id, tool_def, requested_at, refusal)
    return UnprojectedToolExecutionResult(
        step_id=step_id,
        tool_id=tool_def.tool_id,
        status="success",
        started_at=requested_at,
        completed_at=now_utc_iso(),
        output={
            "source": resolved.tool.toolkit,
            "untrusted": True,
            "data": outcome.data,
        },
    )


async def _resolve(*, user_id: str, args: ToolArgs) -> _Resolved | _Refusal:
    try:
        return await _resolve_with_gateway(user_id=user_id, args=args)
    except IntegrationGatewayError:
        return _refuse("upstream_failed", _MESSAGES["upstream_failed"])


async def _resolve_with_gateway(
    *, user_id: str, args: ToolArgs
) -> _Resolved | _Refusal:
    slug = require_string_arg(args, "slug")
    requested = optional_string_arg(args, "account")
    gateway = get_integration_gateway()
    tool = get_integration_tool_index().get(slug)
    if gateway is None or tool is None:
        return _refuse(
            "invalid_arguments",
            f"No tool has the slug {slug!r}; find one with integration_search_tools.",
        )
    connected = await gateway_await(gateway.list_accounts(tool.toolkit))
    usable = [
        item.account for item in usable_accounts(gateway, user_id, tool, connected)
    ]
    if requested is None:
        if len(usable) >= 2:
            return _Refusal(
                error=IntegrationError(kind="account_required", candidates=usable),
                message=_MESSAGES["account_required"],
            )
        if not usable:
            return _refuse(
                "permission_denied",
                f"No connected {tool.toolkit} account may run {slug} now.",
            )
        account = usable[0]
    else:
        target = next((item for item in connected if item.account == requested), None)
        if target is None:
            return _refuse(
                "connection_missing",
                f"{requested!r} is not a connected {tool.toolkit} account.",
            )
        if target not in usable:
            return _refuse("permission_denied", _denial(gateway, user_id, tool, target))
        account = target
    try:
        parsed = json.loads(require_string_arg(args, "arguments"))
        if not isinstance(parsed, dict):
            return _refuse("invalid_arguments", "arguments must be one JSON object.")
        Draft7Validator.check_schema(tool.input_schema)
        # An empty registry resolves only refs inside the schema: the default one
        # would fetch a remote $ref, synchronously and without a timeout.
        Draft7Validator(tool.input_schema, registry=Registry()).validate(parsed)
    except (SchemaError, Unresolvable):
        # The schema is the connection's, and its error text may name a URL.
        return _refuse("upstream_failed", "The tool's input schema could not be used.")
    except json.JSONDecodeError as exc:
        return _refuse("invalid_arguments", "arguments is not valid JSON.", str(exc))
    except ValidationError as exc:
        return _refuse(
            "invalid_arguments",
            "arguments do not match the tool's input_schema.",
            "/".join(str(part) for part in exc.absolute_path) + f": {exc.message}"
            if exc.absolute_path
            else exc.message,
        )
    names = {
        entry.toolkit: entry.display_name for entry in gateway_call(gateway.toolkits)
    }
    return _Resolved(
        gateway=gateway,
        tool=tool,
        toolkit_name=names[tool.toolkit],
        account=account,
        arguments=json_object_copy(parsed),
    )


def _denial(
    gateway: IntegrationGateway,
    user_id: str,
    tool: IntegrationTool,
    account: IntegrationAccount,
) -> str:
    """Say why the current permission refuses, without the connection's own text."""

    if not gateway_call(gateway.limits).can_execute:
        why = "the connection method does not allow running tools now"
    elif (
        gateway_call(
            partial(gateway.api_denied_reason, slug=tool.slug, account=account.account)
        )
        is not None
    ):
        why = "the account's connection does not grant this tool"
    else:
        db_path, busy_timeout_ms = read_local_runtime_db_config()
        access = load_integration_account_access(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            user_id=user_id,
            toolkit=tool.toolkit,
            account=account.account,
        )
        if access == "off":
            why = "the account is turned off"
        elif access == "read_only" and tool.tool_class == "write":
            why = "the account allows read tools only"
        else:
            why = "this tool is blocked for the account"
    return (
        f"Cannot run {tool.slug} with {account.label}: {why}. The permission may "
        "have changed during this Action; do not retry."
    )


def _authorize_write(
    identity: _Identity, resolved: _Resolved, tool_request_id: str, requested_at: str
) -> tuple[str | None, Literal["settings", "prompt"] | None]:
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    return ensure_tool_authorization(
        context=load_broker_context(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            tool_id=INTEGRATION_RUN_TOOL_TOOL_ID,
            path_access_kind="none",
            user_id=identity.user_id,
            actor_process_id=identity.process_id,
            manifest_id=identity.manifest_id,
            execution_session_id=identity.execution_session_id,
        ),
        tool_invocation_id=None,
        tool_request_id=tool_request_id,
        # The approval binds to this summary: a request that resolves to another
        # toolkit, account, slug or arguments no longer matches it and is refused.
        # No tool has its own summary, so the user sees the whole request.
        command_summary={
            "summary_kind": "integration_tool",
            "account": resolved.account.label,
            "toolkit": resolved.toolkit_name,
            "tool": resolved.tool.name,
            "description": resolved.tool.description,
            "arguments": json.dumps(
                resolved.arguments, ensure_ascii=False, indent=2, sort_keys=True
            ),
            "toolkit_id": resolved.tool.toolkit,
            "account_id": resolved.account.account,
            "slug": resolved.tool.slug,
        },
        requested_at=requested_at,
        integration_tool_scope=integration_tool_scope(
            account=resolved.account.account, slug=resolved.tool.slug
        ),
    )


def _claim_write(
    identity: _Identity,
    resolved: _Resolved,
    tool_request_id: str,
    tool_invocation_id: str,
    requested_at: str,
) -> None:
    """Spend the user's approval so that one answer runs exactly one write."""

    session_id, source = _authorize_write(
        identity, resolved, tool_request_id, requested_at
    )
    if source is None:
        raise BrokerPolicyError("an integration write was authorized without a prompt")
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    claim_approved_execution_start(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        user_id=identity.user_id,
        tool_request_id=tool_request_id,
        action_id=identity.action_id,
        approval_session_id=session_id,
        approval_source=source,
        tool_invocation_id=tool_invocation_id,
        # The Action runtime records this tool's invocation row before execution.
        invocation=None,
        started_at=requested_at,
    )


def _resolve_identity(state) -> _Identity:
    trace = get_trace_context()
    values = {
        "action_id": state.get("action_id"),
        "execution_session_id": state.get("execution_session_id"),
        "manifest_id": state.get("manifest_id"),
        "process_id": trace.extra.get("process_id") if trace is not None else None,
        "user_id": state.get("user_id"),
    }
    for name, value in values.items():
        if not isinstance(value, str) or not value.strip():
            raise IntegrationIdentityError(f"{name} is required for integration tools")
    return _Identity(**{name: str(value) for name, value in values.items()})


def _refuse(
    kind: IntegrationErrorKind, message: str, detail: str | None = None
) -> _Refusal:
    return _Refusal(IntegrationError(kind=kind), message, untrusted_detail=detail)


def _refusal_result(
    step_id: str, tool_def: ToolDefinition, requested_at: str, refusal: _Refusal
) -> UnprojectedToolExecutionResult:
    error = refusal.error
    details: dict[str, JSONValue] = {}
    if error.candidates:
        details["candidates"] = [
            {"account": item.account, "label": item.label} for item in error.candidates
        ]
    if error.retry_after_seconds is not None:
        details["retry_after_seconds"] = error.retry_after_seconds
    if refusal.untrusted_detail is not None:
        details["detail"] = {"untrusted": True, "text": refusal.untrusted_detail}
    return _error_result(
        step_id,
        tool_def,
        requested_at,
        error_type=error.kind,
        message=refusal.message,
        details=details or None,
    )


def _error_result(
    step_id: str,
    tool_def: ToolDefinition,
    requested_at: str,
    *,
    error_type: str,
    message: str,
    details: dict[str, JSONValue] | None = None,
) -> UnprojectedToolExecutionResult:
    return UnprojectedToolExecutionResult(
        step_id=step_id,
        tool_id=tool_def.tool_id,
        status="error",
        started_at=requested_at,
        completed_at=now_utc_iso(),
        output=build_runtime_tool_error_output(
            error_type=error_type, message=message, details=details
        ),
    )


def _failed_preparation(
    step_id: str, tool_def: ToolDefinition, requested_at: str, *, error: Exception
) -> ToolExecutionPreparation:
    return ToolExecutionPreparation(
        result=_error_result(
            step_id,
            tool_def,
            requested_at,
            error_type=error.__class__.__name__,
            message=str(error),
        ),
        control=FailedToolControl(failure=tool_failure_identity_from_exception(error)),
    )
