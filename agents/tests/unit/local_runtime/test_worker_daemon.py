from __future__ import annotations

import sqlite3
import threading
from collections.abc import Generator
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from pantaray_agents.local_runtime.runtime.action_messages import (
    ExistingActionTarget,
    NewActionTarget,
    SubmitActionMessageCommand,
    submit_action_message,
)
from pantaray_agents.local_runtime.runtime.db_execution_context import (
    get_local_runtime_db_execution_context,
)
from pantaray_agents.local_runtime.runtime.insight_queue import (
    build_local_insight_enqueue_request,
    build_short_insight_job_payload,
)
from pantaray_agents.local_runtime.runtime.job_capacity import LocalWorkerCapacity
from pantaray_agents.local_runtime.runtime.job_claim import finalize_local_job
from pantaray_agents.local_runtime.runtime.job_enqueue import (
    enqueue_local_job_with_connection,
)
from pantaray_agents.local_runtime.runtime.job_payload_builder import (
    build_memory_update_job_payload,
)
from pantaray_agents.local_runtime.runtime.local_worker import LocalWorkerSpec
from pantaray_agents.local_runtime.runtime.memory_embedding_scheduler import (
    MemoryEmbeddingProjectionResult,
)
from pantaray_agents.local_runtime.runtime.memory_update_queue import (
    build_local_memory_update_enqueue_request,
)
from pantaray_agents.local_runtime.runtime.process_lock import (
    acquire_runtime_process_lock,
    release_runtime_process_lock,
)
from pantaray_agents.local_runtime.runtime.session_store import (
    import_desktop_session,
    mark_configured,
    reset_desktop_session_store,
)
from pantaray_agents.local_runtime.runtime.worker_daemon import (
    stop_local_action_worker_daemon,
)
from pantaray_agents.local_runtime.storage.migrations import (
    MigrationError,
    load_default_migrations,
)
from pantaray_agents.schema.agent.action import ActionUserMessageInput
from pantaray_agents.tasks.types import ActionJobRuntimePayload

from .migrated_db import prepare_test_database
from .worker_daemon_test_support import (
    set_minimum_local_runtime_env,
    start_worker,
)


@pytest.fixture(autouse=True)
def _reset_session_store() -> Generator[None]:
    reset_desktop_session_store()
    yield
    reset_desktop_session_store()


@pytest.fixture(autouse=True)
def _reset_worker_daemon() -> Generator[None]:
    stop_local_action_worker_daemon()
    yield
    stop_local_action_worker_daemon()


