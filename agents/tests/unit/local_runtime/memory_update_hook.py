"""Run the builtin.memory_update hook alone against a test database."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.hooks import gates, runner
from pantaray_agents.local_runtime.hooks.builtin import builtin_hooks
from pantaray_agents.local_runtime.hooks.events import HookEvent
from pantaray_agents.local_runtime.hooks.inbox import record_event
from pantaray_agents.tasks.types import MemoryUpdateJobPayload

Tick = Callable[[Path, datetime], None]


@pytest.fixture
def tick(monkeypatch: pytest.MonkeyPatch) -> Tick:
    hooks = tuple(
        hook for hook in builtin_hooks() if hook.id == "builtin.memory_update"
    )
    monkeypatch.setattr(runner, "builtin_hooks", lambda: hooks)
    monkeypatch.setattr(gates, "owner_is_settled", lambda: True)

    def run(db_path: Path, now: datetime) -> None:
        runner.run_builtin_hooks_once(
            db_path=db_path, busy_timeout_ms=1_000, user_id="user-1", now=now
        )

    return run


def record(db_path: Path, event: HookEvent, *, at: str) -> None:
    """Record the event as its writer does, beside the row it stands for."""
    with sqlite3.connect(db_path) as connection:
        record_event(connection, user_id="user-1", event=event, occurred_at=at)


def memory_payloads(db_path: Path) -> list[MemoryUpdateJobPayload]:
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT payloads.payload_json FROM jobs
            JOIN job_payloads AS payloads ON payloads.job_id = jobs.job_id
            WHERE jobs.job_type = 'memory_update' ORDER BY jobs.rowid
            """
        ).fetchall()
    return [json.loads(row[0]) for row in rows]


def finish_memory_jobs(db_path: Path) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE jobs SET status = 'completed' WHERE job_type = 'memory_update'"
        )
