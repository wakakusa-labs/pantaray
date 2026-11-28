"""The on microphone hooks, decided from Zanei's audio input records.

Read in the activity watcher's tick and transaction, from the same pages. An
app is in use from its ``audio_input.start`` until its ``audio_input.stop``;
a hook fires once the app has kept using the input for ``MIC_HOLD`` and the
record that began that stretch holds the hook's match. A start within
``MIC_END_QUIET`` of the stop goes on with the same use; a later one, or one
after the app quit, begins a new use. A hook fires at most once in a use, and
never in one begun before it was watched.

Whether a stretch was held is read from the records' times; while the
watcher has read everything, a stretch with no stop yet is held by the clock,
less the time Zanei takes to commit a stop.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from pantaray_agents.hooks.definitions import FieldMatch, fields_match
from pantaray_agents.local_runtime.runtime.utc_timestamps import (
    format_utc_iso,
    parse_utc_iso,
)

from .events import MicrophoneStarted
from .inbox import record_event_for

logger = logging.getLogger(__name__)

# How long an app must keep using the input for a hook to fire: a dictation
# or a voice search stops sooner (the requester's decision, 2026-11-28).
MIC_HOLD: Final = timedelta(seconds=5)
# A start this soon after the stop is the same use: a device switch, or
# rejoining the call. Design limit: a rejoin after longer fires again, and
# back-to-back meetings closer than this fire once.
MIC_END_QUIET: Final = timedelta(seconds=60)
# Zanei commits its records in batches every 5 s, so a stop may not be
# readable yet when the clock says the stretch was held.
RECORDING_LAG: Final = timedelta(seconds=10)
# A use not recorded for this long is forgotten, with its app's window title.
# Design limit: a stop lost while Zanei was down keeps the app in use, so its
# next use does not fire until then.
USE_FORGET_AFTER: Final = timedelta(days=7)
AUDIO_INPUT_START: Final = "audio_input.start"
AUDIO_INPUT_STOP: Final = "audio_input.stop"
AUDIO_INPUT_RECORDS: Final = frozenset({AUDIO_INPUT_START, AUDIO_INPUT_STOP})


@dataclass(frozen=True, slots=True)
class MicrophoneHook:
    hook_id: str
    match: tuple[FieldMatch, ...]
    # A use begun before this never fires the hook.
    fires_from: datetime


@dataclass(frozen=True, slots=True)
class _Use:
    record: MicrophoneStarted
    use_started_at: datetime
    stretch_started_at: datetime
    stopped_at: datetime | None


def apply_record(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    event_type: str,
    record: MicrophoneStarted,
    at: datetime,
    hooks: Sequence[MicrophoneHook],
) -> None:
    """Move the app's use on to its start or stop of the input, or to its
    quit (any other ``event_type``), recorded at ``at``."""

    bundle_id = record.bundle_id
    if bundle_id is None:
        return
    use = _read_use(connection, user_id=user_id, bundle_id=bundle_id)
    if use is not None:
        _fire_if_held(connection, user_id=user_id, use=use, by=at, hooks=hooks)
    key = {"user_id": user_id, "bundle_id": bundle_id}
    if event_type == AUDIO_INPUT_STOP:
        if use is not None and use.stopped_at is None:
            connection.execute(
                """UPDATE microphone_uses SET stopped_at = :at
                   WHERE user_id = :user_id AND bundle_id = :bundle_id""",
                {**key, "at": format_utc_iso(at)},
            )
        return
    if event_type != AUDIO_INPUT_START:
        connection.execute(
            """DELETE FROM microphone_uses
               WHERE user_id = :user_id AND bundle_id = :bundle_id""",
            key,
        )
        return
    # A start while in use is Zanei starting again mid-use: the same stretch.
    if use is not None and use.stopped_at is None:
        return
    stretch = {
        **key,
        "app_name": record.app_name,
        "window_title": record.window_title,
        "at": format_utc_iso(at),
    }
    if use is not None and use.stopped_at is not None:
        if at - use.stopped_at <= MIC_END_QUIET:
            connection.execute(
                """UPDATE microphone_uses SET app_name = :app_name,
                     window_title = :window_title, stretch_started_at = :at,
                     stopped_at = NULL
                   WHERE user_id = :user_id AND bundle_id = :bundle_id""",
                stretch,
            )
            return
        # The use ended; the hooks it fired go with it.
        connection.execute(
            """DELETE FROM microphone_uses
               WHERE user_id = :user_id AND bundle_id = :bundle_id""",
            key,
        )
    connection.execute(
        """INSERT INTO microphone_uses(user_id, bundle_id, app_name, window_title,
             use_started_at, stretch_started_at)
           VALUES (:user_id, :bundle_id, :app_name, :window_title, :at, :at)""",
        stretch,
    )


def fire_held_by_clock(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    hooks: Sequence[MicrophoneHook],
    now: datetime,
) -> None:
    """Fire for the apps still using the input since before the hold, once
    every record Zanei has committed was read."""

    by = now - RECORDING_LAG
    rows = connection.execute(
        """SELECT bundle_id FROM microphone_uses
           WHERE user_id = ? AND stopped_at IS NULL AND stretch_started_at <= ?""",
        (user_id, format_utc_iso(by - MIC_HOLD)),
    ).fetchall()
    for (bundle_id,) in rows:
        use = _read_use(connection, user_id=user_id, bundle_id=str(bundle_id))
        assert use is not None  # Read in the same transaction.
        _fire_if_held(connection, user_id=user_id, use=use, by=by, hooks=hooks)


def forget_stale_uses(
    connection: sqlite3.Connection, *, user_id: str, now: datetime
) -> None:
    connection.execute(
        """DELETE FROM microphone_uses
           WHERE user_id = ? AND COALESCE(stopped_at, stretch_started_at) < ?""",
        (user_id, format_utc_iso(now - USE_FORGET_AFTER)),
    )


def _fire_if_held(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    use: _Use,
    by: datetime,
    hooks: Sequence[MicrophoneHook],
) -> None:
    """Fire each hook the use's stretch holds, if it was in use until ``by``
    for the hold and the hook has not fired in the use."""

    if use.stopped_at is not None or by - use.stretch_started_at < MIC_HOLD:
        return
    held_at = format_utc_iso(use.stretch_started_at + MIC_HOLD)
    for hook in hooks:
        if use.use_started_at < hook.fires_from or not fields_match(
            hook.match, use.record.payload()
        ):
            continue
        fired = connection.execute(
            """INSERT INTO microphone_hook_uses(user_id, hook_id, bundle_id)
               VALUES (?, ?, ?) ON CONFLICT DO NOTHING""",
            (user_id, hook.hook_id, use.record.bundle_id),
        )
        if fired.rowcount == 1:
            logger.info("microphone hook matched: hook=%s", hook.hook_id)
            record_event_for(
                connection,
                user_id=user_id,
                hook_ids=(hook.hook_id,),
                event=use.record,
                occurred_at=held_at,
            )


def _read_use(
    connection: sqlite3.Connection, *, user_id: str, bundle_id: str
) -> _Use | None:
    row = connection.execute(
        """SELECT app_name, window_title, use_started_at, stretch_started_at,
                  stopped_at
           FROM microphone_uses WHERE user_id = ? AND bundle_id = ?""",
        (user_id, bundle_id),
    ).fetchone()
    if row is None:
        return None
    app_name, window_title, use_started_at, stretch_started_at, stopped_at = row
    return _Use(
        record=MicrophoneStarted(
            app_name=app_name, bundle_id=bundle_id, window_title=window_title
        ),
        use_started_at=parse_utc_iso(str(use_started_at)),
        stretch_started_at=parse_utc_iso(str(stretch_started_at)),
        stopped_at=None if stopped_at is None else parse_utc_iso(str(stopped_at)),
    )
