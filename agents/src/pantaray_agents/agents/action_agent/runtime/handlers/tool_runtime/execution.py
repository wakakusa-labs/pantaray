from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from typing import Never

from pantaray_agents.agents.action_agent.runtime.config import (
    require_positive_state_config_int,
)
from pantaray_agents.agents.action_agent.runtime.handlers.tool_args import ToolArgs
from pantaray_agents.agents.action_agent.services.token_accounting_service import (
    StateTokenSink,
)
from pantaray_agents.agents.action_agent.tools import (
    CANCEL_SUBAGENT_TOOL_ID,
    CAPTURE_SCREEN_TOOL_ID,
    DRAFT_FINAL_ANSWER_TOOL_ID,
    READ_ACTION_PLAN_TOOL_ID,
    REMEMBER_TOOL_ID,
    SEND_MESSAGE_TO_SUBAGENT_TOOL_ID,
    SPAWN_SUBAGENT_TOOL_ID,
    SUBMIT_FINAL_ANSWER_TOOL_ID,
    WAIT_SUBAGENTS_TOOL_ID,
    WRITE_ACTION_PLAN_TOOL_ID,
    ZANEI_QUERY_TOOL_ID,
    ZANEI_TIMELINE_TOOL_ID,
    ToolDefinition,
)
from pantaray_agents.agents.core import TokenBudgetExceeded
from pantaray_agents.application.action.ports import (
    ActionStepEventPersistenceError,
    ActionToolStepEmission,
)
from pantaray_agents.local_runtime.runtime.utc_timestamps import (
    format_utc_iso,
    now_utc_iso,
)
from pantaray_agents.local_runtime.tooling.brokering.broker_common import (
    GLOB_TOOL_ID,
    GREP_TOOL_ID,
    LIST_TOOL_ID,
    READ_TOOL_ID,
    RENDER_PDF_PAGE_TOOL_ID,
)
from pantaray_agents.local_runtime.tooling.invocation_audit import BROKERED_TOOL_IDS
from pantaray_agents.local_runtime.tooling.tool_result_validation import (
    ToolOutputValidationError,
)
from pantaray_agents.schema.action_tool_call import ActionToolCallOrigin
from pantaray_agents.utils.metrics import (
    record_tool_execution,
)

from .broker_tools import run_broker_tool_wrapper
from .capture_screen import run_capture_screen_preflight, run_capture_screen_tool
from .draft_final_answer import run_draft_final_answer_tool
from .external_tools import (
    run_history_fetch_wrapper,
    run_web_crawl_wrapper,
    run_web_extract_wrapper,
    run_web_search_wrapper,
)
from .finalization_boundary import (
    build_validation_error_output,
    finalize_action_step_preparation,
    finalize_action_tool_invocation,
    start_action_tool_invocation,
)
from .memory_links import (
    run_get_memory_reference_tool,
    run_link_memory_tool,
    run_unlink_memory_tool,
)
from .memory_search import run_memory_search_tool
from .memory_sql import run_memory_sql_tool
from .plan_document import run_action_plan_tool
from .remember import run_remember_tool
from .request_identity import resolve_tool_request_id
from .shared import (
    ApprovalDeniedToolControl,
    ApprovalRequiredToolControl,
    FailedToolControl,
    FinalizedToolExecutionError,
    ToolCompletionAuditPersistenceError,
    ToolExecutionActor,
    ToolExecutionPreparation,
    ToolExecutionResult,
    ToolRuntimeContext,
    ToolValidationError,
    UnprojectedToolExecutionResult,
    ValidatedToolArgs,
    build_tool_execution_control,
    build_tool_execution_result,
)
from .subagent_cancel import run_cancel_subagent_tool
from .subagent_message import run_send_message_to_subagent_tool
from .subagent_spawn import run_spawn_subagent_tool
from .subagent_wait import run_wait_subagents_tool
from .submit_final_answer import run_submit_final_answer_tool
from .thinking import run_thinking_tool
from .validation import validate_tool_args
from .zanei import run_zanei_query_tool, run_zanei_timeline_tool

