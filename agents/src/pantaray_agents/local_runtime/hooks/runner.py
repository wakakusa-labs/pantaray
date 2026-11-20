"""Run the built-in hooks against the local runtime database."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from contextlib import closing
from datetime import UTC, datetime, tzinfo
from functools import cache
from pathlib import Path
from zoneinfo import ZoneInfo

from pantaray_agents.hooks.definitions import HookDefinition, load_hook_definitions
from pantaray_agents.hooks.engine import run_hook_tick
from pantaray_agents.local_runtime.activity_summary_schedule import iso_z, parse_iso_z
from pantaray_agents.local_runtime.storage.migrations.connection import (
    configure_connection,
)
from pantaray_agents.utils.local_time import local_zone_name

from .gates import GATE_NAMES, build_gates
from .jobs import enqueue_hook_job, validate_run


class UnknownLocalZoneError(RuntimeError):
    """A hook fires on local time, but the OS zone has no IANA name."""


@cache
def builtin_hooks() -> tuple[HookDefinition, ...]:
    return load_hook_definitions(
        (Path(__file__).resolve().parent / "builtin.toml").read_text(encoding="utf-8"),
        gate_names=GATE_NAMES,
        validate_run=validate_run,
    )


class SqliteHookStateStore:
    def __init__(self, *, db_path: Path, busy_timeout_ms: int) -> None:
        self._db_path = db_path
        self._busy_timeout_ms = busy_timeout_ms

    def _connect(self) -> closing[sqlite3.Connection]:
        connection = sqlite3.connect(self._db_path)
        configure_connection(connection, self._busy_timeout_ms)
        return closing(connection)

    def read_next_fire_times(self) -> Mapping[str, datetime | None]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT hook_id, next_fire_at FROM hook_states"
            ).fetchall()
        return {
            str(hook_id): None if next_fire_at is None else parse_iso_z(next_fire_at)
            for hook_id, next_fire_at in rows
        }

    def write_next_fire_time(self, hook_id: str, next_fire_at: datetime | None) -> None:
        with self._connect() as connection, connection:
            connection.execute(
                """INSERT INTO hook_states(hook_id, next_fire_at) VALUES (?, ?)
                   ON CONFLICT(hook_id) DO UPDATE SET next_fire_at = excluded.next_fire_at""",
                (hook_id, None if next_fire_at is None else iso_z(next_fire_at)),
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
    run_hook_tick(
        builtin_hooks(),
        now=datetime.now(UTC) if now is None else now,
        local_zone=_local_zone,
        states=SqliteHookStateStore(db_path=db_path, busy_timeout_ms=busy_timeout_ms),
        gate_is_open=lambda name: gates[name](),
        enqueue=lambda fire: enqueue_hook_job(
            fire, db_path=db_path, busy_timeout_ms=busy_timeout_ms, user_id=user_id
        ),
    )
