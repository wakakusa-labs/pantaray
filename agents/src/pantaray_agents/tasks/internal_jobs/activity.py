from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from pantaray_agents.local_runtime.activity_summary_schedule import parse_iso_z
from pantaray_agents.local_runtime.runtime.activity_local_executor import (
    run_local_activity_summary_agent,
)
from pantaray_agents.local_runtime.runtime.activity_source_rows import (
    ACTIVITY_SOURCE_STATUS_CANCELED,
    finalize_activity_summary_start_error_if_processing,
    persist_activity_summary_result,
)
from pantaray_agents.local_runtime.runtime.activity_summary_execution import (
    is_activity_summary_window_expired,
    load_activity_summary_execution_state,
)
from pantaray_agents.local_runtime.runtime.bootstrap import read_local_runtime_db_config
from pantaray_agents.local_runtime.runtime.job_route_identity import (
    require_current_route_identity,
)
from pantaray_agents.local_runtime.runtime.utc_timestamps import (
    format_utc_iso,
    now_utc_iso,
)
from pantaray_agents.local_runtime.tooling.repository.workspace_context import (
    load_workspace_context_prompt,
)
from pantaray_agents.schema.agent.activity import ActivitySummaryAgentRequest
from pantaray_agents.tasks.types import ActivitySummaryJobPayload

from ..job_retry import defer_local_job_if_retryable


class ActivitySummaryTerminalFailureError(RuntimeError):
    """A recovered Summary job already has a non-success terminal result."""


async def _run_activity_summary_job(
    payload: ActivitySummaryJobPayload,
    *,
    now: datetime | None = None,
) -> None:
    db_path, timeout_ms = read_local_runtime_db_config()
    claimed_at = datetime.now(UTC) if now is None else now.astimezone(UTC)
    try:
        existing_state = load_activity_summary_execution_state(
            db_path=db_path,
            busy_timeout_ms=timeout_ms,
            summary_id=payload["summary_id"],
            user_id=payload["user_id"],
            summary_type=payload["summary_type"],
            period_start=payload["period_start"],
            period_end=payload["period_end"],
        )
        if existing_state == "success":
            return
        if existing_state != "processing":
            raise ActivitySummaryTerminalFailureError(
                "Activity Summary job recovered after terminal source failure: "
                f"status={existing_state}"
            )
        # A job that waited in the queue while there was no route is already
        # expired by the time it is claimed. Leaving the window unsummarized is
        # not a failure, so the reserved row terminates as canceled rather than
        # error: every reader selects `status = 'success'`, so the period is
        # simply a gap for the longer summaries and for memory.
        if is_activity_summary_window_expired(
            period_start=parse_iso_z(payload["period_start"]),
            period_end=parse_iso_z(payload["period_end"]),
            now=claimed_at,
        ):
            persist_activity_summary_result(
                db_path=db_path,
                busy_timeout_ms=timeout_ms,
                summary_id=payload["summary_id"],
                user_id=payload["user_id"],
                summary_type=payload["summary_type"],
                period_start=payload["period_start"],
                period_end=payload["period_end"],
                summary="",
                status=ACTIVITY_SOURCE_STATUS_CANCELED,
                error=None,
                prompt_text="",
                thinking=None,
                source_ids=[],
                updated_at=format_utc_iso(claimed_at),
            )
            return
        workspace_context_prompt = load_workspace_context_prompt(
            db_path=db_path,
            busy_timeout_ms=timeout_ms,
            user_id=payload["user_id"],
        )
    except ActivitySummaryTerminalFailureError:
        raise
    except Exception as exc:
        await defer_local_job_if_retryable(
            exc=exc,
            job_id=payload["job_id"],
            process_id=payload["process_id"],
            process_pending_status="enqueued",
            db_path=db_path,
            busy_timeout_ms=timeout_ms,
        )
        finalize_activity_summary_start_error_if_processing(
            db_path=db_path,
            busy_timeout_ms=timeout_ms,
            summary_id=payload["summary_id"],
            user_id=payload["user_id"],
            updated_at=now_utc_iso(),
            error_code="ACTIVITY_SUMMARY_PREFLIGHT_FAILED",
            error_message=(
                f"ActivitySummaryAgent preflight failed: {exc.__class__.__name__}"
            ),
        )
        raise RuntimeError(
            f"ActivitySummaryAgent preflight failed: {exc.__class__.__name__}"
        ) from exc

    try:
        execution = await run_local_activity_summary_agent(
            request=ActivitySummaryAgentRequest(
                user_id=payload["user_id"],
                summary_id=payload["summary_id"],
                summary_type=payload["summary_type"],
                period_start=payload["period_start"],
                period_end=payload["period_end"],
                workspace_context_prompt=workspace_context_prompt or None,
            ),
            db_path=str(db_path),
            busy_timeout_ms=timeout_ms,
        )
        response = execution.response
    except Exception as exc:
        await defer_local_job_if_retryable(
            exc=exc,
            job_id=payload["job_id"],
            process_id=payload["process_id"],
            process_pending_status="enqueued",
            db_path=db_path,
            busy_timeout_ms=timeout_ms,
        )
        finalize_activity_summary_start_error_if_processing(
            db_path=db_path,
            busy_timeout_ms=timeout_ms,
            summary_id=payload["summary_id"],
            user_id=payload["user_id"],
            updated_at=now_utc_iso(),
            error_code="ACTIVITY_SUMMARY_EXECUTION_FAILED",
            error_message=(
                f"ActivitySummaryAgent failed before response: {exc.__class__.__name__}"
            ),
        )
        raise RuntimeError(
            f"ActivitySummaryAgent failed before response: {exc.__class__.__name__}"
        ) from exc

    # The summary row is the only place this job publishes what the LLM
    # produced, so the route it was produced on has to still be current.
    await require_current_route_identity()
    try:
        persist_activity_summary_result(
            db_path=db_path,
            busy_timeout_ms=timeout_ms,
            summary_id=payload["summary_id"],
            user_id=payload["user_id"],
            summary_type=payload["summary_type"],
            period_start=payload["period_start"],
            period_end=payload["period_end"],
            summary=str(response.summary),
            status=str(response.status),
            error=response.error,
            prompt_text=execution.prompt_text,
            thinking=response.thinking,
            source_ids=list(response.source_ids),
            updated_at=str(response.created_at),
        )
    except Exception as exc:
        await defer_local_job_if_retryable(
            exc=exc,
            job_id=payload["job_id"],
            process_id=payload["process_id"],
            process_pending_status="enqueued",
            db_path=db_path,
            busy_timeout_ms=timeout_ms,
        )
        raise
    if str(getattr(response, "status", "")) != "success":
        error = getattr(response, "error", None)
        code = getattr(error, "error_code", None) if error else None
        raise RuntimeError(f"ActivitySummaryAgent failed: {code or 'unknown'}")


def run_activity_summary_job(payload: ActivitySummaryJobPayload) -> None:
    asyncio.run(_run_activity_summary_job(payload))
