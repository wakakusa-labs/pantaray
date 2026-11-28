"""The microphone hooks: a hook fires once in each use of the audio input an
app kept for the hold, from Zanei's records read by the activity watcher."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import timedelta

import pytest

from pantaray_agents.hooks.definitions import FieldMatch
from pantaray_agents.local_runtime.hooks.agent_tasks import new_task_id, save_task
from pantaray_agents.local_runtime.hooks.tasks import TaskSchedule, build_task

from . import test_tasks
from .test_activity_watch import CHROME, T0, ZOOM, _watch, _Zanei
from .test_tasks import USER, _conversation, _World

world = test_tasks.world

ZOOM_APP = (FieldMatch(field="bundle_id", op="equals", values=(ZOOM,)),)
MEET = (
    FieldMatch(field="bundle_id", op="equals", values=(CHROME,)),
    FieldMatch(field="window_title", op="contains", values=("Meet",)),
)


def _hook(
    world: _World,
    match: tuple[FieldMatch, ...] = ZOOM_APP,
    request: str = "request-1",
    until: timedelta | None = None,
) -> str:
    if world.action_id is None:
        world.action_id = _conversation(world)
    hook_id = new_task_id(request)
    schedule = TaskSchedule(
        at=None,
        every=None,
        offset=None,
        until=None if until is None else T0 + until,
        on="microphone",
        match=match,
    )
    assert (
        save_task(
            db_path=world.db,
            busy_timeout_ms=1_000,
            user_id=USER,
            action_id=world.action_id,
            change="create",
            task=build_task(hook_id, schedule, "Take notes for the meeting."),
            new_conversation=None,
            now=T0,
        )
        is None
    )
    return hook_id


def _fired(world: _World, hook_id: str) -> list[tuple[str, str]]:
    """Each fire's app and window title."""
    with closing(sqlite3.connect(world.db)) as connection:
        return [
            (str(bundle_id), str(title))
            for bundle_id, title in connection.execute(
                "SELECT json_extract(payload_json, '$.bundle_id'),"
                " json_extract(payload_json, '$.window_title') FROM hook_inbox"
                " WHERE hook_id = ? AND event_name = 'microphone' ORDER BY delivery_id",
                (hook_id,),
            )
        ]


def _start(zanei: _Zanei, at: timedelta, app: str = ZOOM, title: str = "Zoom") -> None:
    zanei.record(at, title, bundle_id=app, event_type="audio_input.start")


def _stop(zanei: _Zanei, at: timedelta, app: str = ZOOM) -> None:
    zanei.record(at, None, window_id=None, bundle_id=app, event_type="audio_input.stop")


def _started(
    world: _World, match: tuple[FieldMatch, ...] = ZOOM_APP
) -> tuple[str, _Zanei]:
    hook_id = _hook(world, match)
    zanei = _Zanei()
    _watch(world, zanei, timedelta(0))
    return hook_id, zanei


@pytest.mark.parametrize("match", [ZOOM_APP, ()])
def test_a_use_kept_past_the_hold_fires_once(
    world: _World, match: tuple[FieldMatch, ...]
) -> None:
    # Catches a hold never decided on a tick with no new record, and a use
    # that fires on every tick it lasts; with no match, any app's use fires.
    hook_id, zanei = _started(world, match)
    _start(zanei, timedelta(minutes=1))
    _watch(world, zanei, timedelta(minutes=1, seconds=2))
    assert _fired(world, hook_id) == []

    _watch(world, zanei, timedelta(minutes=1, seconds=32))
    _watch(world, zanei, timedelta(minutes=61))

    assert _fired(world, hook_id) == [(ZOOM, "Zoom")]


@pytest.mark.parametrize("match", [ZOOM_APP, ()])
def test_a_dictation_stopped_within_the_hold_does_not_fire(
    world: _World, match: tuple[FieldMatch, ...]
) -> None:
    # The stop reaches the store a batch after the start: a tick reading the
    # start alone must not take the clock for a held stretch.
    hook_id, zanei = _started(world, match)
    _start(zanei, timedelta(minutes=1))
    _watch(world, zanei, timedelta(minutes=1, seconds=8))
    _stop(zanei, timedelta(minutes=1, seconds=3))
    _watch(world, zanei, timedelta(minutes=1, seconds=40))

    assert _fired(world, hook_id) == []


