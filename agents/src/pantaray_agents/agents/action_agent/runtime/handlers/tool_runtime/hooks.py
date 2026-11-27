"""The hook tools: create, list and delete this conversation's hooks.

A create is consent-gated like ``capture_screen``: the preflight checks the
hook and asks the user every time, never the approval review, and the call
spends that approval and saves exactly the hook the user saw, approved.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pantaray_agents.agents.action_agent.runtime.handlers.tool_args import (
    ToolArgs,
    require_string_arg,
)
from pantaray_agents.agents.action_agent.tools import ToolDefinition
from pantaray_agents.agents.action_agent.tools.hook_tools import (
    CREATE_HOOK_TOOL_ID,
    DELETE_HOOK_TOOL_ID,
)
from pantaray_agents.hooks.definitions import hook_has_ended
from pantaray_agents.local_runtime.activity_summary_schedule import iso_z
from pantaray_agents.local_runtime.hooks.agent_tasks import (
    TaskRefusal,
    conversation_task_state,
    delete_task,
    list_conversation_tasks,
    save_task,
)
from pantaray_agents.local_runtime.hooks.controls import NewConversation, TaskRow
from pantaray_agents.local_runtime.hooks.states import first_fire_after
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
from pantaray_agents.local_runtime.tooling.repository import list_workspace_settings
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.schema.tool_result import build_runtime_tool_error_output
from pantaray_agents.tools.contract import BrokerPolicyError
from pantaray_agents.utils.local_time import describe_local_time, local_zone_name
from pantaray_agents.utils.trace_context import get_trace_context

from .approval_preparation import (
    build_approval_denied_preparation,
    build_approval_required_preparation,
)
from .hook_requests import (
    HookRequest,
    HookRequestError,
    resolve_request,
    stored_trigger,
)
from .shared import (
    CompletedToolControl,
    FailedToolControl,
    ToolExecutionPreparation,
    UnprojectedToolExecutionResult,
    tool_failure_identity_from_exception,
)

INVALID_HOOK = "invalid_hook"
HOOK_NOT_FOUND = "hook_not_found"
APPROVAL_STATE_CHANGED = "approval_state_changed"
_NOT_FOUND_MESSAGE = (
    "This conversation has no hook with that hook_id; list_hooks names its hooks."
)


_SAVE_REFUSALS: dict[TaskRefusal, str] = {
    "not_found": _NOT_FOUND_MESSAGE,
    "exists": "This hook was already saved.",
    "ended": (
        "While the user was being asked, the time passed when the first run "
        "could still come before until, so it would never run. Nothing was "
        "saved; propose a later until or an earlier first run."
    ),
}


class HookIdentityError(RuntimeError):
    """The Action state does not identify who asks for a hook."""


@dataclass(frozen=True, slots=True)
class _Identity:
    action_id: str
    execution_session_id: str
    manifest_id: str
    process_id: str
    user_id: str


async def run_create_hook_preflight(
    *,
    step_id: str,
    tool_def: ToolDefinition,
    state,
    args: ToolArgs,
    tool_request_id: str,
    requested_at: str,
) -> ToolExecutionPreparation:
    """Check the hook, then ask the user, before anything is saved."""

    try:
        identity = _resolve_identity(state)
        request = _resolve(identity, args, tool_request_id)
        _check_times(request, now=datetime.now(UTC))
        if request.replaces is not None and _resumes(identity, request) is None:
            raise HookRequestError(_NOT_FOUND_MESSAGE)
    except (HookIdentityError, HookRequestError) as exc:
        return _failed_preparation(step_id, tool_def, requested_at, error=exc)
    try:
        _authorize(identity, request, tool_request_id, requested_at)
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
            return _failed_preparation(step_id, tool_def, requested_at, error=missing)
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


async def run_create_hook_tool(
    *,
    step_id: str,
    tool_def: ToolDefinition,
    state,
    args: ToolArgs,
    tool_request_id: str,
    tool_invocation_id: str,
    requested_at: str,
) -> UnprojectedToolExecutionResult:
    """Spend the user's yes, then save the hook it approved."""

    identity = _resolve_identity(state)
    try:
        # The preflight checked the same arguments before the user was asked;
        # only a place renamed or removed meanwhile fails here.
        request = _resolve(identity, args, tool_request_id)
    except HookRequestError as exc:
        return _error_result(
            step_id, tool_def, requested_at, error_type=INVALID_HOOK, message=str(exc)
        )
    try:
        _claim(identity, request, tool_request_id, tool_invocation_id, requested_at)
    except (
        BrokerApprovalRequiredError,
        BrokerApprovalDeniedError,
        BrokerPolicyError,
    ) as exc:
        return _error_result(
            step_id,
            tool_def,
            requested_at,
            error_type=APPROVAL_STATE_CHANGED,
            message=(
                f"Approval for this hook changed before it was saved "
                f"({type(exc).__name__}), so nothing was saved. Request it again."
            ),
        )
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    refusal = save_task(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        user_id=identity.user_id,
        action_id=identity.action_id,
        change="create" if request.replaces is None else "update",
        task=request.task,
        new_conversation=request.new_conversation,
        now=datetime.now(UTC),
    )
    if refusal is not None:
        return _error_result(
            step_id,
            tool_def,
            requested_at,
            error_type=INVALID_HOOK if refusal == "ended" else HOOK_NOT_FOUND,
            message=_SAVE_REFUSALS[refusal],
        )
    (row,) = (
        row
        for row in list_conversation_tasks(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            user_id=identity.user_id,
            action_id=identity.action_id,
        )
        if row.task.hook_id == request.task.hook_id
    )
    return _success(
        step_id,
        tool_def,
        requested_at,
        {
            "status": "created",
            "hook_id": request.task.hook_id,
            "next_run_at": _describe(row.next_fire_at),
        },
    )


