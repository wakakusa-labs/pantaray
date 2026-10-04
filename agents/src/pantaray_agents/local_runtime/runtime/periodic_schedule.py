"""Schedule periodic convergence work while the local runtime remains active."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from pantaray_agents.orchestration.ws.deliverable_sessions import (
    owner_has_deliverable_session,
)

from .activity_summary_scheduler import (
    _SCHEDULER_INTERVAL_SECONDS,
    run_activity_summary_scheduler_once,
)
from .admission import admission_is_open
from .insight_queue import (
    SHORT_INSIGHT_WINDOW_SECONDS,
    enqueue_due_short_insight_job,
)
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
from .session_store import read_configured
from .suggestion_release import (
    SUGGESTION_RELEASE_INTERVAL_SECONDS,
    release_held_suggestion,
)

logger = logging.getLogger(__name__)

LOCAL_RUNTIME_PERIODIC_REAPER_INTERVAL_SECONDS: Final[float] = 30.0
MEMORY_EMBEDDING_IDLE_INTERVAL_SECONDS: Final[float] = 1.0


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
    """Whether ``owner_user_id`` already names the owner the work belongs to.

    Until Electron main applies ``configure`` the helper has not been told about
    the cloud session, so ``current_owner_id`` still answers the logged-out
    owner; while an identity change is at the barrier that owner is being
    replaced. Work created in either state is bound to an owner the worker stops
    claiming for milliseconds later -- ``claim_next_pending_job`` selects by
    ``user_id`` and nothing reclaims another owner's queue -- so the job stays
    queued for good while what it recorded is spent all the same: the summary
    cursor advanced past the window, the memory trigger marked dispatched. This
    is the enqueue side of the gate ``claimable_specs`` already holds on the
    claim side (design 6.2).
    """
    return admission_is_open() and read_configured()


def _run_short_insight_enqueue(context: PeriodicTaskContext) -> None:
    asyncio.run(
        enqueue_due_short_insight_job(
            db_path=context.db_path,
            busy_timeout_ms=context.busy_timeout_ms,
            user_id=context.owner_user_id,
        )
    )


def _run_memory_agent_dispatch(context: PeriodicTaskContext) -> None:
    dispatch_memory_agent_triggers_once(
        db_path=context.db_path,
        busy_timeout_ms=context.busy_timeout_ms,
        user_id=context.owner_user_id,
    )


def _run_suggestion_release(context: PeriodicTaskContext) -> None:
    release_held_suggestion(
        db_path=context.db_path,
        busy_timeout_ms=context.busy_timeout_ms,
        user_id=context.owner_user_id,
        now=datetime.now(UTC),
        session_can_show=owner_has_deliverable_session(context.owner_user_id),
    )


def build_periodic_schedule(
    *, embedding_slot: MemoryEmbeddingProjectionSlot
) -> tuple[PeriodicTask, ...]:
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
            name="activity_summary",
            interval_seconds=float(_SCHEDULER_INTERVAL_SECONDS),
            is_ready=_owner_is_settled,
            run=lambda context: asyncio.run(
                run_activity_summary_scheduler_once(
                    db_path=context.db_path,
                    busy_timeout_ms=context.busy_timeout_ms,
                )
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
            name="short_insight",
            interval_seconds=float(SHORT_INSIGHT_WINDOW_SECONDS),
            is_ready=_owner_is_settled,
            run=_run_short_insight_enqueue,
        ),
        PeriodicTask(
            name="memory_agent_dispatch",
            interval_seconds=MEMORY_AGENT_DISPATCH_INTERVAL_SECONDS,
            is_ready=_owner_is_settled,
            run=_run_memory_agent_dispatch,
        ),
        PeriodicTask(
            name="suggestion_release",
            interval_seconds=SUGGESTION_RELEASE_INTERVAL_SECONDS,
            is_ready=_owner_is_settled,
            run=_run_suggestion_release,
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
    "LOCAL_RUNTIME_PERIODIC_REAPER_INTERVAL_SECONDS",
    "MEMORY_EMBEDDING_IDLE_INTERVAL_SECONDS",
    "MemoryEmbeddingProjectionSlot",
    "PeriodicTask",
    "PeriodicTaskContext",
    "build_periodic_schedule",
    "run_due_periodic_tasks",
]
