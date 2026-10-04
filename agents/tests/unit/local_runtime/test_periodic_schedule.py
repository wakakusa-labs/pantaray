from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.runtime.admission import admission_closed
from pantaray_agents.local_runtime.runtime.memory_embedding_scheduler import (
    MemoryEmbeddingProjectionResult,
)
from pantaray_agents.local_runtime.runtime.periodic_schedule import (
    MemoryEmbeddingProjectionSlot,
    PeriodicTask,
    PeriodicTaskContext,
    build_periodic_schedule,
    run_due_periodic_tasks,
)
from pantaray_agents.local_runtime.runtime.session_store import mark_configured

# Every task that takes the owner waits for `configure`; the autouse fixture in
# `tests/conftest.py` puts the store back to unconfigured after each test.
OWNER_SCOPED_TASK_NAMES = (
    "activity_summary",
    "short_insight",
    "memory_agent_dispatch",
    "suggestion_release",
    "memory_embedding_projection",
)


@pytest.fixture
def context(tmp_path: Path) -> PeriodicTaskContext:
    return PeriodicTaskContext(
        db_path=tmp_path / "runtime.db",
        busy_timeout_ms=1_000,
        artifact_root=tmp_path / "artifacts",
        owner_user_id="user-1",
        worker_is_idle=True,
    )


@pytest.fixture
def configured() -> None:
    mark_configured()


@pytest.fixture
def schedule_and_slot() -> Generator[
    tuple[tuple[PeriodicTask, ...], MemoryEmbeddingProjectionSlot]
]:
    with ThreadPoolExecutor(max_workers=1) as executor:
        slot = MemoryEmbeddingProjectionSlot(executor, threading.Event())
        yield build_periodic_schedule(embedding_slot=slot), slot


def _task(
    name: str,
    *,
    run: Callable[[PeriodicTaskContext], None],
    is_ready: Callable[[PeriodicTaskContext], bool] = lambda _context: True,
    interval_seconds: float = 30.0,
) -> PeriodicTask:
    return PeriodicTask(
        name=name,
        interval_seconds=interval_seconds,
        is_ready=is_ready,
        run=run,
    )


def test_reaper_task_runs_from_schedule(
    monkeypatch: pytest.MonkeyPatch,
    context: PeriodicTaskContext,
    schedule_and_slot: tuple[tuple[PeriodicTask, ...], MemoryEmbeddingProjectionSlot],
) -> None:
    from pantaray_agents.local_runtime.runtime import periodic_schedule

    calls: list[tuple[Path, int]] = []
    monkeypatch.setattr(
        periodic_schedule,
        "run_local_periodic_reaper_once",
        lambda *, db_path, busy_timeout_ms: calls.append((db_path, busy_timeout_ms)),
    )
    schedule, _slot = schedule_and_slot
    tasks = tuple(task for task in schedule if task.name == "reaper")

    next_runs = run_due_periodic_tasks(
        tasks=tasks,
        context=context,
        next_run_monotonic_by_task={task.name: 0.0 for task in tasks},
        now_monotonic=10.0,
    )

    assert calls == [(context.db_path, context.busy_timeout_ms)]
    assert next_runs == {"reaper": 40.0}


def test_activity_summary_task_runs_from_schedule(
    monkeypatch: pytest.MonkeyPatch,
    context: PeriodicTaskContext,
    configured: None,
    schedule_and_slot: tuple[tuple[PeriodicTask, ...], MemoryEmbeddingProjectionSlot],
) -> None:
    from pantaray_agents.local_runtime.runtime import periodic_schedule

    calls: list[tuple[Path, int]] = []

    async def run_activity_summary(*, db_path: Path, busy_timeout_ms: int) -> None:
        calls.append((db_path, busy_timeout_ms))

    monkeypatch.setattr(
        periodic_schedule,
        "run_activity_summary_scheduler_once",
        run_activity_summary,
    )
    schedule, _slot = schedule_and_slot
    tasks = tuple(task for task in schedule if task.name == "activity_summary")

    next_runs = run_due_periodic_tasks(
        tasks=tasks,
        context=context,
        next_run_monotonic_by_task={"activity_summary": 0.0},
        now_monotonic=10.0,
    )

    assert calls == [(context.db_path, context.busy_timeout_ms)]
    assert next_runs == {"activity_summary": 40.0}