def _insert_user(db_path: Path, *, user_id: str) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO users(
                user_id,
                ui_language,
                created_at,
                updated_at
            ) VALUES (?, 'ja', '2026-03-22T00:00:00Z', '2026-03-22T00:00:00Z')
            """,
            (user_id,),
        )


def _activate_session(db_path: Path, *, user_id: str) -> None:
    import_desktop_session(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id=user_id,
        desktop_access_token="header.payload.signature",
        expires_at="2099-03-27T01:00:00Z",
        session_version="1",
    )
    # The worker only claims once Electron main has applied `configure`.
    mark_configured()


def test_worker_daemon_polls_memory_agent_triggers_for_active_user(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(db_path, 1_000, load_default_migrations())
    set_minimum_local_runtime_env(
        monkeypatch=monkeypatch,
        db_path=db_path,
        tmp_path=tmp_path,
    )
    _activate_session(db_path, user_id="user-daemon")
    dispatched = threading.Event()

    def _repair(**_kwargs: object) -> None:
        return None

    def _dispatch(**kwargs: object) -> None:
        assert kwargs["db_path"] == db_path
        assert kwargs["user_id"] == "user-daemon"
        dispatched.set()

    async def _run_activity_summary(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(
        "pantaray_agents.local_runtime.runtime.periodic_schedule.dispatch_memory_agent_triggers_once",
        _dispatch,
    )
    monkeypatch.setattr(
        "pantaray_agents.local_runtime.runtime.periodic_schedule.run_activity_summary_scheduler_once",
        _run_activity_summary,
    )
    monkeypatch.setattr(
        "pantaray_agents.local_runtime.runtime.periodic_schedule.run_memory_catalog_repair_once",
        _repair,
    )

    start_worker()
    try:
        assert dispatched.wait(timeout=2.0) is True
    finally:
        stop_local_action_worker_daemon()


def test_worker_daemon_withholds_owner_scoped_periodic_work_until_configure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The worker ticks before Electron main can answer who the user is.

    ``lifecycle.start_local_runtime_if_enabled`` starts this daemon before the
    control socket even listens, so the first tick always precedes ``configure``.
    A job enqueued then is owned by the logged-out owner, and
    ``claim_next_pending_job`` selects by ``user_id``, so it is never claimed
    again -- while the summary cursor it advanced is spent all the same.
    """
    from pantaray_agents.local_runtime.runtime import periodic_schedule
    from pantaray_agents.local_runtime.runtime.identity import current_owner_id

    db_path = tmp_path / "runtime.db"
    prepare_test_database(db_path, 1_000, load_default_migrations())
    set_minimum_local_runtime_env(
        monkeypatch=monkeypatch,
        db_path=db_path,
        tmp_path=tmp_path,
    )
    _insert_user(db_path, user_id="user-daemon")

    ticked_twice = threading.Event()
    dispatched = threading.Event()
    owner_scoped_calls: list[tuple[str, str]] = []
    tick_count = 0

    def _reaper(**_kwargs: object) -> int:
        nonlocal tick_count
        tick_count += 1
        if tick_count >= 2:
            ticked_twice.set()
        return 0

    async def _run_activity_summary(**_kwargs: object) -> None:
        owner_scoped_calls.append(("activity_summary", current_owner_id()))

    def _dispatch(*, user_id: str, **_kwargs: object) -> None:
        owner_scoped_calls.append(("memory_agent_dispatch", user_id))
        dispatched.set()

    monkeypatch.setattr(
        periodic_schedule,
        "LOCAL_RUNTIME_PERIODIC_REAPER_INTERVAL_SECONDS",
        0.0,
    )
    monkeypatch.setattr(periodic_schedule, "run_local_periodic_reaper_once", _reaper)
    monkeypatch.setattr(
        periodic_schedule,
        "run_activity_summary_scheduler_once",
        _run_activity_summary,
    )
    monkeypatch.setattr(
        periodic_schedule, "dispatch_memory_agent_triggers_once", _dispatch
    )
    monkeypatch.setattr(
        periodic_schedule, "run_memory_catalog_repair_once", lambda **_kwargs: None
    )
    monkeypatch.setattr(
        periodic_schedule,
        "run_memory_embedding_projection",
        lambda **_kwargs: MemoryEmbeddingProjectionResult(0, 0, 0),
    )

    start_worker()
    try:
        # The reaper takes no owner, so it keeps recovering runtime locks while
        # the helper waits to be told who the user is. Its second run proves the
        # owner-scoped tasks of the first tick had their chance: within a tick
        # they run after it.
        assert ticked_twice.wait(timeout=2.0) is True
        assert owner_scoped_calls == []

        _activate_session(db_path, user_id="user-daemon")

        assert dispatched.wait(timeout=2.0) is True
        assert owner_scoped_calls[0] == ("activity_summary", "user-daemon")
        assert ("memory_agent_dispatch", "user-daemon") in owner_scoped_calls
    finally:
        stop_local_action_worker_daemon()


def test_local_action_worker_daemon_executes_pending_job(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=load_default_migrations(),
    )
    set_minimum_local_runtime_env(
        monkeypatch=monkeypatch,
        db_path=db_path,
        tmp_path=tmp_path,
    )
    _insert_user(db_path, user_id="user-daemon")
    _activate_session(db_path, user_id="user-daemon")
    executed = threading.Event()

    def _fake_runner(job_payload: object) -> None:
        assert isinstance(job_payload, dict)
        assert job_payload["user_id"] == "user-daemon"
        finalize_local_job(
            db_path=str(db_path),
            busy_timeout_ms=1_000,
            job_id=str(job_payload["job_id"]),
            final_status="completed",
            process_final_status="completed",
        )
        executed.set()

    monkeypatch.setattr(
        "pantaray_agents.local_runtime.runtime.worker_daemon._run_action_job_runner",
        _fake_runner,
    )

    start_worker()
    try:
        created = submit_action_message(
            SubmitActionMessageCommand(
                user_id="user-daemon",
                target=NewActionTarget(),
                message=ActionUserMessageInput(
                    message_id="command-daemon",
                    content="Run daemon action",
                    language="ja",
                ),
            )
        )
        assert executed.wait(timeout=2.0) is True
    finally:
        stop_local_action_worker_daemon()

    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT status FROM jobs WHERE job_id = ?",
            (created.job_id,),
        ).fetchone()
    assert row is not None
    assert row[0] == "completed"


