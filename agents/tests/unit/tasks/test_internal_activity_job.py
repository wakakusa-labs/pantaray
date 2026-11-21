from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.activity_log_seed import seed_activity_log

from pantaray_agents.local_runtime.runtime.activity_local_executor import (
    ActivitySummaryExecutionResult,
)
from pantaray_agents.local_runtime.runtime.activity_queue import (
    enqueue_local_activity_summary_job,
)
from pantaray_agents.local_runtime.runtime.activity_source_rows import (
    finalize_activity_summary_start_error_if_processing,
    persist_activity_summary_result,
    reserve_activity_summary_processing,
)
from pantaray_agents.local_runtime.runtime.identity import (
    register_logged_out_owner,
    reset_logged_out_owner,
)
from pantaray_agents.local_runtime.runtime.job_claim import claim_next_pending_job
from pantaray_agents.local_runtime.runtime.job_control import DeferredLocalJob
from pantaray_agents.local_runtime.runtime.job_route_identity import (
    bind_job_route_identity,
    require_current_route_identity,
)
from pantaray_agents.local_runtime.runtime.job_types import (
    LOCAL_ACTIVITY_SUMMARY_JOB_TYPE,
)
from pantaray_agents.local_runtime.runtime.session_store import (
    import_desktop_session,
    mark_configured,
    reset_desktop_session_store,
)
from pantaray_agents.local_runtime.storage.migrations import (
    apply_migrations,
    load_default_migrations,
)
from pantaray_agents.schema.agent.activity import (
    ActivitySummaryAgentRequest,
    ActivitySummaryAgentResponse,
)
from pantaray_agents.tasks.types import (
    ActivitySummaryJobPayload,
)