logger = logging.getLogger(__name__)
NON_PREFLIGHT_BROKERED_TOOL_IDS = frozenset(
    {READ_TOOL_ID, RENDER_PDF_PAGE_TOOL_ID, LIST_TOOL_ID, GLOB_TOOL_ID, GREP_TOOL_ID}
)
PREFLIGHT_APPROVAL_BROKERED_TOOL_IDS = (
    BROKERED_TOOL_IDS - NON_PREFLIGHT_BROKERED_TOOL_IDS
)
# capture_screen is not brokered - it touches no workspace path - but it needs the
# same "settle consent, then run" preflight, so it shares that identity and pause
# machinery instead of growing a second approval path.
PREFLIGHT_APPROVAL_TOOL_IDS = PREFLIGHT_APPROVAL_BROKERED_TOOL_IDS | {
    CAPTURE_SCREEN_TOOL_ID
}


def _build_tool_runtime_context(*, runtime) -> ToolRuntimeContext:
    raw_parallel = require_positive_state_config_int(
        runtime.state_config,
        key="max_parallel_memory_queries",
        error_message=(
            "tool execution requires state_config.max_parallel_memory_queries "
            "to be a positive int"
        ),
    )
    return ToolRuntimeContext(max_parallel_memory_queries=raw_parallel)


def _requires_preflight_approval(tool_id: str) -> bool:
    return tool_id in PREFLIGHT_APPROVAL_TOOL_IDS


def _preflight_blocks_execution(result: ToolExecutionPreparation) -> bool:
    return isinstance(
        result.control,
        (ApprovalRequiredToolControl, ApprovalDeniedToolControl, FailedToolControl),
    )


def _uses_action_runtime_audit(tool_id: str) -> bool:
    return tool_id not in BROKERED_TOOL_IDS


def _raise_finalized_output_error(
    error: ToolOutputValidationError,
) -> Never:
    if error.tool_invocation_id is None or error.finalized_output is None:
        raise error
    raise FinalizedToolExecutionError(
        cause=error,
        finalized_output=error.finalized_output,
        tool_invocation_id=error.tool_invocation_id,
    ) from error