def test_memory_repair_task_runs_from_schedule(
    monkeypatch: pytest.MonkeyPatch,
    context: PeriodicTaskContext,
    schedule_and_slot: tuple[tuple[PeriodicTask, ...], MemoryEmbeddingProjectionSlot],
) -> None:
    from pantaray_agents.local_runtime.runtime import periodic_schedule

    calls: list[tuple[Path, int, Path]] = []
    monkeypatch.setattr(
        periodic_schedule,
        "run_memory_catalog_repair_once",
        lambda *, db_path, busy_timeout_ms, artifact_root: calls.append(
            (db_path, busy_timeout_ms, artifact_root)
        ),
    )
    schedule, _slot = schedule_and_slot
    tasks = tuple(task for task in schedule if task.name == "memory_repair")

    next_runs = run_due_periodic_tasks(
        tasks=tasks,
        context=context,
        next_run_monotonic_by_task={"memory_repair": 0.0},
        now_monotonic=10.0,
    )

    assert calls == [(context.db_path, context.busy_timeout_ms, context.artifact_root)]
    assert next_runs == {"memory_repair": 70.0}


def test_memory_agent_dispatch_task_runs_for_the_current_owner(
    monkeypatch: pytest.MonkeyPatch,
    context: PeriodicTaskContext,
    configured: None,
    schedule_and_slot: tuple[tuple[PeriodicTask, ...], MemoryEmbeddingProjectionSlot],
) -> None:
    from pantaray_agents.local_runtime.runtime import periodic_schedule

    calls: list[tuple[Path, int, str]] = []
    monkeypatch.setattr(
        periodic_schedule,
        "dispatch_memory_agent_triggers_once",
        lambda *, db_path, busy_timeout_ms, user_id: calls.append(
            (db_path, busy_timeout_ms, user_id)
        ),
    )
    schedule, _slot = schedule_and_slot
    tasks = tuple(task for task in schedule if task.name == "memory_agent_dispatch")

    next_runs = run_due_periodic_tasks(
        tasks=tasks,
        context=context,
        next_run_monotonic_by_task={"memory_agent_dispatch": 0.0},
        now_monotonic=11.0,
    )

    assert calls == [(context.db_path, context.busy_timeout_ms, context.owner_user_id)]
    assert next_runs == {"memory_agent_dispatch": 41.0}


def test_embedding_task_submits_while_the_worker_is_idle(
    monkeypatch: pytest.MonkeyPatch,
    context: PeriodicTaskContext,
    configured: None,
    schedule_and_slot: tuple[tuple[PeriodicTask, ...], MemoryEmbeddingProjectionSlot],
) -> None:
    from pantaray_agents.local_runtime.runtime import periodic_schedule

    projection_started = threading.Event()
    release_projection = threading.Event()
    calls: list[tuple[Path, int, str]] = []

    def run_projection(
        *,
        db_path: Path,
        busy_timeout_ms: int,
        user_id: str,
        stop_event: threading.Event,
    ) -> MemoryEmbeddingProjectionResult:
        calls.append((db_path, busy_timeout_ms, user_id))
        projection_started.set()
        assert release_projection.wait(timeout=5.0) is True
        return MemoryEmbeddingProjectionResult(0, 0, 0)

    monkeypatch.setattr(
        periodic_schedule,
        "run_memory_embedding_projection",
        run_projection,
    )
    schedule, slot = schedule_and_slot
    tasks = tuple(
        task for task in schedule if task.name == "memory_embedding_projection"
    )
    busy_worker_context = replace(context, worker_is_idle=False)

    try:
        # Embedding runs against the bundled model, so a signed-out owner is
        # indexed like any other.
        busy_worker_next_runs = run_due_periodic_tasks(
            tasks=tasks,
            context=busy_worker_context,
            next_run_monotonic_by_task={"memory_embedding_projection": 0.0},
            now_monotonic=11.0,
        )
        submitted_next_runs = run_due_periodic_tasks(
            tasks=tasks,
            context=context,
            next_run_monotonic_by_task=busy_worker_next_runs,
            now_monotonic=12.0,
        )
        assert projection_started.wait(timeout=2.0) is True
        in_flight_next_runs = run_due_periodic_tasks(
            tasks=tasks,
            context=context,
            next_run_monotonic_by_task=submitted_next_runs,
            now_monotonic=13.0,
        )

        assert busy_worker_next_runs == {"memory_embedding_projection": 0.0}
        assert submitted_next_runs == {"memory_embedding_projection": 13.0}
        assert in_flight_next_runs == {"memory_embedding_projection": 13.0}
        assert calls == [
            (context.db_path, context.busy_timeout_ms, context.owner_user_id)
        ]
        assert slot.is_busy() is True
    finally:
        release_projection.set()


