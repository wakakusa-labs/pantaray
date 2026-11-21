"""Run the built-in hooks against the local runtime database."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from contextlib import closing
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

from pantaray_agents.hooks.definitions import RunJob
from pantaray_agents.hooks.engine import run_hook_tick
from pantaray_agents.local_runtime.activity_summary_schedule import iso_z, parse_iso_z
from pantaray_agents.local_runtime.storage.migrations.connection import (
    configure_connection,
)
from pantaray_agents.local_runtime.storage.users import ensure_user_row
from pantaray_agents.utils.local_time import local_zone_name

from .builtin import builtin_hooks
from .gates import build_gates, build_predicates
from .inbox import SqliteHookInbox
from .jobs import enqueue_hook_job, run_is_active


class UnknownLocalZoneError(RuntimeError):
    """A hook fires on local time, but the OS zone has no IANA name."""


class SqliteHookStateStore:
    """The hooks' next fire times for the owner the engine fires them for."""

    def __init__(self, *, db_path: Path, busy_timeout_ms: int, user_id: str) -> None:
        self._db_path = db_path
        self._busy_timeout_ms = busy_timeout_ms
        self._user_id = user_id

    def _connect(self) -> closing[sqlite3.Connection]:
        connection = sqlite3.connect(self._db_path)
        configure_connection(connection, self._busy_timeout_ms)
        return closing(connection)

    def read_next_fire_times(self) -> Mapping[str, datetime | None]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT hook_id, next_fire_at FROM hook_states WHERE user_id = ?",
                (self._user_id,),
            ).fetchall()
        return {
            str(hook_id): None if next_fire_at is None else parse_iso_z(next_fire_at)
            for hook_id, next_fire_at in rows
        }

    def write_next_fire_time(self, hook_id: str, next_fire_at: datetime | None) -> None:
        with self._connect() as connection, connection:
            # A hook can fire before anything else wrote the owner's row.
            ensure_user_row(connection, user_id=self._user_id)
            connection.execute(
                """INSERT INTO hook_states(user_id, hook_id, next_fire_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(user_id, hook_id)
                   DO UPDATE SET next_fire_at = excluded.next_fire_at""",
                (
                    self._user_id,
                    hook_id,
                    None if next_fire_at is None else iso_z(next_fire_at),
                ),
            )


def _local_zone() -> tzinfo:
    name = local_zone_name()
    if name is None:
        raise UnknownLocalZoneError("the OS time zone has no IANA name")
    return ZoneInfo(name)


def run_builtin_hooks_once(
    *, db_path: Path, busy_timeout_ms: int, user_id: str, now: datetime | None = None
) -> None:
    gates = build_gates(
        db_path=db_path, busy_timeout_ms=busy_timeout_ms, user_id=user_id
    )
    predicates = build_predicates(
        db_path=db_path, busy_timeout_ms=busy_timeout_ms, user_id=user_id
    )
    run_hook_tick(
        builtin_hooks(),
        now=datetime.now(UTC) if now is None else now,
        local_zone=_local_zone,
        states=SqliteHookStateStore(
            db_path=db_path, busy_timeout_ms=busy_timeout_ms, user_id=user_id
        ),
        inbox=SqliteHookInbox(
            db_path=db_path, busy_timeout_ms=busy_timeout_ms, user_id=user_id
        ),
        gate_is_open=lambda name: gates[name](),
        predicate_holds=lambda name, event: predicates[name](event),
        run_is_active=lambda hook: (
            isinstance(hook.run, RunJob)
            and run_is_active(
                hook.run,
                db_path=db_path,
                busy_timeout_ms=busy_timeout_ms,
                user_id=user_id,
            )
        ),
        enqueue=lambda fire: enqueue_hook_job(
            fire, db_path=db_path, busy_timeout_ms=busy_timeout_ms, user_id=user_id
        ),
    )
