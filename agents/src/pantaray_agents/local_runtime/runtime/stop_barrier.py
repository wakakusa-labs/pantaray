"""Stop what the old identity started, before the new one takes over.

A control-socket operation that changes the effective route identity -- who owns
the local data, and which provider account each LLM and web-search request
reaches -- must not leave work running against the identity it replaces (design
6.2, 6.3, 7.3, acceptance A09). This module is that barrier, and the only place
the order is written down:

1. close admission, so nothing new is claimed and no WebSocket is accepted while
   the swap is in flight;
2. record the cancel of every Action run the old identity still has in flight;
   and stop the other work registered in ``identity_stops`` (its chat turn,
   which runs again under the new identity);
3. close the WebSockets bound to the old owner, when the owner is changing;
4. swap the identity, holding the source gate so the departing owner loses its
   context-source permits in the same turn;
5. open admission again, whether or not the swap succeeded.

An operation that leaves the identity alone -- an access-token rotation inside
the same sign-in, a direct connection stored while signed in, a stale clear --
is applied straight through: it closes nothing, stops nothing, and does not take
the source gate.

Only the steps the difference calls for run. An owner change closes that owner's
sockets; a change confined to the LLM or web-search destination does not, since
the sockets still answer to the same owner. Every identity change cancels the
Action runs, because every field of the identity is something a run in flight is
already using -- unless the runtime had not been configured yet, in which case
there is nothing in flight to cancel at all (see ``apply_with_stop_barrier``).

Nothing converges here. Admission is closed only from the cancels to the swap,
which keeps the barrier inside the five seconds the control socket answers
Electron main in (design 6.2). A claimed background job that is mid-call is
caught instead by the identity it recorded when it was claimed
(``job_route_identity``), and requeues itself.

Barriers never overlap. ``identity_changes_serialized`` queues them, snapshot
included, so an inner one cannot hand admission back while an outer one is still
swapping, and so the later one reads the state the earlier one left.

One identity change has no control-socket operation behind it: a cloud session
whose token runs out. Design 6.2 puts that transition behind this same barrier,
so the timer that raises it lives here too.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import replace
from pathlib import Path
from typing import Final

from pantaray_agents.action_status import ACTION_STATUS_PROCESSING
from pantaray_agents.local_runtime.context.source_control import context_source_control
from pantaray_agents.local_runtime.storage.migrations.connection import (
    configure_connection,
)
from pantaray_agents.orchestration.ws.owner_bound_sockets import (
    close_owner_bound_sockets,
)
from pantaray_agents.routers.action_cancel_service import execute_action_cancel

from .admission import admission_closed
from .identity_stops import stop_registered_work
from .route_identity import (
    EffectiveRouteIdentity,
    RouteInputs,
    effective_route_identity,
    read_route_inputs,
)
from .runtime_env import read_local_runtime_db_config
from .session_store import (
    peek_pending_cloud_session_expiry,
    seconds_until_cloud_session_expiry,
    settle_cloud_session_expiry,
)

logger = logging.getLogger(__name__)

STOP_BARRIER_CANCEL_REASON: Final[str] = "route_identity_changed"
"""Why the Action was stopped, for the existing cancel log line."""

_EXPIRY_TIMER: asyncio.TimerHandle | None = None
_EXPIRY_SETTLEMENT: asyncio.Task[None] | None = None
_SERIALIZED: asyncio.Lock | None = None
_SERIALIZED_LOOP: asyncio.AbstractEventLoop | None = None


def _identity_change_lock() -> asyncio.Lock:
    """The lock for this loop, made on first use.

    An ``asyncio.Lock`` binds to the loop it is first awaited on, and the
    control socket serves its requests on a loop it creates when it starts, so a
    lock made at import time would belong to the wrong one. Everything that
    changes the identity runs on that single loop -- the operations and the
    expiry timer the operations arm -- so one lock per loop is one lock.
    """
    global _SERIALIZED, _SERIALIZED_LOOP
    loop = asyncio.get_running_loop()
    if _SERIALIZED is None or _SERIALIZED_LOOP is not loop:
        _SERIALIZED = asyncio.Lock()
        _SERIALIZED_LOOP = loop
    return _SERIALIZED


@asynccontextmanager
async def identity_changes_serialized() -> AsyncIterator[None]:
    """Hold off every other identity change, from the snapshot to the swap.

    Two barriers must not overlap. The inner one's exit would hand admission
    back while the outer one is still swapping, and the WebSocket handshake
    depends on admission staying closed from before the barrier reads the socket
    ledger until after the swap (``orchestration/router.py``). The later barrier
    would also have taken its snapshot before the earlier one applied, and so
    compare against an identity that no longer exists.

    That overlap is reachable: Electron main's token refresh fails around the
    same time the session lapses, so a sign-out and the expiry timer land
    together.
    """
    async with _identity_change_lock():
        yield


async def apply_with_stop_barrier[T](
    *,
    before: RouteInputs,
    after: EffectiveRouteIdentity,
    apply: Callable[[], T],
) -> T:
    """Run ``apply`` behind the barrier the step from ``before`` to ``after`` needs.

    ``before`` is the snapshot ``after`` was derived from, not just the identity
    it resolves to, because whether anything could be running at all is part of
    that snapshot and not of the identity. Until ``configure`` lands,
    ``claimable_specs`` claims nothing, so an Action left ``processing`` by the
    previous process -- startup recovery re-queues its job and leaves the row
    alone -- is waiting to resume, not running, and the first ``configure`` must
    not cancel it.

    ``apply`` is the operation's own identity swap and must be the only thing
    that writes it, so that the cancels above are already durable by the time
    anything can read the new identity. Callers hold
    ``identity_changes_serialized`` across the snapshot and this call.
    """
    before_identity = effective_route_identity(before)
    if before_identity == after:
        return apply()
    with admission_closed():
        if before.configured:
            await _cancel_in_flight_actions(owner_id=before_identity.owner_id)
            await stop_registered_work(owner_id=before_identity.owner_id)
        owner_changed = before_identity.owner_id != after.owner_id
        if owner_changed:
            closed_socket_count = await close_owner_bound_sockets(
                before_identity.owner_id
            )
            logger.info(
                "Stop barrier closed owner-bound WS: owner_id=%s closed=%d",
                before_identity.owner_id,
                closed_socket_count,
            )
        async with context_source_control.gate.turn():
            result = apply()
            if owner_changed:
                context_source_control.forget_session(before_identity.owner_id)
        return result


def arm_cloud_session_expiry_timer() -> None:
    """Re-arm the one-shot timer that publishes a lapsed cloud session as expired.

    The runtime raises that transition on its own clock rather than waiting for
    Electron main (design 6.2), so that whenever the timer gets there first the
    in-flight runs are cancelled before ``expired`` is published. It is not a
    guarantee that nothing else publishes it first: every other read of the
    store settles an elapsed session as a side effect, the barrier's own cancels
    included, and what that leaves behind is caught by the identity each job
    recorded when it was claimed (``job_route_identity``).

    Callers run on the control socket's event loop and call this after every
    operation that replaces the session, so the timer always describes the
    session that is there now; no session leaves no timer.
    """
    global _EXPIRY_TIMER
    if _EXPIRY_TIMER is not None:
        _EXPIRY_TIMER.cancel()
        _EXPIRY_TIMER = None
    remaining_seconds = seconds_until_cloud_session_expiry()
    if remaining_seconds is None:
        return
    loop = asyncio.get_running_loop()
    _EXPIRY_TIMER = loop.call_later(
        remaining_seconds, _start_cloud_session_expiry_settlement, loop
    )


def cancel_cloud_session_expiry_work() -> asyncio.Task[None] | None:
    """Disarm the timer and cancel a settlement in flight; return that settlement.

    A settlement stopped between closing admission and opening it again only
    runs that ``finally`` once its task resumes, so a caller whose loop is about
    to close awaits what comes back. Otherwise the runtime is left admitting
    nothing, and restarting it in the same process does not clear that.
    """
    global _EXPIRY_TIMER, _EXPIRY_SETTLEMENT
    if _EXPIRY_TIMER is not None:
        _EXPIRY_TIMER.cancel()
        _EXPIRY_TIMER = None
    settlement, _EXPIRY_SETTLEMENT = _EXPIRY_SETTLEMENT, None
    if settlement is not None:
        settlement.cancel()
    return settlement


async def await_cancelled_cloud_session_expiry_work() -> None:
    """Cancel the expiry work and let it unwind, before this loop stops."""
    settlement = cancel_cloud_session_expiry_work()
    if settlement is None:
        return
    with suppress(asyncio.CancelledError):
        await settlement


def _start_cloud_session_expiry_settlement(loop: asyncio.AbstractEventLoop) -> None:
    global _EXPIRY_SETTLEMENT
    # asyncio holds only a weak reference to a running task, so the settlement
    # would be collected part way through the barrier without this one.
    _EXPIRY_SETTLEMENT = loop.create_task(_settle_cloud_session_expiry())


async def _settle_cloud_session_expiry() -> None:
    async with identity_changes_serialized():
        pending = peek_pending_cloud_session_expiry()
        if pending is None:
            # Another read settled it first, or the operation this waited behind
            # replaced the session. Neither is compensated for here: a run that
            # was already mid-call is caught by the identity it recorded when it
            # was claimed.
            return
        inputs = read_route_inputs()
        await apply_with_stop_barrier(
            before=inputs,
            after=effective_route_identity(replace(inputs, cloud=pending)),
            apply=settle_cloud_session_expiry,
        )
        arm_cloud_session_expiry_timer()


async def _cancel_in_flight_actions(*, owner_id: str) -> None:
    """Cancel the owner's in-flight Action runs through the existing Stop path.

    ``execute_action_cancel`` resolves the caller against the current owner, so
    this has to run before the swap, which is where the barrier puts it. Each
    call records the Stop fence and settles the run's subagent children with it;
    a child is never requeued, because it can already have changed the user's
    files (design 6.2).
    """
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    action_ids = _in_flight_action_ids(
        db_path=db_path, busy_timeout_ms=busy_timeout_ms, owner_id=owner_id
    )
    if not action_ids:
        return
    logger.info(
        "Stop barrier canceling in-flight Actions: owner_id=%s count=%d",
        owner_id,
        len(action_ids),
    )
    # Actions run concurrently, so stop them concurrently: the swap waits for the
    # slowest cleanup instead of the sum of all of them.
    await asyncio.gather(
        *(
            execute_action_cancel(
                user_id=owner_id,
                action_id=action_id,
                reason=STOP_BARRIER_CANCEL_REASON,
            )
            for action_id in action_ids
        )
    )


def _in_flight_action_ids(
    *, db_path: Path, busy_timeout_ms: int, owner_id: str
) -> tuple[str, ...]:
    """The Action runs this owner has in flight right now.

    Only ``processing``. A ``queued`` Action has not been claimed yet and the
    closed admission gate keeps it that way until the swap is done, so it starts
    under the new identity instead -- which is where design 6.2 wants it.
    """
    with sqlite3.connect(db_path) as connection:
        configure_connection(connection, busy_timeout_ms)
        rows = connection.execute(
            """
            SELECT action_id
            FROM agent_actions
            WHERE user_id = ? AND status = ?
            ORDER BY created_at, action_id
            """,
            (owner_id, ACTION_STATUS_PROCESSING),
        ).fetchall()
    return tuple(str(row[0]) for row in rows)


__all__ = [
    "STOP_BARRIER_CANCEL_REASON",
    "apply_with_stop_barrier",
    "arm_cloud_session_expiry_timer",
    "await_cancelled_cloud_session_expiry_work",
    "cancel_cloud_session_expiry_work",
    "identity_changes_serialized",
]
