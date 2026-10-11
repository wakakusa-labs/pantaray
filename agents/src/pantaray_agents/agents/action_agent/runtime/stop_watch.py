"""Stop a running Action immediately instead of at the next node boundary.

A Stop is a durable row, not a signal the running graph can receive: the node
boundaries read it through ``ActionCancellationService`` and converge there. That
makes the user wait for whatever is in flight - a ``bash`` build, a 60s web
fetch, a stalled THINK - before the run reacts.

This module closes that gap without moving the cancellation decision. While a
call is in flight, the watch polls the same durable fact
(``action_stop_signal.action_stop_requested``) and, the moment it is recorded,
cancels the awaited task. The call's own boundary then finalizes what it started
- audit row, tool result, step row - and the node converges the run to
``canceled``. Nothing here decides *whether* a run stops; it decides *when* the
in-flight work learns about it.

What cancellation means per call is owned by the call, not by this module:
``bash``/``run_python`` kill the sandbox process group
(``command_sandbox_client.py``), HTTP-backed tools abort the request, and
``apply_patch`` is run uncancellable by its caller so a half-written file is
never possible.

A poll that fails is logged and retried: this watch only ever *starts* a
cancellation that the node boundary would have reached anyway, so it must not
terminalize a run of its own accord. The boundary check keeps its fail-closed
policy for repeated read failures.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from contextlib import suppress
from types import TracebackType
from typing import Any

from pantaray_agents.local_runtime.runtime.action_stop_signal import (
    action_stop_requested,
)
from pantaray_agents.local_runtime.runtime.bootstrap import read_local_runtime_db_config
from pantaray_agents.local_runtime.runtime.db_execution_context import (
    resolve_local_runtime_db_config,
)
from pantaray_agents.local_runtime.tooling.brokering.broker_common import (
    APPLY_PATCH_TOOL_ID,
)
from pantaray_agents.utils.trace_context import get_trace_context

from .log_safety import exception_type_name
from .state import ActionAgentState
from .state.updates import set_status_with_updated_at

logger = logging.getLogger(__name__)

STOP_POLL_INTERVAL_SECONDS = 0.25
"""How long an in-flight call may keep running after the user pressed Stop.

Design limit: one indexed SQLite read per interval per running call site is
cheap enough to leave unconfigured. Shorten it only if a measured Stop latency
budget below ~1s appears.
"""

UNCANCELLABLE_TOOL_IDS = frozenset({APPLY_PATCH_TOOL_ID})
"""停止要求でも中断せず完走させる呼び出し（親の run と subagent の両方）。