def test_worker_daemon_runs_memory_projection_only_while_idle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=load_default_migrations(),
    )
    set_minimum_local_runtime_env(
        monkeypatch=monkeypatch,
        db_path=db_path,
        tmp_path=tmp_path,
    )
    _insert_user(db_path, user_id="user-daemon")
    _activate_session(db_path, user_id="user-daemon")
    projection_ran = threading.Event()

    def _run_projection(**kwargs: object) -> MemoryEmbeddingProjectionResult:
        assert kwargs["db_path"] == db_path
        assert kwargs["busy_timeout_ms"] == 1_000
        assert kwargs["user_id"] == "user-daemon"
        projection_ran.set()
        return MemoryEmbeddingProjectionResult(0, 0, 0)

    monkeypatch.setattr(
        "pantaray_agents.local_runtime.runtime.periodic_schedule.run_memory_embedding_projection",
        _run_projection,
    )

    start_worker()
    try:
        assert projection_ran.wait(timeout=2.0) is True
    finally:
        stop_local_action_worker_daemon()


def test_worker_daemon_keeps_runtime_lock_until_projection_finishes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=load_default_migrations(),
    )
    set_minimum_local_runtime_env(
        monkeypatch=monkeypatch,
        db_path=db_path,
        tmp_path=tmp_path,
    )
    _insert_user(db_path, user_id="user-daemon")
    _activate_session(db_path, user_id="user-daemon")
    projection_started = threading.Event()
    release_projection = threading.Event()
    projection_consumed = threading.Event()

    def _run_projection(**_kwargs: object) -> MemoryEmbeddingProjectionResult:
        projection_started.set()
        assert release_projection.wait(timeout=5.0) is True
        return MemoryEmbeddingProjectionResult(0, 0, 0)

    def _consume_projection(
        future: Future[MemoryEmbeddingProjectionResult],
    ) -> None:
        result = future.result()
        assert result == MemoryEmbeddingProjectionResult(0, 0, 0)
        projection_consumed.set()

    monkeypatch.setattr(
        "pantaray_agents.local_runtime.runtime.periodic_schedule.run_memory_embedding_projection",
        _run_projection,
    )
    monkeypatch.setattr(
        "pantaray_agents.local_runtime.runtime.worker_daemon._consume_memory_embedding_projection_result",
        _consume_projection,
    )

    start_worker()
    assert projection_started.wait(timeout=2.0) is True
    reset_desktop_session_store()
    stop_local_action_worker_daemon()
    with pytest.raises(MigrationError, match="LOCAL_RUNTIME_ALREADY_ACTIVE"):
        acquire_runtime_process_lock(db_path=db_path)

    release_projection.set()
    assert projection_consumed.wait(timeout=2.0) is True
    stop_local_action_worker_daemon()
    release_runtime_process_lock(
        runtime_lock=acquire_runtime_process_lock(db_path=db_path)
    )


def test_worker_drain_does_not_run_periodic_tasks_after_stop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from pantaray_agents.local_runtime.runtime import periodic_schedule

    db_path = tmp_path / "runtime.db"
    prepare_test_database(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=load_default_migrations(),
    )
    set_minimum_local_runtime_env(
        monkeypatch=monkeypatch,
        db_path=db_path,
        tmp_path=tmp_path,
    )
    _insert_user(db_path, user_id="user-daemon")
    _activate_session(db_path, user_id="user-daemon")
    periodic_ran = threading.Event()
    action_started = threading.Event()
    release_action = threading.Event()
    periodic_run_count = 0

    def run_reaper(**_kwargs: object) -> int:
        nonlocal periodic_run_count
        periodic_run_count += 1
        periodic_ran.set()
        return 0

    async def run_activity_summary(**_kwargs: object) -> None:
        return None

    def run_action(_payload: object) -> None:
        action_started.set()
        assert release_action.wait(timeout=5.0) is True

    monkeypatch.setattr(
        periodic_schedule,
        "LOCAL_RUNTIME_PERIODIC_REAPER_INTERVAL_SECONDS",
        0.0,
    )
    monkeypatch.setattr(
        periodic_schedule,
        "run_local_periodic_reaper_once",
        run_reaper,
    )
    monkeypatch.setattr(
        periodic_schedule,
        "run_activity_summary_scheduler_once",
        run_activity_summary,
    )
    monkeypatch.setattr(
        periodic_schedule,
        "run_memory_catalog_repair_once",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        periodic_schedule,
        "dispatch_memory_agent_triggers_once",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        periodic_schedule,
        "run_memory_embedding_projection",
        lambda **_kwargs: MemoryEmbeddingProjectionResult(0, 0, 0),
    )
    monkeypatch.setattr(
        "pantaray_agents.local_runtime.runtime.worker_daemon._run_action_job_runner",
        run_action,
    )

    start_worker()
    try:
        submit_action_message(
            SubmitActionMessageCommand(
                user_id="user-daemon",
                target=NewActionTarget(),
                message=ActionUserMessageInput(
                    message_id="command-stop-drain",
                    content="Keep the worker draining",
                    language="ja",
                ),
            )
        )
        assert action_started.wait(timeout=2.0) is True
        assert periodic_ran.wait(timeout=2.0) is True

        stop_local_action_worker_daemon()
        count_after_stop = periodic_run_count
        threading.Event().wait(timeout=0.25)

        assert periodic_run_count == count_after_stop
    finally:
        release_action.set()
        stop_local_action_worker_daemon()


