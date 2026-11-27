"""The activity watcher: a hook fires once each time a window starts to match,
from Zanei's records read by fixed rules, and only while something listens."""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from pantaray_agents.hooks.definitions import FieldMatch
from pantaray_agents.local_runtime.context.source_gate import ActiveSource, SourceGate
from pantaray_agents.local_runtime.context.source_protocol import (
    GapResponse,
    PageReadRequest,
    PageResponse,
)
from pantaray_agents.local_runtime.hooks.activity_watch import run_activity_watch_once
from pantaray_agents.local_runtime.hooks.agent_tasks import new_task_id, save_task
from pantaray_agents.local_runtime.hooks.tasks import TaskSchedule, build_task
from pantaray_agents.schema.context_source import SourceBinding

from . import test_tasks
from .test_tasks import USER, _conversation, _World

# The tasks' database, owner and conversation.
world = test_tasks.world

T0 = datetime(2026, 11, 27, 10, 0, tzinfo=UTC)
ZOOM = "us.zoom.xos"
CHROME = "com.google.Chrome"
MEETING = (FieldMatch(field="window_title", op="contains", values=("Zoom Meeting",)),)
MEET_TAB = (FieldMatch(field="window_title", op="contains", values=("Meet - ",)),)


@dataclass
class _Zanei:
    """A recorder store the watcher reads in pages, as Zanei answers."""

    records: list[dict[str, object]] = field(default_factory=list)
    calls: int = 0
    gap: bool = False

    def record(
        self,
        at: timedelta,
        title: str | None,
        *,
        window_id: int | None = 1,
        bundle_id: str = ZOOM,
        event_type: str = "window.title",
    ) -> None:
        def text(value: str | None) -> dict[str, str]:
            return (
                {"kind": "absent"}
                if value is None
                else {"kind": "value", "text": value}
            )

        sequence = len(self.records) + 1
        self.records.append(
            {
                "append_sequence": sequence,
                "id": f"event-{sequence}",
                "ts": (T0 + at).isoformat().replace("+00:00", "Z"),
                "source": text("window"),
                "event_type": text(event_type),
                "bundle_id": text(bundle_id),
                "app_name": text(bundle_id.rsplit(".", 1)[-1]),
                "pid": 7,
                "window_title": text(title),
                "window_id": window_id,
            }
        )

    async def read_page(
        self, _source: ActiveSource, request: PageReadRequest
    ) -> PageResponse | GapResponse:
        self.calls += 1
        head = str(len(self.records))
        if self.gap:
            self.gap = False
            return GapResponse.model_validate(
                {
                    "protocol_version": 1,
                    "kind": "gap",
                    "store_identity": "store-1",
                    "reason": "retention_or_deletion",
                    "affected_range": {"after": 0, "through": 0},
                    "resume_cursor": "0",
                    "upper_bound": head,
                }
            )
        after = 0 if request.cursor is None else int(request.cursor)
        page = self.records[after : after + request.limit]
        through = after + len(page)
        return PageResponse.model_validate(
            {
                "protocol_version": 1,
                "kind": "page",
                "store_identity": "store-1",
                "observations": tuple(page),
                "next_cursor": str(through),
                "upper_bound": head,
                "has_more": through < len(self.records),
                "coverage": {"after": after, "through": through},
            }
        )


def _gate() -> SourceGate:
    gate = SourceGate()

    async def activate() -> None:
        async with gate.turn():
            gate.activate(
                SourceBinding(
                    user_id=USER,
                    epoch=UUID(int=1),
                    policy_revision="policy-1",
                    store_id="store-1",
                    protocol_version=1,
                )
            )

    asyncio.run(activate())
    return gate


def _watch(
    world: _World,
    zanei: _Zanei,
    at: timedelta,
    *,
    ready: bool = True,
    gate: SourceGate | None = None,
) -> None:
    """One tick of the watcher; a new gate unless given, as after a restart."""
    run_activity_watch_once(
        db_path=world.db,
        busy_timeout_ms=1_000,
        user_id=USER,
        reader=zanei,
        gate=_gate() if gate is None else gate,
        recording_ready=lambda: ready,
        now=lambda: T0 + at,
    )


def _hook(
    world: _World,
    match: tuple[FieldMatch, ...] = MEETING,
    request: str = "request-1",
    message: str = "Take notes for the meeting.",
) -> str:
    if world.action_id is None:
        world.action_id = _conversation(world)
    hook_id = new_task_id(request)
    schedule = TaskSchedule(at=None, every=None, offset=None, until=None, match=match)
    assert (
        save_task(
            db_path=world.db,
            busy_timeout_ms=1_000,
            user_id=USER,
            action_id=world.action_id,
            change="create",
            task=build_task(hook_id, schedule, message),
            new_conversation=None,
            now=T0,
        )
        is None
    )
    return hook_id