def test_a_start_and_stop_read_in_one_page_fire_when_held(world: _World) -> None:
    # Catches a hold decided only by the clock, which never sees a stretch
    # that has already stopped.
    hook_id, zanei = _started(world)
    _start(zanei, timedelta(minutes=1))
    _stop(zanei, timedelta(minutes=1, seconds=20))
    _watch(world, zanei, timedelta(minutes=2))

    assert len(_fired(world, hook_id)) == 1


def test_a_restart_within_a_minute_is_the_same_use(world: _World) -> None:
    # A device switch or a rejoin mid-meeting, and Zanei starting again
    # mid-use with the watcher restarted, fire nothing more.
    hook_id, zanei = _started(world)
    _start(zanei, timedelta(minutes=1))
    _watch(world, zanei, timedelta(minutes=2))
    _stop(zanei, timedelta(minutes=10))
    _start(zanei, timedelta(minutes=10, seconds=40))
    _watch(world, zanei, timedelta(minutes=11))
    _start(zanei, timedelta(minutes=20))
    _watch(world, zanei, timedelta(minutes=21))

    assert len(_fired(world, hook_id)) == 1


@pytest.mark.parametrize(
    ("end", "gap"),
    [("audio_input.stop", timedelta(minutes=3)), ("app.terminate", timedelta(0))],
)
def test_a_use_after_a_quiet_minute_or_a_quit_fires_again(
    world: _World, end: str, gap: timedelta
) -> None:
    # Back-to-back meetings: catches a use that never ends.
    hook_id, zanei = _started(world)
    _start(zanei, timedelta(minutes=1))
    _watch(world, zanei, timedelta(minutes=2))
    zanei.record(
        timedelta(minutes=30), None, window_id=None, bundle_id=ZOOM, event_type=end
    )
    _start(zanei, timedelta(minutes=30, seconds=1) + gap, title="Zoom Meeting 2")
    _watch(world, zanei, timedelta(minutes=35))

    assert _fired(world, hook_id) == [(ZOOM, "Zoom"), (ZOOM, "Zoom Meeting 2")]


def test_the_app_and_the_window_when_use_started_choose_the_hook(
    world: _World,
) -> None:
    # Catches matching on the wrong app's use, or on a title the start did
    # not carry.
    zoom = _hook(world)
    meet = _hook(world, MEET, request="request-2")
    zanei = _Zanei()
    _watch(world, zanei, timedelta(0))
    _start(zanei, timedelta(minutes=1), CHROME, "Inbox - Gmail")
    _stop(zanei, timedelta(minutes=2), CHROME)
    _start(zanei, timedelta(minutes=5), CHROME, "Meet - abc-defg-hij")
    _watch(world, zanei, timedelta(minutes=6))

    assert _fired(world, zoom) == []
    assert _fired(world, meet) == [(CHROME, "Meet - abc-defg-hij")]


def test_a_use_going_on_when_the_hook_is_saved_does_not_fire_it(
    world: _World,
) -> None:
    other = _hook(world, MEET)
    zanei = _Zanei()
    _start(zanei, -timedelta(minutes=5))
    _watch(world, zanei, timedelta(0))
    hook_id = _hook(world, request="request-2")
    _watch(world, zanei, timedelta(minutes=1))
    _stop(zanei, timedelta(minutes=2))
    _start(zanei, timedelta(minutes=2, seconds=30))
    _watch(world, zanei, timedelta(minutes=3))

    assert _fired(world, hook_id) == []
    assert _fired(world, other) == []


@pytest.mark.parametrize("state", ["off", "stopped", "ended"])
def test_a_hook_off_stopped_or_past_until_neither_reads_nor_fires(
    world: _World, state: str
) -> None:
    hook_id = _hook(world, until=timedelta(minutes=1) if state == "ended" else None)
    if state == "off":
        world.enable(False, hook_id)
    elif state == "stopped":
        with closing(sqlite3.connect(world.db)) as connection, connection:
            connection.execute(
                "UPDATE user_hooks SET stopped_reason = 'turn_failed' WHERE hook_id = ?",
                (hook_id,),
            )
    zanei = _Zanei()
    _start(zanei, timedelta(minutes=2))
    _watch(world, zanei, timedelta(minutes=3))

    assert zanei.calls == 0
    assert _fired(world, hook_id) == []
