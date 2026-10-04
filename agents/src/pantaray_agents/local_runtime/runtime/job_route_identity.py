"""The route a claimed job started on, and the check that it still holds.

A job runs under one effective route identity: who owns the local data, and
which provider account each LLM and web-search request reaches. A control-socket
operation that changes it stops in-flight work at the barrier (design 6.2), but
a run already sitting between two calls has to notice on its own. So the job
records the identity it starts on, and every later LLM call, web search and
publication requires the current identity to still equal it (design 6.2, 7.3,
acceptance A09). Nothing recorded here is a secret: an identity carries key
fingerprints, never keys.

The two kinds of job part ways when the check fails:

- A background job (activity summary, Insight, memory update, Suggestion) has
  nobody waiting, and its edits stay in a run-only workspace until the single
  publication at the end. It goes back to ``queued`` with no terminal row and
  starts over under the new identity, so switching accounts never shows the user
  a failed Insight and never republishes work done for the previous owner.
- An Action and its subagent are not requeued. The barrier has already recorded
  the Stop fence that turns this run's terminal into ``canceled``, and a
  subagent can have changed the user's files through ``apply_patch`` / ``bash``
  / ``run_python``, so the failure simply propagates and the Action converges on
  that canceled terminal rather than running again.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from pantaray_agents.tasks.job_retry import (
    LOCAL_JOB_OPERATIONAL_RETRY_DELAY_SECONDS,
    defer_local_job_transition_with_retry,
)

from .route_identity import (
    EffectiveRouteIdentity,
    effective_route_identity,
    read_route_inputs,
)
from .utc_timestamps import format_utc_iso, utc_now


class LocalJobRouteIdentityChangedError(RuntimeError):
    """This run's route no longer reaches what it started on.

    Deliberately not a ``MigrationError``: this is a routing decision, not a
    storage failure, and the handlers that read ``MigrationError`` as a
    repository problem must not classify it as one.
    """


@dataclass(frozen=True, slots=True)
class _RunningJobRoute:
    """The identity one claimed job started on, and how to put it back."""

    identity: EffectiveRouteIdentity
    job_id: str
    process_id: str
    process_pending_status: str
    db_path: Path
    busy_timeout_ms: int
    requeue_on_change: bool


_RUNNING_JOB_ROUTE: ContextVar[_RunningJobRoute | None] = ContextVar(
    "running_job_route", default=None
)


@contextmanager
def bind_job_route_identity(
    *,
    job_id: str,
    process_id: str,
    process_pending_status: str,
    db_path: Path,
    busy_timeout_ms: int,
    requeue_on_change: bool,
) -> Iterator[None]:
    """Record the identity this run starts on, for as long as it runs.

    ``asyncio.run`` copies the calling context into the task it drives, so this
    binding reaches the LLM and web-search calls the runner makes however deeply
    they are nested, the same way ``get_trace_context`` already reaches them.
    The worker thread this runs on is reused for later jobs, so the binding is
    always released again.
    """
    token = _RUNNING_JOB_ROUTE.set(
        _RunningJobRoute(
            identity=effective_route_identity(read_route_inputs()),
            job_id=job_id,
            process_id=process_id,
            process_pending_status=process_pending_status,
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            requeue_on_change=requeue_on_change,
        )
    )
    try:
        yield
    finally:
        _RUNNING_JOB_ROUTE.reset(token)


async def require_current_route_identity() -> None:
    """Require that this job's route still reaches what it started on.

    Outside a claimed job -- a call from the local HTTP router, or a test --
    there is nothing to compare against, so this does nothing.

    Raises:
        DeferredLocalJob: the identity changed under a background job, which is
            now back in ``queued`` with no terminal row.
        LocalJobRouteIdentityChangedError: the identity changed under an Action
            or its subagent, which must not run again.
    """
    route = _RUNNING_JOB_ROUTE.get()
    if route is None:
        return
    if effective_route_identity(read_route_inputs()) == route.identity:
        return
    if route.requeue_on_change:
        await defer_local_job_transition_with_retry(
            job_id=route.job_id,
            process_id=route.process_id,
            process_pending_status=route.process_pending_status,
            db_path=route.db_path,
            busy_timeout_ms=route.busy_timeout_ms,
            scheduled_at=_restart_after_the_barrier(),
        )
    raise LocalJobRouteIdentityChangedError(
        f"local job route identity changed: job_id={route.job_id}"
    )


def job_owner_changed() -> bool:
    """Whether the local data now belongs to someone other than this run's owner.

    A requeued job is claimed only for its own owner, so a background job whose
    owner is gone never runs again; its caller has to end what it started
    instead of relying on the requeue. Outside a claimed job this is False.
    """
    route = _RUNNING_JOB_ROUTE.get()
    if route is None:
        return False
    return effective_route_identity(read_route_inputs()).owner_id != (
        route.identity.owner_id
    )


def _restart_after_the_barrier() -> str:
    """When a requeued job may be claimed again.

    The same delay the other operational requeues use, which is long enough for
    the barrier still applying the new identity to have finished, and keeps a
    run that keeps losing the race from spinning.
    """
    return format_utc_iso(
        utc_now() + timedelta(seconds=LOCAL_JOB_OPERATIONAL_RETRY_DELAY_SECONDS)
    )


__all__ = [
    "LocalJobRouteIdentityChangedError",
    "bind_job_route_identity",
    "job_owner_changed",
    "require_current_route_identity",
]