def test_embedding_slot_returns_only_completed_future(
    monkeypatch: pytest.MonkeyPatch,
    context: PeriodicTaskContext,
) -> None:
    from pantaray_agents.local_runtime.runtime import periodic_schedule

    projection_started = threading.Event()
    release_projection = threading.Event()
    result = MemoryEmbeddingProjectionResult(1, 1, 0)

    def run_projection(**_kwargs: object) -> MemoryEmbeddingProjectionResult:
        projection_started.set()
        assert release_projection.wait(timeout=5.0) is True
        return result

    monkeypatch.setattr(
        periodic_schedule,
        "run_memory_embedding_projection",
        run_projection,
    )
    executor = ThreadPoolExecutor(max_workers=1)
    slot = MemoryEmbeddingProjectionSlot(executor, threading.Event())
    try:
        slot.submit(context)
        assert projection_started.wait(timeout=2.0) is True
        assert slot.consume_if_done() is None

        release_projection.set()
        executor.shutdown(wait=True)
        future = slot.consume_if_done()

        assert future is not None
        assert future.result() == result
        assert slot.is_busy() is False
    finally:
        release_projection.set()
        executor.shutdown(wait=True)


def test_embedding_submit_failure_advances_deadline(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    context: PeriodicTaskContext,
    configured: None,
) -> None:

    executor = ThreadPoolExecutor(max_workers=1)
    executor.shutdown(wait=True)
    slot = MemoryEmbeddingProjectionSlot(executor, threading.Event())
    tasks = tuple(
        task
        for task in build_periodic_schedule(embedding_slot=slot)
        if task.name == "memory_embedding_projection"
    )

    with caplog.at_level(
        logging.ERROR,
        logger="pantaray_agents.local_runtime.runtime.periodic_schedule",
    ):
        next_runs = run_due_periodic_tasks(
            tasks=tasks,
            context=context,
            next_run_monotonic_by_task={"memory_embedding_projection": 0.0},
            now_monotonic=10.0,
        )

    assert next_runs == {"memory_embedding_projection": 11.0}
    assert slot.is_busy() is False
    assert len(caplog.records) == 1
    assert (
        caplog.records[0].getMessage()
        == "periodic task failed: task=memory_embedding_projection"
    )


def _record_every_periodic_runner(
    monkeypatch: pytest.MonkeyPatch, calls: list[str]
) -> None:
    """Replace every runner in the real schedule with one that only records."""
    from pantaray_agents.local_runtime.runtime import periodic_schedule

    def record(name: str) -> Callable[..., None]:
        def run(**_kwargs: object) -> None:
            calls.append(name)

        return run

    def record_async(name: str) -> Callable[..., object]:
        async def run(**_kwargs: object) -> None:
            calls.append(name)

        return run

    def run_projection(**_kwargs: object) -> MemoryEmbeddingProjectionResult:
        calls.append("memory_embedding_projection")
        return MemoryEmbeddingProjectionResult(0, 0, 0)

    monkeypatch.setattr(
        periodic_schedule, "run_local_periodic_reaper_once", record("reaper")
    )
    monkeypatch.setattr(
        periodic_schedule,
        "run_activity_summary_scheduler_once",
        record_async("activity_summary"),
    )
    monkeypatch.setattr(
        periodic_schedule, "run_memory_catalog_repair_once", record("memory_repair")
    )
    monkeypatch.setattr(
        periodic_schedule,
        "enqueue_due_short_insight_job",
        record_async("short_insight"),
    )
    monkeypatch.setattr(
        periodic_schedule,
        "dispatch_memory_agent_triggers_once",
        record("memory_agent_dispatch"),
    )
    monkeypatch.setattr(
        periodic_schedule,
        "release_held_suggestion",
        record("suggestion_release"),
    )
    monkeypatch.setattr(
        periodic_schedule, "run_memory_embedding_projection", run_projection
    )