# Claimed right after the period ended. Only a job claimed at least 24 hours
# late is expired, so these ordinary-operation tests are unaffected by the limit.
_CLAIMED_AT = datetime(2026, 3, 27, 1, 5, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _reset_session_store() -> None:
    reset_desktop_session_store()
    yield
    reset_desktop_session_store()


def _insert_user(
    db_path: Path, *, user_id: str = "user-1", session_version: str = "1"
) -> None:
    import_desktop_session(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id=user_id,
        desktop_access_token="header.payload.signature",
        expires_at="2099-03-27T01:00:00Z",
        session_version=session_version,
    )


def _claim_activity_job(db_path: Path, *, job_type: str) -> None:
    claimed = claim_next_pending_job(
        db_path=str(db_path),
        busy_timeout_ms=1_000,
        job_type=job_type,
        owner_user_id="user-1",
        claimed_by="worker-1",
        process_running_status="running",
        expected_process_pending_status="enqueued",
    )
    assert claimed is not None


def _assert_activity_job_is_deferred(
    db_path: Path,
    *,
    job_id: str,
    process_id: str,
) -> None:
    with sqlite3.connect(db_path) as connection:
        job_row = connection.execute(
            """
            SELECT status, scheduled_at, claimed_by, claimed_at, heartbeat_at
            FROM jobs
            WHERE job_id = ?
            """,
            (job_id,),
        ).fetchone()
        process_row = connection.execute(
            "SELECT status, current_job_id FROM processes WHERE process_id = ?",
            (process_id,),
        ).fetchone()
        attempt_row = connection.execute(
            "SELECT status FROM job_attempts WHERE job_id = ?",
            (job_id,),
        ).fetchone()

    assert job_row is not None
    assert job_row[0] == "queued"
    assert job_row[1] is not None
    assert job_row[2:] == (None, None, None)
    assert process_row == ("enqueued", None)
    assert attempt_row == ("completed",)


@pytest.mark.asyncio
async def test_activity_summary_success_does_not_enqueue_fact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "runtime.db"
    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=load_default_migrations(),
    )
    _insert_user(db_path)
    monkeypatch.setenv("LOCAL_DB_PATH", str(db_path))
    monkeypatch.setenv("LOCAL_DB_BUSY_TIMEOUT_MS", "1000")

    summary_id = "summary-1"
    user_id = "user-1"
    period_start = "2026-03-26T00:00:00Z"
    period_end = "2026-03-27T00:00:00Z"
    reserve_activity_summary_processing(
        db_path=db_path,
        busy_timeout_ms=1_000,
        summary_id=summary_id,
        user_id=user_id,
        summary_type="24h",
        period_start=period_start,
        period_end=period_end,
        created_at="2026-03-27T00:00:05Z",
    )

    async def _fake_run_local_activity_summary_agent(**_kwargs):  # noqa: ANN003
        return ActivitySummaryExecutionResult(
            response=ActivitySummaryAgentResponse(
                summary_id=summary_id,
                user_id=user_id,
                summary_type="24h",
                summary="summary text",
                thinking=None,
                period_start=period_start,
                period_end=period_end,
                source_ids=[],
                created_at="2026-03-27T00:01:00Z",
                status="success",
                error=None,
            ),
            prompt_text="expanded prompt",
        )

    monkeypatch.setattr(
        "pantaray_agents.tasks.internal_jobs.activity.run_local_activity_summary_agent",
        _fake_run_local_activity_summary_agent,
    )

    from pantaray_agents.tasks.internal_jobs.activity import _run_activity_summary_job

    await _run_activity_summary_job(
        {
            "job_id": "activity-summary-job-1",
            "process_id": "activity-summary-process-1",
            "summary_id": summary_id,
            "user_id": user_id,
            "enqueued_at": "2026-03-27T00:00:00Z",
            "summary_type": "24h",
            "period_start": period_start,
            "period_end": period_end,
        },
        now=_CLAIMED_AT,
    )

    with sqlite3.connect(db_path) as connection:
        fact_job_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM jobs
            WHERE job_type = 'structure_facts'
            """,
        ).fetchone()
        prompt_row = connection.execute(
            """
            SELECT prompt_text
            FROM activity_summaries
            WHERE summary_id = ?
            """,
            (summary_id,),
        ).fetchone()

    assert fact_job_count == (0,)
    assert prompt_row == ("expanded prompt",)


@pytest.mark.asyncio
async def test_activity_summary_job_older_than_the_cap_is_not_summarized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A job queued while there was no route ends without an LLM request."""
    db_path = tmp_path / "runtime.db"
    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=load_default_migrations(),
    )
    _insert_user(db_path)
    monkeypatch.setenv("LOCAL_DB_PATH", str(db_path))
    monkeypatch.setenv("LOCAL_DB_BUSY_TIMEOUT_MS", "1000")

    summary_id = "summary-stale"
    reserve_activity_summary_processing(
        db_path=db_path,
        busy_timeout_ms=1_000,
        summary_id=summary_id,
        user_id="user-1",
        summary_type="24h",
        period_start="2026-03-26T00:00:00Z",
        period_end="2026-03-27T00:00:00Z",
        created_at="2026-03-27T00:10:00Z",
    )

    from pantaray_agents.tasks.internal_jobs import activity as activity_module

    monkeypatch.setattr(
        activity_module,
        "load_workspace_context_prompt",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("an expired window must not reach preflight")
        ),
    )
    monkeypatch.setattr(
        activity_module,
        "run_local_activity_summary_agent",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("an expired window must not reach the LLM")
        ),
    )

    await activity_module._run_activity_summary_job(
        {
            "job_id": "activity-summary-job-stale",
            "process_id": "activity-summary-process-stale",
            "summary_id": summary_id,
            "user_id": "user-1",
            "enqueued_at": "2026-03-27T00:10:00Z",
            "summary_type": "24h",
            "period_start": "2026-03-26T00:00:00Z",
            "period_end": "2026-03-27T00:00:00Z",
        },
        now=datetime(2026, 4, 26, 9, 0, tzinfo=UTC),
    )

    with sqlite3.connect(db_path) as connection:
        summary_row = connection.execute(
            "SELECT status, summary, error FROM activity_summaries WHERE summary_id = ?",
            (summary_id,),
        ).fetchone()
        trigger_count = connection.execute(
            "SELECT COUNT(*) FROM hook_inbox "
            "WHERE json_extract(payload_json, '$.summary_id') = ?",
            (summary_id,),
        ).fetchone()

    assert summary_row == ("canceled", "", None)
    assert trigger_count == (0,)


