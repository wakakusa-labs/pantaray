"""Watch Zanei's records for the on activity and microphone hooks, by fixed
rules, no model.

Every tick reads what Zanei recorded since the watcher's own position, while
such a hook is on and recording is ready; otherwise Zanei is not called. The
audio input records go to ``microphone_watch``; the rest are windows'.
Each window's last recorded form says whether it matches a hook, and stands
while nothing more is recorded of it. A hook fires when a window starts to
match it, and not again until that window has been seen not matching for
``QUIET_PERIOD`` or its app was recorded quitting. Records from before a hook
was watched only build its matches, so a window already matching never fires
it; the first read goes through the whole store for that.

The position, the window forms, the match state and the events a page fires
commit in one transaction, so a restart neither loses nor repeats a fire.
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

from . import microphone_watch
from .controls import read_tasks
from .events import ActivityStarted, MicrophoneStarted
from .inbox import record_event_for
from .microphone_watch import AUDIO_INPUT_RECORDS, MicrophoneHook

logger = logging.getLogger(__name__)

ACTIVITY_WATCH_INTERVAL_SECONDS: Final[float] = 30.0
# How long a window seen not matching must stay so before its match ends.
QUIET_PERIOD: Final = timedelta(minutes=10)
# A tick reads at most this many pages, and the next one goes on from there.
# Design limit: the store keeps two days, a few hundred pages at most, so a
# first read takes a few ticks; raise this if one takes more than ten.
PAGES_PER_TICK: Final = 32
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
    event: str
    match: tuple[FieldMatch, ...]


def _connect(db_path: Path, busy_timeout_ms: int) -> closing[sqlite3.Connection]:
    connection = sqlite3.connect(db_path)
    configure_connection(connection, busy_timeout_ms)
    return closing(connection)


def _listening(
    connection: sqlite3.Connection, *, user_id: str, now: datetime
) -> tuple[_Hook, ...]:
    """The activity and microphone hooks that may fire: on, not stopped, not
    past until."""

    return tuple(
        _Hook(
            hook_id=row.task.hook_id,
            version=row.version,
            event=row.task.schedule.on,
            match=row.task.schedule.match,
        )
        for row in read_tasks(connection, user_id=user_id)
        if row.task.schedule.on is not None
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
        # Every tick, so the titles kept go when they are due even while
        # nothing is read.
        with immediate_transaction(connection):
            _forget_stale_windows(connection, user_id=user_id, now=now())
            microphone_watch.forget_stale_uses(connection, user_id=user_id, now=now())
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
    for _page in range(PAGES_PER_TICK):
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
        gap = isinstance(result, GapResponse)
        if isinstance(result, PageResponse):
            observations, next_cursor = result.observations, result.next_cursor
            more = result.has_more
        # From no position, a gap only says the oldest records expired; from
        # one, the records it skips are not read back.
        elif cursor is None:
            next_cursor, more = result.resume_cursor, True
        else:
            next_cursor, more = result.upper_bound, False
        try:
            async with gate.guard(source):
                with _connect(db_path, busy_timeout_ms) as connection:
                    with immediate_transaction(connection):
                        _commit_page(
                            connection,
                            user_id=user_id,
                            observations=observations,
                            cursor=next_cursor,
                            caught_up=not more,
                            gap=gap,
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
    caught_up: bool,
    gap: bool,
    now: datetime,
) -> None:
    hooks = _listening(connection, user_id=user_id, now=now)
    if gap:
        # A skipped stop would leave a use running, held by the clock: the
        # uses start over from the next start.
        microphone_watch.forget_uses(connection, user_id=user_id)
    fires_from = _watch_hooks(connection, user_id=user_id, hooks=hooks, now=now)
    window_hooks = [hook for hook in hooks if hook.event == ActivityStarted.NAME]
    microphone_hooks = [
        MicrophoneHook(
            hook_id=hook.hook_id, match=hook.match, fires_from=fires_from[hook.hook_id]
        )
        for hook in hooks
        if hook.event == MicrophoneStarted.NAME
    ]
    for observation in observations:
        event_type = _text(observation.event_type)
        if event_type in AUDIO_INPUT_RECORDS or event_type == _APP_QUIT:
            microphone_watch.apply_record(
                connection,
                user_id=user_id,
                event_type=str(event_type),
                record=MicrophoneStarted(
                    app_name=_text(observation.app_name),
                    bundle_id=_text(observation.bundle_id),
                    window_title=_text(observation.window_title),
                ),
                at=parse_iso_z(observation.ts),
                hooks=microphone_hooks,
            )
        if event_type not in AUDIO_INPUT_RECORDS:
            _apply(
                connection,
                user_id=user_id,
                observation=observation,
                hooks=window_hooks,
                fires_from=fires_from,
            )
    if caught_up:
        microphone_watch.fire_held_by_clock(
            connection, user_id=user_id, hooks=microphone_hooks, now=now
        )
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
    is watched afresh, an activity hook's matches taken from the windows' last
    forms.

    Watching starts a tick before now, so a window that starts to match
    between the hook's save and this tick still fires it. A hook that no
    longer listens is no longer watched; switching one on or off drops its
    watch where the switch is written (migration 0155), so a hook switched
    back on starts afresh even before a tick saw it off.
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
        connection.execute(
            "DELETE FROM activity_hook_watches WHERE user_id = ? AND hook_id = ?",
            (user_id, hook.hook_id),
        )
        connection.execute(
            """INSERT INTO activity_hook_watches(user_id, hook_id, version, watched_from)
               VALUES (?, ?, ?, ?)""",
            (user_id, hook.hook_id, hook.version, format_utc_iso(start)),
        )
        fires_from[hook.hook_id] = start
        # A microphone hook needs no seed: a use begun before ``start`` never
        # fires it.
        if hook.event != ActivityStarted.NAME:
            continue
        if windows is None:
            # Design limit: read whole when a hook is new; the table holds a
            # week of windows, a few thousand rows.
            windows = [
                (
                    str(key),
                    {"app_name": app, "bundle_id": bundle, "window_title": title},
                )
                for key, app, bundle, title in connection.execute(
                    """SELECT window_key, app_name, bundle_id, window_title
                       FROM activity_windows WHERE user_id = ?""",
                    (user_id,),
                )
            ]
        connection.executemany(
            """INSERT INTO activity_hook_windows(user_id, hook_id, window_key)
               VALUES (?, ?, ?)""",
            [
                (user_id, hook.hook_id, key)
                for key, form in windows
                if fields_match(hook.match, form)
            ],
        )
    return fires_from


def _apply(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    observation: Observation,
    hooks: Sequence[_Hook],
    fires_from: Mapping[str, datetime],
) -> None:
    seen_at = parse_iso_z(observation.ts)
    seen = format_utc_iso(seen_at)
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
        (user_id, key, bundle_id, event.app_name, event.window_title, seen),
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
                occurred_at=seen,
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
        key = f"window:{window_id}"
        if bundle_id is not None:
            _hand_app_to_window(
                connection, user_id=user_id, bundle_id=bundle_id, key=key
            )
        return key
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


def _hand_app_to_window(
    connection: sqlite3.Connection, *, user_id: str, bundle_id: str, key: str
) -> None:
    """What an app had before it showed a window id becomes its window's, so
    a match begun on a navigation does not start again on that window."""

    app_key = f"app:{bundle_id}"
    # The matches follow the form (ON UPDATE CASCADE). A window that already
    # has a form keeps its own, and the app's goes.
    connection.execute(
        """UPDATE OR IGNORE activity_windows SET window_key = ?
           WHERE user_id = ? AND window_key = ?""",
        (key, user_id, app_key),
    )
    connection.execute(
        "DELETE FROM activity_windows WHERE user_id = ? AND window_key = ?",
        (user_id, app_key),
    )


def _forget_app(
    connection: sqlite3.Connection, *, user_id: str, bundle_id: str
) -> None:
    """The app quit: its windows' forms go, and their matches end with them."""

    connection.execute(
        "DELETE FROM activity_windows WHERE user_id = ? AND bundle_id = ?",
        (user_id, bundle_id),
    )


def _forget_stale_windows(
    connection: sqlite3.Connection, *, user_id: str, now: datetime
) -> None:
    connection.execute(
        "DELETE FROM activity_windows WHERE user_id = ? AND seen_at < ?",
        (user_id, format_utc_iso(now - WINDOW_FORGET_AFTER)),
    )


def _text(value: ObservationText) -> str | None:
    """A recorded text; absent, or left out as too long, is none."""

    return value.text if isinstance(value, ValueText) else None