def test_worker_daemon_runs_insight_while_memory_update_job_is_active(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=load_default_migrations(),
    )
    set_minimum_local_runtime_env(
        monkeypatch=monkeypatch,
        db_path=db_path,
        tmp_path=tmp_path,
    )
    _insert_user(db_path, user_id="user-daemon")
    _activate_session(db_path, user_id="user-daemon")
    memory_started = threading.Event()
    release_memory = threading.Event()
    insight_executed = threading.Event()

    def _run_memory_update(_payload: object) -> None:
        context = get_local_runtime_db_execution_context()
        assert context is not None
        assert context.db_path == db_path
        memory_started.set()
        assert release_memory.wait(timeout=5.0) is True

    def _run_insight(_payload: object) -> None:
        context = get_local_runtime_db_execution_context()
        assert context is not None
        assert context.db_path == db_path
        assert memory_started.is_set()
        assert not release_memory.is_set()
        insight_executed.set()

    monkeypatch.setattr(
        "pantaray_agents.local_runtime.runtime.local_worker.run_memory_update",
        _run_memory_update,
    )
    monkeypatch.setattr(
        "pantaray_agents.local_runtime.runtime.local_worker.run_insight_job",
        _run_insight,
    )

    start_worker()
    try:
        memory_payload = build_memory_update_job_payload(
            {
                "job_id": "memory-job",
                "process_id": "memory-process",
                "user_id": "user-daemon",
                "enqueued_at": "2026-03-22T00:00:01Z",
                "short_insight_ids": ["insight-1"],
                "summary_ids": [],
                "action_terminals": [],
            }
        )
        memory_result = enqueue_local_job_with_connection(
            request=build_local_memory_update_enqueue_request(memory_payload),
            db_path=str(db_path),
            busy_timeout_ms=1_000,
        )
        assert memory_started.wait(timeout=2.0) is True
        insight_payload = build_short_insight_job_payload(
            user_id="user-daemon",
            window_start=datetime(2026, 3, 22, tzinfo=UTC),
        )
        insight_result = enqueue_local_job_with_connection(
            request=build_local_insight_enqueue_request(insight_payload),
            db_path=str(db_path),
            busy_timeout_ms=1_000,
        )
        assert insight_executed.wait(timeout=2.0) is True
    finally:
        release_memory.set()
        stop_local_action_worker_daemon()

    with sqlite3.connect(db_path) as connection:
        statuses = dict(
            connection.execute("SELECT job_id, status FROM jobs").fetchall()
        )
    assert statuses[memory_result["job_id"]] == "completed"
    assert statuses[insight_result["job_id"]] == "completed"