def run_hook_tool(
    *, step_id: str, tool_def: ToolDefinition, args: ToolArgs, state
) -> UnprojectedToolExecutionResult:
    """list_hooks and delete_hook."""

    user_id, action_id = str(state["user_id"]), str(state["action_id"])
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    started_at = now_utc_iso()
    if tool_def.tool_id == DELETE_HOOK_TOOL_ID:
        hook_id = require_string_arg(args, "hook_id")
        if (
            delete_task(
                db_path=db_path,
                busy_timeout_ms=busy_timeout_ms,
                user_id=user_id,
                action_id=action_id,
                hook_id=hook_id,
            )
            == "not_found"
        ):
            return _error_result(
                step_id,
                tool_def,
                started_at,
                error_type=HOOK_NOT_FOUND,
                message=_NOT_FOUND_MESSAGE,
            )
        return _success(
            step_id, tool_def, started_at, {"status": "deleted", "hook_id": hook_id}
        )
    rows = list_conversation_tasks(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        user_id=user_id,
        action_id=action_id,
    )
    names = _place_names(db_path, busy_timeout_ms, user_id) if rows else {}
    now = datetime.now(UTC)
    hooks: list[JSONValue] = [_listed(row, names, now) for row in rows]
    return _success(step_id, tool_def, started_at, {"hooks": hooks})


def _listed(row: TaskRow, names: dict[str, str], now: datetime) -> JSONValue:
    """A hook as create_hook took it, with how its runs go."""

    new = row.target if isinstance(row.target, NewConversation) else None
    until = row.task.schedule.until
    place: JSONValue = None
    if new is not None and (
        new.organization_id is not None or new.project_id is not None
    ):
        place = {
            "organization": None
            if new.organization_id is None
            else names.get(new.organization_id),
            "project": None if new.project_id is None else names.get(new.project_id),
        }
    return {
        "hook_id": row.task.hook_id,
        "trigger": stored_trigger(row.task),
        "until": None if until is None else iso_z(until),
        "send_to": "this_session" if new is None else "new_session",
        "place": place,
        "title": None if new is None else new.title,
        "message": row.task.message,
        "next_run_at": None
        if hook_has_ended(row.hook, now)
        else _describe(row.next_fire_at),
        "enabled": row.enabled,
        "last_result": row.run.last_result,
        "failure_count": row.run.failure_count,
        "stopped_reason": row.run.stopped_reason,
    }


def _place_names(db_path: Path, busy_timeout_ms: int, user_id: str) -> dict[str, str]:
    """Each active organization's and project's name, by its id."""

    settings = list_workspace_settings(
        db_path=db_path, busy_timeout_ms=busy_timeout_ms, user_id=user_id
    )
    return {
        **{o.organization_id: o.display_name for o in settings.organizations},
        **{p.project_id: p.display_name for p in settings.projects},
    }


