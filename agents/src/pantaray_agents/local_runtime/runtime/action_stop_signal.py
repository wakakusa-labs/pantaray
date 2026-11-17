"""Durable read of "this Action run has been told to stop".

``ActionCancellationService`` owns the run's cancellation *decision* at node
boundaries, including what to do when the read itself keeps failing. This module
reads the same durable facts for a different consumer: an in-flight tool call
polls it so it can be cancelled now instead of at the next node boundary. A
failed poll must never terminalize a run, so this reader has no failure policy of
its own - it answers the question or raises, and the caller keeps polling.

Two write-once rows carry the fact, and either one is enough:

- ``jobs.cancel_requested_at`` - the Stop fence recorded on the run's own job.
  A Stop always fences the parent job (``action_cancel_repository.py``), and a
  Stop that still owns a running subagent fences the child job the same way
  (``action_subagent_cancel.py``), so this covers both a parent run and a
  subagent run without knowing which one is asking.
- ``agent_actions.status = 'canceled'`` - the Action terminal an earlier Stop
  already committed.

Neither value is ever cleared, so an observation here can be acted on without
re-reading: the run is stopping.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from pantaray_agents.local_runtime.storage.migrations import MigrationError
from pantaray_agents.local_runtime.storage.migrations.connection import (
    configure_connection,
)

_STOP_REQUESTED_SQL = """
SELECT EXISTS(
    SELECT 1 FROM jobs
    WHERE job_id = :job_id AND user_id = :user_id
      AND cancel_requested_at IS NOT NULL
) OR EXISTS(
    SELECT 1 FROM agent_actions
    WHERE action_id = :action_id AND user_id = :user_id
      AND status = 'canceled'
)
"""


def action_stop_requested(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    action_id: str,
    job_id: str | None,
) -> bool:
    """Report whether this Action run has a durable Stop to obey."""

    if busy_timeout_ms <= 0:
        raise MigrationError("LOCAL_DB_BUSY_TIMEOUT_MS must be a positive integer")
    with sqlite3.connect(db_path) as connection:
        configure_connection(connection, busy_timeout_ms)
        return action_stop_requested_in_connection(
            connection, user_id=user_id, action_id=action_id, job_id=job_id
        )


def action_stop_requested_in_connection(
    connection: sqlite3.Connection, *, user_id: str, action_id: str, job_id: str | None
) -> bool:
    """The same read inside the caller's transaction."""

    row = connection.execute(
        _STOP_REQUESTED_SQL,
        {
            # A run outside the local runtime worker has no job to fence, so
            # it observes the Action terminal alone.
            "job_id": job_id or "",
            "user_id": user_id,
            "action_id": action_id,
        },
    ).fetchone()
    return bool(row[0])


__all__ = ["action_stop_requested", "action_stop_requested_in_connection"]