def _fires(world: _World, hook_id: str) -> int:
    return len(_fired_titles(world, hook_id))


def _fired_titles(world: _World, hook_id: str) -> list[str]:
    with closing(sqlite3.connect(world.db)) as connection:
        return [
            str(row[0])
            for row in connection.execute(
                "SELECT json_extract(payload_json, '$.window_title') FROM hook_inbox"
                " WHERE hook_id = ? AND event_name = 'activity' ORDER BY delivery_id",
                (hook_id,),
            )
        ]


def _started(world: _World) -> tuple[str, _Zanei]:
    """A meeting hook the watcher has begun watching, at T0."""
    hook_id = _hook(world)
    zanei = _Zanei()
    _watch(world, zanei, timedelta(0))
    return hook_id, zanei


def test_a_meeting_left_untouched_for_an_hour_fires_once(world: _World) -> None:
    # Catches silence read as the meeting ending.
    hook_id, zanei = _started(world)
    zanei.record(timedelta(minutes=1), "Zoom Meeting")
    _watch(world, zanei, timedelta(minutes=1, seconds=30))
    _watch(world, zanei, timedelta(minutes=61))
    zanei.record(timedelta(minutes=62), "Zoom Meeting")
    _watch(world, zanei, timedelta(minutes=62, seconds=30))

    assert _fires(world, hook_id) == 1


def test_working_in_another_app_mid_meeting_does_not_fire_again(
    world: _World,
) -> None:
    # Catches a switch of app read as the meeting ending.
    hook_id, zanei = _started(world)
    zanei.record(timedelta(minutes=1), "Zoom Meeting")
    for minute in range(2, 33):
        zanei.record(
            timedelta(minutes=minute), "notes.md", window_id=9, bundle_id="dev.editor"
        )
    zanei.record(timedelta(minutes=33), None, window_id=None, event_type="app.activate")
    zanei.record(timedelta(minutes=33, seconds=1), "Zoom Meeting")
    _watch(world, zanei, timedelta(minutes=34))

    assert _fires(world, hook_id) == 1


def test_a_new_window_or_an_app_quit_starts_a_new_match(world: _World) -> None:
    # Catches a match counted per app rather than per window, and a quit
    # that does not end the app's windows' matches.
    hook_id, zanei = _started(world)
    zanei.record(timedelta(minutes=1), "Zoom Meeting")
    zanei.record(timedelta(minutes=2), "Zoom Workplace")
    zanei.record(timedelta(minutes=4), "Zoom Meeting", window_id=2)
    zanei.record(timedelta(minutes=5), None, window_id=None, event_type="app.terminate")
    zanei.record(timedelta(minutes=6), "Zoom Meeting", window_id=2)
    _watch(world, zanei, timedelta(minutes=7))

    assert _fires(world, hook_id) == 3


def test_a_window_matches_again_only_after_ten_quiet_minutes(world: _World) -> None:
    # Catches a short look away firing again, and a long one never ending.
    hook_id, zanei = _started(world)
    zanei.record(timedelta(minutes=1), "Zoom Meeting")
    zanei.record(timedelta(minutes=2), "Zoom Workplace")
    zanei.record(timedelta(minutes=10), "Zoom Meeting")
    _watch(world, zanei, timedelta(minutes=10, seconds=30))
    assert _fires(world, hook_id) == 1

    zanei.record(timedelta(minutes=11), "Zoom Workplace")
    zanei.record(timedelta(minutes=15), "Zoom Workplace")
    _watch(world, zanei, timedelta(minutes=15, seconds=30))
    zanei.record(timedelta(minutes=22), "Zoom Meeting")
    _watch(world, zanei, timedelta(minutes=22, seconds=30))
    assert _fires(world, hook_id) == 2


def test_a_window_matching_when_the_hook_is_saved_does_not_fire_it(
    world: _World,
) -> None:
    # Catches a new hook firing on a meeting that was already going.
    other = _hook(world, (FieldMatch(field="bundle_id", op="equals", values=("x",)),))
    zanei = _Zanei()
    _watch(world, zanei, timedelta(0))
    zanei.record(timedelta(minutes=1), "Zoom Meeting")
    _watch(world, zanei, timedelta(minutes=1, seconds=30))

    hook_id = _hook(world, request="request-2")
    _watch(world, zanei, timedelta(minutes=5))
    zanei.record(timedelta(minutes=8), "Zoom Meeting")
    _watch(world, zanei, timedelta(minutes=8, seconds=30))

    assert _fires(world, hook_id) == 0
    assert _fires(world, other) == 0


def test_a_restart_mid_meeting_does_not_fire_again(world: _World) -> None:
    # Catches match state kept only in memory.
    hook_id, zanei = _started(world)
    zanei.record(timedelta(minutes=1), "Zoom Meeting")
    _watch(world, zanei, timedelta(minutes=1, seconds=30))
    restarted = _Zanei(records=list(zanei.records))
    restarted.record(timedelta(minutes=20), "Zoom Meeting")
    _watch(world, restarted, timedelta(minutes=20, seconds=30))

    assert _fires(world, hook_id) == 1


