"""The conversation task tools: schedule, list and stop this conversation's tasks.

A schedule is consent-gated like ``capture_screen``: the preflight checks the
schedule and asks the user every time, never the approval review, and the call
spends that approval and writes exactly the task the user saw, approved.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from pantaray_agents.agents.action_agent.runtime.handlers.tool_args import (
    ToolArgs,
    optional_string_arg,
    require_string_arg,
)
from pantaray_agents.agents.action_agent.tools import ToolDefinition
from pantaray_agents.agents.action_agent.tools.conversation_task_tools import (
    SCHEDULE_CONTINUATION_TOOL_ID,
    STOP_SCHEDULED_CONTINUATION_TOOL_ID,
)
from pantaray_agents.local_runtime.hooks.agent_tasks import (
    TaskSchedule,
    is_conversation_task,
    list_conversation_tasks,
    new_task_id,
    parse_task,
    save_task,
    stop_task,
    task_text,
)
from pantaray_agents.local_runtime.hooks.tasks import InvalidTask, Task
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
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.schema.tool_result import build_runtime_tool_error_output
from pantaray_agents.tools.contract import BrokerPolicyError
from pantaray_agents.utils.local_time import describe_local_time, local_zone_name
from pantaray_agents.utils.trace_context import get_trace_context

from .approval_preparation import (
    build_approval_denied_preparation,
    build_approval_required_preparation,
)
from .shared import (
    CompletedToolControl,
    FailedToolControl,
    ToolExecutionPreparation,
    UnprojectedToolExecutionResult,
    tool_failure_identity_from_exception,
)

INVALID_SCHEDULE = "invalid_schedule"
TASK_NOT_FOUND = "task_not_found"
APPROVAL_STATE_CHANGED = "approval_state_changed"
_SUMMARY_KIND = "conversation_task"
_NOT_FOUND_MESSAGE = (
    "This conversation has no schedule with that task_id; "
    "list_scheduled_continuations names its schedules."
)


class ConversationTaskIdentityError(RuntimeError):
    """The Action state does not identify who asks for a schedule."""


class ScheduleError(ValueError):
    """The requested schedule cannot be written; the message says why."""


@dataclass(frozen=True, slots=True)
class _Identity:
    action_id: str
    execution_session_id: str
    manifest_id: str
    process_id: str
    user_id: str


@dataclass(frozen=True, slots=True)
class _Request:
    """The schedule a call asks for, resolved from its arguments."""

    task: Task
    schedule: TaskSchedule
    prompt: str
    # None for a new schedule.
    replaces: str | None


async def run_schedule_continuation_preflight(
    *,
    step_id: str,
    tool_def: ToolDefinition,
    state,
    args: ToolArgs,
    tool_request_id: str,
    requested_at: str,
) -> ToolExecutionPreparation:
    """Check the schedule, then ask the user, before anything is written."""

    try:
        identity = _resolve_identity(state)
        request = _resolve_request(args, tool_request_id=tool_request_id)
        _check_times(request.schedule, now=datetime.now(UTC))
        _check_replaced(identity, request)
    except (ConversationTaskIdentityError, ScheduleError) as exc:
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


async def run_schedule_continuation_tool(
    *,
    step_id: str,
    tool_def: ToolDefinition,
    state,
    args: ToolArgs,
    tool_request_id: str,
    tool_invocation_id: str,
    requested_at: str,
) -> UnprojectedToolExecutionResult:
    """Spend the user's yes, then write the task it approved."""

    identity = _resolve_identity(state)
    # The preflight checked the same arguments before the user was asked.
    request = _resolve_request(args, tool_request_id=tool_request_id)
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
                f"Approval for this schedule changed before it was written "
                f"({type(exc).__name__}), so nothing was scheduled. Request it again."
            ),
        )
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    now = datetime.now(UTC)
    refusal = save_task(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        user_id=identity.user_id,
        action_id=identity.action_id,
        change="create" if request.replaces is None else "update",
        task=request.task,
        now=now,
    )
    if refusal is not None:
        return _error_result(
            step_id,
            tool_def,
            requested_at,
            error_type=TASK_NOT_FOUND,
            message=(
                _NOT_FOUND_MESSAGE
                if refusal == "not_found"
                else "This schedule was already written."
            ),
        )
    (view,) = (
        view
        for view in list_conversation_tasks(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            user_id=identity.user_id,
            action_id=identity.action_id,
        )
        if view.hook_id == request.task.hook_id
    )
    next_run_at = _describe(view.next_fire_at)
    return _success(
        step_id,
        tool_def,
        requested_at,
        {
            "status": "scheduled",
            "task_id": request.task.hook_id,
            "next_run_at": next_run_at,
            "message": (
                "The user approved it. Its first turn is due "
                f"{next_run_at or 'never: it ends before then'}."
            ),
        },
    )


