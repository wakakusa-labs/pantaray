"""Watch Zanei's records for the on activity hooks, by fixed rules, no model.

Every tick reads what Zanei recorded since the watcher's own position, while
at least one activity hook is on and recording is ready; otherwise Zanei is
not called. Each window's last recorded form says whether it matches a hook,
and stands while nothing more is recorded of it. A hook fires when a window
starts to match it: not when one already matched as the hook was saved, and
not again until that window has been seen not matching for ``QUIET_PERIOD``
or its app was recorded quitting. A window is keyed by Zanei's window id; a
record without one belongs to the window its app last showed with an id, or
to the app when it has shown none.

The position, the window forms, the match state and the events a page fires
commit in one transaction, so a restart neither loses nor repeats a fire. A gap
in the store, a first read, or more than a tick's pages resume from the
store's head without reading back.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, Protocol

from pantaray_agents.hooks.definitions import (
    EventValue,
    FieldMatch,
    fields_match,
    hook_has_ended,
)
from pantaray_agents.local_runtime.activity_summary_schedule import parse_iso_z
from pantaray_agents.local_runtime.context.source_gate import (
    ActiveSource,
    SourceGate,
    SourceInvalidated,
)
from pantaray_agents.local_runtime.context.source_protocol import (
    MAX_PAGE_ROWS,
    GapResponse,
    Observation,
    ObservationText,
    PageReadRequest,
    PageResponse,
    ValueText,
)
from pantaray_agents.local_runtime.context.source_reader import ReadResult
from pantaray_agents.local_runtime.runtime.utc_timestamps import (
    format_utc_iso,
    parse_utc_iso,
)
from pantaray_agents.local_runtime.storage.migrations.connection import (
    configure_connection,
)
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction
from pantaray_agents.local_runtime.storage.users import ensure_user_row

from .controls import read_tasks
from .events import ActivityStarted
from .inbox import record_event_for

logger = logging.getLogger(__name__)

ACTIVITY_WATCH_INTERVAL_SECONDS: Final[float] = 30.0
# How long a window seen not matching must stay so before its match ends.
QUIET_PERIOD: Final = timedelta(minutes=10)
# A tick reads at most this many pages; a longer backlog (after a long time
# with no activity hook) is skipped to the store's head.
PAGES_PER_TICK: Final = 8
# A window not recorded for this long is forgotten, with its matches.
# Design limit: a meeting window silent a week fires again when next seen.
WINDOW_FORGET_AFTER: Final = timedelta(days=7)
_APP_QUIT: Final = "app.terminate"


class PageReader(Protocol):
    async def read_page(
        self, source: ActiveSource, request: PageReadRequest
    ) -> ReadResult: ...


@dataclass(frozen=True, slots=True)
class _Hook:
    hook_id: str
    version: int
    match: tuple[FieldMatch, ...]


def _connect(db_path: Path, busy_timeout_ms: int) -> closing[sqlite3.Connection]:
    connection = sqlite3.connect(db_path)
    configure_connection(connection, busy_timeout_ms)
    return closing(connection)


def _listening(
    connection: sqlite3.Connection, *, user_id: str, now: datetime
) -> tuple[_Hook, ...]:
    """The activity hooks that may fire: on, not stopped, not past until."""

    return tuple(
        _Hook(
            hook_id=row.task.hook_id, version=row.version, match=row.task.schedule.match
        )
        for row in read_tasks(connection, user_id=user_id)
        if row.task.schedule.match
        and row.enabled
        and row.run.stopped_reason is None
        and not hook_has_ended(row.hook, now)
    )


def run_activity_watch_once(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    reader: PageReader,
    gate: SourceGate,
    recording_ready: Callable[[], bool],
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    with _connect(db_path, busy_timeout_ms) as connection:
        if not _listening(connection, user_id=user_id, now=now()):
            return
    if not recording_ready():
        return
    asyncio.run(
        _watch(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            user_id=user_id,
            reader=reader,
            gate=gate,
            now=now,
        )
    )


async def _watch(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    reader: PageReader,
    gate: SourceGate,
    now: Callable[[], datetime],
) -> None:
    async with gate.turn():
        source = gate.current(user_id)
    if source is None:
        return
    for page_number in range(PAGES_PER_TICK):
        with _connect(db_path, busy_timeout_ms) as connection:
            cursor = _position(connection, user_id=user_id)
        try:
            result = await reader.read_page(
                source,
                PageReadRequest(cursor=cursor, upper_bound=None, limit=MAX_PAGE_ROWS),
            )
        except asyncio.CancelledError:
            # The reader tracks its read, so revoking access to the recorded
            # context cancels this task; that ends the tick, as no permit is
            # left to read with. Any other cancel goes on.
            if gate.current(user_id) == source:
                raise
            task = asyncio.current_task()
            assert task is not None  # A coroutine under asyncio.run runs in a Task.
            task.uncancel()
            return
        if not isinstance(result, (PageResponse, GapResponse)):
            logger.warning("activity watch read failed: %s", type(result).__name__)
            return
        observations: Sequence[Observation] = ()
        next_cursor = result.upper_bound
        more = False
        # A first read starts at the oldest record and a gap has none: both
        # resume from the head, so nothing recorded before is fired late.
        if isinstance(result, PageResponse) and cursor is not None:
            observations = result.observations
            more = result.has_more and page_number + 1 < PAGES_PER_TICK
            if more or not result.has_more:
                next_cursor = result.next_cursor
        try:
            async with gate.guard(source):
                with _connect(db_path, busy_timeout_ms) as connection:
                    with immediate_transaction(connection):
                        _commit_page(
                            connection,
                            user_id=user_id,
                            observations=observations,
                            cursor=next_cursor,
                            now=now(),
                        )
        # Revoked after the read: what it read is not kept.
        except SourceInvalidated:
            return
        if not more:
            return


def _position(connection: sqlite3.Connection, *, user_id: str) -> str | None:
    row = connection.execute(
        "SELECT cursor FROM activity_watch_positions WHERE user_id = ?", (user_id,)
    ).fetchone()
    return None if row is None else str(row[0])


def _commit_page(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    observations: Sequence[Observation],
    cursor: str,
    now: datetime,
) -> None:
    hooks = _listening(connection, user_id=user_id, now=now)
    fires_from = _watch_hooks(connection, user_id=user_id, hooks=hooks, now=now)
    for observation in observations:
        _apply(
            connection,
            user_id=user_id,
            observation=observation,
            hooks=hooks,
            fires_from=fires_from,
        )
    _forget_stale_windows(connection, user_id=user_id, now=now)
    ensure_user_row(connection, user_id=user_id)
    connection.execute(
        """INSERT INTO activity_watch_positions(user_id, cursor) VALUES (?, ?)
           ON CONFLICT(user_id) DO UPDATE SET cursor = excluded.cursor""",
        (user_id, cursor),
    )


def _watch_hooks(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    hooks: Sequence[_Hook],
    now: datetime,
) -> Mapping[str, datetime]:
    """When each hook fires from; a hook new to the watcher, or replaced since,
    is watched afresh, its matches taken from the windows' last forms.

    Watching starts a tick before now, so a window that starts to match
    between the hook's save and this tick still fires it. A hook that no
    longer listens is no longer watched: switched back on, it starts afresh.
    """

    watched = {
        str(hook_id): (int(version), parse_utc_iso(str(watched_from)))
        for hook_id, version, watched_from in connection.execute(
            """SELECT hook_id, version, watched_from FROM activity_hook_watches
               WHERE user_id = ?""",
            (user_id,),
        )
    }
    listening = {hook.hook_id for hook in hooks}
    connection.executemany(
        "DELETE FROM activity_hook_watches WHERE user_id = ? AND hook_id = ?",
        [(user_id, hook_id) for hook_id in watched.keys() - listening],
    )
    start = now - timedelta(seconds=ACTIVITY_WATCH_INTERVAL_SECONDS)
    fires_from: dict[str, datetime] = {}
    windows: list[tuple[str, Mapping[str, EventValue]]] | None = None
    for hook in hooks:
        current = watched.get(hook.hook_id)
        if current is not None and current[0] == hook.version:
            fires_from[hook.hook_id] = current[1]
            continue
        if windows is None:
            windows = _window_forms(connection, user_id=user_id)
        connection.execute(
            "DELETE FROM activity_hook_watches WHERE user_id = ? AND hook_id = ?",
            (user_id, hook.hook_id),
        )
        connection.execute(
            """INSERT INTO activity_hook_watches(user_id, hook_id, version, watched_from)
               VALUES (?, ?, ?, ?)""",
            (user_id, hook.hook_id, hook.version, format_utc_iso(start)),
        )
        connection.executemany(
            """INSERT INTO activity_hook_windows(user_id, hook_id, window_key)
               VALUES (?, ?, ?)""",
            [
                (user_id, hook.hook_id, key)
                for key, form in windows
                if fields_match(hook.match, form)
            ],
        )
        fires_from[hook.hook_id] = start
    return fires_from


def _window_forms(
    connection: sqlite3.Connection, *, user_id: str
) -> list[tuple[str, Mapping[str, EventValue]]]:
    # Design limit: read whole when a hook is new; the table holds the windows
    # of a week, a few thousand rows.
    return [
        (
            str(key),
            {"app_name": app_name, "bundle_id": bundle_id, "window_title": title},
        )
        for key, app_name, bundle_id, title in connection.execute(
            """SELECT window_key, app_name, bundle_id, window_title
               FROM activity_windows WHERE user_id = ?""",
            (user_id,),
        )
    ]


def _apply(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    observation: Observation,
    hooks: Sequence[_Hook],
    fires_from: Mapping[str, datetime],
) -> None:
    seen_at = parse_iso_z(observation.ts)
    bundle_id = _text(observation.bundle_id)
    if _text(observation.event_type) == _APP_QUIT:
        if bundle_id is not None:
            _forget_app(connection, user_id=user_id, bundle_id=bundle_id)
        return
    key = _window_key(
        connection,
        user_id=user_id,
        window_id=observation.window_id,
        bundle_id=bundle_id,
    )
    if key is None:
        return
    event = ActivityStarted(
        app_name=_text(observation.app_name),
        bundle_id=bundle_id,
        window_title=_text(observation.window_title),
    )
    connection.execute(
        """INSERT INTO activity_windows(user_id, window_key, bundle_id, app_name,
             window_title, seen_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(user_id, window_key) DO UPDATE SET
             bundle_id = excluded.bundle_id, app_name = excluded.app_name,
             window_title = excluded.window_title, seen_at = excluded.seen_at""",
        (
            user_id,
            key,
            bundle_id,
            event.app_name,
            event.window_title,
            format_utc_iso(seen_at),
        ),
    )
    for hook in hooks:
        if _starts_match(
            connection,
            user_id=user_id,
            hook=hook,
            key=key,
            event=event,
            seen_at=seen_at,
        ) and (seen_at >= fires_from[hook.hook_id]):
            logger.info("activity hook matched: hook=%s", hook.hook_id)
            record_event_for(
                connection,
                user_id=user_id,
                hook_ids=(hook.hook_id,),
                event=event,
                occurred_at=format_utc_iso(seen_at),
            )


def _starts_match(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    hook: _Hook,
    key: str,
    event: ActivityStarted,
    seen_at: datetime,
) -> bool:
    """Move the window's match for the hook on to this form; whether a match
    starts with it."""

    row = connection.execute(
        """SELECT unmatched_since FROM activity_hook_windows
           WHERE user_id = ? AND hook_id = ? AND window_key = ?""",
        (user_id, hook.hook_id, key),
    ).fetchone()
    since = None if row is None or row[0] is None else parse_utc_iso(str(row[0]))
    ended = since is not None and seen_at - since >= QUIET_PERIOD
    params = (user_id, hook.hook_id, key)
    if fields_match(hook.match, event.payload()):
        connection.execute(
            """INSERT INTO activity_hook_windows(user_id, hook_id, window_key)
               VALUES (?, ?, ?)
               ON CONFLICT(user_id, hook_id, window_key)
               DO UPDATE SET unmatched_since = NULL""",
            params,
        )
        return row is None or ended
    if ended:
        connection.execute(
            """DELETE FROM activity_hook_windows
               WHERE user_id = ? AND hook_id = ? AND window_key = ?""",
            params,
        )
    elif row is not None and since is None:
        connection.execute(
            """UPDATE activity_hook_windows SET unmatched_since = ?
               WHERE user_id = ? AND hook_id = ? AND window_key = ?""",
            (format_utc_iso(seen_at), *params),
        )
    return False


def _window_key(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    window_id: int | None,
    bundle_id: str | None,
) -> str | None:
    if window_id is not None:
        return f"window:{window_id}"
    if bundle_id is None:
        return None
    # Zanei records a browser's navigation, and some apps' input, without a
    # window id; keyed by the app, the same meeting would fire twice.
    row = connection.execute(
        """SELECT window_key FROM activity_windows
           WHERE user_id = ? AND bundle_id = ? AND window_key LIKE 'window:%'
           ORDER BY seen_at DESC LIMIT 1""",
        (user_id, bundle_id),
    ).fetchone()
    return f"app:{bundle_id}" if row is None else str(row[0])


def _forget_app(
    connection: sqlite3.Connection, *, user_id: str, bundle_id: str
) -> None:
    """The app quit: its windows' matches end, and their forms go."""

    connection.execute(
        """DELETE FROM activity_hook_windows WHERE user_id = ? AND window_key IN (
             SELECT window_key FROM activity_windows
             WHERE user_id = ? AND bundle_id = ?)""",
        (user_id, user_id, bundle_id),
    )
    connection.execute(
        "DELETE FROM activity_windows WHERE user_id = ? AND bundle_id = ?",
        (user_id, bundle_id),
    )


def _forget_stale_windows(
    connection: sqlite3.Connection, *, user_id: str, now: datetime
) -> None:
    cutoff = format_utc_iso(now - WINDOW_FORGET_AFTER)
    connection.execute(
        """DELETE FROM activity_hook_windows WHERE user_id = ? AND window_key IN (
             SELECT window_key FROM activity_windows
             WHERE user_id = ? AND seen_at < ?)""",
        (user_id, user_id, cutoff),
    )
    connection.execute(
        "DELETE FROM activity_windows WHERE user_id = ? AND seen_at < ?",
        (user_id, cutoff),
    )


def _text(value: ObservationText) -> str | None:
    """A recorded text; absent, or left out as too long, is none."""

    return value.text if isinstance(value, ValueText) else None


__all__ = [
    "ACTIVITY_WATCH_INTERVAL_SECONDS",
    "QUIET_PERIOD",
    "run_activity_watch_once",
]