@pytest.mark.asyncio
async def test_activity_summary_job_summarizes_only_the_windows_inside_the_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once a route is back, only windows inside the limit reach the LLM."""
    db_path = tmp_path / "runtime.db"
    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=load_default_migrations(),
    )
    _insert_user(db_path)
    monkeypatch.setenv("LOCAL_DB_PATH", str(db_path))
    monkeypatch.setenv("LOCAL_DB_BUSY_TIMEOUT_MS", "1000")

    claimed_at = datetime(2026, 4, 26, 9, 0, tzinfo=UTC)
    windows = {
        "a-month-old": ("2026-03-27T00:00:00Z", "2026-03-27T01:00:00Z"),
        "exactly-at-the-cap": ("2026-04-25T08:00:00Z", "2026-04-25T09:00:00Z"),
        "inside-the-cap": ("2026-04-26T06:00:00Z", "2026-04-26T07:00:00Z"),
    }
    for summary_id, (period_start, period_end) in windows.items():
        reserve_activity_summary_processing(
            db_path=db_path,
            busy_timeout_ms=1_000,
            summary_id=summary_id,
            user_id="user-1",
            summary_type="1h",
            period_start=period_start,
            period_end=period_end,
            created_at=period_end,
        )

    summarized: list[str] = []

    async def _fake_run_local_activity_summary_agent(
        *, request: ActivitySummaryAgentRequest, **_kwargs: object
    ) -> ActivitySummaryExecutionResult:
        summarized.append(request.summary_id)
        return ActivitySummaryExecutionResult(
            response=ActivitySummaryAgentResponse(
                summary_id=request.summary_id,
                user_id="user-1",
                summary_type="1h",
                summary="summary text",
                thinking=None,
                period_start=request.period_start,
                period_end=request.period_end,
                source_ids=[],
                created_at="2026-04-26T09:00:10Z",
                status="success",
                error=None,
            ),
            prompt_text="prompt",
        )

    from pantaray_agents.tasks.internal_jobs import activity as activity_module

    monkeypatch.setattr(
        activity_module, "load_workspace_context_prompt", lambda **_kwargs: ""
    )
    monkeypatch.setattr(
        activity_module,
        "run_local_activity_summary_agent",
        _fake_run_local_activity_summary_agent,
    )

    for summary_id, (period_start, period_end) in windows.items():
        await activity_module._run_activity_summary_job(
            {
                "job_id": f"job-{summary_id}",
                "process_id": f"process-{summary_id}",
                "summary_id": summary_id,
                "user_id": "user-1",
                "enqueued_at": period_end,
                "summary_type": "1h",
                "period_start": period_start,
                "period_end": period_end,
            },
            now=claimed_at,
        )

    with sqlite3.connect(db_path) as connection:
        statuses = dict(
            connection.execute(
                "SELECT summary_id, status FROM activity_summaries"
            ).fetchall()
        )

    assert summarized == ["inside-the-cap"]
    assert statuses == {
        "a-month-old": "canceled",
        "exactly-at-the-cap": "canceled",
        "inside-the-cap": "success",
    }


@pytest.mark.asyncio
async def test_activity_summary_job_does_not_rerun_terminal_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "runtime.db"
    apply_migrations(db_path, 1_000, load_default_migrations())
    _insert_user(db_path)
    monkeypatch.setenv("LOCAL_DB_PATH", str(db_path))
    monkeypatch.setenv("LOCAL_DB_BUSY_TIMEOUT_MS", "1000")
    payload: ActivitySummaryJobPayload = {
        "job_id": "activity-summary-job-1",
        "process_id": "activity-summary-process-1",
        "summary_id": "summary-1",
        "user_id": "user-1",
        "enqueued_at": "2026-03-27T00:00:00Z",
        "summary_type": "1h",
        "period_start": "2026-03-27T00:00:00Z",
        "period_end": "2026-03-27T01:00:00Z",
    }
    seed_activity_log(
        db_path,
        log_id="log-1",
        user_id=payload["user_id"],
        period_start="2026-03-27T00:00:00Z",
        period_end="2026-03-27T00:04:00Z",
        description="activity",
    )
    reserve_activity_summary_processing(
        db_path=db_path,
        busy_timeout_ms=1_000,
        summary_id=payload["summary_id"],
        user_id=payload["user_id"],
        summary_type=payload["summary_type"],
        period_start=payload["period_start"],
        period_end=payload["period_end"],
        created_at=payload["enqueued_at"],
    )
    persist_activity_summary_result(
        db_path=db_path,
        busy_timeout_ms=1_000,
        summary_id=payload["summary_id"],
        user_id=payload["user_id"],
        summary_type=payload["summary_type"],
        period_start=payload["period_start"],
        period_end=payload["period_end"],
        summary="persisted summary",
        status="success",
        error=None,
        prompt_text="persisted prompt",
        thinking=None,
        source_ids=["log-1"],
        updated_at="2026-03-27T01:00:10Z",
    )

    monkeypatch.setattr(
        "pantaray_agents.tasks.internal_jobs.activity.load_workspace_context_prompt",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("terminal Summary must skip preflight")
        ),
    )
    run_agent_calls = 0

    async def _run_agent(**_kwargs: object) -> None:
        nonlocal run_agent_calls
        run_agent_calls += 1

    monkeypatch.setattr(
        "pantaray_agents.tasks.internal_jobs.activity.run_local_activity_summary_agent",
        _run_agent,
    )

    from pantaray_agents.tasks.internal_jobs.activity import _run_activity_summary_job

    await _run_activity_summary_job(payload, now=_CLAIMED_AT)

    assert run_agent_calls == 0


@pytest.mark.asyncio
async def test_activity_summary_job_does_not_rerun_terminal_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "runtime.db"
    apply_migrations(db_path, 1_000, load_default_migrations())
    _insert_user(db_path)
    monkeypatch.setenv("LOCAL_DB_PATH", str(db_path))
    monkeypatch.setenv("LOCAL_DB_BUSY_TIMEOUT_MS", "1000")
    payload: ActivitySummaryJobPayload = {
        "job_id": "activity-summary-job-1",
        "process_id": "activity-summary-process-1",
        "summary_id": "summary-1",
        "user_id": "user-1",
        "enqueued_at": "2026-03-27T00:00:00Z",
        "summary_type": "1h",
        "period_start": "2026-03-27T00:00:00Z",
        "period_end": "2026-03-27T01:00:00Z",
    }
    reserve_activity_summary_processing(
        db_path=db_path,
        busy_timeout_ms=1_000,
        summary_id=payload["summary_id"],
        user_id=payload["user_id"],
        summary_type=payload["summary_type"],
        period_start=payload["period_start"],
        period_end=payload["period_end"],
        created_at=payload["enqueued_at"],
    )
    finalize_activity_summary_start_error_if_processing(
        db_path=db_path,
        busy_timeout_ms=1_000,
        summary_id=payload["summary_id"],
        user_id=payload["user_id"],
        updated_at="2026-03-27T01:00:10Z",
        error_code="ACTIVITY_SUMMARY_EXECUTION_FAILED",
        error_message="failed",
    )
    run_agent_calls = 0

    async def _run_agent(**_kwargs: object) -> None:
        nonlocal run_agent_calls
        run_agent_calls += 1

    monkeypatch.setattr(
        "pantaray_agents.tasks.internal_jobs.activity.run_local_activity_summary_agent",
        _run_agent,
    )
    monkeypatch.setattr(
        "pantaray_agents.tasks.internal_jobs.activity.load_workspace_context_prompt",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("terminal Summary must skip preflight")
        ),
    )

    from pantaray_agents.tasks.internal_jobs.activity import (
        ActivitySummaryTerminalFailureError,
        _run_activity_summary_job,
    )

    with pytest.raises(ActivitySummaryTerminalFailureError, match="status=error"):
        await _run_activity_summary_job(payload, now=_CLAIMED_AT)

    assert run_agent_calls == 0


@pytest.mark.asyncio
async def test_activity_summary_job_defers_transient_context_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "pantaray_agents.tasks.internal_jobs.activity.load_workspace_context_prompt",
        lambda **_kwargs: "",
    )
    monkeypatch.setattr(
        "pantaray_agents.tasks.internal_jobs.activity.read_local_runtime_db_config",
        lambda: (Path("runtime.db"), 1_000),
    )
    monkeypatch.setattr(
        "pantaray_agents.tasks.internal_jobs.activity.load_activity_summary_execution_state",
        lambda **_kwargs: "processing",
    )

    async def _raise_locked(**_kwargs: object) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(
        "pantaray_agents.tasks.internal_jobs.activity.run_local_activity_summary_agent",
        _raise_locked,
    )

    def _defer(**_kwargs: object) -> None:
        raise DeferredLocalJob(
            job_id="activity-summary-job-1",
            scheduled_at="2026-03-27T00:00:05Z",
        )

    monkeypatch.setattr(
        "pantaray_agents.tasks.internal_jobs.activity.defer_local_job_if_retryable",
        _defer,
    )

    from pantaray_agents.tasks.internal_jobs.activity import _run_activity_summary_job

    with pytest.raises(DeferredLocalJob):
        await _run_activity_summary_job(
            {
                "job_id": "activity-summary-job-1",
                "process_id": "activity-summary-process-1",
                "summary_id": "summary-1",
                "user_id": "user-1",
                "enqueued_at": "2026-03-27T00:00:00Z",
                "summary_type": "1h",
                "period_start": "2026-03-27T00:00:00Z",
                "period_end": "2026-03-27T01:00:00Z",
            },
            now=_CLAIMED_AT,
        )


@pytest.mark.asyncio
async def test_activity_summary_job_defers_terminal_persistence_busy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "runtime.db"
    apply_migrations(db_path, 1_000, load_default_migrations())
    _insert_user(db_path)
    monkeypatch.setenv("LOCAL_DB_PATH", str(db_path))
    monkeypatch.setenv("LOCAL_DB_BUSY_TIMEOUT_MS", "1000")
    payload: ActivitySummaryJobPayload = {
        "job_id": "activity-summary-job-1",
        "process_id": "activity-summary-process-1",
        "summary_id": "summary-1",
        "user_id": "user-1",
        "enqueued_at": "2026-03-27T00:00:00Z",
        "summary_type": "1h",
        "period_start": "2026-03-27T00:00:00Z",
        "period_end": "2026-03-27T01:00:00Z",
    }
    reserve_activity_summary_processing(
        db_path=db_path,
        busy_timeout_ms=1_000,
        summary_id=payload["summary_id"],
        user_id=payload["user_id"],
        summary_type=payload["summary_type"],
        period_start=payload["period_start"],
        period_end=payload["period_end"],
        created_at=payload["enqueued_at"],
    )
    enqueue_local_activity_summary_job(
        payload=payload,
        db_path=db_path,
        busy_timeout_ms=1_000,
    )
    _claim_activity_job(db_path, job_type=LOCAL_ACTIVITY_SUMMARY_JOB_TYPE)

    async def _run_agent(**_kwargs: object) -> ActivitySummaryExecutionResult:
        return ActivitySummaryExecutionResult(
            response=ActivitySummaryAgentResponse(
                summary_id=payload["summary_id"],
                user_id=payload["user_id"],
                summary_type=payload["summary_type"],
                summary="completed summary",
                thinking=None,
                period_start=payload["period_start"],
                period_end=payload["period_end"],
                source_ids=[],
                created_at="2026-03-27T01:00:10Z",
                status="success",
                error=None,
            ),
            prompt_text="expanded prompt",
        )

    def _raise_locked(**_kwargs: object) -> None:
        raise sqlite3.OperationalError("database is locked")

    from pantaray_agents.tasks.internal_jobs import activity as activity_module

    monkeypatch.setattr(
        activity_module, "load_workspace_context_prompt", lambda **_: ""
    )
    monkeypatch.setattr(activity_module, "run_local_activity_summary_agent", _run_agent)
    monkeypatch.setattr(
        activity_module, "persist_activity_summary_result", _raise_locked
    )

    with pytest.raises(DeferredLocalJob):
        await activity_module._run_activity_summary_job(payload, now=_CLAIMED_AT)

    _assert_activity_job_is_deferred(
        db_path,
        job_id=payload["job_id"],
        process_id=payload["process_id"],
    )
    with sqlite3.connect(db_path) as connection:
        source_row = connection.execute(
            "SELECT status, summary FROM activity_summaries WHERE summary_id = ?",
            (payload["summary_id"],),
        ).fetchone()
    assert source_row == ("processing", "")


@pytest.fixture
def _signed_in_runtime():
    """Startup registers the owner; Electron main then applies `configure`.

    Without both, no stored value resolves to a route identity at all, so the
    sign-in below could not change one.
    """
    register_logged_out_owner("local-owner")
    mark_configured()
    yield
    reset_logged_out_owner()


def _claimed_activity_job(
    db_path: Path, payload: ActivitySummaryJobPayload, *, claimed_by: str
) -> None:
    claimed = claim_next_pending_job(
        db_path=str(db_path),
        busy_timeout_ms=1_000,
        job_type=LOCAL_ACTIVITY_SUMMARY_JOB_TYPE,
        owner_user_id=payload["user_id"],
        claimed_by=claimed_by,
        process_running_status="running",
        expected_process_pending_status="enqueued",
    )
    assert claimed is not None


def _bound_activity_run(payload: ActivitySummaryJobPayload, db_path: Path):
    """The binding the executor makes around a claimed background job."""
    return bind_job_route_identity(
        job_id=payload["job_id"],
        process_id=payload["process_id"],
        process_pending_status="enqueued",
        db_path=db_path,
        busy_timeout_ms=1_000,
        requeue_on_change=True,
    )


def _successful_summary(
    payload: ActivitySummaryJobPayload, *, summary: str
) -> ActivitySummaryExecutionResult:
    return ActivitySummaryExecutionResult(
        response=ActivitySummaryAgentResponse(
            summary_id=payload["summary_id"],
            user_id=payload["user_id"],
            summary_type=payload["summary_type"],
            summary=summary,
            thinking=None,
            period_start=payload["period_start"],
            period_end=payload["period_end"],
            source_ids=[],
            created_at="2026-03-27T01:00:10Z",
            status="success",
            error=None,
        ),
        prompt_text="expanded prompt",
    )


def _reserved_and_claimed_activity_job(
    db_path: Path, payload: ActivitySummaryJobPayload
) -> None:
    reserve_activity_summary_processing(
        db_path=db_path,
        busy_timeout_ms=1_000,
        summary_id=payload["summary_id"],
        user_id=payload["user_id"],
        summary_type=payload["summary_type"],
        period_start=payload["period_start"],
        period_end=payload["period_end"],
        created_at=payload["enqueued_at"],
    )
    enqueue_local_activity_summary_job(
        payload=payload,
        db_path=db_path,
        busy_timeout_ms=1_000,
    )
    _claimed_activity_job(db_path, payload, claimed_by="worker-1")


@pytest.mark.asyncio
@pytest.mark.usefixtures("_signed_in_runtime")
async def test_a_summary_produced_on_a_replaced_route_is_never_published(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The account changed while the agent ran, so the summary is dropped."""
    db_path = tmp_path / "runtime.db"
    apply_migrations(db_path, 1_000, load_default_migrations())
    _insert_user(db_path)
    monkeypatch.setenv("LOCAL_DB_PATH", str(db_path))
    monkeypatch.setenv("LOCAL_DB_BUSY_TIMEOUT_MS", "1000")
    payload: ActivitySummaryJobPayload = {
        "job_id": "activity-summary-job-1",
        "process_id": "activity-summary-process-1",
        "summary_id": "summary-1",
        "user_id": "user-1",
        "enqueued_at": "2026-03-27T00:00:00Z",
        "summary_type": "1h",
        "period_start": "2026-03-27T00:00:00Z",
        "period_end": "2026-03-27T01:00:00Z",
    }
    _reserved_and_claimed_activity_job(db_path, payload)

    async def _run_agent(**_kwargs: object) -> ActivitySummaryExecutionResult:
        # The agent answered, and only then did the identity change: a second
        # sign-in of the same account is a new cloud identity.
        _insert_user(db_path, session_version="2")
        return _successful_summary(payload, summary="summary for the old account")

    from pantaray_agents.tasks.internal_jobs import activity as activity_module

    monkeypatch.setattr(
        activity_module, "load_workspace_context_prompt", lambda **_: ""
    )
    monkeypatch.setattr(activity_module, "run_local_activity_summary_agent", _run_agent)

    with _bound_activity_run(payload, db_path):
        with pytest.raises(DeferredLocalJob):
            await activity_module._run_activity_summary_job(payload, now=_CLAIMED_AT)

    _assert_activity_job_is_deferred(
        db_path,
        job_id=payload["job_id"],
        process_id=payload["process_id"],
    )
    with sqlite3.connect(db_path) as connection:
        source_row = connection.execute(
            "SELECT status, summary FROM activity_summaries WHERE summary_id = ?",
            (payload["summary_id"],),
        ).fetchone()
    assert source_row == ("processing", "")


