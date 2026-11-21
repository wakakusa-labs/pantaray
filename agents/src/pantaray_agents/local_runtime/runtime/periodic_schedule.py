"""Schedule periodic convergence work while the local runtime remains active."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from pantaray_agents import todo_ux_feature

from ..hooks.builtin import builtin_hooks
from ..hooks.gates import owner_is_settled
from ..hooks.runner import run_builtin_hooks_once
from ..todos.pickup import pick_up_todos
from ..todos.retention import purge_expired_todos
from .browser_relay import sweep_browser_sessions
from .local_worker import background_requests_can_send
from .memory_agent_dispatcher import (
    MEMORY_AGENT_DISPATCH_INTERVAL_SECONDS,
    dispatch_memory_agent_triggers_once,
)
from .memory_embedding_scheduler import (
    MemoryEmbeddingProjectionResult,
    run_memory_embedding_projection,
)
from .memory_repair_scheduler import (
    MEMORY_REPAIR_PERIODIC_INTERVAL_SECONDS,
    run_memory_catalog_repair_once,
)
from .reaper import run_local_periodic_reaper_once
from .utc_timestamps import now_utc_iso

logger = logging.getLogger(__name__)

LOCAL_RUNTIME_PERIODIC_REAPER_INTERVAL_SECONDS: Final[float] = 30.0
# How late after its time a hook fires, and how often a hook kept due by a
# closed gate or a waiting summary window is looked at again.
HOOKS_TICK_INTERVAL_SECONDS: Final[float] = 30.0
MEMORY_EMBEDDING_IDLE_INTERVAL_SECONDS: Final[float] = 1.0
# Nothing tells the worker that a TODO was written, so this interval is how long
# a new TODO waits for its Action. A pass reads the open TODOs and looks up each
# one's conversation by key; `read_pickup_candidates` names its limit.
TODO_PICKUP_INTERVAL_SECONDS: Final[float] = 3.0
# Deleted TODOs are kept for 30 days, so an hour late is nothing.
TODO_PURGE_INTERVAL_SECONDS: Final[float] = 3_600.0


@dataclass(frozen=True, slots=True)
class PeriodicTaskContext:
    db_path: Path
    busy_timeout_ms: int
    artifact_root: Path
    owner_user_id: str
    worker_is_idle: bool


@dataclass(frozen=True, slots=True)
class PeriodicTask:
    name: str
    interval_seconds: float
    is_ready: Callable[[PeriodicTaskContext], bool]
    run: Callable[[PeriodicTaskContext], None]


class MemoryEmbeddingProjectionSlot:
    def __init__(
        self, executor: ThreadPoolExecutor, stop_event: threading.Event
    ) -> None:
        self._executor = executor
        self._stop_event = stop_event
        self._future: Future[MemoryEmbeddingProjectionResult] | None = None

    def submit(self, context: PeriodicTaskContext) -> None:
        if self._future is not None:
            raise RuntimeError("memory embedding projection is already running")
        self._future = self._executor.submit(
            run_memory_embedding_projection,
            db_path=context.db_path,
            busy_timeout_ms=context.busy_timeout_ms,
            user_id=context.owner_user_id,
            # A pass indexes everything outstanding, so shutdown has to be able
            # to cut it short rather than wait for the whole backlog.
            stop_event=self._stop_event,
        )

    def consume_if_done(self) -> Future[MemoryEmbeddingProjectionResult] | None:
        future = self._future
        if future is None or not future.done():
            return None
        self._future = None
        return future

    def is_busy(self) -> bool:
        return self._future is not None


def _owner_is_settled(_context: PeriodicTaskContext) -> bool:
    return owner_is_settled()


def _run_memory_agent_dispatch(context: PeriodicTaskContext) -> None:
    dispatch_memory_agent_triggers_once(
        db_path=context.db_path,
        busy_timeout_ms=context.busy_timeout_ms,
        user_id=context.owner_user_id,
    )


def _todo_pickup_is_ready(context: PeriodicTaskContext) -> bool:
    # Nobody waits on a pickup and its Action fails for good without a
    # connection, so it starts only when a background job would be claimed.
    return _owner_is_settled(context) and background_requests_can_send()


def build_periodic_schedule(
    *, embedding_slot: MemoryEmbeddingProjectionSlot
) -> tuple[PeriodicTask, ...]:
    builtin_hooks()  # A broken built-in definition fails the worker at start.
    return (
        PeriodicTask(
            name="reaper",
            interval_seconds=LOCAL_RUNTIME_PERIODIC_REAPER_INTERVAL_SECONDS,
            is_ready=lambda _context: True,
            run=lambda context: run_local_periodic_reaper_once(
                db_path=context.db_path,
                busy_timeout_ms=context.busy_timeout_ms,
            ),
        ),
        PeriodicTask(
            name="hooks",
            interval_seconds=HOOKS_TICK_INTERVAL_SECONDS,
            # Each hook names its own gates.
            is_ready=lambda _context: True,
            run=lambda context: run_builtin_hooks_once(
                db_path=context.db_path,
                busy_timeout_ms=context.busy_timeout_ms,
                user_id=context.owner_user_id,
            ),
        ),
        PeriodicTask(
            name="memory_repair",
            interval_seconds=MEMORY_REPAIR_PERIODIC_INTERVAL_SECONDS,
            is_ready=lambda _context: True,
            run=lambda context: run_memory_catalog_repair_once(
                db_path=context.db_path,
                busy_timeout_ms=context.busy_timeout_ms,
                artifact_root=context.artifact_root,
            ),
        ),
        PeriodicTask(
            name="memory_agent_dispatch",
            interval_seconds=MEMORY_AGENT_DISPATCH_INTERVAL_SECONDS,
            is_ready=_owner_is_settled,
            run=_run_memory_agent_dispatch,
        ),
        PeriodicTask(
            name="memory_embedding_projection",
            interval_seconds=MEMORY_EMBEDDING_IDLE_INTERVAL_SECONDS,
            # Embedding runs in this process against the bundled model, so it
            # does not depend on a cloud session: once ``configure`` says the
            # owner is the logged-out one, an owner who never signs in still
            # gets an index.
            is_ready=lambda context: (
                _owner_is_settled(context)
                and context.worker_is_idle
                and not embedding_slot.is_busy()
            ),
            run=embedding_slot.submit,
        ),
        # Off, no TODO turns into an Action, as before the TODO list.
        *(
            (
                PeriodicTask(
                    name="todo_pickup",
                    interval_seconds=TODO_PICKUP_INTERVAL_SECONDS,
                    is_ready=_todo_pickup_is_ready,
                    run=lambda context: pick_up_todos(user_id=context.owner_user_id),
                ),
            )
            if todo_ux_feature.TODO_UX_ENABLED
            else ()
        ),
        PeriodicTask(
            name="todo_purge",
            interval_seconds=TODO_PURGE_INTERVAL_SECONDS,
            is_ready=_owner_is_settled,
            run=lambda context: purge_expired_todos(
                user_id=context.owner_user_id, now=now_utc_iso()
            ),
        ),
        PeriodicTask(
            name="browser_sessions",
            interval_seconds=LOCAL_RUNTIME_PERIODIC_REAPER_INTERVAL_SECONDS,
            is_ready=lambda _context: True,
            run=lambda context: asyncio.run(
                sweep_browser_sessions(
                    db_path=context.db_path,
                    busy_timeout_ms=context.busy_timeout_ms,
                )
            ),
        ),
    )


def run_due_periodic_tasks(
    *,
    tasks: Sequence[PeriodicTask],
    context: PeriodicTaskContext,
    next_run_monotonic_by_task: Mapping[str, float],
    now_monotonic: float | None = None,
) -> dict[str, float]:
    task_names = tuple(task.name for task in tasks)
    if len(task_names) != len(set(task_names)):
        raise ValueError("periodic task names must be unique")

    current_monotonic = time.monotonic() if now_monotonic is None else now_monotonic
    next_runs = dict(next_run_monotonic_by_task)
    for task in tasks:
        if current_monotonic < next_runs[task.name]:
            continue
        try:
            if not task.is_ready(context):
                continue
            task.run(context)
        except Exception:
            logger.exception("periodic task failed: task=%s", task.name)
        next_runs[task.name] = current_monotonic + task.interval_seconds
    return next_runs


__all__ = [
    "HOOKS_TICK_INTERVAL_SECONDS",
    "LOCAL_RUNTIME_PERIODIC_REAPER_INTERVAL_SECONDS",
    "MEMORY_EMBEDDING_IDLE_INTERVAL_SECONDS",
    "TODO_PICKUP_INTERVAL_SECONDS",
    "TODO_PURGE_INTERVAL_SECONDS",
    "MemoryEmbeddingProjectionSlot",
    "PeriodicTask",
    "PeriodicTaskContext",
    "build_periodic_schedule",
    "run_due_periodic_tasks",
]