async def run_validated_tool_impl(
    agent,
    tool_def: ToolDefinition,
    args: ValidatedToolArgs,
    state,
    *,
    sink: StateTokenSink,
    runtime,
    actor: ToolExecutionActor = "supervisor",
    step_id: str | None = None,
    started_at: datetime | None = None,
    origin: ActionToolCallOrigin | None = None,
) -> UnprojectedToolExecutionResult:
    effective_started_at = started_at or datetime.now(UTC)
    resolved_step_id = step_id or str(uuid.uuid4())

    if tool_def.tool_id == DRAFT_FINAL_ANSWER_TOOL_ID:
        result = await run_draft_final_answer_tool(
            agent, resolved_step_id, tool_def, args, state, actor=actor
        )
    elif tool_def.tool_id == SUBMIT_FINAL_ANSWER_TOOL_ID:
        result = await run_submit_final_answer_tool(
            agent, resolved_step_id, tool_def, args, state, actor=actor
        )
    elif tool_def.tool_id in {
        READ_ACTION_PLAN_TOOL_ID,
        WRITE_ACTION_PLAN_TOOL_ID,
    }:
        result = await run_action_plan_tool(
            step_id=resolved_step_id,
            tool_def=tool_def,
            args=args,
            state=state,
            actor=actor,
        )
    elif tool_def.tool_id == SPAWN_SUBAGENT_TOOL_ID:
        result = await run_spawn_subagent_tool(
            agent,
            step_id=resolved_step_id,
            tool_def=tool_def,
            args=args,
            state=state,
            actor=actor,
            origin=origin,
        )
    elif tool_def.tool_id == SEND_MESSAGE_TO_SUBAGENT_TOOL_ID:
        result = await run_send_message_to_subagent_tool(
            step_id=resolved_step_id,
            tool_def=tool_def,
            args=args,
            state=state,
            actor=actor,
        )
    elif tool_def.tool_id == WAIT_SUBAGENTS_TOOL_ID:
        result = await run_wait_subagents_tool(
            step_id=resolved_step_id,
            tool_def=tool_def,
            args=args,
            state=state,
            actor=actor,
        )
    elif tool_def.tool_id == CANCEL_SUBAGENT_TOOL_ID:
        result = await run_cancel_subagent_tool(
            step_id=resolved_step_id,
            tool_def=tool_def,
            args=args,
            state=state,
            actor=actor,
        )
    elif tool_def.tool_id == "thinking":
        result = await run_thinking_tool(
            agent,
            resolved_step_id,
            tool_def,
            args,
            state,
            sink=sink,
        )
    elif tool_def.tool_id == "memory_search":
        result = await run_memory_search_tool(
            agent,
            resolved_step_id,
            tool_def,
            args,
            state,
            tool_runtime_context=_build_tool_runtime_context(runtime=runtime),
        )
    elif tool_def.tool_id == "link_memory":
        result = await run_link_memory_tool(
            step_id=resolved_step_id,
            tool_def=tool_def,
            args=args,
            state=state,
            actor=actor,
        )
    elif tool_def.tool_id == "unlink_memory":
        result = await run_unlink_memory_tool(
            step_id=resolved_step_id,
            tool_def=tool_def,
            args=args,
            state=state,
            actor=actor,
        )
    elif tool_def.tool_id == "get_memory_reference":
        result = await run_get_memory_reference_tool(
            step_id=resolved_step_id,
            tool_def=tool_def,
            args=args,
            state=state,
        )
    elif tool_def.tool_id == REMEMBER_TOOL_ID:
        result = await run_remember_tool(
            step_id=resolved_step_id,
            tool_def=tool_def,
            args=args,
            state=state,
            actor=actor,
        )
    elif tool_def.tool_id == "memory_sql":
        result = await run_memory_sql_tool(
            agent, resolved_step_id, tool_def, args, state
        )
    elif tool_def.tool_id == "web_search":
        result = await run_web_search_wrapper(
            agent, resolved_step_id, tool_def, args, state
        )
    elif tool_def.tool_id == "web_extract":
        result = await run_web_extract_wrapper(
            agent, resolved_step_id, tool_def, args, state
        )
    elif tool_def.tool_id == "web_crawl":
        result = await run_web_crawl_wrapper(
            agent, resolved_step_id, tool_def, args, state
        )
    elif tool_def.tool_id == ZANEI_TIMELINE_TOOL_ID:
        result = await run_zanei_timeline_tool(
            step_id=resolved_step_id,
            tool_def=tool_def,
            args=args,
            state=state,
            runtime=runtime,
        )
    elif tool_def.tool_id == ZANEI_QUERY_TOOL_ID:
        result = await run_zanei_query_tool(
            step_id=resolved_step_id,
            tool_def=tool_def,
            args=args,
            state=state,
            runtime=runtime,
        )
    elif tool_def.tool_id == "history_fetch":
        result = await run_history_fetch_wrapper(
            agent, resolved_step_id, tool_def, args, state
        )
    elif tool_def.tool_id in BROKERED_TOOL_IDS or tool_def.tool_id == (
        CAPTURE_SCREEN_TOOL_ID
    ):
        raise RuntimeError("approval-gated tools must be dispatched from run_tool")
    else:
        raise ValueError(f"Unsupported tool_id: {tool_def.tool_id}")

    result.started_at = format_utc_iso(effective_started_at)
    try:
        completion_dt = datetime.fromisoformat(result.completed_at)
    except ValueError:
        completion_dt = datetime.now(UTC)
        result.completed_at = format_utc_iso(completion_dt)
    elapsed_ms = int((completion_dt - effective_started_at).total_seconds() * 1000)
    result.execution_time_ms = max(elapsed_ms, 0)

    record_tool_execution(
        tool_id=tool_def.tool_id,
        status="success" if result.status == "success" else "error",
        duration_seconds=elapsed_ms / 1000.0,
        prompt_tokens=result.prompt_tokens or 0,
        completion_tokens=result.completion_tokens or 0,
    )
    return result