``apply_patch`` の本体は 1 つのスレッドがワークスペースの排他ロックを取って走る短い
書き込みで、await 側を cancel してもそのスレッドは書き続ける。中断できるのは「結果を
待つのをやめる」ところまでで、書き込み自体は止まらないので、cancel は「半分書けた
ファイルを canceled として記録する」結果にしかならない。完走を待つほうが速く、かつ
正しい。ほかの書き込み系ツール（``bash`` / ``run_python``）は sandbox のプロセス
グループごと kill できるので、この例外には入らない。
"""

type StopProbe = Callable[[], bool]


class ActionStopRequested(Exception):
    """The awaited call was cancelled because the user stopped the run.

    ``cancellation`` is the very ``CancelledError`` the call raised on its way
    out. A call that keeps evidence of how far it got attaches it there - the
    partial output of a killed command - and the boundary that writes the
    call's terminal reads it back from here.
    """

    def __init__(self, message: str, *, cancellation: BaseException) -> None:
        super().__init__(message)
        self.cancellation = cancellation


def build_action_stop_probe(state: ActionAgentState) -> StopProbe | None:
    """Bind the durable stop read to this run, or report that it has no job.

    Only a run executing as a local runtime job can be stopped: the Stop button
    fences that job. A run without one (an in-process invocation, a test) keeps
    the node-boundary check as its only cancellation path.
    """

    trace = get_trace_context()
    job_id = trace.local_job_id if trace is not None else None
    if not job_id:
        return None
    user_id = str(state.get("user_id") or "")
    action_id = str(state.get("action_id") or "")
    if not user_id or not action_id:
        raise RuntimeError("Action stop watch requires the run identity")
    db_path, busy_timeout_ms = resolve_local_runtime_db_config(
        fallback=read_local_runtime_db_config
    )

    def probe() -> bool:
        return action_stop_requested(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            user_id=user_id,
            action_id=action_id,
            job_id=job_id,
        )

    return probe


class ActionStopWatch:
    """Cancel the calls registered with it as soon as a Stop is observed."""

    def __init__(
        self,
        *,
        probe: StopProbe | None,
        poll_interval_seconds: float = STOP_POLL_INTERVAL_SECONDS,
    ) -> None:
        self._probe = probe
        self._poll_interval_seconds = poll_interval_seconds
        self._in_flight: set[asyncio.Task[Any]] = set()
        self._watcher: asyncio.Task[None] | None = None
        self._stop_observed = False

    @property
    def stop_observed(self) -> bool:
        """Whether a durable Stop was read while this watch was open."""

        return self._stop_observed

    async def __aenter__(self) -> ActionStopWatch:
        if self._probe is not None:
            self._watcher = asyncio.create_task(self._poll_until_stopped())
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        watcher = self._watcher
        self._watcher = None
        if watcher is None:
            return
        watcher.cancel()
        try:
            await watcher
        except asyncio.CancelledError:
            if _outer_cancellation_requested():
                raise

    async def run[T](
        self,
        coro: Coroutine[Any, Any, T],
        *,
        cancellable: bool = True,
    ) -> T:
        """Await one call, cancelling it if the user stops the run.

        ``cancellable=False`` runs the call to completion: a caller uses it when
        interrupting the call would be worse than waiting for it.
        """

        if self._probe is None or not cancellable:
            return await coro
        task = asyncio.ensure_future(coro)
        self._in_flight.add(task)
        if self._stop_observed:
            task.cancel()
        try:
            # A cancelled task reaches its awaiter only once it is done, so the
            # call has already run its own cancellation handlers - closing its
            # audit row and killing its process - by the time this raises.
            return await task
        except asyncio.CancelledError as cancellation:
            if _outer_cancellation_requested():
                # The caller is being torn down (its per-call timeout, or the
                # worker). Awaiting the call through its own cancellation keeps
                # the same close-then-report order a direct await would have had,
                # and leaves no task behind. A call that never returns would have
                # hung the caller on a direct await too.
                task.cancel()
                with suppress(BaseException):
                    await task
                raise
            if self._stop_observed:
                raise ActionStopRequested(
                    "the call was cancelled by a user Stop",
                    cancellation=cancellation,
                ) from None
            raise
        finally:
            self._in_flight.discard(task)

    async def stop_requested(self) -> bool:
        """Read the durable Stop now instead of waiting for the next poll.

        A caller uses this where it is about to *start* new side effects: the
        250 ms poll only tells it whether a Stop was already seen, so a Stop
        committed since the last tick would otherwise let one more call run. A
        failed read keeps the previous answer - the node boundary owns the
        fail-closed policy, and this watch must not terminalize a run itself.
        """

        if self._probe is None or self._stop_observed:
            return self._stop_observed
        try:
            stopped = await asyncio.to_thread(self._probe)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed to read the Action Stop signal at a call boundary; the "
                "node boundary still owns cancellation: exception_type=%s",
                exception_type_name(exc),
            )
            return self._stop_observed
        if stopped:
            self._stop_observed = True
        return self._stop_observed

    async def _poll_until_stopped(self) -> None:
        assert self._probe is not None
        while True:
            await asyncio.sleep(self._poll_interval_seconds)
            try:
                stopped = await asyncio.to_thread(self._probe)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Failed to poll the Action Stop signal; the node boundary "
                    "still owns cancellation: exception_type=%s",
                    exception_type_name(exc),
                )
                continue
            if stopped:
                self._stop_observed = True
                for task in tuple(self._in_flight):
                    task.cancel()
                return


def converge_stopped_run(state: ActionAgentState) -> None:
    """Terminalize a run whose in-flight work was cancelled by a Stop.

    The Stop that triggered the cancellation is durable and never withdrawn, so
    this convergence does not re-read it: re-reading could only disagree with a
    fact that has already destroyed the run's work.
    """

    set_status_with_updated_at(state, status="canceled")
    # The canceled calls are already in history; the next run decides again from
    # THINK instead of replaying a batch remainder the user stopped.
    state["next_action"] = None


def _outer_cancellation_requested() -> bool:
    """Whether the *caller* is being cancelled, rather than an awaited call."""

    current = asyncio.current_task()
    return current is not None and current.cancelling() > 0


__all__ = [
    "STOP_POLL_INTERVAL_SECONDS",
    "UNCANCELLABLE_TOOL_IDS",
    "ActionStopRequested",
    "ActionStopWatch",
    "StopProbe",
    "build_action_stop_probe",
    "converge_stopped_run",
]
