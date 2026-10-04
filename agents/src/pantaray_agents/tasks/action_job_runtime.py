"""Local action worker runtime."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Mapping
from pathlib import Path

import pantaray_agents.dependencies as deps
from pantaray_agents.action_status import (
    ACTION_STATUS_CANCELED,
    ACTION_STATUS_ERROR,
    ACTION_STATUS_PROCESSING,
    ActionRuntimeStatus,
    ActionTerminalStatus,
    is_action_terminal_status,
)
from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.action_step_emission import (
    emit_visible_action_step,
)
from pantaray_agents.application.action.ports import (
    ActionStepEmission,
    ActionStepEventPersistenceError,
    ActionUseCase,
)
from pantaray_agents.local_runtime.runtime.action_job_recovery import (
    ACTION_JOB_OPERATIONAL_RETRY_ERROR_CODE,
)
from pantaray_agents.local_runtime.runtime.action_job_runtime_repository import (
    ActionJobRuntimeRepository,
    ActionJobStartSkipCommand,
)
from pantaray_agents.local_runtime.runtime.bootstrap import (
    read_local_runtime_db_config,
)
from pantaray_agents.local_runtime.runtime.db_execution_context import (
    resolve_local_runtime_db_config,
)
from pantaray_agents.local_runtime.runtime.job_control import LocalJobDeferEvent
from pantaray_agents.local_runtime.runtime.process_events import (
    PROCESS_STATUS_ENQUEUED,
    append_local_action_step_event,
    append_local_process_event,
    mark_local_action_job_paused,
)
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.local_runtime.storage.migrations import MigrationError
from pantaray_agents.local_runtime.tooling.repository.executions import (
    load_action_job_root_execution_session_id,
)
from pantaray_agents.local_runtime.tooling.resources.action_session_temp_authority import (
    load_action_session_temp_cleanup_receipt,
)
from pantaray_agents.local_runtime.tooling.resources.action_session_temp_cleanup import (
    cleanup_action_session_temp,
)
from pantaray_agents.local_runtime.tooling.resources.cancel_cleanup import (
    cancel_action_runtime_resources,
)
from pantaray_agents.orchestration.common.types import JSONValue
from pantaray_agents.schema.action_conversation import ActionStepEventPayload
from pantaray_agents.schema.agent.action import ActionAgentRequest
from pantaray_agents.schema.repository_errors import classify_repository_exception
from pantaray_agents.tasks.action_job_support import (
    ACTION_FAILURE_STAGE_START_FAILED,
    ACTION_JOB_EXCEPTION_ERROR_CODE,
    ACTION_JOB_EXCEPTION_ERROR_MESSAGE,
    ACTION_JOB_EXCEPTION_ERROR_TYPE,
    ACTION_PROCESSING_ERROR_CODE,
    persist_terminal_action_status_strict,
)
from pantaray_agents.tasks.action_job_terminal import (
    ActionJobTerminalState,
    ActionJobTerminalWriter,
)
from pantaray_agents.tasks.job_retry import defer_local_job_if_retryable
from pantaray_agents.tasks.process_event_errors import ProcessEventAppendError
from pantaray_agents.tasks.types import (
    ActionJobRuntimePayload,
)
from pantaray_agents.utils.streaming_helpers import coerce_task_status

logger = logging.getLogger(__name__)
type ActionEventPayload = dict[str, JSONValue]


async def _run_action_job(job_payload: ActionJobRuntimePayload) -> None:
    process_id = job_payload["process_id"]
    job_id = job_payload["job_id"]
    action_id = job_payload["action_id"]
    user_id = job_payload["user_id"]
    started_at = now_utc_iso()

    terminal_state = ActionJobTerminalState()
    terminal_status: ActionRuntimeStatus | None = None
    execution_session_id: str | None = None
    error_event_observed = False
    db_path: Path | None = None
    busy_timeout_ms: int | None = None
    terminal_writer: ActionJobTerminalWriter | None = None
    terminal_write_started = False

    async def _append_event(
        *,
        event: str,
        data: ActionEventPayload,
        chunk_index: int | None = None,
        best_effort: bool = True,
    ) -> str | None:
        assert db_path is not None
        assert busy_timeout_ms is not None
        try:
            return str(
                append_local_process_event(
                    db_path=db_path,
                    busy_timeout_ms=busy_timeout_ms,
                    process_id=process_id,
                    event_type=event,
                    payload=data,
                )
            )
        except Exception:
            if not best_effort:
                raise
            return None

    def _load_terminal_execution_session_id() -> str | None:
        if db_path is None or busy_timeout_ms is None:
            return None
        try:
            return load_action_job_root_execution_session_id(
                db_path=db_path,
                busy_timeout_ms=busy_timeout_ms,
                job_id=job_id,
                process_id=process_id,
                user_id=user_id,
                action_id=action_id,
            )
        except sqlite3.Error as exc:
            logger.warning(
                "Action terminal session lookup failed; terminalization continues: "
                "job_id=%s process_id=%s action_id=%s exception_type=%s",
                job_id,
                process_id,
                action_id,
                type(exc).__name__,
            )
            return None

    async def _cleanup_terminal_session_after_commit() -> None:
        if (
            terminal_status is None
            or not is_action_terminal_status(terminal_status)
            or db_path is None
            or busy_timeout_ms is None
            or execution_session_id is None
        ):
            return
        try:
            cleanup_result = await cancel_action_runtime_resources(
                db_path=db_path,
                busy_timeout_ms=busy_timeout_ms,
                action_id=action_id,
                execution_session_id=execution_session_id,
            )
            if cleanup_result.warning_message is not None:
                logger.warning(
                    "Action terminal child cleanup incomplete: action_id=%s message=%s",
                    action_id,
                    cleanup_result.warning_message,
                )
            receipt = load_action_session_temp_cleanup_receipt(
                db_path=db_path,
                busy_timeout_ms=busy_timeout_ms,
                user_id=user_id,
                action_id=action_id,
                execution_session_id=execution_session_id,
            )
            if receipt is None:
                return
            outcome = await asyncio.to_thread(
                cleanup_action_session_temp,
                db_path=db_path,
                busy_timeout_ms=busy_timeout_ms,
                receipt=receipt,
            )
            if outcome.current_resource.status in {"cleanup_failed", "abandoned"}:
                logger.warning(
                    "Action terminal root cleanup incomplete: action_id=%s "
                    "execution_session_id=%s status=%s",
                    action_id,
                    execution_session_id,
                    outcome.current_resource.status,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Action terminal session cleanup deferred to recovery: "
                "action_id=%s execution_session_id=%s exception_type=%s",
                action_id,
                execution_session_id,
                type(exc).__name__,
            )

    async def on_event(event: str, ev_data: Mapping[str, JSONValue]) -> None:
        nonlocal error_event_observed
        event_name = str(event or "unknown")
        data_obj: ActionEventPayload = dict(ev_data)

        if event_name == "error":
            error_event_observed = True
            code = str(data_obj.get("error_code") or "")
            if code == "NDJSON_DECODE_ERROR":
                try:
                    logger.warning(
                        "NDJSON decode error (non-fatal): process_id=%s", process_id
                    )
                except Exception:
                    pass
                return
            if code:
                terminal_state.last_fatal_error_code = code
            await _append_event(event=event_name, data=dict(data_obj))
            return

        await _append_event(event=event_name, data=dict(data_obj))

    try:
        resolved_db_path, resolved_busy_timeout_ms = resolve_local_runtime_db_config(
            fallback=read_local_runtime_db_config
        )
        db_path = resolved_db_path
        busy_timeout_ms = resolved_busy_timeout_ms
        runtime_repository = ActionJobRuntimeRepository(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
        )
        preparation = runtime_repository.prepare_execution(
            payload=job_payload,
            started_at=started_at,
        )
        context = preparation.context
        suggestion_id = context.suggestion_id
        command_id = context.command_id
        accepted_at = context.accepted_at
        terminal_writer = ActionJobTerminalWriter(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            job_id=job_id,
            process_id=process_id,
            user_id=user_id,
            suggestion_id=suggestion_id,
            action_id=action_id,
            command_id=command_id,
            accepted_at=accepted_at,
            state=terminal_state,
            mark_local_action_job_paused=mark_local_action_job_paused,
            persist_terminal_action_status_strict=persist_terminal_action_status_strict,
        )
        if preparation.skip_outcome is not None:
            logger.info(
                "Local action job skipped because start claim did not update state: process_id=%s outcome=%s",
                process_id,
                preparation.skip_outcome,
            )
            runtime_repository.finalize_skipped_action_start(
                command=ActionJobStartSkipCommand(
                    job_id=job_id,
                    process_id=process_id,
                    user_id=user_id,
                    action_id=action_id,
                    suggestion_id=suggestion_id,
                    command_id=command_id,
                    accepted_at=accepted_at,
                    completed_at=now_utc_iso(),
                    failure_stage=ACTION_FAILURE_STAGE_START_FAILED,
                    outcome=preparation.skip_outcome,
                )
            )
            return
        action_use_case: ActionUseCase = await deps.get_action_application_service()
        req = ActionAgentRequest(
            user_id=user_id,
            suggestion_id=suggestion_id,
            action_id=action_id,
            user_step_id=context.user_step_id,
            user_step_number=context.user_step_number,
            user_step_local_step_number=context.user_step_local_step_number,
            user_step_short_id=context.user_step_short_id,
            user_step_created_at=context.user_step_created_at,
            user_message=context.user_message,
            preceding_assistant_messages=context.preceding_assistant_messages,
            execution_target=context.execution_target,
            approval_resume_session_id=context.approval_session_id,
            approval_resume_tool_request_id=context.approval_tool_request_id,
        )

        async def persist_action_step(event: ActionStepEventPayload) -> None:
            assert db_path is not None
            assert busy_timeout_ms is not None
            try:
                append_local_action_step_event(
                    db_path=db_path,
                    busy_timeout_ms=busy_timeout_ms,
                    process_id=process_id,
                    payload=event.model_dump(mode="json"),
                )
            except (MigrationError, OSError, sqlite3.Error) as exc:
                failure = classify_repository_exception(exc)
                raise ActionStepEventPersistenceError(
                    event=event,
                    error_kind=failure.error_kind,
                    retryable=failure.retryable,
                ) from exc

        async def emit_action_step(event: ActionStepEmission) -> None:
            await emit_visible_action_step(
                persist_action_step,
                action_id=action_id,
                process_id=process_id,
                event=event,
            )

        async def emit_error(event: Mapping[str, JSONValue]) -> None:
            await on_event("error", event)

        execution_result = await action_use_case.execute_action_runtime(
            req,
            emit_action_step=emit_action_step,
            emit_error=emit_error,
        )
        run_result = execution_result.run_result
        execution_session_id = execution_result.execution_session_id
        runtime_state_checkpoint = execution_result.runtime_state_checkpoint

        completed_at = str(run_result.completed_at or now_utc_iso())
        status_value = coerce_task_status(run_result.status)
        status_field = status_value.value
        if status_field == ACTION_STATUS_PROCESSING:
            terminal_status_value: ActionTerminalStatus | None = None
        elif is_action_terminal_status(status_field):
            terminal_status_value = status_field
        else:
            raise RuntimeError(
                f"Unsupported terminal action status from ActionRunResult: {status_field!r}"
            )
        if terminal_status_value is not None and execution_session_id is None:
            execution_session_id = _load_terminal_execution_session_id()
        response_error_code = run_result.action_failure_code
        if (
            status_field == "error"
            and not terminal_state.last_fatal_error_code
            and isinstance(response_error_code, str)
            and response_error_code.strip()
        ):
            terminal_state.last_fatal_error_code = response_error_code.strip()
        if status_field == "error" and not terminal_state.last_fatal_error_code:
            terminal_state.last_fatal_error_code = ACTION_PROCESSING_ERROR_CODE
        error_payload = getattr(run_result, "error_payload", None)
        if isinstance(error_payload, dict) and error_payload:
            if status_field == ACTION_STATUS_ERROR and not error_event_observed:
                await _append_event(event="error", data=dict(error_payload))
                error_event_observed = True

        if status_field == "processing":
            terminal_write_started = True
            if not await terminal_writer.write_pause_terminal(
                completed_at=str(completed_at),
                approval_blockers=[
                    blocker.model_dump(mode="json")
                    for blocker in run_result.approval_blockers
                ],
            ):
                # An external Stop fenced this run while it was asking for an
                # approval decision. Converge on the cancel terminal that also
                # settles the children instead of parking behind that decision.
                if execution_session_id is None:
                    execution_session_id = _load_terminal_execution_session_id()
                terminal_status = await terminal_writer.persist_terminal_until_success(
                    requested_status=ACTION_STATUS_CANCELED,
                    completed_at=str(completed_at),
                    reason="stop_fence_during_approval_pause",
                    runtime_state_checkpoint=runtime_state_checkpoint,
                )
        elif terminal_state.terminal_written:
            pass
        else:
            assert terminal_status_value is not None
            terminal_write_started = True
            terminal_status = await terminal_writer.persist_terminal_until_success(
                requested_status=terminal_status_value,
                completed_at=str(completed_at),
                reason="agent_response_terminal",
                runtime_state_checkpoint=runtime_state_checkpoint,
                run_result=run_result,
            )
    except Exception as exc:  # noqa: BLE001
        if db_path is not None and busy_timeout_ms is not None:
            defer_event = None
            if (
                isinstance(exc, ActionStepEventPersistenceError)
                and exc.event.status != "processing"
            ):
                defer_event = LocalJobDeferEvent(
                    event_name="action_step",
                    payload=exc.event.model_dump(mode="json"),
                    created_at=now_utc_iso(),
                )
            await defer_local_job_if_retryable(
                exc=exc,
                job_id=job_id,
                process_id=process_id,
                process_pending_status=PROCESS_STATUS_ENQUEUED,
                db_path=db_path,
                busy_timeout_ms=busy_timeout_ms,
                process_event=defer_event,
                attempt_error_code=ACTION_JOB_OPERATIONAL_RETRY_ERROR_CODE,
            )
            if execution_session_id is None:
                execution_session_id = _load_terminal_execution_session_id()
        if terminal_write_started:
            raise
        if not terminal_state.last_fatal_error_code:
            terminal_state.last_fatal_error_code = ACTION_JOB_EXCEPTION_ERROR_CODE
        try:
            error_event_payload: ActionEventPayload = {
                "error_type": ACTION_JOB_EXCEPTION_ERROR_TYPE,
                "error_code": str(terminal_state.last_fatal_error_code),
                "error_message": ACTION_JOB_EXCEPTION_ERROR_MESSAGE,
            }
            if db_path is not None and busy_timeout_ms is not None:
                append_local_process_event(
                    db_path=db_path,
                    busy_timeout_ms=busy_timeout_ms,
                    process_id=str(process_id),
                    event_type="error",
                    payload=error_event_payload,
                )
        except ProcessEventAppendError:
            logger.exception(
                "Failed to append action job error event before terminalization: process_id=%s action_id=%s",
                process_id,
                action_id,
            )
        if terminal_writer is None:
            raise
        completed_at = now_utc_iso()
        terminal_write_started = True
        terminal_status = await terminal_writer.persist_terminal_until_success(
            requested_status="error",
            completed_at=str(completed_at),
            reason="exception",
            runtime_state_checkpoint=None,
        )
        await _cleanup_terminal_session_after_commit()
        raise
    await _cleanup_terminal_session_after_commit()


__all__ = ["_run_action_job"]