def _resolve(identity: _Identity, args: ToolArgs, tool_request_id: str) -> HookRequest:
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    return resolve_request(
        args,
        tool_request_id=tool_request_id,
        workspace=list_workspace_settings(
            db_path=db_path, busy_timeout_ms=busy_timeout_ms, user_id=identity.user_id
        ),
    )


def _check_times(request: HookRequest, *, now: datetime) -> None:
    schedule = request.task.schedule
    if schedule.at is not None and schedule.at <= now:
        raise HookRequestError("at is not in the future.")
    if schedule.until is None:
        return
    if schedule.until <= now:
        raise HookRequestError("until is not in the future.")
    # The first fire the approval would set: one at or after until never comes.
    first = first_fire_after(request.task.trigger, now)
    if first is not None and first >= schedule.until:
        raise HookRequestError(
            f"The first run, {_describe(first)}, is not before until, so it "
            "would never run. Move until later or the first run earlier."
        )


def _resumes(identity: _Identity, request: HookRequest) -> bool | None:
    """Whether approving the call switches a paused hook back on; None when
    the hook it replaces is not this conversation's."""

    if request.replaces is None:
        return False
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    state = conversation_task_state(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        user_id=identity.user_id,
        action_id=identity.action_id,
        hook_id=request.replaces,
    )
    return None if state is None else state == "paused"


def _authorize(
    identity: _Identity, request: HookRequest, tool_request_id: str, requested_at: str
):
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    return ensure_tool_authorization(
        context=load_broker_context(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            tool_id=CREATE_HOOK_TOOL_ID,
            path_access_kind="none",
            user_id=identity.user_id,
            actor_process_id=identity.process_id,
            manifest_id=identity.manifest_id,
            execution_session_id=identity.execution_session_id,
        ),
        tool_invocation_id=None,
        tool_request_id=tool_request_id,
        # A hook paused while the user was asked no longer matches, so the
        # call is asked again with the resume said.
        command_summary=request.summary(resumes=_resumes(identity, request) is True),
        requested_at=requested_at,
        # A hook sends turns with no one watching: the user says yes to each
        # one, and the approval review never answers for them.
        require_user_prompt=True,
    )


def _claim(
    identity: _Identity,
    request: HookRequest,
    tool_request_id: str,
    tool_invocation_id: str,
    requested_at: str,
) -> None:
    """Spend the user's approval so that one yes saves one hook."""

    session_id, source = _authorize(identity, request, tool_request_id, requested_at)
    if source is None:
        raise BrokerPolicyError("a hook was authorized without a prompt")
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


def _describe(instant: datetime | None) -> str | None:
    return None if instant is None else describe_local_time(instant, local_zone_name())


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
            raise HookIdentityError(f"{name} is required for a hook")
    return _Identity(**{name: str(value) for name, value in values.items()})


def _success(
    step_id: str,
    tool_def: ToolDefinition,
    started_at: str,
    output: dict[str, JSONValue],
) -> UnprojectedToolExecutionResult:
    return UnprojectedToolExecutionResult(
        step_id=step_id,
        tool_id=tool_def.tool_id,
        status="success",
        started_at=started_at,
        completed_at=now_utc_iso(),
        output=output,
    )


def _error_result(
    step_id: str,
    tool_def: ToolDefinition,
    started_at: str,
    *,
    error_type: str,
    message: str,
) -> UnprojectedToolExecutionResult:
    return UnprojectedToolExecutionResult(
        step_id=step_id,
        tool_id=tool_def.tool_id,
        status="error",
        started_at=started_at,
        completed_at=now_utc_iso(),
        output=build_runtime_tool_error_output(error_type=error_type, message=message),
    )


def _failed_preparation(
    step_id: str, tool_def: ToolDefinition, requested_at: str, *, error: Exception
) -> ToolExecutionPreparation:
    return ToolExecutionPreparation(
        result=_error_result(
            step_id,
            tool_def,
            requested_at,
            error_type=INVALID_HOOK
            if isinstance(error, HookRequestError)
            else error.__class__.__name__,
            message=str(error),
        ),
        control=FailedToolControl(failure=tool_failure_identity_from_exception(error)),
    )


__all__ = [
    "run_create_hook_preflight",
    "run_create_hook_tool",
    "run_hook_tool",
]