def test_worker_daemon_runs_general_job_while_action_job_is_active(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=load_default_migrations(),
    )
    set_minimum_local_runtime_env(
        monkeypatch=monkeypatch,
        db_path=db_path,
        tmp_path=tmp_path,
    )
    _insert_user(db_path, user_id="user-daemon")
    _activate_session(db_path, user_id="user-daemon")
    action_started = threading.Event()
    release_action = threading.Event()
    insight_executed = threading.Event()

    def _run_action(_payload: object) -> None:
        action_started.set()
        assert release_action.wait(timeout=5.0) is True

    def _run_insight(_payload: object) -> None:
        assert action_started.is_set()
        assert not release_action.is_set()
        insight_executed.set()

    monkeypatch.setattr(
        "pantaray_agents.local_runtime.runtime.worker_daemon._run_action_job_runner",
        _run_action,
    )
    monkeypatch.setattr(
        "pantaray_agents.local_runtime.runtime.local_worker.run_insight_job",
        _run_insight,
    )

    start_worker()
    try:
        submit_action_message(
            SubmitActionMessageCommand(
                user_id="user-daemon",
                target=NewActionTarget(),
                message=ActionUserMessageInput(
                    message_id="command-general-capacity",
                    content="Run alongside a general worker job",
                    language="ja",
                ),
            )
        )
        assert action_started.wait(timeout=2.0) is True
        enqueue_local_job_with_connection(
            request=build_local_insight_enqueue_request(
                build_short_insight_job_payload(
                    user_id="user-daemon",
                    window_start=datetime(2026, 3, 22, tzinfo=UTC),
                )
            ),
            db_path=str(db_path),
            busy_timeout_ms=1_000,
        )
        assert insight_executed.wait(timeout=2.0) is True
    finally:
        release_action.set()
        stop_local_action_worker_daemon()


