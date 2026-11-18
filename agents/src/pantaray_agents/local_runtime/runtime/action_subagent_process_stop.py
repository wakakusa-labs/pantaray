"""Stop a subagent's long-running processes before a transaction settles it.

The cancel core settles a queued or approval-paused child, and releases its
claims, inside its caller's write transaction, where no stop can run: a stop is
asynchronous and waits out the stop grace while the write lock would be held.
So each owner of such a transaction stops the children it may settle first and
waits for it: the parent's ``cancel_subagent``, the parent's terminal, and the
user's Stop. A running child is only asked to stop there and stops its own at
its end; startup recovery settles children before any process can exist.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ..storage.migrations.connection import configure_connection
from ..tooling.brokering.broker_process import stop_run_processes
from .action_subagent_cancel import ActionSubagentCancelRequest
from .job_types import ACTION_SUBAGENT_PROCESS_KIND

_LIVE_CHILDREN_SQL = """
SELECT process_id FROM processes
WHERE user_id = ? AND action_id = ? AND kind = ? AND parent_process_id = ?
  AND status IN ('enqueued', 'running', 'paused')
"""


def _live_children(
    db_path: Path, busy_timeout_ms: int, condition: str, parameters: tuple[str, ...]
) -> tuple[str, ...]:
    with sqlite3.connect(db_path) as connection:
        configure_connection(connection, busy_timeout_ms)
        rows = connection.execute(
            f"{_LIVE_CHILDREN_SQL}{condition} ORDER BY process_id", parameters
        ).fetchall()
    return tuple(str(row[0]) for row in rows)


async def stop_action_subagent_processes(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    action_id: str,
    parent_process_id: str,
) -> None:
    """Before the parent run's terminal or a Stop of it settles its children.

    Only that run's children: a late Stop or terminal of an earlier run must
    not stop the children of the run after it.
    """

    # Read in place like the transaction it precedes; with no live child
    # nothing here suspends.
    child_process_ids = _live_children(
        db_path,
        busy_timeout_ms,
        "",
        (user_id, action_id, ACTION_SUBAGENT_PROCESS_KIND, parent_process_id),
    )
    await stop_run_processes(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        owner_process_ids=child_process_ids,
    )


async def stop_action_subagent_child_processes(
    *, db_path: Path, busy_timeout_ms: int, request: ActionSubagentCancelRequest
) -> None:
    """Before the parent's cancel settles the child: only a live child of the
    parent that asks, so a parent never stops another run's processes."""

    child_process_ids = _live_children(
        db_path,
        busy_timeout_ms,
        " AND process_id = ?",
        (
            request.user_id,
            request.action_id,
            ACTION_SUBAGENT_PROCESS_KIND,
            request.parent_process_id,
            request.child_process_id,
        ),
    )
    await stop_run_processes(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        owner_process_ids=child_process_ids,
    )


__all__ = [
    "stop_action_subagent_child_processes",
    "stop_action_subagent_processes",
]
