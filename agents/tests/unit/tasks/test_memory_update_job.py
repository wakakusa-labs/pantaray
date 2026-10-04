from __future__ import annotations

import errno
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import cast

import pytest

import pantaray_agents.local_runtime.runtime.job_control as job_control_module
import pantaray_agents.tasks.internal_jobs.memory_update_job as job_module
import pantaray_agents.tasks.job_retry as job_retry_module
from pantaray_agents.local_runtime.memory_catalog.models import MemorySource
from pantaray_agents.local_runtime.runtime.job_claim import claim_next_pending_job
from pantaray_agents.local_runtime.runtime.job_control import DeferredLocalJob
from pantaray_agents.local_runtime.runtime.job_enqueue import (
    enqueue_local_job_with_connection,
)
from pantaray_agents.local_runtime.runtime.job_types import (
    LOCAL_MEMORY_UPDATE_JOB_TYPE,
)
from pantaray_agents.local_runtime.runtime.memory_update_queue import (
    build_local_memory_update_enqueue_request,
)
from pantaray_agents.local_runtime.runtime.utc_timestamps import (
    now_utc_iso,
    parse_utc_iso,
)
from pantaray_agents.local_runtime.storage.migrations import (
    apply_migrations,
    load_default_migrations,
)
from pantaray_agents.local_runtime.tooling.memory_file_editor import (
    LocalMemoryFileEditorRuntime,
)
from pantaray_agents.tasks.internal_jobs.memory_lock_retry import (
    MEMORY_LOCK_RETRY_DELAY_SECONDS,
)
from pantaray_agents.tasks.internal_jobs.memory_update_job import (
    run_memory_update_job,
)
from pantaray_agents.tasks.types import MemoryUpdateJobPayload

BUSY_TIMEOUT_MS = 1_000


async def _never_complete() -> bool:
    return False


class _TrackingLease:
    def __init__(self, runtime: _TrackingRuntime) -> None:
        self._runtime = runtime

    def __enter__(self) -> _TrackingLease:
        assert not self._runtime.active
        self._runtime.active = True
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        assert self._runtime.active
        self._runtime.active = False


class _TrackingRuntime:
    def __init__(self, tmp_path: Path) -> None:
        self.artifact_root = tmp_path / "artifacts"
        self.db_path = tmp_path / "runtime.db"
        self.busy_timeout_ms = BUSY_TIMEOUT_MS
        self.acquire_calls = 0
        self.active = False

    def acquire_user_update_lock(
        self, *, user_id: str, owner_id: str
    ) -> _TrackingLease:
        assert user_id == "user-1"
        assert owner_id == "job-1"
        self.acquire_calls += 1
        return _TrackingLease(self)


def _runtime(runtime: _TrackingRuntime) -> LocalMemoryFileEditorRuntime:
    return cast(LocalMemoryFileEditorRuntime, runtime)


def _stub_recovery(
    monkeypatch: pytest.MonkeyPatch,
    callback: object,
) -> None:
    monkeypatch.setattr(
        job_module,
        "recover_pending_artifact_intent_for_source",
        callback,
    )


def _claim_job(tmp_path: Path) -> tuple[MemoryUpdateJobPayload, Path]:
    db_path = tmp_path / "runtime.db"
    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        migrations=load_default_migrations(),
    )
    payload: MemoryUpdateJobPayload = {
        "job_id": "job-memory-1",
        "process_id": "process-memory-1",
        "user_id": "user-1",
        "enqueued_at": "2026-08-09T00:03:00Z",
        "short_insight_ids": ["insight-1"],
        "summary_ids": [],
        "action_terminals": [],
    }
    enqueue_local_job_with_connection(
        db_path=str(db_path),
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        request=build_local_memory_update_enqueue_request(payload),
    )
    claimed = claim_next_pending_job(
        db_path=str(db_path),
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        job_type=LOCAL_MEMORY_UPDATE_JOB_TYPE,
        owner_user_id="user-1",
        claimed_by="worker-1",
        process_running_status="running",
        expected_process_pending_status="enqueued",
    )
    assert claimed is not None
    return payload, db_path


@pytest.mark.asyncio
async def test_acquires_one_lease_for_recover_complete_and_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _TrackingRuntime(tmp_path)
    events: list[str] = []

    def resolve_sources() -> tuple[tuple[MemorySource, str], ...]:
        assert runtime.active
        events.append("sources")
        return (("fact", "fact-1"),)

    def recover(**_kwargs: object) -> bool:
        assert runtime.active
        events.append("recover")
        return False

    async def is_complete() -> bool:
        assert runtime.active
        events.append("complete")
        return False

    async def run() -> None:
        assert runtime.active
        events.append("run")

    _stub_recovery(monkeypatch, recover)

    await run_memory_update_job(
        runtime=_runtime(runtime),
        user_id="user-1",
        job_id="job-1",
        process_id="process-1",
        job_type="test_memory_update",
        process_pending_status="enqueued",
        resolve_sources=resolve_sources,
        is_complete=is_complete,
        run=run,
    )

    assert runtime.acquire_calls == 1
    assert not runtime.active
    assert events == ["sources", "recover", "complete", "run"]