@pytest.mark.parametrize("window_seen_first", [True, False])
def test_a_browser_navigation_without_a_window_id_fires_its_window_once(
    world: _World, window_seen_first: bool
) -> None:
    # Zanei records a navigation without a window id: keyed by the app, the
    # tab's title would fire once there and again on the window, also when
    # the navigation comes before any record of the window.
    hook_id = _hook(world, MEET_TAB)
    zanei = _Zanei()
    _watch(world, zanei, timedelta(0))
    if window_seen_first:
        zanei.record(timedelta(minutes=1), "Inbox", window_id=5, bundle_id=CHROME)
    zanei.record(
        timedelta(minutes=2),
        "Meet - abc-defg-hij",
        window_id=None,
        bundle_id=CHROME,
        event_type="browser.navigate",
    )
    zanei.record(
        timedelta(minutes=2, seconds=1),
        "Meet - abc-defg-hij",
        window_id=5,
        bundle_id=CHROME,
    )
    _watch(world, zanei, timedelta(minutes=3))

    assert _fires(world, hook_id) == 1


def test_a_gap_resumes_from_the_head_without_firing_what_it_skipped(
    world: _World,
) -> None:
    hook_id, zanei = _started(world)
    zanei.record(timedelta(minutes=1), "Zoom Meeting")
    zanei.gap = True
    _watch(world, zanei, timedelta(minutes=2))
    zanei.record(timedelta(minutes=3), "Zoom Meeting", window_id=2)
    _watch(world, zanei, timedelta(minutes=4))

    assert _fires(world, hook_id) == 1


@pytest.mark.parametrize("listening", [True, False])
def test_zanei_is_not_called_unless_recording_is_ready_and_a_hook_listens(
    world: _World, listening: bool
) -> None:
    # Also catches window titles kept past their week while nothing is read.
    if listening:
        _hook(world)
    with closing(sqlite3.connect(world.db)) as connection, connection:
        connection.execute(
            "INSERT INTO activity_windows(user_id, window_key, seen_at)"
            " VALUES (?, 'window:1', '2026-11-19T10:00:00.000Z')",
            (USER,),
        )
    zanei = _Zanei()
    _watch(world, zanei, timedelta(0), ready=not listening)

    assert zanei.calls == 0
    with closing(sqlite3.connect(world.db)) as connection:
        assert connection.execute("SELECT * FROM activity_windows").fetchall() == []


def test_the_first_read_knows_meetings_going_on_and_fires_ones_just_begun(
    world: _World,
) -> None:
    # Catches a first read that drops the store: a meeting begun between the
    # save and the first tick never fires, one already going fires later.
    zanei = _Zanei()
    zanei.record(-timedelta(minutes=20), "Zoom Meeting A")
    hook_id = _hook(world)
    zanei.record(-timedelta(seconds=10), "Zoom Meeting B", window_id=2)
    _watch(world, zanei, timedelta(0))
    zanei.record(timedelta(minutes=5), "Zoom Meeting A")
    zanei.record(timedelta(minutes=5), "Zoom Meeting B", window_id=2)
    _watch(world, zanei, timedelta(minutes=6))

    assert _fired_titles(world, hook_id) == ["Zoom Meeting B"]


def test_a_hook_switched_back_on_ignores_meetings_begun_while_it_was_off(
    world: _World,
) -> None:
    hook_id, zanei = _started(world)
    world.enable(False, hook_id)
    zanei.record(timedelta(minutes=1), "Zoom Meeting")
    _watch(world, zanei, timedelta(minutes=2))
    world.enable(True, hook_id)
    _watch(world, zanei, timedelta(minutes=3))
    zanei.record(timedelta(minutes=4), "Zoom Meeting")
    _watch(world, zanei, timedelta(minutes=5))

    assert zanei.calls == 3
    assert _fires(world, hook_id) == 0


def test_revoking_recorded_context_mid_read_ends_the_tick(world: _World) -> None:
    # Catches the cancel a revoke sends escaping the tick: it is not an
    # Exception, so it would stop the worker's periodic loop.
    hook_id, zanei = _started(world)
    zanei.record(timedelta(minutes=1), "Zoom Meeting")
    gate = _gate()

    class _Revoked(_Zanei):
        async def read_page(
            self, source: ActiveSource, request: PageReadRequest
        ) -> PageResponse | GapResponse:
            async with gate.track(source):
                async with gate.turn():
                    gate.revoke(USER)
                await asyncio.sleep(10)
            raise AssertionError("the revoke cancels the read")

    _watch(world, _Revoked(), timedelta(minutes=2), gate=gate)
    _watch(world, zanei, timedelta(minutes=3))

    assert _fires(world, hook_id) == 1