def test_worker_daemon_runs_actions_concurrently_and_one_turn_per_action(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    action_count = 5
    db_path = tmp_path / "runtime.db"
    prepare_test_database(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=load_default_migrations(),
    )
    set_minimum_local_runtime_env(
        monkeypatch=monkeypatch,
        db_path=db_path,
        tmp_path=tmp_path,
    )
    _insert_user(db_path, user_id="user-daemon")
    _activate_session(db_path, user_id="user-daemon")
    lock = threading.Lock()
    started_action_ids: set[str] = set()
    running_by_action: dict[str, int] = {}
    max_running_by_action: dict[str, int] = {}
    all_started = threading.Event()
    release_actions = threading.Event()

    def _run_action(payload: object) -> None:
        action_id = cast(ActionJobRuntimePayload, payload)["action_id"]
        with lock:
            running = running_by_action.get(action_id, 0) + 1
            running_by_action[action_id] = running
            max_running_by_action[action_id] = max(
                running, max_running_by_action.get(action_id, 0)
            )
            started_action_ids.add(action_id)
            if len(started_action_ids) == action_count:
                all_started.set()
        try:
            # No runner returns before release, so all_started proves overlap.
            release_actions.wait(timeout=5.0)
        finally:
            with lock:
                running_by_action[action_id] -= 1

    monkeypatch.setattr(
        "pantaray_agents.local_runtime.runtime.worker_daemon._run_action_job_runner",
        _run_action,
    )

    start_worker()
    try:
        results = [
            submit_action_message(
                SubmitActionMessageCommand(
                    user_id="user-daemon",
                    target=NewActionTarget(),
                    message=ActionUserMessageInput(
                        message_id=f"command-concurrent-{index}",
                        content="Run alongside other Actions",
                        language="ja",
                    ),
                )
            )
            for index in range(action_count)
        ]
        assert all_started.wait(timeout=5.0) is True

        first = results[0]
        assert first.process_id is not None
        follow_up = submit_action_message(
            SubmitActionMessageCommand(
                user_id="user-daemon",
                target=ExistingActionTarget(
                    action_id=first.action_id,
                    expected_process_id=first.process_id,
                ),
                message=ActionUserMessageInput(
                    message_id="command-concurrent-follow-up",
                    content="Follow up while the first turn runs",
                    language="ja",
                ),
            )
        )
        assert follow_up.disposition == "pending"
        with sqlite3.connect(db_path) as connection:
            active_jobs = connection.execute(
                """
                SELECT COUNT(*) FROM jobs
                WHERE job_type = 'execute_action' AND logical_key = ?
                  AND status IN ('queued', 'running', 'paused', 'retryable_error')
                """,
                (first.action_id,),
            ).fetchone()
        assert active_jobs == (1,)
    finally:
        release_actions.set()
        stop_local_action_worker_daemon()

    assert started_action_ids == {result.action_id for result in results}
    assert set(max_running_by_action.values()) == {1}


def test_submit_next_reserved_job_releases_capacity_when_claim_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from pantaray_agents.local_runtime.runtime import worker_daemon

    capacity = LocalWorkerCapacity(
        max_running=1,
        job_type_limits={"structure_facts": 1},
    )
    specs = {
        "structure_facts": LocalWorkerSpec(
            job_type="structure_facts",
            parse_payload=lambda payload_json: {"payload_json": payload_json},
            run=lambda _payload: None,
            process_pending_status="enqueued",
            process_running_status="running",
            process_success_status="success",
            process_failure_status="error",
            success_job_status="completed",
            failure_job_status="failed",
        )
    }
    monkeypatch.setattr(
        worker_daemon,
        "_select_next_pending_job_type",
        lambda **_kwargs: "structure_facts",
    )

    def _raise_claim_error(**_kwargs: object) -> None:
        raise RuntimeError("claim boom")

    monkeypatch.setattr(worker_daemon, "_claim_reserved_job", _raise_claim_error)

    with ThreadPoolExecutor(max_workers=1) as executor:
        submitted = worker_daemon._submit_next_reserved_job(
            db_path=tmp_path / "runtime.db",
            busy_timeout_ms=1_000,
            executor=executor,
            specs=specs,
            capacity=capacity,
            owner_user_id="user-1",
        )

    assert submitted is False
    assert capacity.active_count() == 0


def test_post_queue_submit_failure_stops_worker_without_requeue(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from pantaray_agents.local_runtime.runtime import worker_daemon

    shutdowns: dict[str, tuple[bool, bool, int]] = {}
    executors: list[_PostQueueFailureExecutor] = []
    released_locks: list[object] = []

    class _PostQueueFailureExecutor(ThreadPoolExecutor):
        def __init__(self, *, max_workers: int, thread_name_prefix: str = "") -> None:
            super().__init__(
                max_workers=max_workers,
                thread_name_prefix=thread_name_prefix,
            )
            self._test_name = thread_name_prefix
            executors.append(self)

        def _adjust_thread_count(self) -> None:
            raise RuntimeError("thread start failed after queue insertion")

        def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
            shutdowns[self._test_name] = (
                wait,
                cancel_futures,
                self._work_queue.qsize(),
            )
            super().shutdown(wait=wait, cancel_futures=cancel_futures)

    claimed_job = {
        "job_id": "job-rejected",
        "job_type": "execute_action",
        "user_id": "user-1",
        "process_id": "process-rejected",
        "claimed_by": "local-worker-daemon",
        "payload_json": "{}",
    }
    monkeypatch.setattr(worker_daemon, "ThreadPoolExecutor", _PostQueueFailureExecutor)
    monkeypatch.setattr(worker_daemon, "current_owner_id", lambda: "user-1")
    monkeypatch.setattr(worker_daemon, "claimable_specs", lambda specs: specs)

    def _select_job(**kwargs: object) -> str:
        assert kwargs["owner_user_id"] == "user-1"
        return "execute_action"

    def _claim_job(**kwargs: object) -> object:
        assert kwargs["owner_user_id"] == "user-1"
        return claimed_job

    monkeypatch.setattr(worker_daemon, "_select_next_pending_job_type", _select_job)
    monkeypatch.setattr(worker_daemon, "_claim_reserved_job", _claim_job)
    monkeypatch.setattr(
        worker_daemon,
        "_release_runtime_lock_after_worker_exit",
        lambda **kwargs: released_locks.append(kwargs["lease"]),
    )
    runtime_lock = object()
    worker_daemon._STOP_EVENT.clear()

    with pytest.raises(
        worker_daemon._WorkerDaemonFatalError,
        match="submit acceptance is unknown",
    ):
        worker_daemon._worker_loop(
            db_path=tmp_path / "runtime.db",
            busy_timeout_ms=1_000,
            artifact_root=tmp_path / "artifacts",
            runtime_process_lock=runtime_lock,  # type: ignore[arg-type]
        )

    assert len(executors) == 3
    assert shutdowns["local-action-worker"] == (True, False, 1)
    assert shutdowns["local-general-worker"] == (True, False, 0)
    assert shutdowns["local-memory-embedding-worker"] == (True, False, 0)
    assert released_locks == [runtime_lock]


def test_worker_idle_sleep_does_not_wait_on_set_stop_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pantaray_agents.local_runtime.runtime import worker_daemon

    sleeps: list[float] = []
    monkeypatch.setattr(
        worker_daemon.time, "sleep", lambda seconds: sleeps.append(seconds)
    )
    worker_daemon._STOP_EVENT.set()
    try:
        worker_daemon._sleep_worker_idle()
    finally:
        worker_daemon._STOP_EVENT.clear()

    assert sleeps == [0.1]