def test_owner_scoped_tasks_wait_for_configure_and_run_on_the_next_tick(
    monkeypatch: pytest.MonkeyPatch,
    context: PeriodicTaskContext,
    schedule_and_slot: tuple[tuple[PeriodicTask, ...], MemoryEmbeddingProjectionSlot],
) -> None:
    """The first tick beats ``configure``; nothing owner-scoped may run on it.

    ``claim_next_pending_job`` selects by ``user_id``, so a job enqueued for the
    logged-out owner milliseconds before the cloud session lands is never
    claimed, while what the enqueue recorded -- the summary cursor, a dispatched
    memory trigger -- is spent all the same.
    """
    calls: list[str] = []
    _record_every_periodic_runner(monkeypatch, calls)
    schedule, _slot = schedule_and_slot

    unconfigured_next_runs = run_due_periodic_tasks(
        tasks=schedule,
        context=context,
        next_run_monotonic_by_task={task.name: 0.0 for task in schedule},
        now_monotonic=10.0,
    )
    # The reaper and the catalog repair take no owner, so waiting to be told who
    # the user is would only hold back recovery.
    assert calls == ["reaper", "memory_repair"]
    assert [unconfigured_next_runs[name] for name in OWNER_SCOPED_TASK_NAMES] == [
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
    ]

    mark_configured()
    run_due_periodic_tasks(
        tasks=schedule,
        context=context,
        next_run_monotonic_by_task=unconfigured_next_runs,
        now_monotonic=11.0,
    )

    # A withheld task keeps its deadline, so the tick right after `configure`
    # takes every window the first one would have.
    assert calls[2:] == [
        "activity_summary",
        "short_insight",
        "memory_agent_dispatch",
        "suggestion_release",
        "memory_embedding_projection",
    ]


def test_owner_scoped_tasks_wait_while_an_identity_change_is_at_the_barrier(
    monkeypatch: pytest.MonkeyPatch,
    context: PeriodicTaskContext,
    configured: None,
    schedule_and_slot: tuple[tuple[PeriodicTask, ...], MemoryEmbeddingProjectionSlot],
) -> None:
    calls: list[str] = []
    _record_every_periodic_runner(monkeypatch, calls)
    schedule, _slot = schedule_and_slot

    with admission_closed():
        run_due_periodic_tasks(
            tasks=schedule,
            context=context,
            next_run_monotonic_by_task={task.name: 0.0 for task in schedule},
            now_monotonic=10.0,
        )

    assert calls == ["reaper", "memory_repair"]


def test_first_tick_runs_every_ready_task_once(
    context: PeriodicTaskContext,
) -> None:
    calls: list[str] = []
    tasks = (
        _task("first", run=lambda _context: calls.append("first")),
        _task("second", run=lambda _context: calls.append("second")),
    )

    next_runs = run_due_periodic_tasks(
        tasks=tasks,
        context=context,
        next_run_monotonic_by_task={"first": 0.0, "second": 0.0},
        now_monotonic=10.0,
    )

    assert calls == ["first", "second"]
    assert next_runs == {"first": 40.0, "second": 40.0}


def test_not_due_task_is_skipped(context: PeriodicTaskContext) -> None:
    calls: list[str] = []

    next_runs = run_due_periodic_tasks(
        tasks=(_task("task", run=lambda _context: calls.append("task")),),
        context=context,
        next_run_monotonic_by_task={"task": 20.0},
        now_monotonic=10.0,
    )

    assert calls == []
    assert next_runs == {"task": 20.0}