@pytest.mark.asyncio
async def test_lock_conflict_defers_for_fifteen_seconds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload, db_path = _claim_job(tmp_path)
    artifact_root = tmp_path / "artifacts"
    runtime = LocalMemoryFileEditorRuntime(
        artifact_root=artifact_root,
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
    )
    monkeypatch.setenv("LOCAL_DB_PATH", str(db_path))
    monkeypatch.setenv("LOCAL_DB_BUSY_TIMEOUT_MS", str(BUSY_TIMEOUT_MS))
    append_attempts = 0
    retry_delays: list[float] = []
    append_process_event = job_control_module.append_process_event_in_connection

    def _append_then_fail_once(
        *,
        connection: sqlite3.Connection,
        process_id: str,
        event_name: str,
        payload: dict[str, object],
        created_at: str,
        event_id: str | None = None,
    ) -> str:
        nonlocal append_attempts
        append_attempts += 1
        resolved_event_id = append_process_event(
            connection=connection,
            process_id=process_id,
            event_name=event_name,
            payload=payload,
            created_at=created_at,
            event_id=event_id,
        )
        if append_attempts == 1:
            raise sqlite3.OperationalError("database is locked")
        return resolved_event_id

    async def _record_retry_delay(delay: float) -> None:
        retry_delays.append(delay)

    monkeypatch.setattr(
        job_control_module,
        "append_process_event_in_connection",
        _append_then_fail_once,
    )
    monkeypatch.setattr(job_retry_module.asyncio, "sleep", _record_retry_delay)

    async def unexpected_run() -> None:
        raise AssertionError("lock conflict must defer before RUN")

    # Stored times are whole milliseconds, so compare against a floored start.
    before = parse_utc_iso(now_utc_iso())
    with runtime.acquire_user_update_lock(user_id="user-1", owner_id="other-job"):
        with pytest.raises(DeferredLocalJob):
            await run_memory_update_job(
                runtime=runtime,
                user_id="user-1",
                job_id=payload["job_id"],
                process_id=payload["process_id"],
                job_type=LOCAL_MEMORY_UPDATE_JOB_TYPE,
                process_pending_status="enqueued",
                resolve_sources=lambda: (("agent_experience", "user-1"),),
                is_complete=_never_complete,
                run=unexpected_run,
            )
    after = datetime.now(UTC)

    with sqlite3.connect(db_path) as connection:
        job_row = connection.execute(
            "SELECT status, scheduled_at FROM jobs WHERE job_id = ?",
            (payload["job_id"],),
        ).fetchone()
        process_row = connection.execute(
            "SELECT status, current_job_id FROM processes WHERE process_id = ?",
            (payload["process_id"],),
        ).fetchone()
        attempt_row = connection.execute(
            "SELECT status FROM job_attempts WHERE job_id = ?",
            (payload["job_id"],),
        ).fetchone()
        event_rows = connection.execute(
            """
            SELECT payload_json FROM process_events
            WHERE process_id = ? AND event_name = 'memory_update_lock_conflict_deferred'
            """,
            (payload["process_id"],),
        ).fetchall()

    assert job_row is not None
    scheduled_at = datetime.fromisoformat(str(job_row[1]).replace("Z", "+00:00"))
    assert (
        before.timestamp() + MEMORY_LOCK_RETRY_DELAY_SECONDS <= scheduled_at.timestamp()
    )
    assert (
        scheduled_at.timestamp() <= after.timestamp() + MEMORY_LOCK_RETRY_DELAY_SECONDS
    )
    assert job_row[0] == "queued"
    assert process_row == ("enqueued", None)
    assert attempt_row == ("completed",)
    assert append_attempts == 2
    assert retry_delays == [0.1]
    assert len(event_rows) == 1
    event_payload = json.loads(str(event_rows[0][0]))
    assert event_payload == {
        "exception_type": "MemoryUpdateLockConflictError",
        "job_id": payload["job_id"],
        "job_type": LOCAL_MEMORY_UPDATE_JOB_TYPE,
        "retry_delay_seconds": MEMORY_LOCK_RETRY_DELAY_SECONDS,
        "scheduled_at": job_row[1],
    }


