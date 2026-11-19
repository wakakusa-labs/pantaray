"""End runs: close their browser sessions and stop their long-running processes.

The one place a run's end reaches both. The browser goes first: marking the
owners ending is synchronous, so from the start of the end no browser session
opens and no browser operation is sent for them, while the process stop below
waits out its grace. In the other order the page could still act, for example
write to a host approved for the run, for that whole wait. A close is one small
frame, so it delays the stop little.

Both run together on a thread of their own, so a caller that is cancelled, or
whose loop ends, still leaves every session closed or queued for the sweep's
retry and every process end audited.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from pantaray_agents.local_runtime.runtime.browser_relay import end_browser_sessions

from ..brokering.broker_process import stop_run_processes


async def end_run_resources(
    *, db_path: Path, busy_timeout_ms: int, owner_process_ids: tuple[str, ...]
) -> None:
    """At the end of these runs. With no owner it returns without suspending."""

    if owner_process_ids:
        await asyncio.to_thread(
            asyncio.run,
            _end(
                db_path=db_path,
                busy_timeout_ms=busy_timeout_ms,
                owner_process_ids=owner_process_ids,
            ),
        )


async def _end(
    *, db_path: Path, busy_timeout_ms: int, owner_process_ids: tuple[str, ...]
) -> None:
    try:
        await end_browser_sessions(owner_process_ids=owner_process_ids)
    finally:
        # The owners are fenced before the first send, so a failed or cancelled
        # close leaves only the close to the sweep; the stop still runs.
        await stop_run_processes(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            owner_process_ids=owner_process_ids,
        )


__all__ = ["end_run_resources"]