def run_conversation_task_tool(
    *, step_id: str, tool_def: ToolDefinition, args: ToolArgs, state
) -> UnprojectedToolExecutionResult:
    """list_scheduled_continuations and stop_scheduled_continuation."""

    user_id, action_id = str(state["user_id"]), str(state["action_id"])
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    started_at = now_utc_iso()
    if tool_def.tool_id == STOP_SCHEDULED_CONTINUATION_TOOL_ID:
        task_id = require_string_arg(args, "task_id")
        if (
            stop_task(
                db_path=db_path,
                busy_timeout_ms=busy_timeout_ms,
                user_id=user_id,
                action_id=action_id,
                hook_id=task_id,
            )
            == "not_found"
        ):
            return _error_result(
                step_id,
                tool_def,
                started_at,
                error_type=TASK_NOT_FOUND,
                message=_NOT_FOUND_MESSAGE,
            )
        return _success(
            step_id, tool_def, started_at, {"status": "stopped", "task_id": task_id}
        )
    tasks: list[JSONValue] = [
        {
            "task_id": view.hook_id,
            "text": view.text,
            "next_run_at": _describe(view.next_fire_at),
            "approved": view.approved,
            "enabled": view.enabled,
            "error": view.error,
            "last_result": view.last_result,
            "failure_count": view.failure_count,
            "stopped_reason": view.stopped_reason,
        }
        for view in list_conversation_tasks(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            user_id=user_id,
            action_id=action_id,
        )
    ]
    return _success(step_id, tool_def, started_at, {"tasks": tasks})


def _resolve_request(args: ToolArgs, *, tool_request_id: str) -> _Request:
    at = _instant(args, "at")
    every = optional_string_arg(args, "every")
    offset = optional_string_arg(args, "offset")
    if (at is None) == (every is None):
        raise ScheduleError("Give exactly one of at and every.")
    if offset is not None and (every is None or every[0].isdigit()):
        raise ScheduleError("offset goes only with a calendar unit in every.")
    schedule = TaskSchedule(
        at=at, every=every, offset=offset, until=_instant(args, "until")
    )
    prompt = require_string_arg(args, "prompt")
    replaces = optional_string_arg(args, "task_id")
    task = parse_task(
        replaces or new_task_id(tool_request_id), task_text(schedule, prompt)
    )
    if isinstance(task, InvalidTask):
        raise ScheduleError(task.error)
    return _Request(task=task, schedule=schedule, prompt=prompt, replaces=replaces)


def _check_times(schedule: TaskSchedule, *, now: datetime) -> None:
    if schedule.at is not None and schedule.at <= now:
        raise ScheduleError("at is not in the future.")
    if schedule.until is not None and schedule.until <= now:
        raise ScheduleError("until is not in the future.")


def _check_replaced(identity: _Identity, request: _Request) -> None:
    if request.replaces is None:
        return
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    if not is_conversation_task(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        user_id=identity.user_id,
        action_id=identity.action_id,
        hook_id=request.replaces,
    ):
        raise ScheduleError(_NOT_FOUND_MESSAGE)


def _instant(args: ToolArgs, key: str) -> datetime | None:
    value = optional_string_arg(args, key)
    if value is None:
        return None
    try:
        instant = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ScheduleError(f"{key} is not an ISO 8601 date and time.") from exc
    if instant.utcoffset() is None:
        raise ScheduleError(f"{key} needs its offset, such as +09:00.")
    return instant


def _summary(request: _Request) -> dict[str, JSONValue]:
    """What the user approves: the schedule and the prompt, which name the
    whole task file; the approval binds to it."""

    schedule = request.schedule
    return {
        "summary_kind": _SUMMARY_KIND,
        "task_id": request.task.hook_id,
        "replaces": request.replaces is not None,
        "at": None if schedule.at is None else schedule.at.isoformat(),
        "every": schedule.every,
        "offset": schedule.offset,
        "until": None if schedule.until is None else schedule.until.isoformat(),
        "prompt": request.prompt.strip(),
    }


def _authorize(
    identity: _Identity, request: _Request, tool_request_id: str, requested_at: str
):
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    return ensure_tool_authorization(
        context=load_broker_context(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            tool_id=SCHEDULE_CONTINUATION_TOOL_ID,
            path_access_kind="none",
            user_id=identity.user_id,
            actor_process_id=identity.process_id,
            manifest_id=identity.manifest_id,
            execution_session_id=identity.execution_session_id,
        ),
        tool_invocation_id=None,
        tool_request_id=tool_request_id,
        command_summary=_summary(request),
        requested_at=requested_at,
        # A schedule sends turns with no one watching: the user says yes to
        # each one, and the approval review never answers for them.
        require_user_prompt=True,
    )


def _claim(
    identity: _Identity,
    request: _Request,
    tool_request_id: str,
    tool_invocation_id: str,
    requested_at: str,
) -> None:
    """Spend the user's approval so that one yes writes one schedule."""

    session_id, source = _authorize(identity, request, tool_request_id, requested_at)
    if source is None:
        raise BrokerPolicyError("a schedule was authorized without a prompt")
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
            raise ConversationTaskIdentityError(f"{name} is required for a schedule")
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
            error_type=INVALID_SCHEDULE
            if isinstance(error, ScheduleError)
            else error.__class__.__name__,
            message=str(error),
        ),
        control=FailedToolControl(failure=tool_failure_identity_from_exception(error)),
    )


__all__ = [
    "run_conversation_task_tool",
    "run_schedule_continuation_preflight",
    "run_schedule_continuation_tool",
]