@pytest.mark.parametrize(
    ("source", "source_record_id"),
    (
        ("fact", "fact-1"),
        ("long_term_insight", "user-1"),
        ("agent_experience", "user-1"),
    ),
)
@pytest.mark.asyncio
async def test_recovered_completion_skips_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: MemorySource,
    source_record_id: str,
) -> None:
    runtime = _TrackingRuntime(tmp_path)
    recovered = False

    def recover(**_kwargs: object) -> bool:
        nonlocal recovered
        assert runtime.active
        recovered = True
        return True

    async def is_complete() -> bool:
        assert recovered
        assert runtime.active
        return True

    async def unexpected_run() -> None:
        raise AssertionError("recovered completion must skip RUN")

    _stub_recovery(monkeypatch, recover)

    await run_memory_update_job(
        runtime=_runtime(runtime),
        user_id="user-1",
        job_id="job-1",
        process_id="process-1",
        job_type="test_memory_update",
        process_pending_status="enqueued",
        resolve_sources=lambda: ((source, source_record_id),),
        is_complete=is_complete,
        run=unexpected_run,
    )


@pytest.mark.asyncio
async def test_completed_replay_skips_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _TrackingRuntime(tmp_path)
    events: list[str] = []

    def recover(**_kwargs: object) -> bool:
        events.append("recover")
        return False

    async def is_complete() -> bool:
        events.append("complete")
        return True

    async def unexpected_run() -> None:
        raise AssertionError("completed replay must skip RUN")

    _stub_recovery(monkeypatch, recover)

    await run_memory_update_job(
        runtime=_runtime(runtime),
        user_id="user-1",
        job_id="job-1",
        process_id="process-1",
        job_type="test_memory_update",
        process_pending_status="enqueued",
        resolve_sources=lambda: (("fact", "fact-1"),),
        is_complete=is_complete,
        run=unexpected_run,
    )

    assert events == ["recover", "complete"]


@pytest.mark.asyncio
async def test_run_exception_after_durable_success_is_accepted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _TrackingRuntime(tmp_path)
    complete = False

    _stub_recovery(monkeypatch, lambda **_kwargs: False)

    async def is_complete() -> bool:
        return complete

    async def run() -> None:
        nonlocal complete
        complete = True
        raise OSError("response lost after durable commit")

    await run_memory_update_job(
        runtime=_runtime(runtime),
        user_id="user-1",
        job_id="job-1",
        process_id="process-1",
        job_type="test_memory_update",
        process_pending_status="enqueued",
        resolve_sources=lambda: (("fact", "fact-1"),),
        is_complete=is_complete,
        run=run,
    )


@pytest.mark.parametrize(
    "failure",
    (
        sqlite3.OperationalError("database is locked"),
        OSError(errno.ENOSPC, "disk full"),
        OSError(errno.EIO, "I/O error"),
    ),
)
@pytest.mark.asyncio
async def test_retryable_error_is_deferred(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    payload, db_path = _claim_job(tmp_path)
    runtime = LocalMemoryFileEditorRuntime(
        artifact_root=tmp_path / "artifacts",
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
    )
    _stub_recovery(monkeypatch, lambda **_kwargs: False)

    async def run() -> None:
        raise failure

    with pytest.raises(DeferredLocalJob):
        await run_memory_update_job(
            runtime=runtime,
            user_id="user-1",
            job_id=payload["job_id"],
            process_id=payload["process_id"],
            job_type=LOCAL_MEMORY_UPDATE_JOB_TYPE,
            process_pending_status="enqueued",
            resolve_sources=lambda: (("agent_experience", "user-1"),),
            is_complete=_never_complete,
            run=run,
        )

    with sqlite3.connect(db_path) as connection:
        job_row = connection.execute(
            "SELECT status FROM jobs WHERE job_id = ?", (payload["job_id"],)
        ).fetchone()
        process_row = connection.execute(
            "SELECT status, current_job_id FROM processes WHERE process_id = ?",
            (payload["process_id"],),
        ).fetchone()
    assert job_row == ("queued",)
    assert process_row == ("enqueued", None)


@pytest.mark.asyncio
async def test_non_retryable_error_is_reraised(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _TrackingRuntime(tmp_path)
    _stub_recovery(monkeypatch, lambda **_kwargs: False)

    async def run() -> None:
        raise RuntimeError("permanent failure")

    with pytest.raises(RuntimeError, match="permanent failure"):
        await run_memory_update_job(
            runtime=_runtime(runtime),
            user_id="user-1",
            job_id="job-1",
            process_id="process-1",
            job_type="test_memory_update",
            process_pending_status="enqueued",
            resolve_sources=lambda: (("fact", "fact-1"),),
            is_complete=_never_complete,
            run=run,
        )