@pytest.mark.asyncio
@pytest.mark.usefixtures("_signed_in_runtime")
async def test_a_route_change_during_the_llm_call_leaves_the_window_summarizable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A requeue from inside the agent must not end the summary row.

    The LLM entry raises from deep inside the agent call, where this job wraps
    everything in a broad handler that writes a terminal error. A terminal row
    is never reconsidered, so the window would lose its summary for good.
    """
    db_path = tmp_path / "runtime.db"
    apply_migrations(db_path, 1_000, load_default_migrations())
    _insert_user(db_path)
    monkeypatch.setenv("LOCAL_DB_PATH", str(db_path))
    monkeypatch.setenv("LOCAL_DB_BUSY_TIMEOUT_MS", "1000")
    payload: ActivitySummaryJobPayload = {
        "job_id": "activity-summary-job-1",
        "process_id": "activity-summary-process-1",
        "summary_id": "summary-1",
        "user_id": "user-1",
        "enqueued_at": "2026-03-27T00:00:00Z",
        "summary_type": "1h",
        "period_start": "2026-03-27T00:00:00Z",
        "period_end": "2026-03-27T01:00:00Z",
    }
    _reserved_and_claimed_activity_job(db_path, payload)

    async def _stopped_at_the_llm_entry(**_kwargs: object) -> None:
        _insert_user(db_path, session_version="2")
        await require_current_route_identity()
        raise AssertionError("the LLM entry must not return after a route change")

    from pantaray_agents.tasks.internal_jobs import activity as activity_module

    monkeypatch.setattr(
        activity_module, "load_workspace_context_prompt", lambda **_: ""
    )
    monkeypatch.setattr(
        activity_module, "run_local_activity_summary_agent", _stopped_at_the_llm_entry
    )

    with _bound_activity_run(payload, db_path):
        with pytest.raises(DeferredLocalJob):
            await activity_module._run_activity_summary_job(payload, now=_CLAIMED_AT)

    with sqlite3.connect(db_path) as connection:
        stopped_row = connection.execute(
            "SELECT status, summary, error FROM activity_summaries "
            "WHERE summary_id = ?",
            (payload["summary_id"],),
        ).fetchone()
        connection.execute(
            "UPDATE jobs SET scheduled_at = '2000-01-01T00:00:00Z' WHERE job_id = ?",
            (payload["job_id"],),
        )
    assert stopped_row == ("processing", "", None)

    _claimed_activity_job(db_path, payload, claimed_by="worker-2")

    async def _run_agent(**_kwargs: object) -> ActivitySummaryExecutionResult:
        return _successful_summary(payload, summary="summary for the new account")

    monkeypatch.setattr(activity_module, "run_local_activity_summary_agent", _run_agent)

    with _bound_activity_run(payload, db_path):
        await activity_module._run_activity_summary_job(payload, now=_CLAIMED_AT)

    with sqlite3.connect(db_path) as connection:
        summarized_row = connection.execute(
            "SELECT status, summary FROM activity_summaries WHERE summary_id = ?",
            (payload["summary_id"],),
        ).fetchone()
    assert summarized_row == ("success", "summary for the new account")