@pytest.mark.parametrize("failure_stage", ["is_ready", "run"])
def test_task_failure_advances_deadline_and_does_not_stop_later_tasks(
    caplog: pytest.LogCaptureFixture,
    context: PeriodicTaskContext,
    failure_stage: str,
) -> None:
    calls: list[str] = []

    def fail() -> None:
        raise RuntimeError(f"{failure_stage} failed")

    def is_ready(_context: PeriodicTaskContext) -> bool:
        if failure_stage == "is_ready":
            fail()
        return True

    def run(_context: PeriodicTaskContext) -> None:
        if failure_stage == "run":
            fail()

    tasks = (
        _task("failing", is_ready=is_ready, run=run),
        _task("later", run=lambda _context: calls.append("later")),
    )

    with caplog.at_level(
        logging.ERROR,
        logger="pantaray_agents.local_runtime.runtime.periodic_schedule",
    ):
        next_runs = run_due_periodic_tasks(
            tasks=tasks,
            context=context,
            next_run_monotonic_by_task={"failing": 0.0, "later": 0.0},
            now_monotonic=10.0,
        )

    assert calls == ["later"]
    assert next_runs == {"failing": 40.0, "later": 40.0}
    assert len(caplog.records) == 1
    assert caplog.records[0].getMessage() == "periodic task failed: task=failing"
    assert caplog.records[0].exc_info is not None


def test_closed_gate_preserves_deadline_and_runs_immediately_when_opened(
    context: PeriodicTaskContext,
) -> None:
    calls: list[str] = []
    gate_open = False
    task = _task(
        "gated",
        is_ready=lambda _context: gate_open,
        run=lambda _context: calls.append("gated"),
    )

    closed_next_runs = run_due_periodic_tasks(
        tasks=(task,),
        context=context,
        next_run_monotonic_by_task={"gated": 0.0},
        now_monotonic=10.0,
    )
    gate_open = True
    open_next_runs = run_due_periodic_tasks(
        tasks=(task,),
        context=context,
        next_run_monotonic_by_task=closed_next_runs,
        now_monotonic=11.0,
    )

    assert closed_next_runs == {"gated": 0.0}
    assert calls == ["gated"]
    assert open_next_runs == {"gated": 41.0}


def test_due_tasks_run_in_declaration_order(
    context: PeriodicTaskContext,
    schedule_and_slot: tuple[tuple[PeriodicTask, ...], MemoryEmbeddingProjectionSlot],
) -> None:
    calls: list[str] = []
    schedule, _slot = schedule_and_slot
    tasks = tuple(
        _task(
            task.name,
            run=lambda _context, task_name=task.name: calls.append(task_name),
        )
        for task in schedule
    )

    run_due_periodic_tasks(
        tasks=tasks,
        context=context,
        next_run_monotonic_by_task={task.name: 0.0 for task in tasks},
        now_monotonic=10.0,
    )

    assert calls == [
        "reaper",
        "activity_summary",
        "memory_repair",
        "short_insight",
        "memory_agent_dispatch",
        "suggestion_release",
        "memory_embedding_projection",
    ]


def test_deadline_uses_tick_time_plus_interval(
    context: PeriodicTaskContext,
) -> None:
    next_runs = run_due_periodic_tasks(
        tasks=(_task("task", run=lambda _context: None, interval_seconds=2.5),),
        context=context,
        next_run_monotonic_by_task={"task": 0.0},
        now_monotonic=10.0,
    )

    assert next_runs == {"task": 12.5}


def test_duplicate_task_names_are_rejected(context: PeriodicTaskContext) -> None:
    tasks = (
        _task("duplicate", run=lambda _context: None),
        _task("duplicate", run=lambda _context: None),
    )

    with pytest.raises(ValueError, match="periodic task names must be unique"):
        run_due_periodic_tasks(
            tasks=tasks,
            context=context,
            next_run_monotonic_by_task={"duplicate": 0.0},
            now_monotonic=10.0,
        )


def test_monotonic_is_read_once_per_tick(
    monkeypatch: pytest.MonkeyPatch,
    context: PeriodicTaskContext,
) -> None:
    from pantaray_agents.local_runtime.runtime import periodic_schedule

    monotonic_calls = 0

    def monotonic() -> float:
        nonlocal monotonic_calls
        monotonic_calls += 1
        return 10.0

    monkeypatch.setattr(periodic_schedule.time, "monotonic", monotonic)

    next_runs = run_due_periodic_tasks(
        tasks=(
            _task("first", run=lambda _context: None),
            _task("second", run=lambda _context: None),
        ),
        context=context,
        next_run_monotonic_by_task={"first": 0.0, "second": 0.0},
    )

    assert monotonic_calls == 1
    assert next_runs == {"first": 40.0, "second": 40.0}
