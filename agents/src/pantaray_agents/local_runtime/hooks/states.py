"""The hooks' next fire times, and when a just-saved task first fires."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from contextlib import closing
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

from pantaray_agents.hooks.definitions import At, Every, Trigger
from pantaray_agents.hooks.schedule import next_occurrence_after
from pantaray_agents.local_runtime.activity_summary_schedule import iso_z, parse_iso_z
from pantaray_agents.local_runtime.storage.migrations.connection import (
    configure_connection,
)
from pantaray_agents.local_runtime.storage.users import ensure_user_row
from pantaray_agents.utils.local_time import local_zone_name


class UnknownLocalZoneError(RuntimeError):
    """A hook fires on local time, but the OS zone has no IANA name."""


_NO_TASKS: Mapping[str, int] = {}


class SqliteHookStateStore:
    """The hooks' next fire times for the owner the engine fires them for.

    A built-in's time is a row of ``hook_states``; a task's is on its own row,
    and belongs to the version of the task ``task_versions`` names: a write
    for a task replaced or deleted since changes nothing.
    """

    def __init__(
        self,
        *,
        db_path: Path,
        busy_timeout_ms: int,
        user_id: str,
        task_versions: Mapping[str, int] = _NO_TASKS,
    ) -> None:
        self._db_path = db_path
        self._busy_timeout_ms = busy_timeout_ms
        self._user_id = user_id
        self._task_versions = task_versions

    def _connect(self) -> closing[sqlite3.Connection]:
        connection = sqlite3.connect(self._db_path)
        configure_connection(connection, self._busy_timeout_ms)
        return closing(connection)

    def read_next_fire_times(self) -> Mapping[str, datetime | None]:
        # A task always has a row: saving it sets its first fire time.
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT hook_id, next_fire_at FROM hook_states WHERE user_id = ?
                   UNION ALL
                   SELECT hook_id, next_fire_at FROM user_hooks WHERE user_id = ?""",
                (self._user_id, self._user_id),
            ).fetchall()
        return {
            str(hook_id): None if next_fire_at is None else parse_iso_z(next_fire_at)
            for hook_id, next_fire_at in rows
        }

    def write_next_fire_time(self, hook_id: str, next_fire_at: datetime | None) -> None:
        at = None if next_fire_at is None else iso_z(next_fire_at)
        version = self._task_versions.get(hook_id)
        with self._connect() as connection, connection:
            if version is not None:
                connection.execute(
                    """UPDATE user_hooks SET next_fire_at = ?
                       WHERE user_id = ? AND hook_id = ? AND version = ?""",
                    (at, self._user_id, hook_id, version),
                )
                return
            # A hook can fire before anything else wrote the owner's row.
            ensure_user_row(connection, user_id=self._user_id)
            connection.execute(
                """INSERT INTO hook_states(user_id, hook_id, next_fire_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(user_id, hook_id)
                   DO UPDATE SET next_fire_at = excluded.next_fire_at""",
                (self._user_id, hook_id, at),
            )


def local_zone() -> tzinfo:
    name = local_zone_name()
    if name is None:
        raise UnknownLocalZoneError("the OS time zone has no IANA name")
    return ZoneInfo(name)


def first_fire_after(trigger: Trigger, moment: datetime) -> datetime | None:
    """When a hook saved at ``moment`` first fires: an at at its instant, an
    every at its first occurrence after ``moment``; None for an on trigger."""

    if isinstance(trigger, At):
        return trigger.at
    if isinstance(trigger, Every):
        return next_occurrence_after(
            trigger, moment, local_zone() if trigger.tz == "local" else UTC
        )
    return None