async def run_tool(
    agent,
    tool_def: ToolDefinition,
    args: ToolArgs,
    state,
    *,
    sink: StateTokenSink,
    runtime,
    step_number: int,
    actor: ToolExecutionActor,
    step_id: str | None = None,
    origin: ActionToolCallOrigin | None = None,
) -> ToolExecutionResult:
    started_at = datetime.now(UTC)
    step_id = step_id or str(uuid.uuid4())
    raw_args: ValidatedToolArgs = dict(args)
    action_id = state.get("action_id")
    if not isinstance(action_id, str) or not action_id:
        raise RuntimeError("tool execution requires action_id")
    try:
        validated_args: ValidatedToolArgs = dict(raw_args)
        validate_tool_args(tool_def, validated_args)
    except ToolValidationError as exc:
        error_payload = build_validation_error_output(exc)
        validation_request_id = step_id
        validation_invocation_id = start_action_tool_invocation(
            state=state,
            tool_def=tool_def,
            step_id=step_id,
            started_at=format_utc_iso(started_at),
            args=raw_args,
            tool_request_id=validation_request_id,
        )
        finalized_validation_result = finalize_action_tool_invocation(
            tool_id=tool_def.tool_id,
            invocation_id=validation_invocation_id,
            result=UnprojectedToolExecutionResult(
                step_id=step_id,
                tool_id=tool_def.tool_id,
                status="error",
                started_at=format_utc_iso(started_at),
                completed_at=now_utc_iso(),
                output=error_payload,
                tool_request_id=validation_request_id,
                tool_invocation_id=validation_invocation_id,
            ),
        )
        assert finalized_validation_result is not None
        exc.finalized_output = finalized_validation_result
        exc.tool_invocation_id = validation_invocation_id
        raise
    tool_request_id = (
        resolve_tool_request_id(state=state, step_id=step_id)
        if tool_def.tool_id in PREFLIGHT_APPROVAL_TOOL_IDS
        else step_id
    )
    invocation_id: str | None = None
    if _requires_preflight_approval(tool_def.tool_id):
        preflight_result = (
            await run_capture_screen_preflight(
                step_id=step_id,
                tool_def=tool_def,
                state=state,
                # The input schema requires a non-blank string.
                app_name=str(validated_args["app_name"]),
                tool_request_id=tool_request_id,
                requested_at=format_utc_iso(started_at),
            )
            if tool_def.tool_id == CAPTURE_SCREEN_TOOL_ID
            else await run_broker_tool_wrapper(
                agent,
                step_id,
                tool_def,
                validated_args,
                state,
                invocation_id=None,
                tool_request_id=tool_request_id,
                requested_at=format_utc_iso(started_at),
                preflight_only=True,
            )
        )
        if _preflight_blocks_execution(preflight_result):
            finalized_preflight = finalize_action_step_preparation(
                state=state,
                preparation=preflight_result,
            )
            finalized_preflight.tool_request_id = tool_request_id
            return finalized_preflight
    invocation_id = None
    if _uses_action_runtime_audit(tool_def.tool_id):
        invocation_id = start_action_tool_invocation(
            state=state,
            tool_def=tool_def,
            step_id=step_id,
            started_at=format_utc_iso(started_at),
            args=validated_args,
            tool_request_id=tool_request_id,
        )
    try:
        if actor == "supervisor":
            await runtime.emit_action_step(
                ActionToolStepEmission(
                    step_id=step_id,
                    step_number=step_number,
                    step_name=f"tool::{tool_def.tool_id}",
                    label=tool_def.name,
                    tool_args={"args": validated_args},
                    status="processing",
                    started_at=format_utc_iso(started_at),
                    completed_at=None,
                )
            )
        if tool_def.tool_id in BROKERED_TOOL_IDS:
            broker_preparation = await run_broker_tool_wrapper(
                agent,
                step_id,
                tool_def,
                validated_args,
                state,
                invocation_id=invocation_id,
                tool_request_id=tool_request_id,
                requested_at=format_utc_iso(started_at),
                preflight_only=False,
            )
            broker_preparation.result.tool_request_id = tool_request_id
            return finalize_action_step_preparation(
                state=state,
                preparation=broker_preparation,
            )
        elif tool_def.tool_id == CAPTURE_SCREEN_TOOL_ID:
            if invocation_id is None:
                # The claim that makes one approval run one capture binds to this
                # row, so a capture without it would be unclaimable.
                raise RuntimeError("capture_screen requires a tool invocation audit")
            result = await run_capture_screen_tool(
                step_id=step_id,
                tool_def=tool_def,
                state=state,
                app_name=str(validated_args["app_name"]),
                tool_request_id=tool_request_id,
                tool_invocation_id=invocation_id,
                requested_at=format_utc_iso(started_at),
            )
        else:
            result = await run_validated_tool_impl(
                agent,
                tool_def,
                validated_args,
                state,
                sink=sink,
                runtime=runtime,
                actor=actor,
                step_id=step_id,
                started_at=started_at,
                origin=origin,
            )
    except ActionStepEventPersistenceError as exc:
        # The finalizer logs its own failure; keep the retryable event error primary.
        with suppress(ToolCompletionAuditPersistenceError):
            finalize_action_tool_invocation(
                tool_id=tool_def.tool_id,
                invocation_id=invocation_id,
                result=None,
                error=exc,
            )
        raise
    except TokenBudgetExceeded as exc:
        finalize_action_tool_invocation(
            tool_id=tool_def.tool_id,
            invocation_id=invocation_id,
            result=None,
            error=exc,
        )
        raise
    except ToolValidationError as exc:
        finalized_error = finalize_action_tool_invocation(
            tool_id=tool_def.tool_id,
            invocation_id=invocation_id,
            result=None,
            error=exc,
        )
        if invocation_id is None or finalized_error is None:
            raise
        exc.finalized_output = finalized_error
        exc.tool_invocation_id = invocation_id
        raise
    except ToolOutputValidationError as exc:
        _raise_finalized_output_error(exc)
    except ToolCompletionAuditPersistenceError:
        # Broker execution already owns terminal persistence for this invocation.
        raise
    except asyncio.CancelledError as exc:
        # Cancellation (e.g. the parallel batch timeout) must still close the audit
        # row. The finalizer logs its own persistence failure; the cancellation
        # itself always propagates.
        with suppress(ToolCompletionAuditPersistenceError):
            finalize_action_tool_invocation(
                tool_id=tool_def.tool_id,
                invocation_id=invocation_id,
                result=None,
                error=exc,
                error_status="canceled",
            )
        raise
    except Exception as exc:
        finalized_error = finalize_action_tool_invocation(
            tool_id=tool_def.tool_id,
            invocation_id=invocation_id,
            result=None,
            error=exc,
        )
        if invocation_id is None or finalized_error is None:
            raise
        raise FinalizedToolExecutionError(
            cause=exc,
            finalized_output=finalized_error,
            tool_invocation_id=invocation_id,
        ) from exc
    result.step_id = step_id
    result.tool_request_id = tool_request_id
    if result.tool_invocation_id is None:
        result.tool_invocation_id = invocation_id
    try:
        finalized_output = finalize_action_tool_invocation(
            tool_id=tool_def.tool_id,
            invocation_id=invocation_id,
            result=result,
        )
    except ToolOutputValidationError as exc:
        _raise_finalized_output_error(exc)
    if finalized_output is None:
        raise RuntimeError("local tool result was not durably projected")
    return build_tool_execution_result(
        result,
        finalized_output=finalized_output,
        control=build_tool_execution_control(
            status=result.status,
            output=result.output,
        ),
    )
